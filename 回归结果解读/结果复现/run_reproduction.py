"""Generate auditable Stata reproductions for one synthesis specification.

Default: generate reviewable do-files only. --collect reads Stata's exported
estimates and creates paper tables after the researcher has run those files.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, Side


HERE = Path(__file__).resolve().parent
MODES = {"both", "exact", "beginner"}
SUPPLEMENTAL = "定向补充复测"
EFFECT_TITLES = {"main_effect": "主效应", "mediation_effect": "中介效应",
                 "moderation_effect": "调节效应"}


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def synthesis_source(config_path: Path, config: dict) -> tuple[Path, Path]:
    value = config["source"].get("synthesis_file", config["source"].get("synthesis_dir"))
    require(isinstance(value, str) and bool(value.strip()), "请设置 source.synthesis_file")
    path = (config_path.parent / value).resolve()
    if path.is_dir():
        path = path / "merged_full_results.csv"
    require(path.name in {"merged_full_results.xlsx", "merged_full_results.csv"}
            and path.parent.name == "result_synthesis" and path.is_file(),
            f"请填写 result_synthesis 内的 merged_full_results.xlsx 或 .csv 文件路径: {path}")
    return path, path.parent


def read_synthesis(path: Path) -> list[dict[str, str]]:
    if path.suffix == ".csv":
        return read_csv(path)
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = workbook.active.values
        headers = next(rows)
        require(all(isinstance(header, str) for header in headers), "综合 Excel 表头无效")
        return [{header: "" if value is None else str(value)
                 for header, value in zip(headers, row)} for row in rows]
    finally:
        workbook.close()


def names(value: Any) -> list[str]:
    if isinstance(value, str):
        return value.split()
    return list(value or [])


def unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def file_from_snapshot(module_dir: Path, record: dict, name: str) -> Path:
    item = record["snapshots"][name]
    path = module_dir / item["path"]
    require(path.is_file() and digest(path) == item["sha256"], f"快照校验失败: {path}")
    return path


def load_context(config_path: Path) -> tuple[dict, dict]:
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    require(config.get("config_version") == "1.0", "不支持的 reproduction.yaml 版本")
    require(config.get("versions") in MODES, "versions 须为 both/exact/beginner")
    require(config.get("exact", {}).get("scope") == "historical_c_only", "精确版仅支持 historical_c_only")
    simple = config["beginner"]
    require((simple["interpolation"], simple["winsorize"], simple["estimator"])
            == ("ipolate_epolate", "winsor2", "reghdfe"), "当前仅支持 YAML 指定的简明命令组合")
    require(not config["execution"]["run_stata_automatically"]
            and not config["execution"]["install_stata_packages_automatically"],
            "当前版本不自动运行 Stata 或安装扩展")
    source_file, synthesis = synthesis_source(config_path, config)
    sample_dir = synthesis.parent
    baseline = synthesis.parents[3]
    run_id = str(config["selection"]["run_id"])
    spec_id = str(config["selection"]["specification_id"])
    require(bool(re.fullmatch(r"[A-Za-z0-9_-]+", run_id))
            and bool(re.fullmatch(r"spec_[a-f0-9]+", spec_id)), "run_id/specification_id 格式无效")
    matched = [r for r in read_synthesis(source_file)
               if r["run_id"] == run_id and r["specification_id"] == spec_id]
    require(len(matched) == 1, "综合 full 表中必须恰好匹配一条 run_id + specification_id")
    row = matched[0]
    manifest_path = sample_dir.parent / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    batch = manifest["batches"].get(run_id)
    require(batch is not None, "运行清单中找不到该批次")
    require(batch.get("common_fingerprint") == row["common_fingerprint"], "公共设定指纹不一致")
    ids = json.loads(row["model_ids_json"])
    require(isinstance(ids, dict), "model_ids_json 格式错误")

    contexts = {}
    data_file = None
    for module in ("main_effect", "mediation_effect", "moderation_effect"):
        if not ids.get(module) and module != "main_effect" and module != "moderation_effect":
            continue
        module_dir = sample_dir / module
        if not module_dir.is_dir():
            continue
        record = json.loads((module_dir / "run_record.json").read_text(encoding="utf-8"))
        entry = batch["modules"].get(module, {})
        require(entry.get("status") == "success" and record["run_id"] == run_id
                and record["run_instance_id"] == entry.get("run_instance_id"),
                f"{module} 不属于目标成功批次")
        require(record["data_sha256"] == row["data_sha256"], f"{module} 数据指纹不一致")
        global_cfg = yaml.safe_load(file_from_snapshot(module_dir, record, "analysis_config.yaml").read_text(encoding="utf-8"))
        module_cfg = yaml.safe_load(file_from_snapshot(module_dir, record, f"{module}.yaml").read_text(encoding="utf-8"))
        candidate_data = Path(record.get("data_file") or baseline / global_cfg["data"]["input_file"])
        if not candidate_data.is_absolute():
            candidate_data = baseline / candidate_data
        require(candidate_data.is_file() and digest(candidate_data) == row["data_sha256"],
                f"{module} 原始数据不存在或 SHA-256 已变化: {candidate_data}")
        if data_file is not None:
            require(data_file.resolve() == candidate_data.resolve(), "各模块原始数据文件不同")
        data_file = candidate_data
        listed = {r["path"]: r["sha256"] for r in record["files"]}
        for filename in ("standardized_results.csv", "specification_index.csv", "hypothesis_results.csv",
                         "preprocessing_definitions.json"):
            path = module_dir / filename
            require(path.is_file() and listed.get(filename) == digest(path), f"{module} 机器文件校验失败: {filename}")
        standard = read_csv(module_dir / "standardized_results.csv")
        contexts[module] = {
            "dir": module_dir, "record": record, "global": global_cfg, "module": module_cfg,
            "standard": standard, "index": read_csv(module_dir / "specification_index.csv"),
        }
    require(bool(contexts), "找不到可复现的效应模块")
    return config, {"synthesis": synthesis, "sample_dir": sample_dir, "baseline": baseline,
                    "row": row, "model_ids": ids, "modules": contexts, "data_file": data_file}


def standard_row(context: dict, model_id: str) -> dict:
    rows = [r for r in context["standard"] if r["model_id"] == model_id and r.get("检验阶段") in ("C", "C完整检验")]
    require(bool(rows), f"找不到 C 阶段模型: {model_id}")
    return rows[0]


def historical_models(context: dict) -> list[dict]:
    out = []
    for module, ids in context["model_ids"].items():
        require(module != "mediation_effect" or not ids, "第一版暂不支持中介模型，请勿套用主效应模板")
        if not ids:
            continue
        require(module in context["modules"], f"找不到模块结果: {module}")
        source = context["modules"][module]
        for model_id in ids:
            rows = [r for r in source["index"] if r["model_id"] == model_id
                    and r["specification_id"] == context["row"]["specification_id"]]
            require(bool(rows), f"specification_index 无对应模型: {model_id}")
            base = rows[0]
            st = standard_row(source, model_id)
            model_terms = json.loads(base["模型项定义"])
            processing = json.loads(st["processing_definition_json"])
            require(processing["profile"] == context["row"]["处理方案"], "处理方案不一致")
            out.append({"code": base["假设编号"], "module": module, "model_id": model_id,
                        "status": "historical_c", "N": int(base["样本量（N）"]),
                        "sample_identity": base["sample_identity"], "terms": model_terms,
                        "processing": processing, "processing_definition_id": base["processing_definition_id"],
                        "focal": model_terms["focal_terms"], "controls": model_terms["controls"],
                        "reference": [{"term": r["检验项"], "beta": float(r["beta"]),
                                       "se": float(r["se"]), "p": float(r["p"])}
                                      for r in source["standard"] if r["model_id"] == model_id
                                      and r["检验项"] in model_terms["focal_terms"]]})
    require(bool(out), "该设定没有历史 C 阶段模型")
    require(len({m["code"] for m in out}) == len(out), "首版只支持每个假设一条 C 模型")
    return out


def beginner_models(context: dict, exact: list[dict], selection: Any) -> list[dict]:
    by_code = {m["code"]: m for m in exact}
    enabled: dict[str, tuple[str, dict]] = {}
    for module, source in context["modules"].items():
        cfg = source["module"]
        if module == "main_effect":
            hypotheses = cfg.get("hypotheses", [])
        elif module == "moderation_effect":
            hypotheses = cfg.get("moderation_paths", [])
        else:
            continue
        for item in hypotheses:
            if item.get("enabled", False):
                ident = str(item["id"])
                require(ident not in enabled, f"假设 ID 重复: {ident}")
                enabled[ident] = (module, item)
    selected = list(enabled) if selection == "all_enabled" else selection
    require(isinstance(selected, list) and selected and all(isinstance(x, str) for x in selected),
            "beginner.hypotheses 须为 all_enabled 或非空假设 ID 列表")
    require(len(selected) == len(set(selected)), "beginner.hypotheses 有重复 ID")
    require(not set(selected) - set(enabled), f"未启用或未知的假设: {sorted(set(selected)-set(enabled))}")
    out = []
    for code in selected:
        module, hyp = enabled[code]
        source = context["modules"][module]
        if code in by_code:
            model = dict(by_code[code])
        else:
            core, _, _ = hypothesis_parts(module, hyp)
            global_cfg = source["global"]
            optional = names(global_cfg["controls"]["optional_controls"])
            required = names(global_cfg["controls"]["required_controls"])
            candidates = unique(core + required + optional)
            ab = [r for r in source["standard"] if r["假设编号"] == code
                  and r["样本门槛"] == context["row"]["样本门槛"]
                  and r["处理方案"] == context["row"]["处理方案"]
                  and r.get("检验阶段") in ("AB", "A/B初筛")]
            require(bool(ab), f"缺少 {code} 的同门槛/处理方案 A/B 预处理定义")
            recipe = copy.deepcopy(json.loads(ab[0]["processing_definition_json"]))
            require(recipe["threshold"]["core_variables"] == core, f"{code} 核心变量与历史快照不一致")
            continuous = global_cfg["continuous_variable_settings"]
            allowed = set(names(continuous["continuous_variables"])) - set(names(continuous["exclude_variables"]))
            recipe["processing_candidates"] = candidates
            for key, allow_key in (("interpolation", "interpolate_allowed"), ("winsorize", "winsorize_allowed")):
                block = recipe[key]
                block["variables"] = ([v for v in candidates if v in allowed and v in set(names(continuous[allow_key]))]
                                      if block["enabled"] else [])
            model = {"code": code, "module": module, "model_id": None, "status": SUPPLEMENTAL,
                     "N": None, "sample_identity": None, "terms": {}, "processing": recipe,
                     "focal": [], "controls": names(context["row"]["纳入的控制变量"]), "reference": []}
        core, outcome, form = hypothesis_parts(module, hyp)
        model["core"] = core
        model["outcome"] = outcome
        model["form"] = form
        out.append(model)
    return out


def hypothesis_parts(module: str, hyp: dict) -> tuple[list[str], str, str]:
    if module == "main_effect":
        xs = [x["name"] for x in hyp["x"]]
        require(bool(xs) and len(xs) == len(set(xs))
                and (hyp["model_form"] == "linear" or
                     (hyp["model_form"] in ("u_shape", "inverted_u_shape") and len(xs) == 1)),
                f"暂不支持主效应形式: {hyp['id']}")
        return [hyp["y"], *xs], hyp["y"], hyp["model_form"]
    rel = hyp["focal_relationship"]
    require(rel["form"] in ("linear", "u_shape", "inverted_u_shape")
            and hyp["moderation"]["form"] == "linear",
            f"暂不支持调节形式: {hyp['id']}")
    return [rel["from"], rel["to"], hyp["moderator"]], rel["to"], rel["form"]


def validate_recipe(recipe: dict, *, exact: bool) -> None:
    threshold = recipe["threshold"]
    require(threshold.get("entity") and threshold.get("year") and threshold.get("core_variables"), "缺少样本门槛定义")
    interpolation = recipe["interpolation"]
    winsor = recipe["winsorize"]
    require(interpolation["method"] == "linear" and interpolation["scope"] == "within_entity", "不支持该插值方法")
    require(winsor["method"] == "winsorize" and winsor["scope"] == "full_sample", "首版只支持全样本缩尾")
    if exact:
        require(interpolation["limit_direction"] == "both" and interpolation["max_gap_calendar_years"] is None,
                "精确版暂不支持该边界插值或最大缺口限制")
        require(winsor["quantile_interpolation"] == "linear", "精确版分位点算法不支持")


def q(path: Path) -> str:
    value = str(path)
    require('"' not in value and "\n" not in value, f"Stata 路径含非法字符: {path}")
    return value.replace("\\", "/")


def stata_prep(recipe: dict, *, exact: bool) -> list[str]:
    validate_recipe(recipe, exact=exact)
    th, ip, wi = recipe["threshold"], recipe["interpolation"], recipe["winsorize"]
    entity, year, core = th["entity"], th["year"], th["core_variables"]
    lines = [
        "* 1. 从未经本 do 修改的输入重新开始；输入已由研究者按年份/ST 等规则筛好。",
        'use "$REPRO_INPUT", clear',
        f"assert !missing({entity}, {year})", f"isid {entity} {year}",
        "* 2. 插值前按核心变量筛企业：至少有门槛数个原始有效年份。",
        f"egen byte __core_n = rownonmiss({' '.join(core)})",
        f"gen byte __core_ok = __core_n == {len(core)}",
        f"bysort {entity}: egen int __valid_n = total(__core_ok)",
        f"keep if __valid_n >= {int(th['minimum_distinct_valid_years'])}",
        "drop __core_n __core_ok __valid_n",
    ]
    if ip["enabled"] and ip["variables"]:
        if exact:
            lines += ["* 3. 企业内按真实年份插值；两端缺失按 Python 规则用最近已知值。",
                      f"foreach v in {' '.join(ip['variables'])} {{", "    recast double `v'",
                      "    tempvar ly lv ry rv",
                      f"    bysort {entity} ({year}): gen double `ly' = {year} if !missing(`v')",
                      f"    by {entity}: replace `ly' = `ly'[_n-1] if missing(`ly') & _n > 1",
                      f"    bysort {entity} ({year}): gen double `lv' = `v' if !missing(`v')",
                      f"    by {entity}: replace `lv' = `lv'[_n-1] if missing(`lv') & _n > 1",
                      f"    gsort {entity} -{year}",
                      f"    by {entity}: gen double `ry' = {year} if !missing(`v')",
                      f"    by {entity}: replace `ry' = `ry'[_n-1] if missing(`ry') & _n > 1",
                      f"    by {entity}: gen double `rv' = `v' if !missing(`v')",
                      f"    by {entity}: replace `rv' = `rv'[_n-1] if missing(`rv') & _n > 1",
                      f"    replace `v' = `lv' + (`rv' - `lv') * ({year} - `ly') / (`ry' - `ly') ///",
                      "        if missing(`v') & !missing(`ly', `ry')",
                      "    replace `v' = `lv' if missing(`v') & !missing(`lv')",
                      "    replace `v' = `rv' if missing(`v') & !missing(`rv')",
                      "    drop `ly' `lv' `ry' `rv'", "}"]
        else:
            lines += ["* 3. 简明演示：ipolate 插内部缺口，epolate 对两端按斜率外推。",
                      "* 注意：边界外推与 Python 的最近值填充不同。"]
            for i, var in enumerate(ip["variables"], 1):
                lines += [f"recast double {var}",
                          f"bysort {entity} ({year}): ipolate {var} {year}, gen(__ip{i}) epolate",
                          f"replace {var} = __ip{i} if missing({var})", f"drop __ip{i}"]
    if wi["enabled"] and wi["variables"]:
        lo, hi = 100 * float(wi["lower_quantile"]), 100 * float(wi["upper_quantile"])
        if exact:
            lines += ["* 4. 门槛后全样本缩尾：按 pandas 的线性分位点位置 1+(N-1)*p。",
                      f"foreach v in {' '.join(wi['variables'])} {{", "    recast double `v'",
                      "    sort `v'", "    quietly count if !missing(`v')", "    local n = r(N)",
                      "    if `n' > 0 {", "        tempname lo hi", "        if `n' == 1 {",
                      "            scalar `lo' = `v'[1]", "            scalar `hi' = `v'[1]", "        }", "        else {"]
            for label, pct in (("lo", lo / 100), ("hi", hi / 100)):
                lines += [f"            local pos{label} = (`n' - 1) * {pct:.17g}",
                          f"            local i{label} = floor(`pos{label}') + 1",
                          f"            local f{label} = `pos{label}' - floor(`pos{label}')",
                          f"            scalar `{label}' = (1 - `f{label}') * `v'[`i{label}'] + `f{label}' * `v'[`i{label}' + 1]"]
            lines += ["        }", "        replace `v' = `lo' if !missing(`v') & `v' < `lo'",
                      "        replace `v' = `hi' if !missing(`v') & `v' > `hi'", "    }", "}"]
        else:
            require(lo == round(lo) and hi == round(hi), "winsor2 简明版仅支持整数百分位")
            lines += ["* 4. 简明演示：winsor2 将极端值压到分位点，不删除观测。",
                      "* 注意：winsor2 的分位点算法可能不同于 Python。",
                      f"winsor2 {' '.join(wi['variables'])}, cuts({int(lo)} {int(hi)}) replace"]
    return lines


def exact_terms(model: dict) -> tuple[list[str], list[str]]:
    terms = model["terms"]
    generated = terms.get("generated_terms", {})
    lines = ["* 5. 由处理后的原变量生成历史模型项；中心化常数来自当次模型快照。"]
    pending = dict(generated)
    ordered = []
    while pending:
        ready = [name for name, expr in pending.items()
                 if not any(other != name and other in expr for other in pending)]
        require(bool(ready), f"生成项存在循环或未知依赖: {list(pending)}")
        for name in ready:
            ordered.append((name, pending.pop(name)))
    for name, expression in ordered:
        require(re.fullmatch(r"[\w\u4e00-\u9fff]+", name) is not None, f"非法生成项名: {name}")
        require(re.fullmatch(r"[\w\u4e00-\u9fff. +*()\-]+", expression) is not None,
                f"不支持的生成项表达式: {expression}")
        lines.append(f"gen double {name} = {expression.replace('**', '^')}")
    return lines, terms["focal_terms"]


def beginner_terms(model: dict, source_cfg: dict) -> tuple[list[str], list[str]]:
    module = model["module"]
    code = model["code"]
    if module == "main_effect":
        hyp = next(h for h in source_cfg["hypotheses"] if h["id"] == code)
        xs = [item["name"] for item in hyp["x"]]
        if model["form"] == "linear":
            return ["* 5. 线性主效应：全部核心解释变量同时进入回归。"], xs
        x = xs[0]
        shape = "倒 U 型" if model["form"] == "inverted_u_shape" else "U 型"
        return [f"* 5. {shape}主效应：缩尾后再计算平方项；一次项不能省略。",
                f"gen double __x_sq = {x}^2"], [x, "__x_sq"]
    hyp = next(h for h in source_cfg["moderation_paths"] if h["id"] == code)
    x = hyp["focal_relationship"]["from"]
    w = hyp["moderator"]
    lines = ["* 5. 调节项：按本模型门槛和预处理后的均值中心化，再相乘。",
             f"quietly summarize {x}, meanonly", f"gen double __x_c = {x} - r(mean)",
             f"quietly summarize {w}, meanonly", f"gen double __w_c = {w} - r(mean)",
             "gen double __xw = __x_c * __w_c"]
    terms = ["__x_c", "__w_c", "__xw"]
    if model["form"] in ("u_shape", "inverted_u_shape"):
        shape = "倒 U 型" if model["form"] == "inverted_u_shape" else "U 型"
        lines += [f"* 调节{shape}关系还需平方项及平方项与调节变量的交互。",
                  "gen double __x_sq = __x_c^2", "gen double __x2w = __x_sq * __w_c"]
        terms += ["__x_sq", "__x2w"]
    return lines, terms


def stata_model(model: dict, input_file: Path, *, exact: bool, module_cfg: dict) -> list[str]:
    recipe = model["processing"]
    terms_info = model["terms"]
    controls = model["controls"]
    generated, focal = exact_terms(model) if exact else beginner_terms(model, module_cfg)
    outcome = terms_info["outcome"] if exact else model["outcome"]
    fe = terms_info["fixed_effects"] if exact else {
        "entity": recipe["threshold"]["entity"], "time": recipe["threshold"]["year"]}
    require(fe["entity"] and fe["time"], "首版只支持双向固定效应")
    needed = unique([outcome] + focal + controls + [fe["entity"], fe["time"]])
    code = model["code"]
    lines = ["", f"* ===== {code}: {model['status']} =====",
             f"* 原始输入: {input_file.name}；每条假设独立从同一原始文件开始。"]
    lines += stata_prep(recipe, exact=exact)
    lines += generated
    lines += ["* 6. 完整案例：只留下本条公式中全部变量都非缺失的行。",
              f"egen byte __miss_model = rowmiss({' '.join(needed)})",
              "keep if __miss_model == 0", "drop __miss_model"]
    if exact:
        lines += [f"* 历史 N={model['N']}；不一致时停止，不把不同样本称为精确复现。",
                  f"assert _N == {model['N']}"]
    if exact:
        regressors = " ".join(focal + controls + [f"i.{fe['entity']}", f"i.{fe['time']}"])
        lines += ["* 7. 普通 OLS + 双向虚拟变量；标准误与历史未聚类设定一致。",
                  f"regress {outcome} {regressors}"]
    else:
        regressors = " ".join(focal + controls)
        lines += ["* 7. 用 reghdfe 吸收双向固定效应；不要额外加 robust/cluster。",
                  f"reghdfe {outcome} {regressors}, absorb({fe['entity']} {fe['time']})"]
    lines.append(f"estimates store __m{model['table_index']}")
    lines += ["* 8. 将实际估计系数、标准误、p、N 和 R²写成机器可读结果。",
              "* post 依赖文件开头的 postfile；请从头运行整份 do，勿只运行本段。",
              "* 论文三线表只会从这份实际运行数据生成，不从通过标签填数。"]
    report_terms = unique(focal + controls)
    for term in report_terms:
        lines.append(f'post __results ("{code}") ("{term}") (_b[{term}]) (_se[{term}]) '
                     f'(2*ttail(e(df_r), abs(_b[{term}]/_se[{term}]))) (e(N)) (e(r2))')
    lines += [f'display as result "{code}: N=" e(N) " ; R2=" %9.6f e(r2)']
    if exact:
        lines += ["* 9. 关键系数与 Python 历史值核对；通过不等于自动证明因果。"]
        for ref in model["reference"]:
            tolerance = max(1e-7, abs(ref["beta"]) * 1e-6)
            lines.append(f"assert abs(_b[{ref['term']}] - ({ref['beta']:.17g})) < {tolerance:.17g}")
    return lines


def build_do(models: list[dict], context: dict, *, exact: bool, output_dir: Path) -> str:
    mode = "exact" if exact else "beginner"
    input_file = context["data_file"]
    require(input_file is not None, "找不到原始数据")
    lines = ["version 17.0", "clear all", "set more off", "set varabbrev off", "",
             f"* {mode} / {context['row']['specification_id']} / {context['row']['run_id']}",
             "* 运行前核对原始数据与方案；不要把简明补跑覆盖进历史 synthesis。",
             "* 必须从头运行整份 do：postfile 在开头建立句柄，post 在各模型后写入。",
             "* 本输入已经过研究者前置样本筛选；样本标签不是 Stata 筛选条件。",
             f'global REPRO_INPUT "{q(input_file)}"',
             f'global REPRO_OUTPUT "{q(output_dir)}"',
             'capture log close', f'log using "$REPRO_OUTPUT/{mode}.log", text replace',
             f'postfile __results str16 hypothesis str80 term double beta se p N r2 using "$REPRO_OUTPUT/estimates_{mode}.dta", replace']
    if not exact:
        lines += ["* winsor2/reghdfe 是外部扩展；缺少时停止，请用户自行确认安装。",
                  "capture which winsor2", 'if _rc {', '    display as error "缺少 winsor2；请先审阅并自行安装。"', '    exit 499', '}',
                  "capture which reghdfe", 'if _rc {', '    display as error "缺少 reghdfe；请先审阅并自行安装。"', '    exit 499', '}']
    for index, model in enumerate(models, 1):
        model["table_index"] = index
        cfg = context["modules"][model["module"]]["module"]
        lines += stata_model(model, input_file, exact=exact, module_cfg=cfg)
    lines += ["", "* 所有模型完成后再导出；出错中断时不能误用半套估计结果。",
              "postclose __results", f'use "$REPRO_OUTPUT/estimates_{mode}.dta", clear',
              f'export delimited using "$REPRO_OUTPUT/estimates_{mode}.csv", replace']
    lines += ["", "* 按效应类别导出合并 RTF 表；esttab 属于 estout 扩展，未安装时不影响机器结果。",
              "capture which esttab", "if _rc {",
              '    display as text "未安装 esttab：跳过 Stata RTF；运行 Python --collect 可生成三张 Excel 表。"',
              "}", "else {"]
    for module, title in EFFECT_TITLES.items():
        selected = [model for model in models if model["module"] == module]
        if not selected:
            lines.append(f'    display as text "{title}：本设定未纳入可复现模型。"')
            continue
        stored = " ".join(f"__m{model['table_index']}" for model in selected)
        terms = unique([term for model in selected for term in
                        (model["focal"] if exact else beginner_terms(
                            model, context["modules"][model["module"]]["module"])[1]) + model["controls"]])
        titles = " ".join(f'"{model["code"]}"' for model in selected)
        lines += [f'    capture noisily esttab {stored} using "$REPRO_OUTPUT/{mode}_{module}.rtf", replace ///',
                  f'        mtitles({titles}) keep({" ".join(terms)}) b(%9.3f) se(%9.3f) ///',
                  '        star(* 0.10 ** 0.05 *** 0.01) stats(N r2, fmt(0 3) labels("N" "R-squared")) ///',
                  '        addnotes("Entity and year fixed effects included; standard errors in parentheses.")',
                  f'    if _rc display as error "{title} RTF 导出失败；机器结果已保留，请检查 esttab 日志。"']
    lines += ["}",
              f'display as result "{mode}: 完成 {len(models)} 条模型；请再运行 Python --collect 核验并制表。"',
              "log close", ""]
    return "\n".join(lines)


def generation(config_path: Path) -> Path:
    cfg, ctx = load_context(config_path)
    exact = historical_models(ctx)
    beginner = beginner_models(ctx, exact, cfg["beginner"]["hypotheses"])
    for model in exact:
        validate_recipe(model["processing"], exact=True)
        require(model["terms"].get("cluster") is None, "精确版首批不支持聚类标准误")
    for model in beginner:
        validate_recipe(model["processing"], exact=False)
    out = (ctx["sample_dir"] / cfg["output"]["reproduction_subdir"] /
           ctx["row"]["run_id"] / ctx["row"]["specification_id"])
    require(not out.exists() or bool(cfg["output"]["overwrite_existing"]),
            f"目标目录已存在且 overwrite_existing=false: {out}")
    require(not out.exists() or not any(out.iterdir()), "目标目录已有文件；请换编号或明确处理旧文件")
    mode = cfg["versions"]
    chosen = ({"exact": exact, "beginner": beginner} if mode == "both" else
              {mode: exact if mode == "exact" else beginner})
    files = {f"{key}.do": build_do(value, ctx, exact=(key == "exact"), output_dir=out)
             for key, value in chosen.items()}
    plan = {"schema_version": "1.0", "run_id": ctx["row"]["run_id"],
            "specification_id": ctx["row"]["specification_id"],
            "display_id": ctx["row"]["模型合并ID"], "data_file": str(ctx["data_file"]),
            "data_sha256": ctx["row"]["data_sha256"], "sample_dir": str(ctx["sample_dir"]),
            "table_title": cfg["output"]["table_title"],
            "paper_table_dir": str((config_path.parent / cfg["output"]["paper_table_dir"]).resolve()),
            "modes": list(chosen), "models": chosen,
            "do_sha256": {name: hashlib.sha256(content.encode("utf-8")).hexdigest()
                          for name, content in files.items()},
            "notes": ["精确版仅复现历史 C 模型；简明版新模型不能回填 synthesis。",
                      "简明版 epolate 边界外推、winsor2 分位点和 reghdfe 样本/自由度可能不同于 Python。"]}
    out.mkdir(parents=True, exist_ok=True)
    for filename, content in files.items():
        (out / filename).write_text(content, encoding="utf-8")
    (out / "reproduction_plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "README_结果说明.md").write_text(
        f"# {plan['display_id']} 结果复现\n\n"
        "先审阅 `reproduction_plan.json` 和相应 do 文件，再由研究者在 Stata 中从头执行。\n"
        "执行后运行 `run_reproduction.py --collect` 核验并生成论文表。\n"
        "精确版只覆盖历史 C 模型；简明版的缺 C 假设属于新补跑，不改变 synthesis。\n",
        encoding="utf-8")
    print(f"已生成待审阅 do：{out}")
    print("请先审阅并在 Stata 中执行；执行后再调用 --collect。")
    return out


def collect(config_path: Path) -> None:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    _, synth = synthesis_source(config_path, cfg)
    out = synth.parent / cfg["output"]["reproduction_subdir"] / str(cfg["selection"]["run_id"]) / str(cfg["selection"]["specification_id"])
    plan_path = out / "reproduction_plan.json"
    require(plan_path.is_file(), "请先生成 do 与 reproduction_plan.json")
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    require(digest(Path(plan["data_file"])) == plan["data_sha256"], "原始数据已变化，停止制表")
    table_dir = Path(plan["paper_table_dir"])
    audit = {"run_id": plan["run_id"], "specification_id": plan["specification_id"],
             "data_sha256": plan["data_sha256"], "modes": {}}
    for mode in plan["modes"]:
        do_file = out / f"{mode}.do"
        expected_do_hash = plan.get("do_sha256", {}).get(do_file.name)
        if expected_do_hash:
            require(digest(do_file) == expected_do_hash, f"{mode} do 文件已修改；请重新审阅并执行")
        file = out / f"estimates_{mode}.csv"
        if not file.is_file():
            print(f"{mode}: 尚未看到 Stata 估计文件，跳过论文表：{file}")
            audit["modes"][mode] = {"status": "not_run"}
            continue
        log = out / f"{mode}.log"
        require(log.is_file() and f"{mode}: 完成" in log.read_text(encoding="utf-8", errors="replace"),
                f"{mode} 日志缺少完成标记，不能制表")
        rows = read_csv(file)
        models = plan["models"][mode]
        expected = {(m["code"], t) for m in models for t in unique(
            (m["focal"] if mode == "exact" else beginner_terms(m, yaml.safe_load(
                (synth.parent / m["module"] / "config_snapshot" / f"{m['module']}.yaml").read_text(encoding="utf-8")))[1]) + m["controls"])}
        actual = [(r["hypothesis"], r["term"]) for r in rows]
        require(len(actual) == len(set(actual)) and set(actual) == expected,
                f"{mode} 估计文件缺项、多项或重复，停止制表")
        for r in rows:
            for key in ("beta", "se", "p", "N", "r2"):
                require(r.get(key) not in ("", None), f"{mode} 缺少估计值: {r['hypothesis']} / {key}")
        comparison = []
        if mode == "exact":
            lookup = {(r["hypothesis"], r["term"]): r for r in rows}
            for model in models:
                for ref in model["reference"]:
                    got = lookup[(model["code"], ref["term"])]
                    n_ok = int(float(got["N"])) == model["N"]
                    b_ok = abs(float(got["beta"]) - ref["beta"]) < max(1e-7, abs(ref["beta"]) * 1e-6)
                    se_ok = abs(float(got["se"]) - ref["se"]) < max(1e-7, abs(ref["se"]) * 1e-4)
                    p_ok = abs(float(got["p"]) - ref["p"]) < 1e-4
                    comparison.append({"假设": model["code"], "检验项": ref["term"],
                                       "N一致": n_ok, "系数一致": b_ok, "标准误一致": se_ok, "p值一致": p_ok})
            (out / "comparison_exact.json").write_text(json.dumps(comparison, ensure_ascii=False, indent=2), encoding="utf-8")
            require(all(all(v for k, v in row.items() if k not in ("假设", "检验项")) for row in comparison),
                    "精确版与历史结果不一致；已保存 comparison_exact.json，请先诊断，不生成论文表")
        audit["modes"][mode] = {"status": "verified" if mode == "exact" else "stata_run_review_required",
                                 "model_status": {m["code"]: m["status"] for m in models},
                                 "log_sha256": digest(log), "estimates_sha256": digest(file)}
        if cfg["output"]["paper_tables_after_verification"]:
            table_dir.mkdir(parents=True, exist_ok=True)
            dest = table_dir / f"{plan['display_id']}_{mode}_三类效应.xlsx"
            if dest.exists() and not cfg["output"]["overwrite_existing"]:
                audit["modes"][mode]["paper_table"] = "existing_not_overwritten"
                print(f"论文表已存在，未覆盖也未重新核验旧表：{dest}")
            else:
                paper_table(dest, plan, mode, rows)
                audit["modes"][mode]["paper_table"] = str(dest)
                audit["modes"][mode]["paper_table_sha256"] = digest(dest)
                print(f"已生成论文表：{dest}")
    (out / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")


def paper_table(path: Path, plan: dict, mode: str, rows: list[dict[str, str]]) -> None:
    wb = Workbook()
    for index, (module, title) in enumerate(EFFECT_TITLES.items()):
        ws = wb.active if index == 0 else wb.create_sheet()
        ws.title = title
        models = [model for model in plan["models"][mode] if model["module"] == module]
        if not models:
            ws.cell(1, 1, title)
            ws.cell(2, 1, "本设定未纳入该类可复现模型；无估计结果。")
            ws.column_dimensions["A"].width = 52
            continue
        group_rows = [row for row in rows if row["hypothesis"] in {model["code"] for model in models}]
        fill_effect_sheet(ws, plan, mode, title, models, group_rows)
    wb.save(path)


def fill_effect_sheet(ws: Any, plan: dict, mode: str, title: str,
                      models: list[dict], rows: list[dict[str, str]]) -> None:
    values = {(r["hypothesis"], r["term"]): r for r in rows}
    terms = unique([r["term"] for r in rows])
    ncol = len(models) + 1
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncol)
    ws.cell(1, 1, plan["table_title"] + f" - {title}" +
            ("（精确复现）" if mode == "exact" else "（简明复测）"))
    ws.cell(2, 1, "变量 / 模型")
    for col, model in enumerate(models, 2):
        ws.cell(2, col, f"({col-1}) {model['code']}")
    row_num = 3
    for term in terms:
        ws.cell(row_num, 1, term)
        for col, model in enumerate(models, 2):
            item = values.get((model["code"], term))
            if item:
                beta, p = float(item["beta"]), float(item["p"])
                stars = "***" if p < .01 else "**" if p < .05 else "*" if p < .1 else ""
                digits = 6 if beta != 0 and abs(beta) < .0005 else 3
                ws.cell(row_num, col, f"{beta:.{digits}f}{stars}")
                ws.cell(row_num + 1, col, f"({float(item['se']):.{digits}f})")
        row_num += 2
    for label, getter in (("N", lambda r: int(float(r["N"]))),
                          ("R²", lambda r: round(float(r["r2"]), 3)),
                          ("企业固定效应", lambda _: "是"), ("年份固定效应", lambda _: "是"),
                          ("标准误", lambda _: "普通 OLS" if mode == "exact" else "reghdfe 未聚类"),
                          ("历史状态", lambda _: "历史 C" if mode == "exact" else "简明新运行")):
        ws.cell(row_num, 1, label)
        for col, model in enumerate(models, 2):
            item = next(r for r in rows if r["hypothesis"] == model["code"])
            ws.cell(row_num, col, getter(item) if label != "历史状态" else
                    ("历史 C" if model["status"] == "historical_c" else SUPPLEMENTAL))
        row_num += 1
    note = "*** p<0.01, ** p<0.05, * p<0.1; Source(s): Authors' own work"
    if mode == "beginner":
        note += "; ipolate 边界外推、winsor2 分位点及 reghdfe 口径可能不同于 Python。"
    ws.merge_cells(start_row=row_num, start_column=1, end_row=row_num, end_column=ncol)
    ws.cell(row_num, 1, note)
    thick, thin = Side(style="medium"), Side(style="thin")
    for col in range(1, ncol + 1):
        ws.cell(1, col).border = Border(top=thick)
        ws.cell(2, col).border = Border(bottom=thin)
        ws.cell(row_num - 1, col).border = Border(bottom=thick)
    for line in ws:
        for cell in line:
            cell.font = Font(name="Times New Roman", size=7.5, bold=cell.row <= 2)
            cell.alignment = Alignment(vertical="center", horizontal="left" if cell.column == 1 else "center")
    ws.column_dimensions["A"].width = 29
    for col in range(2, ncol + 1):
        ws.column_dimensions[ws.cell(2, col).column_letter].width = 18
    ws.freeze_panes = "B3"
    ws.sheet_view.showGridLines = False


def main() -> None:
    parser = argparse.ArgumentParser(description="为一条 synthesis 设定生成 Stata 复现 do 或收集论文表")
    parser.add_argument("--config", type=Path, default=HERE / "reproduction.yaml")
    parser.add_argument("--collect", action="store_true", help="Stata 已运行后收集估计值并制表")
    args = parser.parse_args()
    config_path = args.config.resolve()
    if args.collect:
        collect(config_path)
    else:
        generation(config_path)


if __name__ == "__main__":
    main()
