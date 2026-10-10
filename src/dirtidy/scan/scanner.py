"""目录扫描：生成 inventory，可选计算文件内容 sha256。

大目录：
- 内容 hash 走线程池；
- 结果写进 `.dirtidy/hash_cache.sqlite`（SQLite，见 scan/hashcache.py），
  再次扫描时按 path+size+mtime 复用，相当于可恢复的 hash 进度；
- 进度回调按 1% 节流，几百万文件时不会变成几百万次 Python 调用。

hash 是**按需**的：`hash_items()` 支持只对「size 落在给定集合里」的文件算 hash。
重复组与归档残留对照都要求同 size（**同内容必然同字节数**），所以只算必要的那批，
结论与全量算一致，却省掉绝大部分 IO 与常驻 sha256 字符串。见 `docs/07`。
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from collections.abc import Callable, Collection
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dirtidy.models import InventoryItem, state_root
from dirtidy.scan.hashcache import HashCache

# 扫描时跳过，避免自递归或把 VCS 元数据当业务垃圾处理
SKIP_DIR_NAMES = {".dirtidy", ".git", ".hg", ".svn"}

#: 进度回调最少间隔条数（按总量 1% 节流，且不超过 500 条一次）
_PROGRESS_MAX_STEP = 500


def file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """流式计算文件 sha256，避免大文件整读进内存。"""
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def hash_cache_path(root: Path) -> Path:
    return state_root(root) / "hash_cache.sqlite"


def legacy_hash_cache_path(root: Path) -> Path:
    return state_root(root) / "hash_cache.json"


def _walk_tree(root: Path) -> tuple[list[InventoryItem], list[dict]]:
    """只扫元数据（相对路径 / size / mtime），不碰文件内容。

    权限错误记进 errors 并继续，不中止整次扫描。
    """
    items: list[InventoryItem] = []
    errors: list[dict] = []

    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        current = Path(dirpath)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        rel_dir = current.relative_to(root)
        # 统一用 posix 相对路径，保证跨平台产物可比
        rel_dir_s = "." if str(rel_dir) == "." else rel_dir.as_posix()

        if rel_dir_s != ".":
            try:
                st = current.stat()
                items.append(
                    InventoryItem(
                        path=rel_dir_s,
                        is_dir=True,
                        size=0,
                        mtime=st.st_mtime,
                    )
                )
            except OSError as e:
                errors.append({"path": rel_dir_s, "error": str(e)})

        for name in filenames:
            p = current / name
            rel = p.relative_to(root).as_posix()
            try:
                st = p.stat()
                items.append(
                    InventoryItem(
                        path=rel,
                        is_dir=False,
                        size=int(st.st_size),
                        mtime=st.st_mtime,
                    )
                )
            except OSError as e:
                errors.append({"path": rel, "error": str(e)})
                items.append(
                    InventoryItem(
                        path=rel,
                        is_dir=False,
                        size=0,
                        mtime=0.0,
                        error=str(e),
                    )
                )

    return items, errors


def hash_items(
    root: Path,
    items: list[InventoryItem],
    *,
    sizes: set[int] | None = None,
    paths: Collection[str] = (),
    max_hash_bytes: int = 64 * 1024 * 1024,
    workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
    use_hash_cache: bool = True,
) -> list[dict]:
    """对**选中**的文件算 sha256，原地写回 `items[i].sha256`；返回 hash 阶段的错误列表。

    选中规则：`sizes is None` 时全选；否则选中 `item.size in sizes or item.path in paths`。
    已经是目录、有 error、已有 sha256、或超过 `max_hash_bytes` 的都不算。

    为什么允许只算一部分：重复组要求同 size，归档残留对照也要求同 size
    （**同内容必然同字节数**）。所以「size 出现 ≥2 次的文件」∪「size 命中某个归档成员的文件」
    就是全部必要集——没被算到的那些，既不可能与别的文件重复，也不可能与归档成员相同。
    这条推理是 `docs/07` 那笔「按 size 分桶」账能成立的全部依据，改动前务必先看 `docs/07`。
    """
    errors: list[dict] = []

    cache: HashCache | None = None
    if use_hash_cache:
        cache = HashCache(hash_cache_path(root), uuid.uuid4().hex)
        if cache.open():
            migrated = cache.migrate_legacy(legacy_hash_cache_path(root))
            if migrated:
                print(f"提示: 已把旧 JSON 哈希缓存迁移到 SQLite（{migrated} 条）", file=sys.stderr)
        else:
            cache = None

    def wanted(it: InventoryItem) -> bool:
        """是否属于本次要算 hash 的集合（不含「已有 hash」这类运行期状态）。"""
        if it.is_dir or it.error:
            return False
        if sizes is None:
            return True
        return it.size in sizes or it.path in paths

    try:
        # 批量复用缓存：一次 JOIN，只把命中项拿回 Python 侧
        if cache is not None and cache.usable:
            hits = cache.lookup_many((it.path, it.size, it.mtime) for it in items if wanted(it))
            if hits:
                for it in items:
                    if not it.is_dir and it.sha256 is None:
                        digest = hits.get(it.path)
                        if digest:
                            it.sha256 = digest

        to_hash: list[tuple[str, Path, int, float]] = [
            (it.path, root / it.path, it.size, it.mtime)
            for it in items
            if wanted(it) and it.sha256 is None and it.size <= max_hash_bytes
        ]

        digests: dict[str, str] = {}
        total = len(to_hash)
        done = 0
        step = max(1, min(total // 100 or 1, _PROGRESS_MAX_STEP))

        def _one(job: tuple[str, Path, int, float]) -> tuple[str, str | None, str | None]:
            rel, path, _size, _mtime = job
            try:
                return rel, file_sha256(path), None
            except OSError as e:
                return rel, None, str(e)

        if to_hash:
            pool_size = max(1, workers)
            with ThreadPoolExecutor(max_workers=pool_size) as pool:
                for rel, digest, err in pool.map(_one, to_hash):
                    done += 1
                    if err:
                        errors.append({"path": rel, "error": f"hash: {err}"})
                    elif digest:
                        digests[rel] = digest
                    if on_progress and (done % step == 0 or done == total):
                        on_progress(done, total)

        for it in items:
            if it.sha256 is None and it.path in digests:
                it.sha256 = digests[it.path]

        if cache is not None and cache.usable:
            cache.upsert_many(
                (it.path, it.size, it.mtime, it.sha256)
                for it in items
                if not it.is_dir and not it.error and it.sha256
            )
            cache.prune()
    finally:
        if cache is not None:
            cache.close()

    return errors


def scan_tree(
    root: Path,
    *,
    quick: bool = False,
    max_hash_bytes: int = 64 * 1024 * 1024,
    workers: int = 4,
    on_progress: Callable[[int, int], None] | None = None,
    use_hash_cache: bool = True,
) -> tuple[list[InventoryItem], list[dict]]:
    """递归扫描 root。

    quick=True 时跳过内容 hash（更快，但无法做重复/解压残留判定）。
    单文件超过 max_hash_bytes 也不哈希，防止拖垮超大媒体盘。
    workers>1 时并行计算文件 hash。
    权限错误写入 errors，不中止整次扫描。

    quick=False 会对**全部**文件算 hash，适合「只要一份带 hash 的 inventory」的调用方。
    编排层（`pipeline.create_run`）不走这条路：它先 `scan_tree(quick=True)` 扫元数据，
    再 `hash_items(sizes=…)` 只算必要的那批——因为要等归档成员解析出来，
    才知道哪些 size 值得算（见 `hash_items` 的说明）。
    """
    root = root.resolve()
    items, errors = _walk_tree(root)
    if not quick:
        errors.extend(
            hash_items(
                root,
                items,
                max_hash_bytes=max_hash_bytes,
                workers=workers,
                on_progress=on_progress,
                use_hash_cache=use_hash_cache,
            )
        )
    return items, errors

