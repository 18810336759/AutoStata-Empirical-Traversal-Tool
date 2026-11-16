"""Specification-level C-stage synthesis and overview workbook."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import yaml

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "skill"))
sys.path.insert(0, str(BASE / "skill" / "other"))

from result_synthesis.run_result_synthesis import (  # noqa: E402
    build_specification_tables, save_overview_workbook, SPECIFICATION_TABLE_COLUMNS)


class SpecificationTableTests(unittest.TestCase):
    def setUp(self):
        self.config = yaml.safe_load(
            (BASE / "skill/result_synthesis/synthesis.yaml").read_text(encoding="utf-8"))
        self.config["hypothesis_registry"]["expected_hypotheses"] = [
            {"id": "H1", "modules": ["main_effect"], "required_pass": "strong"},
            {"id": "W1", "modules": ["moderation_effect"], "required_pass": "weak"},
        ]
        self.rows = pd.DataFrame([
            {"specification_id": spec, "spec_group_id": f"SPEC_{i:03d}",
             "source_module": "main_effect", "假设编号": "H1", "model_id": f"ab-{i}",
             "检验阶段": "AB", "样本门槛": 10, "处理方案": "raw"}
            for i, spec in enumerate(("s1", "s2", "s3"), 1)
        ])
        main = pd.DataFrame([
            self.decision("s1", "H1", "main_effect", "h1-s1", True, True),
            self.decision("s2", "H1", "main_effect", "h1-s2", True, True),
            self.decision("s3", "H1", "main_effect", "h1-s3", False, False),
        ])
        moderation = pd.DataFrame([
            self.decision("s1", "W1", "moderation_effect", "w1-s1", True, True),
            self.decision("s2", "W1", "moderation_effect", "w1-s2", False, True),
        ])
        self.context = {
            "main_effect": {"will_be_included": True,
                            "module_config": {"hypotheses": [{"id": "H1", "enabled": True}]},
                            "hypotheses_df": main},
            "moderation_effect": {"will_be_included": True,
                                  "module_config": {"moderation_paths": [{"id": "W1", "enabled": True}]},
                                  "hypotheses_df": moderation},
        }

    @staticmethod
    def decision(spec, hyp, module, model, strong, weak):
        return {"specification_id": spec, "模块名称": module, "假设编号": hyp,
                "检验阶段": "C", "是否成功运行": "success", "是否候选模型": True,
                "strong_pass": strong, "weak_pass": weak,
                "path_model_ids_json": json.dumps([model]), "chain_id": "", "判定依据": "test"}

    def test_four_tables_use_only_c_decisions(self):
        tables, detail = build_specification_tables(self.rows, self.config, self.context)
        self.assertEqual([len(tables[name]) for name in ("require", "full", "strong", "weak")],
                         [2, 3, 1, 2])
        self.assertEqual(list(tables["full"].columns), SPECIFICATION_TABLE_COLUMNS)
        missing = detail.loc[(detail["specification_id"] == "s3") &
                             (detail["假设编号"] == "W1")].iloc[0]
        self.assertEqual(missing["C阶段状态"], "缺少C结果")
        self.assertEqual(tables["full"].set_index("specification_id").loc["s3", "缺少C结果"], "W1")
        self.assertEqual(json.loads(tables["strong"].iloc[0]["model_ids_json"])
                         ["moderation_effect"], ["w1-s1"])

    def test_empty_strict_tables_keep_headers_and_workbook_sheets(self):
        self.context["moderation_effect"]["hypotheses_df"] = pd.DataFrame()
        tables, detail = build_specification_tables(self.rows, self.config, self.context)
        self.assertTrue(tables["strong"].empty)
        self.assertTrue(tables["weak"].empty)
        self.assertEqual(list(tables["strong"].columns), SPECIFICATION_TABLE_COLUMNS)
        self.assertTrue((detail["C阶段状态"] == "缺少C结果").any())
        with tempfile.TemporaryDirectory() as temporary:
            save_overview_workbook(Path(temporary), self.config, tables, detail)
            workbook = Path(temporary) / "综合结果总览.xlsx"
            self.assertTrue(workbook.is_file())
            with pd.ExcelFile(workbook) as excel:
                self.assertEqual(excel.sheet_names,
                                 ["require", "full", "strong", "weak", "假设明细", "字段说明"])
                self.assertEqual(list(pd.read_excel(excel, "strong").columns),
                                 SPECIFICATION_TABLE_COLUMNS)

    def test_none_only_relaxes_require_not_strict_tables(self):
        self.config["hypothesis_registry"]["expected_hypotheses"][1]["required_pass"] = "none"
        tables, _ = build_specification_tables(self.rows, self.config, self.context)
        self.assertEqual(set(tables["require"]["specification_id"]), {"s1", "s2"})
        self.assertEqual(set(tables["strong"]["specification_id"]), {"s1"})
        self.assertEqual(set(tables["weak"]["specification_id"]), {"s1", "s2"})

    def test_failed_c_is_distinct_from_missing_c(self):
        failed = self.decision("s3", "W1", "moderation_effect", "w1-s3", False, False)
        failed["是否成功运行"] = "failed"
        self.context["moderation_effect"]["hypotheses_df"] = pd.concat([
            self.context["moderation_effect"]["hypotheses_df"], pd.DataFrame([failed])],
            ignore_index=True)
        tables, detail = build_specification_tables(self.rows, self.config, self.context)
        row = detail.loc[(detail["specification_id"] == "s3") &
                         (detail["假设编号"] == "W1")].iloc[0]
        self.assertEqual(row["C阶段状态"], "运行失败")
        self.assertEqual(tables["full"].set_index("specification_id").loc["s3", "运行失败"], "W1")
        self.assertNotIn("s3", set(tables["weak"]["specification_id"]))

    def test_multiple_c_decisions_must_all_pass(self):
        extra = self.decision("s1", "W1", "moderation_effect", "w1-s1-other", False, False)
        self.context["moderation_effect"]["hypotheses_df"] = pd.concat([
            self.context["moderation_effect"]["hypotheses_df"], pd.DataFrame([extra])],
            ignore_index=True)
        tables, detail = build_specification_tables(self.rows, self.config, self.context)
        self.assertTrue(tables["strong"].empty)
        self.assertEqual(set(tables["weak"]["specification_id"]), {"s2"})
        status = detail.loc[(detail["specification_id"] == "s1") &
                            (detail["假设编号"] == "W1"), "C阶段状态"].iloc[0]
        self.assertEqual(status, "未通过")


if __name__ == "__main__":
    unittest.main()
