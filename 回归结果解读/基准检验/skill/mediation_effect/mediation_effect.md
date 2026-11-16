# 中介效应模块：路径、机制链与样本差异

本模块根据 X→M→Y 的理论关系做**路径回归筛查**，不执行 Sobel 或 Bootstrap 间接效应确认。用户在 config/analysis_config.yaml 设置公共数据和估计规则，在本目录 mediation.yaml 的 mechanism_paths 中写明 X、Y、中介变量、路径方向、two_step／three_step 和通过规则，再运行 run_mediation_effect.py。总流程与输出字段见 ../../全局.md。本说明文件不由程序执行。

## 运行前先确定机制口径

用户应先从研究理论确定 M 是否真正位于 X 与 Y 之间，明确变量的时间顺序和数据构造方法，并在输入前按研究方案筛好样本。多中介可以在同一机制假设的 mediators 中列出，程序会展开为独立机制链；不同 X 或 Y 宜写成不同 mechanism_paths。全局 sample_period、sample_rules 只是说明，不自动筛选。

全局 run_settings.enabled_modules.mediation_effect 和 mediation.yaml 顶部 enabled 都要启用。公共企业门槛、插值、缩尾、控制变量、固定效应和聚类标准误来自同一个全局 YAML。路径的理论方向、显著性阈值、候选与阶段 C 筛查规则来自 mediation.yaml，不应在全局文件另写一套。

## 路径如何运行与聚合

- x_to_y：X 对 Y 的总效应，三步法在配置要求时必须检查。
- x_to_m：X 对 M 的第一阶段路径。
- x_m_to_y：Y 同时含 X 与 M，检验控制 X 后的 M→Y；模型还可记录 X 的直接效应及变化提示。

两步法和三步法所需路径由 screening_methods 及每条机制的 screening_method 指定。阶段 A/B 以 required_controls 做基础筛查，标记 candidate；阶段 C 在启用时按允许的可选控制变量组合复核。若三步法要求 x_to_y，程序会在**当前同一组合**重估总效应，不借用另一个控制变量设定下的总效应。

中介链的假设级通过要求：同一公共设定、同一 chain_id、所需路径全部存在、每条路径分别符合当前配置的筛查条件。只通过其中一条路径，整条链不能通过。路径模型使用共享预处理引擎，但各路径根据自身公式所需变量独立选择完整案例，所以观测行、sample_identity 和 N 可以不同；差异写入 hypothesis_results.csv 的 path_samples_json、path_sample_identity_same、path_n_same、sample_difference_note，**不因此自动判失败**。研究者仍须判断样本差异是否影响机制解释。

`hypothesis_results.csv` 的一行代表同一设定、同一机制链、同一运行阶段的聚合判定；它的 strong_pass／weak_pass 只有阶段 C 的所需路径全部达到对应阈值时才会为真。阶段 A/B 的 candidate 和单条路径表中的显著性不能代替这一行的链级结论。

目前的通过只是路径证据，不能等同于间接效应已通过 Bootstrap、因果中介已识别，或机制解释已被证实。正式确认所需检验留给后续数据分析 skill。

## 看哪些输出

输出位于 output/<project.name>/<样本说明>/mediation_effect/。普通 `full_results` 保存全部已尝试路径模型，`control_combination_results` 保存 C 阶段路径模型；`path_results` 和 `chain_results` 是中介专属辅助表。与另外两个效应模块相同，`candidate_results` 保存 A/B 候选的**链级假设判定**，`strong_pass_results` 保存 C 阶段强通过判定，`weak_pass_results` 保存 C 阶段至少弱通过判定（包含强通过）。五张同名人工表以同一组公共字段起头；后三张完整假设级表头跨模块一致，空表仍有表头。普通 CSV／Excel 受输出开关控制。机器衔接使用三份 CSV：

| 文件 | 读法 |
| --- | --- |
| standardized_results.csv | 一行一个路径中的检验项；path_type、chain_id、effect_role 说明它是哪条路径的哪个系数 |
| specification_index.csv | 定位模型公式、模型项定义、公共设定、处理定义、R²、N 和 sample_identity |
| hypothesis_results.csv | 同设定同机制链的候选、strong／weak 判定、所需／已有／缺失路径及各路径样本差异 |

三份机器 CSV 的公共列顺序与主效应、调节效应一致；标准化表和索引可以追加中介诊断列。强通过属于弱通过，但 A/B 候选绝不算 C 通过。synthesis 对每个公共设定、每个已登记且启用的假设核对 C 判定；没有整条路径链的 C 结果时会在 `假设明细` 标记缺少 C，而不是借用 A/B 路径显著性。

processing_definition_json 与 processing_trace_json 分别记录处理方案和实际影响；preprocessing_definitions.json 保存完整结构化定义。普通 R²、调整 R²、within R² 分列记录；不适用的 within R² 留空。run_record.json、config_snapshot/ 与项目 run_manifest.json 记录当次批次、数据指纹、历史 YAML、代码环境和输出文件清单。synthesis 只读取当前批次、快照可核验且机器文件齐全的模块结果。

## 人工审查顺序

先核对 X、M、Y 的理论顺序与各路径公式，再看 hypothesis_results 的 required_paths、missing_paths、strong_pass／weak_pass 和 pass_basis_json；随后对照各路径的 beta、p、方向、N、sample_identity、控制变量和固定效应。路径样本不一致时，检查缺失机制和可比性。最后再做机制确认、稳健性与识别检验；不凭一个显著路径或筛查标签直接写论文结论。
