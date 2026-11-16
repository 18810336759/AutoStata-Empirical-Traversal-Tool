"""Small end-to-end batch run for the three effects and synthesis."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "skill"))
sys.path.insert(0, str(BASE / "skill" / "other"))

from main_effect.run_main_effect import run_main_effect  # noqa: E402
from mediation_effect.run_mediation_effect import run_mediation_effect  # noqa: E402
from moderation_effect.run_moderation_effect import run_moderation_effect  # noqa: E402
from result_synthesis.run_result_synthesis import (  # noqa: E402
    run_result_synthesis, build_model_structure_check, prepare_synthesis_inputs,
)
from config_validation import load_config_file  # noqa: E402
from result_schema import HUMAN_RESULT_COLUMNS  # noqa: E402


class RunnerBatchIntegrationTests(unittest.TestCase):
    def test_three_runners_share_batch_and_synthesis_uses_snapshots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            rng = np.random.default_rng(7)
            x = rng.normal(size=80)
            m = .6 * x + rng.normal(size=80) * .3
            w = rng.normal(size=80)
            y = .4 * x + .7 * m + .25 * x * w + rng.normal(size=80) * .4
            pd.DataFrame({"firm": np.repeat(np.arange(20), 4),
                          "year": np.tile(np.arange(2018, 2022), 20),
                          "x": x, "m": m, "w": w, "y": y,
                          "c": rng.normal(size=80)}).to_csv(root / "data.csv", index=False)
            global_config = yaml.safe_load((BASE / "config/analysis_config.yaml").read_text(encoding="utf-8"))
            global_config["project"] = {"name": "smoke", "stage": "test", "description": "test"}
            global_config["run_settings"].update(
                run_id="smoke", overwrite_existing=True, enabled_modules={name: True for name in
                ("main_effect", "mediation_effect", "moderation_effect", "result_synthesis")})
            global_config["data"]["input_file"] = str(root / "data.csv")
            global_config["data"]["panel"] = {"entity_name": "firm", "time_name": "year"}
            global_config["data"]["sample_info"] = {
                "sample_name": "synthetic", "sample_period": "2018-2021",
                "sample_rules": [], "panel_structure": "firm-year"}
            global_config["output_settings"]["output_root"] = str(root / "output")
            global_config["continuous_variable_settings"] = {
                "continuous_variables": "x y m w c", "interpolate_allowed": "x y m w c",
                "winsorize_allowed": "x y m w c", "exclude_variables": "firm year"}
            global_config["preprocessing"].update(
                sample_thresholds=[2], treatment_profiles={"interpolate": False, "winsorize": False})
            global_config["controls"].update(required_controls="c", optional_controls="",
                active_optional_controls="")
            global_config["controls"]["combination_rules"].update(
                enabled=False, min_optional_controls_in_combination=0,
                max_optional_controls_in_combination=0)
            global_config["model_settings"]["fixed_effects"].update(
                entity_fixed_effect=False, entity_name="firm",
                time_fixed_effect=False, time_name="year")
            global_config["model_settings"]["clustered_standard_errors"] = {
                "enabled": False, "cluster_level": ""}
            global_config["model_settings"]["minimum_sample_size"] = 10

            main = yaml.safe_load((BASE / "skill/main_effect/main.yaml").read_text(encoding="utf-8"))
            main["variables"] = {"dependent_variables": {"outcomes": ["y"]},
                                 "independent_variables": {"causes": ["x"]}}
            main["hypotheses"] = [{"id": "H1", "enabled": True, "description": "x to y",
                "y": "y", "x": [{"name": "x", "expected_direction": "positive", "required": True}],
                "model_form": "linear", "pass_rule": "all_required_terms"}]
            mediation = yaml.safe_load((BASE / "skill/mediation_effect/mediation.yaml").read_text(encoding="utf-8"))
            mediation["variables"] = {"dependent_variables": {"outcomes": ["y"]},
                "independent_variables": {"causes": ["x"]},
                "mediator_variables": {"mechanisms": ["m"]}}
            mediation["mechanism_paths"] = [{"id": "M1", "enabled": True,
                "description": "x to m to y", "x": "x", "y": "y", "mediators": ["m"],
                "path_directions": {"x_to_y": "positive", "x_to_m": "positive",
                                    "m_to_y": "positive", "indirect_effect": "positive"},
                "screening_method": "three_step", "pass_rule": "all_required_paths"}]
            moderation = yaml.safe_load((BASE / "skill/moderation_effect/moderation.yaml").read_text(encoding="utf-8"))
            moderation["moderation_paths"] = [{"id": "W1", "enabled": True,
                "description": "w moderates x to y", "focal_relationship": {
                    "from": "x", "to": "y", "form": "linear", "direction": "positive"},
                "moderator": "w", "moderation": {"form": "linear", "expected_change": "positive"}}]
            moderation["conditional_effect_analysis"]["draw_plot_in_current_module"] = False
            synthesis = yaml.safe_load((BASE / "skill/result_synthesis/synthesis.yaml").read_text(encoding="utf-8"))
            synthesis["input"]["module_selection_mode"] = "enabled_modules"
            synthesis["hypothesis_registry"]["expected_hypotheses"] = [
                {"id": "H1", "modules": ["main_effect"], "description": "x to y", "required_pass": "none"},
                {"id": "M1", "modules": ["mediation_effect"], "description": "x to m to y", "required_pass": "none"},
                {"id": "W1", "modules": ["moderation_effect"],
                 "description": "w moderates x to y", "required_pass": "none"},
            ]
            synthesis["hypothesis_registry"]["fail_on_unregistered_output"] = False
            synthesis["hypothesis_registry"]["fail_on_registered_missing_output"] = False
            synthesis["theory_chains"]["chains"] = []
            synthesis["display_case_selection"]["enabled"] = False
            synthesis["specification_curve"]["enabled"] = False
            global_config["module_config_files"] = {}
            for name, config in (("main_effect", main), ("mediation_effect", mediation),
                                 ("moderation_effect", moderation), ("result_synthesis", synthesis)):
                path = root / f"{name}.yaml"
                path.write_text(yaml.safe_dump(config, allow_unicode=True), encoding="utf-8")
                global_config["module_config_files"][name] = str(path)
            config_path = root / "analysis_config.yaml"
            config_path.write_text(yaml.safe_dump(global_config, allow_unicode=True), encoding="utf-8")

            run_main_effect(config_path)
            run_mediation_effect(config_path)
            run_moderation_effect(config_path)
            run_result_synthesis(config_path)

            project = root / "output" / "smoke"
            manifest = json.loads((project / "run_manifest.json").read_text(encoding="utf-8"))
            batch = manifest["batches"][manifest["current_run_id"]]
            self.assertEqual(set(batch["modules"]),
                             {"main_effect", "mediation_effect", "moderation_effect"})
            self.assertEqual(batch["synthesis"]["status"], "success")
            sample = project / "2018-2021（全样本）"
            shared_ids = set()
            decision_headers = {}
            for module in batch["modules"]:
                self.assertTrue((sample / module / "specification_index.csv").is_file())
                self.assertTrue((sample / module / "standardized_results.csv").is_file())
                self.assertTrue((sample / module / "hypothesis_results.csv").is_file())
                self.assertFalse((sample / module / "full_results.csv").exists())
                self.assertTrue((sample / module / "run_record.json").is_file())
                for stem in ("full_results", "control_combination_results", "candidate_results",
                             "strong_pass_results", "weak_pass_results"):
                    human = pd.read_excel(sample / module / f"{stem}.xlsx")
                    self.assertEqual(human.columns[:len(HUMAN_RESULT_COLUMNS)].tolist(),
                                     HUMAN_RESULT_COLUMNS, (module, stem))
                    if stem in {"strong_pass_results", "weak_pass_results"}:
                        self.assertTrue(human.empty or human["检验阶段"].eq("C").all())
                    if stem in {"candidate_results", "strong_pass_results", "weak_pass_results"}:
                        if stem in decision_headers:
                            self.assertEqual(human.columns.tolist(), decision_headers[stem])
                        else:
                            decision_headers[stem] = human.columns.tolist()
                index = pd.read_csv(sample / module / "specification_index.csv")
                self.assertEqual(set(index["run_id"].dropna()), {batch["run_id"]})
                shared_ids.update(index["specification_id"].dropna())
                self.assertTrue(index.loc[index["是否成功运行"] == "success", "模型公式"].notna().all())
                self.assertTrue(index.loc[index["是否成功运行"] == "success", "模型项定义"].notna().all())
                self.assertTrue(batch["modules"][module]["synthesis_ready"])
            merged = pd.read_csv(sample / "result_synthesis" / "merged_full_results.csv")
            self.assertEqual(len(merged), 1)
            self.assertIn("缺少C结果", merged.columns)
            detail = pd.read_csv(sample / "result_synthesis" / "hypothesis_status_detail.csv")
            self.assertEqual(set(detail["假设编号"]), {"H1", "M1", "W1"})
            self.assertTrue((detail["C阶段状态"] == "缺少C结果").any())
            mediation_decisions = pd.read_csv(sample / "mediation_effect" / "hypothesis_results.csv")
            self.assertEqual(mediation_decisions.loc[0, "required_paths"], "x_to_y x_to_m x_m_to_y")
            self.assertTrue(pd.notna(mediation_decisions.loc[0, "path_samples_json"]))
            self.assertEqual(len(shared_ids), 1)
            for module in batch["modules"]:
                self.assertTrue((sample / "result_synthesis" / "config_snapshot" /
                                 module / "analysis_config.yaml").is_file())

            moderation_path = Path(global_config["module_config_files"]["moderation_effect"])
            changed = yaml.safe_load(moderation_path.read_text(encoding="utf-8"))
            changed["moderation_paths"][0]["description"] = "changed after fitting"
            moderation_path.write_text(yaml.safe_dump(changed, allow_unicode=True), encoding="utf-8")
            model_check, _ = build_model_structure_check(
                load_config_file(config_path),
                load_config_file(global_config["module_config_files"]["result_synthesis"]))
            moderation_check = model_check.set_index("module_name").loc["moderation_effect"]
            self.assertFalse(bool(moderation_check["will_be_included"]))
            self.assertIn("历史结果不适用于当前批次", moderation_check["warning"])

            # Real C-stage paths: the total effect must be refitted with z,
            # while missing mediator observations may change path samples.
            data = pd.read_csv(root / "data.csv")
            data["z"] = rng.normal(size=len(data))
            data.loc[:4, "m"] = np.nan
            data.to_csv(root / "data.csv", index=False)
            global_config["controls"].update(optional_controls="z", active_optional_controls="z")
            global_config["controls"]["combination_rules"].update(
                enabled=True, min_optional_controls_in_combination=1,
                max_optional_controls_in_combination=1)
            config_path.write_text(yaml.safe_dump(global_config, allow_unicode=True), encoding="utf-8")
            run_mediation_effect(config_path)
            with self.assertRaisesRegex(RuntimeError, "当前批次无法进入 synthesis"):
                prepare_synthesis_inputs(config_path)
            c_paths = pd.read_csv(sample / "mediation_effect" / "control_combination_results.csv") if (
                sample / "mediation_effect" / "control_combination_results.csv").exists() else pd.read_excel(
                sample / "mediation_effect" / "control_combination_results.xlsx")
            self.assertEqual(set(c_paths["path_type"]), {"x_to_y", "x_to_m", "x_m_to_y"})
            self.assertEqual(c_paths["控制变量组合ID"].nunique(), 1)
            c_decision = pd.read_csv(sample / "mediation_effect" / "hypothesis_results.csv")
            c_decision = c_decision.loc[c_decision["检验阶段"] == "C"].iloc[0]
            self.assertTrue(bool(c_decision["strong_pass"]))
            self.assertTrue(bool(c_decision["weak_pass"]))
            self.assertFalse(bool(c_decision["path_sample_identity_same"]))
            global_config["run_settings"]["enabled_modules"].update(
                main_effect=False, moderation_effect=False)
            config_path.write_text(yaml.safe_dump(global_config, allow_unicode=True), encoding="utf-8")
            run_result_synthesis(config_path)
            strong_merged = pd.read_csv(sample / "result_synthesis" / "merged_strong_pass_results.csv")
            self.assertEqual(strong_merged["specification_id"].nunique(), 1)
            self.assertEqual(len(json.loads(strong_merged.iloc[0]["model_ids_json"])
                                 ["mediation_effect"]), 3)

            # A later rerun with machine output disabled removes the prior files.
            global_config["run_settings"]["enabled_modules"]["main_effect"] = True
            global_config["output_settings"]["standardized_output"]["save_standardized_results"] = False
            config_path.write_text(yaml.safe_dump(global_config, allow_unicode=True), encoding="utf-8")
            run_main_effect(config_path)
            self.assertFalse((sample / "main_effect" / "standardized_results.csv").exists())
            self.assertFalse((sample / "main_effect" / "specification_index.csv").exists())
            self.assertFalse((sample / "main_effect" / "hypothesis_results.csv").exists())
            record = json.loads((sample / "main_effect" / "run_record.json").read_text(encoding="utf-8"))
            self.assertFalse(record["synthesis_ready"])
            with self.assertRaisesRegex(RuntimeError, "机器衔接产物缺失"):
                prepare_synthesis_inputs(config_path)


if __name__ == "__main__":
    unittest.main()
