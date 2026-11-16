"""Validation of the synthesis specification-level output contract."""

from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

import yaml

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "skill" / "other"))

from config_validation import ConfigError, validate_module, validate_references  # noqa: E402


class SynthesisConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.synthesis = yaml.safe_load(
            (BASE / "skill/result_synthesis/synthesis.yaml").read_text(encoding="utf-8"))
        self.global_cfg = {"run_settings": {"enabled_modules": {
            "main_effect": True, "mediation_effect": False, "moderation_effect": True}}}

    def test_current_contract_is_valid(self):
        self.assertEqual(validate_module(self.global_cfg, self.synthesis, "result_synthesis"), [])

    def test_required_pass_rejects_unknown_level(self):
        cfg = copy.deepcopy(self.synthesis)
        cfg["hypothesis_registry"]["expected_hypotheses"][0]["required_pass"] = "candidate"
        with self.assertRaisesRegex(ConfigError, "required_pass"):
            validate_module(self.global_cfg, cfg, "result_synthesis")

    def test_duplicate_owner_rejected(self):
        cfg = copy.deepcopy(self.synthesis)
        cfg["hypothesis_registry"]["expected_hypotheses"][0]["modules"] *= 2
        with self.assertRaisesRegex(ConfigError, "modules 不能重复"):
            validate_module(self.global_cfg, cfg, "result_synthesis")

    def test_strict_tables_and_workbook_cannot_silently_change_scope(self):
        for change, message in (
            (lambda c: c["pass_tables"]["strong"].update(require_all_hypotheses=False),
             "require_all_hypotheses"),
            (lambda c: c["pass_tables"]["weak"].update(include_strong_rows=False),
             "include_strong_rows"),
            (lambda c: c["output"]["overview_workbook"].update(sheets=["full"]),
             "overview_workbook.sheets"),
        ):
            with self.subTest(message=message):
                cfg = copy.deepcopy(self.synthesis)
                change(cfg)
                with self.assertRaisesRegex(ConfigError, message):
                    validate_module(self.global_cfg, cfg, "result_synthesis")

    def test_unselected_hypothesis_is_warned_not_required(self):
        cfg = copy.deepcopy(self.synthesis)
        cfg["hypothesis_registry"]["expected_hypotheses"] = [
            {"id": "M1", "modules": ["mediation_effect"],
             "description": "mechanism", "required_pass": "strong"}]
        modules = {"mediation_effect": {"enabled": False, "mechanism_paths": [
            {"id": "M1", "enabled": False, "description": "mechanism"}]}}
        notes = validate_references(self.global_cfg, cfg, modules)
        self.assertTrue(any("不会纳入 require/strong/weak 判定" in note for note in notes))


if __name__ == "__main__":
    unittest.main()
