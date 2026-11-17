from __future__ import annotations

import importlib.util
import json
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


def mediation_context(*, stage: str = "C") -> dict:
    paths = {"x_to_y": ("y", ["x"], ["x", "y"], 90),
             "x_to_m": ("m", ["x"], ["x", "m"], 80),
             "x_m_to_y": ("y", ["x", "m"], ["x", "m", "y"], 70)}
    index, standard, ids = [], [], []
    for path_type, (outcome, focal, core, n) in paths.items():
        model_id = f"mediation_M1_{path_type}"
        terms = {"outcome": outcome, "focal_terms": focal, "controls": ["ctrl"],
                 "fixed_effects": {"entity": "id", "time": "year"},
                 "cluster": None, "generated_terms": {}}
        recipe = {"profile": "baseline", "threshold": {"entity": "id", "year": "year",
                  "core_variables": core, "minimum_distinct_valid_years": 2},
                  "interpolation": {"enabled": False, "variables": [], "method": "linear",
                                    "scope": "within_entity", "limit_direction": "both",
                                    "max_gap_calendar_years": None},
                  "winsorize": {"enabled": False, "variables": [], "method": "winsorize",
                                "scope": "full_sample", "quantile_interpolation": "linear"}}
        common = {"model_id": model_id, "假设编号": "M1", "path_type": path_type,
                  "chain_id": "M1_x_m_y", "模型项定义": json.dumps(terms),
                  "processing_definition_json": json.dumps(recipe),
                  "样本门槛": "2", "处理方案": "baseline", "检验阶段": stage}
        index.append({**common, "specification_id": "spec_abc", "样本量（N）": str(n),
                      "sample_identity": f"sample_{path_type}", "processing_definition_id": model_id})
        standard.append({**common, "检验项": "m" if path_type == "x_m_to_y" else "x",
                         "beta": "0.2", "se": "0.05", "p": "0.01",
                         "beta_x": "0.15", "se_x": "0.04", "p_x": "0.02"})
        ids.append(model_id)
    source = {"index": index, "standard": standard,
              "module": {"mechanism_paths": [{"id": "M1", "enabled": True, "x": "x", "y": "y",
                                              "mediators": ["m"]}]},
              "global": {"controls": {"required_controls": "ctrl", "optional_controls": ""},
                         "continuous_variable_settings": {"continuous_variables": "x y m ctrl",
                          "exclude_variables": "id year", "interpolate_allowed": "x y m ctrl",
                          "winsorize_allowed": "x y m ctrl"}}}
    return {"model_ids": {"mediation_effect": ids if stage == "C" else []},
            "modules": {"mediation_effect": source},
            "row": {"specification_id": "spec_abc", "样本门槛": "2",
                    "处理方案": "baseline", "纳入的控制变量": "ctrl", "run_id": "run_1"},
            "data_file": Path("/tmp/raw.dta")}


class ReproductionTests(unittest.TestCase):
    def test_grouped_paper_table_has_three_effect_sheets(self) -> None:
        plan = {"table_title": "回归结果", "models": {"beginner": [
            {"module": "main_effect", "code": "H1", "model_key": "m1", "status": "historical_c"},
            {"module": "moderation_effect", "code": "W1", "model_key": "m2", "status": reproduction.SUPPLEMENTAL}]}}
        rows = [{"hypothesis": code, "model_key": key, "term": term, "beta": "0.25", "se": "0.10",
                 "p": "0.02", "N": "100", "r2": "0.3"}
                for code, key, term in (("H1", "m1", "x"), ("W1", "m2", "xw"))]
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

    def test_mediation_paths_keep_separate_models_and_samples(self) -> None:
        context = mediation_context()
        exact = reproduction.historical_models(context)
        self.assertEqual([m["path_type"] for m in exact], ["x_to_y", "x_to_m", "x_m_to_y"])
        self.assertEqual([m["N"] for m in exact], [90, 80, 70])
        self.assertEqual([m["processing"]["threshold"]["core_variables"] for m in exact],
                         [["x", "y"], ["x", "m"], ["x", "m", "y"]])
        self.assertEqual([len(m["reference"]) for m in exact], [1, 1, 2])
        self.assertEqual({r["term"]: r["beta"] for r in exact[2]["reference"]},
                         {"m": 0.2, "x": 0.15})
        beginner = reproduction.beginner_models(context, exact, "all_enabled")
        self.assertEqual([m["path_type"] for m in beginner], ["x_to_y", "x_to_m", "x_m_to_y"])
        do = reproduction.build_do(exact, context, exact=True, output_dir=Path("/tmp/output"))
        self.assertEqual(do.count('use "$REPRO_INPUT", clear'), 3)
        for index, (path, n) in enumerate((("x_to_y", 90), ("x_to_m", 80), ("x_m_to_y", 70)), 1):
            self.assertIn(f"assert _N == {n}", do)
            self.assertIn(f'post __results ("m{index}") ("M1") ("{path}")', do)
        self.assertIn('esttab __m1 __m2 __m3 using "$REPRO_OUTPUT/exact_mediation_effect.rtf"', do)

        rows = [{"model_key": model["model_key"], "hypothesis": "M1", "path_type": model["path_type"],
                 "term": term, "beta": "0.2", "se": "0.05", "p": "0.01",
                 "N": str(model["N"]), "r2": "0.4"}
                for model in exact for term in model["focal"]]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mediation.xlsx"
            reproduction.paper_table(path, {"table_title": "回归结果", "models": {"exact": exact}}, "exact", rows)
            book = load_workbook(path, read_only=True, data_only=True)
            try:
                sheet = book["中介效应"]
                self.assertIn("总效应", sheet["B2"].value)
                self.assertIn("X→M", sheet["C2"].value)
                self.assertIn("X+M→Y", sheet["D2"].value)
            finally:
                book.close()

    def test_mediation_without_c_can_make_marked_beginner_supplements(self) -> None:
        context = mediation_context(stage="AB")
        self.assertEqual(reproduction.historical_models(context), [])
        beginner = reproduction.beginner_models(context, [], ["M1"])
        self.assertEqual(len(beginner), 3)
        self.assertTrue(all(m["status"] == reproduction.SUPPLEMENTAL for m in beginner))
        self.assertEqual([m["core"] for m in beginner],
                         [["x", "y"], ["x", "m"], ["x", "m", "y"]])
        do = reproduction.build_do(beginner, context, exact=False, output_dir=Path("/tmp/output"))
        self.assertIn("reghdfe m x ctrl, absorb(id year)", do)
        self.assertIn("reghdfe y x m ctrl, absorb(id year)", do)

    def test_omitted_control_is_labeled_in_paper_table(self) -> None:
        plan = {"table_title": "回归结果", "models": {"beginner": [
            {"module": "main_effect", "code": "H1", "model_key": "m1", "status": "historical_c"}]}}
        rows = [{"model_key": "m1", "hypothesis": "H1", "term": "ctrl", "beta": "0",
                 "se": "0", "p": "", "N": "100", "r2": "0.3"}]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "omitted.xlsx"
            reproduction.paper_table(path, plan, "beginner", rows)
            book = load_workbook(path, read_only=True, data_only=True)
            try:
                self.assertEqual(book["主效应"]["B3"].value, "省略")
                self.assertEqual(book["主效应"]["B4"].value, "（共线）")
            finally:
                book.close()

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
