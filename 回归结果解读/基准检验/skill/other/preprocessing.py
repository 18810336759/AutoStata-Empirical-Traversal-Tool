"""One preprocessing contract for main, mediation and moderation screening.

The JSON definition is a recipe for a future Stata implementation. It records
the exact order and parameters; the trace records what happened to this input.
Sample labels in the global YAML are metadata and never filter rows here.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


PROFILE_FLAGS = {
    "baseline_required_controls": (False, False),
    "interpolate_controls": (True, False),
    "winsorize_controls": (False, True),
    "interpolate_winsorize_controls": (True, True),
}
PROCESSING_VERSION = "2.0-shared-1"


def treatment_names(config: Mapping[str, Any]) -> list[str]:
    profiles = config.get("preprocessing", {}).get("treatment_profiles", {})
    names = ["baseline_required_controls"]
    if profiles.get("interpolate", False):
        names.append("interpolate_controls")
    if profiles.get("winsorize", False):
        names.append("winsorize_controls")
    if profiles.get("interpolate", False) and profiles.get("winsorize", False):
        names.append("interpolate_winsorize_controls")
    return names


def _names(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return value.split()
    return [str(item) for item in value if str(item).strip()]


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(value for value in values if value))


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def processing_definition(config: Mapping[str, Any], threshold: int, core_vars: Iterable[str],
                          treatment: str, candidate_vars: Iterable[str]) -> dict[str, Any]:
    if treatment not in PROFILE_FLAGS:
        raise ValueError(f"未知处理方案: {treatment}")
    do_interpolate, do_winsorize = PROFILE_FLAGS[treatment]
    prep = config.get("preprocessing", {})
    panel = config.get("data", {}).get("panel", {})
    continuous = config.get("continuous_variable_settings", {})
    listed = _unique(candidate_vars)
    allowed = set(_names(continuous.get("continuous_variables"))) - set(_names(continuous.get("exclude_variables")))
    interpolation_vars = [v for v in listed if v in allowed and v in set(_names(continuous.get("interpolate_allowed")))] if do_interpolate else []
    winsor_vars = [v for v in listed if v in allowed and v in set(_names(continuous.get("winsorize_allowed")))] if do_winsorize else []
    interp = prep.get("interpolation", {})
    winsor = prep.get("winsorize", {})
    return {
        "schema_version": PROCESSING_VERSION,
        "order": ["valid_year_threshold_on_raw_core", "within_entity_interpolation",
                  "winsorization", "model_specific_complete_case"],
        "sample_descriptions_are_filters": False,
        "profile": treatment,
        "threshold": {"minimum_distinct_valid_years": int(threshold),
                      "core_variables": _unique(core_vars), "entity": panel.get("entity_name"),
                      "year": panel.get("time_name"), "count_before_interpolation": True},
        "processing_candidates": listed,
        "interpolation": {"enabled": do_interpolate, "variables": interpolation_vars,
                          "method": interp.get("method", "linear"), "scope": interp.get("scope", "within_entity"),
                          "limit_direction": interp.get("limit_direction", "both"),
                          "max_gap_calendar_years": interp.get("max_gap"),
                          "interior": "linear_by_numeric_year",
                          "year_values": "finite_integer_calendar_years",
                          "boundary": "nearest_observed_value_for_allowed_direction"},
        "winsorize": {"enabled": do_winsorize, "variables": winsor_vars,
                      "method": winsor.get("method", "winsorize"),
                      "lower_quantile": winsor.get("lower_quantile", .01),
                      "upper_quantile": winsor.get("upper_quantile", .99),
                      "scope": winsor.get("scope", "full_sample"),
                      "industry": winsor.get("industry_name"),
                      "missing_group_keys": "leave_original_value_unchanged",
                      "quantile_interpolation": "linear"},
        "missingness": {"complete_case_after_preprocessing": True,
                        "estimation_variables_supplied_by_model": True},
    }


def _valid_year_mask(raw: pd.DataFrame, definition: Mapping[str, Any]) -> pd.Series:
    threshold = definition["threshold"]
    entity, year = threshold["entity"], threshold["year"]
    core = threshold["core_variables"]
    if not entity or not year or entity not in raw or year not in raw:
        if threshold["minimum_distinct_valid_years"] > 1:
            raise ValueError("有效年份门槛大于 1 时必须提供企业和年份列")
        return pd.Series(True, index=raw.index)
    valid = raw[core].notna().all(axis=1) & raw[entity].notna() & raw[year].notna()
    pairs = raw.loc[valid, [entity, year]].drop_duplicates()
    counts = pairs.groupby(entity, observed=True)[year].nunique()
    allowed_entities = counts.index[counts >= threshold["minimum_distinct_valid_years"]]
    return raw[entity].isin(allowed_entities)


def _interpolate_group(frame: pd.DataFrame, column: str, year: str,
                       direction: str, max_gap: int | None) -> pd.Series:
    years = pd.to_numeric(frame[year], errors="raise").to_numpy(dtype=float)
    if not np.isfinite(years).all():
        raise ValueError("插值年份必须为非缺失的有限数值")
    if not np.equal(years, np.floor(years)).all():
        raise ValueError("插值年份必须为整数日历年")
    values = pd.to_numeric(frame[column], errors="raise").to_numpy(dtype=float)
    known = np.flatnonzero(np.isfinite(values))
    filled = values.copy()
    if len(known) == 0:
        return pd.Series(filled, index=frame.index)
    for pos in np.flatnonzero(np.isnan(values)):
        left = known[known < pos]
        right = known[known > pos]
        if len(left) and len(right):
            lo, hi = left[-1], right[0]
            gap = years[hi] - years[lo] - 1
            if max_gap is None or gap <= max_gap:
                filled[pos] = values[lo] + (values[hi] - values[lo]) * (years[pos] - years[lo]) / (years[hi] - years[lo])
        elif len(left) and direction in {"both", "forward"}:
            lo = left[-1]
            if max_gap is None or years[pos] - years[lo] <= max_gap:
                filled[pos] = values[lo]
        elif len(right) and direction in {"both", "backward"}:
            hi = right[0]
            if max_gap is None or years[hi] - years[pos] <= max_gap:
                filled[pos] = values[hi]
    return pd.Series(filled, index=frame.index)


def _interpolate(data: pd.DataFrame, definition: Mapping[str, Any]) -> dict[str, int]:
    block = definition["interpolation"]
    if not block["enabled"] or not block["variables"]:
        return {}
    entity, year = definition["threshold"]["entity"], definition["threshold"]["year"]
    if block["method"] != "linear" or block["scope"] != "within_entity":
        raise ValueError("当前只支持按企业、按真实年份距离线性插值")
    if not entity or not year or entity not in data or year not in data:
        raise ValueError("插值需要企业列和年份列")
    if data.loc[data[[entity, year]].notna().all(axis=1)].duplicated([entity, year]).any():
        raise ValueError("同一企业-年份存在重复观测，无法确定插值顺序")
    direction = block["limit_direction"]
    if direction not in {"both", "forward", "backward"}:
        raise ValueError(f"未知插值方向: {direction}")
    max_gap = block["max_gap_calendar_years"]
    counts = {}
    for variable in block["variables"]:
        if variable not in data:
            raise KeyError(f"插值变量不存在: {variable}")
        if not pd.api.types.is_numeric_dtype(data[variable]):
            raise TypeError(f"插值变量必须为数值型: {variable}")
        # Linear interpolation yields fractional values even for float32/int inputs.
        # Convert once before assignment so pandas never has to widen a column implicitly.
        data[variable] = pd.to_numeric(data[variable], errors="raise").astype("float64")
        before = data[variable].notna().sum()
        ordered = data.loc[data[[entity, year]].notna().all(axis=1)].sort_values([entity, year], kind="stable")
        for _, group in ordered.groupby(entity, sort=False, observed=True):
            data.loc[group.index, variable] = _interpolate_group(group, variable, year, direction, max_gap)
        counts[variable] = int(data[variable].notna().sum() - before)
    return counts


def _winsorize(data: pd.DataFrame, definition: Mapping[str, Any]) -> tuple[dict[str, int], int]:
    block = definition["winsorize"]
    if not block["enabled"] or not block["variables"]:
        return {}, 0
    if block["method"] != "winsorize":
        raise ValueError("当前只支持 winsorize 缩尾")
    scope = block["scope"]
    year = definition["threshold"]["year"]
    groups = {"full_sample": [], "by_year": [year], "by_industry": [block["industry"]],
              "by_industry_year": [block["industry"], year]}.get(scope)
    if groups is None:
        raise ValueError(f"未知缩尾范围: {scope}")
    if any(not field or field not in data for field in groups):
        raise KeyError(f"缩尾分组列不存在: {groups}")
    low_q, high_q = float(block["lower_quantile"]), float(block["upper_quantile"])
    counts = {}
    group_count = 1 if not groups else int(data[groups].dropna().drop_duplicates().shape[0])
    for variable in block["variables"]:
        if variable not in data:
            raise KeyError(f"缩尾变量不存在: {variable}")
        if not pd.api.types.is_numeric_dtype(data[variable]):
            raise TypeError(f"缩尾变量必须为数值型: {variable}")
        before = data[variable].copy()
        if groups:
            def clip_group(series: pd.Series) -> pd.Series:
                if series.notna().sum() == 0:
                    return series
                return series.clip(series.quantile(low_q), series.quantile(high_q))
            grouped_rows = data[groups].notna().all(axis=1)
            data.loc[grouped_rows, variable] = (
                data.loc[grouped_rows].groupby(groups, observed=True)[variable]
                .transform(clip_group)
            )
        elif before.notna().sum():
            data[variable] = before.clip(before.quantile(low_q), before.quantile(high_q))
        counts[variable] = int((before.notna() & data[variable].notna() & before.ne(data[variable])).sum())
    return counts, group_count


def prepare_data(raw: pd.DataFrame, config: Mapping[str, Any], *, core_vars: Iterable[str],
                 threshold: int, treatment: str, candidate_vars: Iterable[str]) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Threshold on raw core completeness, then interpolate and winsorize.

    No model-specific complete-case deletion occurs here. The caller invokes
    select_estimation_sample after constructing its model terms.
    """
    definition = processing_definition(config, threshold, core_vars, treatment, candidate_vars)
    required = _unique(definition["threshold"]["core_variables"] + definition["processing_candidates"])
    missing = [name for name in required if name not in raw]
    if missing:
        raise KeyError(f"数据中缺少处理变量: {missing}")
    mask = _valid_year_mask(raw, definition)
    data = raw.loc[mask].copy()
    if data.empty:
        raise ValueError("有效年份门槛筛选后为空")
    trace = {"input_rows": int(len(raw)), "eligible_rows": int(len(data)),
             "eligible_entities": int(data[definition["threshold"]["entity"]].nunique())
             if definition["threshold"]["entity"] in data else None,
             "interpolated_cells": {}, "winsorized_cells": {}, "winsor_groups": 0}
    trace["interpolated_cells"] = _interpolate(data, definition)
    trace["winsorized_cells"], trace["winsor_groups"] = _winsorize(data, definition)
    definition_text = _json(definition)
    info = {"处理方案": treatment, "是否插值": PROFILE_FLAGS[treatment][0],
            "是否缩尾": PROFILE_FLAGS[treatment][1],
            "processing_definition_id": _sha(definition),
            "processing_definition_json": definition_text,
            "processing_trace_json": _json(trace),
            "processing_input_n": trace["input_rows"],
            "processing_eligible_n": trace["eligible_rows"]}
    provenance = config.get("_run_provenance", {})
    if provenance.get("data_sha256"):
        info["source_data_sha256"] = provenance["data_sha256"]
    return data, info


class PreparedDataCache:
    """A one-entry cache for a shared preprocessing recipe.

    The cached frame is a read-only base snapshot: callers must copy it before
    adding model terms or otherwise mutating it. A single entry bounds memory
    use while still allowing all control combinations for one hypothesis to
    reuse interpolation and winsorization.
    """

    def __init__(self) -> None:
        self._key: tuple[int, str] | None = None
        self._data: pd.DataFrame | None = None
        self._info: dict[str, Any] | None = None
        self.hits = 0
        self.misses = 0

    def get_or_prepare(self, raw: pd.DataFrame, config: Mapping[str, Any], *,
                       core_vars: Iterable[str], threshold: int, treatment: str,
                       candidate_vars: Iterable[str]) -> tuple[pd.DataFrame, dict[str, Any]]:
        # Materialize iterables because the recipe builder consumes them and
        # prepare_data must receive the same variables on a cache miss.
        core_vars = list(core_vars)
        candidate_vars = list(candidate_vars)
        definition = processing_definition(config, threshold, core_vars, treatment, candidate_vars)
        key = (id(raw), _sha(definition))
        if key == self._key and self._data is not None and self._info is not None:
            self.hits += 1
            return self._data, dict(self._info)

        self._data, self._info = prepare_data(
            raw, config, core_vars=core_vars, threshold=threshold,
            treatment=treatment, candidate_vars=candidate_vars)
        self._key = key
        self.misses += 1
        return self._data, dict(self._info)

    def clear(self) -> None:
        self._key = None
        self._data = None
        self._info = None


def select_estimation_sample(data: pd.DataFrame, needed: Iterable[str],
                             info: Mapping[str, Any] | None = None) -> tuple[pd.DataFrame, dict[str, Any]]:
    columns = _unique(needed)
    missing = [column for column in columns if column not in data]
    if missing:
        raise KeyError(f"估计变量不存在: {missing}")
    selected = data.dropna(subset=columns).copy()
    if selected.empty:
        raise ValueError("模型完整案例筛选后为空")
    positions = selected.index.tolist()
    sample = {"sample_identity": _sha({"data_sha256": (info or {}).get("source_data_sha256"),
                                        "row_positions_in_input": positions}),
              "estimation_sample_n": int(len(selected)),
              "complete_case_removed_n": int(len(data) - len(selected))}
    if info:
        trace = json.loads(info["processing_trace_json"])
        trace["complete_case_variables"] = columns
        trace["estimation_rows"] = sample["estimation_sample_n"]
        trace["complete_case_removed_rows"] = sample["complete_case_removed_n"]
        sample["processing_trace_json"] = _json(trace)
    return selected, sample


def save_processing_definitions(records: pd.DataFrame, output_dir: Path) -> None:
    """Deduplicate recipes into a stable JSON artifact for downstream codegen."""
    definitions: dict[str, Any] = {}
    if not records.empty and {"processing_definition_id", "processing_definition_json"} <= set(records.columns):
        for ident, payload in records[["processing_definition_id", "processing_definition_json"]].itertuples(index=False):
            if isinstance(ident, str) and isinstance(payload, str):
                definitions[ident] = json.loads(payload)
    artifact = {"schema_version": PROCESSING_VERSION, "definitions": definitions}
    (Path(output_dir) / "preprocessing_definitions.json").write_text(
        json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
