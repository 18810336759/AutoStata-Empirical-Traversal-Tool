"""Batch identity, immutable run evidence, and staged effect output publishing."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import platform
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

import pandas as pd
import yaml

from path_utils import get_project_output_root, get_sample_output_dir, resolve_config_path, check_output_overwrite


SCHEMA_VERSION = "2.0-run-2"
MODULES = ("main_effect", "mediation_effect", "moderation_effect")
SHARED_CODE = ("other/preprocessing.py", "other/path_utils.py", "other/result_schema.py",
               "other/config_validation.py", "_internal/run_provenance.py",
               "_internal/statistics_output.py", "_internal/hypothesis_output.py",
               "_internal/progress.py")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      default=str).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True,
                                   default=str), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def read_manifest(config: Mapping[str, Any]) -> dict[str, Any]:
    path = get_project_output_root(config) / "run_manifest.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {
        "schema_version": SCHEMA_VERSION, "current_run_id": None, "batches": {}}


def common_identity(config: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude project/module names and display labels; include every public computation input."""
    data_path = resolve_config_path(config["data"]["input_file"], config, must_exist=True)
    public = {key: config.get(key) for key in (
        "config_version", "continuous_variable_settings", "preprocessing", "controls",
        "combination_execution", "model_settings")}
    public["panel"] = config.get("data", {}).get("panel", {})
    public["input_format"] = data_path.suffix.lower()
    shared_code = Path(__file__).resolve().parents[1] / "other" / "preprocessing.py"
    payload = {"data_sha256": file_sha256(data_path), "public_computation": public,
               "preprocessing_code_sha256": file_sha256(shared_code)}
    return {"common_fingerprint": _digest(payload), "data_sha256": payload["data_sha256"],
            "data_file": str(data_path), "public_computation": public,
            "preprocessing_code_sha256": payload["preprocessing_code_sha256"]}


def environment_info(script: Path) -> dict[str, Any]:
    packages = {}
    for name in ("pandas", "numpy", "statsmodels", "scipy", "PyYAML", "openpyxl"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    skill_dir = Path(__file__).resolve().parents[1]
    code = {name: file_sha256(skill_dir / name) for name in SHARED_CODE}
    code[str(script.resolve().relative_to(skill_dir))] = file_sha256(script)
    return {"python": sys.version.split()[0], "executable": sys.executable,
            "platform": platform.platform(), "packages": packages,
            "runner_file": str(script.resolve()), "runner_sha256": file_sha256(script),
            "code_sha256": code, "code_fingerprint": _digest(code)}


def _new_run_id(requested: Any, existing: Mapping[str, Any]) -> str:
    prefix = str(requested).strip() if requested else datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not prefix or any(c in prefix for c in "/\\\0"):
        raise ValueError("run_settings.run_id 含非法字符")
    candidate = prefix
    while candidate in existing:
        candidate = f"{prefix}-{uuid.uuid4().hex[:8]}"
    return candidate


def _inventory(directory: Path) -> list[dict[str, Any]]:
    return [{"path": str(path.relative_to(directory)), "bytes": path.stat().st_size,
             "sha256": file_sha256(path)}
            for path in sorted(directory.rglob("*")) if path.is_file() and path.name != "run_record.json"]


def _snapshot(stage: Path, global_cfg: Mapping[str, Any], module_cfg: Mapping[str, Any],
              module: str) -> dict[str, Any]:
    folder = stage / "config_snapshot"
    folder.mkdir(parents=True, exist_ok=True)
    result = {}
    for name, cfg in (("analysis_config.yaml", global_cfg), (f"{module}.yaml", module_cfg)):
        source = Path(cfg["_config_path"])
        target = folder / name
        shutil.copyfile(source, target)
        result[name] = {"path": str(target.relative_to(stage)), "sha256": file_sha256(target),
                        "source_path": str(source)}
    return result


def _confirm_inputs(config: Mapping[str, Any], module_config: Mapping[str, Any],
                    identity: Mapping[str, Any], snapshots: Mapping[str, Any], module: str,
                    stage: Path) -> None:
    """Stop publication if a source changed between loading, fitting and output."""
    for name, cfg in (("analysis_config.yaml", config), (f"{module}.yaml", module_config)):
        source = Path(cfg["_config_path"])
        if file_sha256(source) != snapshots[name]["sha256"]:
            raise RuntimeError(f"结果发布已取消：{name} 在回归期间被修改（{source}）。"
                               "请停止编辑该文件，用最终 YAML 重新运行；旧结果未被覆盖。")
        captured = yaml.safe_load((stage / snapshots[name]["path"]).read_text(encoding="utf-8"))
        loaded = {key: value for key, value in cfg.items() if not key.startswith("_")}
        if captured != loaded:
            changed = sorted(key for key in set(captured) | set(loaded)
                             if captured.get(key) != loaded.get(key))
            raise RuntimeError(f"结果发布已取消：运行中的 {name} 配置被程序修改，"
                               f"涉及顶层字段：{changed}。请检查配置处理逻辑；旧结果未被覆盖。")
    if common_identity(config)["common_fingerprint"] != identity["common_fingerprint"]:
        raise RuntimeError("结果发布已取消：输入数据或公共计算设定在回归期间发生变化。"
                           "请固定输入数据和全局 YAML 后重跑；旧结果未被覆盖。")


def _save_manifest(config: Mapping[str, Any], manifest: dict[str, Any]) -> None:
    _atomic_json(get_project_output_root(config) / "run_manifest.json", manifest)


def _publish_directory(stage: Path, final: Path, instance_id: str) -> Path | None:
    backup = final.with_name(f".{final.name}-backup-{instance_id}")
    previous_exists = final.exists()
    try:
        if previous_exists:
            os.replace(final, backup)
        os.replace(stage, final)
    except Exception:
        if backup.exists() and not final.exists():
            os.replace(backup, final)
        raise
    return backup if previous_exists else None


def _restore_directory(final: Path, backup: Path | None) -> None:
    if final.exists():
        shutil.rmtree(final)
    if backup is not None and backup.exists():
        os.replace(backup, final)


def run_effect(module: str, config: dict[str, Any], module_config: dict[str, Any],
               script: Path, execute: Callable[[Path], None]) -> dict[str, Any]:
    """Execute into a sibling directory; publish only complete output and evidence."""
    if module not in MODULES:
        raise ValueError(module)
    final = get_sample_output_dir(config) / module
    check_output_overwrite(final, config, module_config)
    identity = common_identity(config)
    manifest = read_manifest(config)
    current = manifest.get("batches", {}).get(manifest.get("current_run_id"))
    if current and current["common_fingerprint"] == identity["common_fingerprint"]:
        run_id = current["run_id"]
        batch = current
    else:
        run_id = _new_run_id(config.get("run_settings", {}).get("run_id"), manifest["batches"])
        for old in manifest["batches"].values():
            if old.get("status") == "current":
                old["status"] = "superseded"
        batch = {"run_id": run_id, "status": "current", "created_at": _now(),
                 **identity, "modules": {}, "synthesis": {"status": "not_run"}}
        manifest["batches"][run_id] = batch
        manifest["current_run_id"] = run_id
        _save_manifest(config, manifest)
    run_instance_id = uuid.uuid4().hex
    env = environment_info(script)
    module_hash = file_sha256(Path(module_config["_config_path"]))
    revision = _digest({"module_config_sha256": module_hash,
                        "code_fingerprint": env["code_fingerprint"],
                        "environment": {"python": env["python"], "packages": env["packages"]}})
    provenance = {"run_id": run_id, "run_instance_id": run_instance_id,
                  "common_fingerprint": identity["common_fingerprint"],
                  "data_sha256": identity["data_sha256"], "module_revision": revision}
    config["_run_provenance"] = provenance
    final.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f".{module}-staging-", dir=final.parent))
    try:
        snapshots = _snapshot(stage, config, module_config, module)
        if snapshots[f"{module}.yaml"]["sha256"] != module_hash:
            raise RuntimeError("模块 YAML 在读取后发生变化，请重试")
        execute(stage)
        _confirm_inputs(config, module_config, identity, snapshots, module, stage)
        if environment_info(script)["code_fingerprint"] != env["code_fingerprint"]:
            raise RuntimeError("回归期间程序代码发生变化，请重跑")
        files = _inventory(stage)
        produced = {item["path"] for item in files}
        synthesis_ready = {"standardized_results.csv", "specification_index.csv",
                           "hypothesis_results.csv"} <= produced
        definitions_path = stage / "preprocessing_definitions.json"
        if not definitions_path.is_file():
            raise RuntimeError("效应模块未输出结构化预处理定义")
        definitions = json.loads(definitions_path.read_text(encoding="utf-8"))
        processing_evidence = {"path": definitions_path.name,
                               "sha256": file_sha256(definitions_path),
                               "schema_version": definitions.get("schema_version"),
                               "definition_count": len(definitions.get("definitions", {}))}
        record = {"schema_version": SCHEMA_VERSION, "module": module, "status": "success",
                  "started_in_batch": run_id, "completed_at": _now(), **provenance,
                  "module_config_sha256": module_hash,
                  "global_config_sha256": snapshots["analysis_config.yaml"]["sha256"],
                  "environment": env,
                  "snapshots": snapshots, "preprocessing": processing_evidence,
                  "synthesis_ready": synthesis_ready,
                  "synthesis_status": ("ready" if synthesis_ready else
                                       "machine_outputs_disabled_or_incomplete"),
                  "files": files}
        _atomic_json(stage / "run_record.json", record)
        backup = _publish_directory(stage, final, run_instance_id)
        previous_entry = batch["modules"].get(module)
        previous_synthesis = batch.get("synthesis")
        batch["modules"][module] = {"status": "success", "run_instance_id": run_instance_id,
                                    "module_revision": revision, "module_config_sha256": module_hash,
                                    "synthesis_ready": synthesis_ready,
                                    "record_path": str(final / "run_record.json"),
                                    "output_dir": str(final)}
        batch["synthesis"] = {"status": "stale", "reason": f"{module} 结果已更新"}
        try:
            _save_manifest(config, manifest)
        except Exception:
            _restore_directory(final, backup)
            if previous_entry is None:
                batch["modules"].pop(module, None)
            else:
                batch["modules"][module] = previous_entry
            batch["synthesis"] = previous_synthesis
            raise
        if backup is not None:
            shutil.rmtree(backup)
        return record
    except Exception as exc:
        batch.setdefault("failed_attempts", []).append({"module": module, "at": _now(),
            "run_instance_id": run_instance_id, "error": f"{type(exc).__name__}: {exc}"})
        _save_manifest(config, manifest)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def run_synthesis(config: dict[str, Any], synthesis_config: dict[str, Any],
                  script: Path, final: Path, included_sources: Mapping[str, Path],
                  execute: Callable[[Path], None]) -> dict[str, Any]:
    """Publish consolidation only for a stable, fully identified batch."""
    check_output_overwrite(final, config, synthesis_config)
    manifest = read_manifest(config)
    batch = manifest.get("batches", {}).get(manifest.get("current_run_id"))
    if not batch or batch["common_fingerprint"] != common_identity(config)["common_fingerprint"]:
        raise RuntimeError("当前数据或公共配置没有对应的回归批次")
    included = {}
    input_files = {}
    for module, source in included_sources.items():
        source = Path(source)
        entry = batch["modules"].get(module)
        if not entry or entry.get("status") != "success":
            raise RuntimeError(f"{module} 没有当前批次的成功运行记录")
        included[module] = entry["run_instance_id"]
        record_path = Path(entry["record_path"])
        effect_record = json.loads(record_path.read_text(encoding="utf-8"))
        listed = {item["path"]: item["sha256"] for item in effect_record["files"]}
        machine_files = {}
        for name in ("standardized_results.csv", "specification_index.csv", "hypothesis_results.csv"):
            path = source.parent / name
            if listed.get(name) != file_sha256(path):
                raise RuntimeError(f"{module} 的机器衔接文件未登记或已改动: {name}")
            machine_files[name] = {"path": str(path), "sha256": listed[name]}
        if source.resolve() != (source.parent / "standardized_results.csv").resolve():
            raise RuntimeError(f"{module} 的整合输入不是运行记录中的标准化结果")
        snapshots = {}
        for name, item in effect_record["snapshots"].items():
            path = source.parent / item["path"]
            actual = file_sha256(path)
            if actual != item["sha256"]:
                raise RuntimeError(f"{module} 的历史配置快照已被改动: {name}")
            snapshots[name] = {"path": str(path), "sha256": actual}
        input_files[module] = {"path": str(source), "sha256": file_sha256(source),
                               "machine_files": machine_files,
                               "record_path": str(record_path),
                               "record_sha256": file_sha256(record_path),
                               "snapshots": snapshots}
    config_hash = file_sha256(Path(config["_config_path"]))
    synthesis_hash = file_sha256(Path(synthesis_config["_config_path"]))
    for name, cfg in (("analysis_config.yaml", config), ("synthesis.yaml", synthesis_config)):
        captured = yaml.safe_load(Path(cfg["_config_path"]).read_text(encoding="utf-8"))
        loaded = {key: value for key, value in cfg.items() if not key.startswith("_")}
        if captured != loaded:
            raise RuntimeError(f"{name} 与整合时加载的配置不一致，请重跑")
    env = environment_info(script)
    instance_id = uuid.uuid4().hex
    final.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".result_synthesis-staging-", dir=final.parent))
    try:
        execute(stage)
        latest = read_manifest(config)
        current = latest.get("batches", {}).get(latest.get("current_run_id"))
        if (not current or current["run_id"] != batch["run_id"]
                or current["common_fingerprint"] != common_identity(config)["common_fingerprint"]
                or any(current.get("modules", {}).get(module, {}).get("run_instance_id") != number
                       for module, number in included.items())
                or any(file_sha256(Path(item["path"])) != item["sha256"]
                       for item in input_files.values())
                or any(file_sha256(Path(file["path"])) != file["sha256"]
                       for item in input_files.values() for file in item["machine_files"].values())
                or any(file_sha256(Path(item["record_path"])) != item["record_sha256"]
                       or any(file_sha256(Path(snapshot["path"])) != snapshot["sha256"]
                              for snapshot in item["snapshots"].values())
                       for item in input_files.values())
                or file_sha256(Path(config["_config_path"])) != config_hash
                or file_sha256(Path(synthesis_config["_config_path"])) != synthesis_hash
                or environment_info(script)["code_fingerprint"] != env["code_fingerprint"]):
            raise RuntimeError("结果整合期间输入批次或配置发生变化，请重跑")
        files = _inventory(stage)
        record = {"schema_version": SCHEMA_VERSION, "status": "success",
                  "run_id": batch["run_id"], "run_instance_id": instance_id,
                  "completed_at": _now(), "common_fingerprint": batch["common_fingerprint"],
                  "input_modules": included, "input_files": input_files,
                  "synthesis_config_sha256": synthesis_hash,
                  "synthesis_input_config_sha256": config_hash,
                  "environment": env, "files": files}
        _atomic_json(stage / "run_record.json", record)
        backup = _publish_directory(stage, final, instance_id)
        previous_synthesis = current.get("synthesis")
        current["synthesis"] = {"status": "success", "run_instance_id": instance_id,
                                "record_path": str(final / "run_record.json"),
                                "output_dir": str(final), "input_modules": included}
        try:
            _save_manifest(config, latest)
        except Exception:
            _restore_directory(final, backup)
            current["synthesis"] = previous_synthesis
            raise
        if backup is not None:
            shutil.rmtree(backup)
        return record
    except Exception as exc:
        latest = read_manifest(config)
        current = latest.get("batches", {}).get(latest.get("current_run_id"))
        if current and current["run_id"] == batch["run_id"]:
            current.setdefault("failed_attempts", []).append({"module": "result_synthesis",
                "at": _now(), "run_instance_id": instance_id,
                "error": f"{type(exc).__name__}: {exc}"})
            _save_manifest(config, latest)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def specification_id(row: Mapping[str, Any], provenance: Mapping[str, Any]) -> str:
    """Public specification: same data and public rules produce the same cross-module key."""
    controls = str(row.get("纳入的控制变量") or "").split()
    key = {"common_fingerprint": provenance["common_fingerprint"],
           "threshold": str(row.get("样本门槛")), "treatment": str(row.get("处理方案")),
           "controls": sorted(set(controls)), "fixed_effects": str(row.get("固定效应设定")),
           "cluster": str(row.get("聚类标准误设定"))}
    return "spec_" + _digest(key)[:24]


def annotate(frame: pd.DataFrame, config: Mapping[str, Any]) -> pd.DataFrame:
    provenance = config.get("_run_provenance")
    if not provenance or frame.empty:
        return frame
    out = frame.copy()
    out["specification_id"] = out.apply(lambda row: specification_id(row, provenance), axis=1)
    for name, value in provenance.items():
        out[name] = value
    return out


def verify_effect(config: Mapping[str, Any], module: str, result_file: Path,
                  current_module_path: Path) -> tuple[dict[str, Any], Path, Path]:
    """Reject historical/mixed output; return both YAMLs captured at fitting time."""
    manifest = read_manifest(config)
    batch = manifest.get("batches", {}).get(manifest.get("current_run_id"))
    if not batch or batch["common_fingerprint"] != common_identity(config)["common_fingerprint"]:
        raise RuntimeError("数据或公共计算配置已变化；请重跑启用的效应模块")
    entry = batch.get("modules", {}).get(module)
    if not entry or entry.get("status") != "success":
        raise RuntimeError(f"{module} 没有当前批次的成功运行记录，请重跑")
    record_path = Path(entry["record_path"])
    if record_path.parent.resolve() != result_file.parent.resolve():
        raise RuntimeError(f"{module} 结果位置与当前运行清单不一致")
    record = json.loads(record_path.read_text(encoding="utf-8"))
    if (record.get("status") != "success" or record.get("module") != module
            or record["run_id"] != batch["run_id"] or record["run_instance_id"] != entry["run_instance_id"]
            or record["common_fingerprint"] != batch["common_fingerprint"]
            or record["data_sha256"] != batch["data_sha256"]
            or record["module_revision"] != entry["module_revision"]):
        raise RuntimeError(f"{module} 运行记录与当前批次不一致")
    if file_sha256(current_module_path) != record["module_config_sha256"]:
        raise RuntimeError(f"{module} YAML 已更改但结果尚未重跑")
    module_snapshot = result_file.parent / record["snapshots"][f"{module}.yaml"]["path"]
    global_snapshot = result_file.parent / record["snapshots"]["analysis_config.yaml"]["path"]
    for snapshot, name in ((module_snapshot, f"{module}.yaml"),
                           (global_snapshot, "analysis_config.yaml")):
        if file_sha256(snapshot) != record["snapshots"][name]["sha256"]:
            raise RuntimeError(f"{module} 回归时配置快照校验失败: {name}")
    listed = {item["path"]: item["sha256"] for item in record["files"]}
    preprocessing = record.get("preprocessing", {})
    recipe_path = result_file.parent / preprocessing.get("path", "preprocessing_definitions.json")
    if (not recipe_path.is_file() or file_sha256(recipe_path) != preprocessing.get("sha256")
            or listed.get(preprocessing.get("path")) != preprocessing.get("sha256")):
        raise RuntimeError(f"{module} 预处理定义文件缺失或已被改动")
    relative = str(result_file.relative_to(result_file.parent))
    if relative not in listed or file_sha256(result_file) != listed[relative]:
        raise RuntimeError(f"{module} 结果文件不在运行清单中或已被改动")
    return record, module_snapshot, global_snapshot
