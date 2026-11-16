# 调节效应模块：关系形式、交互项与条件解释

本模块按 moderation.yaml 中每条 moderation_paths 的 from、to、form、moderator、moderation.form 与 expected_change 估计调节关系。公共数据、预处理、控制变量、固定效应、聚类及输出由 config/analysis_config.yaml 管理；运行入口为 run_moderation_effect.py。完整操作流程、用户责任与公共表头见 ../../全局.md。本说明文件不由程序执行。

## 配置前的研究判断

研究者先明确被调节的 A→B 关系及其原有形式，再明确 W 预期改变的是线性斜率、条件斜率的 U 型变化，还是 A→B 曲线的曲率或转折点。若模型还需要原始 X 等额外变量，可在 conditioning_variables 中列出。程序参考主效应和中介效应的变量分组推导 target_path；这个标签只是辅助分类，回归公式仍由 from、to、W 和附加变量决定。用户须检查变量时间顺序、数据质量、样本筛选和理论可解释性。

| 原有关系 form | 调节形式 moderation.form | 构造的关键项与当前筛查 |
| --- | --- | --- |
| linear | linear | A、W、A×W；依据交互项与预期方向筛查 |
| linear | quadratic | A、W、W²、A×W、A×W²；A×W² 是核心项，阶段 C 还做配置的条件斜率与范围诊断 |
| u_shape／inverted_u_shape | linear | A、A²、W、A×W、A²×W；当前 beta5_only 规则以 A²×W 的显著性和可判断方向初筛 |

当前不支持原有关系与调节形式同时非线性，也不支持调节之调节。对于曲线 A→B 加线性 W，steepen／flatten 按 U 或倒 U 曲率方向判断；turning_point_left／right **不能仅由 A²×W 显著证明**。程序另记录转折点移动参考量 D、标准误、p 值和条件曲率是否跨零；这些是诊断，不参与 beta5_only 的通过判定。对于 linear 加 quadratic W，A×W² 显著也不足以单独证明条件斜率在观察范围内形成 U／倒 U。完整的区间、拐点与图形判断仍需后续补充检验及人工解释。

## 运行与共享设定

全局 run_settings.enabled_modules.moderation_effect 和 moderation.yaml 顶部 enabled 都须为 true。阶段 A/B 按全局有效年份门槛和已启用处理方案，以 required_controls 回归并依 candidate_rules 标记候选。阶段 C 在配置启用时遍历已执行的 optional_controls 组合；strong_pass 和 weak_pass 是筛查标签，不是正式证明。共享 preprocessing.py 依次执行原始核心变量门槛、企业内真实年份插值、指定分位点与分组缩尾、当前模型完整案例；调节模型有自己的最终 sample_identity 和 N。

只有进入阶段 C 的模型才可能在假设级表中取得 strong_pass／weak_pass；阶段 A/B 的 candidate 仅说明达到初筛条件。`hypothesis_results.csv` 的 `pass_basis_json` 保留实际检验项、阈值与诊断，查看非线性调节时应与完整公式和可选条件效应表一起读。

conditional_effect_analysis 是可选的解释性输出，可生成指定 W 水平下的条件效应表或绘图数据。关闭时不生成本次图表；成功重跑会清理旧的可选文件。打开它也不改变假设级通过判定。条件预测图只显示已标注的模型部分贡献，不能误读为包含全部控制项和固定效应的预测水平。

## 看哪些输出

输出位于 output/<project.name>/<样本说明>/moderation_effect/。普通 `full_results` 保存全部已尝试调节模型，`control_combination_results` 保存 C 阶段模型；二者可用紧凑版布局展示交互项、曲率与转折点诊断。`candidate_results` 保存 A/B 候选假设判定，`strong_pass_results` 保存 C 阶段强通过判定，`weak_pass_results` 保存 C 阶段至少弱通过判定（包含强通过）。五张同名人工表使用跨模块公共列前缀；后三张完整假设级表头也一致，空表保留表头。普通 CSV／Excel 和可选条件效应、图表分别受开关控制。

机器衔接文件为 `standardized_results.csv`、`specification_index.csv`、`hypothesis_results.csv`；公共列顺序与主效应、中介效应一致，专属诊断列可追加。前两者记录检验项、effect_role、完整公式、处理与样本身份、普通 R²／调整 R²／within R²，后者记录 C 阶段假设级判定依据。within R² 不适用或不可计算时留空。`run_record.json`、`config_snapshot/`、`preprocessing_definitions.json` 与项目 `run_manifest.json` 用于核对运行时配置、批次、数据和程序版本；synthesis 不会借用旧文件。

人工查看时先核对 A、B、W 与交互项含义，再读核心项 beta、se、p、预期方向、N、控制变量及固定效应；非线性关系还须看实际观测范围、条件斜率、曲率是否跨零和拐点。若需要正式确认 U／倒 U 或转折点方向，应进入后续严格补充检验，不能把筛查标签直接写成结论。
