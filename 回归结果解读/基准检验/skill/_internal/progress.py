"""One compact progress bar per regression stage."""

from __future__ import annotations

import sys
from typing import Callable, Iterable, Iterator, TypeVar

T = TypeVar("T")

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None


def stage_progress(items: Iterable[T], *, total: int, module: str, stage: str,
                   hypothesis: Callable[[T], str], position: int = 0,
                   leave: bool = True) -> Iterator[T]:
    """Show stage completion after each task, with the hypothesis being evaluated."""
    if total == 0:
        print(f"{module}｜{stage}｜无待检验假设", flush=True)
        return

    if tqdm is None:
        for completed, item in enumerate(items, start=1):
            label = str(hypothesis(item) or "未编号")
            yield item
            print(f"{module}｜{stage}｜假设 {label}｜{completed / total:.0%} ({completed}/{total})",
                  flush=True)
        return

    with tqdm(total=total, desc=f"{module}｜{stage}｜假设 待开始", file=sys.stdout,
              leave=leave, position=position, dynamic_ncols=False,
              bar_format="{desc}｜{bar:20}｜{percentage:3.0f}% ({n_fmt}/{total_fmt})｜已用 {elapsed} / 剩余 {remaining}") as bar:
        for item in items:
            label = str(hypothesis(item) or "未编号")
            bar.set_description_str(f"{module}｜{stage}｜假设 {label}", refresh=True)
            yield item
            bar.update(1)
