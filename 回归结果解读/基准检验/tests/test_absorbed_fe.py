"""Numerical equivalence checks for the absorbed fixed-effect estimator."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np
import pandas as pd
import statsmodels.formula.api as smf

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / "skill"))

from _internal.absorbed_fe import fit_absorbed_ols  # noqa: E402


def panel_data() -> pd.DataFrame:
    rng = np.random.default_rng(1701)
    rows = []
    for firm in range(18):
        firm_effect = rng.normal()
        for year in range(2015, 2022):
            if (firm, year) in {(0, 2015), (1, 2016), (2, 2017),
                                (3, 2018), (4, 2019), (5, 2020)}:
                continue
            x, control = rng.normal(size=2)
            outcome = 1.7 * x - .45 * control + firm_effect + .3 * (year - 2015) + rng.normal(scale=.2)
            rows.append((f"firm_{firm}", year, x, control, outcome))
    return pd.DataFrame(rows, columns=["firm", "year", "x", "control", "y"])


class AbsorbedFixedEffectTests(unittest.TestCase):
    def test_two_way_unbalanced_panel_matches_formula_ols_and_cluster_inference(self):
        frame = panel_data()
        formula = 'Q("y") ~ Q("x") + Q("control") + C(Q("firm")) + C(Q("year"))'
        fe_terms = ['C(Q("firm"))', 'C(Q("year"))']

        for cluster in (None, "firm"):
            with self.subTest(cluster=cluster):
                if cluster:
                    reference = smf.ols(formula, frame).fit(
                        cov_type="cluster", cov_kwds={"groups": frame[cluster]})
                else:
                    reference = smf.ols(formula, frame).fit()
                absorbed = fit_absorbed_ols(
                    frame, formula, fixed_effect_terms=fe_terms,
                    fixed_effect_columns=["firm", "year"], cluster_column=cluster)

                self.assertIsNotNone(absorbed)
                names = ['Q("x")', 'Q("control")']
                np.testing.assert_allclose(absorbed.params[names], reference.params[names], rtol=1e-10, atol=1e-10)
                np.testing.assert_allclose(absorbed.bse[names], reference.bse[names], rtol=1e-8, atol=1e-10)
                np.testing.assert_allclose(absorbed.pvalues[names], reference.pvalues[names], rtol=1e-8, atol=1e-12)
                self.assertAlmostEqual(absorbed.rsquared, reference.rsquared, places=11)
                self.assertAlmostEqual(absorbed.rsquared_adj, reference.rsquared_adj, places=11)
                self.assertEqual(absorbed.df_resid, reference.df_resid)
                if cluster:
                    self.assertEqual(absorbed.df_resid_inference, reference.df_resid_inference)

    def test_moderation_formula_namespace_is_supported(self):
        from patsy import EvalEnvironment
        from patsy.builtins import C, Q

        frame = panel_data()
        namespace = {"__stata_quote__": Q, "__stata_cat__": C}
        env = EvalEnvironment.capture(0).with_outer_namespace(namespace)
        formula = ('__stata_quote__("y") ~ __stata_quote__("x") + '
                   '__stata_cat__(__stata_quote__("firm")) + '
                   '__stata_cat__(__stata_quote__("year"))')
        fe_terms = ['__stata_cat__(__stata_quote__("firm"))',
                    '__stata_cat__(__stata_quote__("year"))']
        reference = smf.ols(formula, frame, eval_env=env).fit()
        absorbed = fit_absorbed_ols(
            frame, formula, fixed_effect_terms=fe_terms,
            fixed_effect_columns=["firm", "year"], eval_env=env)
        self.assertIsNotNone(absorbed)
        np.testing.assert_allclose(absorbed.params['__stata_quote__("x")'],
                                   reference.params['__stata_quote__("x")'], rtol=1e-10, atol=1e-10)

    def test_rank_deficient_regressor_requests_formula_fallback(self):
        frame = panel_data()
        frame["x_firm_constant"] = frame["firm"].str.rsplit("_", n=1).str[-1].astype(int)
        formula = ('Q("y") ~ Q("x_firm_constant") + C(Q("firm")) + C(Q("year"))')
        result = fit_absorbed_ols(
            frame, formula, fixed_effect_terms=['C(Q("firm"))', 'C(Q("year"))'],
            fixed_effect_columns=["firm", "year"])
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
