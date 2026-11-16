# Auto-stata：经管实证回归检验演示版

项目作者：可健佳。项目获得吉林大学 OPC 课题资助（2026OPC016）。

本项目目前提供基准检验、结果整合和 Stata 复现代码生成。默认一键演示只使用纯合成企业面板；仓库另附项目负责人确认可公开的 `测试数据.dta`，见[数据说明](DATA_NOTICE.md)。演示结果不能直接写入论文。

**先看清边界：**示例一键运行会完成主效应、中介效应、调节效应和结果整合，不会启动 Stata。`结果复现` 需要先从综合表选定 `run_id + specification_id`，且当前生成器尚不支持中介模型的 do 复现；请参阅 [复现说明](回归结果解读/结果复现/复现SKILL.md)。

## 第一次试用：Mac

1. 从 [Python 官网](https://www.python.org/downloads/)安装 Python。当前版本在 macOS 的 Python 3.10 环境做过测试；其他 Python 版本和 Windows 仍待首批试用验证。打开“终端”，进入解压后的项目文件夹。没有用过终端的同学可先阅读 [GitHub Desktop 入门](https://docs.github.com/en/desktop/overview/creating-your-first-repository-using-github-desktop)，也可直接下载 ZIP 解压，不必先学 Git。
2. 在该文件夹中依次输入以下命令，每行结束按回车：

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python scripts/run_demo.py
   ```

3. 等待显示“演示完成”。按终端显示的路径打开 `result_synthesis/merged_full_results.xlsx`。`full` 展示所有公共设定，`require` 只展示满足登记要求的设定；`strong` 或 `weak` 为空不一定是报错。

## 第一次试用：Windows 10/11

1. 从 [Python 官网](https://www.python.org/downloads/)安装 **Python 3.11 64 位版**，安装时保留 Python Launcher（`py`）选项。
2. 将仓库 ZIP **完整解压**到普通文件夹，建议路径不要太长；不要在 ZIP 预览窗口中直接运行文件。
3. 双击根目录的 `run_demo_windows.cmd`。脚本会创建独立 Python 环境、安装依赖、运行纯合成数据示例，并在结束时停留在结果路径。
4. 按窗口最后显示的路径，用文件管理器打开 `result_synthesis` 文件夹，再打开 `merged_full_results.xlsx`。若出现错误，保留窗口并按[问题模板](.github/ISSUE_TEMPLATE/bug_report.md)反馈。

不想双击脚本时，可在文件管理器地址栏输入 `cmd` 并回车，依次执行：

```bat
chcp 65001
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
py -3.11 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe scripts\run_demo.py
```

无需在 PowerShell 中运行 `Activate.ps1`。若显示找不到 `py`，请确认 Python Launcher 已安装；若依赖安装失败，先检查网络，再把完整错误信息提交 Issue。Windows 的实际验收将由 GitHub Actions 与首批 Windows 试用者完成；当前不能声称已在 Windows 真机验证。**不要上传自己的研究数据**。

## 下一步

- 全局设置：[全局说明](回归结果解读/基准检验/全局.md)；假设分别在各模块 YAML 中配置。
- 默认演示数据来自 [生成脚本](scripts/generate_demo_data.py)，固定随机种子，`ShortName` 为 `DEMO_###`。另附的授权测试数据不参与默认一键演示，详见[数据说明](DATA_NOTICE.md)。
- 想分析自己的数据，请先确认来源、再分发权限、变量定义、面板唯一性和样本筛选；修改 `analysis_config.yaml` 后再运行相应模块。样本年份和“剔除 ST”等标签**不会自动筛选行**。
- 运行失败、结果异常或操作卡住，请用 GitHub Issues 对应模板反馈；上传截图前遮住本机路径、账号和数据。

本仓库计划采用 [AGPL-3.0](LICENSE)。未来托管服务、账户和订阅系统不在这个发布副本内；但**仅把代码放在另一个仓库并不能自动规避 AGPL 对公开核心及其修改版的义务**。商业版接入公开核心前应先做架构和法律核查。发布前仍需确认所有代码与演示数据的权属及依赖许可。
