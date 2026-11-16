"""Shared regression statistics, model-term provenance, and export switches."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd


WITHIN_R2_DEFINITION = "1 - SSE(完整模型)/SSE(仅含截距和已控制固定效应的模型)"
EMPTY_RESULT_COLUMNS = ["model_id", "模块名称", "假设编号", "模型公式", "模型项定义",
                        "普通 R²", "调整 R²", "within R²", "样本量（N）",
                        "是否成功运行", "失败原因"]


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if np.isfinite(number) else None


def _within_baseline_sse(frame: pd.DataFrame, outcome: str,
                         absorbed: list[str]) -> float | None:
    if not absorbed or outcome not in frame or any(name not in frame for name in absorbed):
        return None
    work = frame[[outcome] + absorbed].dropna()
    if work.empty or len(work) != len(frame):
        return None
    y = pd.to_numeric(work[outcome], errors="coerce")
    if not np.isfinite(y.to_numpy(dtype=float)).all():
        return None
    residual = y.to_numpy(dtype=float, copy=True)
    residual -= residual.mean()
    scale = max(1.0, float(np.max(np.abs(residual))))
    if len(absorbed) == 1:
        residual -= pd.Series(residual, index=work.index).groupby(
            work[absorbed[0]], observed=True).transform("mean").to_numpy()
    else:
        # Alternating orthogonal projections absorb two unbalanced panel effects.
        # Stop only when both group means are negligible; otherwise leave R² blank.
        converged = False
        for _ in range(10000):
            for column in absorbed:
                residual -= pd.Series(residual, index=work.index).groupby(
                    work[column], observed=True).transform("mean").to_numpy()
            max_mean = max(float(pd.Series(residual, index=work.index).groupby(
                work[column], observed=True).mean().abs().max()) for column in absorbed)
            if max_mean <= 1e-10 * scale:
                converged = True
                break
        if not converged:
            return None
    # Avoid the BLAS matmul path for this scalar sum of squares. Scale first
    # so intermediate squares cannot overflow; unavailable SSE stays blank.
    if not np.isfinite(residual).all():
        return None
    magnitude = float(np.max(np.abs(residual)))
    if magnitude == 0:
        return 0.0
    normalized = residual / magnitude
    squared_sum = math.fsum(float(value) ** 2 for value in normalized)
    if magnitude > math.sqrt(np.finfo(float).max / squared_sum):
        return None
    return (squared_sum * magnitude) * magnitude


def regression_statistics(result: Any, frame: pd.DataFrame, outcome: str,
                          fe_settings: Mapping[str, Any]) -> dict[str, Any]:
    """Within R² uses an FE-only null on exactly the fitted observation rows."""
    absorbed = []
    if fe_settings.get("entity_fixed_effect"):
        absorbed.append(str(fe_settings["entity_name"]))
    if fe_settings.get("time_fixed_effect"):
        absorbed.append(str(fe_settings["time_name"]))
    ordinary = _finite(getattr(result, "rsquared", None))
    adjusted = _finite(getattr(result, "rsquared_adj", None))
    within = None
    if absorbed:
        baseline_sse = _within_baseline_sse(frame, outcome, absorbed)
        full_sse = _finite(getattr(result, "ssr", None))
        if baseline_sse is not None and full_sse is not None and baseline_sse > 1e-12:
            value = 1.0 - full_sse / baseline_sse
            if -1e-8 <= value <= 1.0 + 1e-8:
                within = float(np.clip(value, 0.0, 1.0))
    return {"普通 R²": ordinary, "调整 R²": adjusted, "within R²": within,
            "组内 R²定义": WITHIN_R2_DEFINITION if absorbed else None,
            "组内 R²吸收项": ";".join(absorbed) if absorbed else None}


def model_terms_definition(*, outcome: str, focal_terms: Iterable[str],
                           controls: Iterable[str], fe_settings: Mapping[str, Any],
                           cluster: str | None, generated_terms: Mapping[str, str] | None = None,
                           formula: str | None = None) -> str:
    """JSON remains visible in tables and is directly consumable by code generation."""
    block = {"schema_version": "2.0-model-terms-1", "outcome": outcome,
             "focal_terms": list(dict.fromkeys(focal_terms)),
             "controls": list(dict.fromkeys(controls)),
             "fixed_effects": {
                 "entity": fe_settings.get("entity_name") if fe_settings.get("entity_fixed_effect") else None,
                 "time": fe_settings.get("time_name") if fe_settings.get("time_fixed_effect") else None},
             "cluster": cluster or None,
             "generated_terms": dict(generated_terms or {}), "formula": formula}
    return json.dumps(block, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class OutputPolicy:
    human_csv: bool
    human_excel: bool
    raw: bool
    success: bool
    summary: bool
    machine: bool
    specification_index: bool


def output_policy(config: Mapping[str, Any], module_config: Mapping[str, Any]) -> OutputPolicy:
    settings = config.get("output_settings", {})
    standard = settings.get("standardized_output", {})
    module = module_config.get("standardized_output", {})
    machine = bool(standard.get("enabled", True) and standard.get("save_standardized_results", True)
                   and module.get("effect_terms_long_format", True)
                   and module.get("unified_schema", {}).get("enabled", True))
    return OutputPolicy(
        human_csv=bool(settings.get("save_csv_results", True)),
        human_excel=bool(settings.get("save_excel_results", True)),
        raw=bool(settings.get("save_raw_results", True) and module.get("keep_wide_raw_results", True)),
        success=bool(settings.get("save_success_results", True)),
        summary=bool(settings.get("save_summary_md", True)),
        machine=machine,
        specification_index=bool(machine and standard.get("save_specification_index", True)),
    )


def apply_standardized_options(frame: pd.DataFrame, config: Mapping[str, Any]) -> pd.DataFrame:
    """Keep the machine schema stable while honoring optional path visibility."""
    result = frame.copy()
    standard = config.get("output_settings", {}).get("standardized_output", {})
    if not standard.get("include_config_paths", True):
        for column in ("global_config_path", "module_config_path"):
            if column in result:
                result[column] = None
    return result


def write_result_table(frame: pd.DataFrame, directory: Path, stem: str, *,
                       csv: bool, excel: bool, machine: bool = False) -> None:
    """Machine CSV is independent of the ordinary CSV switch."""
    from result_schema import align_standard_columns

    data = align_standard_columns(frame) if machine else frame.copy()
    if data.empty and not len(data.columns):
        data = pd.DataFrame(columns=EMPTY_RESULT_COLUMNS)
    if csv:
        data.to_csv(directory / f"{stem}.csv", index=False, encoding="utf-8-sig")
    if excel:
        data.to_excel(directory / f"{stem}.xlsx", index=False)
