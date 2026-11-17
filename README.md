# auto-stata：面板数据实证回归基准检验工具

面向经管领域的面板数据研究。本项目用 Python 执行预设的回归筛查、整理结果，并为选定设定生成可审阅的 Stata 复现代码；研究者仍需决定研究设计、核查数据并解释结果。初次使用建议先运行纯合成数据演示，不需要安装 Stata。

## 一、项目已经能做什么

| 功能 | 当前内容 |
| --- | --- |
| 基准检验 | 共享样本门槛、企业内插值、缩尾、控制变量组合和固定效应设定；分别运行线性或 U 型主效应、中介路径、线性或曲线调节的基准筛查。 |
| 结果整合 | 将同一批次的主效应、中介效应和调节效应按公共设定合并，输出 `full`、`require`、`strong`、`weak` 及假设明细。 |
| 结果复现 | 对选定的 `run_id + specification_id` 生成精确版与初学者版 Stata do 文件；支持主效应、调节效应和中介的总效应/a/b-c′ 三条路径。研究者实际运行 Stata 后，才可收集估计值并制作回归表。 |

程序不自动下载或构造研究变量，也不替代因果识别、稳健性及内生性检验。代码采用 [AGPL-3.0](LICENSE)；数据的来源和使用范围另见[数据说明](DATA_NOTICE.md)。

## 二、第一次安装与运行

**准备：**获取完整项目 ZIP 并解压到普通文件夹，不要只下载 README，也不要在 ZIP 预览窗口内运行。推荐 Python **3.11 64 位**；Python 3.10–3.13 已在 Windows、macOS、Linux 的[自动测试](.github/workflows/tests.yml)中通过。其他硬件与系统版本仍需试用验证，不建议直接使用 Python 3.9 或 3.14。无需先学习 Git。

### Windows 10/11

1. 从 [Python 官网](https://www.python.org/downloads/)安装 Python 3.11 64 位版，安装时保留 Python Launcher（`py`）。已经安装 3.10、3.12 或 3.13 的用户，可先试用现有版本。
2. 打开解压后的项目文件夹，双击 `run_demo_windows.cmd`。它会优先选用已安装的 Python 3.11，找不到时再尝试 3.12、3.13、3.10，创建本项目专用的 `.venv`，安装依赖并运行合成数据演示；**不会改动你的研究数据**。若 `.venv` 已存在，脚本会沿用其中受支持的 Python。
3. 等待窗口显示 `Done` 和“演示完成”。按窗口给出的路径打开 `result_synthesis/merged_full_results.xlsx`。窗口会停留，便于出现错误时拍照反馈。

若提示找不到 Python，请确认安装了 Python Launcher；若依赖安装失败，先检查网络。不要自行删除已有 `.venv` 或输出目录；先保存错误信息，再按[问题模板](.github/ISSUE_TEMPLATE/bug_report.md)反馈。

### macOS

1. 从 [Python 官网](https://www.python.org/downloads/)安装 Python 3.11。打开“终端”，输入 `cd `（末尾留一个空格），将解压后的项目文件夹拖入终端窗口，再按回车。
2. 输入 `python3 --version`，确认显示 3.10、3.11、3.12 或 3.13。若 `python3` 指向其他版本，请在下面第一行改用已安装的 `python3.11` 等对应命令。
3. 逐行执行：

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python scripts/run_demo.py
   ```

4. 按终端给出的路径打开 `result_synthesis/merged_full_results.xlsx`。

演示默认生成固定随机种子的纯合成面板，另附获授权的 `测试数据.dta` **不参与默认演示**。若再次运行时提示输出已存在，这是为避免覆盖旧结果；请先查看已有结果，需要新一轮演示时修改[全局配置](回归结果解读/基准检验/config/analysis_config.yaml)中的 `project.name`。`full` 是全部已执行设定，`require` 是满足用户逐条门槛的设定；`strong`、`weak` 为空不一定是报错。

## 三、用自己的数据进行一次检验

1. **准备数据。**支持 `.dta`、`.csv`、`.xlsx`。先按研究方案完成年份、行业、ST 等筛选，核对企业 ID 与年份唯一、变量名和缺失情况；保留原始文件与筛选记录。不要把未授权研究数据提交到 GitHub。
2. **改全局设置。**在 [`analysis_config.yaml`](回归结果解读/基准检验/config/analysis_config.yaml)填写 `data.input_file`、面板列、处理方案、控制变量、输出位置和模块开关。`sample_period`、`sample_rules` **只是说明和目录标签，不会自动筛选数据行**。
3. **改假设与整合设置。**在 [主效应](回归结果解读/基准检验/skill/main_effect/main.yaml)、[中介效应](回归结果解读/基准检验/skill/mediation_effect/mediation.yaml)、[调节效应](回归结果解读/基准检验/skill/moderation_effect/moderation.yaml) YAML 中确认变量关系和方向；需要合并时，在 [synthesis.yaml](回归结果解读/基准检验/skill/result_synthesis/synthesis.yaml)登记本轮假设与通过门槛。
4. **先校验，再按需运行。**在 macOS 的项目根目录，默认四个模块全开的命令顺序如下；若关闭某个效应，就跳过对应行，并同步核对 synthesis 的假设登记。Windows 请在 `cmd` 中先运行 `chcp 65001` 和 `set PYTHONUTF8=1`，再把每行前面的 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。不要同时运行同一项目的多个模块。

   ```bash
   .venv/bin/python 回归结果解读/基准检验/skill/other/config_validation.py
   .venv/bin/python 回归结果解读/基准检验/skill/main_effect/run_main_effect.py
   .venv/bin/python 回归结果解读/基准检验/skill/mediation_effect/run_mediation_effect.py
   .venv/bin/python 回归结果解读/基准检验/skill/moderation_effect/run_moderation_effect.py
   .venv/bin/python 回归结果解读/基准检验/skill/result_synthesis/run_result_synthesis.py
   ```
5. **读结果与复现。**先看 `result_synthesis/综合结果总览.xlsx` 的 `full`、`require` 和“假设明细”，再按 `specification_id` 查模块明细。需要 Stata 时，按[结果复现说明](回归结果解读/结果复现/复现SKILL.md)选择一条设定生成 do；生成文件本身不会启动 Stata。

**解释边界：**`candidate`、`strong_pass`、`weak_pass` 是预设条件下的筛查标签，不是因果证明；中介尚无 Bootstrap 间接效应确认，U 型仍需检查拐点和观察范围，模型最终样本也可能不同。论文使用前应核对公式、N、标准误与原始数据，并补充合适的稳健性和内生性检验。更完整的字段、阶段和文件说明见[全局手册](回归结果解读/基准检验/全局.md)。

## 四、项目与反馈

- **作者：**可健佳。**资助：**吉林大学 OPC 课题资助项目（2026OPC016）。
- **使用问题与建议：**通过 [GitHub Issues](https://github.com/18810336759/AutoStata-Empirical-Traversal-Tool/issues) 提交；仓库保持 Private 期间，未获邀请者无法访问 Issues。请写明系统、Python 版本、操作步骤与报错，上传截图前遮住个人路径和数据；不要在公开 Issue 中放密钥或真实研究数据。安全问题见 [SECURITY.md](SECURITY.md)。
- **参考资料：**[基准检验手册](回归结果解读/基准检验/全局.md)、[结果整合说明](回归结果解读/基准检验/skill/result_synthesis/result_synthesis.md)、[结果复现说明](回归结果解读/结果复现/复现SKILL.md)、[Python 文档](https://docs.python.org/3/)、[pandas 文档](https://pandas.pydata.org/docs/)、[statsmodels 文档](https://www.statsmodels.org/stable/)。具体学术文献应由研究者按所用方法和数据另行核对。
- **发布状态：**当前仍是私有试用仓库；完成权属、数据再分发、联系方式和独立试用验收后再决定是否公开，见[发布检查表](RELEASE_CHECKLIST.md)。
