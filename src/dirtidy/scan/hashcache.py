"""SQLite 哈希缓存：常驻内存与缓存规模无关，且增量写入。

原来的 `.dirtidy/hash_cache.json` 在百万文件下有两条硬伤：

1. **读**：`json.loads` 会把整份文本与解析结果同时留在内存（实测放大约 2.6 倍），
   5M 条缓存的常驻峰值轻松上 GB；
2. **写**：每轮扫描都把全量缓存重写一遍，缓存 1 GB 就要再写 1 GB。

换成 SQLite 后：查一批是一次 JOIN，写一批是一次 upsert，删旧行是一条 DELETE，
Python 侧只保留「命中集合」这一个与命中数同阶的小字典。
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hashes (
    path TEXT PRIMARY KEY,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    sha256 TEXT NOT NULL,
    scan_id TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_hashes_scan_id ON hashes(scan_id);
"""


class HashCache:
    """`.dirtidy/hash_cache.sqlite` 的薄封装；任何一步失败都降级为「无缓存」。"""

    __slots__ = ("path", "_conn", "_scan_id")

    def __init__(self, path: Path, scan_id: str) -> None:
        self.path = Path(path)
        self._scan_id = scan_id
        self._conn: sqlite3.Connection | None = None

    # ---------------------------------------------------------------- 生命周期

    def open(self) -> bool:
        """打开缓存；不可用（只读根 / 磁盘满 / 文件损坏）时返回 False，不抛异常。"""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=10.0)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(_SCHEMA)
            conn.commit()
        except (sqlite3.Error, OSError):
            self._conn = None
            return False
        self._conn = conn
        return True

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass
            self._conn = None

    def __enter__(self) -> HashCache:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        self.close()
        return False

    @property
    def usable(self) -> bool:
        return self._conn is not None

    # ---------------------------------------------------------------- 读

    def lookup_many(self, rows: Iterable[tuple[str, int, float]]) -> dict[str, str]:
        """批量查缓存，只返回命中项：{path: sha256}。

        走一张 TEMP 表 + 一次 JOIN，避免几百万次单条查询的往返开销；
        rows 用生成器喂进来，所以 Python 侧不会多出一份全量列表。
        """
        if self._conn is None:
            return {}
        try:
            self._conn.execute("CREATE TEMP TABLE IF NOT EXISTS probe(path TEXT PRIMARY KEY, size INTEGER, mtime REAL)")
            self._conn.execute("DELETE FROM probe")
            self._conn.executemany(
                "INSERT OR REPLACE INTO probe(path, size, mtime) VALUES(?,?,?)",
                ((p, int(s), float(m)) for p, s, m in rows),
            )
            cur = self._conn.execute(
                "SELECT h.path, h.sha256 FROM hashes h JOIN probe p "
                "ON p.path = h.path AND p.size = h.size AND p.mtime = h.mtime"
            )
            return dict(cur)
        except sqlite3.Error:
            return {}

    # ---------------------------------------------------------------- 写

    def upsert_many(self, rows: Iterable[tuple[str, int, float, str]]) -> int:
        if self._conn is None:
            return 0
        n = 0
        try:
            cur = self._conn.executemany(
                "INSERT INTO hashes(path, size, mtime, sha256, scan_id) VALUES(?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime, "
                "sha256=excluded.sha256, scan_id=excluded.scan_id",
                ((p, int(s), float(m), h, self._scan_id) for p, s, m, h in rows),
            )
            n = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
            self._conn.commit()
        except sqlite3.Error:
            return n
        return n

    def prune(self) -> int:
        """删掉本轮扫描没碰到的旧行，避免缓存随目录变更无限膨胀。"""
        if self._conn is None:
            return 0
        try:
            cur = self._conn.execute("DELETE FROM hashes WHERE scan_id <> ?", (self._scan_id,))
            self._conn.commit()
            return int(cur.rowcount)
        except sqlite3.Error:
            return 0

    def stats(self) -> dict[str, int]:
        if self._conn is None:
            return {"entries": 0, "bytes": 0}
        try:
            row = self._conn.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM hashes").fetchone()
        except sqlite3.Error:
            return {"entries": 0, "bytes": 0}
        return {"entries": int(row[0]), "bytes": int(row[1])}

    # ---------------------------------------------------------------- 迁移

    def migrate_legacy(self, legacy: Path) -> int:
        """把旧版 `hash_cache.json` 一次性导入，成功后改名成 `.migrated`。

        这是唯一一处会把旧缓存整份读进内存的地方，且只发生一次。
        """
        if self._conn is None or not legacy.exists():
            return 0
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict):
            return 0
        rows = [
            (str(p), int(v.get("size") or 0), float(v.get("mtime") or 0.0), str(v["sha256"]))
            for p, v in data.items()
            if isinstance(v, dict) and v.get("sha256")
        ]
        if not rows:
            return 0
        self.upsert_many(rows)
        try:
            legacy.rename(legacy.with_name(legacy.name + ".migrated"))
        except OSError:
            pass
        return len(rows)
