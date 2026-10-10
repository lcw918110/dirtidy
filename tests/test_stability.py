"""稳定性回归：产物的崩溃一致性。

后台程序会被 SIGINT / SIGTERM / OOM 打断，这些用例守的是「被打断时不会留下
读不了或对不上的状态」：

- 产物写盘必须**原子**：读者永远看不到半个 JSON（截断的数组比没有文件更糟，
  它会让人以为是数据损坏而不是「这次没跑完」）；
- 隔离是破坏性操作，搬走的每一条都必须在清单里有记录——否则用户不知道文件去哪了。

设计取舍与仍未覆盖的边界（SIGKILL 硬杀）见 `docs/08-稳定性与性能设计.md`。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dirtidy.models import RuleHit, read_json, write_json, write_json_stream
from dirtidy.quarantine.store import nothing_protected, quarantine_hits


def _temp_leftovers(directory: Path) -> list[str]:
    return [p.name for p in directory.iterdir() if p.name.endswith(".tmp")]


# ------------------------------------------------------------------ 原子写

def test_write_json_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "meta.json"
    write_json(target, {"ok": True})
    assert read_json(target) == {"ok": True}
    assert _temp_leftovers(tmp_path) == [], "原子写的临时文件必须被清掉"


def test_write_json_stream_leaves_no_temp_file(tmp_path: Path) -> None:
    target = tmp_path / "inventory.json"
    assert write_json_stream(target, ({"i": i} for i in range(10))) == 10
    assert json.loads(target.read_text(encoding="utf-8")) == [{"i": i} for i in range(10)]
    assert _temp_leftovers(tmp_path) == []


def test_write_json_keeps_old_content_when_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写盘失败（磁盘满 / 进程被杀）时，原文件必须保持**旧的完整内容**。

    直接 `write_text` 会把目标文件截断成半个 JSON——下一次读取报「数组未闭合」，
    一次崩溃连带毁掉整个 run 的产物。
    """
    target = tmp_path / "meta.json"
    write_json(target, {"ok": True})

    real_write_text = Path.write_text

    def boom(self: Path, *args: object, **kwargs: object) -> int:
        if self.name.endswith(".tmp"):
            raise OSError("No space left on device")
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", boom)
    with pytest.raises(OSError):
        write_json(target, {"ok": False})

    assert read_json(target) == {"ok": True}, "失败的写入不得动到原文件"
    assert _temp_leftovers(tmp_path) == []


def test_write_json_stream_keeps_old_content_when_generator_fails(tmp_path: Path) -> None:
    """流式写盘要几十秒到几分钟，中途被中断的概率比一次性写高得多。"""
    target = tmp_path / "inventory.json"
    write_json_stream(target, [{"i": 1}])

    def rows():
        yield {"i": 2}
        raise RuntimeError("生成器中途炸了")

    with pytest.raises(RuntimeError):
        write_json_stream(target, rows())

    assert json.loads(target.read_text(encoding="utf-8")) == [{"i": 1}]
    assert _temp_leftovers(tmp_path) == []


# ------------------------------------------------- 隔离清单的崩溃一致性

def test_quarantine_hits_persists_manifest_when_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """搬到一半被中断时，**已搬走**的条目必须已经在清单里。

    这是本工具最不能出的错：文件离开原位、清单里却查不到，用户不知道它去哪了，
    也无从还原。所以清单写入放在 `finally` 里，而不是等整批搬完。
    """
    import dirtidy.quarantine.store as store

    root = tmp_path / "ws"
    (root / "d").mkdir(parents=True)
    (root / "d" / "a.txt").write_text("a", encoding="utf-8")
    (root / "d" / "b.txt").write_text("b", encoding="utf-8")
    qdir = root / ".dirtidy" / "quarantine" / "r1"
    manifest = root / ".dirtidy" / "runs" / "r1" / "quarantine_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)

    hits = [
        RuleHit(path="d/a.txt", rule_id="t", action="auto_quarantine", reason="t"),
        RuleHit(path="d/b.txt", rule_id="t", action="auto_quarantine", reason="t"),
    ]

    real_move = store.shutil.move
    calls = {"n": 0}

    def flaky(src: object, dest: object) -> object:
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("模拟第二条时被 Ctrl-C")
        return real_move(src, dest)  # type: ignore[arg-type]

    monkeypatch.setattr(store.shutil, "move", flaky)

    with pytest.raises(KeyboardInterrupt):
        quarantine_hits(root, "r1", hits, qdir, manifest, is_protected=nothing_protected)

    recorded = read_json(manifest)
    assert [e["original_path"] for e in recorded] == ["d/a.txt"], "已搬走的那条必须在清单里"
    assert not (root / "d" / "a.txt").exists()
    assert (root / "d" / "b.txt").exists(), "没搬成的那条要留在原位"


def test_quarantine_hits_writes_manifest_when_nothing_moved(tmp_path: Path) -> None:
    """一条都没搬也要写出清单：调用方与 Web 都假定这个文件存在。"""
    root = tmp_path / "ws"
    root.mkdir()
    qdir = root / ".dirtidy" / "quarantine" / "r1"
    manifest = root / ".dirtidy" / "runs" / "r1" / "quarantine_manifest.json"

    entries = quarantine_hits(
        root, "r1", [], qdir, manifest, is_protected=nothing_protected
    )
    assert entries == []
    assert read_json(manifest) == []
