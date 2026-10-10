"""规模基准：条目内存开销、扫描吞吐、inventory 读写峰值。

用法：
    python scripts/bench_scale.py                 # 默认 2000 文件 / 200 目录
    python scripts/bench_scale.py --files 20000

只依赖标准库。合成树建在**当前工作目录**的 `.bench-tmp/`（已进 .gitignore），跑完即删——
不用系统临时目录，是因为某些沙箱不允许往 /tmp 大批建文件；反过来，若所在环境会拦截
「工作目录内批量删除」，请先 `cd` 到一个可自由删除的目录再运行。

注意 [1] 条目内存：样本里每条都必须是**新造**的对象。用 `mtime=1.0`、`suffix=".py"`
这类字面量做样本会被解释器共享，测出来的数字明显偏低（旧版正因此把 5M 条的账算少了一半）。
"""

from __future__ import annotations

import argparse
import json
import resource
import shutil
import sys
import time
import tracemalloc
from collections.abc import Callable
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from dirtidy.models import InventoryItem, iter_json_array, write_json, write_json_stream  # noqa: E402
from dirtidy.scan.scanner import scan_tree  # noqa: E402

N_ENTRIES = 50_000


def _rss_mb() -> float:
    """当前进程峰值 RSS（MB）。macOS 上 ru_maxrss 单位是字节，Linux 是 KB。"""
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw / (1024 * 1024) if sys.platform == "darwin" else raw / 1024


def _row(label: str, per: float) -> None:
    print(
        f"  {label} {per:6.1f} B/条   "
        f"1M={per * 1e6 / 2**20:7.1f} MB   5M={per * 5e6 / 2**20:8.1f} MB"
    )


def _measure(build: Callable[[], list[Any]]) -> float:
    """跑一次构造，返回每条的平均常驻字节数。

    用 tracemalloc 而不是 `sys.getsizeof`：前者统计**实际分配**，
    能把每条各自的路径字符串、mtime、hash 都算进去。
    """
    tracemalloc.start()
    base = tracemalloc.get_traced_memory()[0]
    objs = build()
    used = tracemalloc.get_traced_memory()[0] - base
    tracemalloc.stop()
    del objs
    return used / N_ENTRIES


def bench_entry_memory() -> None:
    """单个 InventoryItem 的常驻开销。

    关键：每条都必须造**新**的 mtime 与 sha256 对象。写成 `mtime=1.0` 这类字面量会被
    解释器共享，测出来的数字明显低于真实扫描场景——旧版基准正因此把 5M 条的账算少了一半。
    """

    def current() -> list[Any]:
        return [
            InventoryItem(
                path=f"proj{i % 50}/svc{i}/src/module_{i}.py",
                is_dir=False,
                size=i,
                mtime=1_700_000_000.0 + i,
                sha256=f"{i:064x}",
            )
            for i in range(N_ENTRIES)
        ]

    class _WithSuffix:
        """对照：第一期保留 suffix 字段的结构（现已改为按需现算）。"""

        __slots__ = ("path", "is_dir", "size", "mtime", "suffix", "sha256", "error")

        def __init__(
            self,
            path: str,
            is_dir: bool,
            size: int,
            mtime: float,
            suffix: str = "",
            sha256: str | None = None,
        ) -> None:
            self.path, self.is_dir, self.size, self.mtime = path, is_dir, size, mtime
            self.suffix, self.sha256, self.error = suffix, sha256, None

    def legacy() -> list[Any]:
        return [
            _WithSuffix(
                f"proj{i % 50}/svc{i}/src/module_{i}.py",
                False,
                i,
                1_700_000_000.0 + i,
                # 必须运行时生成：写成 "." + "py" 会被编译器常量折叠成共享字面量，
                # 于是这条对照反而测不出 suffix 的真实开销。
                f".{i % 100:02d}",
                f"{i:064x}",
            )
            for i in range(N_ENTRIES)
        ]

    class _NoSlots:
        """对照：不用 slots 的等价对象（多背一个 __dict__）。"""

        def __init__(
            self, path: str, is_dir: bool, size: int, mtime: float, sha256: str | None = None
        ) -> None:
            self.path, self.is_dir, self.size, self.mtime = path, is_dir, size, mtime
            self.sha256, self.error = sha256, None

    def no_slots() -> list[Any]:
        return [
            _NoSlots(
                f"proj{i % 50}/svc{i}/src/module_{i}.py", False, i, 1_700_000_000.0 + i, f"{i:064x}"
            )
            for i in range(N_ENTRIES)
        ]

    _row("InventoryItem（slots=True）  ", _measure(current))
    _row("对照：含 suffix 的旧结构    ", _measure(legacy))
    _row("对照：无 slots 的等价对象   ", _measure(no_slots))


def build_tree(root: Path, n_files: int) -> None:
    for i in range(n_files):
        d = root / f"proj{i % 50}" / f"svc{i}" / ("node_modules" if i % 7 == 0 else "src")
        d.mkdir(parents=True, exist_ok=True)
        (d / f"f{i}.txt").write_text(f"payload-{i}\n", encoding="utf-8")
    for i in range(max(1, n_files // 10)):
        (root / f"proj{i % 50}" / f"svc{i}" / "logs").mkdir(parents=True, exist_ok=True)
        (root / f"proj{i % 50}" / f"svc{i}" / "logs" / "app.log").write_text("x\n", encoding="utf-8")


def bench_scan(root: Path) -> None:
    for quick in (True, False):
        before = _rss_mb()
        t0 = time.perf_counter()
        items, errors = scan_tree(root, quick=quick, workers=4)
        dt = time.perf_counter() - t0
        after = _rss_mb()
        tag = "quick" if quick else "full "
        print(
            f"  scan_tree({tag})  {len(items):>6} 条 / {dt:6.2f}s "
            f"({len(items)/max(dt,1e-9):9.0f} 条/s)  峰值 RSS {after:7.1f} MB（+{after-before:.1f}）"
            f"  错误 {len(errors)}"
        )


def bench_inventory_io(root: Path, items: list[InventoryItem]) -> None:
    big = root.parent / "inventory_bench.json"
    naive = big.with_name("inventory_bench_naive.json")

    tracemalloc.start()
    t0 = time.perf_counter()
    write_json_stream(big, (i.to_dict() for i in items))
    dt_ws = time.perf_counter() - t0
    peak_stream = tracemalloc.get_traced_memory()[1] / 2**20
    tracemalloc.stop()

    tracemalloc.start()
    t0 = time.perf_counter()
    payload = [i.to_dict() for i in items]
    write_json(naive, payload)
    dt_wn = time.perf_counter() - t0
    peak_naive = tracemalloc.get_traced_memory()[1] / 2**20
    tracemalloc.stop()
    del payload

    t0 = time.perf_counter()
    n = sum(1 for _ in iter_json_array(big))
    dt_stream = time.perf_counter() - t0

    tracemalloc.start()
    t0 = time.perf_counter()
    n2 = len(json.loads(big.read_text(encoding="utf-8")))
    dt_naive = time.perf_counter() - t0
    peak_read = tracemalloc.get_traced_memory()[1] / 2**20
    tracemalloc.stop()

    size_mb = big.stat().st_size / 2**20
    print(
        f"  inventory 写盘    流式 峰值 {peak_stream:6.1f} MB / {dt_ws:5.2f}s"
        f"   朴素 峰值 {peak_naive:6.1f} MB / {dt_wn:5.2f}s   文件 {size_mb:.1f} MB"
    )
    print(
        f"  inventory 读回    流式 {n} 条 / {dt_stream:5.2f}s / 峰值 {peak_stream:6.1f} MB"
        f"   json.loads {n2} 条 / {dt_naive:5.2f}s / 峰值 {peak_read:6.1f} MB"
    )
    big.unlink(missing_ok=True)
    naive.unlink(missing_ok=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=500)
    args = ap.parse_args()

    print(f"[1] 条目内存（{N_ENTRIES} 条样本）")
    bench_entry_memory()

    # 合成树建在工作目录下：某些沙箱/只读环境不允许往系统临时目录里大批建文件
    tmp = Path.cwd() / ".bench-tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        print(f"\n[2] 合成目录树：{args.files} 文件 → {tmp}")
        t0 = time.perf_counter()
        build_tree(tmp, args.files)
        print(f"  建树耗时 {time.perf_counter()-t0:.2f}s")
        bench_scan(tmp)

        print("\n[3] inventory 读写")
        items, _ = scan_tree(tmp, quick=True, use_hash_cache=False)
        bench_inventory_io(tmp, items)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("\n完成。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
