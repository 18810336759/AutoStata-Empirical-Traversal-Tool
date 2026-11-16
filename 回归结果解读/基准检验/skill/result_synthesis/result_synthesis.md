# Result Synthesis：同批次整合与展示案例

本模块读取主效应、中介效应和调节效应已经发布的**机器结果**，不重新读取原始数据、不重新估计模型、不修改效应模块输出。用户在 config/analysis_config.yaml 决定本轮启用哪些效应，在本目录 synthesis.yaml 登记假设、理论链、合并键、表格、设定曲线与展示案例规则，最后运行 run_result_synthesis.py。总流程、人机职责和公共字段解释见 ../../全局.md。本说明文件不由程序执行。

## 一、何时运行与必须有什么

先运行本轮启用的效应模块，再运行 synthesis。每个请求整合且已启用的模块都须有当前项目 run_manifest.json 中的成功记录，且它的 standardized_results.csv、specification_index.csv、hypothesis_results.csv 三份机器文件齐全。系统核对 run_id、数据指纹、公共配置身份、模块配置版本、代码环境、运行时 YAML 快照、文件哈希和公共表头；只在某个固定目录看到同名 CSV 不算有效来源。缺文件、旧批次、改过的假设 YAML、混合行或不完整结构都会明确停止整合，不会生成貌似可信的新综合结果。关闭的模块不参与整合。

各效应模块的 config_snapshot/ 是**回归执行时**保存的配置。synthesis 不拿整合时最新的 YAML 代替历史配置；若启用整合快照，整合目录中 config_snapshot/<模块名>/ 是所纳入模块的历史快照副本，synthesis_input_config.yaml 仅说明整合时读到的全局 YAML。项目运行清单和每个模块 run_record.json 是追溯入口。

## 二、用户要核对哪些理论设定

- hypothesis_registry.expected_hypotheses：假设编号与所属模块 YAML 对齐；在每条假设下设置 `required_pass: strong / weak / none`，分别表示 `require` 表必须强通过、至少弱通过或不设通过门槛。当前均为 `none`，请由研究者自行决定。`none` 不会将假设移出严格 `strong/weak` 表。
- theory_chains.chains：若要跨模块展示同一研究故事，必须启用覆盖所需模块和假设的完整链，写清 X、Y、M、W。仅把多个显著编号放在一起不够；案例还会逐模型核对历史配置中的变量关系与模型形式。
- merge_keys：定义什么算相同公共设定，包括样本说明、门槛、处理方案、控制变量、固定效应和聚类。缺关键字段的行可按 missing_key_policy 保留在完整审计表，但**不得成为展示案例**。sample_period、sample_rules 是用户输入的描述标签，不是程序实际筛选行为。
- display_case_selection：选择强／弱通过、所需模块和是否必须属于启用理论链。跨模块案例始终需要完整理论链；若只作单模块查看，可按配置放宽。

当前配置的 `input.module_selection_mode` 为 `listed_modules`，只读取主效应和调节效应；全局中介效应也暂时关闭。将来纳入中介时，先启用模块，再调整本文件的读取列表和假设登记。严格表只检查本轮被选中、启用且已登记的假设；停用的示例不计入。若某个应纳入的假设没有 C 阶段结果，它不满足强／弱通过，原因会显示在明细中。

## 三、综合表如何判定

| 文件 | 含义 |
| --- | --- |
| model_structure_check.csv／md | 本轮模块开关、运行记录、机器文件和来源核对 |
| hypothesis_coverage_check.csv／md | 人工登记、模块 YAML 与实际输出假设的覆盖对照 |
| merged_require_results | 每个满足用户逐假设 `required_pass` 门槛的公共设定一行；全部为 `none` 时不附加门槛 |
| merged_full_results | 每个已执行公共设定一行；包括失败、未通过和缺少 C 判定的设定 |
| merged_strong_pass_results | 同一设定下所有纳入假设均在 C 阶段强通过 |
| merged_weak_pass_results | 同一设定下所有纳入假设均在 C 阶段至少弱通过；强通过也计入 |
| hypothesis_status_detail | 每个设定、每条纳入假设一行，区分未通过、运行失败和缺少 C 结果 |
| merged_model_detail_results.csv | 逐模型／路径完整审计长表；保留模块诊断、失败原因及检验项 |
| 综合结果总览.xlsx | 顶层六工作表：require、full、strong、weak、假设明细、字段说明 |
| specification_curve_data／summary、robustness_statement.md | 已执行且符合曲线纳入规则的设定分布和谨慎文字，不代表全部合理预设 |

普通 CSV／Excel 是否生成受 synthesis.yaml 的 output 与相关开关控制；总览工作簿还需 `output.overview_workbook.enabled=true`。四张综合主表有完全相同的表头，即使严格表无入选也保留列名。`Strong pass` 列列出强通过假设，`weak_pass` 列只列**仅弱通过**假设，`no&lack_pass` 汇总未通过、失败及缺少 C 的假设；后三种原因另有独立列和 `筛选说明`。样本门槛、处理方案、控制变量组合ID、纳入的控制变量、纳入的可选控制变量、聚类标准误、成功／候选状态及各模块精确 C 阶段 `model_id` 同行可见；`model_ids_json` 供后续 do-file 精确定位。`模型合并ID` 为本次展示编号，`specification_id` 才是公共设定身份。

通过筛选**只读取 C 阶段的 hypothesis_results**，A/B 候选不能填补缺少的 C。若同一设定、同一假设有多个 C 判定，当前按全部判定都满足门槛处理，不挑最显著的一条。`require` 只检查设置为 `strong/weak` 的假设；`strong/weak` 严格表检查全部纳入假设，包括 `required_pass: none` 的假设。主效应和调节效应使用模型级假设依据；中介根据同一 specification_id、同一 chain_id 的所需路径聚合，每条路径须分别达到当前规则。三步法要求总效应时必须在同一控制组合下重估 X→Y。路径样本或 N 可不同，差异记录供人工审查，不自动使链失败。

三类效应各自的 standardized_results、specification_index、hypothesis_results 有固定机器公共表头；前两者按模型检验项记录，后者按假设／中介链判定。模块诊断列可追加在公共列之后。公式和模型项定义在标准化表与索引中可见，普通／调整／组内 R² 各自记录，不适用或无法计算的组内值留空。缺合并键或来源不明的行可在 `full`／逐模型明细中审查，却不能产生可信展示案例。设定曲线仍读取逐模型结果，不把一行设定摘要误作一个系数。

## 四、展示案例怎么用

整合运行只显示一条整体进度条，不再分 AB/C。启用展示案例筛选时，程序从严格 `strong/weak` 入选设定回到可核验的模型行，再按理论链、模块和历史公式检查；它与 `require` 是不同用途，`require` 入选不保证成为展示案例。display_case_index.csv／json 列出入选 case_id、通过等级、理论链、精确模型 ID、公共设定与样本差异；JSON 索引还受 output.save_json 控制。display_case_rejections.csv 说明被检查分组未能入选的原因；严格表或案例为零可能是缺 C、未联合通过或缺可核验理论链，不等于程序失败。启用 dofile_bridge 时，display_case_manifest.json 是未来复现代码的结构化输入，逐案例保存：

- run_id、数据及公共配置身份、specification_id 和合并键对应的设定；
- 精确的模型列表、检验项、完整公式、模型项定义、预处理定义／影响、N 与 sample_identity；
- 样本是否一致及各模型差异；
- 回归运行记录、预处理文件和实际使用的配置快照索引。

用户应先看拒绝原因及索引，再用 manifest 的模型 ID 对照各模块 specification_index.csv、hypothesis_results.csv 与快照；检查公式、控制变量、时间口径、N 和方向是否符合研究目的。当前版本**不生成 Stata do 文件**。案例清单已为后续 do-file 生成器提供定位信息，但真正复现时还要从经用户审核的原始输入重新处理并运行，由用户检查 Stata 输出。

案例索引中的 `selected_models_json` 是精确模型列表，包含来源模块、`model_id`、假设编号、路径、公式、被检验项、预处理定义和样本标识；`public_specification_json` 是该案例的合并键取值；`sample_differences_json` 列出各模型的 N 与样本身份，`same_sample: false` 是提醒而非自动失败；`source_snapshots_json` 指向各模块的运行记录、预处理文件及回归时配置快照。阅读 CSV 中的 JSON 字段有困难时，可直接打开结构化的 `display_case_manifest.json`。`spec_group_id` 仅为本轮展示分组号，复现时须同时核对 `run_id`、数据指纹和 `specification_id`。

## 五、批次变化与错误处理

数据或公共计算配置变化会开启新 run_id，其他模块旧结果不能进入新 synthesis。只改变某模块假设会更新该模块版本，并使已有 synthesis 失效；同配置重跑模块可复用公共批次，但须用新 run_instance_id 和当次文件。输入在整合过程中发生变化时，发布会停止。生成结果先进入临时目录，成功后整体替换 synthesis 目录；失败不会把上次完整结果覆盖成半套。普通结果或机器文件开关关闭时，程序不会从历史目录借用旧文件。项目名称改变会建立独立目录和运行清单，旧项目保留。

## 六、方法边界与下一步

synthesis 只整合**已执行设定**，不扩大抽样空间，不声称所有合理预设均已遍历，也不负责严谨 U 型确认、Bootstrap 间接效应、内生性分析或投稿三线表生成。未来变量构造 skill 将协助从文章与情境确定变量公式；结果复现／展示 skill 将使用 manifest、运行快照和原始输入生成供用户审阅与执行的 Stata do 文件；数据分析 skill 将补充 Utest、拐点、条件效应图、稳健性与内生性建议。理论、数据质量、识别策略和最终论文解释始终由研究者确认。
