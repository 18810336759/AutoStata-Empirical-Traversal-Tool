# 主效应模块：配置、筛查与核对

本模块检验 X 对 Y 的直接关系，是**基准筛查**而非最终因果结论。用户修改 config/analysis_config.yaml 中的数据、预处理、控制变量、固定效应和输出规则，在本目录的 main.yaml 中修改主效应假设，再运行 run_main_effect.py。共享操作步骤、表头和人机责任见 ../../全局.md。本说明文件不由程序执行。

## 运行前需要确定

- 用户先按研究方案筛好输入数据，确保企业—年份唯一，明白变量的计算口径、单位、理论方向和适当的控制变量。sample_period 和 sample_rules 只是说明，不是筛选命令。
- 全局 run_settings.enabled_modules.main_effect 与 main.yaml 顶部 enabled 均应为 true。main.yaml 的 hypotheses 只运行 enabled: true 的假设；修改 X、Y、方向或模型形式应在这里完成。
- 先在全局 continuous_variable_settings 登记允许插值和缩尾的变量，并在 preprocessing、controls、model_settings 设定公共规则。其他两类效应读取相同公共规则。

## 模型形式与筛查规则

一条假设用 id、y、x、model_form、pass_rule 表达。多个 X 会同时进入一个模型；每个检验项单独记录系数。linear 检查指定 X 的方向与显著性；u_shape 和 inverted_u_shape 构造 X 的平方项，nonlinear_settings 可控制是否先中心化。turning_point_check 字段目前仅为后续检验预留，不参与 candidate／strong_pass 判定。基准 U 型筛查关注平方项及配置的必要项，**不等于**已经证明整个观察区间呈 U 型或倒 U 型；严格区间检验和 Utest 留给后续数据分析 skill。

阶段 A/B 按全局有效年份门槛和处理方案运行，只加入 required_controls、固定效应和聚类设定。candidate_rules 决定是否标记候选；候选不是最终结论。若全局控制变量组合规则与本模块 combination_screening 启用，阶段 C 根据配置对可选控制变量组合回归，并标记 strong_pass、weak_pass。是否只扩展候选取决于配置，不能把强通过比例解释为所有合理设定的比例。失败和不显著结果仍留在完整结果中。

`hypothesis_results.csv` 的 strong_pass／weak_pass 是阶段 C 的假设级标签；强通过也计入弱通过。主效应的原始结果显式标记 `检验阶段=AB/C`。只运行阶段 A/B 时，可能有 candidate，却没有 strong／weak 通过行；这不表示程序遗漏了模型，应对照控制变量组合开关确认实际执行范围。

数据处理由共享 preprocessing.py 执行：原始核心变量有效年份门槛 → 企业内按真实年份插值 → 指定范围分位点缩尾 → 当前模型完整案例。最终 N 与其他模块可不同；sample_identity 标识实际估计观测行。

## 看哪些输出

路径为 output/<project.name>/<样本说明>/main_effect/。普通 `full_results` 保存全部已尝试模型，`control_combination_results` 保存 C 阶段已执行模型，含失败和未通过项；二者保留主效应系数、平方项等诊断列。`candidate_results` 是 A/B 候选假设判定，`strong_pass_results` 是 C 阶段强通过判定，`weak_pass_results` 是 C 阶段至少弱通过判定（包含强通过），后三者与中介、调节模块有完全相同的假设级表头。这五张人工表的公共列顺序也跨模块一致，空表仍有列名；普通 CSV／Excel 是否保存受全局开关控制。

机器衔接始终使用启用后的 `standardized_results.csv`、`specification_index.csv`、`hypothesis_results.csv`，不依赖普通 CSV 开关。标准化表一行一个检验项，索引保留公式、模型项、公共设定、R² 与实际样本，假设表保留候选和 C 阶段强／弱判定依据。模块专属诊断列排在共享公共列之后；跨模块汇总优先读取这三张机器表，而不是人工宽表。

重要字段：model_id 定位模型；specification_id 定位公共计算设定；检验项、effect_role、beta、se、p 对应这一行的核心系数；模型公式和模型项定义说明实际纳入的 X、控制变量、固定效应及生成项；样本量（N）和 sample_identity 表示最终估计样本。普通 R²、调整 R²、within R² 分列记录；within R² 仅在固定效应下按同一估计样本与固定效应基准模型计算，无法计算留空。processing_definition_json 与 processing_trace_json 分别说明处理规则和影响；preprocessing_definitions.json 提供结构化定义。

run_record.json、config_snapshot/ 和项目 run_manifest.json 记录回归时配置、数据指纹、代码环境、批次与输出文件。只更改当前 YAML 不会改写历史快照；更改主效应假设后应重跑本模块，再重跑 synthesis。关闭机器衔接产物时，synthesis 不会借用旧表。成功重跑先形成完整输出，再替换旧目录；失败保留上次完整结果并登记失败尝试。

## 人工审查清单

核对变量口径、被解释变量和模型形式；看检验项的方向、p 值、N、控制变量及固定效应；比较不同已执行设定下的结果分布；对 U／倒 U 型另外检查实际 X 区间、拐点和两端斜率。显著结果需要独立的稳健性和识别检验；本模块不自动决定论文最终报告的模型。
