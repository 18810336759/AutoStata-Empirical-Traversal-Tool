"""Run the bundled synthetic example without editing YAML or using Stata."""

from pathlib import Path
import subprocess
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "回归结果解读" / "基准检验"
RUNNERS = (
    BASELINE / "skill" / "main_effect" / "run_main_effect.py",
    BASELINE / "skill" / "mediation_effect" / "run_mediation_effect.py",
    BASELINE / "skill" / "moderation_effect" / "run_moderation_effect.py",
    BASELINE / "skill" / "result_synthesis" / "run_result_synthesis.py",
)


def main() -> int:
    config = yaml.safe_load((BASELINE / "config" / "analysis_config.yaml").read_text(encoding="utf-8"))
    project_output = BASELINE / "output" / config["project"]["name"]
    sample = config["data"]["sample_info"]
    label = f"{sample['sample_period']}（{'、'.join(sample['sample_rules']) or '全样本'}）"
    if project_output.exists():
        print(f"演示输出已存在，停止以免覆盖：{project_output}")
        print("请先查看已有结果；需要重跑时，请在配置中更换 project.name。")
        return 1
    commands = [ROOT / "scripts" / "generate_demo_data.py", *RUNNERS]
    for index, script in enumerate(commands, 1):
        print(f"\n[{index}/{len(commands)}] {script.name}", flush=True)
        result = subprocess.run([sys.executable, str(script)], cwd=ROOT, check=False)
        if result.returncode:
            print(f"步骤失败：{script.name}；请保留终端错误信息并提交问题反馈。")
            return result.returncode
    print(f"\n演示完成。请打开：{project_output / label / 'result_synthesis'}")
    print("优先查看 merged_full_results.xlsx；严格筛选表为空并不表示运行失败。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
