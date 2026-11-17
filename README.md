# auto-stata

### 面板数据实证回归基准检验工具

主要面向经管专业学生的本地开源工具：用 YAML 写清研究设定，运行主效应、中介路径和调节效应的基准筛查，汇总同一设定下的结果，并为选定模型生成可审阅的 Stata 复现代码。**第一次体验只需 Python，不需要 Stata，也不用提供自己的数据。**

> 当前版本是本地 Python + YAML 工作流，不是零代码网页端。程序负责执行和留痕，研究者负责数据、理论、模型选择及论文解释。项目研究理念来自“智证”课题，但本仓库只介绍已经开放的功能。

[快速开始](#快速开始) · [已有功能](#已有功能) · [使用自己的数据](#使用自己的数据) · [结果怎么读](#结果怎么读) · [边界与路线图](#边界与路线图) · [反馈与项目](#反馈与项目)

## 快速开始

下载仓库的**完整 ZIP**，解压后打开项目文件夹。推荐 Python **3.11（64 位）**；3.10–3.13 已在 Windows、macOS、Linux 的 [CI 测试配置](.github/workflows/tests.yml)中覆盖。演示使用固定随机种子的纯合成数据，不读取你的研究文件。

| 你的电脑 | 第一步 | 运行演示 |
| --- | --- | --- |
| Windows 10/11 | 从 [Python 官网](https://www.python.org/downloads/)安装 Python 3.11，保留 Python Launcher（`py`）；解压完整 ZIP | 双击项目根目录的 `run_demo_windows.cmd`，等待窗口显示 `Done` 和“演示完成” |
| macOS | 从 [Python 官网](https://www.python.org/downloads/)安装 Python 3.11；打开“终端”，输入 `cd `，把项目文件夹拖入终端并回车 | 依次执行下方三行命令 |

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/run_demo.py
```

macOS 如 `python3 --version` 不是 3.10–3.13，请在第一行改用已安装的 `python3.11` 等命令。Windows 脚本会优先选 3.11，再尝试 3.12、3.13、3.10；它会创建项目专用 `.venv`，不会修改原始研究数据。**不要只下载 README，也不要在 ZIP 预览窗口内运行。**

演示结束后，按终端提示打开 `result_synthesis/综合结果总览.xlsx`。先看 `full`，再看 `require`；严格筛选表为空也可能是正常结果。首次遇到报错时保留终端文字或截图，按[问题模板](.github/ISSUE_TEMPLATE/bug_report.md)反馈。Windows 找不到 Python 时先检查 Launcher；安装依赖失败时先检查网络。

## 已有功能

| 阶段 | 你提供什么 | 程序交付什么 |
| --- | --- | --- |
| 基准检验 | 面板数据、变量关系、样本门槛、处理和控制变量设定 | 主效应（线性或 U 型）、中介三条回归路径、调节效应（线性或曲线）的分阶段筛查与逐模型记录 |
| 结果整合 | 同一批次的假设登记与逐条通过要求 | `full`、`require`、`strong`、`weak` 及假设明细；同一公共设定下的效应可并排核对 |
| Stata 复现 | 从整合表选一条 `run_id + specification_id` | 精确版和初学者版 do；主效应、调节效应与中介的总效应/a/b-c′ 路径可逐模型复跑，实际运行并核验后生成三类回归表 |

**工作顺序：**准备数据和设定 → 运行各效应模块 → 整合全部设定 → 选定一条设定 → 审阅、运行 Stata do → 核验并制表。演示只跑前面三步；Stata 是可选的后续复现环节。

程序会保存配置快照、模型 ID、处理方案和运行记录。它帮助研究者看见不同合理设定下的结果，而不是自动挑出显著的一行。详细的字段、阶段和输出说明见[基准检验手册](回归结果解读/基准检验/全局.md)。

## 使用自己的数据

1. **准备输入。**支持 `.dta`、`.csv`、`.xlsx`。先按研究方案处理年份、行业、ST 等样本筛选，确认企业 ID 与年份组合唯一，核对变量名、缺失值和数据授权。不要把未授权研究数据上传到 GitHub。
2. **填写全局 YAML。**在 [analysis_config.yaml](回归结果解读/基准检验/config/analysis_config.yaml)修改 `data.input_file`、面板列、处理方案、控制变量、输出位置与模块开关。`sample_period` 和 `sample_rules` 仅用于说明与目录命名，**不会自动筛选数据行**。
3. **填写假设。**分别核对[主效应](回归结果解读/基准检验/skill/main_effect/main.yaml)、[中介效应](回归结果解读/基准检验/skill/mediation_effect/mediation.yaml)、[调节效应](回归结果解读/基准检验/skill/moderation_effect/moderation.yaml)配置；需要汇总时，再在 [synthesis.yaml](回归结果解读/基准检验/skill/result_synthesis/synthesis.yaml)登记假设和筛选门槛。
4. **依次运行。**在项目根目录按下方顺序执行；关闭某效应模块时跳过对应行，并同步检查 synthesis 登记。不要同时运行同一项目的多个模块。

```bash
.venv/bin/python 回归结果解读/基准检验/skill/other/config_validation.py
.venv/bin/python 回归结果解读/基准检验/skill/main_effect/run_main_effect.py
.venv/bin/python 回归结果解读/基准检验/skill/mediation_effect/run_mediation_effect.py
.venv/bin/python 回归结果解读/基准检验/skill/moderation_effect/run_moderation_effect.py
.venv/bin/python 回归结果解读/基准检验/skill/result_synthesis/run_result_synthesis.py
```

Windows 用户先在 `cmd` 执行 `chcp 65001` 和 `set PYTHONUTF8=1`，再把每行的 `.venv/bin/python` 换成 `.venv\Scripts\python.exe`。最短演示不需要手工运行这些命令；这里是切换到自己数据后的步骤。

需要 Stata do 时，打开[结果复现使用说明](回归结果解读/结果复现/复现SKILL.md)。复现模块**只生成文件，不会自动安装 Stata 扩展或启动 Stata**；请先核对原始数据路径、公式和处理口径，再运行 do。

## 结果怎么读

整合表把一条公共设定固定为相同的样本门槛、处理方案、控制变量组合与标准误设定。四张表都依据各模块 C 阶段的假设级结果筛选：

| 工作表 | 含义 | 先看什么 |
| --- | --- | --- |
| `full` | 全部已执行的公共设定，包含失败、未通过或缺少 C 结果的情况 | 是否成功运行、缺失原因、各效应 `model_id` |
| `require` | 满足你在 synthesis 为各假设指定的 `required_pass` 条件 | 通过了哪些假设、处理方案和控制组合 |
| `strong` | 本轮纳入综合的全部假设都强通过 | 是否为空；不要把筛选标签当因果结论 |
| `weak` | 本轮纳入综合的全部假设至少弱通过，强通过也计入 | 与 `strong` 比较，但仍检查各模型明细 |

精确版 do 只复现选定设定下**已有 C 结果**的模型；简明版可以把缺少 C 的已启用假设标为“定向补充复测”。`candidate`、`strong_pass` 和 `weak_pass` 都是预设规则下的筛查标签。中介三条路径的回归不等于 Bootstrap 间接效应确认；U 型还要检查拐点及观察区间，模型最终样本也可能不同。论文使用前请核对 N、系数、标准误和原始数据，并补充合适的稳健性与识别论证。

## 边界与路线图

| 当前开源版 | 后续规划，尚未交付 |
| --- | --- |
| 本地 YAML 配置、基准效应筛查、同设定整合、选定结果的 Stata do 复现 | 变量下载与构造、设定曲线及稳健性分析、内生性诊断与检验建议、零代码网页端 |

本项目目前面向可整理成企业面板数据的研究问题；不承诺自动完成数据下载、因果识别或论文写作。申报书中的完整“智证”全流程系统是发展方向，不应与本仓库的已交付范围混同。

## 反馈与项目

| 项目 | 信息 |
| --- | --- |
| 作者 | 可健佳 |
| 资助 | 吉林大学 OPC 课题资助项目（2026OPC016） |
| 许可证 | [AGPL-3.0](LICENSE)。未来托管服务及二次开发应自行核对许可义务。 |
| 数据 | 演示使用纯合成数据；仓库内另有获授权的测试数据，其来源与使用范围见[数据说明](DATA_NOTICE.md)。 |
| 反馈 | 在 [GitHub Issues](https://github.com/18810336759/AutoStata-Empirical-Traversal-Tool/issues) 按模板提交系统版本、操作步骤和错误信息。安全问题见 [SECURITY.md](SECURITY.md)。 |

公开前仓库仍为 **Private**，未受邀者暂时打不开 Issues。提交截图前请遮住个人路径、密钥和研究数据。更完整的使用说明见[基准检验手册](回归结果解读/基准检验/全局.md)、[结果整合说明](回归结果解读/基准检验/skill/result_synthesis/result_synthesis.md)和[复现说明](回归结果解读/结果复现/复现SKILL.md)；发布门槛见[检查表](RELEASE_CHECKLIST.md)。
