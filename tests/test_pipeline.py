from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from dirtidy.ai.providers import analyze_run
from dirtidy.models import read_json, run_dir
from dirtidy.pipeline import apply_pending_rules, create_run
from dirtidy.quarantine.store import restore_entries

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "sample_workspace"

#: 默认包里 log_suffix 带 `min_age_days: 1`——「正在写的日志不动」。
#: 需要被清掉的日志必须是旧日志，所以 fixture 里统一把 mtime 回拨。
OLD_MTIME = time.time() - 30 * 86400


def _backdate_logs(root: Path) -> None:
    for p in root.rglob("*.log"):
        os.utime(p, (OLD_MTIME, OLD_MTIME))


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    dest = tmp_path / "ws"
    shutil.copytree(FIXTURE_SRC, dest)
    _backdate_logs(dest)
    return dest


def test_scan_inventory(workspace: Path) -> None:
    meta = create_run(workspace, rules="default", quick=False, apply=False)
    rdir = run_dir(workspace, meta["run_id"])
    inv = read_json(rdir / "inventory.json")
    paths = {i["path"] for i in inv}
    assert "node_modules" in paths
    assert "payload.zip" in paths
    assert "dup_a.txt" in paths


def test_apply_rules_quarantine(workspace: Path) -> None:
    meta = create_run(workspace, rules="default", quick=True, apply=True)
    assert not (workspace / "node_modules").exists()
    assert not (workspace / "app.log").exists()
    rdir = run_dir(workspace, meta["run_id"])
    manifest = read_json(rdir / "quarantine_manifest.json")
    originals = {e["original_path"] for e in manifest}
    assert "node_modules" in originals
    assert "app.log" in originals


def test_duplicate_candidate(workspace: Path) -> None:
    meta = create_run(workspace, rules="default", quick=False, apply=False)
    rdir = run_dir(workspace, meta["run_id"])
    cands = read_json(rdir / "candidates.json")
    dups = [c for c in cands if c["kind"] == "duplicate_group"]
    assert dups
    flat = set()
    for d in dups:
        flat.update(d["paths"])
    assert "dup_a.txt" in flat and "sub/dup_b.txt" in flat


def test_archive_hash_candidate(workspace: Path) -> None:
    meta = create_run(workspace, rules="default", quick=False, apply=False)
    rdir = run_dir(workspace, meta["run_id"])
    cands = read_json(rdir / "candidates.json")
    extracted = [c for c in cands if c["kind"] == "archive_extracted"]
    assert any("payload.zip" in c["paths"] for c in extracted)


def test_restore(workspace: Path) -> None:
    meta = create_run(workspace, rules="default", quick=True, apply=True)
    rdir = run_dir(workspace, meta["run_id"])
    assert not (workspace / "app.log").exists()
    restored = restore_entries(workspace, rdir / "quarantine_manifest.json", restore_all=True)
    assert "app.log" in restored
    assert (workspace / "app.log").exists()


def test_analyze_mock(workspace: Path) -> None:
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    rdir = run_dir(workspace, meta["run_id"])
    results = analyze_run(workspace, rdir, mock=True)
    assert isinstance(results, list)
    assert (rdir / "ai_suggestions.json").exists()


def test_apply_pending(workspace: Path) -> None:
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    assert (workspace / "node_modules").exists()
    result = apply_pending_rules(workspace, meta["run_id"])
    assert result["quarantined"] >= 1
    assert not (workspace / "node_modules").exists()


def test_apply_decisions_archive_and_quarantine(workspace: Path) -> None:
    from dirtidy.decision.store import apply_decisions, save_decisions
    from dirtidy.models import Decision, quarantine_root

    meta = create_run(workspace, rules="default", quick=True, apply=False)
    rdir = run_dir(workspace, meta["run_id"])
    save_decisions(
        rdir,
        [
            Decision(path="keep/readme.md", action="archive", target_relpath="archive/readme.md"),
            Decision(path="zero.txt", action="quarantine", note="test"),
            Decision(path="payload.txt", action="keep"),
        ],
    )
    result = apply_decisions(
        workspace,
        meta["run_id"],
        rdir,
        quarantine_root(workspace, meta["run_id"]),
    )
    assert result["archived"] == 1
    assert result["quarantined"] == 1
    assert result["kept"] == 1
    assert (workspace / "archive/readme.md").exists()
    assert not (workspace / "zero.txt").exists()
    assert (workspace / "payload.txt").exists()


def test_inventory_paths_are_posix(workspace: Path) -> None:
    meta = create_run(workspace, quick=True, apply=False)
    inv = read_json(run_dir(workspace, meta["run_id"]) / "inventory.json")
    assert inv
    assert all("\\" not in item["path"] for item in inv)


def test_sweep_empty_parents_after_move(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    nested = root / "keep_tree" / "inner"
    nested.mkdir(parents=True)
    (nested / "run.log").write_text("line\n", encoding="utf-8")
    (root / "stay.txt").write_text("keep\n", encoding="utf-8")
    _backdate_logs(root)
    create_run(root, rules="default", quick=True, apply=True)
    assert not nested.exists()
    assert not (root / "keep_tree").exists()
    assert (root / "stay.txt").exists()


def test_hash_cache_skips_second_scan(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from dirtidy.scan import scanner

    calls = {"n": 0}
    real = scanner.file_sha256

    def wrapped(path: Path, chunk_size: int = 1024 * 1024) -> str:
        calls["n"] += 1
        return real(path, chunk_size)

    monkeypatch.setattr(scanner, "file_sha256", wrapped)
    create_run(workspace, quick=False, apply=False)
    assert calls["n"] > 0
    calls["n"] = 0
    create_run(workspace, quick=False, apply=False)
    assert calls["n"] == 0


def test_purge_requires_yes_then_deletes(workspace: Path) -> None:
    from dirtidy.cli import main

    meta = create_run(workspace, rules="default", quick=True, apply=True)
    qdir = workspace / ".dirtidy" / "quarantine" / meta["run_id"]
    assert qdir.exists()
    assert main(["purge-quarantine", str(workspace), meta["run_id"]]) == 2
    assert qdir.exists()
    assert main(["purge-quarantine", str(workspace), meta["run_id"], "--yes"]) == 0
    assert not qdir.exists()
    # 清单也要一起收敛，否则「已隔离」列表全是幽灵条目（按它还原则报「文件不存在」）
    assert read_json(run_dir(workspace, meta["run_id"]) / "quarantine_manifest.json") == []


@pytest.mark.parametrize("bad", ["..", "../..", "../../../victim", "a/b", "a\\b", "/etc", "C:evil", ""])
def test_run_id_cannot_escape_the_state_directory(workspace: Path, bad: str) -> None:
    """`run_id` 是纯外部输入，绝不能靠它把路径拼出状态目录之外。

    回归背景：`run_dir` / `quarantine_root` 原先直接把 `run_id` 拼进路径，
    `purge-quarantine <root> ../../../victim --yes` 的 `shutil.rmtree` 会把目标根
    **之外**的任意目录整棵删掉（已复现：`victim/` 真的没了）；`..` 甚至能删掉整个
    `.dirtidy/`，连全部审计历史一起。校验放在这两个函数自身——调用点有十几处，
    每处各查一遍就是「同一个判定写多份」，漏掉的那处就是缝。
    """
    from dirtidy.cli import main

    assert main(["purge-quarantine", str(workspace), bad, "--yes"]) == 2, (
        f"非法 run_id {bad!r} 应该被拒绝（返回 2），而不是照做"
    )


def test_run_id_escape_is_refused_before_anything_is_deleted(workspace: Path) -> None:
    """被拒绝时**一个字节都不许删**——不能先删了再报错。"""
    from dirtidy.cli import main

    victim = workspace.parent / "victim"
    victim.mkdir()
    (victim / "precious.txt").write_text("keep me", encoding="utf-8")
    # 先让中间目录存在，否则 rmtree 会因路径不存在而「碰巧没删成」
    (workspace / ".dirtidy" / "quarantine").mkdir(parents=True, exist_ok=True)

    assert main(["purge-quarantine", str(workspace), "../../../victim", "--yes"]) == 2
    assert victim.exists(), "目标根之外的目录被删了"
    assert (victim / "precious.txt").exists(), "目标根之外的文件被删了"


def test_parent_directory_timestamps_are_preserved_on_quarantine_and_restore(workspace: Path) -> None:
    """隔离与还原文件时，受影响的各级父目录修改时间（mtime）必须毫发无损地保全。

    背景：POSIX / APFS 内核会在目录内文件被移除或新增时自动将父目录 mtime 刷新为当前系统时间。
    若不予保全，用户运行清理后外层历史业务目录的修改时间会被大面积篡改。
    保全机制确保：
      1. 隔离后，包含被移走文件的子目录及祖先目录 mtime 维持原状；
      2. 还原后，被恢复文件所在的子目录及祖先目录 mtime 维持原状。
    """
    from dirtidy.models import RuleHit
    from dirtidy.quarantine.store import nothing_protected, quarantine_hits, restore_entries

    sub = workspace / "业务历史资料" / "子分类A"
    sub.mkdir(parents=True)
    junk_file = sub / "temp.log"
    junk_file.write_text("temporary log content\n", encoding="utf-8")

    # 预先创建 .dirtidy 结构，避免创建本身修改 workspace 根目录
    q_dir = workspace / ".dirtidy" / "quarantine" / "run_test"
    manifest = workspace / ".dirtidy" / "runs" / "run_test" / "quarantine_manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)

    # 设定历史时间戳（模拟两年前）
    old_time = 1650000000.0
    os.utime(workspace, (old_time, old_time))
    os.utime(workspace / "业务历史资料", (old_time, old_time))
    os.utime(sub, (old_time, old_time))

    hit = RuleHit(
        path="业务历史资料/子分类A/temp.log",
        rule_id="log_suffix",
        action="auto_quarantine",
        reason="测试日志",
    )

    # 1. 执行隔离
    quarantine_hits(workspace, "run_test", [hit], q_dir, manifest, is_protected=nothing_protected)
    assert not junk_file.exists(), "垃圾文件未被移至隔离区"
    assert sub.stat().st_mtime == old_time, f"隔离后子目录 mtime 被改变: {sub.stat().st_mtime} != {old_time}"
    assert (workspace / "业务历史资料").stat().st_mtime == old_time, "隔离后父目录 mtime 被改变"
    assert workspace.stat().st_mtime == old_time, "隔离后根目录 mtime 被改变"

    # 2. 执行还原
    restored = restore_entries(workspace, manifest, restore_all=True)
    assert "业务历史资料/子分类A/temp.log" in restored
    assert junk_file.exists(), "文件未能成功还原"
    assert sub.stat().st_mtime == old_time, f"还原后子目录 mtime 被改变: {sub.stat().st_mtime} != {old_time}"
    assert (workspace / "业务历史资料").stat().st_mtime == old_time, "还原后父目录 mtime 被改变"
    assert workspace.stat().st_mtime == old_time, "还原后根目录 mtime 被改变"

