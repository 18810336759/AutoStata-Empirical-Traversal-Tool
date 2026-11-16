"""Batch and historical-evidence checks using disposable project outputs."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
import pandas as pd

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "skill"))
sys.path.insert(0, str(BASE / "skill" / "other"))

from _internal.run_provenance import (  # noqa: E402
    common_identity, read_manifest, run_effect, run_synthesis,
    specification_id, verify_effect,
)
from result_synthesis.run_result_synthesis import save_config_snapshots  # noqa: E402
from result_schema import build_specification_index  # noqa: E402
from main_effect.run_main_effect import build_standardized_results as main_standardized  # noqa: E402
from mediation_effect.run_mediation_effect import build_standardized_results as mediation_standardized  # noqa: E402
from moderation_effect.run_moderation_effect import build_standardized_results as moderation_standardized  # noqa: E402


class RunProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / "input.csv"
        self.data.write_text("firm,year,x\nA,2020,1\n", encoding="utf-8")
        self.global_path = self.root / "analysis_config.yaml"
        self.global_path.write_text("project: test\n", encoding="utf-8")
        self.modules = {}
        for module in ("main_effect", "mediation_effect"):
            path = self.root / f"{module}.yaml"
            path.write_text(f"module_name: {module}\n", encoding="utf-8")
            self.modules[module] = {"_config_path": str(path), "module_name": module}
        self.config = {
            "_config_path": str(self.global_path), "_base_dir": str(self.root),
            "project": {"name": "test"},
            "output_settings": {"output_root": "output"},
            "data": {"input_file": str(self.data), "panel": {"entity_name": "firm", "time_name": "year"},
                     "sample_info": {"sample_period": "2020", "sample_rules": []}},
            "preprocessing": {"winsorize": {"scope": "full_sample", "lower_quantile": .01,
                                              "upper_quantile": .99}},
            "combination_execution": {"random_seed": 1},
            "run_settings": {"run_id": "trial", "overwrite_existing": True},
        }
        self.script = BASE / "skill" / "main_effect" / "run_main_effect.py"
        self.global_path.write_text(yaml.safe_dump(
            {key: value for key, value in self.config.items() if not key.startswith("_")},
            allow_unicode=True), encoding="utf-8")

    @staticmethod
    def write_output(stage: Path):
        (stage / "standardized_results.csv").write_text("model_id,p\na,0.01\n", encoding="utf-8")
        (stage / "specification_index.csv").write_text("model_id\na\n", encoding="utf-8")
        (stage / "hypothesis_results.csv").write_text("model_id\na\n", encoding="utf-8")
        (stage / "preprocessing_definitions.json").write_text(
            json.dumps({"schema_version": "2.0-shared-1", "definitions": {"recipe": {
                "order": ["valid_year_threshold_on_raw_core", "within_entity_interpolation",
                          "winsorization", "model_specific_complete_case"]}}}), encoding="utf-8")

    def test_cross_module_key_reuse_and_changed_computation(self):
        first = run_effect("main_effect", self.config, self.modules["main_effect"],
                           self.script, self.write_output)
        second = run_effect("mediation_effect", self.config, self.modules["mediation_effect"],
                            self.script, self.write_output)
        self.assertEqual(first["run_id"], second["run_id"])
        row = {"样本门槛": 2, "处理方案": "winsorize_controls", "纳入的控制变量": "c1 c2",
               "固定效应设定": "entity:firm", "聚类标准误设定": "none"}
        self.assertEqual(specification_id(row, first), specification_id(row, second))
        self.assertEqual(first["preprocessing"]["definition_count"], 1)
        self.assertIn("other/preprocessing.py", first["environment"]["code_sha256"])

        original = common_identity(self.config)["common_fingerprint"]
        self.config["combination_execution"]["random_seed"] = 2
        self.assertNotEqual(original, common_identity(self.config)["common_fingerprint"])
        self.config["combination_execution"]["random_seed"] = 1
        self.config["preprocessing"]["winsorize"]["upper_quantile"] = .95
        self.assertNotEqual(original, common_identity(self.config)["common_fingerprint"])
        self.config["preprocessing"]["winsorize"]["upper_quantile"] = .99
        self.data.write_text("firm,year,x\nA,2020,2\n", encoding="utf-8")
        self.assertNotEqual(original, common_identity(self.config)["common_fingerprint"])

    def test_failed_rerun_keeps_previous_directory_and_records_failure(self):
        run_effect("main_effect", self.config, self.modules["main_effect"],
                   self.script, self.write_output)
        output = self.root / "output" / "test" / "2020（全样本）" / "main_effect"
        old_result = (output / "standardized_results.csv").read_bytes()
        def fail(stage: Path):
            (stage / "standardized_results.csv").write_text("partial", encoding="utf-8")
            raise ValueError("simulated failure")
        with self.assertRaisesRegex(ValueError, "simulated failure"):
            run_effect("main_effect", self.config, self.modules["main_effect"], self.script, fail)
        self.assertEqual((output / "standardized_results.csv").read_bytes(), old_result)
        manifest = read_manifest(self.config)
        batch = manifest["batches"][manifest["current_run_id"]]
        self.assertEqual(batch["modules"]["main_effect"]["status"], "success")
        self.assertEqual(batch["failed_attempts"][-1]["module"], "main_effect")

    def test_rerun_removes_outputs_disabled_this_time(self):
        def with_optional_chart(stage: Path):
            self.write_output(stage)
            (stage / "figures").mkdir()
            (stage / "figures" / "chart.svg").write_text("<svg/>", encoding="utf-8")
        first = run_effect("main_effect", self.config, self.modules["main_effect"],
                           self.script, with_optional_chart)
        output = self.root / "output" / "test" / "2020（全样本）" / "main_effect"
        self.assertTrue((output / "figures" / "chart.svg").exists())
        second = run_effect("main_effect", self.config, self.modules["main_effect"],
                            self.script, self.write_output)
        self.assertFalse((output / "figures").exists())
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["run_instance_id"], second["run_instance_id"])
        self.assertEqual(json.loads((output / "run_record.json").read_text())["run_instance_id"],
                         second["run_instance_id"])

    def test_new_public_batch_excludes_old_other_module(self):
        first = run_effect("main_effect", self.config, self.modules["main_effect"],
                           self.script, self.write_output)
        run_effect("mediation_effect", self.config, self.modules["mediation_effect"],
                   self.script, self.write_output)
        self.config["preprocessing"]["winsorize"]["upper_quantile"] = .95
        self.global_path.write_text(yaml.safe_dump(
            {key: value for key, value in self.config.items() if not key.startswith("_")},
            allow_unicode=True), encoding="utf-8")
        second = run_effect("main_effect", self.config, self.modules["main_effect"],
                            self.script, self.write_output)
        self.assertNotEqual(first["run_id"], second["run_id"])
        manifest = read_manifest(self.config)
        self.assertEqual(set(manifest["batches"][second["run_id"]]["modules"]), {"main_effect"})
        old_mediation = (self.root / "output" / "test" / "2020（全样本）" /
                         "mediation_effect" / "standardized_results.csv")
        self.assertTrue(old_mediation.exists())
        with self.assertRaisesRegex(RuntimeError, "当前批次"):
            verify_effect(self.config, "mediation_effect", old_mediation,
                          Path(self.modules["mediation_effect"]["_config_path"]))

    def test_module_yaml_revision_keeps_batch_but_stales_synthesis(self):
        first = run_effect("main_effect", self.config, self.modules["main_effect"],
                           self.script, self.write_output)
        module_path = Path(self.modules["main_effect"]["_config_path"])
        module_path.write_text("module_name: main_effect\nhypothesis_note: changed\n", encoding="utf-8")
        self.modules["main_effect"]["hypothesis_note"] = "changed"
        second = run_effect("main_effect", self.config, self.modules["main_effect"],
                            self.script, self.write_output)
        self.assertEqual(first["run_id"], second["run_id"])
        self.assertNotEqual(first["module_revision"], second["module_revision"])
        batch = read_manifest(self.config)["batches"][first["run_id"]]
        self.assertEqual(batch["synthesis"]["status"], "stale")

    def test_project_rename_preserves_previous_project_outputs(self):
        first = run_effect("main_effect", self.config, self.modules["main_effect"],
                           self.script, self.write_output)
        previous = self.root / "output" / "test" / "2020（全样本）" / "main_effect"
        previous_bytes = (previous / "standardized_results.csv").read_bytes()
        self.config["project"]["name"] = "renamed"
        self.global_path.write_text(yaml.safe_dump(
            {key: value for key, value in self.config.items() if not key.startswith("_")},
            allow_unicode=True), encoding="utf-8")
        second = run_effect("main_effect", self.config, self.modules["main_effect"],
                            self.script, self.write_output)
        current = self.root / "output" / "renamed" / "2020（全样本）" / "main_effect"
        self.assertEqual(first["common_fingerprint"], second["common_fingerprint"])
        self.assertEqual((previous / "standardized_results.csv").read_bytes(), previous_bytes)
        self.assertTrue((current / "run_record.json").is_file())
        self.assertTrue((self.root / "output" / "test" / "run_manifest.json").is_file())
        self.assertTrue((self.root / "output" / "renamed" / "run_manifest.json").is_file())

    def test_synthesis_uses_fitting_snapshots_and_rejects_changed_module_yaml(self):
        record = run_effect("main_effect", self.config, self.modules["main_effect"],
                            self.script, self.write_output)
        output = self.root / "output" / "test" / "2020（全样本）" / "main_effect"
        result_file = output / "standardized_results.csv"
        captured, module_snapshot, global_snapshot = verify_effect(
            self.config, "main_effect", result_file, Path(self.modules["main_effect"]["_config_path"]))
        self.assertEqual(captured["run_id"], record["run_id"])
        context = {"main_effect": {"will_be_included": True, "run_record": captured,
                                   "output_dir": output}}
        synthesis_path = self.root / "synthesis.yaml"
        synthesis_path.write_text("enabled: true\n", encoding="utf-8")
        synthesis_config = {"_config_path": str(synthesis_path), "config_snapshot": {"enabled": True}}
        destination = self.root / "synthesis_output"
        save_config_snapshots(destination, self.config, synthesis_config, context)
        self.assertEqual((destination / "config_snapshot" / "main_effect" / "analysis_config.yaml").read_bytes(),
                         global_snapshot.read_bytes())
        self.assertEqual((destination / "config_snapshot" / "main_effect" / "main_effect.yaml").read_bytes(),
                         module_snapshot.read_bytes())
        self.modules["main_effect"]["_config_path"] = str(self.root / "main_effect.yaml")
        Path(self.modules["main_effect"]["_config_path"]).write_text(
            "module_name: main_effect\nchanged: true\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "YAML 已更改"):
            verify_effect(self.config, "main_effect", result_file,
                          Path(self.modules["main_effect"]["_config_path"]))

    def test_synthesis_inventory_and_atomic_rerun(self):
        run_effect("main_effect", self.config, self.modules["main_effect"],
                   self.script, self.write_output)
        synthesis_path = self.root / "synthesis.yaml"
        synthesis_path.write_text("enabled: true\n", encoding="utf-8")
        synthesis_config = {"_config_path": str(synthesis_path), "enabled": True}
        output = self.root / "output" / "test" / "2020（全样本）" / "result_synthesis"
        synthesis_script = BASE / "skill" / "result_synthesis" / "run_result_synthesis.py"
        source = self.root / "output" / "test" / "2020（全样本）" / "main_effect" / "standardized_results.csv"
        def write(stage: Path):
            (stage / "display_case_index.csv").write_text("model_id\na\n", encoding="utf-8")
        record = run_synthesis(self.config, synthesis_config, synthesis_script, output,
                               {"main_effect": source}, write)
        self.assertEqual(record["input_modules"].keys(), {"main_effect"})
        self.assertEqual(set(record["input_files"]["main_effect"]["machine_files"]),
                         {"standardized_results.csv", "specification_index.csv", "hypothesis_results.csv"})
        self.assertEqual(record["files"][0]["path"], "display_case_index.csv")
        self.assertEqual(read_manifest(self.config)["batches"][record["run_id"]]
                         ["synthesis"]["status"], "success")
        old_result = (output / "display_case_index.csv").read_bytes()
        def fail(stage: Path):
            (stage / "display_case_index.csv").write_text("partial", encoding="utf-8")
            raise ValueError("interrupted")
        with self.assertRaisesRegex(ValueError, "interrupted"):
            run_synthesis(self.config, synthesis_config, synthesis_script, output,
                          {"main_effect": source}, fail)
        self.assertEqual((output / "display_case_index.csv").read_bytes(), old_result)
        def change_input(stage: Path):
            write(stage)
            (source.parent / "specification_index.csv").write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "输入批次或配置发生变化"):
            run_synthesis(self.config, synthesis_config, synthesis_script, output,
                          {"main_effect": source}, change_input)
        self.assertEqual((output / "display_case_index.csv").read_bytes(), old_result)

    def test_all_standardized_tables_keep_formula_for_specification_index(self):
        main = main_standardized(pd.DataFrame([{
            "model_id": "m1", "检验项组合": "x", "模型公式": "y ~ x",
            "样本量（N）": 12, "sample_identity": "sample-one"}]),
            self.config, {"nonlinear_settings": {}})
        mediation = mediation_standardized(pd.DataFrame([{
            "model_id": "m2", "path_type": "x_to_m", "检验项": "x",
            "模型公式": "m ~ x", "样本量（N）": 11,
            "sample_identity": "sample-two", "是否成功运行": "success"}]),
            self.config, {})
        moderation = moderation_standardized(pd.DataFrame([{
            "model_id": "m3", "模型公式": "y ~ x + w + x:w",
            "样本量（N）": 10, "sample_identity": "sample-three"}]),
            pd.DataFrame(), self.config,
            {"standardized_output": {"unified_schema": {"strict_common_columns": True}}})
        for frame, formula in ((main, "y ~ x"), (mediation, "m ~ x"),
                               (moderation, "y ~ x + w + x:w")):
            self.assertEqual(frame.iloc[0]["模型公式"], formula)
            index = build_specification_index(frame)
            self.assertEqual(index.iloc[0]["模型公式"], formula)
            self.assertIn("sample_identity", index)


if __name__ == "__main__":
    unittest.main()
