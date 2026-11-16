"""Shared, location-independent path resolution for baseline modules."""

from pathlib import Path
import re
from typing import Any, Mapping

BASELINE_DIR = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BASELINE_DIR.parent.parent


def directory_component(value: Any, field: str) -> str:
    """Reject ambiguous names instead of silently merging different projects."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空文本")
    if value != value.strip() or value in {".", ".."} or value.endswith("."):
        raise ValueError(f"{field} 不允许首尾空白、尾部句点或 . / ..")
    if re.search(r'[\x00-\x1f/\\:*?"<>|]', value):
        raise ValueError(f"{field} 含不适合作为目录名的字符: {value!r}")
    return value


def get_project_output_root(config: Mapping[str, Any]) -> Path:
    """Output paths never fall back to a pre-existing directory in another root."""
    value = config.get("output_settings", {}).get("output_root", "output")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("output_settings.output_root 必须是非空路径")
    root = Path(value).expanduser()
    if not root.is_absolute():
        root = Path(config.get("_base_dir") or BASELINE_DIR) / root
    name = directory_component(config.get("project", {}).get("name"), "project.name")
    return root.resolve() / name


def get_sample_output_dir(config: Mapping[str, Any]) -> Path:
    """Use sample labels only for directories; they never filter input rows."""
    info = config.get("data", {}).get("sample_info", {})
    period = directory_component(info.get("sample_period", "unknown_period"), "data.sample_info.sample_period")
    rules = info.get("sample_rules", [])
    if not isinstance(rules, list):
        raise ValueError("data.sample_info.sample_rules 必须是文本列表")
    safe_rules = [directory_component(rule, "data.sample_info.sample_rules") for rule in rules]
    return get_project_output_root(config) / f"{period}（{'、'.join(safe_rules) if safe_rules else '全样本'}）"


def check_output_overwrite(path: Path, config: Mapping[str, Any], module_config=None) -> None:
    """Global overwrite switch is authoritative; a module may only tighten it."""
    allowed = config.get("run_settings", {}).get("overwrite_existing", True)
    if module_config is not None:
        allowed = allowed and module_config.get("output", {}).get("overwrite_existing", True)
    if path.exists() and (not path.is_dir() or any(path.iterdir())) and not allowed:
        raise FileExistsError(f"输出目录已有结果且禁止覆盖: {path}")


def resolve_config_path(path_value: str | Path, config: Mapping[str, Any], must_exist: bool = False) -> Path:
    """Resolve absolute paths directly and relative paths against stable project locations."""
    value = str(path_value).strip()
    if not value:
        raise ValueError("配置路径不能为空")
    raw = Path(value).expanduser()
    if raw.is_absolute():
        candidates = [raw]
    else:
        base = Path(config.get("_base_dir") or BASELINE_DIR)
        config_dir = Path(config.get("_config_dir") or base / "config")
        candidates = list(dict.fromkeys([
            base / raw,
            config_dir / raw,
            BASELINE_DIR / raw,
            BASELINE_DIR.parent / raw,
            PROJECT_ROOT / raw,
        ]))
    found = next((candidate for candidate in candidates if candidate.exists()), None)
    if must_exist and found is None:
        attempted = "\n".join(str(candidate.resolve()) for candidate in candidates)
        raise FileNotFoundError(f"配置路径不存在，已尝试：\n{attempted}")
    return (found or candidates[0]).resolve()
