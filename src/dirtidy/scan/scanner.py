"""目录扫描：生成 inventory，可选计算文件内容 sha256。

大目录：内容 hash 走线程池；结果写入 `.dirtidy/hash_cache.json`，
再次扫描时按 path+size+mtime 复用，相当于可恢复的 hash 进度。
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dirtidy.models import InventoryItem, read_json, state_root, write_json

# 扫描时跳过，避免自递归或把 VCS 元数据当业务垃圾处理
SKIP_DIR_NAMES = {".dirtidy", ".git", ".hg", ".svn"}


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
    return state_root(root) / "hash_cache.json"


def _load_hash_cache(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = read_json(path)
    except (OSError, ValueError):
        # 缓存损坏/不可读时静默降级为「无缓存」，不让整次扫描失败
        return {}
    return data if isinstance(data, dict) else {}


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
    to_hash: list[tuple[str, Path, int, float]] = []
    cache_path = hash_cache_path(root)
    cache = _load_hash_cache(cache_path) if use_hash_cache and not quick else {}

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
                digest = None
                cached = cache.get(rel) if isinstance(cache.get(rel), dict) else None
                if (
                    cached
                    and cached.get("size") == int(st.st_size)
                    and cached.get("mtime") == st.st_mtime
                    and cached.get("sha256")
                ):
                    digest = str(cached["sha256"])
                elif not quick and st.st_size <= max_hash_bytes and p.is_file():
                    to_hash.append((rel, p, int(st.st_size), st.st_mtime))
                items.append(
                    InventoryItem(
                        path=rel,
                        is_dir=False,
                        size=int(st.st_size),
                        mtime=st.st_mtime,
                        suffix=p.suffix.lower(),
                        sha256=digest,
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

    digests: dict[str, str] = {}
    total = len(to_hash)
    done = 0

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
                if on_progress:
                    on_progress(done, total)

    for item in items:
        if item.path in digests:
            item.sha256 = digests[item.path]

    if use_hash_cache and not quick:
        for item in items:
            if item.is_dir or not item.sha256 or item.error:
                continue
            cache[item.path] = {"size": item.size, "mtime": item.mtime, "sha256": item.sha256}
        # 丢弃已不在本次 inventory 中的旧键，避免缓存无限膨胀
        live = {i.path for i in items if not i.is_dir}
        cache = {k: v for k, v in cache.items() if k in live}
        try:
            write_json(cache_path, cache)
        except OSError as e:
            # 只读根 / 无写权限：缓存写不进去不应作废已完成的扫描成果
            import sys

            print(f"警告: 无法写入 hash 缓存 {cache_path}: {e}", file=sys.stderr)

    return items, errors
