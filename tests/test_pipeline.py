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
