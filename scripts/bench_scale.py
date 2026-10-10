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

from dirtidy.hashing.content import (  # noqa: E402
    archive_member_sizes,
    candidate_duplicate_sizes,
    probe_archives,
)
from dirtidy.models import InventoryItem, iter_json_array, write_json, write_json_stream  # noqa: E402
from dirtidy.scan.scanner import hash_items, scan_tree  # noqa: E402

N_ENTRIES = 50_000

#: 选择性 hash 的对照比例：真实树上「size 有重复 ∪ size 命中归档成员」通常是个位数百分比。
#: 取 5% 只是为了给一个可复现的对照点，具体比例不影响结论方向。
SELECTIVE_RATIO = 20


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

    def selective() -> list[Any]:
        """第二期默认形态：只对必要的那批文件保留 sha256，其余为 None。

        必要性来自「size 出现 ≥2 次」∪「size 命中归档成员」（见 docs/07 §3.2）。
        这里按 1/SELECTIVE_RATIO 的比例造样本，只为给一个可复现的对照点。
        """
        return [
            InventoryItem(
                path=f"proj{i % 50}/svc{i}/src/module_{i}.py",
                is_dir=False,
                size=i,
                mtime=1_700_000_000.0 + i,
                sha256=f"{i:064x}" if i % SELECTIVE_RATIO == 0 else None,
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
    _row("第二期：仅 1/20 带 sha256   ", _measure(selective))
    _row("对照：含 suffix 的旧结构    ", _measure(legacy))
    _row("对照：无 slots 的等价对象   ", _measure(no_slots))

    bench_size_selection()


def bench_size_selection() -> None:
    """「按 size 分桶」这一步本身的开销。

    它要判断「哪些 size 出现 ≥2 次」，因此绕不开一个与文件数同阶的集合。
    这里量的是最坏情形（所有 size 互不相同），也就是这个集合的**上界**：
    set 装的是 `items` 里已有的 int 对象引用，不新造整数，函数返回后即释放。

    必须看 tracemalloc 的**峰值**而不是「结束时仍占用」：真正大的那个 `seen` 集合
    在函数返回时就释放了，只看结束值会得到 0——那是把开销测没了，不是真的没有。
    """
    items = [
        InventoryItem(path=f"p{i}.txt", is_dir=False, size=i, mtime=1_700_000_000.0 + i)
        for i in range(N_ENTRIES)
    ]
    tracemalloc.start()
    tracemalloc.reset_peak()
    t0 = time.perf_counter()
    dups = candidate_duplicate_sizes(items)
    dt = time.perf_counter() - t0
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    per = peak / N_ENTRIES
    print(
        f"  选桶（全 unique size 的最坏情形）：{dt:5.3f}s，峰值额外 {per:5.1f} B/条 "
        f"(5M={per * 5e6 / 2**20:5.0f} MB，返回后即释放；dups={len(dups)})"
    )


def build_tree(root: Path, n_files: int) -> None:
    """合成一棵「像真工作区」的树。

    刻意混入两类文件，好让「选择性 hash」的收益可测：

    - 每 3 个里有 1 个是**同 size**（模拟缓存 / 日志里长度一致的文件）→ 该被算 hash；
    - 其余 size 基本互不相同 → 不该被算 hash。

    如果所有文件都同 size，选择率会是 100%，基准就完全测不出收益——那等于没测。
    """
    for i in range(n_files):
        d = root / f"proj{i % 50}" / f"svc{i}" / ("node_modules" if i % 7 == 0 else "src")
        d.mkdir(parents=True, exist_ok=True)
        if i % 3 == 0:
            (d / f"f{i}.txt").write_text("x" * 64, encoding="utf-8")
        else:
            (d / f"f{i}.txt").write_text(f"payload-{i}\n" + "x" * (i % 4096), encoding="utf-8")
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

    # 第二期默认路径：扫元数据 → 解析归档 → 只对必要 size 算 hash。
    # 与上面 full 的差值就是「不必要的那部分 hash」省下来的 IO。
    items, _errors = scan_tree(root, quick=True, use_hash_cache=False)
    probes = probe_archives(root, items)
    t0 = time.perf_counter()
    sizes = candidate_duplicate_sizes(items) | archive_member_sizes(probes)
    t_select = time.perf_counter() - t0
    before = _rss_mb()
    t0 = time.perf_counter()
    hash_items(root, items, sizes=sizes, use_hash_cache=False)
    dt = time.perf_counter() - t0
    after = _rss_mb()
    hashed = sum(1 for it in items if it.sha256)
    files = sum(1 for it in items if not it.is_dir)
    hashed_bytes = sum(it.size for it in items if it.sha256)
    all_bytes = sum(it.size for it in items if not it.is_dir)
    print(
        f"  pipeline(选择性)  {len(items):>6} 条 / {dt:6.2f}s  "
        f"算 hash {hashed}/{files} 文件（{hashed/max(files,1):5.1%}）"
        f"，{hashed_bytes/2**20:6.2f}/{all_bytes/2**20:6.2f} MB"
        f"（{hashed_bytes/max(all_bytes,1):5.1%}；选桶+解析归档 {t_select:.3f}s）"
        f"  峰值 RSS {after:7.1f} MB（+{after-before:.1f}）"
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
