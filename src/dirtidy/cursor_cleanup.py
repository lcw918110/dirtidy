"""清理本机 Cursor 的全局状态库 `state.vscdb`。

背景
----
Cursor 把 Agent / Composer 的全部运行时数据塞进一张巨大的键值表
`cursorDiskKV`（约 838 万行）。其中：

- `agentKv:blob:<hash>` —— agent 运行时大对象（图片 / 附件 / 上下文快照），
  **体积占比 99% 以上**（实测约 75 GB）；
- `bubbleId:<cid>:<bid>` —— 会话气泡（对话正文），约 767 万行，体积很小；
- `checkpointId:` / `composerData:` —— 检查点与会话元数据，量级极小；
- `composerHeaders` 表 —— 会话索引（标题 / 时间 / 活跃度）；
- `ItemTable` 表 —— 用户设置（插件 / 主题 / 快捷键），**永久保留，永不删**。

所以只删会话正文几乎不缩库；真正能腾出空间的是**没人再引用的 blob**。

核心判定：blob 是不是孤儿
------------------------
blob 的 key 就是它的 64 位 hex 哈希。它被谁引用，藏在**其他行的 value 的 JSON 里**。
用 SQL `INSTR` 逐个哈希查等于全表扫（单个哈希实测 7 分钟无结果），不可行；
正确做法是**顺序读一遍 value，用正则一次性提取所有被引用的哈希**。

⚠️ 引用来源必须扫全
-------------------
只扫 `bubbleId` / `checkpointId` / `composerData` 三个前缀**是不够的**：
实测本机库中 `ofsContent:`（14 个）、`codeBlockPartialInlineDiffFates:`（35 个）、
`composerConvHeaders:`（2 个）等前缀也引用着 blob。漏扫它们会导致
**误删仍被引用的 blob**。因此本模块扫描**除 blob 自身以外的全部键前缀**，
宁可多扫几分钟，也不误删活数据。

安全设计
--------
1. **默认干跑**。不传 `--apply` 只统计不改库——与 dirtidy 其他破坏性命令一致。
2. **绝不删 `ItemTable`**（用户设置）。
3. **只删无引用的孤儿 blob**，被引用的一个不动。
4. **在线模式绝不 VACUUM、绝不替换文件**（会锁死 Cursor）。
5. 写库用**小批次短事务 + 批间让路 + 锁退避重试**，尽量不影响 Cursor 使用。
6. 真正清理前强制检测 Cursor 是否在跑，**在跑就拒绝执行**。
7. 清理前做冷备份（优先 APFS clonefile），压缩后校验再原子替换。

已知坑（集成时踩过）
--------------------
- 任何对 `state.vscdb` 的第三方写连接都可能与 Cursor 抢 WAL 锁，导致 Cursor
  报 `storage file corrupted/moved/permission` 而拒绝启动。**退出 Cursor 后清理最稳妥。**
- 参考实现里 `blob_bytes` 恒为 0（删完从不量），本模块改为删前按 key 精确累加。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

#: Cursor 全局状态库的本机路径（macOS）。
DEFAULT_DB = Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"

#: blob 的 key 前缀（长度 13）。blob 哈希即 key 去掉此前缀后的 64 位 hex。
BLOB_PREFIX = "agentKv:blob:"
BLOB_PREFIX_LEN = len(BLOB_PREFIX)

#: blob 哈希格式：64 位小写 hex。
BLOB_HASH_RE = re.compile(r"[0-9a-f]{64}")

#: 引用来源**不能**扫 blob 自身——那是 75 GB，读它只为找引用没有意义。
BLOB_SELF_PREFIX = BLOB_PREFIX

#: 会话正文类前缀：既是清理目标（删过期会话），也是 blob 的主要引用来源。
SESSION_PREFIXES = ("bubbleId:%", "checkpointId:%", "composerData:%")

#: key 前缀长度（含冒号）。
_PREFIX_BUBBLE = len("bubbleId:")
_PREFIX_CHECKPOINT = len("checkpointId:")
_PREFIX_COMPOSER_DATA = len("composerData:")

#: 删 blob 时每批的行数。批越小，锁窗口越短。
BLOB_BATCH = 500

#: 锁退避重试的等待序列（秒）。
_LOCK_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)


class CursorCleanupError(RuntimeError):
    """清理过程中的可操作错误。CLI 捕获后转成退出码 1 + 人话提示。"""


def human_bytes(n: int) -> str:
    """按量级选单位。参考实现的 `human()` 只有 GB 一档，几十 MB 会显示成 `0.00 GB`。"""
    n = int(n)
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"


def default_backup_dir() -> Path:
    """冷备份目录：dirtidy 状态目录旁的 `CursorStateBackup-<时间戳>`。"""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path.home() / "Library/Application Support/Cursor/User/globalStorage" / f"CursorStateBackup-{stamp}"


def cursor_running() -> bool:
    """检测 Cursor 是否在跑。宁可误报（拒绝清理），不可漏报（边跑边改库）。

    macOS 的 `pgrep -f` 按正则匹配，路径里的 `.` / `()` 会误伤，故改用 `ps` 子串判断。
    """
    markers = (
        "/Applications/Cursor.app/Contents/MacOS/Cursor",
        "/Applications/Cursor.app/Contents/Frameworks/Cursor Helper",
    )
    try:
        out = subprocess.check_output(["ps", "-Ax", "-o", "args="], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return False
    for line in out.splitlines():
        if "cursor_cleanup" in line or "state.vscdb" in line:
            # 排除本清理器自身（命令行里会带这些字样）
            continue
        if any(m in line for m in markers):
            return True
    return False


# ---------------------------------------------------------------------------
# 引用收集
# ---------------------------------------------------------------------------


def _decode(val: object) -> str:
    if isinstance(val, bytes):
        return val.decode("utf-8", "ignore")
    return val if isinstance(val, str) else str(val or "")


def list_all_key_prefixes(cur: sqlite3.Cursor) -> list[str]:
    """库中除 blob 自身外的**全部**键前缀。

    只扫三个会话前缀会漏掉 `ofsContent:` 这类同样引用 blob 的键——漏扫即误删。
    所以这里从库里把前缀实际枚举出来，而不是硬编码一份可能过时的清单。
    """
    rows = cur.execute(
        """
        SELECT DISTINCT substr(key, 1, instr(key, ':')) AS p
        FROM cursorDiskKV
        WHERE instr(key, ':') > 0 AND key NOT LIKE ?
        """,
        (BLOB_SELF_PREFIX + "%",),
    ).fetchall()
    # `substr(key, 1, instr(key, ':'))` 已经含结尾冒号（"bubbleId:"），
    # 这里直接当 LIKE 模式用——再补一个冒号就成 "bubbleId::%"，一条都匹配不到。
    return [r[0] + "%" for r in rows if r[0]]


def collect_referenced_hashes(
    cur: sqlite3.Cursor,
    prefixes: Sequence[str] | None = None,
    *,
    progress_every: int = 500_000,
) -> set[str]:
    """顺序扫描 value，提取被引用的 64 位 hex 哈希（blob 哈希格式）。

    **必须扫全部非 blob 前缀**——漏一个前缀就可能误删它引用的 blob。
    速度约 5000 行/秒、40 MB/s；767 万行约 25 分钟，可接受。
    """
    referenced: set[str] = set()
    pats = list(prefixes) if prefixes is not None else list_all_key_prefixes(cur)
    scanned = 0
    for prefix in pats:
        for (val,) in cur.execute("SELECT value FROM cursorDiskKV WHERE key LIKE ?", (prefix,)):
            referenced.update(BLOB_HASH_RE.findall(_decode(val)))
            scanned += 1
            if progress_every and scanned % progress_every == 0:
                print(f"  [引用扫描] 已读 {scanned} 行，找到 {len(referenced)} 个引用哈希", flush=True)
    return referenced


def collect_blob_hashes(cur: sqlite3.Cursor) -> list[str]:
    """所有 blob 的 64 位 hex 哈希（只读 key 列，不加载 value）。"""
    rows = cur.execute(
        "SELECT key FROM cursorDiskKV WHERE key LIKE ?",
        (BLOB_SELF_PREFIX + "%",),
    ).fetchall()
    return [r[0][BLOB_PREFIX_LEN:] for r in rows]


# ---------------------------------------------------------------------------
# 孤儿判定
# ---------------------------------------------------------------------------


@dataclass
class BlobPlan:
    """blob 清理计划。判定只此一处——预览与实跑共用同一份结论。"""

    total: int = 0
    referenced: int = 0
    orphan: list[str] = field(default_factory=list)
    orphan_bytes: int = 0

    @property
    def referenced_count(self) -> int:
        return self.referenced

    def summary(self) -> dict[str, int]:
        return {
            "blob_total": self.total,
            "blob_referenced": self.referenced,
            "blob_orphan": len(self.orphan),
            "blob_orphan_bytes": self.orphan_bytes,
        }


def plan_blob_cleanup(
    cur: sqlite3.Cursor,
    referenced: set[str],
    *,
    measure_bytes: bool = True,
) -> BlobPlan:
    """算出哪些 blob 是孤儿。

    `measure_bytes` 会额外扫一遍 blob 的 value 算体积——这是 75 GB 的活，
    所以只在用户明确要数字时才做。
    """
    all_hashes = collect_blob_hashes(cur)
    orphans = [h for h in all_hashes if h not in referenced]
    plan = BlobPlan(
        total=len(all_hashes),
        referenced=len(all_hashes) - len(orphans),
        orphan=orphans,
    )
    if measure_bytes and orphans:
        plan.orphan_bytes = _sum_blob_bytes(cur, orphans)
    return plan


def _sum_blob_bytes(cur: sqlite3.Cursor, hashes: Sequence[str]) -> int:
    """按哈希批量累加 blob 体积（分批，避免临时表过大）。"""
    total = 0
    batch = 5000
    for i in range(0, len(hashes), batch):
        chunk = hashes[i : i + batch]
        cur.execute("DROP TABLE IF EXISTS _cc_bytes")
        cur.execute("CREATE TEMP TABLE _cc_bytes (h TEXT PRIMARY KEY)")
        cur.executemany("INSERT OR IGNORE INTO _cc_bytes(h) VALUES (?)", ((h,) for h in chunk))
        n = cur.execute(
            """
            SELECT COALESCE(SUM(LENGTH(key) + LENGTH(value)), 0)
            FROM cursorDiskKV
            WHERE key LIKE ? AND substr(key, ?) IN (SELECT h FROM _cc_bytes)
            """,
            (BLOB_SELF_PREFIX + "%", BLOB_PREFIX_LEN + 1),
        ).fetchone()[0]
        total += int(n or 0)
    cur.execute("DROP TABLE IF EXISTS _cc_bytes")
    return total


# ---------------------------------------------------------------------------
# 会话（bubble / checkpoint / composerData / headers）
# ---------------------------------------------------------------------------


def _sql_extract_cid_exprs() -> tuple[str, str, str]:
    """从 key 抽出 composerId 的 SQL 表达式。"""
    bubble = (
        f"substr(key, {_PREFIX_BUBBLE + 1}, "
        f"instr(substr(key, {_PREFIX_BUBBLE + 1}), ':') - 1)"
    )
    checkpoint = (
        f"substr(key, {_PREFIX_CHECKPOINT + 1}, "
        f"instr(substr(key, {_PREFIX_CHECKPOINT + 1}), ':') - 1)"
    )
    composer_data = f"substr(key, {_PREFIX_COMPOSER_DATA + 1})"
    return bubble, checkpoint, composer_data


def list_old_composer_ids(cur: sqlite3.Cursor, cutoff_ms: int) -> list[str]:
    """`composerHeaders` 里最后活跃早于 cutoff 的会话 id。"""
    return [
        r[0]
        for r in cur.execute(
            "SELECT composerId FROM composerHeaders WHERE recency IS NOT NULL AND recency < ?",
            (cutoff_ms,),
        )
    ]


def list_orphan_composer_ids(cur: sqlite3.Cursor, keep: set[str]) -> list[str]:
    """一次 SQL 扫出孤儿 composerId（会话正文还在，但会话索引已没了）。"""
    bubble, checkpoint, composer_data = _sql_extract_cid_exprs()
    cur.execute("DROP TABLE IF EXISTS _cc_keep")
    cur.execute("CREATE TEMP TABLE _cc_keep (cid TEXT PRIMARY KEY)")
    cur.executemany("INSERT OR IGNORE INTO _cc_keep(cid) VALUES (?)", ((c,) for c in keep))
    rows = cur.execute(
        f"""
        SELECT DISTINCT cid FROM (
          SELECT {bubble} AS cid FROM cursorDiskKV
          WHERE key LIKE 'bubbleId:%' AND instr(substr(key, {_PREFIX_BUBBLE + 1}), ':') > 0
          UNION ALL
          SELECT {checkpoint} AS cid FROM cursorDiskKV
          WHERE key LIKE 'checkpointId:%' AND instr(substr(key, {_PREFIX_CHECKPOINT + 1}), ':') > 0
          UNION ALL
          SELECT {composer_data} AS cid FROM cursorDiskKV WHERE key LIKE 'composerData:%'
        )
        WHERE cid IS NOT NULL AND cid != ''
          AND cid NOT IN (SELECT cid FROM _cc_keep)
        """
    ).fetchall()
    cur.execute("DROP TABLE IF EXISTS _cc_keep")
    return [r[0] for r in rows]


def _parse_time_to_ms(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        v = int(raw)
        return v * 1000 if v < 10_000_000_000 else v
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s.isdigit():
            return _parse_time_to_ms(int(s))
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return int(datetime.fromisoformat(s).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _latest_from_json_blob(val: object) -> int | None:
    try:
        obj = json.loads(_decode(val))
    except (TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    latest = None
    for field_name in ("createdAt", "timestamp", "lastUpdatedAt", "updatedAt", "recency"):
        ms = _parse_time_to_ms(obj.get(field_name))
        if ms is not None and (latest is None or ms > latest):
            latest = ms
    return latest


def orphan_latest_activity_ms(cur: sqlite3.Cursor, cid: str) -> int | None:
    """优先读 composerData（1 行）；否则只抽 1 条气泡（避免每孤儿 30 次 JSON）。"""
    row = cur.execute("SELECT value FROM cursorDiskKV WHERE key = ?", (f"composerData:{cid}",)).fetchone()
    if row:
        ms = _latest_from_json_blob(row[0])
        if ms is not None:
            return ms
    row = cur.execute("SELECT value FROM cursorDiskKV WHERE key LIKE ? LIMIT 1", (f"bubbleId:{cid}:%",)).fetchone()
    if row:
        return _latest_from_json_blob(row[0])
    return None


def filter_stale_orphans(
    cur: sqlite3.Cursor, orphan_ids: list[str], cutoff_ms: int
) -> tuple[list[str], int]:
    """把孤儿按最后活动时间过滤：近 N 天的留着（可能刚被创建但索引没写），老的才删。"""
    stale: list[str] = []
    skipped_recent = 0
    total = len(orphan_ids)
    for i, cid in enumerate(orphan_ids, 1):
        latest = orphan_latest_activity_ms(cur, cid)
        if latest is not None and latest >= cutoff_ms:
            skipped_recent += 1
        else:
            stale.append(cid)
        if total >= 50 and (i % 100 == 0 or i == total):
            print(f"  [孤儿年龄] {i}/{total}", flush=True)
    return stale, skipped_recent


def delete_by_composer_ids(
    con: sqlite3.Connection,
    cur: sqlite3.Cursor,
    ids: list[str],
    *,
    delete_headers: bool,
) -> dict[str, int]:
    """按会话 id 批量删除正文与元数据。"""
    stats = {"bubble": 0, "checkpoint": 0, "composerData": 0, "headers": 0}
    if not ids:
        return stats

    bubble, checkpoint, composer_data = _sql_extract_cid_exprs()
    cur.execute("DROP TABLE IF EXISTS _cc_targets")
    cur.execute("CREATE TEMP TABLE _cc_targets (cid TEXT PRIMARY KEY)")
    cur.executemany("INSERT OR IGNORE INTO _cc_targets(cid) VALUES (?)", ((c,) for c in ids))

    cur.execute(
        f"""
        DELETE FROM cursorDiskKV
        WHERE key LIKE 'bubbleId:%'
          AND instr(substr(key, {_PREFIX_BUBBLE + 1}), ':') > 0
          AND {bubble} IN (SELECT cid FROM _cc_targets)
        """
    )
    stats["bubble"] = cur.rowcount
    con.commit()

    cur.execute(
        f"""
        DELETE FROM cursorDiskKV
        WHERE key LIKE 'checkpointId:%'
          AND instr(substr(key, {_PREFIX_CHECKPOINT + 1}), ':') > 0
          AND {checkpoint} IN (SELECT cid FROM _cc_targets)
        """
    )
    stats["checkpoint"] = cur.rowcount
    con.commit()

    cur.execute(
        f"""
        DELETE FROM cursorDiskKV
        WHERE key LIKE 'composerData:%'
          AND {composer_data} IN (SELECT cid FROM _cc_targets)
        """
    )
    stats["composerData"] = cur.rowcount
    con.commit()

    if delete_headers:
        cur.execute("DELETE FROM composerHeaders WHERE composerId IN (SELECT cid FROM _cc_targets)")
        stats["headers"] = cur.rowcount
        con.commit()

    cur.execute("DROP TABLE IF EXISTS _cc_targets")
    return stats


# ---------------------------------------------------------------------------
# blob 删除
# ---------------------------------------------------------------------------


def delete_orphan_blobs(
    con: sqlite3.Connection,
    cur: sqlite3.Cursor,
    orphan_hashes: Sequence[str],
    *,
    pause: float = 3.0,
) -> dict[str, int]:
    """删除无引用的孤儿 blob。

    为「不影响 Cursor 使用」：
    - 每批之间主动 sleep `pause` 秒，给 Cursor 留出写窗口；
    - 每批用短事务（DELETE 后立即 commit），不长时间持锁；
    - 撞锁时指数退避重试，绝不抛异常中止。
    """
    total = len(orphan_hashes)
    deleted = 0
    if not total:
        return {"blob": 0}

    for i in range(0, total, BLOB_BATCH):
        chunk = list(orphan_hashes[i : i + BLOB_BATCH])
        cur.execute("DROP TABLE IF EXISTS _cc_blob")
        cur.execute("CREATE TEMP TABLE _cc_blob (h TEXT PRIMARY KEY)")
        cur.executemany("INSERT OR IGNORE INTO _cc_blob(h) VALUES (?)", ((h,) for h in chunk))

        done = False
        wait = 1.0
        for _ in range(len(_LOCK_BACKOFF)):
            try:
                cur.execute(
                    """
                    DELETE FROM cursorDiskKV
                    WHERE key LIKE ?
                      AND substr(key, ?) IN (SELECT h FROM _cc_blob)
                    """,
                    (BLOB_SELF_PREFIX + "%", BLOB_PREFIX_LEN + 1),
                )
                n = cur.rowcount
                con.commit()
                deleted += n
                done = True
                break
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    time.sleep(wait)
                    wait *= 2
                    continue
                raise
        cur.execute("DROP TABLE IF EXISTS _cc_blob")
        if not done:
            print(f"  [blob] 第 {i // BLOB_BATCH + 1} 批持续撞锁，跳过", flush=True)
        if total >= 10_000 and (i + BLOB_BATCH) % 20_000 == 0:
            print(f"  [blob] 已删 {min(i + BLOB_BATCH, total)}/{total}", flush=True)
        if i + BLOB_BATCH < total:
            time.sleep(pause)

    return {"blob": deleted}


# ---------------------------------------------------------------------------
# 备份 / 压缩 / 校验
# ---------------------------------------------------------------------------


def fast_copy(src: Path, dst: Path) -> None:
    """优先 APFS clonefile（`cp -c`，秒级且不占额外空间），失败再 shutil.copy2。"""
    try:
        subprocess.run(["cp", "-c", str(src), str(dst)], check=True, capture_output=True)
        return
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        shutil.copy2(src, dst)


def backup_database(db: Path, backup_dir: Path) -> Path:
    """冷备份主库与 WAL/SHM 边车文件。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    fast_copy(db, backup_dir / db.name)
    for side in (db.name + "-wal", db.name + "-shm"):
        p = db.with_name(side)
        if p.exists() and p.stat().st_size > 0:
            fast_copy(p, backup_dir / side)
    return backup_dir


def verify_database(path: Path) -> None:
    """校验压缩后的库：表齐全、`ItemTable` 非空。任一不满足就中止替换。"""
    if not path.is_file() or path.stat().st_size < 1024:
        raise CursorCleanupError(f"压缩库异常（过小或不存在）：{path}")
    con = sqlite3.connect(str(path))
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"ItemTable", "cursorDiskKV", "composerHeaders"} - tables
        if missing:
            raise CursorCleanupError(f"压缩库缺表 {sorted(missing)}：{path}")
        n = con.execute("SELECT COUNT(*) FROM ItemTable").fetchone()[0]
        if n < 1:
            raise CursorCleanupError("压缩库 ItemTable 为空，疑似损坏，中止替换")
    finally:
        con.close()


def checkpoint_wal(db: Path) -> None:
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA busy_timeout=60000")
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 顶层动作
# ---------------------------------------------------------------------------


def open_readonly(db: Path) -> sqlite3.Connection:
    """只读连接。`mode=ro` 保证不改主库；TEMP 表仍可写（勿开 query_only）。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=60000")
    return con


@dataclass
class CleanupReport:
    """一次清理的完整结论。CLI 与界面共用这一份，不各算一遍。"""

    mode: str = "dry-run"
    days: int = 90
    db: str = ""
    db_bytes: int = 0
    old_sessions: int = 0
    orphan_sessions: int = 0
    orphan_skipped: int = 0
    old_stats: dict[str, int] = field(default_factory=dict)
    orphan_stats: dict[str, int] = field(default_factory=dict)
    blob: dict[str, int] = field(default_factory=dict)
    backup_dir: str = ""
    vacuumed: bool = False
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "days": self.days,
            "db": self.db,
            "db_bytes": self.db_bytes,
            "old_sessions": self.old_sessions,
            "orphan_sessions": self.orphan_sessions,
            "orphan_skipped": self.orphan_skipped,
            "old_stats": self.old_stats,
            "orphan_stats": self.orphan_stats,
            "blob": self.blob,
            "backup_dir": self.backup_dir,
            "vacuumed": self.vacuumed,
            "elapsed_s": round(self.elapsed_s, 1),
            "notes": self.notes,
        }


def analyze(
    db: Path,
    *,
    days: int = 90,
    include_orphan: bool = True,
    include_blob: bool = True,
    measure_blob_bytes: bool = False,
) -> CleanupReport:
    """只统计，不改库。"""
    t0 = time.perf_counter()
    if not db.is_file():
        raise CursorCleanupError(f"找不到数据库：{db}")

    cutoff_ms = int((time.time() - days * 86400) * 1000)
    con = open_readonly(db)
    cur = con.cursor()
    report = CleanupReport(
        mode="dry-run",
        days=days,
        db=str(db),
        db_bytes=db.stat().st_size,
    )

    try:
        old_ids = list_old_composer_ids(cur, cutoff_ms)
        headers_n = cur.execute("SELECT COUNT(*) FROM composerHeaders").fetchone()[0]
        report.old_sessions = len(old_ids)
        report.notes.append(f"会话索引共 {headers_n} 条，超过 {days} 天未活跃 {len(old_ids)} 条")

        if include_orphan:
            keep = {r[0] for r in cur.execute("SELECT composerId FROM composerHeaders")}
            orphan_ids = list_orphan_composer_ids(cur, keep)
            stale, skipped = filter_stale_orphans(cur, orphan_ids, cutoff_ms)
            report.orphan_sessions = len(stale)
            report.orphan_skipped = skipped

        if include_blob:
            referenced = collect_referenced_hashes(cur)
            plan = plan_blob_cleanup(cur, referenced, measure_bytes=measure_blob_bytes)
            report.blob = plan.summary()
    finally:
        con.close()

    report.elapsed_s = time.perf_counter() - t0
    return report


def clean(
    db: Path,
    *,
    days: int = 90,
    include_orphan: bool = True,
    include_blob: bool = True,
    vacuum: bool = True,
    backup_dir: Path | None = None,
    blob_pause: float = 3.0,
) -> CleanupReport:
    """真正清理。**调用方必须已确认 Cursor 已退出**（`cursor_running()` 由 CLI 检查）。"""
    t0 = time.perf_counter()
    if not db.is_file():
        raise CursorCleanupError(f"找不到数据库：{db}")
    if days < 1:
        raise CursorCleanupError("--days 必须 >= 1")

    gs = db.parent
    vacuumed_path = gs / "state.vscdb.vacuumed"
    old_path = gs / "state.vscdb.pre-cleanup"
    backup = backup_dir or default_backup_dir()

    report = CleanupReport(
        mode="clean",
        days=days,
        db=str(db),
        db_bytes=db.stat().st_size,
        backup_dir=str(backup),
    )

    checkpoint_wal(db)
    backup_database(db, backup)
    report.notes.append(f"冷备份完成：{backup}")

    cutoff_ms = int((time.time() - days * 86400) * 1000)
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-200000")
    con.execute("PRAGMA synchronous=NORMAL")
    cur = con.cursor()

    try:
        old_ids = list_old_composer_ids(cur, cutoff_ms)
        report.old_sessions = len(old_ids)
        report.old_stats = delete_by_composer_ids(con, cur, old_ids, delete_headers=True)

        if include_orphan:
            keep = {r[0] for r in cur.execute("SELECT composerId FROM composerHeaders")}
            orphan_ids = list_orphan_composer_ids(cur, keep)
            stale, skipped = filter_stale_orphans(cur, orphan_ids, cutoff_ms)
            report.orphan_sessions = len(stale)
            report.orphan_skipped = skipped
            report.orphan_stats = delete_by_composer_ids(con, cur, stale, delete_headers=False)

        if include_blob:
            referenced = collect_referenced_hashes(cur)
            plan = plan_blob_cleanup(cur, referenced, measure_bytes=True)
            report.blob = plan.summary()
            report.blob = {**report.blob, **delete_orphan_blobs(con, cur, plan.orphan, pause=blob_pause)}
    finally:
        cur.close()
        con.commit()
        con.close()

    checkpoint_wal(db)

    if vacuum:
        if vacuumed_path.exists():
            vacuumed_path.unlink()
        con2 = sqlite3.connect(str(db))
        try:
            con2.execute("PRAGMA temp_store=MEMORY")
            con2.execute("VACUUM INTO ?", (str(vacuumed_path),))
        finally:
            con2.close()
        verify_database(vacuumed_path)
        if old_path.exists():
            old_path.unlink()
        os.replace(db, old_path)
        os.replace(vacuumed_path, db)
        for side in ("state.vscdb-wal", "state.vscdb-shm"):
            p = gs / side
            if p.exists():
                p.unlink()
        report.vacuumed = True
        report.notes.append(f"压缩后原库另存：{old_path}")
    else:
        report.notes.append("已跳过 VACUUM（文件体积暂不缩小）")

    report.elapsed_s = time.perf_counter() - t0
    return report


def iter_table_names(db: Path) -> Iterator[str]:
    """列出库里的表名（只读）。测试与诊断用。"""
    con = open_readonly(db)
    try:
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            yield name
    finally:
        con.close()
