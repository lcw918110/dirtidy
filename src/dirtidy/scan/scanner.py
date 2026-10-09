"""目录扫描：生成 inventory，可选计算文件内容 sha256。

大目录：
- 内容 hash 走线程池；
- 结果写进 `.dirtidy/hash_cache.sqlite`（SQLite，见 scan/hashcache.py），
  再次扫描时按 path+size+mtime 复用，相当于可恢复的 hash 进度；
- 进度回调按 1% 节流，几百万文件时不会变成几百万次 Python 调用。
"""

from __future__ import annotations

import hashlib
import os
import sys
import uuid
from collections.abc import Callable
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
    """
    root = root.resolve()
    items: list[InventoryItem] = []
    errors: list[dict] = []

    cache: HashCache | None = None
    if use_hash_cache and not quick:
        cache = HashCache(hash_cache_path(root), uuid.uuid4().hex)
        if cache.open():
            migrated = cache.migrate_legacy(legacy_hash_cache_path(root))
            if migrated:
                print(f"提示: 已把旧 JSON 哈希缓存迁移到 SQLite（{migrated} 条）", file=sys.stderr)
        else:
            cache = None

    try:
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
                            suffix="",
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
                            suffix=p.suffix.lower(),
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
                            suffix=Path(name).suffix.lower(),
                            error=str(e),
                        )
                    )

        # 批量复用缓存：一次 JOIN，只把命中项拿回 Python 侧
        if cache is not None and cache.usable:
            hits = cache.lookup_many(
                (it.path, it.size, it.mtime) for it in items if not it.is_dir and not it.error
            )
            if hits:
                for it in items:
                    if not it.is_dir and it.sha256 is None:
                        digest = hits.get(it.path)
                        if digest:
                            it.sha256 = digest

        to_hash: list[tuple[str, Path, int, float]] = [
            (it.path, root / it.path, it.size, it.mtime)
            for it in items
            if not it.is_dir and it.sha256 is None and not it.error and it.size <= max_hash_bytes
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

    return items, errors
