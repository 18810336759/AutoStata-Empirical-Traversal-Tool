from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import yaml
from openpyxl import Workbook, load_workbook


MODULE_PATH = Path(__file__).resolve().parents[1] / "run_reproduction.py"
SPEC = importlib.util.spec_from_file_location("run_reproduction", MODULE_PATH)
assert SPEC and SPEC.loader
reproduction = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reproduction)


class ReproductionTests(unittest.TestCase):
    def test_grouped_paper_table_has_three_effect_sheets(self) -> None:
        plan = {"table_title": "回归结果", "models": {"beginner": [
            {"module": "main_effect", "code": "H1", "status": "historical_c"},
            {"module": "moderation_effect", "code": "W1", "status": reproduction.SUPPLEMENTAL}]}}
        rows = [{"hypothesis": code, "term": term, "beta": "0.25", "se": "0.10",
                 "p": "0.02", "N": "100", "r2": "0.3"}
                for code, term in (("H1", "x"), ("W1", "xw"))]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "effects.xlsx"
            reproduction.paper_table(path, plan, "beginner", rows)
            book = load_workbook(path, read_only=True, data_only=True)
            try:
                self.assertEqual(book.sheetnames, ["主效应", "中介效应", "调节效应"])
                self.assertEqual(book["主效应"]["B2"].value, "(1) H1")
                self.assertIn("未纳入", book["中介效应"]["A2"].value)
                self.assertEqual(book["调节效应"]["B2"].value, "(1) W1")
            finally:
                book.close()

    def test_stata_table_groups_follow_effect_module(self) -> None:
        models = [{"module": "main_effect", "code": "H1", "controls": [], "focal": ["x"],
                   "terms": {"outcome": "y", "fixed_effects": {"entity": "id", "time": "year"},
                             "focal_terms": ["x"], "controls": []},
                   "processing": {"threshold": {"entity": "id", "year": "year",
                                                "core_variables": ["x", "y"], "minimum_distinct_valid_years": 2},
                                  "interpolation": {"enabled": False, "variables": [], "method": "linear",
                                                    "scope": "within_entity", "limit_direction": "both",
                                                    "max_gap_calendar_years": None},
                                  "winsorize": {"enabled": False, "variables": [], "method": "winsorize",
                                                "scope": "full_sample", "quantile_interpolation": "linear"}},
                   "N": 10, "reference": [], "status": "historical_c"}]
        context = {"data_file": Path("/tmp/raw.dta"), "row": {"specification_id": "spec_1",
                   "run_id": "run_1"}, "modules": {"main_effect": {"module": {}}}}
        do = reproduction.build_do(models, context, exact=True, output_dir=Path("/tmp/output"))
        self.assertIn("estimates store __m1", do)
        self.assertIn("esttab __m1 using \"$REPRO_OUTPUT/exact_main_effect.rtf\"", do)
        self.assertIn("中介效应：本设定未纳入", do)
        self.assertLess(do.index("postfile __results"), do.index("post __results"))
        self.assertLess(do.index("post __results"), do.index("postclose __results"))

    def test_multiple_linear_main_effect_terms(self) -> None:
        hypothesis = {"id": "H2", "enabled": True, "y": "outcome", "model_form": "linear",
                      "x": [{"name": "centrality"}, {"name": "hole"}]}
        core, outcome, form = reproduction.hypothesis_parts("main_effect", hypothesis)
        self.assertEqual((core, outcome, form),
                         (["outcome", "centrality", "hole"], "outcome", "linear"))
        _, terms = reproduction.beginner_terms(
            {"module": "main_effect", "code": "H2", "form": "linear"},
            {"hypotheses": [hypothesis]})
        self.assertEqual(terms, ["centrality", "hole"])

    def test_multiple_curved_main_effects_still_rejected(self) -> None:
        hypothesis = {"id": "H2", "y": "outcome", "model_form": "u_shape",
                      "x": [{"name": "centrality"}, {"name": "hole"}]}
        with self.assertRaisesRegex(ValueError, "H2"):
            reproduction.hypothesis_parts("main_effect", hypothesis)

    def test_inverted_u_main_effect_keeps_linear_and_square_terms(self) -> None:
        hypothesis = {"id": "H1a", "y": "outcome", "model_form": "inverted_u_shape",
                      "x": [{"name": "centrality"}]}
        self.assertEqual(reproduction.hypothesis_parts("main_effect", hypothesis),
                         (["outcome", "centrality"], "outcome", "inverted_u_shape"))
        lines, terms = reproduction.beginner_terms(
            {"module": "main_effect", "code": "H1a", "form": "inverted_u_shape"},
            {"hypotheses": [hypothesis]})
        self.assertEqual(terms, ["centrality", "__x_sq"])
        self.assertIn("gen double __x_sq = centrality^2", lines)

    def test_inverted_u_moderation_keeps_both_interactions(self) -> None:
        hypothesis = {"id": "W1", "focal_relationship": {"from": "centrality", "to": "outcome",
                                                     "form": "inverted_u_shape"},
                      "moderator": "distinctiveness", "moderation": {"form": "linear"}}
        self.assertEqual(reproduction.hypothesis_parts("moderation_effect", hypothesis),
                         (["centrality", "outcome", "distinctiveness"], "outcome", "inverted_u_shape"))
        _, terms = reproduction.beginner_terms(
            {"module": "moderation_effect", "code": "W1", "form": "inverted_u_shape"},
            {"moderation_paths": [hypothesis]})
        self.assertEqual(terms, ["__x_c", "__w_c", "__xw", "__x_sq", "__x2w"])

    def test_reads_synthesis_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "merged_full_results.xlsx"
            workbook = Workbook()
            workbook.active.append(["run_id", "specification_id", "model_ids_json"])
            workbook.active.append(["batch_1", "spec_123", '{"main_effect": []}'])
            workbook.save(path)
            self.assertEqual(reproduction.read_synthesis(path)[0]["specification_id"], "spec_123")

    def test_synthesis_source_rejects_wrong_filename(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "result_synthesis"
            path.mkdir()
            (path / "summary.xlsx").touch()
            with self.assertRaisesRegex(ValueError, "merged_full_results"):
                reproduction.synthesis_source(Path(directory) / "reproduction.yaml",
                                              {"source": {"synthesis_file": str(path / "summary.xlsx")}})

    def test_post_commands_are_complete_lines(self) -> None:
        source = MODULE_PATH.read_text(encoding="utf-8")
        self.assertIn('lines.append(f\'post __results', source)
        self.assertNotIn('(_se[{term}]) ///', source)

    def test_exact_generated_terms_follow_dependencies(self) -> None:
        model = {"terms": {"generated_terms": {
            "xw": "xc * wc", "wc": "w - 2.5", "xc": "x - 1.5"},
            "focal_terms": ["xc", "wc", "xw"]}}
        lines, terms = reproduction.exact_terms(model)
        self.assertEqual(terms, ["xc", "wc", "xw"])
        self.assertGreater(lines.index("gen double xw = xc * wc"), lines.index("gen double xc = x - 1.5"))
        self.assertGreater(lines.index("gen double xw = xc * wc"), lines.index("gen double wc = w - 2.5"))

    def test_beginner_curved_moderation_keeps_lower_order_terms(self) -> None:
        model = {"module": "moderation_effect", "code": "W2", "form": "u_shape"}
        config = {"moderation_paths": [{"id": "W2", "focal_relationship": {"from": "结构洞"},
                                        "moderator": "Distinctiveness"}]}
        lines, terms = reproduction.beginner_terms(model, config)
        self.assertEqual(terms, ["__x_c", "__w_c", "__xw", "__x_sq", "__x2w"])
        self.assertGreater(lines.index("gen double __x2w = __x_sq * __w_c"),
                           lines.index("gen double __x_sq = __x_c^2"))

    def test_unimplemented_recipe_does_not_silently_change_scope(self) -> None:
        recipe = {"threshold": {"entity": "stkcd", "year": "year", "core_variables": ["x"]},
                  "interpolation": {"method": "linear", "scope": "within_entity",
                                    "limit_direction": "both", "max_gap_calendar_years": None},
                  "winsorize": {"method": "winsorize", "scope": "by_year",
                                "quantile_interpolation": "linear"}}
        with self.assertRaisesRegex(ValueError, "全样本缩尾"):
            reproduction.validate_recipe(recipe, exact=True)

    def test_selected_case_separates_history_and_supplemental(self) -> None:
        config = MODULE_PATH.parent / "reproduction.yaml"
        if not config.exists():
            self.skipTest("示例配置缺失")
        settings = yaml.safe_load(config.read_text(encoding="utf-8"))
        source = (config.parent / settings["source"]["synthesis_file"]).resolve()
        if not source.is_file() or settings["selection"]["run_id"].startswith("请"):
            self.skipTest("尚未选定已完成整合的演示设定")
        _, context = reproduction.load_context(config)
        exact = reproduction.historical_models(context)
        beginner = reproduction.beginner_models(context, exact, "all_enabled")
        exact_codes = {model["code"] for model in exact}
        beginner_codes = {model["code"] for model in beginner}
        self.assertTrue(exact_codes <= beginner_codes)
        self.assertEqual({model["code"] for model in beginner
                          if model["status"] == reproduction.SUPPLEMENTAL},
                         beginner_codes - exact_codes)


if __name__ == "__main__":
    unittest.main()
