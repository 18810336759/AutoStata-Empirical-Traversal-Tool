#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Regression screening for linear moderation and two distinct curvilinear hypotheses.

The global YAML supplies data, preprocessing, controls, fixed effects and output
rules. moderation.yaml supplies each focal relationship and moderator hypothesis.
A/B screens base specifications; C evaluates control combinations; D reports
conditional effects and plot data. The module does not estimate indirect effects.
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
from 回归结果解读.基准检验.skill.other.result_schema import align_standard_columns, align_human_result_columns, build_specification_index
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


def validate_columns_exist(df: pd.DataFrame, columns: List[str]) -> Tuple[bool, List[str]]:
    missing = [c for c in columns if c and c not in df.columns]
    return len(missing) == 0, missing







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


from scipy.stats import norm, chi2, t as student_t
from patsy.builtins import Q as __stata_quote__, C as __stata_cat__
from patsy import EvalEnvironment


def quote_var(name: str) -> str:
    escaped = str(name).replace('"', '\\"')
    return f'__stata_quote__("{escaped}")'


def build_fe_terms(config: Dict[str, Any]) -> List[str]:
    fe = get_fe_settings(config)
    terms = []
    if fe["entity_fixed_effect"]:
        terms.append(f"__stata_cat__({quote_var(fe['entity_name'])})")
    if fe["time_fixed_effect"]:
        terms.append(f"__stata_cat__({quote_var(fe['time_name'])})")
    return terms


def fit_model(df: pd.DataFrame, formula: str, cluster_col: Optional[str] = None, *,
              fixed_effect_terms: Optional[List[str]] = None,
              fixed_effect_columns: Optional[List[str]] = None,
              use_absorbed: bool = True) -> Any:
    env = EvalEnvironment.capture(0).with_outer_namespace({"__stata_quote__": __stata_quote__, "__stata_cat__": __stata_cat__})
    if use_absorbed and fixed_effect_columns:
        absorbed = fit_absorbed_ols(
            df, formula, fixed_effect_terms=fixed_effect_terms or [],
            fixed_effect_columns=fixed_effect_columns,
            cluster_column=cluster_col, eval_env=env)
        if absorbed is not None:
            return absorbed
    model = smf.ols(formula=formula, data=df, eval_env=env)
    if cluster_col:
        return model.fit(cov_type="cluster", cov_kwds={"groups": df[cluster_col]})
    return model.fit()




def flatten_groups(groups: Dict[str, Any]) -> set[str]:
    return {str(value) for values in groups.values() for value in values}


def infer_path_kind(source: str, outcome: str, main_cfg: Dict[str, Any], mediation_cfg: Dict[str, Any]) -> str:
    main_vars = main_cfg.get("variables", {})
    mediation_vars = mediation_cfg.get("variables", {})
    xs = flatten_groups(main_vars.get("independent_variables", {})) | flatten_groups(mediation_vars.get("independent_variables", {}))
    ys = flatten_groups(main_vars.get("dependent_variables", {})) | flatten_groups(mediation_vars.get("dependent_variables", {}))
    ms = flatten_groups(mediation_vars.get("mediator_variables", {}))
    if source in xs and outcome in ms:
        return "x_to_m"
    if source in ms and outcome in ys:
        return "m_to_y"
    if source in xs and outcome in ys:
        return "x_to_y"
    return "custom"


def parse_hypotheses(module_cfg: Dict[str, Any], global_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    if str(module_cfg.get("moderation_schema_version")) != "3.0":
        raise ValueError("调节 YAML 必须使用 moderation_schema_version: '3.0'")
    main_cfg = load_module_yaml(global_cfg, "main_effect")
    mediation_cfg = load_module_yaml(global_cfg, "mediation_effect")
    paths = []
    seen = set()
    for item in module_cfg.get("moderation_paths", []):
        label = str(item.get("id", "")).strip()
        if not label or label in seen:
            raise ValueError(f"调节假设 id 为空或重复: {label!r}")
        seen.add(label)
        if not bool(item.get("enabled", True)):
            continue
        relation = item.get("focal_relationship") or {}
        mod = item.get("moderation") or {}
        source, outcome = str(relation.get("from", "")).strip(), str(relation.get("to", "")).strip()
        moderator = str(item.get("moderator", "")).strip()
        relation_form = str(relation.get("form", "")).strip()
        mod_form = str(mod.get("form", "")).strip()
        change = str(mod.get("expected_change", "")).strip()
        if not all([source, outcome, moderator]) or len({source, outcome, moderator}) < 3:
            raise ValueError(f"{label}: from、to、moderator 必须是三个不同的非空变量")
        if relation_form not in {"linear", "u_shape", "inverted_u_shape"}:
            raise ValueError(f"{label}: focal_relationship.form 无效: {relation_form}")
        if mod_form not in {"linear", "quadratic"}:
            raise ValueError(f"{label}: moderation.form 无效: {mod_form}")
        if relation_form == "linear" and mod_form == "linear":
            allowed = {"positive", "negative", "any"}
        elif relation_form == "linear" and mod_form == "quadratic":
            allowed = {"u_shape", "inverted_u_shape"}
        elif mod_form == "linear":
            allowed = {"steepen", "flatten", "turning_point_left", "turning_point_right", "any"}
        else:
            raise ValueError(f"{label}: 两种关系同时非线性暂不支持")
        if change not in allowed:
            raise ValueError(f"{label}: expected_change={change!r}；可选 {sorted(allowed)}")
        direction = str(relation.get("direction", "any")).strip() if relation_form == "linear" else "any"
        if direction not in {"positive", "negative", "any"}:
            raise ValueError(f"{label}: 线性关系 direction 只能是 positive、negative 或 any")
        paths.append({
            "label": label, "description": str(item.get("description", "")),
            "from": source, "to": outcome, "moderator": moderator,
            "relationship_form": relation_form, "relationship_direction": direction,
            "moderation_form": mod_form, "expected_change": change,
            "conditioning_variables": safe_str_split(item.get("conditioning_variables")),
            "target_path": infer_path_kind(source, outcome, main_cfg, mediation_cfg),
            "path_id": f"{sanitize_name(label)}_{sanitize_name(source)}_{sanitize_name(outcome)}_{sanitize_name(moderator)}",
        })
    return paths


def validate_module_settings(module_cfg: Dict[str, Any]) -> None:
    construction = module_cfg.get("interaction_construction", {})
    centering = module_cfg.get("centering", {})
    if centering.get("enabled", True) and centering.get("method", "mean_centering") != "mean_centering":
        raise ValueError("当前只支持 mean_centering")
    if not construction.get("auto_create_terms", True) and centering.get("enabled", True):
        raise ValueError("auto_create_terms=false 时请关闭 centering，并在数据中预先生成相应交互项")
    rules = module_cfg.get("moderation_settings", {}).get("candidate_rules", {})
    labels = module_cfg.get("moderation_settings", {}).get("combination_screening", {}).get("result_labels", {})
    for key, value in [("candidate p", rules.get("p_value_threshold", 0.2)), ("strong_pass", labels.get("strong_pass", .05)), ("weak_pass", labels.get("weak_pass", .1))]:
        if not 0 < float(value) < 1:
            raise ValueError(f"{key} 阈值必须介于 0 与 1 之间")
    if not 0 < float(module_cfg.get("conditional_effect_analysis", {}).get("confidence_level", .95)) < 1:
        raise ValueError("confidence_level 必须介于 0 与 1 之间")
    settings = module_cfg.get("moderation_settings", {})
    if settings.get("u_curve_linear_rule", "beta5_only") != "beta5_only":
        raise ValueError("u_curve_linear_rule 当前仅支持 beta5_only")
    output = module_cfg.get("standardized_output", {})
    if output.get("full_results_layout", "compact") != "compact":
        raise ValueError("full_results_layout 当前仅支持 compact")
    if output.get("coefficient_label_style", "term_role_with_name") != "term_role_with_name":
        raise ValueError("coefficient_label_style 当前仅支持 term_role_with_name")
    curved = settings.get("curved_relationship_checks", {})
    if curved.get("test_method", "lind_mehlum") != "lind_mehlum":
        raise ValueError("curved_relationship_checks.test_method 当前仅支持 lind_mehlum")
    if curved.get("alpha_source", "strong_pass") not in {"strong_pass", "weak_pass"}:
        raise ValueError("curved_relationship_checks.alpha_source 只能是 strong_pass 或 weak_pass")
    if curved.get("evaluation_levels", "configured_and_extremes") != "configured_and_extremes":
        raise ValueError("curved_relationship_checks.evaluation_levels 当前仅支持 configured_and_extremes")
    allowed_levels = {"mean_minus_1sd", "mean", "mean_plus_1sd"}
    levels = module_cfg.get("conditional_effect_analysis", {}).get("moderator_values", ["mean_minus_1sd", "mean", "mean_plus_1sd"])
    if not isinstance(levels, list) or not levels or any(level not in allowed_levels for level in levels):
        raise ValueError(f"moderator_values 可选 {sorted(allowed_levels)}，且必须是非空列表")


def combination(result: Any, weights: Dict[str, float]) -> Tuple[float, float, float]:
    """Estimate, robust standard error and two-sided p value of a linear contrast."""
    names = [quote_var(name) for name in weights]
    vector = np.array([weights[name] for name in weights], dtype=float)
    coef = np.array([result.params[name] for name in names], dtype=float)
    cov = result.cov_params().loc[names, names].to_numpy(dtype=float)
    estimate = float(vector @ coef)
    se = float(np.sqrt(max(0.0, vector @ cov @ vector)))
    p = float(2 * norm.sf(abs(estimate / se))) if se > 0 else (0.0 if estimate else 1.0)
    return estimate, se, p


def curve_test(result: Any, a: str, a2: str, aw: str | None, a2w: str | None,
               w: float, low: float, high: float, expected: str, alpha: float,
               require_turning_inside: bool, method: str = "legacy") -> Dict[str, Any]:
    """Check a conditional U shape; Lind–Mehlum uses a one-sided IUT at A bounds."""
    slope_low = {a: 1.0, a2: 2 * low}
    slope_high = {a: 1.0, a2: 2 * high}
    curvature = {a2: 1.0}
    if aw:
        slope_low[aw] = slope_high[aw] = w
    if a2w:
        slope_low[a2w] = 2 * low * w
        slope_high[a2w] = 2 * high * w
        curvature[a2w] = w
    left, left_se, p_left_two = combination(result, slope_low)
    right, right_se, p_right_two = combination(result, slope_high)
    quad, quad_se, p_quad = combination(result, curvature)
    linear = combination(result, {a: 1.0, **({aw: w} if aw else {})})[0]
    turn = -linear / (2 * quad) if abs(quad) > 1e-12 else None
    u_shape = expected == "u_shape"
    sign_ok = left < 0 < right and quad > 0 if u_shape else left > 0 > right and quad < 0
    inside = turn is not None and low < turn < high
    def one_sided_p(value: float, se: float, positive: bool) -> float:
        if se <= 0:
            return 0.0 if (value > 0 if positive else value < 0) else 1.0
        statistic = value / se
        if getattr(result, "use_t", False):
            return float(student_t.sf(statistic, result.df_resid) if positive else
                         student_t.cdf(statistic, result.df_resid))
        return float(norm.sf(statistic) if positive else norm.cdf(statistic))
    p_left_one = one_sided_p(left, left_se, positive=not u_shape)
    p_right_one = one_sided_p(right, right_se, positive=u_shape)
    lm_p = max(p_left_one, p_right_one)
    if method == "lind_mehlum":
        p_shape = lm_p
    else:
        p_shape = max(p_left_two, p_right_two, p_quad)
    passed = sign_ok and p_shape < alpha and (inside or not require_turning_inside)
    return {"pass": bool(passed), "turn": turn, "inside": inside,
            "left_slope": left, "right_slope": right,
            "se_left_slope": left_se, "se_right_slope": right_se,
            "p_left_slope": p_left_two, "p_right_slope": p_right_two,
            "p_left_one_sided": p_left_one, "p_right_one_sided": p_right_one,
            "lind_mehlum_p": lm_p, "shape_p": p_shape, "test_method": method,
            "curvature": quad, "se_curvature": quad_se, "p_curvature": p_quad}


def curved_diagnostic_settings(module_cfg: Dict[str, Any]) -> Dict[str, Any]:
    settings = module_cfg.get("moderation_settings", {}).get("curved_relationship_checks", {})
    enabled = bool(settings.get("enabled", False))
    labels = module_cfg.get("moderation_settings", {}).get("combination_screening", {}).get("result_labels", {})
    source = settings.get("alpha_source", "strong_pass")
    return {"enabled": enabled, "method": settings.get("test_method", "lind_mehlum") if enabled else "legacy",
            "alpha": float(labels.get(source, .05 if source == "strong_pass" else .10)) if enabled else
                     float(module_cfg.get("moderation_settings", {}).get("candidate_rules", {}).get("p_value_threshold", .2))}


def moderator_diagnostic_levels(low: float, high: float, mean: float, sd: float,
                                module_cfg: Dict[str, Any]) -> Dict[str, float]:
    levels = {"observed_min": low, "mean": mean, "observed_max": high}
    for label in module_cfg.get("conditional_effect_analysis", {}).get("moderator_values", []):
        if label == "mean_minus_1sd":
            levels[label] = max(low, mean - sd)
        elif label == "mean_plus_1sd":
            levels[label] = min(high, mean + sd)
    return levels


def turning_shift(result: Any, a: str, a2: str, aw: str, a2w: str,
                  low_w: float, high_w: float) -> Tuple[float | None, float | None, float | None]:
    names = [a, a2, aw, a2w]
    values = np.array([result.params[quote_var(name)] for name in names], dtype=float)
    def vertex(v: np.ndarray, w: float) -> float:
        denominator = 2 * (v[1] + v[3] * w)
        return -(v[0] + v[2] * w) / denominator if abs(denominator) > 1e-12 else np.nan
    left, right = vertex(values, low_w), vertex(values, high_w)
    if not np.isfinite(left) or not np.isfinite(right):
        return None, None, None
    delta = right - left
    gradient = np.zeros(4)
    for i in range(4):
        step = max(1e-6, abs(values[i]) * 1e-5)
        plus, minus = values.copy(), values.copy()
        plus[i] += step
        minus[i] -= step
        gradient[i] = ((vertex(plus, high_w) - vertex(plus, low_w)) -
                       (vertex(minus, high_w) - vertex(minus, low_w))) / (2 * step)
    covariance = result.cov_params().loc[[quote_var(n) for n in names], [quote_var(n) for n in names]].to_numpy()
    se = float(np.sqrt(max(0.0, gradient @ covariance @ gradient)))
    p = float(2 * norm.sf(abs(delta / se))) if se > 0 else (0.0 if delta else 1.0)
    return float(delta), se, p



def turning_shift_index(result: Any, a: str, a2: str, aw: str, a2w: str) -> Tuple[float, float, float]:
    """D=β₂β₄−β₁β₅; delta-method SE uses the full coefficient covariance."""
    names = [quote_var(term) for term in (a, a2, aw, a2w)]
    b1, b2, b4, b5 = (float(result.params[name]) for name in names)
    estimate = b2 * b4 - b1 * b5
    gradient = np.array([-b5, b4, b2, -b1], dtype=float)
    covariance = result.cov_params().loc[names, names].to_numpy(dtype=float)
    se = float(np.sqrt(max(0.0, gradient @ covariance @ gradient)))
    p = float(2 * norm.sf(abs(estimate / se))) if se > 0 else (0.0 if estimate else 1.0)
    return estimate, se, p


def prepare_model_data(raw: pd.DataFrame, path: Dict[str, Any], controls: List[str],
                       threshold: int, treatment: str, global_cfg: Dict[str, Any],
                       prepared_base: Tuple[pd.DataFrame, Dict[str, Any]] | None = None) -> Tuple[pd.DataFrame, Dict[str, Any]]:
    fe = get_fe_settings(global_cfg)
    necessary = list(dict.fromkeys([path["from"], path["to"], path["moderator"]] + path["conditioning_variables"] + controls +
        ([fe["entity_name"]] if fe["entity_fixed_effect"] else []) +
        ([fe["time_name"]] if fe["time_fixed_effect"] else []) +
        ([fe["cluster_level"]] if fe["cluster_enabled"] else [])))
    ok, missing = validate_columns_exist(raw, necessary)
    if not ok:
        raise KeyError(f"数据中缺少变量: {missing}")
    if prepared_base is None:
        work, info = prepare_data(
            raw, global_cfg, core_vars=[path["from"], path["to"], path["moderator"]],
            threshold=threshold, treatment=treatment,
            candidate_vars=[path["from"], path["to"], path["moderator"]] + path["conditioning_variables"] + controls)
    else:
        # Each model gets its own mutable frame and trace, while the costly
        # interpolation/winsorization result is shared across control sets.
        cached_frame, cached_info = prepared_base
        work, info = cached_frame.copy(deep=True), dict(cached_info)
    work, sample_info = select_estimation_sample(work, necessary, info)
    info.update(sample_info)
    return work, info


def fit_hypothesis(raw: pd.DataFrame, path: Dict[str, Any], global_cfg: Dict[str, Any],
                   module_cfg: Dict[str, Any], controls: List[str], threshold: int,
                   treatment: str, phase: str, with_fit: bool = False,
                   prepared_base: Tuple[pd.DataFrame, Dict[str, Any]] | None = None) -> Dict[str, Any]:
    a, b, w = path["from"], path["to"], path["moderator"]
    fe = get_fe_settings(global_cfg)
    construction = module_cfg.get("interaction_construction", {})
    center = module_cfg.get("centering", {})
    rules = module_cfg.get("moderation_settings", {}).get("candidate_rules", {})
    alpha = float(rules.get("p_value_threshold", .05))
    screening_only = phase == "AB" and not with_fit
    curve_linear = path["relationship_form"] != "linear" and path["moderation_form"] == "linear"
    strong_alpha = float(module_cfg.get("moderation_settings", {}).get("combination_screening", {})
                         .get("result_labels", {}).get("strong_pass", .05))
    conditional = module_cfg.get("conditional_effect_analysis", {})
    result_record = {
        "模块名称": "moderation_effect", "relationship_type": "moderation",
        "假设编号": path["label"], "假设描述": path["description"], "path_id": path["path_id"],
        "target_path": path["target_path"], "derived_target_path": path["target_path"],
        "focal_from": a, "focal_to": b, "focal_relationship_form": path["relationship_form"],
        "moderation_form": path["moderation_form"], "expected_change": path["expected_change"],
        "被解释变量": b, "源解释变量": a, "核心解释变量": a, "调节变量": w,
        "理论方向": path["expected_change"], "样本门槛": threshold, "处理方案": treatment,
        "控制变量组合ID": "ctrl_" + sanitize_name("_".join(
            c for c in controls if c not in safe_str_split(global_cfg.get("controls", {}).get("required_controls"))
        )) if any(c not in safe_str_split(global_cfg.get("controls", {}).get("required_controls")) for c in controls) else "ctrl_required_only",
        "控制变量组合": " ".join(controls), "纳入的控制变量": " ".join(controls),
        "纳入的可选控制变量": " ".join([c for c in controls if c not in safe_str_split(global_cfg.get("controls", {}).get("required_controls"))]),
        "控制变量数量": len(controls), "optional_control_count": len([c for c in controls if c not in safe_str_split(global_cfg.get("controls", {}).get("required_controls"))]),
        "固定效应设定": build_fixed_effects_label(global_cfg), "聚类标准误设定": build_cluster_label(global_cfg),
        "是否成功运行": "failed", "失败原因": None, "是否候选模型": False,
        "检验阶段": "A/B初筛" if screening_only else "C完整检验",
        "ab_test_term": None, "ab_p_value": None, "ab_direction_match": None,
        "ab_screen_pass": False if screening_only else None,
        "full_hypothesis_pass": None if screening_only else False,
        "strong_pass": False, "weak_pass": False,
    }
    result_record["model_id"] = "moderation_" + "_".join([path["path_id"], phase, str(threshold), sanitize_name(treatment), result_record["控制变量组合ID"]])
    try:
        df, info = prepare_model_data(raw, path, controls, threshold, treatment, global_cfg,
                                      prepared_base=prepared_base)
        result_record.update(info)
        if len(df) < fe["minimum_sample_size"] and rules.get("minimum_sample_size_required", True):
            raise ValueError(f"有效样本量 {len(df)} < {fe['minimum_sample_size']}")
        if fe["cluster_enabled"] and df[fe["cluster_level"]].nunique() < 2:
            raise ValueError("聚类组数量不足")
        continuous = parse_continuous_settings(global_cfg)
        centered = {}
        generated_expressions = {}
        for name, flag in [(a, center.get("apply_to", {}).get("focal_variable", True)),
                           (w, center.get("apply_to", {}).get("moderator", True))]:
            allowed = not center.get("center_only_continuous_variables", True) or name in continuous["continuous_variables"]
            if center.get("enabled", True) and flag and allowed and name not in continuous["exclude_variables"]:
                if not pd.api.types.is_numeric_dtype(df[name]):
                    raise TypeError(f"需要中心化的变量不是数值型: {name}")
                generated = f"{sanitize_name(name)}__centered"
                center_mean = float(df[name].mean())
                df[generated] = df[name] - center_mean
                centered[name] = generated
                generated_expressions[generated] = f"{name} - {center_mean:.17g}"
            else:
                centered[name] = name
        au, wu = centered[a], centered[w]
        a2 = a + construction.get("squared_term_suffix", "_sq")
        w2 = w + construction.get("squared_term_suffix", "_sq")
        aw = construction.get("interaction_name_pattern", "{focal}__X__{moderator}").format(focal=a, moderator=w)
        aw2 = construction.get("squared_moderator_interaction_name_pattern", "{focal}__X__{moderator}_sq").format(focal=a, moderator=w)
        a2w = construction.get("squared_focal_interaction_name_pattern", "{focal}_sq__X__{moderator}").format(focal=a, moderator=w)
        terms = [au, wu, aw]
        generated_values = {aw: df[au] * df[wu]}
        generated_expressions[aw] = f"{au} * {wu}"
        if path["moderation_form"] == "quadratic":
            terms += [w2, aw2]
            generated_values.update({w2: df[wu] ** 2, aw2: df[au] * df[wu] ** 2})
            generated_expressions.update({w2: f"{wu} ** 2", aw2: f"{au} * {wu} ** 2"})
        if path["relationship_form"] != "linear":
            terms += [a2, a2w]
            generated_values.update({a2: df[au] ** 2, a2w: df[au] ** 2 * df[wu]})
            generated_expressions.update({a2: f"{au} ** 2", a2w: f"{au} ** 2 * {wu}"})
        if construction.get("auto_create_terms", True):
            for name, values in generated_values.items():
                if name in df.columns and name not in {a, b, w}:
                    raise ValueError(f"自动生成列名与原数据冲突: {name}")
                df[name] = values
        else:
            ok, missing = validate_columns_exist(df, list(generated_values))
            if not ok:
                raise KeyError(f"需要预先生成模型项: {missing}")
        control_terms = []
        for variable in path["conditioning_variables"] + controls:
            if (center.get("enabled", True) and center.get("apply_to", {}).get("controls", False)
                    and variable in continuous["continuous_variables"] and variable not in continuous["exclude_variables"]
                    and pd.api.types.is_numeric_dtype(df[variable])):
                centered_control = f"{sanitize_name(variable)}__centered"
                control_mean = float(df[variable].mean())
                df[centered_control] = df[variable] - control_mean
                generated_expressions[centered_control] = f"{variable} - {control_mean:.17g}"
                control_terms.append(centered_control)
            else:
                control_terms.append(variable)
        rhs = list(dict.fromkeys(terms + control_terms))
        formula = quote_var(b) + " ~ " + " + ".join([quote_var(t) for t in rhs] + build_fe_terms(global_cfg))
        result_record["模型公式"] = formula
        result_record["模型项定义"] = model_terms_definition(
            outcome=b, focal_terms=terms, controls=control_terms, fe_settings=fe,
            cluster=fe["cluster_level"] if fe["cluster_enabled"] else None,
            generated_terms=generated_expressions, formula=formula)
        needed = list(dict.fromkeys([b] + rhs + ([fe["entity_name"]] if fe["entity_fixed_effect"] else []) +
                    ([fe["time_name"]] if fe["time_fixed_effect"] else []) +
                    ([fe["cluster_level"]] if fe["cluster_enabled"] else [])))
        df, sample_info = select_estimation_sample(df, needed, info)
        info.update(sample_info)
        result_record.update(sample_info)
        fit = fit_model(
            df, formula, fe["cluster_level"] if fe["cluster_enabled"] else None,
            fixed_effect_terms=build_fe_terms(global_cfg),
            fixed_effect_columns=[name for enabled, name in (
                (fe["entity_fixed_effect"], fe["entity_name"]),
                (fe["time_fixed_effect"], fe["time_name"])) if enabled],
            # Stage D uses formula-based prediction, so preserve that fitted
            # design there. A/B and C use the absorbed estimator.
            use_absorbed=not with_fit)
        focal_low, focal_high = float(df[au].min()), float(df[au].max())
        moderator_low, moderator_high = float(df[wu].min()), float(df[wu].max())
        mean_w = float(df[wu].mean())
        sd_w = float(df[wu].std())
        curved_options = curved_diagnostic_settings(module_cfg)
        shape_alpha = curved_options["alpha"]
        base_beta, base_se, base_p = combination(fit, {au: 1.0, aw: mean_w})
        base_direction = path["relationship_direction"]
        base_ok = None if screening_only else (base_direction == "any" or direction_matches(base_beta, base_direction))
        base_curve = None
        if not screening_only and path["relationship_form"] != "linear" and not curve_linear:
            base_curve = curve_test(fit, au, a2, aw, a2w, mean_w, focal_low, focal_high,
                path["relationship_form"], shape_alpha,
                conditional.get("check_turning_point_inside_observed_range", True), curved_options["method"])
            base_ok = base_curve["pass"]
        effect_role = "interaction_linear"
        core = aw
        beta, se, p = safe_get_param(fit, aw)
        shape_ok = True
        change = path["expected_change"]
        diagnostics: Dict[str, Any] = {}
        if path["moderation_form"] == "quadratic":
            effect_role, core = "interaction_moderator_squared", aw2
            beta, se, p = safe_get_param(fit, aw2)
            if screening_only:
                # The A×W² sign is the necessary initial direction check; endpoint
                # slopes and the moderator turning point belong to the C-stage test.
                shape_ok = beta is not None and (beta > 0 if change == "u_shape" else beta < 0)
            else:
                left = combination(fit, {aw: 1.0, aw2: 2 * moderator_low})
                right = combination(fit, {aw: 1.0, aw2: 2 * moderator_high})
                turn = -float(fit.params[quote_var(aw)]) / (2 * beta) if beta and abs(beta) > 1e-12 else None
                sign_ok = (left[0] < 0 < right[0] and beta > 0) if change == "u_shape" else (left[0] > 0 > right[0] and beta < 0)
                inside = turn is not None and moderator_low < turn < moderator_high
                shape_ok = sign_ok and left[2] < alpha and right[2] < alpha
                if conditional.get("check_turning_point_inside_observed_range", True):
                    shape_ok = shape_ok and inside
                diagnostics = {"turning_point": turn, "turning_point_check": inside,
                               "left_slope": left[0], "right_slope": right[0],
                               "p_left_slope": left[2], "p_right_slope": right[2]}
        elif curve_linear:
            # Both A/B and C screen the same curvature interaction. Vertex movement
            # is descriptive and never changes the pass decision.
            effect_role, core = "interaction_focal_squared", a2w
            beta, se, p = safe_get_param(fit, a2w)
            expected_sign = 1 if path["relationship_form"] == "u_shape" else -1
            if change == "flatten":
                expected_sign *= -1
            shape_ok = (beta is not None and beta * expected_sign > 0) if change in {"steepen", "flatten"} else True
            index_d, index_se, index_p = turning_shift_index(fit, au, a2, aw, a2w)
            q_low = float(fit.params[quote_var(a2)] + fit.params[quote_var(a2w)] * moderator_low)
            q_high = float(fit.params[quote_var(a2)] + fit.params[quote_var(a2w)] * moderator_high)
            curvature_crosses_zero = q_low * q_high <= 0
            diagnostics = {
                "beta_核心检验项": beta, "se_核心检验项": se, "p_核心检验项": p,
                "turning_shift_index_D": index_d, "se_turning_shift_index_D": index_se,
                "p_turning_shift_index_D": index_p,
                "turning_shift_direction": ("left" if index_d > 0 else "right" if index_d < 0 else "none"),
                "curvature_crosses_zero": curvature_crosses_zero,
            }
            # A significant β₅ cannot establish the user's left/right hypothesis.
            # The D sign is reported separately, with a curvature warning.
            base_ok = None
        else:
            shape_ok = change == "any" or direction_matches(beta, change)
        match = (bool(shape_ok) if curve_linear else bool(shape_ok and base_ok)) if not screening_only else None
        preliminary_direction = bool(shape_ok) if screening_only else None
        result_record.update({
            "是否成功运行": "success", "模型公式": formula, "样本量（N）": int(fit.nobs),
            **regression_statistics(fit, df, b, fe), "检验项": core, "effect_role": effect_role,
            "交互项变量": aw, "二次交互项变量": aw2 if path["moderation_form"] == "quadratic" else (a2w if path["relationship_form"] != "linear" else None),
            "平方项变量": a2 if path["relationship_form"] != "linear" else (w2 if path["moderation_form"] == "quadratic" else None),
            "beta_main": base_beta, "se_main": base_se, "p_main": base_p,
            "beta_linear_interaction": safe_get_param(fit, aw)[0], "p_linear_interaction": safe_get_param(fit, aw)[2],
            "beta_interaction": beta, "se_interaction": se, "p_interaction": p,
            "beta": beta, "se": se, "p": p, "direction_match": match,
            "direction_match_scope": ("curvature_interaction_only" if curve_linear else "hypothesis_direction"),
            "beta_核心检验项": beta, "se_核心检验项": se, "p_核心检验项": p,
            "核心检验项变量": core,
            "ab_test_term": core if screening_only else None,
            "ab_p_value": p if screening_only else None,
            "ab_direction_match": preliminary_direction,
            "focal_relation_check": base_ok,
            "curve_shape_check": bool(shape_ok) if not screening_only and not curve_linear else None,
            "turning_point_check": diagnostics.get("turning_point_check", base_curve["inside"] if base_curve else None),
            "moderator_min": moderator_low, "moderator_max": moderator_high,
            "moderator_mean": mean_w, "moderator_sd": sd_w,
            "focal_min": focal_low, "focal_max": focal_high, "focal_mean": float(df[au].mean()),
            "focal_term": au, "moderator_term": wu,
            "focal_squared_term": a2 if path["relationship_form"] != "linear" else None,
            "moderator_squared_term": w2 if path["moderation_form"] == "quadratic" else None,
            "interaction_linear_term": aw,
            "interaction_moderator_squared_term": aw2 if path["moderation_form"] == "quadratic" else None,
            "interaction_focal_squared_term": a2w if path["relationship_form"] != "linear" else None,
            **diagnostics,
        })
        if screening_only:
            result_record["是否候选模型"] = qualifies(result_record, alpha, module_cfg, fe["minimum_sample_size"])
            result_record["ab_screen_pass"] = result_record["是否候选模型"]
        else:
            result_record["full_hypothesis_pass"] = bool(match and p is not None and np.isfinite(p) and p < strong_alpha)
        if with_fit:
            result_record["_fit"] = fit
            result_record["_model_data"] = df
        return result_record
    except Exception as exc:
        result_record["失败原因"] = f"{type(exc).__name__}: {exc}"
        return result_record


def qualifies(record: Dict[str, Any], alpha: float, module_cfg: Dict[str, Any], minimum_n: int) -> bool:
    if record.get("是否成功运行") != "success":
        return False
    rules = module_cfg.get("moderation_settings", {}).get("candidate_rules", {})
    if rules.get("minimum_sample_size_required", True) and int(record.get("样本量（N）") or 0) < minimum_n:
        return False
    p = record.get("ab_p_value")
    if p is None or not np.isfinite(p) or p >= alpha:
        return False
    return not rules.get("require_expected_change_match", True) or bool(record.get("ab_direction_match"))


def preflight_columns(raw: pd.DataFrame, paths: List[Dict[str, Any]], global_cfg: Dict[str, Any]) -> None:
    validation = global_cfg.get("validation", {})
    if not validation.get("check_variables_exist", True):
        return
    names = set()
    for path in paths:
        names.update([path["from"], path["to"], path["moderator"]])
        names.update(path["conditioning_variables"])
    controls = get_controls(global_cfg)
    names.update(controls["required_controls"])
    if validation.get("check_global_variables_exist", True):
        names.update(controls["optional_controls"])
    fe = get_fe_settings(global_cfg)
    if fe["entity_fixed_effect"]:
        names.add(fe["entity_name"])
    if fe["time_fixed_effect"]:
        names.add(fe["time_name"])
    if fe["cluster_enabled"]:
        names.add(fe["cluster_level"])
    missing = sorted(str(name) for name in names if name and name not in raw.columns)
    if missing and validation.get("stop_if_missing_required_variables", True):
        raise KeyError(f"数据缺少配置中声明的变量: {missing}")


def run_stage_ab(raw: pd.DataFrame, paths: List[Dict[str, Any]], global_cfg: Dict[str, Any],
                 module_cfg: Dict[str, Any]) -> pd.DataFrame:
    prep = get_preprocessing_settings(global_cfg)
    required = get_controls(global_cfg)["required_controls"]
    records = []
    tasks = [(path, threshold, treatment) for path in paths
             for threshold in prep["sample_thresholds"] for treatment in prep["treatment_names"]]
    for path, threshold, treatment in stage_progress(
            tasks, total=len(tasks), module="调节效应", stage="阶段 A/B 基础筛查",
            hypothesis=lambda item: item[0]["label"]):
        record = fit_hypothesis(raw, path, global_cfg, module_cfg, required, threshold, treatment, "AB")
        records.append(record)
        if record["是否成功运行"] != "success" and global_cfg.get("run_settings", {}).get("fail_fast", False):
            raise RuntimeError(record["失败原因"])
    return pd.DataFrame(records)


def run_stage_c(raw: pd.DataFrame, paths: List[Dict[str, Any]], candidates: pd.DataFrame,
                global_cfg: Dict[str, Any], module_cfg: Dict[str, Any]) -> pd.DataFrame:
    controls = get_controls(global_cfg)
    search = get_control_search_settings(global_cfg)
    screen = module_cfg.get("moderation_settings", {}).get("combination_screening", {})
    if candidates.empty or not screen.get("enabled", True):
        return pd.DataFrame()
    optional = controls["optional_controls"]
    active = controls["active_optional_controls"]
    if active and any(name not in optional for name in active):
        raise ValueError("active_optional_controls 必须是 optional_controls 的子集")
    rules = controls["combination_rules"]
    min_k = int(rules["min_optional_controls_in_combination"])
    max_k = min(int(rules["max_optional_controls_in_combination"] or len(optional)), len(optional))
    expand_controls = bool(rules["enabled"] and search["enabled"] and optional and min_k <= max_k)
    mode = controls["combination_execution"]["mode"]
    paths_by_id = {p["path_id"]: p for p in paths}
    alpha_strong = float(screen.get("result_labels", {}).get("strong_pass", .05))
    alpha_weak = float(screen.get("result_labels", {}).get("weak_pass", .10))
    records = []
    selected = candidates.head(search["max_candidates_to_expand"])
    preprocessing_cache = PreparedDataCache()
    for index, (_, row) in enumerate(stage_progress(
            selected.iterrows(), total=len(selected), module="调节效应", stage="阶段 C 控制组合",
            hypothesis=lambda item: item[1].get("假设编号"))):
        path = paths_by_id[row["path_id"]]
        if not expand_controls:
            # Complete the C-stage hypothesis check even without optional controls.
            combos = [[]]
        elif active:
            combos = [active]
        elif mode == "full_enumeration":
            combos = generate_full_enumeration_combos(optional, min_k, max_k)
        elif mode == "sampling_enumeration":
            combos = generate_sampling_enumeration_combos(optional, min_k, max_k,
                search["random_seed"] + index, search["layer_sampling"]["random_seeds_per_size"],
                search["retention_rules"]["force_keep_full_model"])
        else:
            raise ValueError(f"不支持的控制组合模式: {mode}")
        # Prepare the union of variables needed by this candidate once. Each
        # combination still applies its own complete-case filter and computes
        # centering values on that combination's sample in fit_hypothesis.
        controls_in_combinations = list(dict.fromkeys(
            control for combo in combos for control in combo))
        preprocessing_controls = controls["required_controls"] + controls_in_combinations
        candidate_vars = ([path["from"], path["to"], path["moderator"]]
                          + path["conditioning_variables"] + preprocessing_controls)
        prepared_base = preprocessing_cache.get_or_prepare(
            raw, global_cfg,
            core_vars=[path["from"], path["to"], path["moderator"]],
            threshold=int(row["样本门槛"]), treatment=row["处理方案"],
            candidate_vars=candidate_vars)
        for combo in combos:
            record = fit_hypothesis(raw, path, global_cfg, module_cfg,
                controls["required_controls"] + combo, int(row["样本门槛"]), row["处理方案"], "C",
                prepared_base=prepared_base)
            valid = record.get("是否成功运行") == "success" and bool(record.get("direction_match"))
            p = record.get("p_interaction")
            record["是否候选模型"] = bool(row.get("是否候选模型"))
            record["ab_screen_pass"] = bool(row.get("ab_screen_pass", row.get("是否候选模型")))
            record["ab_test_term"] = row.get("ab_test_term")
            record["ab_p_value"] = row.get("ab_p_value")
            record["ab_direction_match"] = row.get("ab_direction_match")
            record["strong_pass"] = bool(valid and p is not None and np.isfinite(p) and p < alpha_strong)
            record["weak_pass"] = bool(valid and p is not None and np.isfinite(p) and p < alpha_weak)
            record["full_hypothesis_pass"] = record["strong_pass"]
            records.append(record)
            if record["是否成功运行"] != "success" and global_cfg.get("run_settings", {}).get("fail_fast", False):
                raise RuntimeError(record["失败原因"])
            if record["strong_pass"] and controls["combination_execution"].get("early_stop", False):
                break
    result = pd.DataFrame(records)
    top_k = controls["combination_execution"].get("top_k_limit")
    if top_k is not None and not result.empty:
        if "p_interaction" not in result.columns:
            result["p_interaction"] = np.nan
        retain_strong = screen.get("keep_all_strong_pass", True)
        retain_weak = screen.get("keep_all_weak_pass", True)
        protected = result.loc[(result["strong_pass"] if retain_strong else False) |
                               (result["weak_pass"] if retain_weak else False)]
        others = result.drop(index=protected.index).sort_values("p_interaction", na_position="last")
        result = pd.concat([protected, others.head(max(0, top_k - len(protected)))], ignore_index=True)
    return result


def conditional_values(record: Dict[str, Any], options: List[str]) -> List[Tuple[str, float, bool]]:
    mean, sd = float(record["moderator_mean"]), float(record["moderator_sd"])
    low, high = float(record["moderator_min"]), float(record["moderator_max"])
    mapping = {"mean_minus_1sd": mean - sd, "mean": mean, "mean_plus_1sd": mean + sd}
    result = []
    for label in options:
        if label not in mapping:
            raise ValueError(f"未知 moderator_values 选项: {label}")
        value = mapping[label]
        clipped = not low <= value <= high
        result.append((label, min(high, max(low, value)), clipped))
    return result


def conditional_prediction_rows(fit: Any, model_data: pd.DataFrame, model: Dict[str, Any],
                                path: Dict[str, Any], label: str, wval: float,
                                confidence: float, global_cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Predict B with numeric controls at their means and fixed effects at one observed category."""
    reference = model_data.iloc[0].copy()
    for control in path["conditioning_variables"] + safe_str_split(model["纳入的控制变量"]):
        if control in model_data and pd.api.types.is_numeric_dtype(model_data[control]):
            reference[control] = float(model_data[control].mean())
        centered_control = f"{sanitize_name(control)}__centered"
        if centered_control in model_data:
            reference[centered_control] = float(model_data[centered_control].mean())
    values = np.linspace(model["focal_min"], model["focal_max"], 51)
    prediction_data = pd.DataFrame([reference.to_dict() for _ in values])
    a, w = model["focal_term"], model["moderator_term"]
    prediction_data[a] = values
    prediction_data[w] = wval
    prediction_data[model["interaction_linear_term"]] = values * wval
    if model.get("focal_squared_term"):
        prediction_data[model["focal_squared_term"]] = values ** 2
        prediction_data[model["interaction_focal_squared_term"]] = values ** 2 * wval
    if model.get("moderator_squared_term"):
        prediction_data[model["moderator_squared_term"]] = wval ** 2
        prediction_data[model["interaction_moderator_squared_term"]] = values * wval ** 2
    original_center = float(model_data[path["from"]].mean()) if a != path["from"] else 0.0
    summary = fit.get_prediction(prediction_data).summary_frame(alpha=1 - confidence)
    turning_model = model.get(f"turning_point_{label}")
    if turning_model is None or not np.isfinite(turning_model):
        turning_model = None
    return [{"model_id": model["model_id"], "plot_type": "conditional_prediction",
             "moderator_value_label": label, "moderator_value": wval,
             "focal_value": float(x + original_center), "focal_value_model": float(x),
             "estimate": float(row["mean"]), "ci_low": float(row["mean_ci_lower"]),
             "ci_high": float(row["mean_ci_upper"]),
             "turning_point": float(turning_model + original_center) if turning_model is not None else None,
             "prediction_reference": "numeric_controls_at_mean;entity_time_at_first_observed",
             "expected_change": path["expected_change"]}
            for x, (_, row) in zip(values, summary.iterrows())]


def run_conditional_stage(raw: pd.DataFrame, paths: List[Dict[str, Any]], candidates: pd.DataFrame,
                          global_cfg: Dict[str, Any], module_cfg: Dict[str, Any]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    settings = module_cfg.get("conditional_effect_analysis", {})
    if not settings.get("enabled", True) or candidates.empty:
        return pd.DataFrame(), pd.DataFrame()
    by_id = {p["path_id"]: p for p in paths}
    required = get_controls(global_cfg)["required_controls"]
    output, plot_rows = [], []
    z = float(norm.ppf((1 + float(settings.get("confidence_level", .95))) / 2))
    for _, candidate in stage_progress(
            candidates.iterrows(), total=len(candidates), module="调节效应", stage="阶段 D 条件效应",
            hypothesis=lambda item: item[1].get("假设编号")):
        path = by_id[candidate["path_id"]]
        model = fit_hypothesis(raw, path, global_cfg, module_cfg, required,
            int(candidate["样本门槛"]), candidate["处理方案"], "AB", with_fit=True)
        if model.get("是否成功运行") != "success":
            output.append({"model_id": candidate["model_id"], "假设编号": path["label"],
                           "expected_change": path["expected_change"], "是否成功运行": "failed", "失败原因": model.get("失败原因")})
            continue
        fit = model.pop("_fit")
        model_data = model.pop("_model_data")
        a, w, aw = model["focal_term"], model["moderator_term"], model["interaction_linear_term"]
        a2, aw2, a2w = model["focal_squared_term"], model["interaction_moderator_squared_term"], model["interaction_focal_squared_term"]
        values = conditional_values(model, settings.get("moderator_values", ["mean_minus_1sd", "mean", "mean_plus_1sd"]))
        curved_options = curved_diagnostic_settings(module_cfg)
        if a2 and curved_options["enabled"]:
            values = [("observed_min", model["moderator_min"], False)] + values + [
                ("observed_max", model["moderator_max"], False)]
        values = list({label: (label, wval, clipped) for label, wval, clipped in values}.values())
        for label, wval, clipped in values:
            slope_weights = {a: 1.0, aw: wval}
            if aw2:
                slope_weights[aw2] = wval ** 2
            if a2w:
                slope_weights[a2w] = 2 * model["focal_mean"] * wval
                slope_weights[a2] = 2 * model["focal_mean"]
            estimate, se, p = combination(fit, slope_weights)
            curve = None
            if a2:
                curve = curve_test(fit, a, a2, aw, a2w, wval,
                    model["focal_min"], model["focal_max"], path["relationship_form"],
                    curved_options["alpha"],
                    settings.get("check_turning_point_inside_observed_range", True),
                    curved_options["method"])
            output.append({
                "model_id": model["model_id"], "path_id": path["path_id"], "假设编号": path["label"],
                "expected_change": path["expected_change"],
                "target_path": path["target_path"], "被解释变量": path["to"], "核心解释变量": path["from"],
                "调节变量": path["moderator"], "moderator_value_label": label, "moderator_value": wval,
                "moderator_value_clipped": clipped, "simple_slope": estimate, "se": se, "p": p,
                "ci_low": estimate - z * se, "ci_high": estimate + z * se,
                "curvature": curve["curvature"] if curve else None,
                "se_curvature": curve["se_curvature"] if curve else None,
                "p_curvature": curve["p_curvature"] if curve else None,
                "turning_point": curve["turn"] if curve else None,
                "turning_point_inside": curve["inside"] if curve else None,
                "left_slope": curve["left_slope"] if curve else None,
                "right_slope": curve["right_slope"] if curve else None,
                "p_left_slope_one_sided": curve["p_left_one_sided"] if curve else None,
                "p_right_slope_one_sided": curve["p_right_one_sided"] if curve else None,
                "lind_mehlum_p": curve["lind_mehlum_p"] if curve else None,
                "curve_test_method": curve["test_method"] if curve else None,
                "curve_shape_check": curve["pass"] if curve else None,
                "shape_flip": model.get("shape_flip"),
                "beta_curvature_change": model.get("beta_curvature_change"),
                "p_curvature_change": model.get("p_curvature_change"),
                "turning_point_change": model.get("turning_point_change"),
                "p_turning_point_change": model.get("p_turning_point_change"),
                "样本门槛": model["样本门槛"], "处理方案": model["处理方案"],
                "是否插值": model["是否插值"], "是否缩尾": model["是否缩尾"],
                "控制变量组合ID": model["控制变量组合ID"],
                "控制变量组合": model["控制变量组合"],
                "纳入的控制变量": model["纳入的控制变量"],
                "纳入的可选控制变量": model["纳入的可选控制变量"],
                "控制变量数量": model["控制变量数量"],
                "optional_control_count": model["optional_control_count"],
                "固定效应设定": model["固定效应设定"],
                "聚类标准误设定": model["聚类标准误设定"],
                "普通 R²": model.get("普通 R²"), "调整 R²": model.get("调整 R²"),
                "within R²": model["within R²"],
                "组内 R²定义": model.get("组内 R²定义"),
                "组内 R²吸收项": model.get("组内 R²吸收项"),
                "模型公式": model.get("模型公式"), "模型项定义": model.get("模型项定义"),
                "样本量（N）": model["样本量（N）"],
                "是否成功运行": "success", "失败原因": None,
            })
            if settings.get("save_plot_data", True) or settings.get("draw_plot_in_current_module", False):
                if aw2:
                    grid_w = np.linspace(model["moderator_min"], model["moderator_max"], 51)
                    for value in grid_w:
                        weights = {a: 1.0, aw: float(value), aw2: float(value ** 2)}
                        yhat, yse, _ = combination(fit, weights)
                        plot_rows.append({"model_id": model["model_id"], "plot_type": "slope_by_moderator",
                            "moderator_value": value, "focal_value": None, "estimate": yhat,
                            "expected_change": path["expected_change"],
                            "ci_low": yhat-z*yse, "ci_high": yhat+z*yse})
                else:
                    plot_rows.extend(conditional_prediction_rows(fit, model_data, model, path,
                        label, wval, float(settings.get("confidence_level", .95)), global_cfg))
    return pd.DataFrame(output), pd.DataFrame(plot_rows)


def build_standardized_results(models: pd.DataFrame, conditional: pd.DataFrame,
                               global_cfg: Dict[str, Any], module_cfg: Dict[str, Any]) -> pd.DataFrame:
    rows = []
    for _, record in models.iterrows():
        row = record.to_dict()
        row.update({"specification_id": make_specification_id(record), "effect_type": "moderation",
                    "path_type": record.get("target_path"),
                    "模型形式": f"{record.get('focal_relationship_form')} × {record.get('moderation_form')}",
                    "通过规则": ("beta5_only" if record.get("focal_relationship_form") != "linear"
                                 and record.get("moderation_form") == "linear" else "module_rule"),
                    "核心解释变量组合": f"{record.get('focal_from')} × {record.get('调节变量')}",
                    "样本区间": global_cfg.get("data", {}).get("sample_info", {}).get("sample_period"),
                    "样本规则": "、".join(global_cfg.get("data", {}).get("sample_info", {}).get("sample_rules", []) or ["全样本"]),
                    "解释变量": record.get("focal_from"),
                    "global_config_path": global_cfg.get("_config_path"),
                    "module_config_path": module_cfg.get("_config_path")})
        rows.append(row)
    for _, record in conditional.iterrows():
        if record.get("是否成功运行") != "success":
            continue
        row = record.to_dict()
        row.update({"specification_id": make_specification_id(record), "模块名称": "moderation_effect",
                    "样本区间": global_cfg.get("data", {}).get("sample_info", {}).get("sample_period"),
                    "样本规则": "、".join(global_cfg.get("data", {}).get("sample_info", {}).get("sample_rules", []) or ["全样本"]),
                    "relationship_type": "moderation", "effect_type": "conditional_effect",
                    "检验项": record.get("moderator_value_label"), "effect_role": "conditional_slope",
                    "解释变量": record.get("核心解释变量"), "beta": record.get("simple_slope"),
                    "global_config_path": global_cfg.get("_config_path"),
                    "module_config_path": module_cfg.get("_config_path")})
        rows.append(row)
    standard = align_standard_columns(pd.DataFrame(rows))
    return standard


COMPACT_RESULT_COLUMNS = [
    "model_id", "specification_id", "假设编号", "假设描述", "检验阶段", "target_path",
    "被解释变量", "focal_from", "focal_to", "调节变量",
    "focal_relationship_form", "moderation_form",
    "核心检验项变量", "effect_role",
    "beta_核心检验项", "se_核心检验项", "p_核心检验项",
    "direction_match", "direction_match_scope", "是否候选模型", "strong_pass", "weak_pass",
    "turning_shift_index_D", "se_turning_shift_index_D", "p_turning_shift_index_D",
    "turning_shift_direction", "curvature_crosses_zero",
    "样本门槛", "处理方案", "是否插值", "是否缩尾", "控制变量组合ID", "控制变量组合", "纳入的控制变量", "纳入的可选控制变量",
    "固定效应设定", "聚类标准误设定", "模型公式", "模型项定义",
    "普通 R²", "调整 R²", "within R²", "组内 R²定义", "组内 R²吸收项", "样本量（N）",
    "sample_identity", "estimation_sample_n", "complete_case_removed_n",
    "processing_definition_id", "processing_trace_json", "processing_input_n", "processing_eligible_n",
    "是否成功运行", "失败原因", "expected_change",
]


def compact_results(frame: pd.DataFrame) -> pd.DataFrame:
    """One model per row, with an explicit term name beside its beta/SE/p."""
    result = frame.copy()
    for column in COMPACT_RESULT_COLUMNS:
        if column not in result.columns:
            result[column] = None
    return result[COMPACT_RESULT_COLUMNS]


def draw_conditional_plots(plot_data: pd.DataFrame, output_dir: Path) -> None:
    """Write portable SVG curves without requiring a plotting package."""
    if plot_data.empty:
        return
    from html import escape
    plot_dir = ensure_dir(output_dir / "conditional_plots")
    for model_id, group in plot_data.groupby("model_id"):
        is_slope = group["plot_type"].iloc[0] == "slope_by_moderator"
        xcol = "moderator_value" if is_slope else "focal_value"
        series = [("conditional slope", group.drop_duplicates(xcol))] if is_slope else list(group.groupby("moderator_value_label"))
        values = group[["estimate", "ci_low", "ci_high"]].to_numpy(dtype=float)
        ymin, ymax = float(np.nanmin(values)), float(np.nanmax(values))
        xmin, xmax = float(group[xcol].min()), float(group[xcol].max())
        if xmax <= xmin:
            xmax = xmin + 1
        if ymax <= ymin:
            ymax = ymin + 1
        def point(x: float, y: float) -> str:
            px = 58 + 620 * (x - xmin) / (xmax - xmin)
            py = 405 - 350 * (y - ymin) / (ymax - ymin)
            return f"{px:.1f},{py:.1f}"
        svg = ['<svg xmlns="http://www.w3.org/2000/svg" width="720" height="460" viewBox="0 0 720 460">',
               '<rect width="720" height="460" fill="white"/>',
               '<path d="M58 55 V405 H678" fill="none" stroke="#444" stroke-width="1.5"/>']
        colors = ["#2463a5", "#c04d3b", "#38835e", "#8057a0"]
        for index, (label, data) in enumerate(series):
            data = data.sort_values(xcol)
            color = colors[index % len(colors)]
            points = " ".join(point(float(row[xcol]), float(row["estimate"])) for _, row in data.iterrows())
            svg.append(f'<polyline points="{points}" fill="none" stroke="{color}" stroke-width="2.5"/>')
            svg.append(f'<text x="{80 + index * 120}" y="28" font-size="12" fill="{color}">{escape(str(label))}</text>')
            if not is_slope and "turning_point" in data:
                turns = data["turning_point"].dropna()
                if not turns.empty and xmin <= float(turns.iloc[0]) <= xmax:
                    tx = 58 + 620 * (float(turns.iloc[0]) - xmin) / (xmax - xmin)
                    svg.append(f'<path d="M{tx:.1f} 55 V405" stroke="{color}" stroke-dasharray="4 4" opacity="0.5"/>')
        svg.extend([f'<text x="58" y="428" font-size="12">{xmin:.3g}</text>',
                    f'<text x="645" y="428" font-size="12">{xmax:.3g}</text>',
                    f'<text x="7" y="61" font-size="12">{ymax:.3g}</text>',
                    f'<text x="7" y="405" font-size="12">{ymin:.3g}</text>',
                    f'<text x="275" y="451" font-size="13">{"Moderator" if is_slope else "Focal variable"}</text>',
                    '</svg>'])
        (plot_dir / f"{sanitize_name(model_id)}.svg").write_text("\n".join(svg), encoding="utf-8")



def _execute_moderation_effect(global_cfg: Dict[str, Any], module_cfg: Dict[str, Any],
                               output_dir_override: Path | None = None) -> None:
    preflight(global_cfg, module_cfg)
    if not global_cfg.get("run_settings", {}).get("enabled_modules", {}).get("moderation_effect", True):
        print("全局配置关闭了调节模块。")
        return
    if not module_cfg.get("enabled", True):
        print("调节模块配置已关闭。")
        return
    validate_module_settings(module_cfg)
    paths = parse_hypotheses(module_cfg, global_cfg)
    if not paths:
        if global_cfg.get("validation", {}).get("stop_if_no_enabled_hypotheses", False):
            raise ValueError("没有启用的调节假设")
        print("没有启用的调节假设。")
        return
    raw = load_data(global_cfg["data"]["input_file"], global_cfg)
    preflight(global_cfg, module_cfg, raw.columns)
    preflight_columns(raw, paths, global_cfg)
    final_output_dir = get_output_root(global_cfg) / get_sample_folder_name(global_cfg) / "moderation_effect"
    output_dir = output_dir_override or final_output_dir
    if output_dir_override is None:
        check_output_overwrite(output_dir, global_cfg)
    output_dir = ensure_dir(output_dir)
    screening = run_stage_ab(raw, paths, global_cfg, module_cfg)
    candidates = screening.loc[screening["是否候选模型"] == True].copy() if not screening.empty else pd.DataFrame()
    global_candidate_only = get_controls(global_cfg)["combination_rules"].get("run_only_for_candidates", True)
    module_candidate_only = module_cfg.get("moderation_settings", {}).get("combination_screening", {}).get("run_only_for_candidates", True)
    stage_c_inputs = candidates if global_candidate_only or module_candidate_only else screening
    combos = run_stage_c(raw, paths, stage_c_inputs, global_cfg, module_cfg)
    conditional_cfg = module_cfg.get("conditional_effect_analysis", {})
    conditional_inputs = candidates if conditional_cfg.get("run_only_for_candidates", True) else screening.loc[screening["是否成功运行"] == "success"]
    conditional, plot_data = run_conditional_stage(raw, paths, conditional_inputs, global_cfg, module_cfg)
    full = annotate(pd.concat([screening, combos], ignore_index=True) if not combos.empty else screening, global_cfg)
    hypothesis_df = single_model_hypotheses(full, "moderation_effect", module_cfg)
    ab_decisions = hypothesis_df.loc[hypothesis_df["检验阶段"] == "AB"]
    c_decisions = hypothesis_df.loc[hypothesis_df["检验阶段"] == "C"]
    candidate_results = ab_decisions.loc[ab_decisions["是否候选模型"] == True].copy()
    strong = c_decisions.loc[c_decisions["strong_pass"] == True].copy()
    weak = c_decisions.loc[c_decisions["weak_pass"] == True].copy()
    standard = align_standard_columns(apply_standardized_options(annotate(
        build_standardized_results(full, conditional, global_cfg, module_cfg), global_cfg), global_cfg))
    save_processing_definitions(full, output_dir)
    policy = output_policy(global_cfg, module_cfg)
    compact = module_cfg.get("standardized_output", {}).get("full_results_layout", "compact") == "compact"
    display = compact_results if compact else lambda frame: frame
    if policy.raw:
        for stem, frame in (("full_results", full), ("control_combination_results", combos)):
            source = frame if stem == "full_results" else annotate(frame, global_cfg)
            write_result_table(align_human_result_columns(display(source)), output_dir, stem,
                               csv=policy.human_csv, excel=policy.human_excel)
    if policy.success:
        for stem, frame in (("candidate_results", candidate_results), ("strong_pass_results", strong),
                            ("weak_pass_results", weak)):
            write_result_table(align_human_result_columns(frame), output_dir, stem,
                               csv=policy.human_csv, excel=policy.human_excel)
        if (module_cfg.get("conditional_effect_analysis", {}).get("save_effect_table", True)
                and module_cfg.get("standardized_output", {}).get("save_conditional_effect_results", True)):
            for stem in ("conditional_effect_results", "simple_slope_results"):
                write_result_table(conditional, output_dir, stem,
                                   csv=policy.human_csv, excel=policy.human_excel)
        if module_cfg.get("conditional_effect_analysis", {}).get("save_plot_data", True):
            write_result_table(plot_data, output_dir, "conditional_plot_data",
                               csv=policy.human_csv, excel=policy.human_excel)
    if module_cfg.get("conditional_effect_analysis", {}).get("draw_plot_in_current_module", False):
        draw_conditional_plots(plot_data, output_dir)
    if policy.machine:
        write_result_table(hypothesis_df, output_dir, "hypothesis_results",
                           csv=True, excel=policy.human_excel)
        write_result_table(standard, output_dir, "standardized_results",
                           csv=True, excel=policy.human_excel, machine=True)
        if policy.specification_index:
            write_result_table(build_specification_index(standard), output_dir,
                               "specification_index", csv=True, excel=policy.human_excel)
    if policy.summary:
        summary = ["# 调节效应运行摘要", "", f"基础模型：{len(screening)}",
                   f"候选模型：{len(candidates)}", f"控制变量组合：{len(combos)}",
                   f"强通过：{len(strong)}", f"弱通过：{len(weak)}",
                   f"条件效应记录：{len(conditional)}",
                   f"失败模型：{int((full['是否成功运行'] == 'failed').sum()) if not full.empty else 0}"]
        (output_dir / "summary.md").write_text("\n".join(summary) + "\n", encoding="utf-8")
    print("调节效应计算完成，正在校验并发布结果。")


def run_moderation_effect(config_path: str | Path = DEFAULT_CONFIG_PATH) -> None:
    global_cfg = load_yaml(config_path)
    module_cfg = load_module_yaml(global_cfg, "moderation_effect")
    if not (global_cfg.get("run_settings", {}).get("enabled_modules", {}).get("moderation_effect", True)
            and module_cfg.get("enabled", True)):
        _execute_moderation_effect(global_cfg, module_cfg)
        return
    run_effect("moderation_effect", global_cfg, module_cfg, SCRIPT_PATH,
               lambda stage: _execute_moderation_effect(global_cfg, module_cfg, stage))
    print(f"调节效应模块运行完成；结果已输出到: {get_output_root(global_cfg) / get_sample_folder_name(global_cfg) / 'moderation_effect'}")


if __name__ == "__main__":
    run_moderation_effect(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CONFIG_PATH)
