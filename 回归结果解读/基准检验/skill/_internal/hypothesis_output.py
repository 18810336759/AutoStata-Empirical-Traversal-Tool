"""Hypothesis-level decisions over fitted model and mediation-path records."""

from __future__ import annotations

import json
import math
from typing import Any, Mapping

import numpy as np
import pandas as pd


HYPOTHESIS_COLUMNS = [
    "run_id", "run_instance_id", "common_fingerprint", "data_sha256", "module_revision",
    "specification_id", "model_id", "模块名称", "假设编号", "假设描述",
    "chain_id", "检验阶段", "样本门槛", "处理方案", "控制变量组合ID",
    "纳入的控制变量", "纳入的可选控制变量", "固定效应设定", "聚类标准误设定",
    "是否成功运行", "是否候选模型", "strong_pass", "weak_pass",
    "required_paths", "present_paths", "missing_paths", "path_model_ids_json",
    "path_samples_json", "path_sample_identity_same", "path_n_same",
    "sample_difference_note", "判定规则", "判定依据", "pass_basis_json",
]


def _value(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(value, np.generic):
        return value.item()
    return value


def _truth(value: Any) -> bool:
    cleaned = _value(value)
    return isinstance(cleaned, (bool, np.bool_)) and bool(cleaned)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str,
                      separators=(",", ":"))


def align_hypothesis_columns(frame: pd.DataFrame) -> pd.DataFrame:
    result = frame.copy()
    for column in HYPOTHESIS_COLUMNS:
        if column not in result:
            result[column] = None
    for column in ("是否候选模型", "strong_pass", "weak_pass",
                   "path_sample_identity_same", "path_n_same"):
        result[column] = result[column].map(
            lambda value: _truth(value) if _value(value) is not None else pd.NA).astype("boolean")
    result["样本门槛"] = pd.to_numeric(result["样本门槛"], errors="coerce").astype("Int64")
    return result[HYPOTHESIS_COLUMNS + [c for c in result if c not in HYPOTHESIS_COLUMNS]]


def _common(row: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _value(row.get(key)) for key in HYPOTHESIS_COLUMNS
            if key not in {"是否候选模型", "strong_pass", "weak_pass",
                           "是否成功运行", "判定规则", "判定依据", "pass_basis_json",
                           "required_paths", "present_paths", "missing_paths",
                           "path_model_ids_json", "path_samples_json",
                           "path_sample_identity_same", "path_n_same", "sample_difference_note"}}


def single_model_hypotheses(frame: pd.DataFrame, module: str,
                            module_config: Mapping[str, Any]) -> pd.DataFrame:
    """One decision per fitted main or moderation model; retain term evidence."""
    rows = []
    settings = module_config.get(f"{module.replace('_effect', '')}_effect_settings", {})
    # Main uses main_effect_settings, moderation uses moderation_settings.
    if module == "moderation_effect":
        settings = module_config.get("moderation_settings", {})
    for _, source in frame.iterrows():
        row = source.to_dict()
        phase = _value(row.get("检验阶段")) or ("C" if _value(row.get("strong_pass")) is not None else "AB")
        phase = "C" if str(phase).startswith("C") else "AB"
        success = row.get("是否成功运行") == "success"
        candidate = bool(success and _truth(row.get("是否候选模型")))
        strong = bool(success and phase == "C" and _truth(row.get("strong_pass")))
        weak = bool(strong or (success and phase == "C" and _truth(row.get("weak_pass"))))
        if module == "main_effect":
            terms = str(_value(row.get("检验项组合")) or "").split()
            evidence = {term: {"beta": _value(row.get(f"beta_{term}")),
                               "p": _value(row.get(f"p_{term}")),
                               "direction_match": _value(row.get(f"direction_match_{term}"))}
                        for term in terms}
            rule = row.get("通过规则") or "all_required_terms"
            configured = next((hyp for hyp in module_config.get("hypotheses", [])
                               if str(hyp.get("id")) == str(row.get("假设编号"))), {})
        else:
            term = _value(row.get("检验项"))
            evidence = {str(term): {"beta": _value(row.get("beta")),
                                    "p": _value(row.get("p_interaction")),
                                    "direction_match": _value(row.get("direction_match")),
                                    "direction_match_scope": _value(row.get("direction_match_scope")),
                                    "focal_relation_check": _value(row.get("focal_relation_check")),
                                    "curve_shape_check": _value(row.get("curve_shape_check")),
                                    "turning_point_check": _value(row.get("turning_point_check")),
                                    "curvature_crosses_zero": _value(row.get("curvature_crosses_zero"))}}
            rule = "moderation_candidate_or_full_check"
            configured = {"expected_change": _value(row.get("expected_change")),
                          "focal_relationship_form": _value(row.get("focal_relationship_form")),
                          "moderation_form": _value(row.get("moderation_form")),
                          "ab_screen_pass": _value(row.get("ab_screen_pass")),
                          "full_hypothesis_pass": _value(row.get("full_hypothesis_pass"))}
        thresholds = {"candidate": settings.get("candidate_rules", {}).get("p_value_threshold"),
                      "strong": settings.get("combination_screening", {}).get("result_labels", {}).get("strong_pass"),
                      "weak": settings.get("combination_screening", {}).get("result_labels", {}).get("weak_pass")}
        basis = {"schema_version": "2.0-hypothesis-1", "phase": phase,
                 "rule": rule, "thresholds": thresholds, "term_evidence": evidence,
                 "configured_hypothesis": configured,
                 "model_form": _value(row.get("模型形式")),
                 "failure": _value(row.get("失败原因"))}
        output = _common(row)
        output.update({"模块名称": module, "检验阶段": phase,
                       "是否成功运行": "success" if success else "failed",
                       "是否候选模型": candidate, "strong_pass": strong, "weak_pass": weak,
                       "判定规则": rule,
                       "判定依据": ("模型失败" if not success else
                                "阶段 C 完整判定通过" if strong else
                                "阶段 C 达到弱通过" if weak else
                                "阶段 A/B 达到候选条件" if candidate else "未达到当前阶段判定条件"),
                       "pass_basis_json": _json(basis),
                       "path_model_ids_json": _json([row.get("model_id")])})
        rows.append(output)
    return align_hypothesis_columns(pd.DataFrame(rows))


def mediation_required_paths(chain: Mapping[str, Any], settings: Mapping[str, Any]) -> list[str]:
    if chain.get("pass_rule") == "core_mediation_paths":
        flags = {"require_x_to_y": False, "require_x_to_m": True, "require_m_to_y": True}
    else:
        key = "two_step_requirements" if chain.get("screening_method") == "two_step" else "three_step_requirements"
        flags = settings["candidate_rules"][key]
    return [name for flag, name in (("require_x_to_y", "x_to_y"),
            ("require_x_to_m", "x_to_m"), ("require_m_to_y", "x_m_to_y"))
            if flags.get(flag, False)]


def assess_mediation_chain(records: list[Mapping[str, Any]], chain: Mapping[str, Any],
                           settings: Mapping[str, Any], minimum_n: int,
                           phase: str) -> dict[str, Any]:
    """Every required path must belong to this one specification and chain."""
    required = mediation_required_paths(chain, settings)
    by_path: dict[str, Mapping[str, Any]] = {}
    duplicates = []
    for record in records:
        name = _value(record.get("path_type"))
        if name in by_path:
            duplicates.append(name)
        else:
            by_path[name] = record
    specs = {_value(record.get("specification_id")) for record in records}
    chains = {_value(record.get("chain_id")) for record in records}
    identity_mismatch = len(specs) != 1 or None in specs or len(chains) != 1 or None in chains
    missing = [name for name in required if name not in by_path]
    candidate_rules = settings["candidate_rules"]
    thresholds = {"candidate": candidate_rules["p_value_threshold"],
                  "strong": settings["combination_screening"]["result_labels"]["strong_pass"],
                  "weak": settings["combination_screening"]["result_labels"]["weak_pass"]}
    evidence: dict[str, Any] = {}
    for name, rec in by_path.items():
        p = _value(rec.get("p"))
        try:
            p = float(p) if p is not None and math.isfinite(float(p)) else None
        except (TypeError, ValueError):
            p = None
        n = _value(rec.get("样本量（N）"))
        evidence[name] = {"model_id": _value(rec.get("model_id")), "p": p,
                          "direction_match": _value(rec.get("direction_match")),
                          "n": n, "sample_identity": _value(rec.get("sample_identity")),
                          "success": rec.get("是否成功运行") == "success",
                          "failure": _value(rec.get("失败原因"))}

    def passes(level: str) -> bool:
        if missing or duplicates or identity_mismatch or not required:
            return False
        for name in required:
            item = evidence[name]
            if not item["success"] or item["p"] is None or item["p"] >= thresholds[level]:
                return False
            if candidate_rules["require_direction_match"] and not _truth(item["direction_match"]):
                return False
            if candidate_rules["minimum_sample_size_required"] and (item["n"] is None or item["n"] < minimum_n):
                return False
        if candidate_rules["x_weakened_after_m_required"]:
            second = by_path.get("x_m_to_y", {})
            if not _truth(second.get("x_weakened_after_m")):
                return False
        return True

    samples = {name: {"n": item["n"], "sample_identity": item["sample_identity"]}
               for name, item in evidence.items()}
    identities = [samples[name]["sample_identity"] for name in required if name in samples]
    ns = [samples[name]["n"] for name in required if name in samples]
    identity_same = len(identities) == len(required) and all(v is not None for v in identities) and len(set(identities)) == 1
    n_same = len(ns) == len(required) and all(v is not None for v in ns) and len(set(ns)) == 1
    note = ("所需路径样本相同" if identity_same else
            "所需路径样本标识不同或无法确认；此差异不影响链级通过判定")
    candidate = passes("candidate")
    strong = phase == "C" and passes("strong")
    weak = strong or (phase == "C" and passes("weak"))
    basis = {"schema_version": "2.0-hypothesis-1", "phase": phase,
             "screening_method": chain.get("screening_method"),
             "pass_rule": chain.get("pass_rule"), "required_paths": required,
             "missing_paths": missing, "duplicate_paths": duplicates,
             "identity_mismatch": identity_mismatch,
             "thresholds": thresholds, "path_evidence": evidence,
             "x_weakened_after_m_required": candidate_rules["x_weakened_after_m_required"]}
    return {"required_paths": " ".join(required),
            "present_paths": " ".join(n for n in ("x_to_y", "x_to_m", "x_m_to_y") if n in by_path),
            "missing_paths": " ".join(missing),
            "path_model_ids_json": _json({name: item["model_id"] for name, item in evidence.items()}),
            "path_samples_json": _json(samples),
            "path_sample_identity_same": identity_same, "path_n_same": n_same,
            "sample_difference_note": note,
            "是否成功运行": "success" if not missing and not duplicates and not identity_mismatch and all(
                evidence[name]["success"] for name in required) else "failed",
            "是否候选模型": candidate, "strong_pass": strong, "weak_pass": weak,
            "判定规则": chain.get("pass_rule") or "all_required_paths",
            "判定依据": ("缺少所需路径: " + ",".join(missing) if missing else
                     "同一设定中存在重复路径: " + ",".join(duplicates) if duplicates else
                     "路径的公共设定或机制链不一致" if identity_mismatch else
                     "所需路径达到强通过" if strong else "所需路径达到弱通过" if weak else
                     "所需路径达到候选条件" if candidate else "所需路径未全部达到当前判定条件"),
            "pass_basis_json": _json(basis)}


def mediation_hypotheses(frame: pd.DataFrame, chains: list[Mapping[str, Any]],
                         settings: Mapping[str, Any], minimum_n: int) -> pd.DataFrame:
    chain_map = {chain["chain_id"]: chain for chain in chains}
    rows = []
    if frame.empty:
        return align_hypothesis_columns(pd.DataFrame())
    keys = ["specification_id", "chain_id", "检验阶段"]
    for _, group in frame.groupby(keys, dropna=False, sort=False):
        source = group.iloc[0].to_dict()
        chain = chain_map.get(source.get("chain_id"), {})
        phase = source.get("检验阶段") or "AB"
        decision = assess_mediation_chain(group.to_dict("records"), chain,
                                          settings, minimum_n, phase)
        output = _common(source)
        output["model_id"] = f"mediation_chain_{source.get('chain_id')}_{source.get('specification_id')}_{phase}"
        output["模块名称"] = "mediation_effect"
        output["检验阶段"] = phase
        output.update(decision)
        rows.append(output)
    return align_hypothesis_columns(pd.DataFrame(rows))
