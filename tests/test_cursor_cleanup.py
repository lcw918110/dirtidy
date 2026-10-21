"""Cursor 状态库清理的守卫。

这些用例守的是三件「错了就毁数据」的事：
1. **只删无引用的 blob**——被会话正文引用的 blob 必须原样留下。
2. **绝不删 `ItemTable`**（用户设置：插件 / 主题 / 快捷键）。
3. **引用来源要扫全**——只扫 bubble/checkpoint/composerData 三个前缀会漏掉
   `ofsContent:` 这类同样引用 blob 的键，漏扫即误删活数据（真机实测已复现）。

外加一条 CLI 层的守卫：**默认不执行**，必须显式 `--apply` 才改库。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dirtidy.cursor_cleanup import (
    BLOB_PREFIX,
    CursorCleanupError,
    analyze,
    clean,
    collect_referenced_hashes,
    delete_orphan_blobs,
    human_bytes,
    list_all_key_prefixes,
    plan_blob_cleanup,
    verify_database,
)

# 三个 64 位 hex 哈希：被引用的 / 两个孤儿。
HASH_REFERENCED = "a" * 64
HASH_ORPHAN_1 = "b" * 64
HASH_ORPHAN_2 = "c" * 64
# 只被「非会话前缀」引用的 blob —— 参考实现只扫三个前缀会漏掉它。
HASH_OFSCONTENT = "d" * 64


def _make_db(path: Path) -> None:
    """构造一个迷你 state.vscdb。

    布局刻意贴近真库：
    - `cursorDiskKV`：blob 3 个 + 会话正文 + 一个 `ofsContent:` 键；
    - `composerHeaders`：会话索引；
    - `ItemTable`：用户设置。
    """
    con = sqlite3.connect(str(path))
    cur = con.cursor()
    cur.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    cur.execute("CREATE TABLE composerHeaders (composerId TEXT, recency INTEGER)")
    cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")

    # 三个 blob：正文引用 1 个，另外 2 个是孤儿。
    cur.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (f"{BLOB_PREFIX}{HASH_REFERENCED}", b"referenced-blob-body"),
    )
    cur.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (f"{BLOB_PREFIX}{HASH_ORPHAN_1}", b"orphan-blob-body-1"),
    )
    cur.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (f"{BLOB_PREFIX}{HASH_ORPHAN_2}", b"orphan-blob-body-2"),
    )
    # 只被 ofsContent 引用 —— 三个前缀扫不到它。
    cur.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (f"{BLOB_PREFIX}{HASH_OFSCONTENT}", b"ofs-referenced-blob-body"),
    )

    # 会话正文引用 HASH_REFERENCED
    cur.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "bubbleId:cid1:bid1",
            json.dumps({"text": f"see blob {HASH_REFERENCED}"}).encode(),
        ),
    )
    # ofsContent 引用 HASH_OFSCONTENT
    cur.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "ofsContent:xyz",
            json.dumps({"ref": HASH_OFSCONTENT}).encode(),
        ),
    )
    # 一条孤儿正文：会话索引里没有它
    cur.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:orphan-cid:bid1", json.dumps({"text": "orphan"}).encode()),
    )

    cur.execute("INSERT INTO composerHeaders VALUES (?, ?)", ("cid1", 2_000_000_000_000))
    cur.execute("INSERT INTO ItemTable VALUES (?, ?)", ("theme", b"dark"))
    con.commit()
    con.close()

def _blob_keys(db: Path) -> set[str]:
    con = sqlite3.connect(str(db))
    try:
        return {r[0] for r in con.execute("SELECT key FROM cursorDiskKV WHERE key LIKE ?", (f"{BLOB_PREFIX}%",))}
    finally:
        con.close()


def _item_table_count(db: Path) -> int:
    con = sqlite3.connect(str(db))
    try:
        return con.execute("SELECT COUNT(*) FROM ItemTable").fetchone()[0]
    finally:
        con.close()


@pytest.fixture
def mini_db(tmp_path: Path) -> Path:
    db = tmp_path / "state.vscdb"
    _make_db(db)
    return db


# ---------------------------------------------------------------------------
# 引用扫描
# ---------------------------------------------------------------------------


def test_referenced_hashes_include_non_session_prefixes(mini_db: Path) -> None:
    """引用扫描必须覆盖**全部**非 blob 前缀，不能只扫会话那三个。

    回归背景：参考实现只扫 `bubbleId` / `checkpointId` / `composerData`，
    而真机库里 `ofsContent:`、`codeBlockPartialInlineDiffFates:` 等前缀
    同样引用 blob。漏扫它们 = 误删仍被引用的活数据。
    """
    con = sqlite3.connect(f"file:{mini_db}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        prefixes = list_all_key_prefixes(cur)
        assert "bubbleId:%" in prefixes
        assert "ofsContent:%" in prefixes
        # blob 自身不在扫描范围内（读它只为找引用 = 白读 75 GB）
        assert f"{BLOB_PREFIX}%" not in prefixes

        refs = collect_referenced_hashes(cur)
        assert HASH_REFERENCED in refs
        assert HASH_OFSCONTENT in refs
    finally:
        con.close()


def test_listed_prefixes_actually_match_rows(mini_db: Path) -> None:
    """枚举出的前缀必须**真能匹配到行**——防双冒号这类拼接错误。

    回归背景：集成时踩过。`substr(key, 1, instr(key, ':'))` 取出的前缀
    已经含结尾冒号（`bubbleId:`），再补一个冒号就成了 `bubbleId::%`——
    一条都匹配不上，引用集合恒为空，于是**所有 blob 都被判成孤儿**。
    这个错不会崩、不会报错，只会让清理悄悄删掉活数据。

    所以这里不只断言「前缀列表长什么样」，而是断言「按它去查真的有行」。
    """
    con = sqlite3.connect(f"file:{mini_db}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        for prefix in list_all_key_prefixes(cur):
            n = cur.execute(
                "SELECT COUNT(*) FROM cursorDiskKV WHERE key LIKE ?", (prefix,)
            ).fetchone()[0]
            assert n > 0, f"前缀 {prefix!r} 一条都匹配不上——拼接出错了"
            assert "::" not in prefix, f"前缀 {prefix!r} 有双冒号，LIKE 必然匹配不到"
    finally:
        con.close()


def test_blob_plan_keeps_referenced_and_drops_orphans(mini_db: Path) -> None:
    """计划里：被引用的留下，孤儿进删除名单。"""
    con = sqlite3.connect(f"file:{mini_db}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        refs = collect_referenced_hashes(cur)
        plan = plan_blob_cleanup(cur, refs, measure_bytes=True)

        assert HASH_REFERENCED not in plan.orphan
        assert HASH_OFSCONTENT not in plan.orphan
        assert set(plan.orphan) == {HASH_ORPHAN_1, HASH_ORPHAN_2}
        assert plan.total == 4
        assert plan.referenced == 2
        assert plan.orphan_bytes > 0
    finally:
        con.close()


def test_plan_blob_cleanup_is_the_only_orphan_verdict(mini_db: Path) -> None:
    """判定只此一处：预览与实跑必须用同一份结论。

    回归背景：本项目已多次栽在「同一个判定写两份」上（apply-decisions 漏查保护、
    plan_quarantine 与实跑各算一遍、explain 自算结论）。这里守住：
    `plan_blob_cleanup` 产出的名单，就是实跑要删的名单。
    """
    con = sqlite3.connect(f"file:{mini_db}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        refs = collect_referenced_hashes(cur)
        plan = plan_blob_cleanup(cur, refs)
    finally:
        con.close()

    # 实跑：只按 plan 的名单删
    con = sqlite3.connect(str(mini_db))
    try:
        cur = con.cursor()
        stats = delete_orphan_blobs(con, cur, plan.orphan, pause=0)
        con.commit()
    finally:
        con.close()

    assert stats["blob"] == 2
    remaining = _blob_keys(mini_db)
    assert f"{BLOB_PREFIX}{HASH_REFERENCED}" in remaining
    assert f"{BLOB_PREFIX}{HASH_OFSCONTENT}" in remaining
    assert f"{BLOB_PREFIX}{HASH_ORPHAN_1}" not in remaining
    assert f"{BLOB_PREFIX}{HASH_ORPHAN_2}" not in remaining


# ---------------------------------------------------------------------------
# 安全性
# ---------------------------------------------------------------------------


def test_item_table_is_never_deleted(mini_db: Path) -> None:
    """`ItemTable` 是用户设置（插件 / 主题 / 快捷键）——清理绝不能碰它。"""
    before = _item_table_count(mini_db)
    assert before == 1

    report = clean(mini_db, days=90, vacuum=False, blob_pause=0)

    assert _item_table_count(mini_db) == before
    assert report.mode == "clean"


def test_clean_removes_orphan_blobs_and_keeps_referenced(mini_db: Path) -> None:
    """端到端：真正清理后，被引用的 blob 还在，孤儿没了，ItemTable 还在。"""
    clean(mini_db, days=90, vacuum=False, blob_pause=0)

    remaining = _blob_keys(mini_db)
    assert f"{BLOB_PREFIX}{HASH_REFERENCED}" in remaining, "被引用的 blob 不该被删"
    assert f"{BLOB_PREFIX}{HASH_OFSCONTENT}" in remaining, "ofsContent 引用的 blob 不该被删"
    assert f"{BLOB_PREFIX}{HASH_ORPHAN_1}" not in remaining
    assert f"{BLOB_PREFIX}{HASH_ORPHAN_2}" not in remaining
    assert _item_table_count(mini_db) == 1


def test_analyze_does_not_touch_the_database(mini_db: Path) -> None:
    """干跑必须**一字不改**数据库——这是「默认不执行」的地基。"""
    before = mini_db.read_bytes()
    report = analyze(mini_db, days=90)
    assert report.mode == "dry-run"
    assert mini_db.read_bytes() == before, "干跑改动了数据库"
    assert report.blob["blob_orphan"] == 2
    assert report.blob["blob_referenced"] == 2


def test_verify_database_rejects_a_library_without_item_table(tmp_path: Path) -> None:
    """压缩库缺表 / ItemTable 为空 → 拒绝替换（拿损坏库覆盖好库是灾难）。"""
    bad = tmp_path / "bad.vscdb"
    con = sqlite3.connect(str(bad))
    con.execute("CREATE TABLE cursorDiskKV (key TEXT)")
    con.execute("CREATE TABLE composerHeaders (composerId TEXT)")
    con.commit()
    con.close()
    with pytest.raises(CursorCleanupError):
        verify_database(bad)


def test_human_bytes_picks_a_readable_unit() -> None:
    """几十 MB 不能显示成 `0.00 GB`（参考实现的 `human()` 只有 GB 一档）。"""
    assert human_bytes(512) == "512 B"
    assert human_bytes(2048) == "2.0 KB"
    assert human_bytes(5 * 1024**2) == "5.0 MB"
    assert human_bytes(3 * 1024**3) == "3.00 GB"


# ---------------------------------------------------------------------------
# CLI 层：默认不执行
# ---------------------------------------------------------------------------


def test_cursor_cleanup_cli_is_dry_run_by_default(mini_db: Path) -> None:
    """CLI 默认只统计；没有 `--apply` 就绝不能改库。

    这条守的是 dirtidy 的通用约定：破坏性命令默认不动手。
    """
    from dirtidy.cli import main

    before = mini_db.read_bytes()
    rc = main(["cursor-cleanup", "--db", str(mini_db)])
    assert rc == 0
    assert mini_db.read_bytes() == before, "没加 --apply 却改了库"


def test_cursor_cleanup_cli_requires_apply_to_mutate(mini_db: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """干跑输出必须是 JSON 且标明是干跑；`--apply` 才会真正删。"""
    from dirtidy.cli import main

    rc = main(["cursor-cleanup", "--db", str(mini_db)])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out.split("\n干跑结束")[0])
    assert payload["mode"] == "dry-run"
    assert payload["blob"]["blob_orphan"] == 2
    assert "干跑结束" in out

    # 加上 --apply 后才动手
    rc = main(["cursor-cleanup", "--db", str(mini_db), "--apply", "--no-vacuum"])
    assert rc == 0
    remaining = _blob_keys(mini_db)
    assert f"{BLOB_PREFIX}{HASH_ORPHAN_1}" not in remaining
    assert f"{BLOB_PREFIX}{HASH_REFERENCED}" in remaining


def test_cursor_cleanup_cli_refuses_while_cursor_runs(
    mini_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Cursor 还在跑就拒绝执行——改库会抢 WAL 锁，可能让 Cursor 起不来。"""
    from dirtidy import cli

    monkeypatch.setattr("dirtidy.cursor_cleanup.cursor_running", lambda: True)
    rc = cli.main(["cursor-cleanup", "--db", str(mini_db), "--apply"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "Cursor" in err
    assert _blob_keys(mini_db), "库不该被动过"


def test_cursor_running_does_not_crash_when_ps_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """`ps` 不可用时宁可误报为「在跑」也不能崩——崩了就等于没保护。"""
    import dirtidy.cursor_cleanup as cc

    def boom(*args, **kwargs):
        raise FileNotFoundError("ps not found")

    monkeypatch.setattr(cc.subprocess, "check_output", boom)
    assert cc.cursor_running() is False
