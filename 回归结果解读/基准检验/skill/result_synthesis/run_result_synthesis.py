#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
run_result_synthesis.py

结果整合与设定曲线模块。

本模块是主效应、中介效应、调节效应运行完成后的后处理模块：
- 读取全局 analysis_config.yaml
- 读取 result_synthesis/synthesis.yaml
- 检查本轮启用模块、模块配置和输出文件是否完整
- 仅读取已验证的 standardized_results.csv、specification_index.csv 与 hypothesis_results.csv
- 检查用户登记假设、模块 YAML 假设和实际输出假设是否一致

输出包括：
- require / full / strong / weak 设定级综合表与多工作表 Excel
- 逐假设状态和逐模型审计明细
- 设定曲线数据、摘要和图片
- robustness_statement.md
- display_case_index.csv / json
- summary.md 和 config_snapshot

运行：
python3 skill/result_synthesis/run_result_synthesis.py config/analysis_config.yaml

若不传入配置路径，默认读取当前基准检验目录下：
config/analysis_config.yaml
"""

from __future__ import annotations

import json
import math
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import pandas as pd
import yaml
try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


# =========================
# 基础路径
# =========================

SCRIPT_PATH = Path(__file__).resolve()
BASELINE_DIR = SCRIPT_PATH.parents[2]
PROJECT_ROOT = SCRIPT_PATH.parents[4]
DEFAULT_CONFIG_PATH = BASELINE_DIR / "config" / "analysis_config.yaml"

sys.path.insert(0, str(SCRIPT_PATH.parents[1]))
sys.path.insert(0, str(SCRIPT_PATH.parents[1] / "other"))
sys.path.insert(0, str(PROJECT_ROOT))
from 回归结果解读.基准检验.skill.other.path_utils import resolve_config_path, get_project_output_root
from 回归结果解读.基准检验.skill.other.config_validation import load_config_file, preflight_synthesis
from 回归结果解读.基准检验.skill.other.result_schema import (align_standard_columns, STANDARD_COLUMNS,
                           SPECIFICATION_INDEX_COLUMNS)
from _internal.hypothesis_output import HYPOTHESIS_COLUMNS
from _internal.run_provenance import (verify_effect, run_synthesis, file_sha256,
                                      read_manifest, common_identity, environment_info,
                                      SCHEMA_VERSION, specification_id)

EFFECT_MODULES = ["main_effect", "mediation_effect", "moderation_effect"]
RESULT_SYNTHESIS_MODULE = "result_synthesis"


# =========================
# 通用工具
# =========================

def ensure_dir(path: str | Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_config_path() -> Path:
    return DEFAULT_CONFIG_PATH


def safe_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    try:
        if pd.isna(value):
            return default
    except TypeError:
        pass
    return str(value)


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    try:
        if pd.isna(value):
            return True
    except TypeError:
        pass
    return str(value).strip() == ""


def parse_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    text = str(value).strip().lower()
    if text in {"true", "1", "yes", "y", "是", "成功", "success"}:
        return True
    if text in {"false", "0", "no", "n", "否", "failed", "fail"}:
        return False
    return None


def bool_for_filter(value: Any) -> bool:
    parsed = parse_bool(value)
    return bool(parsed) if parsed is not None else False


def parse_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except TypeError:
        pass
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


def safe_str_split(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    try:
        if pd.isna(value):
            return []
    except TypeError:
        pass
    return [v.strip() for v in str(value).split() if v.strip()]


def sanitize_name(name: Any) -> str:
    s = safe_str(name, "unknown").strip()
    for ch in [" ", "/", "\\", ":", "*", "?", "\"", "<", ">", "|", "（", "）", "(", ")", ",", "，", "；", ";"]:
        s = s.replace(ch, "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_") or "unknown"


def normalize_text_for_key(value: Any) -> str:
    if is_blank(value):
        return ""
    parts = safe_str_split(value)
    if not parts:
        return safe_str(value).strip()
    return " ".join(sorted(parts))


def list_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return " ".join([str(x).strip() for x in value if str(x).strip()])
    return str(value).strip()


def dataframe_to_markdown(df: pd.DataFrame, max_rows: int = 200) -> str:
    if df is None or df.empty:
        return "无记录。\n"
    view = df.head(max_rows).copy()
    try:
        return view.to_markdown(index=False) + "\n"
    except Exception:
        return view.to_csv(index=False)


# =========================
# YAML 与路径读取
# =========================

def load_yaml(yaml_path: str | Path) -> Dict[str, Any]:
    yaml_text = str(yaml_path).strip()
    path = Path(yaml_text).expanduser()

    if not path.is_absolute() and path.name in {"analysis_config.yaml", "analysis_config.yml"}:
        baseline_config = default_config_path()
        if baseline_config.exists():
            path = baseline_config

    candidate_paths = [path]

    if str(path).endswith(".yamly"):
        candidate_paths.append(Path(str(path)[:-1]))

    if path.suffix == "":
        candidate_paths.append(Path(str(path) + ".yaml"))
        candidate_paths.append(Path(str(path) + ".yml"))
    elif path.suffix == ".yml":
        candidate_paths.append(path.with_suffix(".yaml"))
    elif path.suffix == ".yaml":
        candidate_paths.append(path.with_suffix(".yml"))

    seen: Set[str] = set()
    unique_candidates: List[Path] = []
    for candidate in candidate_paths:
        key = str(candidate)
        if key not in seen:
            seen.add(key)
            unique_candidates.append(candidate)

    for candidate in unique_candidates:
        if candidate.exists():
            return load_config_file(candidate)

    tried = "\n".join([str(p.resolve()) for p in unique_candidates])
    raise FileNotFoundError(f"未找到配置文件。已尝试以下路径：\n{tried}")



def get_output_root(global_config: Dict[str, Any]) -> Path:
    return get_project_output_root(global_config)


def get_sample_folder_name(global_config: Dict[str, Any]) -> str:
    sample_info = global_config.get("data", {}).get("sample_info", {})
    sample_period = sample_info.get("sample_period", "unknown_period")
    sample_rules = sample_info.get("sample_rules", [])
    if not sample_rules:
        rules_text = "全样本"
    else:
        rules_text = "、".join([str(x) for x in sample_rules if str(x).strip()])
        if not rules_text:
            rules_text = "全样本"
    return f"{sample_period}（{rules_text}）"


def get_sample_output_dir(global_config: Dict[str, Any]) -> Path:
    return get_output_root(global_config) / get_sample_folder_name(global_config)


def load_module_yaml_if_exists(global_config: Dict[str, Any], module_name: str) -> Tuple[Optional[Dict[str, Any]], Optional[Path], Optional[str]]:
    module_files = global_config.get("module_config_files", {})
    raw_path = module_files.get(module_name)
    if not raw_path:
        return None, None, f"全局 YAML 缺少 module_config_files.{module_name}"

    try:
        resolved_path = resolve_config_path(raw_path, global_config, must_exist=True)
        module_config = load_yaml(resolved_path)
    except Exception as exc:
        return None, None, str(exc)

    declared_module = str(module_config.get("module_name", module_name)).strip()
    if declared_module != module_name:
        return module_config, resolved_path, f"模块 YAML 的 module_name 应为 {module_name}，当前为 {declared_module}"

    return module_config, resolved_path, None


def load_synthesis_yaml(global_config: Dict[str, Any]) -> Dict[str, Any]:
    synthesis_config, synthesis_path, err = load_module_yaml_if_exists(global_config, RESULT_SYNTHESIS_MODULE)
    if err:
        raise RuntimeError(f"无法读取 result_synthesis 配置: {err}")
    if synthesis_config is None:
        raise RuntimeError("无法读取 result_synthesis 配置")

    declared_module = str(synthesis_config.get("module_name", "")).strip()
    if declared_module != RESULT_SYNTHESIS_MODULE:
        raise ValueError(f"synthesis.yaml 的 module_name 应为 result_synthesis，当前为 {declared_module}")

    global_version = str(global_config.get("config_version", "")).strip()
    synthesis_version = str(synthesis_config.get("config_version", "")).strip()
    if global_version and synthesis_version:
        allowed_pairs = {("2.0", "2.1"), ("2.1", "2.1"), (global_version, global_version)}
        if (global_version, synthesis_version) not in allowed_pairs:
            raise ValueError(
                f"全局 YAML config_version={global_version} 与 synthesis.yaml "
                f"config_version={synthesis_version} 不兼容"
            )

    return synthesis_config


def get_synthesis_output_dir(global_config: Dict[str, Any], synthesis_config: Dict[str, Any]) -> Path:
    output_subdir = synthesis_config.get("output", {}).get("output_subdir", RESULT_SYNTHESIS_MODULE)
    return get_sample_output_dir(global_config) / output_subdir


# =========================
# 模块选择与结构核对
# =========================

def get_requested_modules(global_config: Dict[str, Any], synthesis_config: Dict[str, Any]) -> List[str]:
    input_cfg = synthesis_config.get("input", {})
    mode = input_cfg.get("module_selection_mode", "enabled_modules")

    if mode == "listed_modules":
        modules = input_cfg.get("modules", EFFECT_MODULES)
        return [m for m in modules if m in EFFECT_MODULES]

    enabled_modules = global_config.get("run_settings", {}).get("enabled_modules", {})
    return [m for m in EFFECT_MODULES if bool(enabled_modules.get(m, False))]


def build_model_structure_check(
    global_config: Dict[str, Any],
    synthesis_config: Dict[str, Any],
) -> Tuple[pd.DataFrame, Dict[str, Dict[str, Any]]]:
    input_cfg = synthesis_config.get("input", {})
    preferred_name = input_cfg.get("preferred_result_file", "standardized_results.csv")
    fallback_name = input_cfg.get("fallback_result_file", "full_results.csv")
    allow_fallback = bool(input_cfg.get("allow_fallback_to_full_results", True))
    requested_modules = set(get_requested_modules(global_config, synthesis_config))
    enabled_modules = global_config.get("run_settings", {}).get("enabled_modules", {})
    module_files = global_config.get("module_config_files", {})
    sample_output_dir = get_sample_output_dir(global_config)
    manifest = read_manifest(global_config)
    identity = common_identity(global_config)
    batch = manifest.get("batches", {}).get(manifest.get("current_run_id"))
    if batch and batch.get("common_fingerprint") != identity["common_fingerprint"]:
        batch = None

    rows: List[Dict[str, Any]] = []
    module_context: Dict[str, Dict[str, Any]] = {}

    for module_name in EFFECT_MODULES:
        module_config, module_path, config_error = load_module_yaml_if_exists(global_config, module_name)
        expected_output_dir = sample_output_dir / module_name
        entry = batch.get("modules", {}).get(module_name) if batch else None
        declared_output = Path(entry["output_dir"]) if entry and entry.get("output_dir") else None
        output_location_valid = bool(declared_output and
                                     declared_output.resolve() == expected_output_dir.resolve())
        output_dir = declared_output if output_location_valid else expected_output_dir
        preferred_path = output_dir / preferred_name
        index_path = output_dir / "specification_index.csv"
        hypothesis_path = output_dir / "hypothesis_results.csv"
        fallback_path = output_dir / fallback_name

        enabled_in_global = bool(enabled_modules.get(module_name, False))
        enabled_in_module = bool(module_config.get("enabled", False)) if module_config else False
        config_exists = bool(module_path and module_path.exists())
        output_dir_exists = output_dir.exists()
        preferred_exists = preferred_path.exists()
        index_exists = index_path.exists()
        hypothesis_exists = hypothesis_path.exists()
        fallback_exists = fallback_path.exists()

        source_file: Optional[Path] = None
        source_kind = ""
        warning_parts: List[str] = []

        if config_error:
            warning_parts.append(config_error)

        if module_name not in requested_modules:
            will_be_included = False
            warning_parts.append("本轮未请求读取该模块")
        elif not enabled_in_global:
            will_be_included = False
            warning_parts.append("全局 YAML 未启用该模块")
        elif config_error:
            will_be_included = False
            warning_parts.append("模块配置无法核验")
        elif not enabled_in_module:
            will_be_included = False
            warning_parts.append("模块 YAML 未启用该模块")
        elif not batch or not entry or entry.get("status") != "success":
            will_be_included = False
            warning_parts.append("运行清单中没有当前数据和公共设定对应的成功模块记录；目录中的旧文件不能作为输入")
        elif not output_location_valid:
            will_be_included = False
            warning_parts.append("运行清单输出位置与当前项目样本目录不一致")
        elif not entry.get("synthesis_ready"):
            will_be_included = False
            warning_parts.append("机器衔接产物缺失：运行清单标记该模块未生成完整结果")
        elif preferred_exists and index_exists and hypothesis_exists:
            will_be_included = True
            source_file = preferred_path
            source_kind = "preferred_standardized"
        else:
            will_be_included = False
            warning_parts.append("机器衔接产物缺失：需要 standardized_results.csv、specification_index.csv 和 hypothesis_results.csv；请启用标准化输出并重跑该模块")

        run_record = None
        captured_global_config = None
        if will_be_included and source_file is not None:
            try:
                run_record, module_snapshot, global_snapshot = verify_effect(
                    global_config, module_name, source_file, module_path)
            except (RuntimeError, FileNotFoundError, KeyError) as exc:
                will_be_included = False
                warning_parts.append(f"历史结果不适用于当前批次：{exc}")
                source_file = None
                source_kind = ""
            else:
                listed = {item["path"]: item["sha256"] for item in run_record["files"]}
                captured_module = load_config_file(module_snapshot)
                captured_global = load_config_file(global_snapshot)
                expected_script = BASELINE_DIR / "skill" / module_name / f"run_{module_name}.py"
                current_environment = environment_info(expected_script)
                evidence_valid = (
                    run_record.get("schema_version") == SCHEMA_VERSION
                    and run_record.get("synthesis_ready") is True
                    and run_record.get("module_config_sha256") == file_sha256(module_snapshot)
                    and run_record.get("global_config_sha256") == file_sha256(global_snapshot)
                    and str(captured_global.get("config_version")) == str(global_config.get("config_version"))
                    and str(captured_module.get("config_version")) == str(module_config.get("config_version"))
                    and run_record.get("environment", {}).get("code_fingerprint") == current_environment["code_fingerprint"]
                    and run_record.get("environment", {}).get("python") == current_environment["python"]
                    and run_record.get("environment", {}).get("packages") == current_environment["packages"]
                    and listed.get(index_path.name) == file_sha256(index_path)
                    and listed.get(hypothesis_path.name) == file_sha256(hypothesis_path))
                if not evidence_valid:
                    will_be_included = False
                    warning_parts.append("运行记录、配置版本、代码环境或机器文件校验失败；请重跑该模块")
                    source_file = None
                    source_kind = ""
                module_config = captured_module
                captured_global_config = captured_global
                module_path = module_snapshot

        row = {
            "module_name": module_name,
            "enabled_in_global_config": enabled_in_global,
            "module_config_path": str(module_path) if module_path else module_files.get(module_name, ""),
            "module_config_exists": config_exists,
            "enabled_in_module_config": enabled_in_module,
            "output_dir": str(output_dir),
            "manifest_record_path": str(entry.get("record_path", "")) if entry else "",
            "manifest_run_id": batch.get("run_id", "") if batch else "",
            "output_location_valid": output_location_valid,
            "output_dir_exists": output_dir_exists,
            "preferred_result_file": preferred_name,
            "preferred_result_path": str(preferred_path),
            "standardized_results_exists": preferred_exists,
            "specification_index_exists": index_exists,
            "hypothesis_results_exists": hypothesis_exists,
            "fallback_result_file": fallback_name,
            "fallback_result_path": str(fallback_path),
            "fallback_results_exists": fallback_exists,
            "allow_fallback": allow_fallback,
            "will_be_included": will_be_included,
            "source_kind": source_kind,
            "source_file": str(source_file) if source_file else "",
            "warning": "；".join([w for w in warning_parts if w]),
        }
        rows.append(row)

        module_context[module_name] = {
            "module_config": module_config,
            "module_config_path": module_path,
            "run_record": run_record,
            "captured_global_config": captured_global_config,
            "output_dir": output_dir,
            "preferred_path": preferred_path,
            "index_path": index_path,
            "hypothesis_path": hypothesis_path,
            "fallback_path": fallback_path,
            "source_file": source_file,
            "source_kind": source_kind,
            "will_be_included": will_be_included,
            "warning": row["warning"],
        }

    check_df = pd.DataFrame(rows)
    return check_df, module_context


# =========================
# 旧宽表转换辅助函数（保留供历史数据迁移，不用于本次安全衔接）
# =========================




def fixed_effect_text(global_config: Dict[str, Any]) -> str:
    fe = global_config.get("model_settings", {}).get("fixed_effects", {})
    parts: List[str] = []
    if bool(fe.get("entity_fixed_effect", False)):
        parts.append(f"entity:{fe.get('entity_name', '')}")
    if bool(fe.get("time_fixed_effect", False)):
        parts.append(f"time:{fe.get('time_name', '')}")
    return ";".join([p for p in parts if p and not p.endswith(":")]) or "none"


def cluster_text(global_config: Dict[str, Any]) -> str:
    cluster = global_config.get("model_settings", {}).get("clustered_standard_errors", {})
    if bool(cluster.get("enabled", False)):
        return str(cluster.get("cluster_level", "") or "cluster")
    return "none"


def make_specification_id(row: Dict[str, Any]) -> str:
    parts = [
        f"s{safe_str(row.get('样本门槛'), 'NA')}",
        sanitize_name(row.get("处理方案", "")),
        f"interp_{safe_str(row.get('是否插值'), 'NA')}",
        f"winsor_{safe_str(row.get('是否缩尾'), 'NA')}",
        sanitize_name(row.get("控制变量组合ID", "ctrl_required_only")),
        sanitize_name(row.get("固定效应设定", "none")),
        sanitize_name(row.get("聚类标准误设定", "none")),
    ]
    return "__".join(parts)


def base_standard_row(
    row: pd.Series,
    module_name: str,
    global_config: Dict[str, Any],
    module_config: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    record = row.to_dict()
    out = {
        "model_id": record.get("model_id"),
        "模块名称": record.get("模块名称", module_name),
        "假设编号": record.get("假设编号"),
        "假设描述": record.get("假设描述"),
        "relationship_type": record.get("relationship_type"),
        "effect_type": module_name.replace("_effect", ""),
        "path_type": record.get("path_type"),
        "target_path": record.get("target_path"),
        "path_id": record.get("path_id"),
        "chain_id": record.get("chain_id"),
        "模型形式": record.get("模型形式"),
        "通过规则": record.get("通过规则"),
        "被解释变量": record.get("被解释变量"),
        "解释变量": record.get("解释变量") or record.get("源解释变量") or record.get("核心解释变量"),
        "中介变量": record.get("中介变量"),
        "调节变量": record.get("调节变量"),
        "核心解释变量组合": record.get("核心解释变量组合") or record.get("核心解释变量"),
        "样本区间": record.get("样本区间") or global_config.get("data", {}).get("sample_info", {}).get("sample_period"),
        "样本规则": record.get("样本规则") or "、".join(global_config.get("data", {}).get("sample_info", {}).get("sample_rules", []) or ["全样本"]),
        "样本门槛": record.get("样本门槛"),
        "处理方案": record.get("处理方案"),
        "是否插值": record.get("是否插值"),
        "是否缩尾": record.get("是否缩尾"),
        "控制变量组合ID": record.get("控制变量组合ID"),
        "控制变量组合": record.get("控制变量组合"),
        "纳入的控制变量": record.get("纳入的控制变量"),
        "纳入的可选控制变量": record.get("纳入的可选控制变量"),
        "控制变量数量": record.get("控制变量数量"),
        "optional_control_count": record.get("optional_control_count"),
        "固定效应设定": record.get("固定效应设定") or fixed_effect_text(global_config),
        "聚类标准误设定": record.get("聚类标准误设定") or cluster_text(global_config),
        "within R²": record.get("within R²"),
        "样本量（N）": record.get("样本量（N）"),
        "是否候选模型": record.get("是否候选模型"),
        "strong_pass": record.get("strong_pass"),
        "weak_pass": record.get("weak_pass"),
        "是否成功运行": record.get("是否成功运行"),
        "失败原因": record.get("失败原因"),
        "global_config_path": global_config.get("_config_path", ""),
        "module_config_path": module_config.get("_config_path", "") if module_config else "",
    }
    out["specification_id"] = record.get("specification_id") or make_specification_id(out)
    return out


def standardize_main_full_results(
    df: pd.DataFrame,
    global_config: Dict[str, Any],
    module_config: Optional[Dict[str, Any]],
) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    suffix = "_sq"
    if module_config:
        suffix = module_config.get("nonlinear_settings", {}).get("squared_term_suffix", "_sq")

    for _, row in df.iterrows():
        model_terms = safe_str_split(row.get("检验项组合"))
        if not model_terms:
            for col in row.index:
                if str(col).startswith("beta_"):
                    model_terms.append(str(col).replace("beta_", "", 1))

        for term in model_terms:
            out = base_standard_row(row, "main_effect", global_config, module_config)
            out.update({
                "relationship_type": out.get("relationship_type") or "main_effect",
                "effect_type": "main_effect",
                "path_type": "main_effect",
                "解释变量": term.replace(suffix, "") if str(term).endswith(suffix) else term,
                "核心解释变量组合": row.get("核心解释变量组合"),
                "检验项": term,
                "effect_role": "main_squared" if str(term).endswith(suffix) else "main_linear",
                "理论方向": row.get(f"理论方向_{term}"),
                "direction_match": row.get(f"direction_match_{term}"),
                "beta": row.get(f"beta_{term}"),
                "se": row.get(f"se_{term}"),
                "p": row.get(f"p_{term}"),
            })
            rows.append(out)
    return pd.DataFrame(rows)


def standardize_mediation_full_results(
    df: pd.DataFrame,
    global_config: Dict[str, Any],
    module_config: Optional[Dict[str, Any]],
) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        path_type = safe_str(row.get("path_type"))
        out = base_standard_row(row, "mediation_effect", global_config, module_config)
        out.update({
            "relationship_type": out.get("relationship_type") or "mediation",
            "effect_type": "mediation",
            "path_type": path_type,
            "target_path": None,
            "调节变量": None,
        })

        if path_type == "x_m_to_y":
            term = row.get("中介变量") or row.get("检验项")
            effect_role = "m_to_y"
            beta = row.get("beta_m")
            se = row.get("se_m")
            p = row.get("p_m")
        else:
            term = row.get("解释变量") or row.get("检验项")
            effect_role = path_type or "mediation_path"
            beta = row.get("beta_x")
            se = row.get("se_x")
            p = row.get("p_x")

        out.update({
            "检验项": term,
            "effect_role": effect_role,
            "理论方向": row.get("理论方向"),
            "direction_match": row.get("direction_match"),
            "beta": beta,
            "se": se,
            "p": p,
        })
        rows.append(out)
    return pd.DataFrame(rows)


def standardize_moderation_full_results(
    df: pd.DataFrame,
    global_config: Dict[str, Any],
    module_config: Optional[Dict[str, Any]],
) -> pd.DataFrame:
    rows: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        term = row.get("交互项变量") or row.get("检验项")
        out = base_standard_row(row, "moderation_effect", global_config, module_config)
        out.update({
            "relationship_type": out.get("relationship_type") or "moderation",
            "effect_type": "moderation",
            "path_type": row.get("target_path") or row.get("path_type"),
            "target_path": row.get("target_path"),
            "解释变量": row.get("源解释变量") or row.get("核心解释变量") or row.get("解释变量"),
            "核心解释变量组合": row.get("核心解释变量") or row.get("源解释变量"),
            "检验项": term,
            "effect_role": row.get("effect_role") or "interaction_term",
            "理论方向": row.get("理论方向"),
            "direction_match": row.get("direction_match"),
            "beta": row.get("beta_interaction") if "beta_interaction" in row.index else row.get("beta"),
            "se": row.get("se_interaction") if "se_interaction" in row.index else row.get("se"),
            "p": row.get("p_interaction") if "p_interaction" in row.index else row.get("p"),
        })
        rows.append(out)
    return pd.DataFrame(rows)


def ensure_standard_columns(df: pd.DataFrame) -> pd.DataFrame:
    return align_standard_columns(df)


def validate_effect_tables(module_name: str, standardized: pd.DataFrame,
                           index: pd.DataFrame, hypotheses: pd.DataFrame,
                           run_record: Dict[str, Any]) -> None:
    """Reject structurally incomplete or mixed-run machine outputs before merging."""
    for label, frame, columns in (("standardized_results", standardized, STANDARD_COLUMNS),
                                  ("specification_index", index, SPECIFICATION_INDEX_COLUMNS),
                                  ("hypothesis_results", hypotheses, HYPOTHESIS_COLUMNS)):
        if frame.columns[:len(columns)].tolist() != columns:
            raise RuntimeError(f"{module_name} {label} 公共表头或顺序不符合当前契约")
        if frame.empty:
            continue
        expected = {"run_id": run_record["run_id"],
                    "run_instance_id": run_record["run_instance_id"],
                    "common_fingerprint": run_record["common_fingerprint"],
                    "module_revision": run_record["module_revision"]}
        if label != "specification_index":
            expected["data_sha256"] = run_record["data_sha256"]
        for field, value in expected.items():
            if frame[field].isna().any() or not frame[field].astype(str).eq(str(value)).all():
                raise RuntimeError(f"{module_name} {label}.{field} 与运行记录不一致")
        if frame["model_id"].isna().any() or frame["specification_id"].isna().any():
            raise RuntimeError(f"{module_name} {label} 缺少模型或设定身份")
        if frame["模块名称"].isna().any() or not frame["模块名称"].astype(str).eq(module_name).all():
            raise RuntimeError(f"{module_name} {label} 模块名称与运行记录不一致")
    if standardized.empty != index.empty:
        raise RuntimeError(f"{module_name} 标准化表与设定索引的空结果状态不一致")
    key = ["model_id", "specification_id", "path_type", "检验项", "effect_role"]
    def signatures(frame: pd.DataFrame) -> set[tuple[str, ...]]:
        return {tuple(safe_str(item) for item in row) for row in frame[key].itertuples(index=False, name=None)}
    if signatures(standardized) != signatures(index):
        raise RuntimeError(f"{module_name} 设定索引与标准化模型项不一致")
    if not standardized.empty:
        provenance = {name: run_record[name] for name in
                      ("run_id", "run_instance_id", "common_fingerprint", "data_sha256", "module_revision")}
        expected_ids = standardized.apply(lambda row: specification_id(row, provenance), axis=1)
        if not standardized["specification_id"].astype(str).eq(expected_ids.astype(str)).all():
            raise RuntimeError(f"{module_name} 设定身份与实际公共参数不一致")


def read_and_standardize_module_results(
    module_name: str,
    context: Dict[str, Any],
    global_config: Dict[str, Any],
) -> pd.DataFrame:
    source_file = context.get("source_file")
    if not source_file:
        return pd.DataFrame()

    source_path = Path(source_file)
    if source_path.suffix.lower() == ".xlsx":
        raw_df = pd.read_excel(source_path)
    else:
        raw_df = pd.read_csv(source_path)

    module_config = context.get("module_config")
    historical_global_config = context.get("captured_global_config") or global_config
    source_kind = context.get("source_kind", "")

    if source_kind != "preferred_standardized":
        raise RuntimeError(f"{module_name} 不是已验证的标准化结果；不能降级读取普通宽表")
    standardized = raw_df.copy()
    hypothesis_path = context.get("hypothesis_path")
    index_path = context.get("index_path")
    if not hypothesis_path or not index_path or not context.get("run_record"):
        raise RuntimeError(f"{module_name} 缺少可核验的机器文件或运行记录")
    index = pd.read_csv(index_path)
    hypotheses = pd.read_csv(hypothesis_path)
    validate_effect_tables(module_name, standardized, index, hypotheses, context["run_record"])
    context["hypotheses_df"] = hypotheses
    decisions: Dict[str, Dict[str, Any]] = {}
    for _, decision in hypotheses.iterrows():
        raw_ids = decision.get("path_model_ids_json")
        if is_blank(raw_ids):
            raise RuntimeError(f"{module_name} 假设判定缺少模型关联")
        parsed = json.loads(raw_ids)
        ids = parsed.values() if isinstance(parsed, dict) else parsed
        for model_id in ids:
            if model_id in decisions:
                raise RuntimeError(f"{module_name} 模型 {model_id} 对应多条假设判定")
            decisions[model_id] = decision.to_dict()
    if not standardized.empty:
        standard_models = set(standardized["model_id"].dropna().astype(str))
        missing_decisions = standard_models - set(decisions)
        if missing_decisions:
            raise RuntimeError(f"{module_name} 存在未关联假设级判定的模型: {sorted(missing_decisions)[:5]}")
        unmatched_decisions = set(decisions) - standard_models
        if unmatched_decisions:
            raise RuntimeError(f"{module_name} 假设级判定关联了不存在的模型: {sorted(unmatched_decisions)[:5]}")
        for _, model in standardized.iterrows():
            decision = decisions[str(model["model_id"])]
            for field in ("specification_id", "假设编号", "chain_id", "检验阶段"):
                left, right = safe_str(model.get(field)), safe_str(decision.get(field))
                if field == "检验阶段":
                    left = "C" if left.startswith("C") else "AB"
                    right = "C" if right.startswith("C") else "AB"
                if field in model and field in decision and left != right:
                    raise RuntimeError(f"{module_name} 模型 {model['model_id']} 的{field}与假设级判定不一致")
        if module_name == "mediation_effect":
            standardized["path_strong_pass"] = standardized["strong_pass"]
            standardized["path_weak_pass"] = standardized["weak_pass"]
        for name in ("是否候选模型", "strong_pass", "weak_pass"):
            standardized[name] = standardized["model_id"].map(
                lambda model_id: decisions.get(str(model_id), {}).get(name))
        standardized["hypothesis_pass_basis_json"] = standardized["model_id"].map(
            lambda model_id: decisions.get(str(model_id), {}).get("pass_basis_json"))
        standardized["hypothesis_decision_reason"] = standardized["model_id"].map(
            lambda model_id: decisions.get(str(model_id), {}).get("判定依据"))
        standardized["hypothesis_sample_difference_note"] = standardized["model_id"].map(
            lambda model_id: decisions.get(str(model_id), {}).get("sample_difference_note"))
        standardized["hypothesis_path_samples_json"] = standardized["model_id"].map(
            lambda model_id: decisions.get(str(model_id), {}).get("path_samples_json"))
    elif decisions:
        raise RuntimeError(f"{module_name} 假设级判定存在，但标准化模型结果为空")

    standardized = ensure_standard_columns(standardized)
    sample_info = historical_global_config.get("data", {}).get("sample_info", {})
    for col, value in (("样本区间", sample_info.get("sample_period")),
                       ("样本规则", "、".join(sample_info.get("sample_rules", []) or ["全样本"]))):
        if value is not None:
            standardized[col] = standardized[col].replace("", pd.NA).fillna(value)
    # Older moderation files used all controls in the ID, while the other modules
    # used optional controls only. Rebuild the common key for existing exports.
    optional = standardized["纳入的可选控制变量"].map(safe_str_split)
    canonical_ids = optional.map(lambda values: "ctrl_" + sanitize_name("_".join(values)) if values else "ctrl_required_only")
    changed = standardized["控制变量组合ID"].fillna("").astype(str) != canonical_ids
    if changed.any():
        standardized["source_control_combination_id"] = standardized["控制变量组合ID"]
        standardized["source_specification_id"] = standardized["specification_id"]
        standardized["控制变量组合ID"] = canonical_ids
        historical = changed & standardized.get("run_id", pd.Series(index=standardized.index, dtype=object)).isna()
        if historical.any():
            standardized.loc[historical, "specification_id"] = standardized.loc[historical].apply(make_specification_id, axis=1)
    standardized["source_module"] = module_name
    standardized["source_file"] = str(source_path)
    standardized["source_kind"] = source_kind
    standardized["provenance_verified"] = True
    standardized["source_record_path"] = str(Path(context["output_dir"]) / "run_record.json")
    standardized["source_record_sha256"] = file_sha256(Path(context["output_dir"]) / "run_record.json")
    standardized["included_by_synthesis"] = True
    standardized["synthesis_warning"] = context.get("warning", "")
    return standardized


def read_all_module_results(
    module_context: Dict[str, Dict[str, Any]],
    global_config: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[str]]:
    frames: List[pd.DataFrame] = []
    warnings: List[str] = []
    for module_name in EFFECT_MODULES:
        context = module_context.get(module_name, {})
        if not context.get("will_be_included"):
            continue
        try:
            df = read_and_standardize_module_results(module_name, context, global_config)
        except Exception as exc:
            raise RuntimeError(f"{module_name} 假设级结果读取或关联失败：{exc}") from exc
        if df.empty:
            warnings.append(f"{module_name} 结果为空")
            continue
        frames.append(df)

    if not frames:
        return pd.DataFrame(), warnings

    concat_ready = [f.dropna(axis=1, how="all") for f in frames if f is not None and not f.empty]
    merged = pd.concat(concat_ready, ignore_index=True, sort=False)
    return ensure_standard_columns(merged), warnings


# =========================
# 假设登记与覆盖检查
# =========================

def extract_module_hypotheses(module_name: str, module_config: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not module_config:
        return []

    if module_name == "main_effect":
        items = module_config.get("hypotheses", [])
    elif module_name == "mediation_effect":
        items = module_config.get("mechanism_paths", [])
    elif module_name == "moderation_effect":
        items = module_config.get("moderation_paths", [])
    else:
        items = []

    rows: List[Dict[str, Any]] = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        rows.append({
            "hypothesis_id": item.get("id"),
            "module_name": module_name,
            "description": item.get("description"),
            "module_yaml_enabled": bool(item.get("enabled", True)),
        })
    return rows


def build_registered_hypothesis_map(synthesis_config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    registry_cfg = synthesis_config.get("hypothesis_registry", {})
    expected = registry_cfg.get("expected_hypotheses", []) or []
    out: Dict[str, Dict[str, Any]] = {}
    for item in expected:
        if not isinstance(item, dict):
            continue
        hid = safe_str(item.get("id")).strip()
        if not hid:
            continue
        out[hid] = {
            "hypothesis_id": hid,
            "registered_in_synthesis_yaml": True,
            "expected_modules": item.get("modules", []),
            "registered_description": item.get("description", ""),
        }
    return out


def build_hypothesis_coverage_check(
    synthesis_config: Dict[str, Any],
    module_context: Dict[str, Dict[str, Any]],
    merged_results_df: pd.DataFrame,
) -> pd.DataFrame:
    registry_cfg = synthesis_config.get("hypothesis_registry", {})
    registry_mode = registry_cfg.get("registry_mode", "manual_plus_auto_check")
    registered = build_registered_hypothesis_map(synthesis_config)

    module_rows: List[Dict[str, Any]] = []
    for module_name, context in module_context.items():
        module_rows.extend(extract_module_hypotheses(module_name, context.get("module_config")))

    module_df = pd.DataFrame(module_rows)
    if module_df.empty:
        module_ids: Set[str] = set()
    else:
        module_ids = set(module_df["hypothesis_id"].dropna().astype(str))

    if merged_results_df is None or merged_results_df.empty or "假设编号" not in merged_results_df.columns:
        output_ids: Set[str] = set()
    else:
        output_ids = set(merged_results_df["假设编号"].dropna().astype(str))

    if registry_mode == "manual_only":
        all_ids = set(registered.keys())
    elif registry_mode == "auto_only":
        all_ids = module_ids | output_ids
    else:
        all_ids = set(registered.keys()) | module_ids | output_ids

    rows: List[Dict[str, Any]] = []
    for hid in sorted(all_ids):
        reg = registered.get(hid, {})
        module_subset = module_df[module_df["hypothesis_id"].astype(str) == hid] if not module_df.empty else pd.DataFrame()
        output_subset = merged_results_df[merged_results_df["假设编号"].astype(str) == hid] if (
            merged_results_df is not None and not merged_results_df.empty and "假设编号" in merged_results_df.columns
        ) else pd.DataFrame()

        found_in_module_yaml = not module_subset.empty
        found_in_output = not output_subset.empty
        registered_in_yaml = bool(reg.get("registered_in_synthesis_yaml", False))
        module_enabled = bool(module_subset["module_yaml_enabled"].any()) if found_in_module_yaml else False

        warnings: List[str] = []
        if registered_in_yaml and not found_in_output:
            status = "registered_but_missing_output"
            warnings.append("登记了该假设，但结果中没有出现")
        elif found_in_output and not registered_in_yaml and registry_mode != "auto_only":
            status = "output_not_registered"
            warnings.append("结果中出现了该假设，但未在 synthesis.yaml 登记")
        elif found_in_module_yaml and not module_enabled:
            status = "module_disabled"
            warnings.append("模块 YAML 中存在该假设，但 enabled=false")
        elif registered_in_yaml and found_in_output:
            status = "ok"
        elif found_in_module_yaml or found_in_output:
            status = "auto_detected"
        else:
            status = "unknown"

        expected_modules = reg.get("expected_modules", [])
        found_modules = sorted(set(module_subset["module_name"].dropna().astype(str))) if found_in_module_yaml else []
        output_modules = sorted(set(output_subset["source_module"].dropna().astype(str))) if (
            found_in_output and "source_module" in output_subset.columns
        ) else []

        rows.append({
            "hypothesis_id": hid,
            "registered_in_synthesis_yaml": registered_in_yaml,
            "expected_modules": " ".join(expected_modules) if isinstance(expected_modules, list) else safe_str(expected_modules),
            "registered_description": reg.get("registered_description", ""),
            "found_in_module_yaml": found_in_module_yaml,
            "found_modules": " ".join(found_modules),
            "module_yaml_enabled": module_enabled,
            "found_in_output": found_in_output,
            "output_modules": " ".join(output_modules),
            "output_row_count": int(len(output_subset)) if found_in_output else 0,
            "coverage_status": status,
            "warning": "；".join(warnings),
        })

    return pd.DataFrame(rows)


def enforce_first_half_stop_rules(
    global_config: Dict[str, Any],
    synthesis_config: Dict[str, Any],
    model_check_df: pd.DataFrame,
    merged_results_df: pd.DataFrame,
    hypothesis_check_df: pd.DataFrame,
) -> None:
    input_cfg = synthesis_config.get("input", {})
    registry_cfg = synthesis_config.get("hypothesis_registry", {})

    if bool(input_cfg.get("fail_on_missing_enabled_module", False)):
        missing = model_check_df[
            (model_check_df["enabled_in_global_config"] == True)
            & (model_check_df["enabled_in_module_config"] == True)
            & (model_check_df["will_be_included"] == False)
        ]
        if not missing.empty:
            raise RuntimeError("存在已启用但无法纳入结果整合的模块：\n" + missing.to_string(index=False))

    if bool(input_cfg.get("fail_if_no_readable_results", True)) and (merged_results_df is None or merged_results_df.empty):
        raise RuntimeError("没有任何可读取的效应结果，无法继续结果整合。")

    if bool(registry_cfg.get("fail_on_unregistered_output", False)) and not hypothesis_check_df.empty:
        bad = hypothesis_check_df[hypothesis_check_df["coverage_status"] == "output_not_registered"]
        if not bad.empty:
            raise RuntimeError("存在未登记但出现在结果中的假设：\n" + bad.to_string(index=False))

    if bool(registry_cfg.get("fail_on_registered_missing_output", False)) and not hypothesis_check_df.empty:
        bad = hypothesis_check_df[hypothesis_check_df["coverage_status"] == "registered_but_missing_output"]
        if not bad.empty:
            raise RuntimeError("存在已登记但缺少输出结果的假设：\n" + bad.to_string(index=False))


# =========================
# 输出与报告工具
# =========================

def save_dataframe(df: pd.DataFrame, output_dir: Path, basename: str, synthesis_config: Dict[str, Any]) -> None:
    output_cfg = synthesis_config.get("output", {})
    if bool(output_cfg.get("save_csv", True)):
        df.to_csv(output_dir / f"{basename}.csv", index=False, encoding="utf-8-sig")
    if bool(output_cfg.get("save_excel", True)):
        try:
            df.to_excel(output_dir / f"{basename}.xlsx", index=False)
        except Exception as exc:
            (output_dir / f"{basename}.xlsx.error.txt").write_text(str(exc), encoding="utf-8")


def save_json(data: Any, output_dir: Path, filename: str, synthesis_config: Dict[str, Any]) -> None:
    if not bool(synthesis_config.get("output", {}).get("save_json", True)):
        return
    with open(output_dir / filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2, default=str)


def write_markdown(path: Path, title: str, body: str) -> None:
    path.write_text(f"# {title}\n\n{body}", encoding="utf-8")


def write_check_reports(
    output_dir: Path,
    synthesis_config: Dict[str, Any],
    model_check_df: pd.DataFrame,
    hypothesis_check_df: pd.DataFrame,
) -> None:
    model_cfg = synthesis_config.get("model_structure_check", {})
    if bool(model_cfg.get("write_csv_report", True)):
        model_check_df.to_csv(output_dir / "model_structure_check.csv", index=False, encoding="utf-8-sig")
    if bool(model_cfg.get("write_markdown_report", True)):
        write_markdown(output_dir / "model_structure_check.md", "模型结构核对", dataframe_to_markdown(model_check_df))

    registry_cfg = synthesis_config.get("hypothesis_registry", {})
    if bool(registry_cfg.get("enabled", True)):
        hypothesis_check_df.to_csv(output_dir / "hypothesis_coverage_check.csv", index=False, encoding="utf-8-sig")
        write_markdown(output_dir / "hypothesis_coverage_check.md", "假设完整性核对", dataframe_to_markdown(hypothesis_check_df))


# =========================
# 合并键与综合结果表
# =========================

def build_spec_key(row: pd.Series, merge_keys: Sequence[str], normalize_controls: bool) -> Tuple[str, ...]:
    values: List[str] = []
    for key in merge_keys:
        value = row.get(key)
        if normalize_controls and key in {"纳入的控制变量", "纳入的可选控制变量"}:
            values.append(normalize_text_for_key(value))
        else:
            values.append(safe_str(value).strip())
    return tuple(values)


def add_spec_group_id(
    df: pd.DataFrame,
    synthesis_config: Dict[str, Any],
) -> Tuple[pd.DataFrame, List[str]]:
    if df is None or df.empty:
        return pd.DataFrame(), []

    out = df.copy()
    merge_keys = synthesis_config.get("merge_keys", [])
    merge_settings = synthesis_config.get("merge_settings", {})
    prefix = merge_settings.get("spec_group_id_prefix", "SPEC")
    normalize_controls = bool(merge_settings.get("normalize_control_variable_text", True))
    missing_policy = merge_settings.get("missing_key_policy", "warn_and_keep")
    warnings: List[str] = []

    for key in merge_keys:
        if key not in out.columns:
            out[key] = None
            warnings.append(f"合并键字段缺失，已补空列：{key}")

    def missing_merge_key(row: pd.Series) -> bool:
        for key in merge_keys:
            if not is_blank(row[key]):
                continue
            # No optional controls is a valid required-only specification.
            if key == "纳入的可选控制变量" and row.get("控制变量组合ID") == "ctrl_required_only":
                continue
            return True
        return False

    missing_mask = out.apply(missing_merge_key, axis=1) if merge_keys else pd.Series(False, index=out.index)
    out["merge_key_complete"] = ~missing_mask
    if missing_mask.any():
        msg = f"存在 {int(missing_mask.sum())} 行缺少至少一个合并键字段"
        if missing_policy == "fail":
            raise RuntimeError(msg)
        if missing_policy == "drop":
            warnings.append(msg + "，已按配置丢弃")
            out = out.loc[~missing_mask].copy()
        else:
            warnings.append(msg + "，已保留并写入 synthesis_warning")
            out.loc[missing_mask, "synthesis_warning"] = (
                out.loc[missing_mask, "synthesis_warning"].fillna("").astype(str)
                + "；缺少合并键字段"
            ).str.strip("；")

    key_to_id: Dict[Tuple[str, ...], str] = {}
    ids: List[str] = []
    for _, row in out.iterrows():
        spec_key = build_spec_key(row, merge_keys, normalize_controls)
        if spec_key not in key_to_id:
            key_to_id[spec_key] = f"{prefix}_{len(key_to_id) + 1:03d}"
        ids.append(key_to_id[spec_key])

    out["spec_group_id"] = ids
    return out, warnings


def is_success_series(s: pd.Series) -> pd.Series:
    return s.fillna("").astype(str).str.lower().eq("success")


def filter_pass_table(df: pd.DataFrame, table_cfg: Dict[str, Any], table_name: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=df.columns if df is not None else None)

    out = df.copy()
    if table_name == "full":
        if not bool(table_cfg.get("include_failed_models", True)) and "是否成功运行" in out.columns:
            out = out[is_success_series(out["是否成功运行"])].copy()
        return out

    pass_col = table_cfg.get("pass_column", f"{table_name}_pass")
    if pass_col not in out.columns:
        return out.iloc[0:0].copy()

    mask = out[pass_col].apply(bool_for_filter)
    if bool(table_cfg.get("require_success", True)) and "是否成功运行" in out.columns:
        mask = mask & is_success_series(out["是否成功运行"])
    if bool(table_cfg.get("require_direction_match", False)) and "direction_match" in out.columns:
        mask = mask & out["direction_match"].apply(bool_for_filter)
    if table_name == "weak" and not bool(table_cfg.get("include_strong_rows", True)) and "strong_pass" in out.columns:
        mask = mask & ~out["strong_pass"].apply(bool_for_filter)
    return out[mask].copy()


def build_row_pass_tables(full_df: pd.DataFrame, synthesis_config: Dict[str, Any]) -> Dict[str, pd.DataFrame]:
    tables: Dict[str, pd.DataFrame] = {}
    pass_cfg = synthesis_config.get("pass_tables", {})
    for table_name in ["full", "strong", "weak"]:
        table_cfg = pass_cfg.get(table_name, {})
        if not bool(table_cfg.get("enabled", True)):
            tables[table_name] = pd.DataFrame(columns=full_df.columns if full_df is not None else None)
            continue
        tables[table_name] = filter_pass_table(full_df, table_cfg, table_name)
    return tables


SPECIFICATION_TABLE_COLUMNS = [
    "模型合并ID", "specification_id", "Strong pass", "weak_pass", "no&lack_pass",
    "未通过", "运行失败", "缺少C结果", "样本区间", "样本规则", "样本门槛", "处理方案",
    "是否插值", "是否缩尾", "控制变量组合ID", "控制变量组合", "纳入的控制变量",
    "纳入的可选控制变量", "固定效应设定", "聚类标准误设定",
    "是否成功运行", "是否候选模型", "main_effect_model_ids",
    "mediation_effect_model_ids", "moderation_effect_model_ids", "model_ids_json",
    "run_id", "common_fingerprint", "data_sha256", "筛选说明",
]
HYPOTHESIS_DETAIL_COLUMNS = [
    "模型合并ID", "specification_id", "模块名称", "假设编号", "假设描述", "required_pass",
    "C阶段状态", "是否成功运行", "是否候选模型", "strong_pass", "weak_pass",
    "C模型ID", "AB模型ID", "chain_id", "判定依据", "缺失或失败原因",
]


def _model_ids(value: Any) -> list[str]:
    if is_blank(value):
        return []
    parsed = json.loads(value)
    ids = parsed.values() if isinstance(parsed, dict) else parsed
    return [str(item) for item in ids if not is_blank(item)]


def _unique_join(values: Iterable[Any]) -> str:
    return " ".join(dict.fromkeys(str(v) for v in values if not is_blank(v)))


def _active_registered_hypotheses(synthesis_config: Dict[str, Any],
                                  module_context: Dict[str, Dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for entry in synthesis_config.get("hypothesis_registry", {}).get("expected_hypotheses", []):
        for module in entry.get("modules", []):
            context = module_context.get(module, {})
            if not context.get("will_be_included"):
                continue
            configured = extract_module_hypotheses(module, context.get("module_config"))
            if not any(item["hypothesis_id"] == entry["id"] and item["module_yaml_enabled"]
                       for item in configured):
                continue
            rows.append({"模块名称": module, "假设编号": entry["id"],
                         "假设描述": entry.get("description", ""),
                         "required_pass": entry.get("required_pass", "none")})
    return pd.DataFrame(rows, columns=["模块名称", "假设编号", "假设描述", "required_pass"])


def build_specification_tables(full_rows: pd.DataFrame, synthesis_config: Dict[str, Any],
                               module_context: Dict[str, Dict[str, Any]]) -> tuple[dict[str, pd.DataFrame], pd.DataFrame]:
    """Summarize verified C decisions once per public specification and registered hypothesis."""
    empty_tables = {name: pd.DataFrame(columns=SPECIFICATION_TABLE_COLUMNS)
                    for name in ("require", "full", "strong", "weak")}
    empty_detail = pd.DataFrame(columns=HYPOTHESIS_DETAIL_COLUMNS)
    if full_rows.empty:
        return empty_tables, empty_detail
    if full_rows["specification_id"].isna().any():
        raise RuntimeError("综合主表缺少 specification_id")

    specs = full_rows.drop_duplicates("specification_id", keep="first")
    if full_rows.groupby("specification_id")["spec_group_id"].nunique().gt(1).any():
        raise RuntimeError("同一个 specification_id 对应多个合并ID，不能生成设定级主表")
    registry = _active_registered_hypotheses(synthesis_config, module_context)
    decisions = []
    for module, context in module_context.items():
        if not context.get("will_be_included"):
            continue
        frame = context.get("hypotheses_df")
        if frame is None or frame.empty:
            continue
        selected = frame.loc[frame["检验阶段"].astype(str).str.startswith("C")].copy()
        if not selected.empty:
            selected["模块名称"] = module
            decisions.append(selected)
    c_rows = pd.concat(decisions, ignore_index=True) if decisions else pd.DataFrame()
    key = ["specification_id", "模块名称", "假设编号"]
    if not c_rows.empty:
        c_rows = c_rows.merge(registry[["模块名称", "假设编号"]],
                              on=["模块名称", "假设编号"], how="inner", validate="many_to_one")
        c_rows["_success"] = c_rows["是否成功运行"].astype(str).str.lower().eq("success")
        c_rows["_strong"] = c_rows["strong_pass"].map(bool_for_filter)
        c_rows["_weak"] = c_rows["weak_pass"].map(bool_for_filter) | c_rows["_strong"]
        c_rows["_candidate"] = c_rows["是否候选模型"].map(bool_for_filter)
        c_rows["_model_ids"] = c_rows["path_model_ids_json"].map(_model_ids)
        grouped = c_rows.groupby(key, sort=False, dropna=False)
        decisions_by_hypothesis = grouped.agg(
            _success=("_success", "all"), _strong=("_strong", "all"),
            _weak=("_weak", "all"), _candidate=("_candidate", "all"),
            chain_id=("chain_id", _unique_join), 判定依据=("判定依据", lambda s: "；".join(dict.fromkeys(s.dropna().astype(str))))
        ).reset_index()
        ids = grouped["_model_ids"].agg(lambda items: _unique_join(
            model_id for item in items for model_id in item)).reset_index(name="C模型ID")
        decisions_by_hypothesis = decisions_by_hypothesis.merge(ids, on=key, validate="one_to_one")
    else:
        decisions_by_hypothesis = pd.DataFrame(columns=key + ["_success", "_strong", "_weak",
                                                     "_candidate", "chain_id", "判定依据", "C模型ID"])

    if registry.empty:
        detail = empty_detail
    else:
        detail = (specs[["specification_id", "spec_group_id"]].merge(registry, how="cross")
                  .merge(decisions_by_hypothesis, on=key, how="left", validate="one_to_one"))
        ab = full_rows.loc[~full_rows.get("检验阶段", pd.Series("", index=full_rows.index))
                           .fillna("").astype(str).str.startswith("C")].copy()
        if not ab.empty:
            ab["模块名称"] = ab["source_module"]
            ab_ids = ab.groupby(key, sort=False)["model_id"].agg(_unique_join).reset_index(name="AB模型ID")
            detail = detail.merge(ab_ids, on=key, how="left", validate="one_to_one")
        else:
            detail["AB模型ID"] = ""
        present = detail["_success"].notna()
        success = detail["_success"].map(bool_for_filter)
        strong = detail["_strong"].map(bool_for_filter)
        weak = detail["_weak"].map(bool_for_filter)
        detail["C阶段状态"] = "缺少C结果"
        detail.loc[present & ~success, "C阶段状态"] = "运行失败"
        detail.loc[present & success, "C阶段状态"] = "未通过"
        detail.loc[present & success & weak, "C阶段状态"] = "弱通过"
        detail.loc[present & success & strong, "C阶段状态"] = "强通过"
        detail["是否成功运行"] = present & success
        detail["是否候选模型"] = present & detail["_candidate"].map(bool_for_filter)
        detail["strong_pass"] = detail["C阶段状态"].eq("强通过")
        detail["weak_pass"] = detail["C阶段状态"].isin(["强通过", "弱通过"])
        detail["缺失或失败原因"] = detail["C阶段状态"].where(
            detail["C阶段状态"].isin(["缺少C结果", "运行失败", "未通过"]), "")
        detail = detail.rename(columns={"spec_group_id": "模型合并ID"})
        for col in HYPOTHESIS_DETAIL_COLUMNS:
            if col not in detail:
                detail[col] = ""
        detail = detail[HYPOTHESIS_DETAIL_COLUMNS]

    detail_groups = {spec: frame for spec, frame in detail.groupby("specification_id", sort=False)}
    summary = []
    for source in specs.to_dict(orient="records"):
        spec_id = source["specification_id"]
        group = detail_groups.get(spec_id, empty_detail)
        by_status = {status: group.loc[group["C阶段状态"] == status, "假设编号"].tolist()
                     for status in ("强通过", "弱通过", "未通过", "运行失败", "缺少C结果")}
        bad = by_status["未通过"] + by_status["运行失败"] + by_status["缺少C结果"]
        model_map = {module: list(dict.fromkeys(
            model_id for cell in group.loc[group["模块名称"] == module, "C模型ID"]
            for model_id in safe_str_split(cell))) for module in EFFECT_MODULES}
        required = group.loc[group["required_pass"] != "none"]
        require_ok = all(status == "强通过" if level == "strong"
                         else status in {"强通过", "弱通过"}
                         for status, level in zip(required["C阶段状态"], required["required_pass"]))
        strict_strong = bool(len(group) and not (by_status["弱通过"] or bad))
        strict_weak = bool(len(group) and not bad)
        item = {"模型合并ID": source["spec_group_id"], "specification_id": spec_id,
                "Strong pass": _unique_join(by_status["强通过"]),
                "weak_pass": _unique_join(by_status["弱通过"]),
                "no&lack_pass": _unique_join(bad),
                "未通过": _unique_join(by_status["未通过"]),
                "运行失败": _unique_join(by_status["运行失败"]),
                "缺少C结果": _unique_join(by_status["缺少C结果"]),
                "是否成功运行": bool(len(group) and group["是否成功运行"].all()),
                "是否候选模型": bool(len(group) and group["是否候选模型"].all()),
                "model_ids_json": json.dumps(model_map, ensure_ascii=False, sort_keys=True),
                "_require_ok": require_ok, "_strong_ok": strict_strong, "_weak_ok": strict_weak}
        item["筛选说明"] = "；".join(f"{label}: {_unique_join(values)}" for label, values in
                                (("未通过", by_status["未通过"]), ("运行失败", by_status["运行失败"]),
                                 ("缺少C结果", by_status["缺少C结果"])) if values)
        for col in SPECIFICATION_TABLE_COLUMNS:
            if col not in item:
                item[col] = source.get(col, "")
        for module in EFFECT_MODULES:
            item[f"{module}_model_ids"] = _unique_join(model_map[module])
        summary.append(item)
    all_specs = pd.DataFrame(summary)
    tables = {"full": all_specs[SPECIFICATION_TABLE_COLUMNS].copy(),
              "require": all_specs.loc[all_specs["_require_ok"], SPECIFICATION_TABLE_COLUMNS].copy(),
              "strong": all_specs.loc[all_specs["_strong_ok"], SPECIFICATION_TABLE_COLUMNS].copy(),
              "weak": all_specs.loc[all_specs["_weak_ok"], SPECIFICATION_TABLE_COLUMNS].copy()}
    return tables, detail


def save_pass_tables(output_dir: Path, synthesis_config: Dict[str, Any], tables: Dict[str, pd.DataFrame]) -> None:
    pass_cfg = synthesis_config.get("pass_tables", {})
    defaults = {
        "require": "merged_require_results",
        "full": "merged_full_results",
        "strong": "merged_strong_pass_results",
        "weak": "merged_weak_pass_results",
    }
    for name, df in tables.items():
        basename = pass_cfg.get(name, {}).get("output_basename", defaults[name])
        save_dataframe(df, output_dir, basename, synthesis_config)


def save_overview_workbook(output_dir: Path, synthesis_config: Dict[str, Any],
                           tables: Dict[str, pd.DataFrame], detail: pd.DataFrame) -> None:
    output = synthesis_config.get("output", {})
    workbook = output.get("overview_workbook", {})
    if not workbook.get("enabled", False):
        return
    descriptions = {
        "模型合并ID": "本次整合生成的展示编号；复现时应使用 specification_id 与 model_id。",
        "specification_id": "同一数据和公共设定的稳定身份。",
        "Strong pass": "本设定 C 阶段强通过的假设编号。",
        "weak_pass": "仅弱通过的假设编号；强通过假设列在 Strong pass 中。",
        "no&lack_pass": "未通过、运行失败或缺少 C 结果的假设编号汇总。",
        "未通过": "存在 C 阶段判定，但未达到弱通过的假设。",
        "运行失败": "存在 C 阶段判定，但模型运行失败的假设。",
        "缺少C结果": "本公共设定下未找到该假设的 C 阶段判定。",
        "是否成功运行": "纳入的全部假设均有成功运行的 C 阶段判定。",
        "是否候选模型": "纳入的全部假设在其 C 阶段判定中均标记为候选。",
        "model_ids_json": "各效应 C 阶段精确模型 ID；供后续 do-file 定位。",
        "筛选说明": "未通过、失败和缺少 C 结果的具体假设。",
    }
    field_rows = [{"字段": col, "含义": descriptions.get(col, "公共设定或来源追溯字段；详见全局说明。")}
                  for col in SPECIFICATION_TABLE_COLUMNS]
    field_rows.extend([
        {"字段": "require", "含义": "只满足各假设 required_pass 的设定；none 不添加门槛。"},
        {"字段": "full", "含义": "全部已执行公共设定；缺少 C 判定时仍保留。"},
        {"字段": "strong", "含义": "全部纳入假设在 C 阶段强通过；空表仍保留列名。"},
        {"字段": "weak", "含义": "全部纳入假设在 C 阶段至少弱通过；强通过也计入。"},
    ])
    views = {**tables, "假设明细": detail, "字段说明": pd.DataFrame(field_rows)}
    target = output_dir / workbook["filename"]
    with pd.ExcelWriter(target) as writer:
        for sheet in workbook["sheets"]:
            views[sheet].to_excel(writer, sheet_name=sheet, index=False)


# =========================
# 设定曲线与稳健性陈述
# =========================

def numeric_column(df: pd.DataFrame, col: str) -> pd.Series:
    if col not in df.columns:
        return pd.Series([None] * len(df), index=df.index, dtype="float64")
    return pd.to_numeric(df[col], errors="coerce")


def build_defensible_curve_data(
    full_df: pd.DataFrame,
    global_config: Dict[str, Any],
    synthesis_config: Dict[str, Any],
) -> pd.DataFrame:
    if full_df is None or full_df.empty:
        return pd.DataFrame()

    cfg = synthesis_config.get("defensible_specifications", {})
    if not bool(cfg.get("enabled", True)):
        return pd.DataFrame()

    out = full_df.copy()
    allowed_modules = set(cfg.get("allowed_modules", EFFECT_MODULES))
    if "source_module" in out.columns:
        out = out[out["source_module"].isin(allowed_modules)].copy()

    if not bool(cfg.get("include_failed_models", False)) or bool(cfg.get("require_success", True)):
        if "是否成功运行" in out.columns:
            out = out[is_success_series(out["是否成功运行"])].copy()

    if bool(cfg.get("require_minimum_sample_size_from_global", True)):
        min_n = parse_float(global_config.get("model_settings", {}).get("minimum_sample_size"))
        if min_n is not None and "样本量（N）" in out.columns:
            out = out[numeric_column(out, "样本量（N）") >= min_n].copy()

    if not bool(cfg.get("include_baseline_required_controls", True)):
        out = out[out["控制变量组合ID"].fillna("").astype(str) != "ctrl_required_only"].copy()
    if not bool(cfg.get("include_control_combinations", True)):
        out = out[out["控制变量组合ID"].fillna("").astype(str) == "ctrl_required_only"].copy()

    if bool(cfg.get("require_direction_match_for_curve", False)) and "direction_match" in out.columns:
        out = out[out["direction_match"].apply(bool_for_filter)].copy()

    out["beta_numeric"] = numeric_column(out, "beta")
    out["se_numeric"] = numeric_column(out, "se")
    out["p_numeric"] = numeric_column(out, "p")
    out = out[out["beta_numeric"].notna()].copy()

    curve_cfg = synthesis_config.get("specification_curve", {})
    order_by = curve_cfg.get("order_by", "beta")
    ascending = curve_cfg.get("order_direction", "ascending") != "descending"
    order_col = {"beta": "beta_numeric", "p": "p_numeric", "spec_group_id": "spec_group_id"}.get(order_by, "beta_numeric")

    group_cols = ["假设编号", "source_module", "effect_role", "检验项"]
    for col in group_cols:
        if col not in out.columns:
            out[col] = ""

    out = out.sort_values(group_cols + [order_col], ascending=ascending, na_position="last").copy()
    out["curve_id"] = (
        out["假设编号"].fillna("").astype(str).map(sanitize_name)
        + "__"
        + out["source_module"].fillna("").astype(str).map(sanitize_name)
        + "__"
        + out["effect_role"].fillna("").astype(str).map(sanitize_name)
        + "__"
        + out["检验项"].fillna("").astype(str).map(sanitize_name)
    )
    out["curve_order"] = out.groupby("curve_id").cumcount() + 1

    ci_cfg = curve_cfg.get("confidence_interval", {})
    if bool(ci_cfg.get("enabled", True)):
        level = parse_float(ci_cfg.get("level", 0.95)) or 0.95
        # 1.96 is used for the common 95% interval; for other levels this remains an approximation.
        z = 1.96 if abs(level - 0.95) < 1e-9 else 1.96
        out["ci_lower"] = out["beta_numeric"] - z * out["se_numeric"]
        out["ci_upper"] = out["beta_numeric"] + z * out["se_numeric"]

    return out


def share_true(series: pd.Series) -> Optional[float]:
    if series.empty:
        return None
    return float(series.apply(bool_for_filter).mean())


def build_curve_summary(curve_df: pd.DataFrame, synthesis_config: Dict[str, Any]) -> pd.DataFrame:
    if curve_df is None or curve_df.empty:
        return pd.DataFrame()

    cfg = synthesis_config.get("defensible_specifications", {})
    summary_df = curve_df.copy()
    if bool(cfg.get("require_direction_match_for_summary", False)) and "direction_match" in summary_df.columns:
        summary_df = summary_df[summary_df["direction_match"].apply(bool_for_filter)].copy()

    rows: List[Dict[str, Any]] = []
    for curve_id, g in summary_df.groupby("curve_id", dropna=False):
        beta = g["beta_numeric"].dropna()
        p = g["p_numeric"].dropna()
        rows.append({
            "curve_id": curve_id,
            "hypothesis_id": g["假设编号"].dropna().astype(str).iloc[0] if not g["假设编号"].dropna().empty else "",
            "source_module": g["source_module"].dropna().astype(str).iloc[0] if not g["source_module"].dropna().empty else "",
            "effect_role": g["effect_role"].dropna().astype(str).iloc[0] if not g["effect_role"].dropna().empty else "",
            "检验项": g["检验项"].dropna().astype(str).iloc[0] if not g["检验项"].dropna().empty else "",
            "n_defensible_specs": int(len(g)),
            "n_success": int(is_success_series(g["是否成功运行"]).sum()) if "是否成功运行" in g.columns else int(len(g)),
            "n_strong_pass": int(g["strong_pass"].apply(bool_for_filter).sum()) if "strong_pass" in g.columns else 0,
            "n_weak_pass": int(g["weak_pass"].apply(bool_for_filter).sum()) if "weak_pass" in g.columns else 0,
            "strong_pass_share": share_true(g["strong_pass"]) if "strong_pass" in g.columns else None,
            "weak_pass_share": share_true(g["weak_pass"]) if "weak_pass" in g.columns else None,
            "direction_match_share": share_true(g["direction_match"]) if "direction_match" in g.columns else None,
            "beta_median": float(beta.median()) if not beta.empty else None,
            "beta_mean": float(beta.mean()) if not beta.empty else None,
            "beta_q25": float(beta.quantile(0.25)) if not beta.empty else None,
            "beta_q75": float(beta.quantile(0.75)) if not beta.empty else None,
            "p_median": float(p.median()) if not p.empty else None,
        })
    return pd.DataFrame(rows)


def robustness_label(strong_share: Optional[float], thresholds: Dict[str, Any]) -> str:
    if strong_share is None:
        return "暂无法判断"
    if strong_share >= float(thresholds.get("strong_robust_share", 0.70)):
        return "较强稳健性"
    if strong_share >= float(thresholds.get("moderate_robust_share", 0.50)):
        return "一定稳健性"
    if strong_share >= float(thresholds.get("limited_robust_share", 0.30)):
        return "有限稳健性"
    return "稳健性较弱"


def build_robustness_statement(summary_df: pd.DataFrame, synthesis_config: Dict[str, Any]) -> str:
    narrative_cfg = synthesis_config.get("narrative", {})
    if not bool(narrative_cfg.get("enabled", True)):
        return "本轮未启用自动稳健性陈述。\n"
    if summary_df is None or summary_df.empty:
        return "本轮没有可用于生成稳健性陈述的设定曲线结果。\n"

    thresholds = narrative_cfg.get("conclusion_thresholds", {})
    lines = [
        "以下陈述仅基于本批次已执行、且符合 synthesis.yaml 可辩护规则的设定；不代表全部合理预设，用于补充主设定解释。",
        "",
    ]
    for _, row in summary_df.iterrows():
        strong_share = parse_float(row.get("strong_pass_share"))
        weak_share = parse_float(row.get("weak_pass_share"))
        direction_share = parse_float(row.get("direction_match_share"))
        label = robustness_label(strong_share, thresholds)
        lines.append(
            f"- 假设 {row.get('hypothesis_id')} 的 `{row.get('检验项')}`（{row.get('source_module')} / {row.get('effect_role')}）"
            f"在 {int(row.get('n_defensible_specs', 0))} 个已执行且可辩护的设定中，"
            f"5% 强通过比例为 {strong_share:.1%}，10% 弱通过比例为 {weak_share:.1%}，"
            f"方向一致比例为 {direction_share:.1%}，系数中位数为 {row.get('beta_median')}。"
            f"按当前阈值，该结果表现为：{label}。"
        )
    lines.append("")
    lines.append("提醒：设定曲线用于诚实呈现结果分布，不应用于事后挑选最显著设定。")
    return "\n".join(lines) + "\n"


def try_plot_specification_curves(curve_df: pd.DataFrame, output_dir: Path, synthesis_config: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    curve_cfg = synthesis_config.get("specification_curve", {})
    plot_cfg = curve_cfg.get("plot", {})
    if not bool(curve_cfg.get("enabled", True)) or not bool(plot_cfg.get("enabled", True)):
        return warnings
    if curve_df is None or curve_df.empty:
        return warnings

    try:
        import matplotlib.pyplot as plt  # type: ignore
    except Exception as exc:
        warnings.append(f"matplotlib 不可用，已跳过设定曲线图片输出：{exc}")
        return warnings

    figures_dir = ensure_dir(output_dir / curve_cfg.get("output", {}).get("figures_dir", "figures"))
    fmt = plot_cfg.get("file_format", "png")
    dpi = int(plot_cfg.get("dpi", 300))
    width = float(plot_cfg.get("figure_width", 10))
    height = float(plot_cfg.get("figure_height", 6))

    for curve_id, g in curve_df.groupby("curve_id", dropna=False):
        g = g.sort_values("curve_order")
        fig, ax = plt.subplots(figsize=(width, height))
        colors = ["#1f77b4" if bool_for_filter(v) else "#9aa0a6" for v in g.get("strong_pass", pd.Series([False] * len(g)))]
        ax.scatter(g["curve_order"], g["beta_numeric"], c=colors, s=18)
        if bool(plot_cfg.get("show_confidence_interval", True)) and {"ci_lower", "ci_upper"}.issubset(g.columns):
            ax.vlines(g["curve_order"], g["ci_lower"], g["ci_upper"], color="#c8c8c8", linewidth=0.8)
        if bool(plot_cfg.get("show_zero_line", True)):
            ax.axhline(0, color="#444444", linewidth=0.8)
        ax.set_title(str(curve_id))
        ax.set_xlabel("Specification order")
        ax.set_ylabel("Coefficient")
        fig.tight_layout()
        fig.savefig(figures_dir / f"spec_curve_{sanitize_name(curve_id)}.{fmt}", dpi=dpi)
        plt.close(fig)

    return warnings


def save_curve_outputs(
    full_df: pd.DataFrame,
    global_config: Dict[str, Any],
    synthesis_config: Dict[str, Any],
    output_dir: Path,
) -> Tuple[pd.DataFrame, pd.DataFrame, List[str]]:
    curve_cfg = synthesis_config.get("specification_curve", {})
    if not bool(curve_cfg.get("enabled", True)):
        return pd.DataFrame(), pd.DataFrame(), []

    curve_df = build_defensible_curve_data(full_df, global_config, synthesis_config)
    summary_df = build_curve_summary(curve_df, synthesis_config)

    output_cfg = curve_cfg.get("output", {})
    save_dataframe(curve_df, output_dir, output_cfg.get("data_basename", "specification_curve_data"), synthesis_config)
    save_dataframe(summary_df, output_dir, output_cfg.get("summary_basename", "specification_curve_summary"), synthesis_config)

    statement = build_robustness_statement(summary_df, synthesis_config)
    narrative_file = synthesis_config.get("narrative", {}).get("output_file", "robustness_statement.md")
    write_markdown(output_dir / narrative_file, "设定曲线稳健性陈述", statement)

    plot_warnings = try_plot_specification_curves(curve_df, output_dir, synthesis_config)
    return curve_df, summary_df, plot_warnings


# =========================
# 可展示案例
# =========================

DISPLAY_CASE_COLUMNS = [
    "case_id",
    "pass_level",
    "spec_group_id",
    "theory_chain_id",
    "样本门槛",
    "处理方案",
    "是否插值",
    "是否缩尾",
    "控制变量组合ID",
    "纳入的控制变量",
    "纳入的可选控制变量",
    "固定效应设定",
    "聚类标准误设定",
    "covered_hypotheses",
    "covered_modules",
    "main_effect_model_ids",
    "mediation_effect_model_ids",
    "moderation_effect_model_ids",
    "mediation_sample_difference_note",
    "mediation_path_samples_json",
    "all_required_modules_pass",
    "recommendation_reason",
    "run_id", "common_fingerprint", "data_sha256", "specification_id",
    "selected_models_json", "public_specification_json", "sample_differences_json",
    "source_snapshots_json",
]

DISPLAY_REJECTION_COLUMNS = ["spec_group_id", "theory_chain_id", "reason"]


def _allowed_names(value: Any) -> set[str]:
    if value is None:
        return set()
    return {str(item) for item in (value if isinstance(value, list) else [value]) if not is_blank(item)}


def _case_json(value: Any, *, indent: Optional[int] = None) -> str:
    def clean(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(key): clean(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [clean(value) for value in item]
        if is_blank(item):
            return None
        if hasattr(item, "item"):
            return clean(item.item())
        return item
    return json.dumps(clean(value), ensure_ascii=False, allow_nan=False, indent=indent)


def _model_matches_hypothesis(row: pd.Series, module: str, config: Dict[str, Any],
                              chain: Optional[Dict[str, Any]]) -> bool:
    block = {"main_effect": "hypotheses", "mediation_effect": "mechanism_paths",
             "moderation_effect": "moderation_paths"}[module]
    hypothesis = next((item for item in config.get(block, [])
                       if str(item.get("id")) == safe_str(row.get("假设编号"))
                       and item.get("enabled", True)), None)
    if not hypothesis:
        return False
    x, y = safe_str(row.get("解释变量")), safe_str(row.get("被解释变量"))
    if module == "main_effect":
        configured_x = {str(item["name"]) for item in hypothesis.get("x", [])}
        actual_x = set(safe_str_split(row.get("核心解释变量组合")))
        valid = (y == hypothesis.get("y") and x in configured_x
                 and actual_x == configured_x
                 and safe_str(row.get("模型形式")) == hypothesis.get("model_form", "linear"))
    elif module == "mediation_effect":
        mediator = safe_str(row.get("中介变量"))
        valid = (x == hypothesis.get("x") and y == hypothesis.get("y")
                 and mediator in _allowed_names(hypothesis.get("mediators"))
                 and safe_str(row.get("chain_id")) == "_".join((
                     str(hypothesis.get("id")), sanitize_name(x), sanitize_name(mediator), sanitize_name(y)))
                 and safe_str(row.get("path_type")) in {"x_to_y", "x_to_m", "x_m_to_y"})
        if valid:
            try:
                basis = json.loads(row.get("hypothesis_pass_basis_json"))
                valid = (not basis.get("missing_paths") and not basis.get("duplicate_paths")
                         and basis.get("screening_method") == hypothesis.get("screening_method")
                         and basis.get("pass_rule") == hypothesis.get("pass_rule"))
            except (TypeError, ValueError):
                valid = False
    else:
        relation = hypothesis.get("focal_relationship", {})
        moderation = hypothesis.get("moderation", {})
        valid = (x == relation.get("from") and y == relation.get("to")
                 and safe_str(row.get("调节变量")) == hypothesis.get("moderator")
                 and safe_str(row.get("focal_relationship_form")) == relation.get("form", "linear")
                 and safe_str(row.get("moderation_form")) == moderation.get("form", "linear")
                 and safe_str(row.get("expected_change")) == moderation.get("expected_change"))
    if not valid or chain is None:
        return valid
    chain_x, chain_y = _allowed_names(chain.get("x")), _allowed_names(chain.get("y"))
    if not chain_x or not chain_y:
        return False
    if module == "main_effect":
        required_x = {str(item["name"]) for item in hypothesis.get("x", [])
                      if item.get("required", True)}
        return bool(required_x & chain_x) and y in chain_y
    mediator = _allowed_names(chain.get("mediator"))
    if module == "mediation_effect":
        return x in chain_x and y in chain_y and safe_str(row.get("中介变量")) in mediator
    moderator = _allowed_names(chain.get("moderator"))
    return (safe_str(row.get("调节变量")) in moderator
            and ((x in chain_x and y in chain_y)
                 or (x in chain_x and y in mediator)
                 or (x in mediator and y in chain_y)))


def enabled_effect_modules(global_config: Dict[str, Any]) -> List[str]:
    enabled = global_config.get("run_settings", {}).get("enabled_modules", {})
    return [m for m in EFFECT_MODULES if bool(enabled.get(m, False))]


def active_theory_chains(synthesis_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    cfg = synthesis_config.get("theory_chains", {})
    if not bool(cfg.get("enabled", True)):
        return []
    return [c for c in cfg.get("chains", []) or [] if isinstance(c, dict) and bool(c.get("enabled", False))]


def build_display_case_index(
    pass_tables: Dict[str, pd.DataFrame],
    global_config: Dict[str, Any],
    synthesis_config: Dict[str, Any],
    module_context: Optional[Dict[str, Dict[str, Any]]] = None,
) -> pd.DataFrame:
    cases, _ = build_verified_display_cases(pass_tables, global_config, synthesis_config,
                                             module_context or {})
    return cases


def build_verified_display_cases(pass_tables: Dict[str, pd.DataFrame],
                                 global_config: Dict[str, Any],
                                 synthesis_config: Dict[str, Any],
                                 module_context: Dict[str, Dict[str, Any]]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    cfg = synthesis_config.get("display_case_selection", {})
    if not bool(cfg.get("enabled", True)):
        return pd.DataFrame(columns=DISPLAY_CASE_COLUMNS), pd.DataFrame(columns=DISPLAY_REJECTION_COLUMNS)

    pass_level = cfg.get("pass_level", "strong")
    source_df = pass_tables.get(pass_level, pd.DataFrame()).copy()
    if source_df.empty:
        return pd.DataFrame(columns=DISPLAY_CASE_COLUMNS), pd.DataFrame(columns=DISPLAY_REJECTION_COLUMNS)

    required_mode = cfg.get("required_modules_mode", "enabled_modules")
    min_modules = int(cfg.get("minimum_covered_modules", 1))
    max_cases = int(cfg.get("max_cases_per_pass_level", 30))
    prefixes = cfg.get("case_id_prefix", {})
    case_prefix = prefixes.get(pass_level, f"CASE_{pass_level.upper()}")

    chains = active_theory_chains(synthesis_config)
    required_modules_global = set(enabled_effect_modules(global_config))
    rows: List[Dict[str, Any]] = []
    rejected: List[Dict[str, Any]] = []

    def reject(spec: Any, chain_id: Any, reason: str) -> None:
        rejected.append({"spec_group_id": spec, "theory_chain_id": chain_id, "reason": reason})

    for spec_group_id, g in source_df.groupby("spec_group_id", dropna=False):
        if not g.get("merge_key_complete", pd.Series(False, index=g.index)).map(bool_for_filter).all():
            reject(spec_group_id, "", "缺少合并键；该组不能生成展示案例")
            continue
        identity_fields = ("run_id", "common_fingerprint", "data_sha256", "specification_id")
        if (not g.get("provenance_verified", pd.Series(False, index=g.index)).map(bool_for_filter).all()
                or any(field not in g or g[field].map(is_blank).any() or g[field].astype(str).nunique() != 1
                       for field in identity_fields)):
            reject(spec_group_id, "", "批次、数据或公共设定身份不完整或不一致")
            continue

        candidate_chains: List[Optional[Dict[str, Any]]] = [None]
        if required_mode == "theory_chain" or bool(cfg.get("require_theory_chain", False)):
            candidate_chains = chains
            if not candidate_chains:
                reject(spec_group_id, "", "没有启用且可核验的理论链")
                continue

        for chain in candidate_chains:
            if chain:
                required_modules = set(chain.get("required_modules", []))
                required_hypotheses = set(chain.get("required_hypotheses", []))
                theory_chain_id = chain.get("chain_id")
            elif required_mode == "enabled_modules":
                required_modules = required_modules_global
                required_hypotheses = set()
                theory_chain_id = ""
            else:
                required_modules = set(cfg.get("required_modules", []))
                required_hypotheses = set()
                theory_chain_id = ""

            selected = g
            if chain:
                selected = g[g["假设编号"].astype(str).isin(required_hypotheses)].copy()
                registered = build_registered_hypothesis_map(synthesis_config)
                for hypothesis_id in required_hypotheses:
                    expected_modules = set(registered.get(hypothesis_id, {}).get("expected_modules", [])) & required_modules
                    if not expected_modules or not expected_modules.issubset(
                            set(selected.loc[selected["假设编号"].astype(str) == hypothesis_id, "source_module"].astype(str))):
                        reject(spec_group_id, theory_chain_id, f"{hypothesis_id} 未在登记的模块中通过")
                        selected = selected.iloc[0:0]
                        break
                if not selected.empty:
                    selected = selected[selected.apply(
                        lambda row: row["source_module"] in registered.get(
                            safe_str(row["假设编号"]), {}).get("expected_modules", []), axis=1)]
            if selected.empty:
                continue
            covered_modules = sorted(set(selected["source_module"].dropna().astype(str)))
            covered_hypotheses = sorted(set(selected["假设编号"].dropna().astype(str)))

            if len(covered_modules) < min_modules:
                reject(spec_group_id, theory_chain_id, "通过的模块数不足")
                continue
            if required_modules and not required_modules.issubset(set(covered_modules)):
                reject(spec_group_id, theory_chain_id, "所需模块未全部通过")
                continue
            if required_hypotheses and not required_hypotheses.issubset(set(covered_hypotheses)):
                reject(spec_group_id, theory_chain_id, "所需假设未全部通过")
                continue
            if bool(cfg.get("require_all_registered_hypotheses", False)):
                registered = build_registered_hypothesis_map(synthesis_config)
                if not set(registered.keys()).issubset(set(covered_hypotheses)):
                    reject(spec_group_id, theory_chain_id, "登记假设未全部通过")
                    continue

            invalid = [safe_str(row.get("model_id")) for _, row in selected.iterrows()
                       if row.get("source_module") not in module_context
                       or not _model_matches_hypothesis(
                           row, row["source_module"],
                           module_context[row["source_module"]].get("module_config") or {}, chain)]
            if invalid:
                reject(spec_group_id, theory_chain_id, f"模型与历史假设变量、形式或理论链不匹配: {invalid[:5]}")
                continue
            if not chain and len(covered_modules) > 1:
                reject(spec_group_id, theory_chain_id, "跨模块展示缺少已启用的完整理论链")
                continue
            mediation_valid = True
            mediation_rows = selected.loc[selected["source_module"] == "mediation_effect"]
            if not mediation_rows.empty and "chain_id" not in mediation_rows:
                mediation_valid = False
            for _, paths in (mediation_rows.groupby(["假设编号", "chain_id"], dropna=False)
                             if mediation_valid and not mediation_rows.empty else []):
                try:
                    basis = json.loads(paths.iloc[0]["hypothesis_pass_basis_json"])
                    expected_paths = set(basis["required_paths"])
                    actual_paths = set(paths["path_type"].astype(str))
                    if (not expected_paths.issubset(actual_paths)
                            or paths["path_type"].astype(str).duplicated().any()
                            or paths["hypothesis_pass_basis_json"].astype(str).nunique() != 1):
                        mediation_valid = False
                except (KeyError, TypeError, ValueError):
                    mediation_valid = False
            if not mediation_valid:
                reject(spec_group_id, theory_chain_id, "中介链所需路径缺失、重复或判定依据不一致")
                continue
            selected_models = []
            for (module, model_id), model_rows in selected.groupby(["source_module", "model_id"], sort=False):
                row = model_rows.iloc[0]
                selected_models.append({"module": module, "model_id": model_id,
                    "hypothesis_id": row.get("假设编号"), "chain_id": row.get("chain_id"),
                    "path_type": row.get("path_type"), "formula": row.get("模型公式"),
                    "terms": row.get("模型项定义"),
                    "tested_terms": [{"term": term.get("检验项"), "effect_role": term.get("effect_role")}
                                     for _, term in model_rows.iterrows()],
                    "sample_identity": row.get("sample_identity"),
                    "n": row.get("样本量（N）"), "processing_definition_id": row.get("processing_definition_id"),
                    "processing_definition_json": row.get("processing_definition_json"),
                    "processing_trace_json": row.get("processing_trace_json"),
                    "source_file": row.get("source_file"), "source_record_sha256": row.get("source_record_sha256")})
            if any(is_blank(model["formula"]) or is_blank(model["sample_identity"]) for model in selected_models):
                reject(spec_group_id, theory_chain_id, "模型公式或实际估计样本标识缺失")
                continue
            snapshots = {}
            for module in covered_modules:
                context = module_context[module]
                record = context.get("run_record") or {}
                snapshots[module] = {"run_record": str(Path(context["output_dir"]) / "run_record.json"),
                    "run_instance_id": record.get("run_instance_id"),
                    "preprocessing": record.get("preprocessing", {}),
                    "config_snapshots": record.get("snapshots", {})}
            sample_map = {f"{model['module']}:{model['model_id']}":
                          {"sample_identity": model["sample_identity"], "n": model["n"]}
                          for model in selected_models}
            sample_signatures = {(safe_str(model["sample_identity"]), safe_str(model["n"]))
                                 for model in selected_models}

            first = selected.iloc[0]
            rows.append({
                "case_id": f"{case_prefix}_{len(rows) + 1:03d}",
                "pass_level": pass_level,
                "spec_group_id": spec_group_id,
                "theory_chain_id": theory_chain_id,
                "样本门槛": first.get("样本门槛"),
                "处理方案": first.get("处理方案"),
                "是否插值": first.get("是否插值"),
                "是否缩尾": first.get("是否缩尾"),
                "控制变量组合ID": first.get("控制变量组合ID"),
                "纳入的控制变量": first.get("纳入的控制变量"),
                "纳入的可选控制变量": first.get("纳入的可选控制变量"),
                "固定效应设定": first.get("固定效应设定"),
                "聚类标准误设定": first.get("聚类标准误设定"),
                "covered_hypotheses": " ".join(covered_hypotheses),
                "covered_modules": " ".join(covered_modules),
                "main_effect_model_ids": " ".join(model["model_id"] for model in selected_models if model["module"] == "main_effect"),
                "mediation_effect_model_ids": " ".join(model["model_id"] for model in selected_models if model["module"] == "mediation_effect"),
                "moderation_effect_model_ids": " ".join(model["model_id"] for model in selected_models if model["module"] == "moderation_effect"),
                "mediation_sample_difference_note": "；".join(selected.loc[selected["source_module"] == "mediation_effect", "hypothesis_sample_difference_note"].dropna().astype(str).unique()) if "hypothesis_sample_difference_note" in selected.columns else "",
                "mediation_path_samples_json": "\n".join(selected.loc[selected["source_module"] == "mediation_effect", "hypothesis_path_samples_json"].dropna().astype(str).unique()) if "hypothesis_path_samples_json" in selected.columns else "",
                "all_required_modules_pass": not required_modules or required_modules.issubset(set(covered_modules)),
                "recommendation_reason": f"同一设定下覆盖 {len(covered_modules)} 个模块、{len(covered_hypotheses)} 个假设，达到 {pass_level} 筛选规则。",
                "run_id": first["run_id"], "common_fingerprint": first["common_fingerprint"],
                "data_sha256": first["data_sha256"], "specification_id": first["specification_id"],
                "selected_models_json": _case_json(selected_models),
                "public_specification_json": _case_json({key: first.get(key) for key in
                    synthesis_config.get("merge_keys", [])}),
                "sample_differences_json": _case_json({"same_sample": len(sample_signatures) == 1,
                    "models": sample_map}),
                "source_snapshots_json": _case_json(snapshots),
            })

            if len(rows) >= max_cases:
                return (pd.DataFrame(rows, columns=DISPLAY_CASE_COLUMNS),
                        pd.DataFrame(rejected, columns=DISPLAY_REJECTION_COLUMNS))

    return (pd.DataFrame(rows, columns=DISPLAY_CASE_COLUMNS),
            pd.DataFrame(rejected, columns=DISPLAY_REJECTION_COLUMNS))


def save_display_cases(display_df: pd.DataFrame, output_dir: Path, synthesis_config: Dict[str, Any],
                       rejected_df: Optional[pd.DataFrame] = None) -> None:
    cfg = synthesis_config.get("display_case_selection", {})
    if not bool(cfg.get("enabled", True)):
        return
    csv_name = cfg.get("output_csv", "display_case_index.csv")
    json_name = cfg.get("output_json", "display_case_index.json")
    display_df.to_csv(output_dir / csv_name, index=False, encoding="utf-8-sig")
    save_json(display_df.to_dict(orient="records"), output_dir, json_name, synthesis_config)
    (rejected_df if rejected_df is not None else pd.DataFrame(columns=DISPLAY_REJECTION_COLUMNS)).to_csv(
        output_dir / cfg.get("rejections_csv", "display_case_rejections.csv"),
        index=False, encoding="utf-8-sig")
    bridge = synthesis_config.get("dofile_bridge", {})
    if bridge.get("enabled", True):
        cases = []
        for row in display_df.to_dict(orient="records"):
            item = {key: value for key, value in row.items() if not key.endswith("_json")}
            for key in ("selected_models", "public_specification", "sample_differences", "source_snapshots"):
                item[key] = json.loads(row[f"{key}_json"])
            cases.append(item)
        (output_dir / bridge.get("case_manifest_file", "display_case_manifest.json")).write_text(
            _case_json({"schema_version": "2.0-display-case-1",
                        "statistics_scope": "executed_specifications", "cases": cases}, indent=2),
            encoding="utf-8")


# =========================
# 配置快照与 summary
# =========================

def save_config_snapshots(
    output_dir: Path,
    global_config: Dict[str, Any],
    synthesis_config: Dict[str, Any],
    module_context: Dict[str, Dict[str, Any]],
) -> None:
    cfg = synthesis_config.get("config_snapshot", {})
    if not (bool(global_config.get("output_settings", {}).get("save_config_snapshot", True))
            and bool(cfg.get("enabled", True))):
        return

    snapshot_dir = ensure_dir(output_dir / cfg.get("snapshot_dir", "config_snapshot"))
    files: List[Tuple[Path, Path]] = []
    if bool(cfg.get("include_synthesis_config", True)):
        files.append((Path(synthesis_config["_config_path"]), snapshot_dir / "synthesis.yaml"))
    # The current global file controls this consolidation, but is clearly labelled
    # separately from the global files that produced historical regressions.
    files.append((Path(global_config["_config_path"]), snapshot_dir / "synthesis_input_config.yaml"))
    for module_name in EFFECT_MODULES:
        context = module_context.get(module_name, {})
        if not context.get("will_be_included"):
            continue
        record = context["run_record"]
        source_dir = Path(context["output_dir"])
        if bool(cfg.get("include_global_config", True)):
            source = source_dir / record["snapshots"]["analysis_config.yaml"]["path"]
            files.append((source, snapshot_dir / module_name / "analysis_config.yaml"))
        if bool(cfg.get("include_module_configs", True)):
            source = source_dir / record["snapshots"][f"{module_name}.yaml"]["path"]
            files.append((source, snapshot_dir / module_name / f"{module_name}.yaml"))
    for source, target in files:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)


def build_summary_text(
    prepared: Dict[str, Any],
    pass_tables: Dict[str, pd.DataFrame],
    curve_df: pd.DataFrame,
    curve_summary_df: pd.DataFrame,
    display_df: pd.DataFrame,
    warnings: List[str],
) -> str:
    global_config = prepared["global_config"]
    synthesis_config = prepared["synthesis_config"]
    lines = [
        "# Result Synthesis Summary",
        "",
        f"- 项目名称：{global_config.get('project', {}).get('name', '')}",
        f"- 样本目录：{get_sample_folder_name(global_config)}",
        f"- 全局配置：{global_config.get('_config_path', '')}",
        f"- 结果整合配置：{synthesis_config.get('_config_path', '')}",
        f"- 输出目录：{prepared['output_dir']}",
        "",
        "## 已执行设定的统计",
        "",
        f"- 纳入标准化结果行数：{len(prepared['merged_results_df'])}",
        f"- require 综合表行数：{len(pass_tables.get('require', pd.DataFrame()))}",
        f"- full 综合表行数：{len(pass_tables.get('full', pd.DataFrame()))}",
        f"- strong 综合表行数：{len(pass_tables.get('strong', pd.DataFrame()))}",
        f"- weak 综合表行数：{len(pass_tables.get('weak', pd.DataFrame()))}",
        f"- 设定曲线数据行数：{len(curve_df)}",
        f"- 设定曲线摘要行数：{len(curve_summary_df)}",
        f"- 可展示案例数：{len(display_df)}",
        "",
        "## 模块结构核对",
        "",
        dataframe_to_markdown(prepared["model_check_df"], max_rows=50),
        "",
        "## 假设完整性核对",
        "",
        dataframe_to_markdown(prepared["hypothesis_check_df"], max_rows=100),
    ]
    all_warnings = list(prepared.get("read_warnings", [])) + warnings
    if all_warnings:
        lines.extend(["", "## 运行提醒", ""])
        lines.extend([f"- {w}" for w in all_warnings])
    return "\n".join(lines) + "\n"


# =========================
# 完整运行入口
# =========================

def _execute_result_synthesis(prepared: Dict[str, Any], output_dir: Path,
                              progress: Any = None) -> Dict[str, Any]:
    synthesis_config = prepared["synthesis_config"]
    ensure_dir(output_dir)

    def advance() -> None:
        if progress is not None:
            progress.update(1)

    write_check_reports(
        output_dir=output_dir,
        synthesis_config=synthesis_config,
        model_check_df=prepared["model_check_df"],
        hypothesis_check_df=prepared["hypothesis_check_df"],
    )
    advance()

    full_rows, spec_warnings = add_spec_group_id(prepared["merged_results_df"], synthesis_config)
    pass_tables, hypothesis_detail = build_specification_tables(
        full_rows, synthesis_config, prepared["module_context"])
    advance()
    if synthesis_config.get("output", {}).get("save_csv", True):
        full_rows.to_csv(output_dir / "merged_model_detail_results.csv", index=False, encoding="utf-8-sig")
    save_pass_tables(output_dir, synthesis_config, pass_tables)
    save_dataframe(hypothesis_detail, output_dir, "hypothesis_status_detail", synthesis_config)
    save_overview_workbook(output_dir, synthesis_config, pass_tables, hypothesis_detail)
    advance()

    curve_df, curve_summary_df, plot_warnings = save_curve_outputs(
        full_df=full_rows,
        global_config=prepared["global_config"],
        synthesis_config=synthesis_config,
        output_dir=output_dir,
    )
    advance()

    row_tables = build_row_pass_tables(full_rows, synthesis_config)
    for level in ("strong", "weak"):
        accepted = set(pass_tables[level]["specification_id"])
        row_tables[level] = row_tables[level].loc[
            row_tables[level]["specification_id"].isin(accepted)].copy()
    display_df, rejected_df = build_verified_display_cases(
        row_tables, prepared["global_config"], synthesis_config, prepared["module_context"])
    save_display_cases(display_df, output_dir, synthesis_config, rejected_df)
    advance()
    save_config_snapshots(output_dir, prepared["global_config"], synthesis_config, prepared["module_context"])

    warnings = spec_warnings + plot_warnings
    summary_text = build_summary_text(prepared, pass_tables, curve_df, curve_summary_df, display_df, warnings)
    summary_file = synthesis_config.get("output", {}).get("summary_file", "summary.md")
    (output_dir / summary_file).write_text(summary_text, encoding="utf-8")
    advance()

    print("=" * 60)
    print("结果整合计算完成，正在校验并发布结果")
    print(f"输出目录: {prepared['output_dir'].resolve()}")
    print(f"require 设定数: {len(pass_tables.get('require', pd.DataFrame()))}")
    print(f"full 行数: {len(pass_tables.get('full', pd.DataFrame()))}")
    print(f"strong 行数: {len(pass_tables.get('strong', pd.DataFrame()))}")
    print(f"weak 行数: {len(pass_tables.get('weak', pd.DataFrame()))}")
    print(f"设定曲线摘要行数: {len(curve_summary_df)}")
    print(f"可展示案例数: {len(display_df)}")
    if warnings:
        print("运行提醒:")
        for warning in warnings:
            print(f"- {warning}")
    print("=" * 60)

    return {
        **prepared,
        "full_df": pass_tables.get("full", pd.DataFrame()),
        "require_df": pass_tables.get("require", pd.DataFrame()),
        "strong_df": pass_tables.get("strong", pd.DataFrame()),
        "weak_df": pass_tables.get("weak", pd.DataFrame()),
        "hypothesis_detail_df": hypothesis_detail,
        "curve_df": curve_df,
        "curve_summary_df": curve_summary_df,
        "display_case_df": display_df,
        "warnings": warnings,
    }


def run_result_synthesis(config_path: str | Path = "analysis_config.yaml") -> Dict[str, Any]:
    global_config = load_yaml(config_path)
    if not bool(global_config.get("run_settings", {}).get("enabled_modules", {}).get(RESULT_SYNTHESIS_MODULE, True)):
        print("全局 YAML 中 run_settings.enabled_modules.result_synthesis = false，结果整合模块跳过。")
        return {}
    synthesis_config = load_synthesis_yaml(global_config)
    if not bool(synthesis_config.get("enabled", True)):
        print("synthesis.yaml 中 enabled = false，结果整合模块跳过。")
        return {}
    progress = tqdm(total=8, desc="结果整合", file=sys.stdout, unit="步") if tqdm else None
    try:
        prepared = prepare_synthesis_inputs(config_path)
        if progress is not None:
            progress.update(1)
        included = {module: context["source_file"] for module, context in prepared["module_context"].items()
                    if context.get("will_be_included")}
        result: Dict[str, Any] = {}
        record = run_synthesis(
            prepared["global_config"], prepared["synthesis_config"], SCRIPT_PATH,
            prepared["output_dir"], included,
            lambda stage: result.update(_execute_result_synthesis(prepared, stage, progress)))
        if progress is not None:
            progress.update(1)
    finally:
        if progress is not None:
            progress.close()
    result["run_record"] = record
    print(f"结果整合模块运行完成；结果已输出到: {prepared['output_dir']}")
    return result


# =========================
# 前半部分入口
# =========================

def prepare_synthesis_inputs(config_path: str | Path = "analysis_config.yaml") -> Dict[str, Any]:
    global_config = load_yaml(config_path)
    synthesis_config = load_synthesis_yaml(global_config)
    preflight_synthesis(global_config, synthesis_config)

    if not bool(global_config.get("run_settings", {}).get("enabled_modules", {}).get(RESULT_SYNTHESIS_MODULE, True)):
        raise RuntimeError("全局 YAML 中 run_settings.enabled_modules.result_synthesis = false，结果整合模块跳过。")

    if not bool(synthesis_config.get("enabled", True)):
        raise RuntimeError("synthesis.yaml 中 enabled = false，结果整合模块跳过。")

    output_dir = get_synthesis_output_dir(global_config, synthesis_config)
    model_check_df, module_context = build_model_structure_check(global_config, synthesis_config)
    unavailable = model_check_df.loc[
        model_check_df["module_name"].isin(get_requested_modules(global_config, synthesis_config))
        & model_check_df["enabled_in_global_config"]
        & (model_check_df["enabled_in_module_config"] | ~model_check_df["module_config_exists"])
        & ~model_check_df["will_be_included"]]
    if not unavailable.empty:
        reasons = "；".join(f"{row.module_name}: {row.warning}"
                           for row in unavailable.itertuples())
        raise RuntimeError(f"当前批次无法进入 synthesis：{reasons}")
    merged_results_df, read_warnings = read_all_module_results(module_context, global_config)
    hypothesis_check_df = build_hypothesis_coverage_check(synthesis_config, module_context, merged_results_df)

    enforce_first_half_stop_rules(
        global_config=global_config,
        synthesis_config=synthesis_config,
        model_check_df=model_check_df,
        merged_results_df=merged_results_df,
        hypothesis_check_df=hypothesis_check_df,
    )

    return {
        "global_config": global_config,
        "synthesis_config": synthesis_config,
        "output_dir": output_dir,
        "model_check_df": model_check_df,
        "module_context": module_context,
        "merged_results_df": merged_results_df,
        "hypothesis_check_df": hypothesis_check_df,
        "read_warnings": read_warnings,
    }


def run_result_synthesis_first_half(config_path: str | Path = "analysis_config.yaml") -> Dict[str, Any]:
    prepared = prepare_synthesis_inputs(config_path)

    print("=" * 60)
    print("结果整合模块：前半部分检查完成")
    print(f"全局配置文件: {Path(prepared['global_config'].get('_config_path', config_path)).resolve()}")
    print(f"结果整合配置文件: {Path(prepared['synthesis_config'].get('_config_path')).resolve()}")
    print(f"输出目录: {prepared['output_dir'].resolve()}")
    print(f"纳入结果行数: {len(prepared['merged_results_df'])}")
    print(f"假设覆盖记录数: {len(prepared['hypothesis_check_df'])}")
    if prepared["read_warnings"]:
        print("读取提醒:")
        for warning in prepared["read_warnings"]:
            print(f"- {warning}")
    print("=" * 60)

    return prepared


if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "analysis_config.yaml"
    run_result_synthesis(cfg_path)
