"""协作式运行超时：只在**指定的安全位置**退出，不用 signal / 异步异常。

为什么不用 `signal.alarm` 或线程强杀：Python 没有安全的中断手段，异步异常可以落在
任意字节码边界上——包括 `write_json` 写到一半的时候。对 dirtidy 来说，
「跑了一半的产物」比「这次没跑完」难收拾得多（见 `docs/08` §1.1）。

所以超时做成**协作式**：在扫描与 hash 的循环里埋检查点，到点抛 `RunTimeout`，
并且把**已经扫到的部分结果**一起带出来——调用方照样落盘、照样报错，
用户拿到的是「这次跑了一部分」而不是「什么都没有」。

触发场景见 `docs/06` 待办 T-3：`watch` 遇到一块慢盘，一轮扫描长时间不返回，
整个监视就静默停住了。超时让它至少能报一句、并且继续跑下一轮。
"""

from __future__ import annotations

import time
from typing import Any

#: 每处理多少条做一次截止时间检查。目录多而每目录文件少时靠「每个目录查一次」兜底，
#: 单目录塞满文件时靠这个计数兜底（见 `docs/06` T-7）。
DEADLINE_CHECK_STEP = 500


class RunTimeout(Exception):
    """一次运行超过给定时限。

    携带已完成的部分结果，供调用方落盘——**不带结果的超时等于把这次的活白干**。
    """

    def __init__(
        self,
        *,
        seconds: float,
        phase: str,
        processed: int,
        partial_items: list[Any] | None = None,
        partial_errors: list[dict] | None = None,
    ) -> None:
        self.seconds = seconds
        self.phase = phase
        self.processed = processed
        self.partial_items = partial_items if partial_items is not None else []
        self.partial_errors = partial_errors if partial_errors is not None else []
        super().__init__(f"运行超过 {seconds:g} 秒（{phase} 阶段，已处理 {processed} 项）")


class Deadline:
    """协作式截止时间。`seconds <= 0` 或 `None` 表示不限制。

    用 `time.monotonic()` 而不是 `time.time()`：墙上时钟会被 NTP 校准或用户改系统时间
    往回拨，那会让「还剩 10 分钟」突然变成「已经超时」或永不超时。
    """

    def __init__(self, seconds: float | None) -> None:
        self.seconds = float(seconds or 0.0)
        self._end = time.monotonic() + self.seconds if self.seconds > 0 else None

    @property
    def enabled(self) -> bool:
        return self._end is not None

    def remaining(self) -> float | None:
        """剩余秒数；不限制时为 None。"""
        if self._end is None:
            return None
        return max(0.0, self._end - time.monotonic())

    def expired(self) -> bool:
        return self._end is not None and time.monotonic() >= self._end

    def check(
        self,
        *,
        phase: str,
        processed: int,
        partial_items: list[Any] | None = None,
        partial_errors: list[dict] | None = None,
    ) -> None:
        """到点就抛 `RunTimeout`；调用方在检查点把已完成的进度传进来。"""
        if self.expired():
            raise RunTimeout(
                seconds=self.seconds,
                phase=phase,
                processed=processed,
                partial_items=partial_items,
                partial_errors=partial_errors,
            )
