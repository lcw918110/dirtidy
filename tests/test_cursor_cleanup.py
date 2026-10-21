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
    default_backup_dir,
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

    report = clean(mini_db, days=90, vacuum=False, blob_pause=0, backup_dir=mini_db.parent / "bk1")

    assert _item_table_count(mini_db) == before
    assert report.mode == "clean"


def test_clean_removes_orphan_blobs_and_keeps_referenced(mini_db: Path) -> None:
    """端到端：真正清理后，被引用的 blob 还在，孤儿没了，ItemTable 还在。"""
    clean(mini_db, days=90, vacuum=False, blob_pause=0, backup_dir=mini_db.parent / "bk2")

    remaining = _blob_keys(mini_db)
    assert f"{BLOB_PREFIX}{HASH_REFERENCED}" in remaining, "被引用的 blob 不该被删"
    assert f"{BLOB_PREFIX}{HASH_OFSCONTENT}" in remaining, "ofsContent 引用的 blob 不该被删"
    assert f"{BLOB_PREFIX}{HASH_ORPHAN_1}" not in remaining
    assert f"{BLOB_PREFIX}{HASH_ORPHAN_2}" not in remaining
    assert _item_table_count(mini_db) == 1


def test_default_backup_dir_follows_the_database_not_home(mini_db: Path) -> None:
    """备份目录默认跟着**被清理的库**走，绝不写死用户 HOME。

    回归背景：`default_backup_dir()` 原先硬编码 `~/Library/Application Support/
    Cursor/User/globalStorage/...`，而 13 个用例都调 `clean(临时库)` 且不传
    `backup_dir` —— 于是每跑一次测试就往**真实的 Cursor 应用目录**里塞一个备份目录，
    实测在开发机上留下 17 个。CI 的 Linux/Windows 上没有这个路径，
    `mkdir(parents=True)` 还会在那里造出多层不存在的目录。

    这条用例守的是「跑测试不许碰真实 HOME」。
    """
    d = default_backup_dir(mini_db)
    assert d.parent == mini_db.parent, f"备份没跟着库走：{d}"
    assert "CursorStateBackup-" in d.name
    home_cursor = Path.home() / "Library/Application Support/Cursor/User/globalStorage"
    assert home_cursor not in d.parents, f"备份落进了真实 Cursor 目录：{d}"


def test_clean_without_explicit_backup_dir_never_touches_home(mini_db: Path) -> None:
    """不传 `backup_dir` 时，备份也必须落在临时库里，而不是真实 HOME。

    与上一条配合：上一条测「算出来的路径」，这条测「`clean()` 真的用了它」。
    只测前者的话，`clean()` 里另写一个默认值仍然会漏。
    """
    home_cursor = Path.home() / "Library/Application Support/Cursor/User/globalStorage"
    before = set(home_cursor.glob("CursorStateBackup-*")) if home_cursor.exists() else set()

    clean(mini_db, days=90, vacuum=False, blob_pause=0)

    after = set(home_cursor.glob("CursorStateBackup-*")) if home_cursor.exists() else set()
    assert after == before, f"clean() 往真实 Cursor 目录写了备份：{sorted(after - before)}"
    assert list(mini_db.parent.glob("CursorStateBackup-*")), "备份应该落在库旁边"


def test_default_db_path_is_not_macos_only(monkeypatch: pytest.MonkeyPatch) -> None:
    """三个平台各有各的默认库路径，不能写死 macOS。

    写死 macOS 的后果：Linux/Windows 上 `dirtidy cursor-cleanup` 一上手就报
    「找不到数据库」，功能在这两个平台等于不存在。

    注：Windows 分支在 POSIX 上无法真正构造 `WindowsPath`（pathlib 会拒），
    所以这里只校验**拼出来的字符串**，用 `PurePath` 的思路绕开实例化。
    """
    import dirtidy.cursor_cleanup as cc

    monkeypatch.setattr(cc.sys, "platform", "darwin")
    monkeypatch.setattr(cc.os, "name", "posix")
    assert "Library/Application Support/Cursor/User/globalStorage/state.vscdb" in (
        cc.default_db_path().as_posix()
    )

    monkeypatch.setattr(cc.sys, "platform", "linux")
    assert cc.default_db_path().as_posix().endswith(
        ".config/Cursor/User/globalStorage/state.vscdb"
    ), f"Linux 默认路径不对：{cc.default_db_path()}"

    # Windows 分支：把 Path 换成 PureWindowsPath 才能在本机构造并比较
    from pathlib import PureWindowsPath

    monkeypatch.setattr(cc.os, "name", "nt")
    monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
    monkeypatch.setattr(cc, "Path", PureWindowsPath)
    win = cc.default_db_path()
    assert str(win).replace("\\", "/").endswith(
        "Cursor/User/globalStorage/state.vscdb"
    ), f"Windows 默认路径不对：{win}"
    assert "Users/x/AppData/Roaming" in str(win).replace("\\", "/")


def test_clean_writes_an_audit_trail_that_survives_a_crash(mini_db: Path, tmp_path: Path) -> None:
    """清理过程要**边做边记**，不能只在跑完时写一份报告。

    真实库删 blob 是分批做的（每批 500 行、批间暂停），全程几十分钟。中途被
    Ctrl-C / 被杀掉时，内存里的结果直接蒸发——而这是**不可恢复**的删除，
    用户既不知道删了多少，也无从查证。每阶段追加一行 JSONL，断了也留着记录。
    """
    bk = tmp_path / "bk"
    clean(mini_db, days=90, vacuum=False, blob_pause=0, backup_dir=bk)

    log = bk / "dirtidy-cursor-cleanup.jsonl"
    assert log.exists(), "没有审计日志"
    events = [json.loads(line)["event"] for line in log.read_text().splitlines()]
    # 顺序即执行顺序，最后一笔必须是 done——中途断了就不会有它
    assert events[0] == "start"
    assert events[-1] == "done"
    for must in ("backup", "old_sessions", "orphan_sessions", "blob_plan", "blob_deleted"):
        assert must in events, f"审计缺阶段 {must}：{events}"


def test_clean_audit_records_an_abort_when_a_stage_blows_up(
    mini_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """某个阶段炸了也要留一条 `aborted`——用户需要知道「删到第几阶段时断的」。"""
    import dirtidy.cursor_cleanup as cc

    def boom(*args, **kwargs):
        raise RuntimeError("模拟磁盘满")

    monkeypatch.setattr(cc, "collect_referenced_hashes", boom)
    bk = tmp_path / "bk"
    with pytest.raises(RuntimeError, match="模拟磁盘满"):
        clean(mini_db, days=90, vacuum=False, blob_pause=0, backup_dir=bk)

    log = bk / "dirtidy-cursor-cleanup.jsonl"
    assert log.exists(), "失败时也必须留下审计"
    recs = [json.loads(line) for line in log.read_text().splitlines()]
    aborted = [r for r in recs if r["event"] == "aborted"]
    assert aborted, f"缺 aborted 记录：{[r['event'] for r in recs]}"
    assert "模拟磁盘满" in aborted[0]["error"]
    # 断点之前已完成的阶段仍然有据可查
    assert any(r["event"] == "orphan_sessions" for r in recs), "断点前的阶段记录丢了"


def test_audit_records_the_replace_window_so_a_broken_vacuum_is_diagnosable(
    mini_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """VACUUM / 替换阶段的失败也要留痕——那里的窗口最危险。

    `os.replace(db, old_path)` 与 `os.replace(vacuumed_path, db)` 之间，
    `state.vscdb` 这一刻**不存在**。若在此被打断而日志里什么都没写，
    用户打开目录看到库不见了、旁边多了个 `state.vscdb.pre-cleanup`，
    完全无从判断发生了什么。故先记 `replacing`，失败再记 `aborted`。
    """
    import dirtidy.cursor_cleanup as cc

    def bad_verify(path):
        raise RuntimeError("模拟校验不过")

    monkeypatch.setattr(cc, "verify_database", bad_verify)
    bk = tmp_path / "bk"
    with pytest.raises(RuntimeError, match="模拟校验不过"):
        clean(mini_db, days=90, vacuum=True, blob_pause=0, backup_dir=bk)

    recs = [json.loads(line) for line in (bk / "dirtidy-cursor-cleanup.jsonl").read_text().splitlines()]
    names = [r["event"] for r in recs]
    assert "aborted" in names, f"VACUUM 阶段失败没留痕：{names}"
    aborted = next(r for r in recs if r["event"] == "aborted")
    assert aborted["stage"] == "vacuum", f"阶段记错了：{aborted['stage']}"
    # 库必须还在原地：校验不过就绝不能替换
    assert mini_db.exists(), "校验不过却把原库换掉了"


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


def test_cursor_cleanup_cli_requires_apply_to_mutate(
    mini_db: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """干跑输出必须是 JSON 且标明是干跑；`--apply` 才会真正删。

    这里显式把 `cursor_running` 打桩成 False：`cursor_running` 现在是 fail-closed 的
    （取不到进程列表就报「在跑」），而测试环境里 `ps` 可能被沙箱拦掉。
    这条用例要测的是「干跑 vs --apply」，不是「Cursor 在跑时会不会拒绝」——
    后者由 `test_cursor_cleanup_cli_refuses_while_cursor_runs` 负责。
    """
    from dirtidy.cli import main

    monkeypatch.setattr("dirtidy.cursor_cleanup.cursor_running", lambda: False)

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


def test_cursor_running_is_fail_closed_when_ps_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    """取不到进程列表时必须报「在跑」（拒绝清理），而不是放行。

    这里的**方向不能反**。返回 False 的语义是「可以放心改库」；判断不了却给 False，
    就等于在 Cursor 正在写库时拆它的库。

    回归背景：原实现在 `ps` 不存在时直接 `return False`，而这条用例当时断言的正是
    `is False`——把 fail-open 这个 bug 固化成「正确行为」了。Windows 上没有 `ps`，
    于是 `--apply` 在最需要保护的平台上畅通无阻。
    """
    import dirtidy.cursor_cleanup as cc

    def boom(*args, **kwargs):
        raise FileNotFoundError("ps not found")

    monkeypatch.setattr(cc.subprocess, "check_output", boom)
    assert cc.cursor_running() is True, "判断不了就要按「在跑」处理，不能放行"
