#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
run_main_effect.py

主效应两阶段版本（对齐 oneclick 思想）

阶段A/B：
- 仅使用 required_controls 做基础筛查
- 识别 candidate 模型

阶段C：
- 仅对 candidate 模型执行 optional_controls 组合筛查
- 不做最优搜索
- 不 early stop
- 不默认 top-k 截断
- 输出已测试组合集合，并做 strong / weak / direction 标记

运行：
python3 skill/main_effect/run_main_effect.py config/analysis_config.yaml

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
from _internal.hypothesis_output import single_model_hypotheses
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


def get_output_root(config: Dict[str, Any]) -> Path:
    return get_project_output_root(config)


def get_preprocessing_settings(config: Dict[str, Any]) -> Dict[str, Any]:
    preprocessing = config.get("preprocessing", {})
    sample_thresholds = preprocessing.get("sample_thresholds", [2, 3, 4, 5])
    sample_thresholds = [int(x) for x in sample_thresholds]

    return {
        "sample_thresholds": sample_thresholds,
        "treatment_names": treatment_names(config),
        "interpolation": preprocessing.get("interpolation", {}),
        "winsorize": preprocessing.get("winsorize", {}),
    }


def get_main_effect_settings(module_config: Dict[str, Any]) -> Dict[str, Any]:
    settings = module_config.get("main_effect_settings", {})
    candidate_rules = settings.get("candidate_rules", {})
    combination_screening = settings.get("combination_screening", {})
    result_labels = combination_screening.get("result_labels", {})

    return {
        "candidate_rules": {
            "p_value_threshold": float(candidate_rules.get("p_value_threshold", 0.05)),
            "require_direction_match": bool(candidate_rules.get("require_direction_match", True)),
            "minimum_sample_size_required": bool(candidate_rules.get("minimum_sample_size_required", True)),
        },
        "combination_screening": {
            "enabled": bool(combination_screening.get("enabled", True)),
            "run_only_for_candidates": bool(combination_screening.get("run_only_for_candidates", True)),
            "keep_all_strong_pass": bool(combination_screening.get("keep_all_strong_pass", True)),
            "keep_all_weak_pass": bool(combination_screening.get("keep_all_weak_pass", True)),
            "result_labels": {
                "strong_pass": float(result_labels.get("strong_pass", 0.05)),
                "weak_pass": float(result_labels.get("weak_pass", 0.10)),
            },
        },
    }


def get_nonlinear_settings(module_config: Dict[str, Any]) -> Dict[str, Any]:
    raw = module_config.get("nonlinear_settings", {})
    turning = raw.get("turning_point_check", {})
    return {
        "create_squared_terms": bool(raw.get("create_squared_terms", True)),
        "squared_term_suffix": str(raw.get("squared_term_suffix", "_sq")),
        "center_before_square": bool(raw.get("center_before_square", True)),
        "turning_point_check": {
            "enabled": bool(turning.get("enabled", False)),
            "require_turning_point_inside_sample": bool(turning.get("require_turning_point_inside_sample", False)),
        },
    }


def extract_main_effect_hypotheses(module_config: Dict[str, Any]) -> List[Dict[str, Any]]:
    hypotheses = module_config.get("hypotheses", [])
    out = []
    for hyp in hypotheses:
        if not bool(hyp.get("enabled", True)):
            continue
        normalized = dict(hyp)
        normalized["label"] = str(hyp.get("id", hyp.get("label", "unknown_label"))).strip()
        normalized["relationship_type"] = "main_effect"
        normalized["model_form"] = str(hyp.get("model_form", "linear")).strip()
        normalized["pass_rule"] = str(hyp.get("pass_rule", "all_required_terms")).strip()
        out.append(normalized)
    return out


def get_independent_variable_groups(module_config: Dict[str, Any]) -> Dict[str, List[str]]:
    groups = module_config.get("variables", {}).get("independent_variables", {})
    parsed = {}
    for group_name, vars_in_group in groups.items():
        if isinstance(vars_in_group, list):
            parsed[group_name] = [str(v).strip() for v in vars_in_group if str(v).strip()]
        else:
            parsed[group_name] = safe_str_split(vars_in_group)
    return parsed


def get_dependent_variable_groups(module_config: Dict[str, Any]) -> Dict[str, List[str]]:
    groups = module_config.get("variables", {}).get("dependent_variables", {})
    parsed = {}
    for group_name, vars_in_group in groups.items():
        if isinstance(vars_in_group, list):
            parsed[group_name] = [str(v).strip() for v in vars_in_group if str(v).strip()]
        else:
            parsed[group_name] = safe_str_split(vars_in_group)
    return parsed


def expand_x_combinations_for_hypothesis(
    hypothesis: Dict[str, Any],
    x_group_map: Dict[str, List[str]],
) -> List[List[str]]:
    if hypothesis.get("x"):
        return [[str(item.get("name", item)).strip() for item in hypothesis.get("x", []) if str(item.get("name", item)).strip()]]

    x_groups = hypothesis.get("x_groups", [])
    if not isinstance(x_groups, list):
        x_groups = [x_groups]

    group_lists = []
    for group_name in x_groups:
        group_name = str(group_name).strip()
        if group_name not in x_group_map:
            raise KeyError(f"x_groups 中引用的组不存在: {group_name}")
        vars_in_group = [v for v in x_group_map[group_name] if str(v).strip()]
        if not vars_in_group:
            raise ValueError(f"x_groups 中的组为空: {group_name}")
        group_lists.append(vars_in_group)

    combos = list(itertools.product(*group_lists))
    return [list(combo) for combo in combos]


def get_x_specs(hypothesis: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw_x = hypothesis.get("x", [])
    if not raw_x:
        return [{"name": x, "expected_direction": "any", "required": True} for x in safe_str_split(hypothesis.get("x_vars"))]

    specs = []
    for item in raw_x:
        if isinstance(item, dict):
            name = str(item.get("name", "")).strip()
            if not name:
                continue
            specs.append({
                "name": name,
                "expected_direction": str(item.get("expected_direction", item.get("expected_shape", "any"))).strip(),
                "expected_shape": str(item.get("expected_shape", "")).strip(),
                "required": bool(item.get("required", True)),
            })
        else:
            name = str(item).strip()
            if name:
                specs.append({"name": name, "expected_direction": "any", "expected_shape": "", "required": True})
    return specs


def get_base_x_vars(hypothesis: Dict[str, Any]) -> List[str]:
    return [x["name"] for x in get_x_specs(hypothesis)]


def squared_term_name(base_var: str, nonlinear_settings: Dict[str, Any]) -> str:
    return f"{base_var}{nonlinear_settings['squared_term_suffix']}"


def get_model_terms(hypothesis: Dict[str, Any], nonlinear_settings: Dict[str, Any]) -> List[str]:
    base_vars = get_base_x_vars(hypothesis)
    model_form = str(hypothesis.get("model_form", "linear")).strip()
    if model_form in {"u_shape", "inverted_u_shape"}:
        terms = []
        for x in base_vars:
            terms.append(x)
            terms.append(squared_term_name(x, nonlinear_settings))
        return terms
    return base_vars


def add_nonlinear_terms(
    df: pd.DataFrame,
    hypothesis: Dict[str, Any],
    nonlinear_settings: Dict[str, Any],
) -> pd.DataFrame:
    model_form = str(hypothesis.get("model_form", "linear")).strip()
    if model_form not in {"u_shape", "inverted_u_shape"}:
        return df
    if not nonlinear_settings["create_squared_terms"]:
        return df

    out = df.copy()
    for x in get_base_x_vars(hypothesis):
        sq = squared_term_name(x, nonlinear_settings)
        if x not in out.columns:
            continue
        base = out[x]
        if nonlinear_settings["center_before_square"]:
            base = base - base.mean(skipna=True)
        out[sq] = base ** 2
    return out


def expected_direction_for_term(
    hypothesis: Dict[str, Any],
    term: str,
    nonlinear_settings: Dict[str, Any],
) -> str:
    model_form = str(hypothesis.get("model_form", "linear")).strip()
    spec_by_name = {x["name"]: x for x in get_x_specs(hypothesis)}

    for base_name, spec in spec_by_name.items():
        sq_name = squared_term_name(base_name, nonlinear_settings)
        if term == sq_name:
            if model_form == "u_shape":
                return "positive"
            if model_form == "inverted_u_shape":
                return "negative"
            shape = spec.get("expected_shape", "")
            if shape == "u_shape":
                return "positive"
            if shape == "inverted_u":
                return "negative"
            return "any"
        if term == base_name:
            return spec.get("expected_direction") or "any"
    return "any"


def is_required_term(
    hypothesis: Dict[str, Any],
    term: str,
    nonlinear_settings: Dict[str, Any],
) -> bool:
    model_form = str(hypothesis.get("model_form", "linear")).strip()
    for spec in get_x_specs(hypothesis):
        base_name = spec["name"]
        sq_name = squared_term_name(base_name, nonlinear_settings)
        if term == base_name:
            return bool(spec.get("required", True)) and model_form == "linear"
        if term == sq_name:
            return bool(spec.get("required", True)) and model_form in {"u_shape", "inverted_u_shape"}
    return False


def direction_matches(beta: Optional[float], expected_direction: str) -> bool:
    if beta is None:
        return False
    if expected_direction == "positive":
        return beta > 0
    if expected_direction == "negative":
        return beta < 0
    return True


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


def build_formula(y_var: str, x_vars: List[str], control_vars: List[str], fe_terms: List[str]) -> str:
    rhs_terms = [quote_var(x) for x in x_vars] + [quote_var(c) for c in control_vars] + fe_terms
    rhs_terms = [x for x in rhs_terms if x]
    return f"{quote_var(y_var)} ~ " + " + ".join(rhs_terms)


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
    if cluster_col:
        model = smf.ols(formula=formula, data=df)
        result = model.fit(cov_type="cluster", cov_kwds={"groups": df[cluster_col]})
    else:
        model = smf.ols(formula=formula, data=df)
        result = model.fit()
    return result


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


# =========================
# Candidate 规则
# =========================

def candidate_by_core_vars(
    hypothesis: Dict[str, Any],
    model_terms: List[str],
    record: Dict[str, Any],
    min_sample_size: int,
    nonlinear_settings: Dict[str, Any],
    p_threshold: float = 0.05,
    require_direction_match: bool = True,
    minimum_sample_size_required: bool = True,
) -> bool:
    if record.get("是否成功运行") != "success":
        return False

    n = record.get("样本量（N）")
    if minimum_sample_size_required:
        if n is None or n < min_sample_size:
            return False

    pass_rule = str(hypothesis.get("pass_rule", "all_required_terms")).strip()
    required_terms = [term for term in model_terms if is_required_term(hypothesis, term, nonlinear_settings)]
    if not required_terms:
        required_terms = model_terms

    term_pass = {}
    for term in required_terms:
        p = record.get(f"p_{term}")
        if p is None or p >= p_threshold:
            term_pass[term] = False
            continue
        if require_direction_match:
            direction_ok = record.get(f"direction_match_{term}")
            term_pass[term] = bool(direction_ok)
        else:
            term_pass[term] = True

    if pass_rule == "any_required_term":
        return any(term_pass.values())
    return all(term_pass.values())


# =========================
# 阶段A/B：基础筛查
# =========================

def build_stage_ab_tasks(
    hypotheses: List[Dict[str, Any]],
    x_group_map: Dict[str, List[str]],
    preprocessing_settings: Dict[str, Any],
    nonlinear_settings: Dict[str, Any],
) -> List[Dict[str, Any]]:
    tasks = []
    for hyp in hypotheses:
        label = str(hyp.get("label", "unknown_label"))
        description = str(hyp.get("description", ""))
        y_var = str(hyp.get("y", "")).strip()

        x_var_combos = expand_x_combinations_for_hypothesis(hyp, x_group_map)
        for x_vars in x_var_combos:
            task_hyp = dict(hyp)
            if not task_hyp.get("x"):
                task_hyp["x"] = [{"name": x, "expected_direction": "any", "required": True} for x in x_vars]
            model_terms = get_model_terms(task_hyp, nonlinear_settings)
            for sample_threshold in preprocessing_settings["sample_thresholds"]:
                for treatment_name in preprocessing_settings["treatment_names"]:
                    tasks.append({
                        "label": label,
                        "description": description,
                        "hypothesis": task_hyp,
                        "y_var": y_var,
                        "x_vars": x_vars,
                        "model_terms": model_terms,
                        "model_form": task_hyp.get("model_form", "linear"),
                        "pass_rule": task_hyp.get("pass_rule", "all_required_terms"),
                        "sample_threshold": sample_threshold,
                        "treatment_name": treatment_name,
                    })
    return tasks


def run_screening_stage(
    config: Dict[str, Any],
    df_raw: pd.DataFrame,
    hypotheses: List[Dict[str, Any]],
    x_group_map: Dict[str, List[str]],
    continuous_settings: Dict[str, set],
    preprocessing_settings: Dict[str, Any],
    controls_cfg: Dict[str, Any],
    fe_settings: Dict[str, Any],
    main_effect_settings: Dict[str, Any],
    nonlinear_settings: Dict[str, Any],
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    full_records: List[Dict[str, Any]] = []

    entity_col = fe_settings["entity_name"] if fe_settings["entity_fixed_effect"] else None
    year_col = fe_settings["time_name"] if fe_settings["time_fixed_effect"] else None
    cluster_col = fe_settings["cluster_level"] if fe_settings["cluster_enabled"] else None

    required_controls = controls_cfg["required_controls"]
    fe_terms = build_fe_terms(config)
    tasks = build_stage_ab_tasks(hypotheses, x_group_map, preprocessing_settings, nonlinear_settings)
    preprocessing_cache = PreparedDataCache()

    for task in stage_progress(tasks, total=len(tasks), module="主效应", stage="阶段 A/B 基础筛查",
                               hypothesis=lambda task: task["label"]):
        label = task["label"]
        description = task["description"]
        hypothesis = task["hypothesis"]
        y_var = task["y_var"]
        x_vars = task["x_vars"]
        model_terms = task["model_terms"]
        model_form = task["model_form"]
        pass_rule = task["pass_rule"]
        sample_threshold = task["sample_threshold"]
        treatment_name = task["treatment_name"]

        x_combo_label = "__".join([sanitize_name(x) for x in x_vars])
        model_id = None
        treatment_info: Dict[str, Any] = {}

        try:
            control_vars = required_controls.copy()
            all_needed_vars = [y_var] + x_vars + control_vars
            if entity_col:
                all_needed_vars.append(entity_col)
            if year_col:
                all_needed_vars.append(year_col)
            if cluster_col:
                all_needed_vars.append(cluster_col)

            ok, missing_cols = validate_columns_exist(df_raw, all_needed_vars)
            if not ok:
                raise KeyError(f"变量不存在: {missing_cols}")

            process_candidate_vars = [y_var] + x_vars + control_vars
            df_proc, treatment_info = preprocessing_cache.get_or_prepare(
                df_raw, config, core_vars=[y_var] + x_vars,
                threshold=sample_threshold, treatment=treatment_name,
                candidate_vars=process_candidate_vars)
            # Nonlinear terms are model-specific; never add them to the cached base.
            df_proc = df_proc.copy(deep=True)
            df_proc = add_nonlinear_terms(df_proc, hypothesis, nonlinear_settings)

            formula = build_formula(y_var=y_var, x_vars=model_terms, control_vars=control_vars, fe_terms=fe_terms)

            formula_vars = [y_var] + model_terms + control_vars
            if entity_col:
                formula_vars.append(entity_col)
            if year_col:
                formula_vars.append(year_col)
            if cluster_col:
                formula_vars.append(cluster_col)

            df_model, sample_info = select_estimation_sample(df_proc, formula_vars, treatment_info)
            treatment_info.update(sample_info)

            if cluster_col:
                if cluster_col not in df_model.columns:
                    raise KeyError(f"聚类变量不存在: {cluster_col}")
                if df_model[cluster_col].nunique() < 2:
                    raise ValueError("聚类组数量不足，无法使用 cluster robust SE")

            result = fit_model(
                df_model, formula=formula, cluster_col=cluster_col,
                fixed_effect_terms=fe_terms,
                fixed_effect_columns=[v for v in (entity_col, year_col) if v])
            squared_suffix = nonlinear_settings.get("squared_term_suffix", "_sq")
            generated_terms = {term: f"{term[:-len(squared_suffix)]} ** 2"
                               for term in model_terms if squared_suffix and term.endswith(squared_suffix)}

            model_id = (
                f"main_effect_{label}_{sanitize_name(y_var)}_{x_combo_label}_"
                f"s{sample_threshold}_{sanitize_name(treatment_name)}_ctrl_required_only"
            )

            record = {
                "model_id": model_id,
                "模块名称": "main_effect",
                "假设编号": label,
                "假设描述": description,
                "relationship_type": "main_effect",
                "被解释变量": y_var,
                "核心解释变量组合": " ".join(x_vars),
                "检验项组合": " ".join(model_terms),
                "模型形式": model_form,
                "模型公式": formula,
                "模型项定义": model_terms_definition(
                    outcome=y_var, focal_terms=model_terms, controls=control_vars,
                    fe_settings=fe_settings, cluster=cluster_col,
                    generated_terms=generated_terms, formula=formula),
                "通过规则": pass_rule,
                "样本门槛": sample_threshold,
                "处理方案": treatment_name,
                "控制变量组合ID": "ctrl_required_only",
                "控制变量组合": "required_only",
                "纳入的控制变量": " ".join(control_vars),
                "纳入的可选控制变量": "",
                "控制变量数量": len(control_vars),
                "固定效应设定": build_fixed_effects_label(config),
                "聚类标准误设定": build_cluster_label(config),
                "是否插值": treatment_info["是否插值"],
                "是否缩尾": treatment_info["是否缩尾"],
                **{key: treatment_info[key] for key in ("processing_definition_id", "processing_definition_json",
                    "processing_trace_json", "processing_input_n", "processing_eligible_n",
                    "sample_identity", "estimation_sample_n", "complete_case_removed_n")},
                **regression_statistics(result, df_model, y_var, fe_settings),
                "样本量（N）": int(result.nobs) if hasattr(result, "nobs") else None,
                "是否成功运行": "success",
                "失败原因": None,
            }

            for term in model_terms:
                beta, se, p = safe_get_param(result, term)
                expected_direction = expected_direction_for_term(hypothesis, term, nonlinear_settings)
                record[f"beta_{term}"] = beta
                record[f"se_{term}"] = se
                record[f"p_{term}"] = p
                record[f"理论方向_{term}"] = expected_direction
                record[f"direction_match_{term}"] = direction_matches(beta, expected_direction)

            record["是否候选模型"] = candidate_by_core_vars(
                hypothesis=hypothesis,
                model_terms=model_terms,
                record=record,
                min_sample_size=fe_settings["minimum_sample_size"],
                nonlinear_settings=nonlinear_settings,
                p_threshold=main_effect_settings["candidate_rules"]["p_value_threshold"],
                require_direction_match=main_effect_settings["candidate_rules"]["require_direction_match"],
                minimum_sample_size_required=main_effect_settings["candidate_rules"]["minimum_sample_size_required"],
            )

            full_records.append(record)

        except Exception as e:
            if model_id is None:
                model_id = (
                    f"main_effect_{label}_{sanitize_name(y_var or 'unknown_y')}_{x_combo_label}_"
                    f"s{sample_threshold}_{sanitize_name(treatment_name)}_ctrl_required_only"
                )

            fail_record = {
                "model_id": model_id,
                "模块名称": "main_effect",
                "假设编号": label,
                "假设描述": description,
                "relationship_type": "main_effect",
                "被解释变量": y_var,
                "核心解释变量组合": " ".join(x_vars),
                "检验项组合": " ".join(model_terms),
                "模型形式": model_form,
                "模型公式": build_formula(y_var, model_terms, required_controls, fe_terms) if y_var and model_terms else None,
                "模型项定义": model_terms_definition(
                    outcome=y_var, focal_terms=model_terms, controls=required_controls,
                    fe_settings=fe_settings, cluster=cluster_col),
                "通过规则": pass_rule,
                "样本门槛": sample_threshold,
                "处理方案": treatment_name,
                "控制变量组合ID": "ctrl_required_only",
                "控制变量组合": "required_only",
                "纳入的控制变量": " ".join(required_controls),
                "纳入的可选控制变量": "",
                "控制变量数量": len(required_controls),
                "固定效应设定": build_fixed_effects_label(config),
                "聚类标准误设定": build_cluster_label(config),
                "是否插值": None,
                "是否缩尾": None,
                "within R²": None,
                "样本量（N）": None,
                "是否成功运行": "failed",
                "失败原因": f"{type(e).__name__}: {e}",
                "是否候选模型": False,
            }
            fail_record.update(treatment_info)
            for term in model_terms:
                expected_direction = expected_direction_for_term(hypothesis, term, nonlinear_settings)
                fail_record[f"beta_{term}"] = None
                fail_record[f"se_{term}"] = None
                fail_record[f"p_{term}"] = None
                fail_record[f"理论方向_{term}"] = expected_direction
                fail_record[f"direction_match_{term}"] = False

            full_records.append(fail_record)

    full_df = pd.DataFrame(full_records)
    candidate_df = full_df.loc[full_df["是否候选模型"] == True].copy() if not full_df.empty else pd.DataFrame()
    return full_df, candidate_df


# =========================
# 阶段C：组合生成
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


def pass_by_record_terms(
    hypothesis: Dict[str, Any],
    model_terms: List[str],
    record: Dict[str, Any],
    nonlinear_settings: Dict[str, Any],
    p_threshold: float,
    require_direction_match: bool = True,
) -> bool:
    return candidate_by_core_vars(
        hypothesis=hypothesis,
        model_terms=model_terms,
        record={**record, "是否成功运行": "success", "样本量（N）": record.get("样本量（N）", 1)},
        min_sample_size=0,
        nonlinear_settings=nonlinear_settings,
        p_threshold=p_threshold,
        require_direction_match=require_direction_match,
        minimum_sample_size_required=False,
    )


def direction_match_by_record_terms(
    hypothesis: Dict[str, Any],
    model_terms: List[str],
    record: Dict[str, Any],
    nonlinear_settings: Dict[str, Any],
) -> bool:
    required_terms = [term for term in model_terms if is_required_term(hypothesis, term, nonlinear_settings)]
    if not required_terms:
        required_terms = model_terms
    return all(bool(record.get(f"direction_match_{term}")) for term in required_terms)


def evaluate_control_combo(
    df_proc: pd.DataFrame,
    y_var: str,
    model_terms: List[str],
    required_controls: List[str],
    optional_vars_included: List[str],
    fe_terms: List[str],
    cluster_col: Optional[str],
    fe_columns: List[str],
    fe_settings: Dict[str, Any],
    treatment_info: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    control_vars = required_controls + optional_vars_included
    formula = build_formula(y_var=y_var, x_vars=model_terms, control_vars=control_vars, fe_terms=fe_terms)

    formula_vars = [y_var] + model_terms + control_vars + fe_columns
    if cluster_col:
        formula_vars.append(cluster_col)

    try:
        df_model, sample_info = select_estimation_sample(df_proc, formula_vars, treatment_info)
    except (KeyError, ValueError) as exc:
        return None, str(exc)

    if cluster_col and df_model[cluster_col].nunique() < 2:
        return None, "聚类组数量不足，无法使用 cluster robust SE"

    try:
        result = fit_model(
            df_model, formula=formula, cluster_col=cluster_col,
            fixed_effect_terms=fe_terms, fixed_effect_columns=fe_columns)
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"

    return {
        "result": result,
        "nobs": int(result.nobs) if hasattr(result, "nobs") else None,
        "statistics": regression_statistics(result, df_model, y_var, fe_settings),
        "formula": formula,
        "sample_info": sample_info,
    }, None


def build_model_data_for_candidate(
    df_raw: pd.DataFrame,
    y_var: str,
    x_vars: List[str],
    hypothesis: Dict[str, Any],
    sample_threshold: int,
    treatment_name: str,
    continuous_settings: Dict[str, set],
    nonlinear_settings: Dict[str, Any],
    entity_col: Optional[str],
    year_col: Optional[str],
    cluster_col: Optional[str],
    process_vars: List[str],
    config: Dict[str, Any],
    prepared_base: Optional[Tuple[pd.DataFrame, Dict[str, Any]]] = None,
) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    if prepared_base is None:
        df_proc, treatment_info = prepare_data(
            df_raw, config, core_vars=[y_var] + x_vars,
            threshold=sample_threshold, treatment=treatment_name,
            candidate_vars=process_vars,
        )
    else:
        cached_frame, cached_info = prepared_base
        df_proc, treatment_info = cached_frame.copy(deep=True), dict(cached_info)
    df_proc = add_nonlinear_terms(df_proc, hypothesis, nonlinear_settings)
    return df_proc, treatment_info


def sort_key_for_retention(record: Dict[str, Any], x_vars: List[str]) -> Tuple[int, int, float, int]:
    strong = 1 if record.get("strong_pass") else 0
    weak = 1 if record.get("weak_pass") else 0

    p_sum = 0.0
    for x in x_vars:
        p = record.get(f"p_{x}")
        p_sum += 999 if p is None else p

    n_optional = int(record.get("optional_control_count", 0))
    return (-strong, -weak, p_sum, n_optional)


def retain_results_by_rules(
    records: List[Dict[str, Any]],
    x_vars: List[str],
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

    if not keep_all_strong_pass or not keep_all_weak_pass:
        bucket = strong_records + weak_records + other_records
    else:
        bucket = other_records

    if keep_per_size is not None:
        grouped: Dict[int, List[Dict[str, Any]]] = {}
        for r in bucket:
            size = int(r.get("optional_control_count", 0))
            grouped.setdefault(size, []).append(r)

        for _, rows in grouped.items():
            rows = sorted(rows, key=lambda rr: sort_key_for_retention(rr, x_vars))
            kept.extend(rows[:keep_per_size])
    else:
        kept.extend(bucket)

    if keep_global_extra is not None:
        remainder = [r for r in records if r not in kept]
        remainder = sorted(remainder, key=lambda rr: sort_key_for_retention(rr, x_vars))
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


def run_control_combination_stage(
    config: Dict[str, Any],
    df_raw: pd.DataFrame,
    hypotheses: List[Dict[str, Any]],
    candidate_df: pd.DataFrame,
    continuous_settings: Dict[str, set],
    controls_cfg: Dict[str, Any],
    control_search_settings: Dict[str, Any],
    fe_settings: Dict[str, Any],
    main_effect_settings: Dict[str, Any],
    nonlinear_settings: Dict[str, Any],
) -> pd.DataFrame:
    combination_rules = controls_cfg["combination_rules"]
    combination_execution = controls_cfg["combination_execution"]
    main_combination_screening = main_effect_settings["combination_screening"]
    required_controls = controls_cfg["required_controls"]
    optional_controls = controls_cfg["optional_controls"]

    if not combination_rules["enabled"]:
        print("阶段C已关闭：combination_rules.enabled = false")
        return pd.DataFrame()

    if not main_combination_screening["enabled"]:
        print("阶段C已关闭：main_effect_settings.combination_screening.enabled = false")
        return pd.DataFrame()

    if not control_search_settings["enabled"]:
        print("阶段C已关闭：control_search_settings.enabled = false")
        return pd.DataFrame()

    if candidate_df.empty:
        print("阶段C跳过：没有 candidate 模型。")
        return pd.DataFrame()

    if not optional_controls:
        print("阶段C跳过：optional_controls 为空。")
        return pd.DataFrame()

    entity_col = fe_settings["entity_name"] if fe_settings["entity_fixed_effect"] else None
    year_col = fe_settings["time_name"] if fe_settings["time_fixed_effect"] else None
    cluster_col = fe_settings["cluster_level"] if fe_settings["cluster_enabled"] else None
    fe_terms = build_fe_terms(config)

    min_k = combination_rules["min_optional_controls_in_combination"]
    max_k = combination_rules["max_optional_controls_in_combination"]
    if max_k is None:
        max_k = len(optional_controls)
    max_k = min(max_k, len(optional_controls))

    if min_k > len(optional_controls):
        print("阶段C跳过：min_optional_controls_in_combination 大于 optional_controls 数量。")
        return pd.DataFrame()

    mode = combination_execution["mode"]
    max_candidates_to_expand = min(control_search_settings["max_candidates_to_expand"], len(candidate_df))
    candidate_df = candidate_df.head(max_candidates_to_expand).copy()

    all_records: List[Dict[str, Any]] = []
    preprocessing_cache = PreparedDataCache()
    hypotheses_by_label = {str(h.get("label")): h for h in hypotheses}

    outer_bar = stage_progress(
        candidate_df.iterrows(),
        total=len(candidate_df),
        module="主效应", stage="阶段 C 控制组合",
        hypothesis=lambda item: item[1].get("假设编号"),
    )

    for idx, cand_row in outer_bar:
        label = cand_row.get("假设编号")
        description = cand_row.get("假设描述")
        y_var = cand_row.get("被解释变量")
        x_vars = safe_str_split(cand_row.get("核心解释变量组合"))
        model_terms = safe_str_split(cand_row.get("检验项组合")) or x_vars
        hypothesis = hypotheses_by_label.get(str(label), {
            "label": label,
            "x": [{"name": x, "expected_direction": "any", "required": True} for x in x_vars],
            "model_form": cand_row.get("模型形式", "linear"),
            "pass_rule": cand_row.get("通过规则", "all_required_terms"),
        })
        sample_threshold = int(cand_row.get("样本门槛"))
        treatment_name = cand_row.get("处理方案")

        if not x_vars:
            continue

        x_combo_label = "__".join([sanitize_name(x) for x in x_vars])

        process_vars = [y_var] + x_vars + required_controls + optional_controls
        if entity_col:
            process_vars.append(entity_col)
        if year_col:
            process_vars.append(year_col)
        if cluster_col:
            process_vars.append(cluster_col)

        try:
            prepared_base = preprocessing_cache.get_or_prepare(
                df_raw, config, core_vars=[y_var] + x_vars,
                threshold=sample_threshold, treatment=treatment_name,
                candidate_vars=process_vars)
            df_proc, treatment_info = build_model_data_for_candidate(
                df_raw=df_raw,
                y_var=y_var,
                x_vars=x_vars,
                hypothesis=hypothesis,
                sample_threshold=sample_threshold,
                treatment_name=treatment_name,
                continuous_settings=continuous_settings,
                nonlinear_settings=nonlinear_settings,
                entity_col=entity_col,
                year_col=year_col,
                cluster_col=cluster_col,
                process_vars=process_vars,
                config=config,
                prepared_base=prepared_base,
            )
        except Exception as e:
            all_records.append({
                "model_id": f"main_effect_{label}_{sanitize_name(y_var)}_{x_combo_label}_"
                            f"s{sample_threshold}_{sanitize_name(treatment_name)}_stageC_build_failed",
                "模块名称": "main_effect",
                "假设编号": label,
                "假设描述": description,
                "relationship_type": "main_effect",
                "被解释变量": y_var,
                "核心解释变量组合": " ".join(x_vars),
                "检验项组合": " ".join(model_terms),
                "模型形式": hypothesis.get("model_form", "linear"),
                "模型公式": build_formula(y_var, model_terms, required_controls, fe_terms) if y_var and model_terms else None,
                "模型项定义": model_terms_definition(
                    outcome=y_var, focal_terms=model_terms, controls=required_controls,
                    fe_settings=fe_settings, cluster=cluster_col),
                "通过规则": hypothesis.get("pass_rule", "all_required_terms"),
                "样本门槛": sample_threshold,
                "处理方案": treatment_name,
                "控制变量组合ID": "stageC_build_failed",
                "控制变量组合": "stageC_build_failed",
                "纳入的控制变量": "",
                "纳入的可选控制变量": "",
                "控制变量数量": None,
                "optional_control_count": None,
                "固定效应设定": build_fixed_effects_label(config),
                "聚类标准误设定": build_cluster_label(config),
                "是否插值": None,
                "是否缩尾": None,
                "within R²": None,
                "样本量（N）": None,
                "是否成功运行": "failed",
                "失败原因": f"{type(e).__name__}: {e}",
                "strong_pass": False,
                "weak_pass": False,
                "direction_match": False,
            })
            continue

        if mode == "full_enumeration":
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

        candidate_records_for_this_model: List[Dict[str, Any]] = []

        for combo in stage_progress(
            optional_combos, total=len(optional_combos), module="主效应",
            stage=f"阶段 C 当前候选 {idx + 1}/{len(candidate_df)} 控制组合",
            hypothesis=lambda _: str(label), position=1, leave=False,
        ):
            eval_result, err = evaluate_control_combo(
                df_proc=df_proc,
                y_var=y_var,
                model_terms=model_terms,
                required_controls=required_controls,
                optional_vars_included=combo,
                fe_terms=fe_terms,
                cluster_col=cluster_col,
                fe_columns=[v for v in (entity_col, year_col) if v],
                fe_settings=fe_settings,
                treatment_info=treatment_info,
            )

            model_id = (
                f"main_effect_{label}_{sanitize_name(y_var)}_{x_combo_label}_"
                f"s{sample_threshold}_{sanitize_name(treatment_name)}_"
                f"ctrl_{sanitize_name('_'.join(combo))}"
            )

            if err is None and eval_result is not None:
                record = {
                    "model_id": model_id,
                    "模块名称": "main_effect",
                    "假设编号": label,
                    "假设描述": description,
                    "relationship_type": "main_effect",
                    "被解释变量": y_var,
                    "核心解释变量组合": " ".join(x_vars),
                    "检验项组合": " ".join(model_terms),
                    "模型形式": hypothesis.get("model_form", "linear"),
                    "通过规则": hypothesis.get("pass_rule", "all_required_terms"),
                    "样本门槛": sample_threshold,
                    "处理方案": treatment_name,
                    "控制变量组合ID": f"ctrl_{sanitize_name('_'.join(combo))}",
                    "控制变量组合": "required + " + " + ".join(combo),
                    "纳入的控制变量": " ".join(required_controls + combo),
                    "纳入的可选控制变量": " ".join(combo),
                    "控制变量数量": len(required_controls) + len(combo),
                    "optional_control_count": len(combo),
                    "固定效应设定": build_fixed_effects_label(config),
                    "聚类标准误设定": build_cluster_label(config),
                    "是否插值": treatment_info["是否插值"],
                    "是否缩尾": treatment_info["是否缩尾"],
                    "模型公式": eval_result["formula"],
                    "模型项定义": model_terms_definition(
                        outcome=y_var, focal_terms=model_terms,
                        controls=required_controls + combo, fe_settings=fe_settings,
                        cluster=cluster_col, formula=eval_result["formula"],
                        generated_terms={term: f"{term[:-len(suffix)]} ** 2"
                            for term in model_terms
                            for suffix in [nonlinear_settings.get("squared_term_suffix", "_sq")]
                            if suffix and term.endswith(suffix)}),
                    **{key: treatment_info[key] for key in ("processing_definition_id", "processing_definition_json",
                        "processing_input_n", "processing_eligible_n")},
                    **eval_result["sample_info"],
                    **eval_result["statistics"],
                    "样本量（N）": eval_result["nobs"],
                    "是否成功运行": "success",
                    "失败原因": None,
                }

                for term in model_terms:
                    beta, se, p = safe_get_param(eval_result["result"], term)
                    expected_direction = expected_direction_for_term(hypothesis, term, nonlinear_settings)
                    record[f"beta_{term}"] = beta
                    record[f"se_{term}"] = se
                    record[f"p_{term}"] = p
                    record[f"理论方向_{term}"] = expected_direction
                    record[f"direction_match_{term}"] = direction_matches(beta, expected_direction)

                record["direction_match"] = direction_match_by_record_terms(
                    hypothesis, model_terms, record, nonlinear_settings
                )
                record["strong_pass"] = pass_by_record_terms(
                    hypothesis=hypothesis,
                    model_terms=model_terms,
                    record=record,
                    nonlinear_settings=nonlinear_settings,
                    p_threshold=main_combination_screening["result_labels"]["strong_pass"],
                    require_direction_match=main_effect_settings["candidate_rules"]["require_direction_match"],
                )
                record["weak_pass"] = pass_by_record_terms(
                    hypothesis=hypothesis,
                    model_terms=model_terms,
                    record=record,
                    nonlinear_settings=nonlinear_settings,
                    p_threshold=main_combination_screening["result_labels"]["weak_pass"],
                    require_direction_match=main_effect_settings["candidate_rules"]["require_direction_match"],
                )

                candidate_records_for_this_model.append(record)

            else:
                fail_record = {
                    "model_id": model_id,
                    "模块名称": "main_effect",
                    "假设编号": label,
                    "假设描述": description,
                    "relationship_type": "main_effect",
                    "被解释变量": y_var,
                    "核心解释变量组合": " ".join(x_vars),
                    "检验项组合": " ".join(model_terms),
                    "模型形式": hypothesis.get("model_form", "linear"),
                    "模型公式": build_formula(y_var, model_terms, required_controls + combo, fe_terms) if y_var and model_terms else None,
                    "模型项定义": model_terms_definition(
                        outcome=y_var, focal_terms=model_terms,
                        controls=required_controls + combo, fe_settings=fe_settings,
                        cluster=cluster_col),
                    "通过规则": hypothesis.get("pass_rule", "all_required_terms"),
                    "样本门槛": sample_threshold,
                    "处理方案": treatment_name,
                    "控制变量组合ID": f"ctrl_{sanitize_name('_'.join(combo))}",
                    "控制变量组合": "required + " + " + ".join(combo),
                    "纳入的控制变量": " ".join(required_controls + combo),
                    "纳入的可选控制变量": " ".join(combo),
                    "控制变量数量": len(required_controls) + len(combo),
                    "optional_control_count": len(combo),
                    "固定效应设定": build_fixed_effects_label(config),
                    "聚类标准误设定": build_cluster_label(config),
                    "是否插值": None,
                    "是否缩尾": None,
                    "within R²": None,
                    "样本量（N）": None,
                    "是否成功运行": "failed",
                    "失败原因": err,
                    "strong_pass": False,
                    "weak_pass": False,
                    "direction_match": False,
                }
                fail_record.update(treatment_info)
                for term in model_terms:
                    expected_direction = expected_direction_for_term(hypothesis, term, nonlinear_settings)
                    fail_record[f"beta_{term}"] = None
                    fail_record[f"se_{term}"] = None
                    fail_record[f"p_{term}"] = None
                    fail_record[f"理论方向_{term}"] = expected_direction
                    fail_record[f"direction_match_{term}"] = False

                candidate_records_for_this_model.append(fail_record)

        retained = retain_results_by_rules(
            records=candidate_records_for_this_model,
            x_vars=x_vars,
            keep_all_strong_pass=main_combination_screening["keep_all_strong_pass"],
            keep_all_weak_pass=main_combination_screening["keep_all_weak_pass"],
            keep_per_size=control_search_settings["retention_rules"]["keep_per_size"],
            keep_global_extra=control_search_settings["retention_rules"]["keep_global_extra"],
            prefer_diversity=control_search_settings["retention_rules"]["prefer_diversity"],
        )

        if combination_execution["top_k_limit"] is not None:
            retained = retained[: combination_execution["top_k_limit"]]

        all_records.extend(retained)

    return pd.DataFrame(all_records)


# =========================
# 保存与汇总
# =========================

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
        model_terms = safe_str_split(row.get("检验项组合")) or [None]
        for term in model_terms:
            rows.append(preserve_module_diagnostics({
                "model_id": row.get("model_id"),
                "specification_id": make_specification_id(row),
                "模块名称": row.get("模块名称", "main_effect"),
                "假设编号": row.get("假设编号"),
                "假设描述": row.get("假设描述"),
                "relationship_type": row.get("relationship_type", "main_effect"),
                "effect_type": "main_effect",
                "path_type": "main_effect",
                "path_id": None,
                "chain_id": None,
                "模型形式": row.get("模型形式"),
                "通过规则": row.get("通过规则"),
                "被解释变量": row.get("被解释变量"),
                "解释变量": term.replace(module_config.get("nonlinear_settings", {}).get("squared_term_suffix", "_sq"), "")
                if term and term.endswith(module_config.get("nonlinear_settings", {}).get("squared_term_suffix", "_sq")) else term,
                "中介变量": None,
                "调节变量": None,
                "核心解释变量组合": row.get("核心解释变量组合"),
                "检验项": term,
                "effect_role": "main_squared" if term and term.endswith(module_config.get("nonlinear_settings", {}).get("squared_term_suffix", "_sq")) else "main_linear" if term else "unavailable",
                "理论方向": row.get(f"理论方向_{term}") if term else None,
                "direction_match": row.get(f"direction_match_{term}") if term else None,
                "beta": row.get(f"beta_{term}") if term else None,
                "se": row.get(f"se_{term}") if term else None,
                "p": row.get(f"p_{term}") if term else None,
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


def _execute_main_effect(config: Dict[str, Any], module_config: Dict[str, Any],
                         output_dir_override: Path | None = None) -> None:
    preflight(config, module_config)

    if not bool(config.get("run_settings", {}).get("enabled_modules", {}).get("main_effect", True)):
        print("全局 YAML 中 run_settings.enabled_modules.main_effect = false，主效应模块跳过。")
        return

    if not bool(module_config.get("enabled", True)):
        print("主效应 YAML 中 enabled = false，主效应模块跳过。")
        return

    df_raw = load_data(config["data"]["input_file"], config)
    preflight(config, module_config, df_raw.columns)

    continuous_settings = parse_continuous_settings(config)
    preprocessing_settings = get_preprocessing_settings(config)
    controls_cfg = get_controls(config)
    control_search_settings = get_control_search_settings(config)
    fe_settings = get_fe_settings(config)
    main_effect_settings = get_main_effect_settings(module_config)
    nonlinear_settings = get_nonlinear_settings(module_config)
    hypotheses = extract_main_effect_hypotheses(module_config)
    x_group_map = get_independent_variable_groups(module_config)

    if not hypotheses:
        print("未发现 enabled = true 的主效应假设，模块结束。")
        return

    final_output_dir = get_output_root(config) / get_sample_folder_name(config) / "main_effect"
    output_dir = output_dir_override or final_output_dir
    if output_dir_override is None:
        check_output_overwrite(output_dir, config)
    output_dir = ensure_dir(output_dir)

    print("=" * 60)
    print("开始运行主效应模块")
    print(f"全局配置文件: {Path(config['_config_path']).resolve()}")
    print(f"主效应配置文件: {Path(module_config.get('_config_path')).resolve()}")
    print(f"输出目录: {final_output_dir.resolve()}")
    print("=" * 60)

    full_df, candidate_df = run_screening_stage(
        config=config,
        df_raw=df_raw,
        hypotheses=hypotheses,
        x_group_map=x_group_map,
        continuous_settings=continuous_settings,
        preprocessing_settings=preprocessing_settings,
        controls_cfg=controls_cfg,
        fe_settings=fe_settings,
        main_effect_settings=main_effect_settings,
        nonlinear_settings=nonlinear_settings,
    )

    control_combination_df = run_control_combination_stage(
        config=config,
        df_raw=df_raw,
        hypotheses=hypotheses,
        candidate_df=candidate_df,
        continuous_settings=continuous_settings,
        controls_cfg=controls_cfg,
        control_search_settings=control_search_settings,
        fe_settings=fe_settings,
        main_effect_settings=main_effect_settings,
        nonlinear_settings=nonlinear_settings,
    )

    if not full_df.empty:
        full_df["检验阶段"] = "AB"
    if not control_combination_df.empty:
        control_combination_df["检验阶段"] = "C"
    if control_combination_df.empty:
        merged_full_df = full_df.copy()
    else:
        merged_full_df = pd.concat([full_df, control_combination_df], axis=0, ignore_index=True)
    merged_full_df = annotate(merged_full_df, config)
    policy = output_policy(config, module_config)
    hypothesis_df = single_model_hypotheses(merged_full_df, "main_effect", module_config)

    ab_decisions = hypothesis_df.loc[hypothesis_df["检验阶段"] == "AB"]
    c_decisions = hypothesis_df.loc[hypothesis_df["检验阶段"] == "C"]
    candidate_results_df = ab_decisions.loc[ab_decisions["是否候选模型"] == True].copy()
    strong_df = c_decisions.loc[c_decisions["strong_pass"] == True].copy()
    weak_df = c_decisions.loc[c_decisions["weak_pass"] == True].copy()

    if policy.raw:
        for stem, frame in (("full_results", merged_full_df),
                            ("control_combination_results", control_combination_df)):
            write_result_table(align_human_result_columns(frame if stem == "full_results" else annotate(frame, config)), output_dir, stem, csv=policy.human_csv,
                               excel=policy.human_excel)
    if policy.success:
        for stem, frame in (("candidate_results", candidate_results_df),
                            ("strong_pass_results", strong_df),
                            ("weak_pass_results", weak_df)):
            write_result_table(align_human_result_columns(frame), output_dir, stem, csv=policy.human_csv,
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

    candidate_threshold = main_effect_settings["candidate_rules"]["p_value_threshold"]
    strong_threshold = main_effect_settings["combination_screening"]["result_labels"]["strong_pass"]
    weak_threshold = main_effect_settings["combination_screening"]["result_labels"]["weak_pass"]

    summary_lines = [
        "# Main Effect Summary",
        "",
        "## 阶段A/B：基础筛查",
        f"- 基础筛查结果数：{len(full_df) if not full_df.empty else 0}",
        f"- 候选模型数：{candidate_n}",
        f"- candidate 判定阈值：p < {candidate_threshold}",
        f"- candidate 是否要求方向一致：{main_effect_settings['candidate_rules']['require_direction_match']}",
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
    print("主效应计算完成，正在校验并发布结果。")
    print("=" * 60)


def run_main_effect(config_path: str | Path = "analysis_config.yaml") -> None:
    config = load_yaml(config_path)
    module_config = load_module_yaml(config, "main_effect")
    if not (config.get("run_settings", {}).get("enabled_modules", {}).get("main_effect", True)
            and module_config.get("enabled", True)):
        _execute_main_effect(config, module_config)
        return
    run_effect("main_effect", config, module_config, SCRIPT_PATH,
               lambda stage: _execute_main_effect(config, module_config, stage))
    print(f"主效应模块运行完成；结果已输出到: {get_output_root(config) / get_sample_folder_name(config) / 'main_effect'}")


if __name__ == "__main__":
    config_path = sys.argv[1] if len(sys.argv) > 1 else default_config_path()
    run_main_effect(config_path)
