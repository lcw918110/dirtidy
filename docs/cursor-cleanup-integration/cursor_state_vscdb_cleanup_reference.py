#!/usr/bin/env python3
"""清理本机 Cursor `state.vscdb` 中过期 Agent/Composer 气泡与孤儿数据。

默认（无参数）：删除最后活跃超过 **90 天（约三个月）** 的会话正文，清理过期孤儿，
**并连带清理无引用的 agentKv:blob**（旧会话删除后不再被任何存活数据引用的 blob）；
然后 VACUUM；结束时输出清理汇总。不删设置（ItemTable）。

必须在 Cursor 完全退出后、用系统「终端.app」运行：

  ./.venv/bin/python tools/scripts/cursor_state_vscdb_cleanup.py
  ./.venv/bin/python tools/scripts/cursor_state_vscdb_cleanup.py --days 30
  ./.venv/bin/python tools/scripts/cursor_state_vscdb_cleanup.py --dry-run
  ./.venv/bin/python tools/scripts/cursor_state_vscdb_cleanup.py --days 60 --no-vacuum
  ./.venv/bin/python tools/scripts/cursor_state_vscdb_cleanup.py --no-blob

保留天数：`--days N`（默认 90≈三个月）。
`--no-blob`：跳过 agentKv:blob 孤儿清理（默认会清理）。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from _bootstrap import ROOT, wpath

DEFAULT_DAYS = 90
GS = Path.home() / "Library/Application Support/Cursor/User/globalStorage"
DB = GS / "state.vscdb"

# key 前缀长度（含冒号）
_PREFIX_BUBBLE = len("bubbleId:")  # 9
_PREFIX_CHECKPOINT = len("checkpointId:")  # 13
_PREFIX_COMPOSER_DATA = len("composerData:")  # 13


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    raise SystemExit(code)


def human(n: int) -> str:
    return f"{n / 1024 / 1024 / 1024:.2f} GB"


def timed(label: str):
    """简单阶段计时上下文。"""

    class _T:
        def __enter__(self):
            self.t0 = time.perf_counter()
            print(f"→ {label} ...")
            return self

        def __exit__(self, *args):
            print(f"← {label} 耗时 {time.perf_counter() - self.t0:.1f}s")

    return _T()


def cursor_running() -> bool:
    """检测 Cursor 是否在跑。宁可误报（拒绝清理），不可漏报（边跑边改库）。

    注意：macOS `pgrep -f` 按正则匹配，路径里的 `.`/`()` 会误伤，故改用 `ps` 子串判断。
    """
    markers = (
        "/Applications/Cursor.app/Contents/MacOS/Cursor",
        "/Applications/Cursor.app/Contents/Frameworks/Cursor Helper",
    )
    try:
        out = subprocess.check_output(["ps", "-Ax", "-o", "args="], text=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False
    for line in out.splitlines():
        # 排除本清理脚本自身
        if "cursor_state_vscdb_cleanup.py" in line:
            continue
        if any(m in line for m in markers):
            return True
    return False


def checkpoint_wal(db_path: Path) -> None:
    con = sqlite3.connect(str(db_path))
    con.execute("PRAGMA busy_timeout=60000")
    try:
        mode, ckpt, log = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        print(f"WAL checkpoint: mode={mode} checkpointed={ckpt} log={log}")
    finally:
        con.close()


def list_old_composer_ids(cur: sqlite3.Cursor, cutoff_ms: int) -> list[str]:
    return [
        r[0]
        for r in cur.execute(
            "SELECT composerId FROM composerHeaders "
            "WHERE recency IS NOT NULL AND recency < ?",
            (cutoff_ms,),
        )
    ]


def ensure_target_table(cur: sqlite3.Cursor) -> None:
    cur.execute("DROP TABLE IF EXISTS _cleanup_targets")
    cur.execute("CREATE TEMP TABLE _cleanup_targets (cid TEXT PRIMARY KEY)")


def fill_targets(cur: sqlite3.Cursor, ids: list[str]) -> None:
    ensure_target_table(cur)
    cur.executemany(
        "INSERT OR IGNORE INTO _cleanup_targets(cid) VALUES (?)",
        ((cid,) for cid in ids),
    )


def sql_extract_cid_exprs() -> tuple[str, str, str]:
    """从 key 抽出 composerId 的 SQL 表达式（bubble / checkpoint / composerData）。"""
    # bubbleId:<cid>:<bid>
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


def list_orphan_composer_ids_sql(
    cur: sqlite3.Cursor, keep: set[str], exclude: set[str] | None = None
) -> list[str]:
    """一次 SQL 扫出孤儿 composerId（比 Python 逐行快一个数量级以上）。"""
    bubble, checkpoint, composer_data = sql_extract_cid_exprs()
    cur.execute("DROP TABLE IF EXISTS _keep_cids")
    cur.execute("CREATE TEMP TABLE _keep_cids (cid TEXT PRIMARY KEY)")
    cur.executemany(
        "INSERT OR IGNORE INTO _keep_cids(cid) VALUES (?)",
        ((cid,) for cid in keep),
    )
    if exclude:
        cur.executemany(
            "INSERT OR IGNORE INTO _keep_cids(cid) VALUES (?)",
            ((cid,) for cid in exclude),
        )

    rows = cur.execute(
        f"""
        SELECT DISTINCT cid FROM (
          SELECT {bubble} AS cid
          FROM cursorDiskKV
          WHERE key LIKE 'bubbleId:%' AND instr(substr(key, {_PREFIX_BUBBLE + 1}), ':') > 0
          UNION ALL
          SELECT {checkpoint} AS cid
          FROM cursorDiskKV
          WHERE key LIKE 'checkpointId:%' AND instr(substr(key, {_PREFIX_CHECKPOINT + 1}), ':') > 0
          UNION ALL
          SELECT {composer_data} AS cid
          FROM cursorDiskKV
          WHERE key LIKE 'composerData:%'
        )
        WHERE cid IS NOT NULL AND cid != ''
          AND cid NOT IN (SELECT cid FROM _keep_cids)
        """
    ).fetchall()
    return [r[0] for r in rows]


def _parse_time_to_ms(raw: object) -> int | None:
    if raw is None:
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
        if isinstance(val, bytes):
            val = val.decode("utf-8", "replace")
        obj = json.loads(val)
    except (TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    latest = None
    for field in (
        "createdAt",
        "timestamp",
        "lastUpdatedAt",
        "updatedAt",
        "recency",
    ):
        ms = _parse_time_to_ms(obj.get(field))
        if ms is not None and (latest is None or ms > latest):
            latest = ms
    return latest


def orphan_latest_activity_ms(cur: sqlite3.Cursor, cid: str) -> int | None:
    """优先读 composerData（1 行）；否则只抽 1 条气泡（避免每孤儿 30 次 JSON）。"""
    row = cur.execute(
        "SELECT value FROM cursorDiskKV WHERE key = ?",
        (f"composerData:{cid}",),
    ).fetchone()
    if row:
        ms = _latest_from_json_blob(row[0])
        if ms is not None:
            return ms
    row = cur.execute(
        "SELECT value FROM cursorDiskKV WHERE key LIKE ? LIMIT 1",
        (f"bubbleId:{cid}:%",),
    ).fetchone()
    if row:
        return _latest_from_json_blob(row[0])
    return None


def filter_stale_orphans(
    cur: sqlite3.Cursor, orphan_ids: list[str], cutoff_ms: int
) -> tuple[list[str], int]:
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
            print(f"  [orphan-age] {i}/{total}")
    return stale, skipped_recent


_BLOB_HASH_RE = re.compile(r"[0-9a-f]{64}")
_BLOB_PREFIX = "agentKv:blob:"  # 13 字符
_BLOB_PREFIX_LEN = len(_BLOB_PREFIX)


def collect_blob_keys(cur: sqlite3.Cursor) -> list[str]:
    """收集所有 agentKv:blob:<hash> 的 key（只读 key 列，不加载 value）。"""
    rows = cur.execute(
        "SELECT key FROM cursorDiskKV WHERE key LIKE 'agentKv:blob:%'"
    ).fetchall()
    return [r[0] for r in rows]


def collect_referenced_hashes(cur: sqlite3.Cursor) -> set[str]:
    """顺序扫描会话正文 value，提取被引用的 64 位 hex 哈希（blob 哈希格式）。

    只扫 bubble/checkpoint/composerData（不扫 agentKv:blob 自身，避免重复读 75GB）。
    速度约 5000 行/秒、40MB/s，767 万行约 25 分钟；允许小时级后台运行。
    """
    referenced: set[str] = set()
    prefixes = ("bubbleId:%", "checkpointId:%", "composerData:%")
    for prefix in prefixes:
        cur.execute(
            "SELECT value FROM cursorDiskKV WHERE key LIKE ?",
            (prefix,),
        )
        for (val,) in cur:
            if isinstance(val, bytes):
                val = val.decode("utf-8", "ignore")
            referenced.update(_BLOB_HASH_RE.findall(val))
    return referenced


def delete_orphan_blobs(
    con: sqlite3.Connection,
    cur: sqlite3.Cursor,
    blob_keys: list[str],
    referenced: set[str],
    *,
    pause: float = 3.0,
) -> dict[str, int]:
    """删除无引用的孤儿 blob。blob 哈希即 key 末尾 64 位 hex。

    为「不影响 Cursor 使用」：
    - 每批之间主动 sleep `pause` 秒，给 Cursor 留出写窗口；
    - 每批用短事务（DELETE 后立即 commit），不长时间持锁；
    - 撞锁时指数退避重试（最多约 1 分钟），绝不抛异常中止。
    """
    orphan_hashes = [
        h
        for k in blob_keys
        if (h := k[_BLOB_PREFIX_LEN:]) not in referenced
    ]
    if not orphan_hashes:
        return {"blob": 0, "blob_bytes": 0}

    total = len(orphan_hashes)
    deleted = 0
    batch = 500  # 小批次，事务更短，锁窗口更小
    with timed(f"删除孤儿 blob（共 {total} 个，分批 {batch}/批，批间 {pause}s）"):
        for i in range(0, total, batch):
            chunk = orphan_hashes[i:i + batch]
            cur.execute("DROP TABLE IF EXISTS _blob_targets")
            cur.execute("CREATE TEMP TABLE _blob_targets (h TEXT PRIMARY KEY)")
            cur.executemany(
                "INSERT OR IGNORE INTO _blob_targets(h) VALUES (?)",
                ((h,) for h in chunk),
            )
            # 删除本批：短事务 + 锁退避重试（指数退避，最多约 1 分钟）
            done = False
            wait = 1.0
            for _ in range(6):
                try:
                    n = cur.execute(
                        """
                        DELETE FROM cursorDiskKV
                        WHERE key LIKE 'agentKv:blob:%'
                          AND substr(key, 14) IN (SELECT h FROM _blob_targets)
                        """
                    ).rowcount
                    con.commit()
                    deleted += n
                    done = True
                    break
                except sqlite3.OperationalError as e:
                    if "locked" in str(e).lower():
                        time.sleep(wait)
                        wait *= 2  # 1s, 2s, 4s, 8s, 16s, 32s
                        continue
                    raise
            cur.execute("DROP TABLE IF EXISTS _blob_targets")
            if not done:
                # 本批多次撞锁仍失败：跳过，继续下一批（不中断整体）
                print(f"  [blob] 第 {i // batch + 1} 批持续撞锁，跳过")
            if total >= 10000 and (i + batch) % 20000 == 0:
                print(f"  [blob] 已删 {min(i + batch, total)}/{total}")
            # 批间主动让路，给 Cursor 写窗口
            if i + batch < total:
                time.sleep(pause)

    return {"blob": deleted, "blob_bytes": 0}


def estimate_bytes_for_targets(cur: sqlite3.Cursor) -> tuple[int, int]:
    """对 _cleanup_targets 一次聚合估算（需先 fill_targets）。"""
    bubble, checkpoint, composer_data = sql_extract_cid_exprs()
    n, b = cur.execute(
        f"""
        SELECT COUNT(*), COALESCE(SUM(LENGTH(key)+LENGTH(value)), 0)
        FROM cursorDiskKV
        WHERE
          (key LIKE 'bubbleId:%'
            AND instr(substr(key, {_PREFIX_BUBBLE + 1}), ':') > 0
            AND {bubble} IN (SELECT cid FROM _cleanup_targets))
          OR
          (key LIKE 'checkpointId:%'
            AND instr(substr(key, {_PREFIX_CHECKPOINT + 1}), ':') > 0
            AND {checkpoint} IN (SELECT cid FROM _cleanup_targets))
          OR
          (key LIKE 'composerData:%'
            AND {composer_data} IN (SELECT cid FROM _cleanup_targets))
        """
    ).fetchone()
    return int(b or 0), int(n or 0)


def delete_by_composer_ids(
    con: sqlite3.Connection,
    cur: sqlite3.Cursor,
    ids: list[str],
    label: str,
    *,
    delete_headers: bool,
) -> dict[str, int]:
    """用临时表 + 单次/少数次 DELETE，避免每会话 3 次 LIKE。"""
    stats = {"bubble": 0, "checkpoint": 0, "composerData": 0, "headers": 0}
    if not ids:
        return stats

    fill_targets(cur, ids)
    bubble, checkpoint, composer_data = sql_extract_cid_exprs()

    with timed(f"删除[{label}] bubbleId"):
        stats["bubble"] = cur.execute(
            f"""
            DELETE FROM cursorDiskKV
            WHERE key LIKE 'bubbleId:%'
              AND instr(substr(key, {_PREFIX_BUBBLE + 1}), ':') > 0
              AND {bubble} IN (SELECT cid FROM _cleanup_targets)
            """
        ).rowcount
        con.commit()

    with timed(f"删除[{label}] checkpointId"):
        stats["checkpoint"] = cur.execute(
            f"""
            DELETE FROM cursorDiskKV
            WHERE key LIKE 'checkpointId:%'
              AND instr(substr(key, {_PREFIX_CHECKPOINT + 1}), ':') > 0
              AND {checkpoint} IN (SELECT cid FROM _cleanup_targets)
            """
        ).rowcount
        con.commit()

    with timed(f"删除[{label}] composerData"):
        stats["composerData"] = cur.execute(
            f"""
            DELETE FROM cursorDiskKV
            WHERE key LIKE 'composerData:%'
              AND {composer_data} IN (SELECT cid FROM _cleanup_targets)
            """
        ).rowcount
        con.commit()

    if delete_headers:
        with timed(f"删除[{label}] composerHeaders"):
            stats["headers"] = cur.execute(
                """
                DELETE FROM composerHeaders
                WHERE composerId IN (SELECT cid FROM _cleanup_targets)
                """
            ).rowcount
            con.commit()

    print(f"  [{label}] sessions={len(ids)} stats={stats}")
    return stats


def verify_db(path: Path) -> None:
    if not path.is_file() or path.stat().st_size < 1024:
        die(f"压缩库异常（过小或不存在）: {path}")
    con = sqlite3.connect(str(path))
    try:
        tables = {
            r[0]
            for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        required = {"ItemTable", "cursorDiskKV", "composerHeaders"}
        missing = required - tables
        if missing:
            die(f"压缩库缺表 {missing}: {path}")
        n = con.execute("SELECT COUNT(*) FROM ItemTable").fetchone()[0]
        if n < 1:
            die("压缩库 ItemTable 为空，疑似损坏，中止替换")
        print(
            f"压缩库校验通过: tables={sorted(tables)} "
            f"ItemTable={n} size={human(path.stat().st_size)}"
        )
    finally:
        con.close()


def default_backup_dir() -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return wpath("_整理归档", f"CursorStateBackup-{stamp}")


def fast_copy(src: Path, dst: Path) -> None:
    """优先 APFS clonefile（cp -c），失败再 shutil.copy2。"""
    try:
        subprocess.run(
            ["cp", "-c", str(src), str(dst)],
            check=True,
            capture_output=True,
        )
        return
    except (subprocess.CalledProcessError, FileNotFoundError):
        shutil.copy2(src, dst)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="清理 Cursor state.vscdb：默认直接清理近三个月前数据并输出汇总"
    )
    p.add_argument(
        "--days",
        type=int,
        default=DEFAULT_DAYS,
        help=f"删除最后活跃早于该天数的会话（默认 {DEFAULT_DAYS}=约三个月）",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="只统计、不改库（默认无参数会真正清理）",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help=argparse.SUPPRESS,  # 兼容旧用法；默认即清理
    )
    p.add_argument(
        "--backup-dir",
        type=Path,
        default=None,
        help="冷备份目录（默认工作区 _整理归档/CursorStateBackup-<时间戳>）",
    )
    p.add_argument(
        "--no-orphan",
        action="store_true",
        help="不清理孤儿 bubble/checkpoint/composerData",
    )
    p.add_argument(
        "--no-vacuum",
        action="store_true",
        help="跳过 VACUUM（删除快，但文件体积暂不缩小）",
    )
    p.add_argument(
        "--no-blob",
        action="store_true",
        help="跳过 agentKv:blob 孤儿清理（默认会清理无引用 blob）",
    )
    p.add_argument(
        "--blob-only",
        action="store_true",
        help="只清理无引用的 agentKv:blob（在线删，不 VACUUM、不替换文件，Cursor 可继续工作）",
    )
    p.add_argument(
        "--blob-pause",
        type=float,
        default=3.0,
        help="blob 删除每批之间的间隔秒数（默认 3 秒，越大越不影响 Cursor，但越慢）",
    )
    p.add_argument(
        "--estimate",
        action="store_true",
        help="干跑时额外估算将删字节与 agentKv 体积（较慢）",
    )
    p.add_argument(
        "--db",
        type=Path,
        default=DB,
        help="state.vscdb 路径（默认本机 Cursor globalStorage）",
    )
    return p.parse_args(argv)


def print_cleanup_summary(
    *,
    days: int,
    before_bytes: int,
    after_bytes: int,
    old_sessions: int,
    orphan_sessions: int,
    orphan_skipped: int,
    old_stats: dict[str, int],
    orphan_stats: dict[str, int],
    blob_stats: dict[str, int],
    backup_dir: Path,
    vacuumed: bool,
    elapsed_s: float,
    db: Path,
) -> None:
    freed = max(0, before_bytes - after_bytes)
    rows = {
        k: old_stats.get(k, 0) + orphan_stats.get(k, 0)
        for k in ("bubble", "checkpoint", "composerData", "headers")
    }
    print()
    print("=" * 48)
    print("清理汇总")
    print("=" * 48)
    print(f"保留策略        近 {days} 天（约三个月）")
    print(f"原始体积        {human(before_bytes)}")
    print(f"现状体积        {human(after_bytes)}")
    print(f"清理量(文件)    {human(freed)}" + ("（未 VACUUM，文件可能未缩小）" if not vacuumed else ""))
    print(f"旧会话数        {old_sessions}")
    print(f"可删孤儿会话    {orphan_sessions}（近 {days} 天跳过 {orphan_skipped}）")
    print(
        f"删除行数        bubble={rows['bubble']}  checkpoint={rows['checkpoint']}  "
        f"composerData={rows['composerData']}  headers={rows['headers']}"
    )
    print(f"备份目录        {backup_dir}")
    print(f"数据库路径      {db}")
    print(f"总耗时          {elapsed_s:.1f}s")
    if blob_stats.get("blob"):
        print(
            f"孤儿 blob        {blob_stats['blob']} 个 / "
            f"约 {human(blob_stats['blob_bytes'])}"
        )
    print("=" * 48)


def dry_run(args: argparse.Namespace) -> None:
    db: Path = args.db
    if not db.is_file():
        die(f"找不到数据库: {db}")
    print("模式: 干跑（--dry-run，不改库）")
    print(f"工作区根: {ROOT}")
    print(f"数据库: {db} ({human(db.stat().st_size)})")
    print(
        f"保留近 {args.days} 天；将统计 >{args.days} 天会话"
        f"{'' if args.no_orphan else ' + 孤儿'}"
    )

    # mode=ro 保证不改主库；TEMP 表仍可写（勿开 query_only，否则无法建临时表）
    uri = f"file:{db}?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    cur = con.cursor()
    cutoff_ms = int((time.time() - args.days * 86400) * 1000)

    with timed("统计旧会话"):
        old_ids = list_old_composer_ids(cur, cutoff_ms)
        headers_n = cur.execute("SELECT COUNT(*) FROM composerHeaders").fetchone()[0]
    print(f"composerHeaders 总数: {headers_n}")
    print(
        f"> {args.days} 天未活跃会话: {len(old_ids)}（将保留约 {headers_n - len(old_ids)}）"
    )

    if args.estimate and old_ids:
        with timed("估算旧会话占用"):
            fill_targets(cur, old_ids)
            b, n = estimate_bytes_for_targets(cur)
        print(f"  旧会话关联 KV 约 {human(b)} / {n} 行")

    if not args.no_orphan:
        keep_now = {r[0] for r in cur.execute("SELECT composerId FROM composerHeaders")}
        keep_after = keep_now - set(old_ids)
        with timed("SQL 扫描孤儿候选"):
            orphan_ids = list_orphan_composer_ids_sql(
                cur, keep_after, exclude=set(old_ids)
            )
        with timed("孤儿年龄过滤"):
            stale_orphans, skipped_recent = filter_stale_orphans(
                cur, orphan_ids, cutoff_ms
            )
        print(
            f"按「删旧后」预计可删孤儿: {len(stale_orphans)} "
            f"（候选 {len(orphan_ids)}，近 {args.days} 天跳过 {skipped_recent}）"
        )
        if args.estimate and stale_orphans:
            with timed("估算可删孤儿占用"):
                fill_targets(cur, stale_orphans)
                b, n = estimate_bytes_for_targets(cur)
            print(f"  孤儿关联 KV 约 {human(b)} / {n} 行")

    if args.estimate:
        with timed("统计 agentKv 体积"):
            agent_n, agent_b = cur.execute(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(key)+LENGTH(value)),0) "
                "FROM cursorDiskKV WHERE key LIKE 'agentKv:%'"
            ).fetchone()
        print(f"本轮不删 agentKv: {agent_n} 行 / 约 {human(int(agent_b or 0))}")
    else:
        print("本轮不删 agentKv（加 --estimate 可统计其行数/体积）")

    # 干跑：只读统计孤儿 blob（不删）
    if not args.no_blob:
        with timed("统计 blob key"):
            blob_keys = collect_blob_keys(cur)
        print(f"agentKv:blob 总数: {len(blob_keys)}")
        if blob_keys:
            with timed("统计被引用哈希"):
                referenced = collect_referenced_hashes(cur)
            orphan_hashes = [
                k[len("agentKv:blob:"):]
                for k in blob_keys
                if k[len("agentKv:blob:"):] not in referenced
            ]
            print(
                f"预计可删孤儿 blob: {len(orphan_hashes)} 个 "
                f"（被引用 {len(referenced)}，总 {len(blob_keys)}）"
            )
            if args.estimate and orphan_hashes:
                with timed("估算孤儿 blob 占用"):
                    cur.execute("DROP TABLE IF EXISTS _blob_targets")
                    cur.execute(
                        "CREATE TEMP TABLE _blob_targets (h TEXT PRIMARY KEY)"
                    )
                    cur.executemany(
                        "INSERT OR IGNORE INTO _blob_targets(h) VALUES (?)",
                        ((h,) for h in orphan_hashes),
                    )
                    b = cur.execute(
                        """
                        SELECT COALESCE(SUM(LENGTH(key)+LENGTH(value)), 0)
                        FROM cursorDiskKV
                        WHERE key LIKE 'agentKv:blob:%'
                          AND substr(key, 14) IN (SELECT h FROM _blob_targets)
                        """
                    ).fetchone()[0]
                    print(f"  孤儿 blob 约 {human(int(b or 0))}")
        else:
            print("无 agentKv:blob。")

    con.close()
    print("干跑结束。去掉 --dry-run 且确保 Cursor 已完全退出后，即可真正清理。")
    print("提示: 只删不压体积可加 --no-vacuum。")


def blob_only(args: argparse.Namespace) -> None:
    """在线清理无引用 blob：Cursor 可继续工作，不 VACUUM、不替换文件。

    步骤：只读连接顺序扫 value 收集被引用哈希（约 25 分钟），
    再用独立写连接分批 DELETE 孤儿 blob（每批 commit，不长时间锁库）。
    """
    db: Path = args.db
    if not db.is_file():
        die(f"找不到数据库: {db}")

    t0 = time.perf_counter()
    before = db.stat().st_size
    print("模式: 只清理无引用 agentKv:blob（在线，Cursor 可继续工作）")
    print(f"当前体积: {human(before)}")

    # 只读连接：扫 value 收集被引用哈希
    uri = f"file:{db}?mode=ro"
    ro = sqlite3.connect(uri, uri=True)
    ro.execute("PRAGMA busy_timeout=60000")
    rcur = ro.cursor()

    with timed("收集 blob key"):
        blob_keys = collect_blob_keys(rcur)
    print(f"agentKv:blob 总数: {len(blob_keys)}")

    if not blob_keys:
        print("无 blob，无需清理。")
        ro.close()
        return

    with timed("扫描引用（顺序读 value，约 25 分钟）"):
        referenced = collect_referenced_hashes(rcur)
    ro.close()
    print(f"被引用哈希数: {len(referenced)}")

    orphan_hashes = [
        k[_BLOB_PREFIX_LEN:] for k in blob_keys if k[_BLOB_PREFIX_LEN:] not in referenced
    ]
    print(f"孤儿 blob（无引用）: {len(orphan_hashes)} 个")

    if not orphan_hashes:
        print("没有孤儿 blob，无需删除。")
        return

    # 写连接：分批删除孤儿 blob（短事务 + 批间让路，尽量不影响 Cursor）
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")  # 短超时，撞锁交给 Python 层退避
    con.execute("PRAGMA synchronous=NORMAL")
    cur = con.cursor()

    stats = delete_orphan_blobs(
        con, cur, blob_keys, referenced, pause=args.blob_pause
    )
    con.close()

    after = db.stat().st_size
    print()
    print("=" * 48)
    print("blob 清理汇总")
    print("=" * 48)
    print(f"删除 blob        {stats['blob']} 个")
    print(f"文件体积变化    {human(before)} → {human(after)}")
    print("说明: 在线 DELETE 不压缩主文件；体积真正下降需退出 Cursor 后跑一次 VACUUM。")
    print(f"总耗时          {time.perf_counter() - t0:.1f}s")
    print("=" * 48)


def apply_cleanup(args: argparse.Namespace) -> None:
    db: Path = args.db
    if cursor_running():
        die(
            "检测到 Cursor 仍在运行。请先完全退出 Cursor（Dock 右键 → 退出），"
            "再在系统「终端.app」执行本脚本（无参数即清理近三个月）。"
        )
    if not db.is_file():
        die(f"找不到数据库: {db}")
    if args.days < 1:
        die("--days 必须 >= 1")

    t0 = time.perf_counter()
    gs = db.parent
    vacuumed_path = gs / "state.vscdb.vacuumed"
    old = gs / "state.vscdb.pre-cleanup"
    backup_dir = args.backup_dir or default_backup_dir()

    before = db.stat().st_size
    print("模式: 清理（默认保留近三个月）")
    print(f"原始体积: {human(before)}")
    print(f"保留近 {args.days} 天")

    with timed("合并 WAL"):
        checkpoint_wal(db)

    print(f"备份目录: {backup_dir}")
    backup_dir.mkdir(parents=True, exist_ok=False)
    with timed("冷备份（优先 APFS clone）"):
        fast_copy(db, backup_dir / "state.vscdb")
        for side in ("state.vscdb-wal", "state.vscdb-shm"):
            p = gs / side
            if p.exists() and p.stat().st_size > 0:
                fast_copy(p, backup_dir / side)
    print("备份完成。")

    cutoff_ms = int((time.time() - args.days * 86400) * 1000)
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-200000")  # ~200MB
    con.execute("PRAGMA synchronous=NORMAL")
    cur = con.cursor()

    old_ids = list_old_composer_ids(cur, cutoff_ms)
    print(f"> {args.days} 天未活跃会话: {len(old_ids)}")
    old_stats = delete_by_composer_ids(
        con, cur, old_ids, "old", delete_headers=True
    )
    print("旧会话删除:", old_stats)

    orphan_stats = {"bubble": 0, "checkpoint": 0, "composerData": 0, "headers": 0}
    stale_orphans: list[str] = []
    skipped_recent = 0
    if not args.no_orphan:
        with timed("SQL 扫描孤儿"):
            keep = {r[0] for r in cur.execute("SELECT composerId FROM composerHeaders")}
            orphan_ids = list_orphan_composer_ids_sql(cur, keep)
        with timed("孤儿年龄过滤"):
            stale_orphans, skipped_recent = filter_stale_orphans(
                cur, orphan_ids, cutoff_ms
            )
        print(
            f"可删孤儿: {len(stale_orphans)} "
            f"（候选 {len(orphan_ids)}，近 {args.days} 天跳过 {skipped_recent}）"
        )
        orphan_stats = delete_by_composer_ids(
            con, cur, stale_orphans, "orphan", delete_headers=False
        )
        print("孤儿删除:", orphan_stats)

    blob_stats = {"blob": 0, "blob_bytes": 0}
    if not args.no_blob:
        with timed("收集 blob key"):
            blob_keys = collect_blob_keys(cur)
        print(f"agentKv:blob 总数: {len(blob_keys)}")
        if blob_keys:
            with timed("收集被引用哈希"):
                referenced = collect_referenced_hashes(cur)
            print(f"被引用哈希数: {len(referenced)}")
            blob_stats = delete_orphan_blobs(con, cur, blob_keys, referenced)
            print(
                f"孤儿 blob 删除: {blob_stats['blob']} 个 / "
                f"约 {human(blob_stats['blob_bytes'])}"
            )
        else:
            print("无 agentKv:blob，跳过。")

    # 释放游标上的所有未决 prepared statement 并提交事务，避免触发 database table is locked
    cur.close()
    con.commit()
    con.close()

    with timed("WAL checkpoint"):
        checkpoint_wal(db)

    did_vacuum = False
    if args.no_vacuum:
        after = db.stat().st_size
        print("已跳过 VACUUM（--no-vacuum）。")
    else:
        if vacuumed_path.exists():
            vacuumed_path.unlink()
        with timed("VACUUM INTO"):
            con2 = sqlite3.connect(str(db))
            try:
                con2.execute("PRAGMA temp_store=MEMORY")
                con2.execute("VACUUM INTO ?", (str(vacuumed_path),))
            finally:
                con2.close()

        verify_db(vacuumed_path)
        after = vacuumed_path.stat().st_size

        if old.exists():
            old.unlink()
        os.replace(db, old)
        os.replace(vacuumed_path, db)
        for side in ("state.vscdb-wal", "state.vscdb-shm"):
            p = gs / side
            if p.exists():
                p.unlink()
        did_vacuum = True

    print_cleanup_summary(
        days=args.days,
        before_bytes=before,
        after_bytes=after,
        old_sessions=len(old_ids),
        orphan_sessions=len(stale_orphans),
        orphan_skipped=skipped_recent,
        old_stats=old_stats,
        orphan_stats=orphan_stats,
        blob_stats=blob_stats,
        backup_dir=backup_dir,
        vacuumed=did_vacuum,
        elapsed_s=time.perf_counter() - t0,
        db=db,
    )
    if did_vacuum:
        print(f"替换前原库另存: {old}（回滚次选；优先用冷备份）")
    print("请重新打开 Cursor 验证。")


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    if args.blob_only:
        blob_only(args)
    elif args.dry_run:
        dry_run(args)
    else:
        apply_cleanup(args)


if __name__ == "__main__":
    main()
