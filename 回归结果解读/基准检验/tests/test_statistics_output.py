"""Contract checks for statistics and machine-readable effect outputs."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "skill"))
sys.path.insert(0, str(BASE / "skill" / "other"))

from _internal.statistics_output import (  # noqa: E402
    regression_statistics, output_policy, write_result_table, _within_baseline_sse,
)
from result_schema import STANDARD_COLUMNS, align_standard_columns, build_specification_index  # noqa: E402
from main_effect.run_main_effect import build_standardized_results as main_standard  # noqa: E402


class StatisticsOutputTests(unittest.TestCase):
    def test_sse_handles_nonfinite_and_overflow_without_matmul(self):
        import warnings
        frame = pd.DataFrame({"entity": [1, 1, 2, 2], "y": [1., 3., 5., 9.]})
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            self.assertAlmostEqual(_within_baseline_sse(frame, "y", ["entity"]), 10.)
            frame["y"] = [1., np.inf, 5., 9.]
            self.assertIsNone(_within_baseline_sse(frame, "y", ["entity"]))
            frame["y"] = [1e200, -1e200, 1e200, -1e200]
            self.assertIsNone(_within_baseline_sse(frame, "y", ["entity"]))

    def test_within_r2_uses_fe_only_baseline_on_estimation_rows(self):
        rng = np.random.default_rng(12)
        entity = np.repeat(np.arange(12), 5)
        year = np.tile(np.arange(2018, 2023), 12)
        x = rng.normal(size=60)
        y = 2 * entity + .5 * year + 1.3 * x + rng.normal(scale=.3, size=60)
        frame = pd.DataFrame({"entity": entity, "year": year, "x": x, "y": y})
        fitted = smf.ols("y ~ x + C(entity) + C(year)", data=frame).fit()
        stats = regression_statistics(fitted, frame, "y", {
            "entity_fixed_effect": True, "entity_name": "entity",
            "time_fixed_effect": True, "time_name": "year"})
        fe_only = smf.ols("y ~ C(entity) + C(year)", data=frame).fit()
        expected = 1 - fitted.ssr / fe_only.ssr
        self.assertAlmostEqual(stats["within R²"], expected, places=8)
        self.assertAlmostEqual(stats["普通 R²"], fitted.rsquared)
        self.assertAlmostEqual(stats["调整 R²"], fitted.rsquared_adj)
        self.assertEqual(stats["组内 R²吸收项"], "entity;year")
        no_fe = regression_statistics(smf.ols("y ~ x", data=frame).fit(), frame, "y", {
            "entity_fixed_effect": False, "time_fixed_effect": False})
        self.assertIsNone(no_fe["within R²"])
        self.assertIsNone(no_fe["组内 R²定义"])

    def test_failed_model_and_empty_schema_are_stable(self):
        failed = pd.DataFrame([{"model_id": "bad", "检验项组合": None,
                                "是否成功运行": "failed", "失败原因": "missing column",
                                "模型公式": "y ~ x", "模型项定义": '{}',
                                "curvature_diagnostic": "not_tested"}])
        standardized = main_standard(failed, {"data": {}}, {"nonlinear_settings": {}})
        self.assertEqual(len(standardized), 1)
        self.assertEqual(standardized.loc[0, "是否成功运行"], "failed")
        self.assertTrue(pd.isna(standardized.loc[0, "检验项"]))
        self.assertEqual(standardized.loc[0, "模型公式"], "y ~ x")
        self.assertEqual(standardized.loc[0, "curvature_diagnostic"], "not_tested")
        empty = align_standard_columns(pd.DataFrame())
        self.assertEqual(empty.columns[:len(STANDARD_COLUMNS)].tolist(), STANDARD_COLUMNS)
        index = build_specification_index(empty)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary)
            write_result_table(empty, path, "standardized_results", csv=True, excel=False, machine=True)
            write_result_table(index, path, "specification_index", csv=True, excel=False)
            self.assertEqual(pd.read_csv(path / "standardized_results.csv").columns[:len(STANDARD_COLUMNS)].tolist(),
                             STANDARD_COLUMNS)
            self.assertEqual(len(pd.read_csv(path / "specification_index.csv")), 0)

    def test_machine_csv_independent_of_human_csv(self):
        settings = {"output_settings": {"save_csv_results": False,
                                        "save_excel_results": False,
                                        "standardized_output": {"enabled": True,
                                            "save_standardized_results": True,
                                            "save_specification_index": True}}}
        policy = output_policy(settings, {"standardized_output": {}})
        self.assertFalse(policy.human_csv)
        self.assertTrue(policy.machine)
        self.assertTrue(policy.specification_index)
        settings["output_settings"]["standardized_output"]["save_standardized_results"] = False
        self.assertFalse(output_policy(settings, {"standardized_output": {}}).machine)


if __name__ == "__main__":
    unittest.main()
