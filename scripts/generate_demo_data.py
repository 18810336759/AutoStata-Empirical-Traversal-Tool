"""Generate a deterministic, entirely synthetic panel for the public demo."""

from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "回归结果解读" / "基准检验" / "data" / "演示数据.dta"


def main() -> None:
    rng = np.random.default_rng(20261116)
    firms, years = 100, np.arange(2018, 2024)
    n = firms * len(years)
    firm_id = np.repeat(np.arange(1, firms + 1), len(years))
    year = np.tile(years, firms)
    firm_effect = np.repeat(rng.normal(0, 0.15, firms), len(years))
    investment = rng.lognormal(2.0, 0.35, n)
    assets = rng.lognormal(4.0, 0.3, n)
    management = rng.uniform(0.01, 0.35, n)
    patents = 0.8 * investment + rng.normal(0, 1.5, n) + 4
    outcome = (0.5 + 0.07 * investment - 0.003 * investment**2
               + 0.025 * patents + 0.12 * management + firm_effect
               + rng.normal(0, 0.3, n))
    frame = pd.DataFrame({
        "stkcd": firm_id, "year": year,
        "ShortName": [f"DEMO_{i:03d}" for i in firm_id],
        "研发投入金额": investment, "总资产收益率": outcome, "创新专利": patents,
        "高管持股": management, "总资产": assets,
        "企业年龄": (year - 1995 - firm_id % 18).astype(float),
        "监管层持股": rng.uniform(0, 0.3, n),
        "董事会持股": rng.uniform(0, 0.3, n),
        "监事会持股": rng.uniform(0, 0.25, n),
        "流动比率": rng.uniform(0.5, 4, n),
        "存货周转率": rng.uniform(0.5, 10, n),
        "资产负债率": rng.uniform(0.1, 0.8, n),
        "营业毛利率": rng.uniform(0.05, 0.65, n),
        "营业成本率": rng.uniform(0.2, 0.9, n),
        "销售费用率": rng.uniform(0.01, 0.35, n),
        "管理费用率": rng.uniform(0.01, 0.3, n),
        "财务费用率": rng.uniform(0.005, 0.2, n),
    })
    for name in ("研发投入金额", "创新专利", "流动比率"):
        frame.loc[rng.choice(n, size=12, replace=False), name] = np.nan
    DEST.parent.mkdir(parents=True, exist_ok=True)
    frame.to_stata(DEST, write_index=False, version=118)
    print(f"已生成纯合成演示面板：{DEST}，{n} 行、{len(frame.columns)} 列。")


if __name__ == "__main__":
    main()
