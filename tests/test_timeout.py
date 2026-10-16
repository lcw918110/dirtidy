"""单次运行超时（`docs/06` T-3）：超时不许丢结果，也不许假装跑完了。

触发场景是 `watch` 遇上一块慢盘，一轮扫描长时间不返回，整个监视就静默停住。
所以这里要守两条：**超时后已扫到的部分照样落盘**，以及**超时那轮不许执行隔离**。
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from dirtidy import deadline as deadline_mod
from dirtidy import watch
from dirtidy.deadline import Deadline, RunTimeout
from dirtidy.models import iter_json_array, read_json, run_dir
from dirtidy.pipeline import create_run
from dirtidy.scan.scanner import _walk_tree, hash_items, scan_tree


class _Clock:
    """可控时钟：每次读表前进 `step` 秒。

    不用真的 sleep 去撞超时——那会让用例依赖机器速度：快机器上根本来不及超时，
    慢机器上又会提前超时。**超时逻辑必须能在确定的时刻被触发**。
    """

    def __init__(self, start: float = 1000.0, step: float = 0.0) -> None:
        self.now = start
        self.step = step

    def monotonic(self) -> float:
        value = self.now
        self.now += self.step
        return value


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch):
    """只替换 `dirtidy.deadline` 里的时钟，不动全局 `time` 模块。"""

    def _install(*, step: float) -> _Clock:
        c = _Clock(step=step)
        monkeypatch.setattr(deadline_mod, "time", SimpleNamespace(monotonic=c.monotonic))
        return c

    return _install


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """15 个目录 + 若干文件：足够让「第 4 次检查」发生时已经有扫到的条目。"""
    root = tmp_path / "ws"
    for i in range(6):
        d = root / f"d{i}" / "sub"
        d.mkdir(parents=True)
        (d / "a.txt").write_text("x", encoding="utf-8")
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "pkg" / "__pycache__" / "m.pyc").write_text("x", encoding="utf-8")
    return root


def test_deadline_disabled_never_expires() -> None:
    """0 或负数 = 不限，且 `check()` 不能抛。"""
    d = Deadline(0)
    assert d.enabled is False
    assert d.expired() is False
    assert d.remaining() is None
    d.check(phase="任意", processed=0)


def test_deadline_carries_partial_progress(clock) -> None:
    """超时异常必须带着已完成的进度——不带结果的超时等于把这次的活白干。"""
    clock(step=10.0)
    d = Deadline(1.0)
    with pytest.raises(RunTimeout) as ei:
        d.check(
            phase="扫描元数据",
            processed=7,
            partial_items=["a"],
            partial_errors=[{"path": "b", "error": "e"}],
        )
    assert ei.value.phase == "扫描元数据"
    assert ei.value.seconds == 1.0
    assert ei.value.processed == 7
    assert ei.value.partial_items == ["a"]
    assert ei.value.partial_errors == [{"path": "b", "error": "e"}]


def test_deadline_uses_monotonic_clock() -> None:
    """必须用单调时钟：墙上时钟被 NTP 校准或用户改系统时间后，
    超时要么提前触发、要么永不触发，而且很难查。

    用 AST 查**实际调用**，不查文本——源码注释里就写着「不用 `time.time()`」，
    文本匹配会被那句说明反过来骗到。
    """
    tree = ast.parse(Path(deadline_mod.__file__).read_text(encoding="utf-8"))
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "time"
    }
    assert called == {"monotonic"}, f"deadline 只该读单调时钟，实际调用了 {sorted(called)}"


def test_walk_tree_stops_and_keeps_partial(clock, tree: Path) -> None:
    clock(step=0.25)  # 第 4 次检查越过 1001
    with pytest.raises(RunTimeout) as ei:
        _walk_tree(tree, deadline=Deadline(1.0))
    assert ei.value.phase == "扫描元数据"
    assert ei.value.partial_items, "超时必须把已扫到的条目带出来"


def test_hash_items_honours_deadline(clock, tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    for i in range(3):
        (root / f"f{i}.txt").write_text("same", encoding="utf-8")
    items, _ = scan_tree(root, quick=True)

    clock(step=10.0)
    with pytest.raises(RunTimeout) as ei:
        hash_items(root, items, deadline=Deadline(1.0), use_hash_cache=False)
    assert ei.value.phase == "内容 hash"


def test_scan_timeout_still_writes_partial_run(clock, tree: Path) -> None:
    """超时**不丢结果**：已扫到的部分照常落盘，meta 标出来，errors 里留一条可操作提示。"""
    clock(step=0.25)
    meta = create_run(tree.resolve(), quick=True, timeout=1.0)

    assert meta["timed_out"] is True
    assert meta["timeout"]["phase"] == "扫描元数据"
    assert meta["timeout_seconds"] == 1.0

    rdir = run_dir(tree.resolve(), meta["run_id"])
    assert list(iter_json_array(rdir / "inventory.json")), "超时也要写已扫到的部分"
    errors = read_json(rdir / "errors.json")
    joined = " ".join(str(e.get("error", "")) for e in errors)
    assert "超时" in joined and "--timeout" in joined, "提示要能照着做"


def test_scan_timeout_does_not_quarantine(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """超时后**不许**执行隔离。

    回归背景：基于「只扫了一半的树」去搬文件，比不搬更糟——用户看到的是一次
    「跑完了」的隔离，实际只覆盖了半棵树。这里让「确实扫到了待隔离项」与「超时」
    同时成立，才能证明跳过隔离是超时导致的，而不是因为没有命中项。
    """
    root = tmp_path / "ws"
    (root / "pkg" / "__pycache__").mkdir(parents=True)
    (root / "pkg" / "__pycache__" / "m.pyc").write_text("x", encoding="utf-8")

    from dirtidy import pipeline

    real = pipeline.scan_tree

    def fake_scan_tree(r, *, quick=False, workers=4, on_progress=None, deadline=None, **_kw):
        # 模拟「已经扫到 __pycache__ 之后才超时」
        items, errors = real(r, quick=True)
        raise RunTimeout(
            seconds=1.0,
            phase="扫描元数据",
            processed=len(items),
            partial_items=items,
            partial_errors=errors,
        )

    monkeypatch.setattr(pipeline, "scan_tree", fake_scan_tree)
    meta = pipeline.create_run(root.resolve(), quick=True, apply=True, timeout=1.0)

    assert meta["timed_out"] is True
    assert meta["auto_hits"] >= 1, "这次确实扫到了待隔离项"
    assert meta["applied"] is False, "没执行隔离就不能报 applied=True"
    assert meta["apply_skipped_by_timeout"] is True
    assert (root / "pkg" / "__pycache__" / "m.pyc").exists(), "命中项必须原地不动"
    assert not (root / ".dirtidy" / "quarantine").exists(), "不该建隔离区"


def test_scan_exit_code_is_3_on_timeout(clock, tree: Path, capsys: pytest.CaptureFixture) -> None:
    """退出码要和「跑失败」区分开：脚本能据此判断「重跑或调大时限即可」。"""
    from dirtidy.cli import EXIT_TIMEOUT, build_parser, cmd_scan

    clock(step=0.25)
    args = build_parser().parse_args(["scan", str(tree), "--quick", "--timeout", "1"])
    assert cmd_scan(args) == EXIT_TIMEOUT
    err = capsys.readouterr().err
    assert "--timeout" in err and "未执行隔离" in err


def test_watch_continues_after_round_timeout(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    """一轮超时只跳过这一轮——监视是无人值守的，卡一轮就整个停摆是不可接受的。"""
    rounds: list[object] = [
        RunTimeout(seconds=1.0, phase="扫描元数据", processed=3),
        {"a"},
    ]

    def fake_scan(root: Path, **kwargs: object) -> set[str]:
        item = rounds.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, set)
        return item

    rc = watch.run_watch(
        tmp_path,
        max_rounds=2,
        notify_enabled=False,
        timeout=1.0,
        _scan=fake_scan,
        _sleep=lambda _s: None,
    )

    assert rc == 0
    out = capsys.readouterr().out
    assert "第 1 轮扫描超时" in out
    assert "下一轮照常" in out, "要说清监视还活着，否则用户以为它已经死了"
    assert "新增 1 项" in out, "超时之后必须还能继续跑出结果"


def test_watch_timeout_does_not_poison_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    """超时那轮不许更新比对基线。

    拿只扫了一半的集合去当 `previous`，下一轮会把没扫到的东西全报成「消失」——
    那是在编数据，比不报更糟。
    """
    rounds: list[object] = [
        {"a", "b"},
        RunTimeout(seconds=1.0, phase="扫描元数据", processed=1),
        {"a", "b"},
    ]

    def fake_scan(root: Path, **kwargs: object) -> set[str]:
        item = rounds.pop(0)
        if isinstance(item, Exception):
            raise item
        assert isinstance(item, set)
        return item

    watch.run_watch(
        tmp_path,
        max_rounds=3,
        notify_enabled=False,
        timeout=1.0,
        _scan=fake_scan,
        _sleep=lambda _s: None,
    )

    out = capsys.readouterr().out
    # 首轮把所有命中报成「新增」是有意的，所以「新增」只该出现这一次；
    # 若超时那轮把基线清空或写成了半份结果，第 3 轮会再报一次「新增」。
    assert out.count("新增") == 1, "超时那轮不该清空或污染比对基线"
    assert "无变化" in out, "第 3 轮与最后一次完整结果相同，应报「无变化」"
