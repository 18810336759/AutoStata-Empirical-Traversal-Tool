from __future__ import annotations

import pandas as pd

STANDARD_COLUMNS = [
    "model_id",
    "specification_id",
    "run_id",
    "run_instance_id",
    "common_fingerprint",
    "data_sha256",
    "module_revision",
    "模块名称",
    "假设编号",
    "假设描述",
    "relationship_type",
    "effect_type",
    "path_type",
    "target_path",
    "path_id",
    "chain_id",
    "模型形式",
    "通过规则",
    "被解释变量",
    "解释变量",
    "中介变量",
    "调节变量",
    "核心解释变量组合",
    "检验项",
    "effect_role",
    "理论方向",
    "direction_match",
    "beta",
    "se",
    "p",
    "样本区间",
    "样本规则",
    "样本门槛",
    "处理方案",
    "是否插值",
    "是否缩尾",
    "控制变量组合ID",
    "控制变量组合",
    "纳入的控制变量",
    "纳入的可选控制变量",
    "控制变量数量",
    "optional_control_count",
    "固定效应设定",
    "聚类标准误设定",
    "模型公式",
    "模型项定义",
    "普通 R²",
    "调整 R²",
    "within R²",
    "组内 R²定义",
    "组内 R²吸收项",
    "样本量（N）",
    "sample_identity",
    "estimation_sample_n",
    "complete_case_removed_n",
    "processing_definition_id",
    "processing_definition_json",
    "processing_trace_json",
    "processing_input_n",
    "processing_eligible_n",
    "是否候选模型",
    "strong_pass",
    "weak_pass",
    "是否成功运行",
    "失败原因",
    "global_config_path",
    "module_config_path",
    "expected_change",
]

FLOAT_COLUMNS = ["beta", "se", "p", "普通 R²", "调整 R²", "within R²"]
INTEGER_COLUMNS = ["样本门槛", "控制变量数量", "optional_control_count",
                   "样本量（N）", "estimation_sample_n", "complete_case_removed_n",
                   "processing_input_n", "processing_eligible_n"]
BOOLEAN_COLUMNS = ["direction_match", "是否插值", "是否缩尾", "是否候选模型",
                   "strong_pass", "weak_pass"]

HUMAN_RESULT_COLUMNS = [
    "model_id", "specification_id", "模块名称", "假设编号", "假设描述", "检验阶段",
    "样本区间", "样本规则", "样本门槛", "处理方案", "是否插值", "是否缩尾",
    "控制变量组合ID", "控制变量组合", "纳入的控制变量", "纳入的可选控制变量",
    "固定效应设定", "聚类标准误设定", "是否成功运行", "是否候选模型",
    "strong_pass", "weak_pass", "失败原因",
]


def align_human_result_columns(frame: pd.DataFrame) -> pd.DataFrame:
    """Common human-facing prefix, followed by each effect's diagnostic columns."""
    result = frame.copy()
    for column in HUMAN_RESULT_COLUMNS:
        if column not in result:
            result[column] = None
    return result[HUMAN_RESULT_COLUMNS + [c for c in result if c not in HUMAN_RESULT_COLUMNS]]


def _nullable_boolean(series: pd.Series) -> pd.Series:
    def convert(value):
        if pd.isna(value):
            return pd.NA
        if isinstance(value, bool):
            return value
        if str(value).strip().lower() in {"true", "1", "yes", "是"}:
            return True
        if str(value).strip().lower() in {"false", "0", "no", "否"}:
            return False
        return pd.NA
    return series.map(convert).astype("boolean")


def align_standard_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Keep the same shared columns first; preserve module-specific diagnostics after them."""
    out = df.copy()
    for col in STANDARD_COLUMNS:
        if col not in out.columns:
            out[col] = None
    for col in FLOAT_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("Float64")
    for col in INTEGER_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("Int64")
    for col in BOOLEAN_COLUMNS:
        out[col] = _nullable_boolean(out[col])
    for col in STANDARD_COLUMNS:
        if col not in FLOAT_COLUMNS + INTEGER_COLUMNS + BOOLEAN_COLUMNS:
            out[col] = out[col].astype("string")
    return out[STANDARD_COLUMNS + [col for col in out.columns if col not in STANDARD_COLUMNS]]


def preserve_module_diagnostics(standard_row: dict, source_row: pd.Series) -> dict:
    """Append raw module diagnostics without changing the shared contract."""
    result = standard_row.copy()
    for name, value in source_row.items():
        if name not in STANDARD_COLUMNS and name not in result:
            result[name] = value
    return result


SPECIFICATION_INDEX_COLUMNS = [
    "run_id", "run_instance_id", "common_fingerprint", "module_revision",
    "specification_id", "model_id", "模块名称", "假设编号", "path_id", "chain_id",
    "path_type", "检验项", "effect_role", "样本门槛", "处理方案", "是否插值", "是否缩尾",
    "控制变量组合ID", "纳入的控制变量", "固定效应设定", "聚类标准误设定",
    "processing_definition_id", "processing_trace_json", "processing_input_n",
    "processing_eligible_n", "sample_identity", "complete_case_removed_n",
    "样本量（N）", "模型公式", "模型项定义", "普通 R²", "调整 R²", "within R²",
    "组内 R²定义", "组内 R²吸收项",
    "是否成功运行", "失败原因",
]


def build_specification_index(standardized: pd.DataFrame) -> pd.DataFrame:
    """One searchable provenance row per distinct model term and sample."""
    frame = align_standard_columns(standardized)
    for name in SPECIFICATION_INDEX_COLUMNS:
        if name not in frame:
            frame[name] = None
    return frame[SPECIFICATION_INDEX_COLUMNS].drop_duplicates().reset_index(drop=True)
