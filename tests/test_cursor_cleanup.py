"""Cursor 状态库清理的守卫。

这些用例守的是四件「错了就毁数据」的事：
1. **只删无引用的 blob**——被会话正文引用的 blob 必须原样留下。
2. **绝不删 `ItemTable`**（用户设置：插件 / 主题 / 快捷键）。
3. **引用来源要扫全**——只扫 bubble/checkpoint/composerData 三个前缀会漏掉
   `ofsContent:` 这类同样引用 blob 的键，漏扫即误删活数据（真机实测已复现）。
4. **默认不删 blob**（2026-10-22 事故）——会话还原依赖的 tool-result blob
   在 value 扫描里是「孤儿」（引用在气泡 key 的 `/t:call_<toolCallId>` 协议里，
  不在任何 value 里），删了旧会话就打不开。blob 无法证明年龄与完整归属，
  clean 即使收到逐字确认也必须在写入前拒绝；只读分析保留 toolCallId 守卫。

外加一条 CLI 层的守卫：**默认不执行**，必须显式 `--apply` 才改库。
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from dirtidy.cli import BLOB_CONFIRM_PHRASE_CLI_HELP
from dirtidy.cursor_cleanup import (
    BLOB_CONFIRM_PHRASE,
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


def _database_rows(db: Path) -> dict[str, list[tuple]]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            table: con.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("cursorDiskKV", "composerHeaders", "ItemTable")
        }
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
    """保留历史用例名：blob 年龄与归属不可证，确认后也须在任何写入前拒绝。"""
    before = mini_db.read_bytes()
    rows = _database_rows(mini_db)
    bk = mini_db.parent / "bk2"
    with pytest.raises(CursorCleanupError, match="blob"):
        clean(
            mini_db,
            days=90,
            vacuum=False,
            blob_pause=0,
            backup_dir=bk,
            include_blob=True,
            blob_ack=True,
        )

    assert mini_db.read_bytes() == before, "拒绝 blob 清理时改了数据库字节"
    assert _database_rows(mini_db) == rows, "拒绝 blob 清理时改了数据库行"
    assert not bk.exists(), "拒绝前不应创建备份或审计目录"


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

    只替换 `current_platform()` 而**不去改 `os.name`**：后者是全局单例模块，
    改它等于整个进程都以为自己是另一个平台。实测在 Windows 上跑这类用例，
    pathlib 会去构造 `PosixPath`，一旦用例失败，pytest 格式化失败信息时调
    `Path(os.getcwd())` 就抛 `NotImplementedError`，整个 session 变成
    `INTERNALERROR`——真失败被自己的诊断代码吃掉，CI 上只看到一堆无关的栈。
    """
    import dirtidy.cursor_cleanup as cc

    monkeypatch.setattr(cc, "current_platform", lambda: "darwin")
    assert "Library/Application Support/Cursor/User/globalStorage/state.vscdb" in (
        cc.default_db_path().as_posix()
    )

    monkeypatch.setattr(cc, "current_platform", lambda: "linux")
    assert cc.default_db_path().as_posix().endswith(
        ".config/Cursor/User/globalStorage/state.vscdb"
    ), f"Linux 默认路径不对：{cc.default_db_path()}"

    # Windows 分支：把 Path 换成 PureWindowsPath 才能在本机构造并比较
    from pathlib import PureWindowsPath

    monkeypatch.setattr(cc, "current_platform", lambda: "nt")
    monkeypatch.setenv("APPDATA", r"C:\Users\x\AppData\Roaming")
    monkeypatch.setattr(cc, "Path", PureWindowsPath)
    win = cc.default_db_path()
    assert str(win).replace("\\", "/").endswith(
        "Cursor/User/globalStorage/state.vscdb"
    ), f"Windows 默认路径不对：{win}"
    assert "Users/x/AppData/Roaming" in str(win).replace("\\", "/")


def test_clean_writes_an_audit_trail_that_survives_a_crash(mini_db: Path, tmp_path: Path) -> None:
    """清理过程要**边做边记**，不能只在跑完时写一份报告。

    会话清理中途被 Ctrl-C / 被杀掉时，内存里的结果直接蒸发——
    而这是**不可恢复**的删除，
    用户既不知道删了多少，也无从查证。每阶段追加一行 JSONL，断了也留着记录。
    """
    bk = tmp_path / "bk"
    clean(
        mini_db, days=90, vacuum=False, blob_pause=0, backup_dir=bk,
    )

    log = bk / "dirtidy-cursor-cleanup.jsonl"
    assert log.exists(), "没有审计日志"
    events = [json.loads(line)["event"] for line in log.read_text(encoding="utf-8").splitlines()]
    # 顺序即执行顺序，最后一笔必须是 done——中途断了就不会有它
    assert events[0] == "start"
    assert events[-1] == "done"
    for must in ("backup", "old_sessions", "orphan_sessions"):
        assert must in events, f"审计缺阶段 {must}：{events}"


def test_clean_audit_records_an_abort_when_a_stage_blows_up(
    mini_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """某个阶段炸了也要留一条 `aborted`——用户需要知道「删到第几阶段时断的」。"""
    import dirtidy.cursor_cleanup as cc

    def boom(*args, **kwargs):
        raise RuntimeError("模拟磁盘满")

    monkeypatch.setattr(cc, "filter_stale_orphans", boom)
    bk = tmp_path / "bk"
    with pytest.raises(RuntimeError, match="模拟磁盘满"):
        clean(
            mini_db, days=90, vacuum=False, blob_pause=0, backup_dir=bk,
        )

    log = bk / "dirtidy-cursor-cleanup.jsonl"
    assert log.exists(), "失败时也必须留下审计"
    recs = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    aborted = [r for r in recs if r["event"] == "aborted"]
    assert aborted, f"缺 aborted 记录：{[r['event'] for r in recs]}"
    assert "模拟磁盘满" in aborted[0]["error"]
    # 断点之前已完成的阶段仍然有据可查
    assert any(r["event"] == "old_sessions" for r in recs), "断点前的阶段记录丢了"


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

    recs = [
        json.loads(line)
        for line in (bk / "dirtidy-cursor-cleanup.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    names = [r["event"] for r in recs]
    assert "aborted" in names, f"VACUUM 阶段失败没留痕：{names}"
    aborted = next(r for r in recs if r["event"] == "aborted")
    assert aborted["stage"] == "vacuum", f"阶段记错了：{aborted['stage']}"
    # 库必须还在原地：校验不过就绝不能替换
    assert mini_db.exists(), "校验不过却把原库换掉了"


def test_analyze_does_not_touch_the_database(mini_db: Path) -> None:
    """干跑必须**一字不改**数据库——这是「默认不执行」的地基。

    blob 统计自 2026-10-22 事故后默认关闭（`include_blob=False`），
    所以默认干跑的 `blob` 字段是空 dict；要看 blob 数字须显式
    `include_blob=True`（CLI 的 `--delete-blobs`）。
    """
    before = mini_db.read_bytes()
    report = analyze(mini_db, days=90)
    assert report.mode == "dry-run"
    assert mini_db.read_bytes() == before, "干跑改动了数据库"
    assert report.blob == {}, "默认干跑不该统计 blob（那要读 75 GB 且结论不可信）"

    report_blobs = analyze(mini_db, days=90, include_blob=True)
    assert report_blobs.blob["blob_orphan"] == 2
    assert report_blobs.blob["blob_referenced"] == 2
    assert mini_db.read_bytes() == before, "含 blob 统计的干跑改动了数据库"


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

    blob 自 2026-10-22 事故后默认不删：`--apply` 默认只清会话正文；
    `--delete-blobs` 即使给出逐字确认也会拒绝（由
    `test_cursor_cleanup_cli_blob_delete_requires_typed_confirmation` 负责）。
    """
    from dirtidy.cli import main

    monkeypatch.setattr("dirtidy.cursor_cleanup.cursor_running", lambda: False)

    rc = main(["cursor-cleanup", "--db", str(mini_db)])
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out.split("\n干跑结束")[0])
    assert payload["mode"] == "dry-run"
    assert payload["blob"] == {}, "默认干跑不该统计 blob"
    assert "干跑结束" in out

    # 加上 --apply 后才动手（默认不含 blob）
    rc = main(["cursor-cleanup", "--db", str(mini_db), "--apply", "--no-vacuum"])
    assert rc == 0
    remaining = _blob_keys(mini_db)
    assert f"{BLOB_PREFIX}{HASH_ORPHAN_1}" in remaining, "--apply 默认不该删 blob"
    assert f"{BLOB_PREFIX}{HASH_ORPHAN_2}" in remaining, "--apply 默认不该删 blob"
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


# ---------------------------------------------------------------------------
# 2026-10-22 事故回归：默认不删 blob + toolCallId 守卫 + 逐字确认
# ---------------------------------------------------------------------------


def test_clean_default_never_deletes_blobs(mini_db: Path) -> None:
    """**事故核心回归**：`clean()` 默认一个 blob 都不删。

    2026-10-22 事故：办公仓脚本删了 5413 个「无引用」blob，其中 3 个
    tool-result blob 是会话还原要取的（引用在气泡 key 的 `/t:call_<toolCallId>`
    协议里、不在任何 value 里），整条会话 UI 判死且无法恢复。
    自此 blob 清理默认关闭——「无 64 位引用」证明不了「会话还原不需要」。
    """
    before = _blob_keys(mini_db)
    clean(mini_db, days=90, vacuum=False, blob_pause=0, backup_dir=mini_db.parent / "bk-def")
    assert _blob_keys(mini_db) == before, "默认清理删了 blob"


def test_clean_refuses_blob_deletion_without_typed_ack(mini_db: Path) -> None:
    """未确认的 blob 清理必须整体拒绝，不能先清会话或写备份、审计。"""
    before = mini_db.read_bytes()
    rows = _database_rows(mini_db)
    bk = mini_db.parent / "bk-ack"
    with pytest.raises(CursorCleanupError, match="blob"):
        clean(
            mini_db, days=90, vacuum=False, blob_pause=0, backup_dir=bk,
            include_blob=True, blob_ack=False,
        )
    assert mini_db.read_bytes() == before
    assert _database_rows(mini_db) == rows
    assert not bk.exists(), "拒绝前不应创建备份或审计目录"


def test_clean_deletes_blobs_only_with_full_ack(mini_db: Path) -> None:
    """保留历史用例名：完整确认也不能证明 blob 属于 days 范围，必须拒绝。"""
    before = mini_db.read_bytes()
    rows = _database_rows(mini_db)
    bk = mini_db.parent / "bk-ack-ok"
    with pytest.raises(CursorCleanupError, match="blob"):
        clean(
            mini_db, days=90, vacuum=False, blob_pause=0, backup_dir=bk,
            include_blob=True, blob_ack=True,
        )
    assert mini_db.read_bytes() == before
    assert _database_rows(mini_db) == rows
    assert not bk.exists(), "完整确认也不能触发备份或审计写入"


def test_blob_confirm_phrase_is_single_sourced() -> None:
    """确认语只此一处：CLI 帮助文案与 `cursor_cleanup` 判定用同一个常量。

    两处各写一份迟早漂移——漂移的那份就是「用户照着帮助敲了确认语却被拒」
    或者更糟「确认语校验被绕过」。
    """
    assert BLOB_CONFIRM_PHRASE_CLI_HELP == BLOB_CONFIRM_PHRASE


def test_cursor_cleanup_cli_blob_delete_requires_typed_confirmation(
    mini_db: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """CLI 层：错误、缺失或完整确认都不能放行 blob 删除（退出码 2）。"""
    from dirtidy.cli import main

    monkeypatch.setattr("dirtidy.cursor_cleanup.cursor_running", lambda: False)
    before = mini_db.read_bytes()
    rows = _database_rows(mini_db)

    # 错误的确认语
    rc = main(["cursor-cleanup", "--db", str(mini_db), "--apply", "--no-vacuum",
               "--delete-blobs", "--blob-confirm", "yes"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "逐字确认" in err
    assert mini_db.read_bytes() == before, "确认语错误却动了库"
    assert _database_rows(mini_db) == rows

    # 缺确认语
    rc = main(["cursor-cleanup", "--db", str(mini_db), "--apply", "--no-vacuum",
               "--delete-blobs"])
    assert rc == 2
    assert mini_db.read_bytes() == before
    assert _database_rows(mini_db) == rows
    capsys.readouterr()

    # 正确的确认语也不能证明 blob 年龄与归属，仍须拒绝
    rc = main(["cursor-cleanup", "--db", str(mini_db), "--apply", "--no-vacuum",
               "--delete-blobs", "--blob-confirm", BLOB_CONFIRM_PHRASE])
    assert rc == 2
    assert "blob" in capsys.readouterr().err
    assert mini_db.read_bytes() == before
    assert _database_rows(mini_db) == rows
    assert not list(mini_db.parent.glob("CursorStateBackup-*"))


def test_tool_result_blobs_are_guarded_even_when_unreferenced(tmp_path: Path) -> None:
    """**事故核心回归**：tool-result 形态的孤儿 blob 必须被守卫保留。

    复刻 2026-10-22 事故结构：
    - 一个 blob，value 是 tool-result JSON（含 toolCallId）；
    - 会话气泡 key 带 `/t:call_<同一 toolCallId>`，但 value 里**没有**
      任何 64 位哈希（真机取证：toolCallId 只在 key 里）；
    - 旧判定（扫 value 找 64 位哈希）会把这个 blob 判成孤儿。

    守卫要求：只读 planner 不将其列入孤儿名单，默认会话清理保留所有 blob。
    """
    import hashlib

    # 构造事故形态的 blob：value 是 tool-result，key = sha256(value)
    tool_result = (
        b'{"role":"tool","content":[{"type":"tool-result",'
        b'"toolCallId":"call_iiqiugnjzpx5oeluicabqrt7","toolName":"Read",'
        b'"result":"file content..."}]}'
    )
    blob_hash = hashlib.sha256(tool_result).hexdigest()

    db = tmp_path / "state.vscdb"
    con = sqlite3.connect(str(db))
    cur = con.cursor()
    cur.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    cur.execute("CREATE TABLE composerHeaders (composerId TEXT, recency INTEGER)")
    cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")

    cur.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"{BLOB_PREFIX}{blob_hash}", tool_result))
    # 工具结果气泡：key 带 /t:call_<toolCallId>，value 无 64 位哈希
    cur.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        (
            "bubbleId:c86e8b80-a181-4c6e-ab44-575551342b16:"
            "2efba5d4-94e2-4d79-b4b4-5beccd128725/t:call_iiqiugnjzpx5oeluicabqrt7",
            json.dumps({"_v": 3, "type": 2, "text": "tool result bubble"}).encode(),
        ),
    )
    # 一个普通孤儿 blob（非 tool-result）作对照：它应该照常进孤儿名单
    plain = b'{"some":"unreferenced snapshot"}'
    plain_hash = hashlib.sha256(plain).hexdigest()
    cur.execute("INSERT INTO cursorDiskKV VALUES (?, ?)", (f"{BLOB_PREFIX}{plain_hash}", plain))

    cur.execute(
        "INSERT INTO composerHeaders VALUES (?, ?)",
        ("c86e8b80-a181-4c6e-ab44-575551342b16", 2_000_000_000_000),
    )
    cur.execute("INSERT INTO ItemTable VALUES (?, ?)", ("theme", b"dark"))
    con.commit()
    con.close()

    # 判定：tool-result blob 被守卫保留，普通孤儿照常是孤儿
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cur = con.cursor()
        refs = collect_referenced_hashes(cur)
        plan = plan_blob_cleanup(cur, refs)
        assert blob_hash not in plan.orphan, "tool-result blob 进了孤儿名单——事故重演"
        assert plan.guarded == 1
        assert plain_hash in plan.orphan
    finally:
        con.close()

    # 端到端：默认会话清理不删除任何 blob，包括 planner 中的普通孤儿
    clean(db, days=90, vacuum=False, blob_pause=0,
          backup_dir=tmp_path / "bk-guard")
    remaining = _blob_keys(db)
    assert f"{BLOB_PREFIX}{blob_hash}" in remaining, "默认清理删了 tool-result blob——事故重演"
    assert f"{BLOB_PREFIX}{plain_hash}" in remaining


def test_recent_sessions_are_fully_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """90 天边界、未知孤儿与正文近期的会话逐行逐字节保留，只有确证过期才删。"""
    import dirtidy.cursor_cleanup as cc

    now_s = 1_800_000_000
    monkeypatch.setattr(cc.time, "time", lambda: now_s)
    now_ms = now_s * 1000
    day_ms = 86400 * 1000
    cutoff_ms = now_ms - 90 * day_ms
    old_ms = now_ms - 91 * day_ms
    recent_ms = now_ms - 89 * day_ms

    db = tmp_path / "state.vscdb"
    con = sqlite3.connect(str(db))
    cur = con.cursor()
    cur.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value BLOB)")
    cur.execute("CREATE TABLE composerHeaders (composerId TEXT, recency INTEGER)")
    cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
    headers = {
        "recent-cid": now_ms,
        "boundary-cid": cutoff_ms,
        "89-day-cid": recent_ms,
        "91-day-cid": old_ms,
        "stale-header-cid": old_ms,
    }
    cur.executemany("INSERT INTO composerHeaders VALUES (?, ?)", headers.items())
    # 同样覆盖有索引和缺索引会话，边界恰好等于 cutoff 必须保留。
    activity = {
        "recent-cid": now_ms,
        "boundary-cid": cutoff_ms,
        "89-day-cid": recent_ms,
        "91-day-cid": old_ms,
        "stale-header-cid": recent_ms,
        "orphan-boundary-cid": cutoff_ms,
        "orphan-89-day-cid": recent_ms,
        "orphan-91-day-cid": old_ms,
        "unknown-orphan-cid": None,
        "mixed-orphan-cid": old_ms,
    }
    for cid, timestamp in activity.items():
        value = {"text": cid}
        if timestamp is not None:
            value["createdAt"] = timestamp
        raw = json.dumps(value, ensure_ascii=False, indent=2).encode()
        cur.executemany(
            "INSERT INTO cursorDiskKV VALUES (?, ?)",
            [
                (f"composerData:{cid}", raw),
                (f"bubbleId:{cid}:bid1", raw),
                (f"checkpointId:{cid}:cp1", b"checkpoint\x00\xff"),
            ],
        )
    # 首条正文过期不能代表整个孤儿：最后一条气泡仍在保留窗内。
    cur.execute(
        "INSERT INTO cursorDiskKV VALUES (?, ?)",
        ("bubbleId:mixed-orphan-cid:bid2", json.dumps({"timestamp": recent_ms}).encode()),
    )
    cur.executemany(
        "INSERT INTO ItemTable VALUES (?, ?)",
        [("theme", b"dark\x00\xff"), ("settings", b'{ "keep": true }\n')],
    )
    con.commit()
    con.close()

    before_bytes = db.read_bytes()
    before = _database_rows(db)
    preview = analyze(db, days=90)
    assert db.read_bytes() == before_bytes, "干跑改了数据库"
    assert _database_rows(db) == before
    assert preview.old_sessions == 1
    assert preview.orphan_sessions == 1
    assert preview.orphan_skipped == 4

    report = clean(
        db, days=90, vacuum=False, blob_pause=0, backup_dir=tmp_path / "bk-recent"
    )
    assert report.old_sessions == preview.old_sessions
    assert report.orphan_sessions == preview.orphan_sessions
    assert report.orphan_skipped == preview.orphan_skipped
    assert report.old_stats == {"bubble": 1, "checkpoint": 1, "composerData": 1, "headers": 1}
    assert report.orphan_stats == {"bubble": 1, "checkpoint": 1, "composerData": 1, "headers": 0}

    deleted_cids = {"91-day-cid", "orphan-91-day-cid"}
    expected = {
        "cursorDiskKV": [
            row for row in before["cursorDiskKV"]
            if not any(
                row[0] == f"composerData:{cid}"
                or row[0].startswith((f"bubbleId:{cid}:", f"checkpointId:{cid}:"))
                for cid in deleted_cids
            )
        ],
        "composerHeaders": [row for row in before["composerHeaders"] if row[0] not in deleted_cids],
        "ItemTable": before["ItemTable"],
    }
    assert _database_rows(db) == expected, "保留对象的完整行或原始 value 字节被改动"


def test_forbidden_paths_are_never_referenced_in_source() -> None:
    """事故文档硬约束第 5 条：清理范围只许是 `state.vscdb` 及其伴生文件。

    源码层面守卫：`cursor_cleanup.py` 里不许出现对 `agent-transcripts`、
    `chats`、`blob_storage`、`IndexedDB` 的任何路径拼接或字符串引用。
    这些是 Cursor 的会话逐字稿与浏览器存储，碰了就丢用户数据。
    """
    import re as _re

    src = Path(__file__).resolve().parent.parent / "src" / "dirtidy" / "cursor_cleanup.py"
    text = src.read_text(encoding="utf-8")
    # 模块头「事故」一节会提到这些词（作为文档），所以只查**代码行**：
    # 排除注释与 docstring 的粗略办法是查 Path 拼接/字符串字面量形态。
    for forbidden in ("agent-transcripts", "blob_storage", "IndexedDB", "chats"):
        # 出现在非注释代码行（不以 # 开头、不在 docstring 里的字符串字面量）
        for m in _re.finditer(rf'["\']([^\n]*{forbidden}[^\n]*)["\']', text):
            line_no = text[: m.start()].count("\n") + 1
            line = text.splitlines()[line_no - 1]
            stripped = line.strip()
            # 允许：纯注释行（# 开头）——文档性提及
            if stripped.startswith("#"):
                continue
            pytest.fail(f"cursor_cleanup.py:{line_no} 引用了禁止路径 {forbidden!r}：{line}")


# ---------------------------------------------------------------------------
# 根治：定期清理的闭环（备份不回收 = 磁盘翻倍；没人记得手动跑 = 永远涨）
# ---------------------------------------------------------------------------


def test_prune_backups_keeps_only_the_newest(tmp_path: Path) -> None:
    """旧备份必须按名字时间戳回收，且本次刚建的那份永远保留。

    回归背景：每次 `clean()` 都留一份几十 GB 的全量备份，只做不收就会让磁盘
    每轮翻倍（本机曾同时躺着 pre-cleanup 75 GB + backup 2.9 GB + 冷备份三份）。
    """
    from dirtidy.cursor_cleanup import prune_backups

    root = tmp_path / "gs"
    root.mkdir()
    old1 = root / "CursorStateBackup-20261001-090000"
    old2 = root / "CursorStateBackup-20261015-090000"
    current = root / "CursorStateBackup-20261023-090000"
    for d in (old1, old2, current):
        d.mkdir()
        (d / "state.vscdb").write_bytes(b"x")
    # 名字不合规范的目录（用户手工放的）必须原样留下
    handmade = root / "my-notes"
    handmade.mkdir()

    removed = prune_backups(root, keep=1, current=current)
    assert {p.name for p in removed} == {old1.name, old2.name}, f"回收错了：{removed}"
    assert current.exists(), "本次备份被自己回收了"
    assert handmade.exists(), "名字不合规范的目录被误删了"


def test_clean_reclaims_the_replaced_old_library(mini_db: Path, tmp_path: Path) -> None:
    """VACUUM 替换成功后，中转旧库 `pre-cleanup` 必须被回收。

    它只是两步 `os.replace` 之间的崩溃安全垫；新库校验通过并就位后，完整原库
    已在冷备份目录里，再留着它就是无意义的双份（本机 75 GB × 2 的由来）。
    """
    bk = tmp_path / "bk"
    report = clean(mini_db, days=90, vacuum=True, blob_pause=0, backup_dir=bk)
    assert report.vacuumed, "VACUUM 没执行"
    old_path = mini_db.with_name("state.vscdb.pre-cleanup")
    assert not old_path.exists(), "替换成功后中转旧库仍在，磁盘双倍占用"
    # 冷备份里必须有完整原库——这是回收中转旧库的前提
    assert (bk / mini_db.name).exists(), "冷备份里没有原库，却把中转旧库删了"


def test_schedule_never_deletes_blobs(
    mini_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """定期调度器**签名里就没有删 blob 的口子**——无人值守场景高危动作必须不可达。

    这不是靠默认值约定（默认值能被传参覆盖），是靠 `run_cursor_cleanup_schedule`
    内部硬编码 `include_blob=False` 调 `clean()`。
    """
    import dirtidy.cursor_cleanup as cc

    seen: dict[str, object] = {}

    def fake_clean(db, **kwargs):
        seen.update(kwargs)
        return cc.CleanupReport(mode="clean", db=str(db))

    monkeypatch.setattr(cc, "cursor_running", lambda: False)
    monkeypatch.setattr(cc, "clean", fake_clean)
    n = cc.run_cursor_cleanup_schedule(
        mini_db, interval_hours=0.0001, max_rounds=1, notify_enabled=False, _sleep=lambda _s: None
    )
    assert n == 1, "Cursor 退出时应该执行清理"
    assert seen.get("include_blob") is False, f"调度器传了 include_blob={seen.get('include_blob')}"


def test_schedule_skips_while_cursor_runs(
    mini_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cursor 在跑时定期清理必须整轮跳过，绝不写库。"""
    import dirtidy.cursor_cleanup as cc

    called = {"n": 0}

    def boom(*_a, **_k):
        called["n"] += 1
        raise AssertionError("Cursor 在跑时不该调用 clean()")

    monkeypatch.setattr(cc, "cursor_running", lambda: True)
    monkeypatch.setattr(cc, "clean", boom)
    n = cc.run_cursor_cleanup_schedule(
        mini_db, interval_hours=0.0001, max_rounds=2, notify_enabled=False, _sleep=lambda _s: None
    )
    assert n == 0, "Cursor 在跑时不该有任何一轮执行"
    assert called["n"] == 0


def test_cli_schedule_refuses_delete_blobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """CLI 定期模式混用 --delete-blobs 必须直接拒绝（退出码 2），且不碰库。"""
    import dirtidy.cursor_cleanup as cc
    from dirtidy.cli import main

    monkeypatch.setattr(cc, "cursor_running", lambda: False)
    db = tmp_path / "state.vscdb"
    _make_db(db)
    rc = main(
        [
            "cursor-cleanup",
            "--db", str(db),
            "--schedule-hours", "24",
            "--delete-blobs",
        ]
    )
    assert rc == 2, f"定期模式允许了 --delete-blobs（rc={rc}）"
