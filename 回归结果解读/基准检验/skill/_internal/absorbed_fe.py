"""Fast OLS with one or two categorical fixed effects absorbed by demeaning.

The within transformation implements the Frisch-Waugh-Lovell estimator while
avoiding a dense design matrix containing thousands of fixed-effect dummies.
When the transformed regressors are rank deficient or the projection fails to
converge, callers can fall back to their existing formula-based fit.
"""

from __future__ import annotations

from typing import Any, Sequence

import numpy as np
import pandas as pd
import statsmodels.api as sm
import statsmodels.formula.api as smf


def _group_codes(frame: pd.DataFrame, columns: Sequence[str]) -> list[tuple[np.ndarray, int]]:
    result = []
    for column in columns:
        codes, uniques = pd.factorize(frame[column], sort=False)
        if (codes < 0).any():
            raise ValueError(f"固定效应变量存在缺失值: {column}")
        result.append((codes.astype(np.intp, copy=False), len(uniques)))
    return result


def _demean(values: np.ndarray, codes: np.ndarray, group_count: int) -> np.ndarray:
    counts = np.bincount(codes, minlength=group_count).astype(float)
    transformed = np.empty_like(values, dtype=float)
    for column in range(values.shape[1]):
        sums = np.bincount(codes, weights=values[:, column], minlength=group_count)
        transformed[:, column] = values[:, column] - (sums / counts)[codes]
    return transformed


def _absorb(values: np.ndarray, groups: Sequence[tuple[np.ndarray, int]],
            *, tolerance: float = 1e-10, max_iterations: int = 10_000) -> np.ndarray | None:
    if not groups:
        return values.copy()
    if len(groups) == 1:
        return _demean(values, *groups[0])

    transformed = values.copy()
    scale = max(1.0, float(np.max(np.abs(transformed))))
    for _ in range(max_iterations):
        for codes, count in groups:
            transformed = _demean(transformed, codes, count)
        max_mean = 0.0
        for codes, count in groups:
            means = _demean(transformed, codes, count) - transformed
            max_mean = max(max_mean, float(np.max(np.abs(means))))
        if max_mean <= tolerance * scale:
            return transformed
    return None


def _fixed_effect_rank(groups: Sequence[tuple[np.ndarray, int]]) -> int:
    if len(groups) == 1:
        return groups[0][1]
    if len(groups) != 2:
        raise ValueError("只支持吸收一维或二维固定效应")

    first_codes, first_count = groups[0]
    second_codes, second_count = groups[1]
    parent = list(range(first_count + second_count))

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in zip(first_codes, second_codes):
        left_root = find(int(left))
        right_node = first_count + int(right)
        right_root = find(right_node)
        if left_root != right_root:
            parent[right_root] = left_root
    components = len({find(node) for node in range(first_count + second_count)})
    return first_count + second_count - components


def _formula_without_absorbed_terms(formula: str, fixed_effect_terms: Sequence[str]) -> str:
    lhs, rhs = formula.split("~", 1)
    rhs = rhs.rstrip()
    for term in reversed(fixed_effect_terms):
        suffix = " + " + term
        if rhs.endswith(suffix):
            rhs = rhs[:-len(suffix)]
        elif rhs.strip() == term:
            rhs = "1"
        else:
            raise ValueError(f"无法从公式末尾定位固定效应项: {term}")
    return f"{lhs.strip()} ~ {rhs.strip()}"


def fit_absorbed_ols(
    frame: pd.DataFrame,
    formula: str,
    *,
    fixed_effect_terms: Sequence[str],
    fixed_effect_columns: Sequence[str],
    cluster_column: str | None = None,
    eval_env: Any = None,
) -> Any | None:
    """Fit the formula after absorbing its trailing fixed-effect terms.

    Returns ``None`` when the transformed design is rank deficient or the
    alternating projection does not converge; callers should use the original
    formula fit in that case. The result retains statsmodels' familiar API and
    coefficient names.
    """
    if not fixed_effect_columns:
        return None
    if len(fixed_effect_columns) > 2:
        return None
    if any(column not in frame for column in fixed_effect_columns):
        raise KeyError(f"固定效应列不存在: {list(fixed_effect_columns)}")

    reduced_formula = _formula_without_absorbed_terms(formula, fixed_effect_terms)
    formula_model = (smf.ols(reduced_formula, data=frame) if eval_env is None else
                     smf.ols(reduced_formula, data=frame, eval_env=eval_env))
    names = list(formula_model.exog_names)
    endogenous = np.asarray(formula_model.endog, dtype=float).reshape(-1, 1)
    design = np.asarray(formula_model.exog, dtype=float)
    predictor_positions = [i for i, name in enumerate(names) if name != "Intercept"]
    predictor_names = [names[i] for i in predictor_positions]
    if not predictor_positions:
        return None

    groups = _group_codes(frame, fixed_effect_columns)
    transformed = _absorb(np.column_stack([endogenous, design[:, predictor_positions]]), groups)
    if transformed is None:
        return None
    y_within = transformed[:, 0]
    x_within = transformed[:, 1:]
    if np.linalg.matrix_rank(x_within) < x_within.shape[1]:
        return None

    fixed_rank = _fixed_effect_rank(groups)
    predictor_rank = int(np.linalg.matrix_rank(x_within))
    full_rank = fixed_rank + predictor_rank
    nobs = len(frame)
    residual_df = nobs - full_rank
    if residual_df <= 0:
        return None

    x_frame = pd.DataFrame(x_within, index=frame.index, columns=predictor_names)
    y_series = pd.Series(y_within, index=frame.index, name=formula_model.endog_names)
    absorbed_model = sm.OLS(y_series, x_frame, hasconst=False)
    # Use the degrees of freedom of the equivalent formula model, including
    # the fixed-effect rank, when computing classical covariance and p-values.
    absorbed_model.df_resid = residual_df

    if cluster_column:
        if cluster_column not in frame:
            raise KeyError(f"聚类列不存在: {cluster_column}")
        cluster_count = int(frame[cluster_column].nunique(dropna=True))
        if cluster_count < 2:
            raise ValueError("聚类组数量不足，无法使用 cluster robust SE")
        result = absorbed_model.fit(
            cov_type="cluster",
            cov_kwds={"groups": frame[cluster_column], "use_correction": False,
                      "df_correction": True})
        # Match statsmodels' small-sample correction for the full formula
        # design, whose columns include the absorbed categorical contrasts.
        effect_columns = sum(
            max(0, len(frame[column].cat.categories) - 1)
            if isinstance(frame[column].dtype, pd.CategoricalDtype)
            else max(0, int(frame[column].nunique(dropna=True)) - 1)
            for column in fixed_effect_columns)
        full_design_columns = design.shape[1] + effect_columns
        if nobs > full_design_columns:
            correction = (cluster_count / (cluster_count - 1)
                          * (nobs - 1) / (nobs - full_design_columns))
            result._results.cov_params_default *= correction
    else:
        result = absorbed_model.fit()

    sse = float(np.dot(result.resid, result.resid))
    centered_tss = float(np.sum((endogenous[:, 0] - endogenous[:, 0].mean()) ** 2))
    r_squared = 1.0 - sse / centered_tss if centered_tss > 0 else np.nan
    adjusted_r_squared = (1.0 - (1.0 - r_squared) * (nobs - 1) / residual_df
                          if np.isfinite(r_squared) else np.nan)
    result._results.df_resid = residual_df
    result._results.df_model = max(0, full_rank - 1)
    # Statsmodels exposes these through cached properties, so seed its cache
    # directly with the equivalent full-formula statistics.
    result._results._cache.update({
        "ssr": sse, "centered_tss": centered_tss,
        "rsquared": r_squared, "rsquared_adj": adjusted_r_squared,
    })
    return result
