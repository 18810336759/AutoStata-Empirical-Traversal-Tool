"""Display admission must follow captured hypotheses and verifiable model identities."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "skill"))
sys.path.insert(0, str(BASE / "skill" / "other"))
from result_synthesis.run_result_synthesis import (  # noqa: E402
    build_verified_display_cases, save_display_cases)


class DisplayCaseTests(unittest.TestCase):
    def setUp(self):
        self.main = {"hypotheses": [{"id": "H1", "enabled": True, "y": "y",
            "x": [{"name": "x", "required": True}], "model_form": "linear"}]}
        self.context = {"main_effect": {"module_config": self.main,
            "run_record": {"run_instance_id": "one", "snapshots": {
                "analysis_config.yaml": {"path": "config_snapshot/analysis_config.yaml", "sha256": "a"},
                "main_effect.yaml": {"path": "config_snapshot/main_effect.yaml", "sha256": "b"}}},
            "output_dir": "/tmp/main_effect"}}
        self.global_config = {"run_settings": {"enabled_modules": {"main_effect": True}}}
        self.config = {"display_case_selection": {"enabled": True, "pass_level": "strong",
            "required_modules_mode": "theory_chain", "require_theory_chain": True,
            "minimum_covered_modules": 1},
            "hypothesis_registry": {"expected_hypotheses": [{"id": "H1", "modules": ["main_effect"]}]},
            "theory_chains": {"enabled": True, "chains": [{"chain_id": "C1", "enabled": True,
                "required_hypotheses": ["H1"], "required_modules": ["main_effect"],
                "x": ["x"], "y": ["y"]}]},
            "merge_keys": ["样本门槛", "处理方案"]}
        self.row = {"spec_group_id": "SPEC_001", "source_module": "main_effect",
            "model_id": "m1", "假设编号": "H1", "解释变量": "x", "被解释变量": "y",
            "核心解释变量组合": "x", "模型形式": "linear", "模型公式": "y ~ x",
            "模型项定义": "{}", "sample_identity": "sample-a", "样本量（N）": 20,
            "样本门槛": 2, "处理方案": "raw", "run_id": "batch-1",
            "common_fingerprint": "common-1", "data_sha256": "data-1",
            "specification_id": "spec-1", "merge_key_complete": True,
            "provenance_verified": True, "source_file": "/tmp/main_effect/standardized_results.csv",
            "source_record_sha256": "record-a"}

    def build(self, row=None):
        frame = pd.DataFrame([row or self.row])
        return build_verified_display_cases({"strong": frame}, self.global_config,
                                            self.config, self.context)

    def test_case_contains_exact_model_and_snapshot_reference(self):
        cases, rejected = self.build()
        self.assertEqual(len(cases), 1)
        self.assertTrue(rejected.empty)
        case = cases.iloc[0]
        self.assertEqual(json.loads(case["selected_models_json"])[0]["model_id"], "m1")
        self.assertEqual(json.loads(case["source_snapshots_json"])["main_effect"]["run_instance_id"], "one")
        with tempfile.TemporaryDirectory() as directory:
            save_display_cases(cases, Path(directory), {**self.config,
                "dofile_bridge": {"enabled": True, "generate_dofile_in_this_version": False}}, rejected)
            manifest = json.loads((Path(directory) / "display_case_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["statistics_scope"], "executed_specifications")
            self.assertEqual(manifest["cases"][0]["selected_models"][0]["model_id"], "m1")
            self.assertFalse(list(Path(directory).glob("*.do")))

    def test_missing_merge_key_never_becomes_display_case(self):
        cases, rejected = self.build({**self.row, "merge_key_complete": False})
        self.assertTrue(cases.empty)
        self.assertIn("合并键", rejected.iloc[0]["reason"])

    def test_wrong_model_form_or_theory_variable_is_rejected(self):
        for change in ({"模型形式": "u_shape"}, {"解释变量": "other"}):
            cases, rejected = self.build({**self.row, **change})
            self.assertTrue(cases.empty)
            self.assertIn("不匹配", rejected.iloc[0]["reason"])

    def test_sample_identity_does_not_have_to_match_between_models(self):
        second = {**self.row, "model_id": "m2", "sample_identity": "sample-b", "样本量（N）": 18}
        frame = pd.DataFrame([self.row, second])
        cases, _ = build_verified_display_cases({"strong": frame}, self.global_config,
                                                self.config, self.context)
        self.assertEqual(len(cases), 1)
        self.assertFalse(json.loads(cases.iloc[0]["sample_differences_json"])["same_sample"])

    def test_optional_main_covariate_does_not_change_theory_focal_x(self):
        self.main["hypotheses"][0]["x"].append({"name": "z", "required": False})
        first = {**self.row, "核心解释变量组合": "x z", "检验项": "x"}
        second = {**first, "解释变量": "z", "检验项": "z"}
        cases, rejected = build_verified_display_cases(
            {"strong": pd.DataFrame([first, second])}, self.global_config,
            self.config, self.context)
        self.assertEqual(len(cases), 1)
        self.assertTrue(rejected.empty)
        models = json.loads(cases.iloc[0]["selected_models_json"])
        self.assertEqual(len(models), 1)
        self.assertEqual({item["term"] for item in models[0]["tested_terms"]}, {"x", "z"})

    def test_incomplete_mediation_chain_is_not_displayed(self):
        self.context["mediation_effect"] = {"module_config": {"mechanism_paths": [{
            "id": "M1", "enabled": True, "x": "x", "y": "y", "mediators": ["m"],
            "screening_method": "three_step", "pass_rule": "all_required_paths"}]},
            "run_record": {"run_instance_id": "two", "snapshots": {}},
            "output_dir": "/tmp/mediation_effect"}
        self.global_config["run_settings"]["enabled_modules"]["mediation_effect"] = True
        self.config["hypothesis_registry"]["expected_hypotheses"].append(
            {"id": "M1", "modules": ["mediation_effect"]})
        chain = self.config["theory_chains"]["chains"][0]
        chain["required_modules"].append("mediation_effect")
        chain["required_hypotheses"].append("M1")
        chain["mediator"] = "m"
        basis = json.dumps({"required_paths": ["x_to_y", "x_to_m", "x_m_to_y"],
            "missing_paths": [], "duplicate_paths": [], "screening_method": "three_step",
            "pass_rule": "all_required_paths"})
        mediation = {**self.row, "source_module": "mediation_effect", "假设编号": "M1",
            "model_id": "med-1", "中介变量": "m", "chain_id": "M1_x_m_y",
            "path_type": "x_to_m", "hypothesis_pass_basis_json": basis}
        incomplete = pd.DataFrame([self.row, mediation])
        cases, rejected = build_verified_display_cases({"strong": incomplete}, self.global_config,
                                                        self.config, self.context)
        self.assertTrue(cases.empty)
        self.assertIn("路径缺失", rejected.iloc[0]["reason"])
        complete = pd.DataFrame([self.row] + [
            {**mediation, "model_id": f"med-{index}", "path_type": path,
             "sample_identity": f"sample-{index}", "样本量（N）": 20 - index}
            for index, path in enumerate(["x_to_y", "x_to_m", "x_m_to_y"], 1)])
        cases, rejected = build_verified_display_cases({"strong": complete}, self.global_config,
                                                        self.config, self.context)
        self.assertEqual(len(cases), 1)
        self.assertTrue(rejected.empty)
        self.assertEqual(len(json.loads(cases.iloc[0]["selected_models_json"])), 4)


if __name__ == "__main__":
    unittest.main()
