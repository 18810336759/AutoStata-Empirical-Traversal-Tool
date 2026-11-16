"""Check that the authorized example panel shipped with the repository is usable."""

import unittest
from pathlib import Path

import pandas as pd


DATA = Path(__file__).resolve().parents[1] / "data" / "测试数据.dta"


class BundledDataTests(unittest.TestCase):
    def test_authorized_example_panel_is_readable_and_unique(self) -> None:
        self.assertTrue(DATA.is_file(), "发布包缺少已授权的测试数据.dta")
        data = pd.read_stata(DATA, convert_categoricals=False)
        self.assertGreater(len(data), 0)
        self.assertTrue({"stkcd", "year", "研发投入金额", "总资产收益率"} <= set(data.columns))
        self.assertFalse(data[["stkcd", "year"]].isna().any().any())
        self.assertFalse(data.duplicated(["stkcd", "year"]).any())


if __name__ == "__main__":
    unittest.main()
