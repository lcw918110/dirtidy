"""定时监视：定期扫描并报告**新增**的规则命中，绝不执行隔离。

硬边界见 `docs/02` FR-11：监视天然是「自动触发」，而本工具的核心价值是
「自动只到候选/隔离区为止，执行必须人工」。所以这里只有「扫描 → 比对 → 通知」三步，
代码里**不存在** apply 这条路径——不是靠约定，是靠不写。
`tests/test_watch.py` 里有静态守护，防止日后有人顺手加回来。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from dirtidy.deadline import Deadline, RunTimeout
from dirtidy.pipeline import protect_rules_from_paths
from dirtidy.quarantine.store import drop_covered_descendants
from dirtidy.rules.engine import load_rule_pack, match_rules
from dirtidy.scan.scanner import scan_tree


class StopSignal(Protocol):
    """停止信号（`threading.Event` 同形）。

    只要求 `is_set` / `wait` 两个方法，这样 watch 不必 import threading——
    CLI 直接跑时传 None，等待就退化成普通 sleep；Web 后台线程传 Event，
    停止时能立刻醒来，不必等满一个 interval。
    """

    def is_set(self) -> bool: ...

    def wait(self, timeout: float | None = None) -> bool: ...


def scan_hits(
    root: Path,
    *,
    rules: str = "default",
    quick: bool = True,
    protect: Sequence[str] = (),
    deadline: Deadline | None = None,
) -> set[str]:
    """扫一遍，返回会被自动隔离的相对路径集合。

    **不落盘、不执行**：watch 只回答「现在有什么」，动不动是人的决定。
    也因此不产生 run 产物——每半小时留一份 run 目录会把 `.dirtidy/` 撑爆。

    deadline 到点会抛 `RunTimeout`，由 `run_watch` 接住并跳过本轮。
    """
    items, _errors = scan_tree(root, quick=quick, deadline=deadline)
    pack = load_rule_pack(rules)
    match = match_rules(root, items, pack, extra_protect_rules=protect_rules_from_paths(protect))
    # 目录命中会覆盖其内的文件命中（执行时只搬父目录）。这里沿用执行侧同一份去重，
    # 否则报「5 项」而实际只会动 4 项，用户按数字去核对会以为漏了。
    return {h.path for h in drop_covered_descendants(match.hits)}


def diff_hits(previous: set[str], current: set[str]) -> tuple[set[str], set[str]]:
    """返回 (新增, 消失)。

    消失也要报——那说明用户自己处理掉了，这是正反馈，不该静默。
    """
    return current - previous, previous - current


def notify(title: str, message: str) -> bool:
    """尽力发一条系统通知；失败静默返回 False。

    通知是锦上添花：发不出去不能让监视中断，更不能让它抛异常。
    """
    try:
        if sys.platform == "darwin":
            script = (
                f"display notification {json.dumps(message, ensure_ascii=False)} "
                f"with title {json.dumps(title, ensure_ascii=False)}"
            )
            subprocess.run(
                ["osascript", "-e", script], check=False, capture_output=True, timeout=5
            )
            return True
        if sys.platform.startswith("linux") and shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", title, message], check=False, capture_output=True, timeout=5
            )
            return True
    except Exception:
        return False
    return False


def _report(
    root: Path,
    added: set[str],
    removed: set[str],
    total: int,
    *,
    notify_enabled: bool,
    preview: int = 10,
) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    if not added and not removed:
        print(f"[{stamp}] 无变化（当前命中 {total} 项）", flush=True)
        return

    print(f"[{stamp}] 新增 {len(added)} 项，消失 {len(removed)} 项（当前命中 {total} 项）", flush=True)
    for path in sorted(added)[:preview]:
        print(f"  + {path}", flush=True)
    if len(added) > preview:
        print(f"  … 其余 {len(added) - preview} 项略", flush=True)
    for path in sorted(removed)[:preview]:
        print(f"  - {path}", flush=True)

    if notify_enabled and added:
        lines = "\n".join(sorted(added)[:5])
        more = f"\n… 共 {len(added)} 项" if len(added) > 5 else ""
        notify("dirtidy 发现新的可清理项", f"{lines}{more}\n\n查看：dirtidy desktop {root}")


def _report_timeout(e: RunTimeout, rounds: int, *, first: bool) -> None:
    """超时也要说话，而且要说清「下一轮照常」——否则用户以为监视已经死了。"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print(
        f"[{stamp}] 第 {rounds} 轮扫描超时：{e.phase}阶段超过 {e.seconds:g} 秒"
        f"（已处理 {e.processed} 项）。",
        flush=True,
    )
    if first:
        print(
            "  本轮跳过，下一轮照常。若属正常（盘慢 / 目录很大），"
            "用 --timeout 调大或设为 0 关闭。",
            flush=True,
        )


# 单轮扫描的默认时限（秒）。**监视是无人值守的**：一轮卡在慢盘/NFS 上就等于整个
# 监视静默停摆。CLI `watch --timeout` 与 Web 启动监视都从这里取默认值，
# 两处各写一个数字迟早对不上——对不上的那天，网页上开的监视会比命令行开的更容易
# 变成僵尸体，而用户以为自己在盯着。
WATCH_DEFAULT_TIMEOUT: float = 1800.0


def run_watch(
    root: Path,
    *,
    interval_minutes: float = 30.0,
    rules: str = "default",
    quick: bool = True,
    protect: Sequence[str] = (),
    max_rounds: int | None = None,
    notify_enabled: bool = True,
    timeout: float = WATCH_DEFAULT_TIMEOUT,
    stop_event: StopSignal | None = None,
    on_round: Callable[[dict[str, Any]], None] | None = None,
    _sleep: Callable[[float], None] = time.sleep,
    _scan: Callable[..., set[str]] = scan_hits,
) -> int:
    """定期扫描并报告变化，直到 Ctrl-C、跑满 max_rounds 或被 stop_event 叫停。

    首轮必然把所有命中报成「新增」——这是有意的：用户启动监视时就想知道
    「现在有哪些待处理」，而不是等第二次扫描才有反馈。

    stop_event 非 None 时，等待改用 `stop_event.wait(timeout)`：界面点「停止」
    能立刻退出，而不是傻等满一个 interval（默认 30 分钟，用户会以为按钮坏了）。
    on_round 每轮结束回调一次，供 Web 侧记录进度。

    timeout > 0 时给**每一轮**单独设时限（见 `docs/06` T-3）。监视是无人值守的，
    一轮卡在慢盘上就等于整个监视静默停摆——超时至少能报一句、并且继续跑下一轮。
    超时那轮**不动 `previous`**：拿只扫了一半的集合去比，会把没扫到的东西全报成
    「消失」，那是在编数据。下一轮仍与最后一次**完整**的扫描结果比对。
    """
    previous: set[str] = set()
    rounds = 0
    print(f"开始监视 {root}，每 {interval_minutes:g} 分钟扫描一次（只扫描，不执行任何隔离）。")
    if timeout > 0:
        print(f"单轮扫描时限 {timeout:g} 秒；超时只跳过本轮，不影响后续轮次。")
    print("按 Ctrl-C 停止。\n")

    def _wait(seconds: float) -> None:
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            _sleep(seconds)

    failing = False

    try:
        while max_rounds is None or rounds < max_rounds:
            if stop_event is not None and stop_event.is_set():
                break
            # 每轮一个独立时限：一轮卡住不该把后面的轮次也一起判死刑。
            deadline = Deadline(timeout)
            try:
                current = _scan(
                    root, rules=rules, quick=quick, protect=protect, deadline=deadline
                )
            except RunTimeout as e:
                rounds += 1
                _report_timeout(e, rounds, first=not failing)
                # 只在「进入超时状态」时通知一次，避免每轮都弹一条把人淹掉。
                if notify_enabled and not failing:
                    notify(
                        "dirtidy 监视：单轮扫描超时",
                        f"{e.phase}阶段超过 {e.seconds:g} 秒（已处理 {e.processed} 项）。\n"
                        f"本轮已跳过，下一轮照常。\n\n可用 --timeout 调大或设为 0 关闭。",
                    )
                failing = True
                if on_round is not None:
                    on_round(
                        {
                            "rounds": rounds,
                            "total": len(previous),
                            "added": [],
                            "removed": [],
                            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                            "timed_out": True,
                            "error": str(e),
                        }
                    )
                if max_rounds is not None and rounds >= max_rounds:
                    break
                _wait(interval_minutes * 60)
                continue
            failing = False
            added, removed = diff_hits(previous, current)
            _report(root, added, removed, len(current), notify_enabled=notify_enabled)
            previous = current
            rounds += 1
            if on_round is not None:
                on_round(
                    {
                        "rounds": rounds,
                        "total": len(current),
                        "added": sorted(added),
                        "removed": sorted(removed),
                        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "timed_out": False,
                    }
                )
            if max_rounds is not None and rounds >= max_rounds:
                break
            _wait(interval_minutes * 60)
    except KeyboardInterrupt:
        print("\n已停止监视。")
    return 0
