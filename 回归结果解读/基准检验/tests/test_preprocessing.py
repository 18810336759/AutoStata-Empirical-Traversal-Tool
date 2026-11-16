"""Behavior checks for the shared preprocessing contract."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "skill"))
sys.path.insert(0, str(BASE / "skill" / "other"))

from config_validation import load_config_file  # noqa: E402
from preprocessing import PreparedDataCache, prepare_data, select_estimation_sample, save_processing_definitions  # noqa: E402
from mediation_effect.run_mediation_effect import build_task_level_model_data, get_fe_settings  # noqa: E402


def config():
    cfg = load_config_file(BASE / "config" / "analysis_config.yaml")
    cfg["data"]["panel"] = {"entity_name": "firm", "time_name": "year"}
    cfg["continuous_variable_settings"] = {
        "continuous_variables": "x y m", "interpolate_allowed": "x y m",
        "winsorize_allowed": "x y m", "exclude_variables": "firm year industry"}
    cfg["model_settings"]["fixed_effects"] = {
        "entity_fixed_effect": False, "entity_name": "firm",
        "time_fixed_effect": False, "time_name": "year"}
    cfg["model_settings"]["clustered_standard_errors"] = {"enabled": False, "cluster_level": ""}
    return cfg


class SharedPreprocessingTests(unittest.TestCase):
    def test_preprocessing_cache_reuses_recipe_and_returns_independent_info(self):
        cfg = config()
        raw = pd.DataFrame({"firm": ["A", "A", "B", "B"],
                            "year": [2020, 2021, 2020, 2021],
                            "x": [1., np.nan, 5., 7.]})
        cache = PreparedDataCache()
        kwargs = dict(core_vars=["firm"], threshold=1,
                      treatment="interpolate_controls", candidate_vars=["x"])

        first, first_info = cache.get_or_prepare(raw, cfg, **kwargs)
        first_copy = first.copy(deep=True)
        first_copy.loc[:, "x"] = -1
        first_info["临时字段"] = "只属于第一次调用"
        second, second_info = cache.get_or_prepare(raw, cfg, **kwargs)

        self.assertIs(first, second)
        self.assertEqual(cache.misses, 1)
        self.assertEqual(cache.hits, 1)
        self.assertNotIn("临时字段", second_info)
        self.assertEqual(second.loc[1, "x"], 1.)

        cache.get_or_prepare(raw, cfg, core_vars=["firm"], threshold=1,
                             treatment="interpolate_controls", candidate_vars=["x", "firm"])
        self.assertEqual(cache.misses, 2)

    def test_shared_preprocessing_keeps_model_specific_complete_case_samples(self):
        cfg = config()
        raw = pd.DataFrame({"firm": ["A"] * 4, "year": [2020, 2021, 2022, 2023],
                            "x": [1., 2., 3., 4.], "y": [2., 3., 4., 5.],
                            "m": [3., 4., 5., 6.], "control_a": [np.nan, 1., 1., 1.],
                            "control_b": [1., np.nan, 1., 1.]})
        cache = PreparedDataCache()
        prepared, info = cache.get_or_prepare(
            raw, cfg, core_vars=["x", "y", "m"], threshold=1,
            treatment="baseline_required_controls",
            candidate_vars=["x", "y", "m", "control_a", "control_b"])

        sample_a, info_a = select_estimation_sample(
            prepared.copy(deep=True), ["x", "y", "m", "control_a"], info)
        sample_b, info_b = select_estimation_sample(
            prepared.copy(deep=True), ["x", "y", "m", "control_b"], info)

        self.assertEqual(len(sample_a), 3)
        self.assertEqual(len(sample_b), 3)
        self.assertNotEqual(info_a["sample_identity"], info_b["sample_identity"])
        self.assertEqual(len(prepared), 4)

    def test_interpolation_widens_float32_without_future_warning(self):
        import warnings

        cfg = config()
        raw = pd.DataFrame({"firm": ["A", "A", "A"], "year": [2018, 2019, 2020],
                            "x": pd.Series([1.0, np.nan, 2.5], dtype="float32")})
        with warnings.catch_warnings():
            warnings.simplefilter("error", FutureWarning)
            result, _ = prepare_data(raw, cfg, core_vars=["firm"], threshold=2,
                                     treatment="interpolate_controls", candidate_vars=["x"])
        self.assertEqual(result["x"].dtype, np.dtype("float64"))
        self.assertAlmostEqual(result["x"].iloc[1], 1.75)

    def test_real_year_distance_and_no_cross_firm_interpolation(self):
        cfg = config()
        raw = pd.DataFrame({"firm": ["A"] * 3 + ["B"] * 3,
                            "year": [2018, 2019, 2021] * 2,
                            "x": [1., np.nan, 4., 100., np.nan, 130.]})
        frame, info = prepare_data(raw, cfg, core_vars=["firm"], threshold=2,
                                   treatment="interpolate_controls", candidate_vars=["x"])
        self.assertEqual(frame["x"].iloc[1], 2.)
        self.assertEqual(frame["x"].iloc[4], 110.)
        self.assertEqual(json.loads(info["processing_trace_json"])["interpolated_cells"]["x"], 2)

        cfg["preprocessing"]["interpolation"]["max_gap"] = 1
        limited, _ = prepare_data(raw, cfg, core_vars=["firm"], threshold=2,
                                  treatment="interpolate_controls", candidate_vars=["x"])
        self.assertTrue(pd.isna(limited["x"].iloc[1]))
        self.assertTrue(pd.isna(limited["x"].iloc[4]))

    def test_boundary_direction_and_calendar_gap(self):
        cfg = config()
        raw = pd.DataFrame({"firm": ["A"] * 4, "year": [2017, 2018, 2020, 2021],
                            "x": [np.nan, 3., 7., np.nan]})
        for direction, expected in [("both", (3., 7.)), ("forward", (np.nan, 7.)),
                                    ("backward", (3., np.nan))]:
            cfg["preprocessing"]["interpolation"]["limit_direction"] = direction
            frame, _ = prepare_data(raw, cfg, core_vars=["firm"], threshold=2,
                                    treatment="interpolate_controls", candidate_vars=["x"])
            for actual, value in zip((frame["x"].iloc[0], frame["x"].iloc[-1]), expected):
                self.assertEqual(pd.isna(actual), pd.isna(value))
                if not pd.isna(value):
                    self.assertEqual(actual, value)

    def test_interpolation_rejects_ambiguous_or_fractional_firm_year(self):
        cfg = config()
        for years in ([2020, 2020, 2021], [2020, 2020.5, 2021]):
            raw = pd.DataFrame({"firm": ["A"] * 3, "year": years,
                                "x": [1., np.nan, 3.]})
            with self.assertRaises(ValueError):
                prepare_data(raw, cfg, core_vars=["firm"], threshold=1,
                             treatment="interpolate_controls", candidate_vars=["x"])

    def test_all_four_winsor_scopes_and_json_recipe(self):
        cfg = config()
        raw = pd.DataFrame({"firm": list("AAAABBBB"), "year": [2020] * 2 + [2021] * 2 + [2020] * 2 + [2021] * 2,
                            "industry": ["I"] * 4 + ["J"] * 4,
                            "x": [0., 10., 20., 30., 100., 110., 120., 1000.]})
        cfg["preprocessing"]["winsorize"].update(lower_quantile=.25, upper_quantile=.50,
                                                      industry_name="industry")
        values = {}
        for scope in ("full_sample", "by_year", "by_industry", "by_industry_year"):
            cfg["preprocessing"]["winsorize"]["scope"] = scope
            frame, info = prepare_data(raw, cfg, core_vars=["x"], threshold=1,
                                       treatment="winsorize_controls", candidate_vars=["x"])
            grouping = {"full_sample": [], "by_year": ["year"],
                        "by_industry": ["industry"],
                        "by_industry_year": ["industry", "year"]}[scope]
            if grouping:
                expected = raw.groupby(grouping)["x"].transform(
                    lambda series: series.clip(series.quantile(.25), series.quantile(.50)))
            else:
                expected = raw["x"].clip(raw["x"].quantile(.25), raw["x"].quantile(.50))
            pd.testing.assert_series_equal(frame["x"].reset_index(drop=True),
                                           expected.reset_index(drop=True), check_names=False)
            values[scope] = frame.loc[7, "x"]
            definition = json.loads(info["processing_definition_json"])
            self.assertEqual(definition["winsorize"]["scope"], scope)
            self.assertEqual(definition["winsorize"]["upper_quantile"], .50)
        self.assertEqual(len(set(values.values())), 4)
        with tempfile.TemporaryDirectory() as temp:
            save_processing_definitions(pd.DataFrame([info]), Path(temp))
            artifact = json.loads((Path(temp) / "preprocessing_definitions.json").read_text(encoding="utf-8"))
            self.assertIn(info["processing_definition_id"], artifact["definitions"])

    def test_winsor_missing_group_key_keeps_original_value(self):
        cfg = config()
        cfg["preprocessing"]["winsorize"].update(
            scope="by_industry", industry_name="industry",
            lower_quantile=.25, upper_quantile=.75)
        raw = pd.DataFrame({"firm": list("ABCD"), "year": [2020] * 4,
                            "industry": ["I", "I", "I", None],
                            "x": [1., 2., 100., 999.]})
        frame, info = prepare_data(raw, cfg, core_vars=["x"], threshold=1,
                                   treatment="winsorize_controls", candidate_vars=["x"])
        self.assertEqual(frame.loc[3, "x"], 999.)
        self.assertEqual(json.loads(info["processing_definition_json"])
                         ["winsorize"]["missing_group_keys"],
                         "leave_original_value_unchanged")

    def test_mediation_paths_have_independent_eligibility_and_samples(self):
        cfg = config()
        raw = pd.DataFrame({"firm": ["A"] * 4 + ["B"] * 4,
                            "year": [2018, 2019, 2020, 2021] * 2,
                            "x": [1., 2., 3., 4., 5., 6., 7., 8.],
                            "y": [1., 2., np.nan, 4., 5., 6., np.nan, 8.],
                            "m": [1., np.nan, 3., 4., 5., 6., 7., 8.]})
        samples = {}
        for path, needed in [("x_to_y", ["x", "y"]), ("x_to_m", ["x", "m"]),
                             ("x_m_to_y", ["x", "m", "y"])]:
            frame, info = build_task_level_model_data(
                raw, "y", "x", "m", [], [], 2, "baseline_required_controls",
                {}, get_fe_settings(cfg), cfg, path)
            selected, sample = select_estimation_sample(frame, needed, info)
            samples[path] = (len(selected), sample["sample_identity"])
        self.assertEqual([samples[p][0] for p in ("x_to_y", "x_to_m", "x_m_to_y")], [6, 7, 5])
        self.assertEqual(len({value[1] for value in samples.values()}), 3)

    def test_sample_identity_includes_source_data_fingerprint(self):
        cfg = config()
        raw = pd.DataFrame({"firm": ["A", "A"], "year": [2020, 2021], "x": [1., 2.]})
        identities = []
        for digest in ("data-version-one", "data-version-two"):
            cfg["_run_provenance"] = {"data_sha256": digest}
            frame, info = prepare_data(raw, cfg, core_vars=["x"], threshold=1,
                                       treatment="baseline_required_controls", candidate_vars=["x"])
            _, sample = select_estimation_sample(frame, ["x"], info)
            identities.append(sample["sample_identity"])
        self.assertNotEqual(*identities)


if __name__ == "__main__":
    unittest.main()
