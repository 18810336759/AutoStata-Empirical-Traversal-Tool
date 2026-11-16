"""The mediation decision must use complete paths from one specification."""

from __future__ import annotations

import sys
import json
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "skill"))
sys.path.insert(0, str(BASE / "skill" / "other"))

from _internal.hypothesis_output import assess_mediation_chain, single_model_hypotheses  # noqa: E402
import mediation_effect.run_mediation_effect as mediation  # noqa: E402


def settings() -> dict:
    return {"candidate_rules": {"p_value_threshold": .05,
                                "require_direction_match": True,
                                "minimum_sample_size_required": True,
                                "x_weakened_after_m_required": False,
                                "three_step_requirements": {"require_x_to_y": True,
                                                            "require_x_to_m": True,
                                                            "require_m_to_y": True},
                                "two_step_requirements": {"require_x_to_y": False,
                                                          "require_x_to_m": True,
                                                          "require_m_to_y": True}},
            "combination_screening": {"result_labels": {"strong_pass": .05, "weak_pass": .10},
                                      "apply_to_paths": ["x_to_m", "x_m_to_y"],
                                      "enabled": True}}


def paths() -> list[dict]:
    return [{"path_type": name, "model_id": name, "specification_id": "same",
             "chain_id": "M1", "是否成功运行": "success", "p": .01,
             "direction_match": True, "样本量（N）": n, "sample_identity": identity,
             "x_weakened_after_m": True}
            for name, n, identity in (("x_to_y", 60, "s1"),
                                      ("x_to_m", 57, "s2"),
                                      ("x_m_to_y", 55, "s3"))]


class HypothesisOutputTests(unittest.TestCase):
    def test_main_and_moderation_explain_model_level_decisions(self):
        main = pd.DataFrame([{"model_id": "main_1", "specification_id": "spec_1",
                              "假设编号": "H1", "是否成功运行": "success",
                              "检验项组合": "x", "beta_x": .4, "p_x": .02,
                              "direction_match_x": True, "通过规则": "all_required_terms",
                              "strong_pass": True, "weak_pass": True}])
        main_config = {"hypotheses": [{"id": "H1", "x": [{"name": "x", "required": True}]}],
                       "main_effect_settings": {"candidate_rules": {"p_value_threshold": .05},
                           "combination_screening": {"result_labels": {"strong_pass": .05,
                                                                           "weak_pass": .10}}}}
        main_result = single_model_hypotheses(main, "main_effect", main_config).iloc[0]
        self.assertTrue(main_result["strong_pass"])
        self.assertEqual(json.loads(main_result["pass_basis_json"])["term_evidence"]["x"]["p"], .02)
        moderation = pd.DataFrame([{"model_id": "mod_1", "specification_id": "spec_1",
                                    "假设编号": "W1", "是否成功运行": "success",
                                    "检验阶段": "C完整检验", "检验项": "x_w",
                                    "beta": .2, "p_interaction": .01,
                                    "direction_match": True, "strong_pass": True,
                                    "weak_pass": True, "turning_point_check": False}])
        mod_result = single_model_hypotheses(moderation, "moderation_effect", {
            "moderation_settings": {"candidate_rules": {"p_value_threshold": .2},
                                    "combination_screening": {"result_labels": {
                                        "strong_pass": .05, "weak_pass": .1}}}}).iloc[0]
        self.assertTrue(mod_result["strong_pass"])
        self.assertFalse(json.loads(mod_result["pass_basis_json"])["term_evidence"]["x_w"]["turning_point_check"])

    def test_strong_is_always_included_in_weak_and_ab_never_passes(self):
        frame = pd.DataFrame([
            {"model_id": "ab", "specification_id": "s", "假设编号": "H1",
             "检验阶段": "AB", "是否成功运行": "success", "strong_pass": True,
             "weak_pass": True},
            {"model_id": "c", "specification_id": "s", "假设编号": "H1",
             "检验阶段": "C", "是否成功运行": "success", "strong_pass": True,
             "weak_pass": False},
        ])
        result = single_model_hypotheses(frame, "main_effect", {"hypotheses": []})
        self.assertFalse(bool(result.iloc[0]["strong_pass"]))
        self.assertFalse(bool(result.iloc[0]["weak_pass"]))
        self.assertTrue(bool(result.iloc[1]["strong_pass"]))
        self.assertTrue(bool(result.iloc[1]["weak_pass"]))

    def test_three_step_requires_same_spec_total_effect_but_allows_different_samples(self):
        chain = {"chain_id": "M1", "screening_method": "three_step",
                 "pass_rule": "all_required_paths"}
        result = assess_mediation_chain(paths(), chain, settings(), 30, "C")
        self.assertTrue(result["strong_pass"])
        self.assertFalse(result["path_sample_identity_same"])
        self.assertFalse(result["path_n_same"])
        self.assertIn("不影响链级通过判定", result["sample_difference_note"])

        missing_total = assess_mediation_chain(paths()[1:], chain, settings(), 30, "C")
        self.assertFalse(missing_total["strong_pass"])
        self.assertEqual(missing_total["missing_paths"], "x_to_y")

        other_spec = paths()
        other_spec[0]["specification_id"] = "different_controls"
        mixed = assess_mediation_chain(other_spec, chain, settings(), 30, "C")
        self.assertFalse(mixed["strong_pass"])
        self.assertIn("公共设定", mixed["判定依据"])
        partial = paths()
        partial[1]["p"] = .30
        only_two_paths_pass = assess_mediation_chain(partial, chain, settings(), 30, "C")
        self.assertFalse(only_two_paths_pass["strong_pass"])
        self.assertFalse(only_two_paths_pass["weak_pass"])
        self.assertEqual(only_two_paths_pass["present_paths"], "x_to_y x_to_m x_m_to_y")
        samples = json.loads(only_two_paths_pass["path_samples_json"])
        self.assertEqual({path: item["n"] for path, item in samples.items()},
                         {"x_to_y": 60, "x_to_m": 57, "x_m_to_y": 55})

    def test_two_step_and_configured_path_conditions(self):
        chain = {"chain_id": "M1", "screening_method": "two_step",
                 "pass_rule": "all_required_paths"}
        self.assertTrue(assess_mediation_chain(paths()[1:], chain, settings(), 30, "C")["strong_pass"])
        strict = settings()
        strict["candidate_rules"]["x_weakened_after_m_required"] = True
        records = paths()
        records[2]["x_weakened_after_m"] = False
        self.assertFalse(assess_mediation_chain(records, chain, strict, 30, "C")["strong_pass"])
        records[2]["x_weakened_after_m"] = True
        records[1]["p"] = .07
        weak_only = assess_mediation_chain(records, chain, strict, 30, "C")
        self.assertFalse(weak_only["strong_pass"])
        self.assertTrue(weak_only["weak_pass"])

    def test_stage_c_runs_total_effect_for_same_control_combo(self):
        chain = {"chain_id": "M1", "label": "M1", "screening_method": "three_step",
                 "pass_rule": "all_required_paths",
                 "path_directions": {"x_to_y": "positive", "x_to_m": "positive", "m_to_y": "positive"}}
        candidate = pd.DataFrame([{"假设编号": "M1", "假设描述": "mechanism",
                                   "被解释变量": "y", "解释变量": "x", "中介变量": "m",
                                   "chain_id": "M1", "样本门槛": 2, "处理方案": "raw"}])
        controls = {"combination_rules": {"enabled": True,
                                            "min_optional_controls_in_combination": 1,
                                            "max_optional_controls_in_combination": 1},
                    "combination_execution": {"mode": "full_enumeration", "top_k_limit": None},
                    "required_controls": ["c"], "optional_controls": ["z"]}
        search = {"enabled": True, "max_candidates_to_expand": 1,
                  "random_seed": 1, "layer_sampling": {"random_seeds_per_size": 1},
                  "retention_rules": {"force_keep_full_model": False}}
        calls = []

        def fake_path(_raw, _config, **kwargs):
            calls.append((kwargs["path_type"], tuple(kwargs["optional_controls"]),
                          kwargs.get("x_to_y_coef_x")))
            return {"path_type": kwargs["path_type"], "model_id": kwargs["path_type"],
                    "是否成功运行": "success", "p": .01, "direction_match": True,
                    "beta_x": 0.8, "样本量（N）": 50}

        with patch.object(mediation, "run_preprocessed_path", side_effect=fake_path):
            result = mediation.run_control_combination_stage(
                config={}, df_raw=pd.DataFrame(), chains=[chain], candidate_df=candidate,
                continuous_settings={}, controls_cfg=controls,
                control_search_settings=search, fe_settings={}, mediation_settings=settings())
        self.assertEqual(result["path_type"].tolist(), ["x_to_y", "x_to_m", "x_m_to_y"])
        self.assertTrue(all(combo == ("z",) for _, combo, _ in calls))
        self.assertEqual(calls[2][2], .8)


if __name__ == "__main__":
    unittest.main()
