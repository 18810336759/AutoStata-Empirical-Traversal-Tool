#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
run_mediation_effect.py

中介效应基准检验版本（已移除 Sobel / Bootstrap）

阶段A/B：
- 对每条 mediation hypothesis 执行 4 个样本门槛 × 4 个处理方案 = 16 个基准检验
- 每个 task 下展开三条路径：
    1) x_to_y
    2) x_to_m
    3) x_m_to_y
- 按 candidate 规则识别 candidate task

阶段C：
- 仅对 candidate task 执行 optional_controls 组合筛查
- 按同一控制变量组合执行当前判定规则所需的全部路径
- 输出 control_combination_results / strong_pass / weak_pass

说明：
- 当前版本不执行 Sobel / Bootstrap
- 间接效应正式确认应在后续稳健性检验模块中完成

运行：
python3 -u skill/mediation_effect/run_mediation_effect.py config/analysis_config.yaml

若不传入配置路径，默认读取当前基准检验目录下的：
config/analysis_config.yaml
"""

from __future__ import annotations

import itertools
import random
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf
import yaml

try:
    from tqdm import tqdm
except ImportError:
    def tqdm(iterable=None, total=None, desc=None, leave=True):
        return iterable if iterable is not None else range(total or 0)


# =========================
# 基础工具
# =========================

SCRIPT_PATH = Path(__file__).resolve()
BASELINE_DIR = SCRIPT_PATH.parents[2]
PROJECT_ROOT = SCRIPT_PATH.parents[4]
DEFAULT_CONFIG_PATH = BASELINE_DIR / "config" / "analysis_config.yaml"

sys.path.insert(0, str(SCRIPT_PATH.parents[1]))
sys.path.insert(0, str(SCRIPT_PATH.parents[1] / "other"))
sys.path.insert(0, str(PROJECT_ROOT))
from 回归结果解读.基准检验.skill.other.path_utils import resolve_config_path, get_project_output_root, check_output_overwrite
from 回归结果解读.基准检验.skill.other.config_validation import load_config_file, preflight
from 回归结果解读.基准检验.skill.other.preprocessing import PreparedDataCache, prepare_data, select_estimation_sample, save_processing_definitions, treatment_names
from 回归结果解读.基准检验.skill.other.result_schema import align_standard_columns, align_human_result_columns, build_specification_index, preserve_module_diagnostics
from _internal.absorbed_fe import fit_absorbed_ols
from _internal.run_provenance import run_effect, annotate
from _internal.statistics_output import regression_statistics, model_terms_definition, output_policy, write_result_table, apply_standardized_options
from _internal.hypothesis_output import mediation_hypotheses
from _internal.progress import stage_progress


def default_config_path() -> Path:
    return DEFAULT_CONFIG_PATH

def load_yaml(yaml_path: str | Path) -> Dict[str, Any]:
    yaml_text = str(yaml_path).strip()
    yaml_path = Path(yaml_text)

    if not yaml_path.is_absolute() and yaml_path.name in {"analysis_config.yaml", "analysis_config.yml"}:
        baseline_config = default_config_path()
        if baseline_config.exists():
            yaml_path = baseline_config

    candidate_paths = [yaml_path]

    if str(yaml_path).endswith(".yamly"):
        candidate_paths.append(Path(str(yaml_path)[:-1]))

    if yaml_path.suffix == "":
        candidate_paths.append(Path(str(yaml_path) + ".yaml"))
        candidate_paths.append(Path(str(yaml_path) + ".yml"))

    if yaml_path.suffix == ".yml":
        candidate_paths.append(yaml_path.with_suffix(".yaml"))
    if yaml_path.suffix == ".yaml":
        candidate_paths.append(yaml_path.with_suffix(".yml"))

    seen = set()
    unique_candidates = []
    for p in candidate_paths:
        rp = str(p)
        if rp not in seen:
            seen.add(rp)
            unique_candidates.append(p)

    for p in unique_candidates:
        if p.exists():
            return load_config_file(p)

    tried = "\n".join([str(p.resolve()) for p in unique_candidates])
    raise FileNotFoundError(f"未找到配置文件。已尝试以下路径：\n{tried}")



def load_module_yaml(global_config: Dict[str, Any], module_name: str) -> Dict[str, Any]:
    module_files = global_config.get("module_config_files", {})
    module_path = module_files.get(module_name)
    if not module_path:
        raise KeyError(f"全局 YAML 缺少 module_config_files.{module_name}")

    resolved_path = resolve_config_path(module_path, global_config, must_exist=True)
    module_config = load_yaml(resolved_path)

    global_version = str(global_config.get("config_version", "")).strip()
    module_version = str(module_config.get("config_version", "")).strip()
    if global_version and module_version and global_version != module_version:
        raise ValueError(
            f"全局 YAML config_version={global_version} 与 {module_name} YAML "
            f"config_version={module_version} 不一致"
        )

    declared_module = str(module_config.get("module_name", module_name)).strip()
    if declared_module != module_name:
        raise ValueError(f"模块 YAML 的 module_name 应为 {module_name}，当前为 {declared_module}")

    return module_config


def load_data(input_file: str | Path, config: Dict[str, Any]) -> pd.DataFrame:
    input_file = resolve_config_path(input_file, config, must_exist=True)
    if not input_file.exists():
        raise FileNotFoundError(f"未找到数据文件: {input_file.resolve()}")
    suffix = input_file.suffix.lower()
    if suffix == ".dta":
        return pd.read_stata(input_file)
    if suffix == ".csv":
        return pd.read_csv(input_file)
    if suffix == ".xlsx":
        return pd.read_excel(input_file)
    raise ValueError(f"当前仅支持 .dta / .csv / .xlsx，收到: {input_file}")


def safe_str_split(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    return [v.strip() for v in str(value).split() if v.strip()]


def ensure_dir(path: str | Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def sanitize_name(name: str) -> str:
    s = str(name).strip()
    for ch in [" ", "/", "\\", ":", "*", "?", "\"", "<", ">", "|", "（", "）", "(", ")", ",", "，", "；", ";"]:
        s = s.replace(ch, "_")
    while "__" in s:
        s = s.replace("__", "_")
    return s.strip("_")


def quote_var(name: str) -> str:
    escaped = str(name).replace('"', '\\"')
    return f'Q("{escaped}")'




def get_sample_folder_name(config: Dict[str, Any]) -> str:
    sample_info = config.get("data", {}).get("sample_info", {})
    sample_period = sample_info.get("sample_period", "unknown_period")
    sample_rules = sample_info.get("sample_rules", [])
    if not sample_rules:
        rules_text = "全样本"
    else:
        rules_text = "、".join([str(x) for x in sample_rules if str(x).strip()])
        if not rules_text:
            rules_text = "全样本"
    return f"{sample_period}（{rules_text}）"


# =========================
# 配置读取
# =========================

def parse_continuous_settings(config: Dict[str, Any]) -> Dict[str, set]:
    c = config.get("continuous_variable_settings", {})
    return {
        "continuous_variables": set(safe_str_split(c.get("continuous_variables"))),
        "interpolate_allowed": set(safe_str_split(c.get("interpolate_allowed"))),
        "winsorize_allowed": set(safe_str_split(c.get("winsorize_allowed"))),
        "exclude_variables": set(safe_str_split(c.get("exclude_variables"))),
    }


def get_controls(config: Dict[str, Any]) -> Dict[str, Any]:
    controls = config.get("controls", {})
    combination_rules = controls.get("combination_rules", {})
    combination_execution = config.get("combination_execution", controls.get("combination_execution", {}))

    max_k = combination_rules.get("max_optional_controls_in_combination")
    max_k = None if max_k in (None, "", "null") else int(max_k)

    top_k_limit = combination_execution.get("top_k_limit")
    top_k_limit = None if top_k_limit in (None, "", "null") else int(top_k_limit)

    return {
        "required_controls": safe_str_split(controls.get("required_controls")),
        "optional_controls": safe_str_split(controls.get("optional_controls")),
        "active_optional_controls": safe_str_split(controls.get("active_optional_controls")),
        "combination_rules": {
            "enabled": bool(combination_rules.get("enabled", True)),
            "run_only_for_candidates": bool(combination_rules.get("run_only_for_candidates", True)),
            "min_optional_controls_in_combination": int(combination_rules.get("min_optional_controls_in_combination", 0)),
            "max_optional_controls_in_combination": max_k,
        },
        "combination_execution": {
            "mode": combination_execution.get("mode", "sampling_enumeration"),
            "early_stop": bool(combination_execution.get("early_stop", False)),
            "top_k_limit": top_k_limit,
        }
    }


def get_control_search_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    raw = config.get("control_search_settings", {})
    combination_execution = config.get("combination_execution", {})
    layer_sampling = raw.get("layer_sampling", {})
    retention_rules = raw.get("retention_rules", {})
    runtime_limits = raw.get("runtime_limits", {})

    keep_per_size = retention_rules.get("keep_per_size")
    keep_per_size = None if keep_per_size in (None, "", "null") else int(keep_per_size)

    keep_global_extra = retention_rules.get("keep_global_extra")
    keep_global_extra = None if keep_global_extra in (None, "", "null") else int(keep_global_extra)

    return {
        "enabled": bool(raw.get("enabled", True)),
        "method": raw.get("method", combination_execution.get("mode", "sampling_enumeration")),
        "random_seed": int(raw.get("random_seed", combination_execution.get("random_seed", 12345))),
        "max_candidates_to_expand": int(raw.get("max_candidates_to_expand", combination_execution.get("max_candidates_to_expand", 50))),
        "layer_sampling": {
            "random_seeds_per_size": int(layer_sampling.get("random_seeds_per_size", combination_execution.get("random_seeds_per_size", 20))),
            "local_expansions_per_seed": int(layer_sampling.get("local_expansions_per_seed", combination_execution.get("local_expansions_per_seed", 0))),
        },
        "retention_rules": {
            "keep_per_size": keep_per_size,
            "keep_global_extra": keep_global_extra,
            "force_keep_strong_pass": bool(retention_rules.get("force_keep_strong_pass", True)),
            "force_keep_weak_pass": bool(retention_rules.get("force_keep_weak_pass", True)),
            "force_keep_full_model": bool(retention_rules.get("force_keep_full_model", True)),
            "prefer_diversity": bool(retention_rules.get("prefer_diversity", True)),
        },
        "runtime_limits": {
            "max_runtime_seconds_per_candidate": int(runtime_limits.get("max_runtime_seconds_per_candidate", 60)),
        }
    }


def get_fe_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    model_settings = config.get("model_settings", {})
    fe = model_settings.get("fixed_effects", {})
    cluster = model_settings.get("clustered_standard_errors", {})

    return {
        "entity_fixed_effect": bool(fe.get("entity_fixed_effect", False)),
        "entity_name": fe.get("entity_name"),
        "time_fixed_effect": bool(fe.get("time_fixed_effect", False)),
        "time_name": fe.get("time_name"),
        "cluster_enabled": bool(cluster.get("enabled", False)),
        "cluster_level": cluster.get("cluster_level"),
        "minimum_sample_size": int(model_settings.get("minimum_sample_size", 100)),
    }


def get_output_root(config: Dict[str, Any]) -> Path:
    return get_project_output_root(config)


def get_preprocessing_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    preprocessing = config.get("preprocessing", {})
    sample_thresholds = [int(x) for x in preprocessing.get("sample_thresholds", [2, 3, 4, 5])]

    return {
        "sample_thresholds": sample_thresholds,
        "treatment_names": treatment_names(config),
    }


def get_mediation_settings(module_config: Dict[str, Any]) -> Dict[str, Any]:
    med = module_config.get("mediation_settings", {})
    candidate_rules = med.get("candidate_rules", {})
    combination_screening = med.get("combination_screening", {})
    result_labels = combination_screening.get("result_labels", {})
    three_req = candidate_rules.get("three_step_requirements", {})
    two_req = candidate_rules.get("two_step_requirements", {})

    return {
        "candidate_rules": {
            "p_value_threshold": float(candidate_rules.get("p_value_threshold", 0.05)),
            "require_direction_match": bool(candidate_rules.get("require_direction_match", True)),
            "minimum_sample_size_required": bool(candidate_rules.get("minimum_sample_size_required", True)),
            "x_weakened_after_m_required": bool(candidate_rules.get("x_weakened_after_m_required", False)),
            "three_step_requirements": {
                "require_x_to_y": bool(three_req.get("require_x_to_y", True)),
                "require_x_to_m": bool(three_req.get("require_x_to_m", True)),
                "require_m_to_y": bool(three_req.get("require_m_to_y", True)),
            },
            "two_step_requirements": {
                "require_x_to_y": bool(two_req.get("require_x_to_y", False)),
                "require_x_to_m": bool(two_req.get("require_x_to_m", True)),
                "require_m_to_y": bool(two_req.get("require_m_to_y", True)),
            },
        },
        "combination_screening": {
            "enabled": bool(combination_screening.get("enabled", True)),
            "run_only_for_candidates": bool(combination_screening.get("run_only_for_candidates", True)),
            "keep_all_strong_pass": bool(combination_screening.get("keep_all_strong_pass", True)),
            "keep_all_weak_pass": bool(combination_screening.get("keep_all_weak_pass", True)),
            "apply_to_paths": combination_screening.get("apply_to_paths", ["x_to_m", "x_m_to_y"]),
            "result_labels": {
                "strong_pass": float(result_labels.get("strong_pass", 0.05)),
                "weak_pass": float(result_labels.get("weak_pass", 0.10)),
            },
        },
    }


def get_screening_methods(module_config: Dict[str, Any]) -> Dict[str, Any]:
    raw = module_config.get("screening_methods", {})
    return {
        "default_method": raw.get("default_method", "three_step"),
        "three_step": raw.get("three_step", {"enabled": True, "required_paths": ["x_to_y", "x_to_m", "x_m_to_y"]}),
        "two_step": raw.get("two_step", {"enabled": True, "required_paths": ["x_to_m", "x_m_to_y"]}),
    }


def extract_mediation_chains(module_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    chains: List[Dict[str, Any]] = []
    screening_methods = get_screening_methods(module_config)
    default_method = screening_methods["default_method"]

    for path in module_config.get("mechanism_paths", []):
        if not bool(path.get("enabled", True)):
            continue
        label = str(path.get("id", path.get("label", "unknown_label"))).strip()
        description = str(path.get("description", "")).strip()
        x_var = str(path.get("x", "")).strip()
        y_var = str(path.get("y", "")).strip()
        mediators = safe_str_split(path.get("mediators"))
        directions = path.get("path_directions", {}) or {}
        screening_method = str(path.get("screening_method", default_method)).strip()
        pass_rule = str(path.get("pass_rule", "all_required_paths")).strip()

        for m_var in mediators:
            chain_id = f"{label}_{sanitize_name(x_var)}_{sanitize_name(m_var)}_{sanitize_name(y_var)}"
            chains.append({
                "label": label,
                "description": description,
                "relationship_type": "mediation",
                "chain_id": chain_id,
                "y_var": y_var,
                "x_var": x_var,
                "m_var": m_var,
                "path_directions": {
                    "x_to_y": str(directions.get("x_to_y", "any")).strip(),
                    "x_to_m": str(directions.get("x_to_m", "any")).strip(),
                    "m_to_y": str(directions.get("m_to_y", "any")).strip(),
                    "indirect_effect": str(directions.get("indirect_effect", "any")).strip(),
                },
                "screening_method": screening_method,
                "pass_rule": pass_rule,
            })
    return chains


# =========================
# 数据处理与公式
# =========================

def validate_columns_exist(df: pd.DataFrame, columns: List[str]) -> Tuple[bool, List[str]]:
    missing = [c for c in columns if c and c not in df.columns]
    return len(missing) == 0, missing






def build_fe_terms(config: Dict[str, Any]) -> List[str]:
    fe = get_fe_settings(config)
    terms = []

    if fe["entity_fixed_effect"]:
        if not fe["entity_name"]:
            raise ValueError("entity_fixed_effect = true，但 entity_name 未设置")
        terms.append(f"C({quote_var(fe['entity_name'])})")

    if fe["time_fixed_effect"]:
        if not fe["time_name"]:
            raise ValueError("time_fixed_effect = true，但 time_name 未设置")
        terms.append(f"C({quote_var(fe['time_name'])})")

    return terms


def build_formula(path_type: str, y_var: str, x_var: str, m_var: str, control_vars: List[str], fe_terms: List[str]) -> str:
    if path_type == "x_to_y":
        lhs = quote_var(y_var)
        rhs_terms = [quote_var(x_var)]
    elif path_type == "x_to_m":
        lhs = quote_var(m_var)
        rhs_terms = [quote_var(x_var)]
    elif path_type == "x_m_to_y":
        lhs = quote_var(y_var)
        rhs_terms = [quote_var(x_var), quote_var(m_var)]
    else:
        raise ValueError(f"未知 path_type: {path_type}")

    rhs_terms += [quote_var(c) for c in control_vars] + fe_terms
    rhs_terms = [x for x in rhs_terms if x]
    return lhs + " ~ " + " + ".join(rhs_terms)


def fit_model(df: pd.DataFrame, formula: str, cluster_col: Optional[str] = None, *,
              fixed_effect_terms: Optional[List[str]] = None,
              fixed_effect_columns: Optional[List[str]] = None) -> Any:
    if fixed_effect_columns:
        absorbed = fit_absorbed_ols(
            df, formula, fixed_effect_terms=fixed_effect_terms or [],
            fixed_effect_columns=fixed_effect_columns,
            cluster_column=cluster_col)
        if absorbed is not None:
            return absorbed
    model = smf.ols(formula=formula, data=df)
    if cluster_col:
        return model.fit(cov_type="cluster", cov_kwds={"groups": df[cluster_col]})
    return model.fit()


def safe_get_param(result: Any, raw_var_name: str) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    term = quote_var(raw_var_name)
    try:
        beta = float(result.params.get(term, np.nan))
        se = float(result.bse.get(term, np.nan))
        p = float(result.pvalues.get(term, np.nan))
        if np.isnan(beta):
            beta = None
        if np.isnan(se):
            se = None
        if np.isnan(p):
            p = None
        return beta, se, p
    except Exception:
        return None, None, None


def direction_matches(beta: Optional[float], expected_direction: str) -> bool:
    if beta is None:
        return False
    if expected_direction == "positive":
        return beta > 0
    if expected_direction == "negative":
        return beta < 0
    return True


def get_path_key_term(path_type: str, x_var: str, m_var: str) -> Tuple[str, str]:
    if path_type == "x_to_y":
        return x_var, "total_effect_x_to_y"
    if path_type == "x_to_m":
        return x_var, "first_stage_x_to_m"
    if path_type == "x_m_to_y":
        return m_var, "second_stage_m_to_y"
    raise ValueError(f"未知 path_type: {path_type}")


def get_path_expected_direction(path_type: str, path_directions: Dict[str, str]) -> str:
    if path_type == "x_to_y":
        return path_directions.get("x_to_y", "any")
    if path_type == "x_to_m":
        return path_directions.get("x_to_m", "any")
    if path_type == "x_m_to_y":
        return path_directions.get("m_to_y", "any")
    return "any"


def build_fixed_effects_label(config: Dict[str, Any]) -> str:
    fe = get_fe_settings(config)
    parts = []
    if fe["entity_fixed_effect"]:
        parts.append(f"entity:{fe['entity_name']}")
    if fe["time_fixed_effect"]:
        parts.append(f"time:{fe['time_name']}")
    return ";".join(parts) if parts else "none"


def build_cluster_label(config: Dict[str, Any]) -> str:
    fe = get_fe_settings(config)
    return f"cluster:{fe['cluster_level']}" if fe["cluster_enabled"] else "none"


# =========================
# 路径运行
# =========================

def run_single_path(
    df_model: pd.DataFrame,
    config: Dict[str, Any],
    label: str,
    description: str,
    chain_id: str,
    y_var: str,
    x_var: str,
    m_var: str,
    path_type: str,
    sample_threshold: int,
    treatment_name: str,
    required_controls: List[str],
    optional_controls: List[str],
    path_directions: Dict[str, str],
    x_to_y_coef_x: Optional[float] = None,
    processing_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    fe_settings = get_fe_settings(config)
    cluster_col = fe_settings["cluster_level"] if fe_settings["cluster_enabled"] else None
    fe_terms = build_fe_terms(config)

    formula = build_formula(
        path_type=path_type,
        y_var=y_var,
        x_var=x_var,
        m_var=m_var,
        control_vars=required_controls + optional_controls,
        fe_terms=fe_terms,
    )
    path_outcome = m_var if path_type == "x_to_m" else y_var
    focal_terms = [x_var] + ([m_var] if path_type == "x_m_to_y" else [])
    term_definition = model_terms_definition(
        outcome=path_outcome, focal_terms=focal_terms,
        controls=required_controls + optional_controls, fe_settings=fe_settings,
        cluster=cluster_col, formula=formula)

    model_id = (
        f"mediation_{label}_{path_type}_{sanitize_name(y_var)}_{sanitize_name(x_var)}_{sanitize_name(m_var)}_"
        f"s{sample_threshold}_{sanitize_name(treatment_name)}_ctrl_{sanitize_name('_'.join(optional_controls) if optional_controls else 'required_only')}"
    )
    control_id = f"ctrl_{sanitize_name('_'.join(optional_controls) if optional_controls else 'required_only')}"
    path_id = f"{chain_id}_{path_type}"
    key_term, effect_role = get_path_key_term(path_type, x_var, m_var)
    expected_direction = get_path_expected_direction(path_type, path_directions)

    vars_needed = [m_var if path_type == "x_to_m" else y_var, x_var]
    if path_type == "x_m_to_y":
        vars_needed.append(m_var)
    vars_needed += required_controls + optional_controls

    if fe_settings["entity_fixed_effect"]:
        vars_needed.append(fe_settings["entity_name"])
    if fe_settings["time_fixed_effect"]:
        vars_needed.append(fe_settings["time_name"])
    if cluster_col:
        vars_needed.append(cluster_col)

    vars_needed = [v for v in dict.fromkeys(vars_needed) if v]

    try:
        ok, missing_cols = validate_columns_exist(df_model, vars_needed)
        if not ok:
            raise KeyError(f"变量不存在: {missing_cols}")

        sub_df, sample_info = select_estimation_sample(df_model, vars_needed, processing_info)

        if cluster_col and sub_df[cluster_col].nunique() < 2:
            raise ValueError("聚类组数量不足，无法使用 cluster robust SE")

        min_n = fe_settings["minimum_sample_size"]
        if len(sub_df) < min_n:
            raise ValueError(f"有效样本量不足: {len(sub_df)} < {min_n}")

        fixed_effect_columns = [
            fe_settings[key] for enabled, key in (
                ("entity_fixed_effect", "entity_name"), ("time_fixed_effect", "time_name"))
            if fe_settings[enabled]
        ]
        result = fit_model(
            sub_df, formula=formula, cluster_col=cluster_col,
            fixed_effect_terms=fe_terms,
            fixed_effect_columns=fixed_effect_columns)

        beta_x, se_x, p_x = safe_get_param(result, x_var)
        beta_m, se_m, p_m = safe_get_param(result, m_var)
        beta_key, se_key, p_key = safe_get_param(result, key_term)
        direction_match = direction_matches(beta_key, expected_direction)

        x_weakened_after_m = None
        x_still_significant_after_m = None
        mediation_type_hint = None

        if path_type == "x_m_to_y" and beta_x is not None and x_to_y_coef_x is not None:
            x_weakened_after_m = abs(beta_x) < abs(x_to_y_coef_x)
            x_still_significant_after_m = (p_x is not None) and (p_x < 0.05)

            if (p_m is not None and p_m < 0.05) and (p_x is not None):
                if p_x >= 0.05:
                    mediation_type_hint = "full_mediation_like"
                elif x_weakened_after_m:
                    mediation_type_hint = "partial_mediation_like"
                else:
                    mediation_type_hint = "unclear"
            else:
                mediation_type_hint = "unclear"

        return {
            "model_id": model_id,
            "模块名称": "mediation_effect",
            "假设编号": label,
            "假设描述": description,
            "relationship_type": "mediation",
            "path_type": path_type,
            "path_id": path_id,
            "chain_id": chain_id,
            "被解释变量": y_var,
            "解释变量": x_var,
            "中介变量": m_var,
            "检验项": key_term,
            "effect_role": effect_role,
            "理论方向": expected_direction,
            "样本门槛": sample_threshold,
            "处理方案": treatment_name,
            "控制变量组合ID": control_id,
            "控制变量组合": "required_only" if not optional_controls else "required + " + " + ".join(optional_controls),
            "纳入的控制变量": " ".join(required_controls + optional_controls),
            "纳入的可选控制变量": " ".join(optional_controls),
            "控制变量数量": len(required_controls) + len(optional_controls),
            "optional_control_count": len(optional_controls),
            "固定效应设定": build_fixed_effects_label(config),
            "聚类标准误设定": build_cluster_label(config),
            "模型公式": formula,
            "模型项定义": term_definition,
            **regression_statistics(result, sub_df, path_outcome, fe_settings),
            "样本量（N）": int(result.nobs) if hasattr(result, "nobs") else None,
            **sample_info,
            "是否成功运行": "success",
            "失败原因": None,
            "beta_x": beta_x,
            "se_x": se_x,
            "p_x": p_x,
            "beta_m": beta_m if path_type == "x_m_to_y" else None,
            "se_m": se_m if path_type == "x_m_to_y" else None,
            "p_m": p_m if path_type == "x_m_to_y" else None,
            "beta": beta_key,
            "se": se_key,
            "p": p_key,
            "direction_match": direction_match,
            "strong_pass": False,
            "weak_pass": False,
            "x_weakened_after_m": x_weakened_after_m,
            "x_still_significant_after_m": x_still_significant_after_m,
            "mediation_type_hint": mediation_type_hint,
        }

    except Exception as e:
        return {
            "model_id": model_id,
            "模块名称": "mediation_effect",
            "假设编号": label,
            "假设描述": description,
            "relationship_type": "mediation",
            "path_type": path_type,
            "path_id": path_id,
            "chain_id": chain_id,
            "被解释变量": y_var,
            "解释变量": x_var,
            "中介变量": m_var,
            "检验项": key_term,
            "effect_role": effect_role,
            "理论方向": expected_direction,
            "样本门槛": sample_threshold,
            "处理方案": treatment_name,
            "控制变量组合ID": control_id,
            "控制变量组合": "required_only" if not optional_controls else "required + " + " + ".join(optional_controls),
            "纳入的控制变量": " ".join(required_controls + optional_controls),
            "纳入的可选控制变量": " ".join(optional_controls),
            "控制变量数量": len(required_controls) + len(optional_controls),
            "optional_control_count": len(optional_controls),
            "固定效应设定": build_fixed_effects_label(config),
            "聚类标准误设定": build_cluster_label(config),
            "模型公式": formula,
            "模型项定义": term_definition,
            "within R²": None,
            "样本量（N）": None,
            "是否成功运行": "failed",
            "失败原因": f"{type(e).__name__}: {e}",
            "beta_x": None,
            "se_x": None,
            "p_x": None,
            "beta_m": None,
            "se_m": None,
            "p_m": None,
            "beta": None,
            "se": None,
            "p": None,
            "direction_match": False,
            "strong_pass": False,
            "weak_pass": False,
            "x_weakened_after_m": None,
            "x_still_significant_after_m": None,
            "mediation_type_hint": None,
        }


# =========================
# 阶段A/B：16 个基准检验
# =========================

def build_stage_ab_tasks(
    chains: List[Dict[str, Any]],
    preprocessing_settings: Dict[str, Any],
) -> List[Dict[str, Any]]:
    tasks = []
    for chain in chains:
        for sample_threshold in preprocessing_settings["sample_thresholds"]:
            for treatment_name in preprocessing_settings["treatment_names"]:
                tasks.append({
                    **chain,
                    "sample_threshold": sample_threshold,
                    "treatment_name": treatment_name,
                })
    return tasks


def candidate_by_three_paths(
    rec_xy: Dict[str, Any],
    rec_xm: Dict[str, Any],
    rec_xmy: Dict[str, Any],
    min_sample_size: int,
    mediation_settings: Dict[str, Any],
    screening_method: str,
    pass_rule: str,
) -> bool:
    records = {
        "x_to_y": rec_xy,
        "x_to_m": rec_xm,
        "m_to_y": rec_xmy,
    }

    if pass_rule == "core_mediation_paths":
        required_flags = {"require_x_to_y": False, "require_x_to_m": True, "require_m_to_y": True}
    elif screening_method == "two_step":
        required_flags = mediation_settings["candidate_rules"]["two_step_requirements"]
    else:
        required_flags = mediation_settings["candidate_rules"]["three_step_requirements"]

    path_map = {
        "require_x_to_y": "x_to_y",
        "require_x_to_m": "x_to_m",
        "require_m_to_y": "m_to_y",
    }

    required_paths = [path_name for flag, path_name in path_map.items() if required_flags.get(flag, False)]

    if mediation_settings["candidate_rules"]["minimum_sample_size_required"]:
        for path_name in required_paths:
            if (records[path_name].get("样本量（N）") or 0) < min_sample_size:
                return False

    p_threshold = mediation_settings["candidate_rules"]["p_value_threshold"]
    require_direction = mediation_settings["candidate_rules"]["require_direction_match"]

    for path_name in required_paths:
        rec = records[path_name]
        if rec.get("是否成功运行") != "success":
            return False
        p = rec.get("p")
        if p is None or p >= p_threshold:
            return False
        if require_direction and not bool(rec.get("direction_match")):
            return False

    if mediation_settings["candidate_rules"]["x_weakened_after_m_required"]:
        if rec_xmy.get("x_weakened_after_m") is not True:
            return False

    return True


def build_task_level_model_data(
    df_raw: pd.DataFrame,
    y_var: str,
    x_var: str,
    m_var: str,
    required_controls: List[str],
    optional_controls: List[str],
    sample_threshold: int,
    treatment_name: str,
    continuous_settings: Dict[str, set],
    fe_settings: Dict[str, Any],
    config: Dict[str, Any],
    path_type: str,
    prepared_base: Optional[Tuple[pd.DataFrame, Dict[str, Any]]] = None,
    preprocessing_cache: Optional[PreparedDataCache] = None,
    preprocessing_candidate_vars: Optional[List[str]] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    core_vars = ([x_var, m_var] if path_type == "x_to_m" else
                 [x_var, y_var] if path_type == "x_to_y" else [x_var, m_var, y_var])
    process_candidate_vars = [y_var, x_var, m_var] + required_controls + optional_controls
    if prepared_base is None:
        candidate_vars = preprocessing_candidate_vars or process_candidate_vars
        if preprocessing_cache is None:
            df_proc, treatment_info = prepare_data(
                df_raw, config, core_vars=core_vars,
                threshold=sample_threshold, treatment=treatment_name,
                candidate_vars=candidate_vars,
            )
        else:
            df_proc, treatment_info = preprocessing_cache.get_or_prepare(
                df_raw, config, core_vars=core_vars,
                threshold=sample_threshold, treatment=treatment_name,
                candidate_vars=candidate_vars)
    else:
        cached_frame, cached_info = prepared_base
        df_proc, treatment_info = cached_frame.copy(deep=True), dict(cached_info)
    return df_proc, treatment_info


def run_preprocessed_path(df_raw: pd.DataFrame, config: Dict[str, Any], *,
                          label: str, description: str, chain_id: str,
                          y_var: str, x_var: str, m_var: str, path_type: str,
                          sample_threshold: int, treatment_name: str,
                          required_controls: List[str], optional_controls: List[str],
                          path_directions: Dict[str, str],
                          x_to_y_coef_x: Optional[float] = None,
                          prepared_base: Optional[Tuple[pd.DataFrame, Dict[str, Any]]] = None,
                          preprocessing_cache: Optional[PreparedDataCache] = None,
                          preprocessing_candidate_vars: Optional[List[str]] = None) -> Dict[str, Any]:
    """Prepare each mediation path independently while using the shared rules."""
    settings = get_fe_settings(config)
    details = dict(config=config, label=label, description=description, chain_id=chain_id,
                   y_var=y_var, x_var=x_var, m_var=m_var, path_type=path_type,
                   sample_threshold=sample_threshold, treatment_name=treatment_name,
                   required_controls=required_controls, optional_controls=optional_controls,
                   path_directions=path_directions, x_to_y_coef_x=x_to_y_coef_x)
    try:
        frame, info = build_task_level_model_data(
            df_raw=df_raw, y_var=y_var, x_var=x_var, m_var=m_var,
            required_controls=required_controls, optional_controls=optional_controls,
            sample_threshold=sample_threshold, treatment_name=treatment_name,
            continuous_settings=parse_continuous_settings(config), fe_settings=settings,
            config=config, path_type=path_type, prepared_base=prepared_base,
            preprocessing_cache=preprocessing_cache,
            preprocessing_candidate_vars=preprocessing_candidate_vars)
    except Exception as exc:
        record = run_single_path(df_model=df_raw.iloc[0:0], **details)
        record["失败原因"] = f"{type(exc).__name__}: {exc}"
        return record
    record = run_single_path(df_model=frame, processing_info=info, **details)
    record.update(info)
    return record


def run_screening_stage(
    config: Dict[str, Any],
    df_raw: pd.DataFrame,
    chains: List[Dict[str, Any]],
    continuous_settings: Dict[str, set],
    preprocessing_settings: Dict[str, Any],
    controls_cfg: Dict[str, Any],
    fe_settings: Dict[str, Any],
    mediation_settings: Dict[str, Any],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    full_records: List[Dict[str, Any]] = []
    candidate_records: List[Dict[str, Any]] = []

    required_controls = controls_cfg["required_controls"]
    tasks = build_stage_ab_tasks(chains, preprocessing_settings)

    for task in stage_progress(tasks, total=len(tasks), module="中介效应", stage="阶段 A/B 路径筛查",
                               hypothesis=lambda task: task["label"]):
        label = task["label"]
        description = task["description"]
        chain_id = task["chain_id"]
        y_var = task["y_var"]
        x_var = task["x_var"]
        m_var = task["m_var"]
        path_directions = task["path_directions"]
        screening_method = task["screening_method"]
        pass_rule = task["pass_rule"]
        sample_threshold = task["sample_threshold"]
        treatment_name = task["treatment_name"]

        try:
            shared = dict(df_raw=df_raw, config=config, label=label, description=description,
                          chain_id=chain_id, y_var=y_var, x_var=x_var, m_var=m_var,
                          sample_threshold=sample_threshold, treatment_name=treatment_name,
                          required_controls=required_controls, optional_controls=[],
                          path_directions=path_directions)
            rec_xy = run_preprocessed_path(path_type="x_to_y", **shared)
            rec_xm = run_preprocessed_path(path_type="x_to_m", **shared)
            rec_xmy = run_preprocessed_path(path_type="x_m_to_y",
                                            x_to_y_coef_x=rec_xy.get("beta_x"), **shared)
            treatment_info = {"是否插值": treatment_name in {"interpolate_controls", "interpolate_winsorize_controls"},
                              "是否缩尾": treatment_name in {"winsorize_controls", "interpolate_winsorize_controls"}}

            for rec in (rec_xy, rec_xm, rec_xmy):
                rec["检验阶段"] = "AB"
                full_records.append(rec)

            is_candidate = candidate_by_three_paths(
                rec_xy=rec_xy,
                rec_xm=rec_xm,
                rec_xmy=rec_xmy,
                min_sample_size=fe_settings["minimum_sample_size"],
                mediation_settings=mediation_settings,
                screening_method=screening_method,
                pass_rule=pass_rule,
            )

            candidate_records.append({
                "model_id": (
                    f"mediation_chain_{label}_{sanitize_name(y_var)}_{sanitize_name(x_var)}_{sanitize_name(m_var)}_"
                    f"s{sample_threshold}_{sanitize_name(treatment_name)}"
                ),
                "模块名称": "mediation_effect",
                "假设编号": label,
                "假设描述": description,
                "relationship_type": "mediation",
                "chain_id": chain_id,
                "被解释变量": y_var,
                "解释变量": x_var,
                "中介变量": m_var,
                "screening_method": screening_method,
                "pass_rule": pass_rule,
                "样本门槛": sample_threshold,
                "处理方案": treatment_name,
                "是否插值": treatment_info["是否插值"],
                "是否缩尾": treatment_info["是否缩尾"],
                "固定效应设定": build_fixed_effects_label(config),
                "聚类标准误设定": build_cluster_label(config),
                "x_to_y_p_x": rec_xy.get("p_x"),
                "x_to_m_p_x": rec_xm.get("p_x"),
                "x_m_to_y_p_m": rec_xmy.get("p_m"),
                "x_to_y_direction_match": rec_xy.get("direction_match"),
                "x_to_m_direction_match": rec_xm.get("direction_match"),
                "m_to_y_direction_match": rec_xmy.get("direction_match"),
                "x_to_y_n": rec_xy.get("样本量（N）"),
                "x_to_m_n": rec_xm.get("样本量（N）"),
                "x_m_to_y_n": rec_xmy.get("样本量（N）"),
                "x_weakened_after_m": rec_xmy.get("x_weakened_after_m"),
                "x_still_significant_after_m": rec_xmy.get("x_still_significant_after_m"),
                "mediation_type_hint": rec_xmy.get("mediation_type_hint"),
                "是否候选模型": is_candidate,
                "是否成功运行": "success" if all(r.get("是否成功运行") == "success" for r in [rec_xy, rec_xm, rec_xmy]) else "failed",
                "失败原因": None if all(r.get("是否成功运行") == "success" for r in [rec_xy, rec_xm, rec_xmy]) else "三条路径中至少一条失败",
            })

        except Exception as e:
            fail_prefix = (
                f"mediation_chain_{label}_{sanitize_name(y_var)}_{sanitize_name(x_var)}_{sanitize_name(m_var)}_"
                f"s{sample_threshold}_{sanitize_name(treatment_name)}"
            )
            for path_type in ["x_to_y", "x_to_m", "x_m_to_y"]:
                key_term, effect_role = get_path_key_term(path_type, x_var, m_var)
                expected_direction = get_path_expected_direction(path_type, path_directions)
                full_records.append({
                    "model_id": f"{fail_prefix}_{path_type}",
                    "模块名称": "mediation_effect",
                    "假设编号": label,
                    "假设描述": description,
                    "relationship_type": "mediation",
                    "path_type": path_type,
                    "path_id": f"{chain_id}_{path_type}",
                    "chain_id": chain_id,
                    "被解释变量": y_var,
                    "解释变量": x_var,
                    "中介变量": m_var,
                    "检验项": key_term,
                    "effect_role": effect_role,
                    "理论方向": expected_direction,
                    "样本门槛": sample_threshold,
                    "处理方案": treatment_name,
                    "控制变量组合ID": "ctrl_required_only",
                    "控制变量组合": "required_only",
                    "纳入的控制变量": " ".join(required_controls),
                    "纳入的可选控制变量": "",
                    "控制变量数量": len(required_controls),
                    "optional_control_count": 0,
                    "固定效应设定": build_fixed_effects_label(config),
                    "聚类标准误设定": build_cluster_label(config),
                    "within R²": None,
                    "样本量（N）": None,
                    "是否成功运行": "failed",
                    "失败原因": f"{type(e).__name__}: {e}",
                    "beta_x": None,
                    "se_x": None,
                    "p_x": None,
                    "beta_m": None,
                    "se_m": None,
                    "p_m": None,
                    "beta": None,
                    "se": None,
                    "p": None,
                    "direction_match": False,
                    "strong_pass": False,
                    "weak_pass": False,
                    "x_weakened_after_m": None,
                    "x_still_significant_after_m": None,
                    "mediation_type_hint": None,
                    "是否插值": None,
                    "是否缩尾": None,
                })

            candidate_records.append({
                "model_id": fail_prefix,
                "模块名称": "mediation_effect",
                "假设编号": label,
                "假设描述": description,
                "relationship_type": "mediation",
                "chain_id": chain_id,
                "被解释变量": y_var,
                "解释变量": x_var,
                "中介变量": m_var,
                "screening_method": screening_method,
                "pass_rule": pass_rule,
                "样本门槛": sample_threshold,
                "处理方案": treatment_name,
                "是否插值": None,
                "是否缩尾": None,
                "固定效应设定": build_fixed_effects_label(config),
                "聚类标准误设定": build_cluster_label(config),
                "x_to_y_p_x": None,
                "x_to_m_p_x": None,
                "x_m_to_y_p_m": None,
                "x_to_y_direction_match": False,
                "x_to_m_direction_match": False,
                "m_to_y_direction_match": False,
                "x_to_y_n": None,
                "x_to_m_n": None,
                "x_m_to_y_n": None,
                "x_weakened_after_m": None,
                "x_still_significant_after_m": None,
                "mediation_type_hint": None,
                "是否候选模型": False,
                "是否成功运行": "failed",
                "失败原因": f"{type(e).__name__}: {e}",
            })

    for rec in full_records:
        rec.setdefault("检验阶段", "AB")
    return pd.DataFrame(full_records), pd.DataFrame(candidate_records)


# =========================
# 阶段C：组合筛查
# =========================

def generate_full_enumeration_combos(optional_controls: List[str], min_k: int, max_k: int) -> List[List[str]]:
    combos = []
    for r in range(min_k, max_k + 1):
        for combo in itertools.combinations(optional_controls, r):
            combos.append(list(combo))
    return combos


def generate_sampling_enumeration_combos(
    optional_controls: List[str],
    min_k: int,
    max_k: int,
    random_seed: int,
    random_seeds_per_size: int,
    force_keep_full_model: bool,
) -> List[List[str]]:
    rng = random.Random(random_seed)
    all_combos: List[List[str]] = []

    for r in range(min_k, max_k + 1):
        all_r_combos = list(itertools.combinations(optional_controls, r))

        if len(all_r_combos) <= random_seeds_per_size:
            sampled = [list(x) for x in all_r_combos]
        else:
            sampled_idx = rng.sample(range(len(all_r_combos)), random_seeds_per_size)
            sampled = [list(all_r_combos[i]) for i in sampled_idx]

        all_combos.extend(sampled)

    if force_keep_full_model and optional_controls:
        full_model = sorted(optional_controls)
        if full_model not in all_combos and min_k <= len(full_model) <= max_k:
            all_combos.append(full_model)

    dedup = []
    seen = set()
    for combo in all_combos:
        key = tuple(sorted(combo))
        if key not in seen:
            seen.add(key)
            dedup.append(sorted(combo))
    return dedup


def retain_results_by_rules(
    records: List[Dict[str, Any]],
    p_field: str,
    keep_all_strong_pass: bool,
    keep_all_weak_pass: bool,
    keep_per_size: Optional[int],
    keep_global_extra: Optional[int],
    prefer_diversity: bool,
) -> List[Dict[str, Any]]:
    if not records:
        return []

    strong_records = [r for r in records if r.get("strong_pass") is True]
    weak_records = [r for r in records if r.get("strong_pass") is not True and r.get("weak_pass") is True]
    other_records = [r for r in records if r.get("strong_pass") is not True and r.get("weak_pass") is not True]

    kept: List[Dict[str, Any]] = []

    if keep_all_strong_pass:
        kept.extend(strong_records)
    if keep_all_weak_pass:
        kept.extend(weak_records)

    bucket = other_records if (keep_all_strong_pass and keep_all_weak_pass) else (strong_records + weak_records + other_records)

    if keep_per_size is not None:
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for r in bucket:
            size = int(r.get("optional_control_count", 0))
            grouped.setdefault(size, []).append(r)

        for _, rows in grouped.items():
            rows = sorted(rows, key=lambda rr: sort_key_for_retention(rr, p_field))
            kept.extend(rows[:keep_per_size])
    else:
        kept.extend(bucket)

    if keep_global_extra is not None:
        remainder = [r for r in records if r not in kept]
        remainder = sorted(remainder, key=lambda rr: sort_key_for_retention(rr, p_field))
        kept.extend(remainder[:keep_global_extra])

    if prefer_diversity:
        final = []
        seen_controls = set()
        for r in kept:
            control_signature = tuple(sorted(safe_str_split(r.get("纳入的可选控制变量"))))
            if control_signature not in seen_controls:
                seen_controls.add(control_signature)
                final.append(r)
        kept = final

    dedup = []
    seen_ids = set()
    for r in kept:
        mid = r.get("model_id")
        if mid not in seen_ids:
            seen_ids.add(mid)
            dedup.append(r)

    return dedup


def sort_key_for_retention(record: Dict[str, Any], p_field: str) -> Tuple[int, int, float, int]:
    strong = 1 if record.get("strong_pass") else 0
    weak = 1 if record.get("weak_pass") else 0
    p = record.get(p_field)
    p_val = 999 if p is None else float(p)
    n_optional = int(record.get("optional_control_count", 0))
    return (-strong, -weak, p_val, n_optional)


def run_control_combination_stage(
    config: Dict[str, Any],
    df_raw: pd.DataFrame,
    chains: List[Dict[str, Any]],
    candidate_df: pd.DataFrame,
    continuous_settings: Dict[str, set],
    controls_cfg: Dict[str, Any],
    control_search_settings: Dict[str, Any],
    fe_settings: Dict[str, Any],
    mediation_settings: Dict[str, Any],
) -> pd.DataFrame:
    combination_rules = controls_cfg["combination_rules"]
    combination_execution = controls_cfg["combination_execution"]
    mediation_combination_screening = mediation_settings["combination_screening"]
    required_controls = controls_cfg["required_controls"]
    optional_controls = controls_cfg["optional_controls"]

    if not combination_rules["enabled"]:
        print("阶段C已关闭：combination_rules.enabled = false")
        return pd.DataFrame()

    if not mediation_combination_screening["enabled"]:
        print("阶段C已关闭：mediation_settings.combination_screening.enabled = false")
        return pd.DataFrame()

    if not control_search_settings["enabled"]:
        print("阶段C已关闭：control_search_settings.enabled = false")
        return pd.DataFrame()

    if candidate_df.empty:
        print("阶段C跳过：没有 candidate 模型。")
        return pd.DataFrame()

    min_k = combination_rules["min_optional_controls_in_combination"]
    max_k = combination_rules["max_optional_controls_in_combination"]
    if max_k is None:
        max_k = len(optional_controls)
    max_k = min(max_k, len(optional_controls))

    if min_k > len(optional_controls) and optional_controls:
        print("阶段C跳过：min_optional_controls_in_combination 大于 optional_controls 数量。")
        return pd.DataFrame()

    mode = combination_execution["mode"]
    max_candidates_to_expand = min(control_search_settings["max_candidates_to_expand"], len(candidate_df))
    candidate_df = candidate_df.head(max_candidates_to_expand).copy()

    apply_to_paths = mediation_combination_screening["apply_to_paths"]

    all_records: List[Dict[str, Any]] = []
    chains_by_id = {c["chain_id"]: c for c in chains}

    outer_bar = stage_progress(
        candidate_df.iterrows(),
        total=len(candidate_df),
        module="中介效应", stage="阶段 C 控制组合",
        hypothesis=lambda item: item[1].get("假设编号"),
    )

    for idx, (_, cand_row) in enumerate(outer_bar, start=1):
        label = cand_row.get("假设编号")
        description = cand_row.get("假设描述")
        y_var = cand_row.get("被解释变量")
        x_var = cand_row.get("解释变量")
        m_var = cand_row.get("中介变量")
        chain_id = cand_row.get("chain_id")
        chain = chains_by_id.get(chain_id, {
            "chain_id": chain_id,
            "path_directions": {"x_to_y": "any", "x_to_m": "any", "m_to_y": "any", "indirect_effect": "any"},
        })
        path_directions = chain.get("path_directions", {})
        sample_threshold = int(cand_row.get("样本门槛"))
        treatment_name = cand_row.get("处理方案")

        if not optional_controls:
            optional_combos = [[]]
        elif mode == "full_enumeration":
            optional_combos = generate_full_enumeration_combos(optional_controls, min_k, max_k)
        elif mode == "sampling_enumeration":
            optional_combos = generate_sampling_enumeration_combos(
                optional_controls=optional_controls,
                min_k=min_k,
                max_k=max_k,
                random_seed=control_search_settings["random_seed"] + idx,
                random_seeds_per_size=control_search_settings["layer_sampling"]["random_seeds_per_size"],
                force_keep_full_model=control_search_settings["retention_rules"]["force_keep_full_model"],
            )
        else:
            raise ValueError(f"不支持的 combination_execution.mode: {mode}")

        required_flags = mediation_settings["candidate_rules"][
            "two_step_requirements" if chain.get("screening_method") == "two_step"
            else "three_step_requirements"]
        if chain.get("pass_rule") == "core_mediation_paths":
            required_flags = {"require_x_to_y": False, "require_x_to_m": True, "require_m_to_y": True}
        required_paths = [name for flag, name in (("require_x_to_y", "x_to_y"),
                          ("require_x_to_m", "x_to_m"), ("require_m_to_y", "x_m_to_y"))
                          if required_flags.get(flag, False)]
        paths_to_run = set(apply_to_paths) | set(required_paths)
        if mediation_settings["candidate_rules"]["x_weakened_after_m_required"]:
            paths_to_run.update({"x_to_y", "x_m_to_y"})
        ordered_paths = [name for name in ("x_to_y", "x_to_m", "x_m_to_y") if name in paths_to_run]

        # A/B and C use path-specific raw-core eligibility. Within this C
        # candidate, prepare each required path once for the union of possible
        # controls, then let every model combination apply its own complete-case
        # selection in run_single_path.
        candidate_vars = [y_var, x_var, m_var] + required_controls + optional_controls
        path_caches = {path_type: PreparedDataCache() for path_type in ordered_paths}

        for combo in optional_combos:
            by_path: Dict[str, Dict[str, Any]] = {}
            for path_type in ordered_paths:
                rec = run_preprocessed_path(
                    df_raw, config, label=label, description=description, chain_id=chain_id,
                    y_var=y_var, x_var=x_var, m_var=m_var, path_type=path_type,
                    sample_threshold=sample_threshold, treatment_name=treatment_name,
                    required_controls=required_controls, optional_controls=combo,
                    path_directions=path_directions,
                    x_to_y_coef_x=by_path.get("x_to_y", {}).get("beta_x"),
                    preprocessing_cache=path_caches[path_type],
                    preprocessing_candidate_vars=candidate_vars)
                rec["检验阶段"] = "C"
                p_value = rec.get("p")
                direction_ok = (not mediation_settings["candidate_rules"]["require_direction_match"]
                                or bool(rec.get("direction_match")))
                valid = rec.get("是否成功运行") == "success" and p_value is not None and np.isfinite(p_value)
                rec["strong_pass"] = bool(valid and p_value < mediation_combination_screening["result_labels"]["strong_pass"] and direction_ok)
                rec["weak_pass"] = bool(valid and p_value < mediation_combination_screening["result_labels"]["weak_pass"] and direction_ok)
                by_path[path_type] = rec
                all_records.append(rec)

    return pd.DataFrame(all_records)


# =========================
# 保存与汇总
# =========================

def make_specification_id(row: pd.Series) -> str:
    parts = [
        f"s{row.get('样本门槛')}",
        sanitize_name(row.get("处理方案", "")),
        f"interp_{row.get('是否插值')}",
        f"winsor_{row.get('是否缩尾')}",
        sanitize_name(row.get("控制变量组合ID", "ctrl_required_only")),
        sanitize_name(row.get("固定效应设定", "none")),
        sanitize_name(row.get("聚类标准误设定", "none")),
    ]
    return "__".join(parts)


def build_standardized_results(
    result_df: pd.DataFrame,
    global_config: Dict[str, Any],
    module_config: Dict[str, Any],
) -> pd.DataFrame:
    if result_df.empty:
        return pd.DataFrame()

    rows: List[Dict[str, Any]] = []
    global_config_path = global_config.get("_config_path", "")
    module_config_path = module_config.get("_config_path", "")

    for _, row in result_df.iterrows():
        if row.get("是否成功运行") not in ("success", "failed"):
            continue
        path_type = row.get("path_type")
        if path_type not in {"x_to_y", "x_to_m", "x_m_to_y"} and row.get("是否成功运行") != "failed":
            continue

        rows.append(preserve_module_diagnostics({
            "model_id": row.get("model_id"),
            "specification_id": make_specification_id(row),
            "模块名称": row.get("模块名称", "mediation_effect"),
            "假设编号": row.get("假设编号"),
            "假设描述": row.get("假设描述"),
            "relationship_type": row.get("relationship_type", "mediation"),
            "effect_type": "mediation",
            "path_type": path_type,
            "path_id": row.get("path_id"),
            "chain_id": row.get("chain_id"),
            "被解释变量": row.get("被解释变量"),
            "解释变量": row.get("解释变量"),
            "中介变量": row.get("中介变量"),
            "调节变量": None,
            "检验项": row.get("检验项"),
            "effect_role": row.get("effect_role"),
            "理论方向": row.get("理论方向"),
            "direction_match": row.get("direction_match"),
            "beta": row.get("beta"),
            "se": row.get("se"),
            "p": row.get("p"),
            "样本区间": global_config.get("data", {}).get("sample_info", {}).get("sample_period"),
            "样本规则": "、".join(global_config.get("data", {}).get("sample_info", {}).get("sample_rules", []) or ["全样本"]),
            "样本门槛": row.get("样本门槛"),
            "处理方案": row.get("处理方案"),
            "是否插值": row.get("是否插值"),
            "是否缩尾": row.get("是否缩尾"),
            "控制变量组合ID": row.get("控制变量组合ID"),
            "控制变量组合": row.get("控制变量组合"),
            "纳入的控制变量": row.get("纳入的控制变量"),
            "纳入的可选控制变量": row.get("纳入的可选控制变量"),
            "控制变量数量": row.get("控制变量数量"),
            "optional_control_count": row.get("optional_control_count"),
            "固定效应设定": row.get("固定效应设定"),
            "聚类标准误设定": row.get("聚类标准误设定"),
            "模型公式": row.get("模型公式"),
            "模型项定义": row.get("模型项定义"),
            "普通 R²": row.get("普通 R²"),
            "调整 R²": row.get("调整 R²"),
            "within R²": row.get("within R²"),
            "组内 R²定义": row.get("组内 R²定义"),
            "组内 R²吸收项": row.get("组内 R²吸收项"),
            "样本量（N）": row.get("样本量（N）"),
            "sample_identity": row.get("sample_identity"),
            "estimation_sample_n": row.get("estimation_sample_n"),
            "complete_case_removed_n": row.get("complete_case_removed_n"),
            "processing_definition_id": row.get("processing_definition_id"),
            "processing_definition_json": row.get("processing_definition_json"),
            "processing_trace_json": row.get("processing_trace_json"),
            "processing_input_n": row.get("processing_input_n"),
            "processing_eligible_n": row.get("processing_eligible_n"),
            "是否候选模型": row.get("是否候选模型"),
            "strong_pass": row.get("strong_pass"),
            "weak_pass": row.get("weak_pass"),
            "是否成功运行": row.get("是否成功运行"),
            "失败原因": row.get("失败原因"),
            "global_config_path": global_config_path,
            "module_config_path": module_config_path,
        }, row))

    return align_standard_columns(pd.DataFrame(rows))


def _execute_mediation_effect(config: Dict[str, Any], module_config: Dict[str, Any],
                              output_dir_override: Path | None = None) -> None:
    preflight(config, module_config)

    if not bool(config.get("run_settings", {}).get("enabled_modules", {}).get("mediation_effect", True)):
        print("全局 YAML 中 run_settings.enabled_modules.mediation_effect = false，中介效应模块跳过。")
        return

    if not bool(module_config.get("enabled", True)):
        print("中介 YAML 中 enabled = false，中介效应模块跳过。")
        return

    df_raw = load_data(config["data"]["input_file"], config)
    preflight(config, module_config, df_raw.columns)

    continuous_settings = parse_continuous_settings(config)
    preprocessing_settings = get_preprocessing_settings(config)
    controls_cfg = get_controls(config)
    control_search_settings = get_control_search_settings(config)
    fe_settings = get_fe_settings(config)
    mediation_settings = get_mediation_settings(module_config)
    chains = extract_mediation_chains(module_config)

    if not chains:
        print("未发现 enabled = true 的中介机制路径，模块结束。")
        return

    final_output_dir = get_output_root(config) / get_sample_folder_name(config) / "mediation_effect"
    output_dir = output_dir_override or final_output_dir
    if output_dir_override is None:
        check_output_overwrite(output_dir, config)
    output_dir = ensure_dir(output_dir)

    print("=" * 60)
    print("开始运行中介效应模块")
    print(f"全局配置文件: {Path(config['_config_path']).resolve()}")
    print(f"中介配置文件: {Path(module_config.get('_config_path')).resolve()}")
    print(f"输出目录: {final_output_dir.resolve()}")
    print("=" * 60)

    full_df, candidate_df = run_screening_stage(
        config=config,
        df_raw=df_raw,
        chains=chains,
        continuous_settings=continuous_settings,
        preprocessing_settings=preprocessing_settings,
        controls_cfg=controls_cfg,
        fe_settings=fe_settings,
        mediation_settings=mediation_settings,
    )

    control_combination_df = run_control_combination_stage(
        config=config,
        df_raw=df_raw,
        chains=chains,
        candidate_df=candidate_df.loc[candidate_df["是否候选模型"] == True].copy() if not candidate_df.empty else pd.DataFrame(),
        continuous_settings=continuous_settings,
        controls_cfg=controls_cfg,
        control_search_settings=control_search_settings,
        fe_settings=fe_settings,
        mediation_settings=mediation_settings,
    )

    frames = []
    if not full_df.empty:
        frames.append(full_df)
    if not control_combination_df.empty:
        frames.append(control_combination_df)

    merged_full_df = annotate(pd.concat(frames, axis=0, ignore_index=True) if frames else pd.DataFrame(), config)
    policy = output_policy(config, module_config)
    hypothesis_df = mediation_hypotheses(merged_full_df, chains, mediation_settings,
                                         fe_settings["minimum_sample_size"])

    ab_decisions = hypothesis_df.loc[hypothesis_df["检验阶段"] == "AB"]
    candidate_results_df = ab_decisions.loc[ab_decisions["是否候选模型"] == True].copy()
    c_decisions = hypothesis_df.loc[hypothesis_df["检验阶段"] == "C"]
    strong_df = c_decisions.loc[c_decisions["strong_pass"] == True].copy()
    weak_df = c_decisions.loc[c_decisions["weak_pass"] == True].copy()

    if policy.raw:
        for stem, frame in (("full_results", merged_full_df), ("path_results", full_df),
                            ("control_combination_results", control_combination_df)):
            display_frame = frame if stem == "full_results" else annotate(frame, config)
            if stem in {"full_results", "control_combination_results"}:
                display_frame = align_human_result_columns(display_frame)
            write_result_table(display_frame, output_dir, stem, csv=policy.human_csv,
                               excel=policy.human_excel)
    if policy.success:
        for stem, frame in (("chain_results", hypothesis_df),
                            ("candidate_results", candidate_results_df),
                            ("strong_pass_results", strong_df),
                            ("weak_pass_results", weak_df)):
            write_result_table(align_human_result_columns(frame) if stem != "chain_results" else frame,
                               output_dir, stem, csv=policy.human_csv,
                               excel=policy.human_excel)

    standardized_df = align_standard_columns(apply_standardized_options(annotate(
        build_standardized_results(merged_full_df, config, module_config), config), config))
    save_processing_definitions(merged_full_df, output_dir)
    if policy.machine:
        write_result_table(hypothesis_df, output_dir, "hypothesis_results",
                           csv=True, excel=policy.human_excel)
        write_result_table(standardized_df, output_dir, "standardized_results",
                           csv=True, excel=policy.human_excel, machine=True)
        if policy.specification_index:
            write_result_table(build_specification_index(standardized_df), output_dir,
                               "specification_index", csv=True, excel=policy.human_excel)

    full_success_n = int((merged_full_df["是否成功运行"] == "success").sum()) if not merged_full_df.empty and "是否成功运行" in merged_full_df.columns else 0
    full_fail_n = int((merged_full_df["是否成功运行"] == "failed").sum()) if not merged_full_df.empty and "是否成功运行" in merged_full_df.columns else 0
    candidate_n = len(candidate_df) if not candidate_df.empty else 0
    combo_n = len(control_combination_df) if not control_combination_df.empty else 0
    strong_n = len(strong_df) if not strong_df.empty else 0
    weak_n = len(weak_df) if not weak_df.empty else 0

    candidate_threshold = mediation_settings["candidate_rules"]["p_value_threshold"]
    strong_threshold = mediation_settings["combination_screening"]["result_labels"]["strong_pass"]
    weak_threshold = mediation_settings["combination_screening"]["result_labels"]["weak_pass"]

    summary_lines = [
        "# Mediation Effect Summary",
        "",
        "## 阶段A/B：路径筛查",
        f"- 基础路径结果数：{len(full_df) if not full_df.empty else 0}",
        f"- 中介链结果数：{len(candidate_df) if not candidate_df.empty else 0}",
        f"- candidate 中介链数：{int((candidate_df['是否候选模型'] == True).sum()) if not candidate_df.empty and '是否候选模型' in candidate_df.columns else 0}",
        f"- candidate 判定阈值：p < {candidate_threshold}",
        f"- candidate 是否要求方向一致：{mediation_settings['candidate_rules']['require_direction_match']}",
        "",
        "## 阶段C：控制变量组合筛查",
        f"- 组合筛查结果数：{combo_n}",
        f"- strong_pass 结果数（p < {strong_threshold}）：{strong_n}",
        f"- weak_pass 结果数（p < {weak_threshold}）：{weak_n}",
        "",
        "## 全部结果汇总",
        f"- 全部结果总数：{len(merged_full_df) if not merged_full_df.empty else 0}",
        f"- 标准化长表结果数：{len(standardized_df) if not standardized_df.empty else 0}",
        f"- 成功运行数：{full_success_n}",
        f"- 失败数：{full_fail_n}",
    ]

    if policy.summary:
        with open(output_dir / "summary.md", "w", encoding="utf-8") as f:
            f.write("\n".join(summary_lines))

    print("=" * 60)
    print("中介效应计算完成，正在校验并发布结果。")
    print("=" * 60)


def run_mediation_effect(config_path: str | Path = "analysis_config.yaml") -> None:
    config = load_yaml(config_path)
    module_config = load_module_yaml(config, "mediation_effect")
    if not (config.get("run_settings", {}).get("enabled_modules", {}).get("mediation_effect", True)
            and module_config.get("enabled", True)):
        _execute_mediation_effect(config, module_config)
        return
    run_effect("mediation_effect", config, module_config, SCRIPT_PATH,
               lambda stage: _execute_mediation_effect(config, module_config, stage))
    print(f"中介效应模块运行完成；结果已输出到: {get_output_root(config) / get_sample_folder_name(config) / 'mediation_effect'}")


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else default_config_path()
    run_mediation_effect(config_path)
