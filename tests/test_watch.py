"""定时监视：只扫描不执行这条不变量，以及「新增/消失」比对。"""

from __future__ import annotations

from pathlib import Path

import pytest

from dirtidy import watch


@pytest.fixture()
def ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    (root / "app" / "__pycache__").mkdir(parents=True)
    (root / "app" / "__pycache__" / "m.cpython-312.pyc").write_text("x", encoding="utf-8")
    (root / "keep.txt").write_text("hi", encoding="utf-8")
    return root


def test_watch_source_never_applies() -> None:
    """硬边界：监视只能扫描，不能执行。

    这条不变量靠「源码里不出现执行入口」保证，而不是靠注释约定——
    日后有人顺手加回来，这里会红。
    """
    src = Path(watch.__file__).read_text(encoding="utf-8")
    for forbidden in ("apply=True", "quarantine_hits", "apply_pending_rules", "apply_decisions"):
        assert forbidden not in src, f"watch 不该出现 {forbidden}：监视只扫描，不执行隔离"


def test_scan_hits_finds_auto_quarantine(ws: Path) -> None:
    hits = watch.scan_hits(ws)
    assert any(p.endswith("__pycache__") for p in hits), f"应命中 __pycache__，实际 {hits}"


def test_scan_hits_dedupes_nested(ws: Path) -> None:
    """目录命中覆盖其内文件命中：报「5 项」而实际只动 4 项会让人以为漏了。"""
    hits = watch.scan_hits(ws)
    assert "app/__pycache__" in hits
    assert "app/__pycache__/m.cpython-312.pyc" not in hits


def test_scan_hits_does_not_touch_files(ws: Path) -> None:
    """扫描不能动任何用户文件——这是监视能被放心开着的前提。"""

    def user_files() -> list[str]:
        return sorted(
            p.relative_to(ws).as_posix()
            for p in ws.rglob("*")
            if not p.relative_to(ws).as_posix().startswith(".dirtidy")
        )

    before = user_files()
    watch.scan_hits(ws)
    assert user_files() == before


def test_scan_hits_leaves_no_run_artifacts(ws: Path) -> None:
    """每半小时留一份 run 目录会把 .dirtidy/ 撑爆，所以监视不落盘。"""
    watch.scan_hits(ws)
    runs = ws / ".dirtidy" / "runs"
    assert not runs.exists() or not any(runs.iterdir())


def test_diff_hits_reports_both_directions() -> None:
    added, removed = watch.diff_hits({"a", "b"}, {"b", "c"})
    assert added == {"c"}
    assert removed == {"a"}


def test_notify_swallows_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    """通知发不出去不能让监视中断。"""

    def boom(*args: object, **kwargs: object) -> None:
        raise OSError("没有可用的通知命令")

    monkeypatch.setattr(watch.subprocess, "run", boom)
    assert watch.notify("标题", "正文") is False


def test_run_watch_reports_first_round_as_new(ws: Path, capsys: pytest.CaptureFixture) -> None:
    """首轮把所有命中报成「新增」是有意的：启动监视就想知道现在有什么。"""
    rounds: list[set[str]] = [{"a", "b"}]

    def fake_scan(root: Path, **kwargs: object) -> set[str]:
        return rounds.pop(0)

    rc = watch.run_watch(ws, max_rounds=1, notify_enabled=False, _scan=fake_scan)

    assert rc == 0
    out = capsys.readouterr().out
    assert "新增 2 项" in out


def test_run_watch_second_round_reports_only_new(ws: Path, capsys: pytest.CaptureFixture) -> None:
    """第二轮只报增量，不把老命中再喊一遍。"""
    rounds: list[set[str]] = [{"a"}, {"a", "b"}]

    def fake_scan(root: Path, **kwargs: object) -> set[str]:
        return rounds.pop(0)

    watch.run_watch(
        ws, max_rounds=2, notify_enabled=False, _scan=fake_scan, _sleep=lambda _s: None
    )

    out = capsys.readouterr().out
    assert "新增 1 项" in out
    assert "+ b" in out
    assert out.count("+ a") == 1, "老命中不该在第二轮被重复报成新增"


def test_run_watch_reports_disappeared_items(ws: Path, capsys: pytest.CaptureFixture) -> None:
    """消失也要报：那是用户自己处理掉了，是正反馈。"""
    rounds: list[set[str]] = [{"a", "b"}, {"b"}]

    def fake_scan(root: Path, **kwargs: object) -> set[str]:
        return rounds.pop(0)

    watch.run_watch(
        ws, max_rounds=2, notify_enabled=False, _scan=fake_scan, _sleep=lambda _s: None
    )

    out = capsys.readouterr().out
    assert "消失 1 项" in out
    assert "- a" in out


def test_run_watch_quiet_round(ws: Path, capsys: pytest.CaptureFixture) -> None:
    """没有变化时给一行简短输出，不刷屏也不沉默。"""
    rounds: list[set[str]] = [{"a"}, {"a"}]

    def fake_scan(root: Path, **kwargs: object) -> set[str]:
        return rounds.pop(0)

    watch.run_watch(
        ws, max_rounds=2, notify_enabled=False, _scan=fake_scan, _sleep=lambda _s: None
    )

    assert "无变化" in capsys.readouterr().out


def test_run_watch_sleeps_between_rounds(ws: Path) -> None:
    """轮次之间必须真的等，否则会变成忙等把 CPU 打满。"""
    slept: list[float] = []

    def fake_scan(root: Path, **kwargs: object) -> set[str]:
        return set()

    watch.run_watch(
        ws,
        interval_minutes=0.5,
        max_rounds=2,
        notify_enabled=False,
        _scan=fake_scan,
        _sleep=slept.append,
    )

    assert slept == [30.0]


def test_cli_registers_watch() -> None:
    from dirtidy.cli import build_parser

    args = build_parser().parse_args(["watch", "--rounds", "1", "--interval", "0.25"])
    assert args.rounds == 1
    assert args.interval == 0.25
    assert args.root == "."
