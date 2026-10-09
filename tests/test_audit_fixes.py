"""2026-10-09 审核修复的回归用例。

覆盖安全与成本边界：路径穿越、归档目标越界、凭据摘要泄漏、
候选为空时的外发目标选择、只读根扫描、决策跳过明细。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dirtidy.ai.providers import build_summary
from dirtidy.decision.store import apply_decisions, save_decisions
from dirtidy.models import Decision, quarantine_root, read_json, resolve_under, run_dir
from dirtidy.pipeline import create_run


def _ws(tmp_path: Path) -> tuple[Path, Path]:
    """返回 (root, outside)，outside 位于 root 之外，用于验证越界被拒。"""
    root = tmp_path / "ws"
    outside = tmp_path / "outside"
    root.mkdir(parents=True)
    outside.mkdir(parents=True)
    return root, outside


# ---------------------------------------------------------------- 路径穿越

def test_resolve_under_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    with pytest.raises(ValueError):
        resolve_under(root, "../outside/x.txt")
    with pytest.raises(ValueError):
        resolve_under(root, "/etc/passwd")
    with pytest.raises(ValueError):
        resolve_under(root, "")
    # 正常相对路径仍可用
    assert resolve_under(root, "a/b.txt") == root / "a" / "b.txt"


def test_decision_path_traversal_blocked(tmp_path: Path) -> None:
    root, outside = _ws(tmp_path)
    victim = outside / "victim.txt"
    victim.write_text("SECRET", encoding="utf-8")
    (root / "a.txt").write_text("hello", encoding="utf-8")

    meta = create_run(root, quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    save_decisions(rdir, [Decision(path="../outside/victim.txt", action="quarantine", note="t")])
    res = apply_decisions(root, meta["run_id"], rdir, quarantine_root(root, meta["run_id"]))

    assert res["quarantined"] == 0
    assert res["skipped"] and "越界" in res["skipped"][0]["reason"]
    assert victim.exists(), "根外文件不得被搬动"


def test_archive_target_traversal_blocked(tmp_path: Path) -> None:
    root, outside = _ws(tmp_path)
    (root / "a.txt").write_text("hello", encoding="utf-8")

    meta = create_run(root, quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    save_decisions(
        rdir,
        [Decision(path="a.txt", action="archive", target_relpath="../outside/escaped.txt")],
    )
    res = apply_decisions(root, meta["run_id"], rdir, quarantine_root(root, meta["run_id"]))

    assert res["archived"] == 0
    assert res["skipped"]
    assert not (outside / "escaped.txt").exists(), "归档目标不得越出根"
    assert (root / "a.txt").exists(), "源文件应留在原地"


def test_apply_decisions_reports_skipped_when_target_exists(tmp_path: Path) -> None:
    root, _ = _ws(tmp_path)
    (root / "a.txt").write_text("hello", encoding="utf-8")
    (root / "archive").mkdir()
    (root / "archive" / "a.txt").write_text("occupied", encoding="utf-8")

    meta = create_run(root, quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    save_decisions(
        rdir,
        [Decision(path="a.txt", action="archive", target_relpath="archive/a.txt")],
    )
    res = apply_decisions(root, meta["run_id"], rdir, quarantine_root(root, meta["run_id"]))

    assert res["archived"] == 0
    assert res["skipped"] and "已存在" in res["skipped"][0]["reason"]


# ---------------------------------------------------------------- 外发边界

def test_analyze_targets_limited_when_no_candidates(tmp_path: Path) -> None:
    """候选为空集时，深层非候选文件也不得外发（原实现会被短路放行）。"""
    from dirtidy.ai.providers import analyze_run

    root = tmp_path / "ws"
    deep = root / "a" / "b" / "c" / "d" / "e"
    deep.mkdir(parents=True)
    for i in range(4):
        (deep / f"f{i}.txt").write_text(f"unique-{i}", encoding="utf-8")
    (root / "top.txt").write_text("top-unique", encoding="utf-8")

    meta = create_run(root, quick=False, apply=False)
    rdir = run_dir(root, meta["run_id"])
    assert read_json(rdir / "candidates.json") == [], "本场景应无候选"

    results = analyze_run(root, rdir, mock=True)
    paths = [r["path"] for r in results]
    assert paths == ["top.txt"], f"深层非候选文件不应外发，实际: {paths}"


def test_summary_skips_sensitive_filenames(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "credentials.json").write_text(
        '{"aws_access_key_id":"AKIAXXXX","aws_secret_access_key":"leak"}', encoding="utf-8"
    )
    (root / "my_secret.txt").write_text("token=abcdef", encoding="utf-8")
    (root / "config.yaml").write_text("db:\n  password: hunter2\n", encoding="utf-8")

    # 敏感文件名：完全不抽样
    assert build_summary(root, "credentials.json")["text_sample"] is None
    assert build_summary(root, "my_secret.txt")["text_sample"] is None

    # 普通文件名但内容含密钥：抽样必须把「值」抹掉，不能只截断
    sample = build_summary(root, "config.yaml")["text_sample"]
    assert sample is not None
    assert "hunter2" not in sample
    assert "***" in sample


# ---------------------------------------------------------------- 只读根 / 状态目录

def test_state_dir_override_relocates_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """设置 DIRTIDY_STATE_DIR 后，状态目录落在外部，目标根不被污染。"""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "f.txt").write_text("data", encoding="utf-8")
    monkeypatch.setenv("DIRTIDY_STATE_DIR", str(tmp_path / "state"))

    meta = create_run(root, quick=False, apply=False)
    assert meta["run_id"]
    assert not (root / ".dirtidy").exists(), "目标根下不应产生状态目录"
    assert (tmp_path / "state").exists()


def test_state_dir_creation_failure_gives_actionable_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """状态目录无法创建时（只读根场景），报错须给出可操作提示而非裸异常。"""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "f.txt").write_text("data", encoding="utf-8")

    real_mkdir = Path.mkdir

    def fake_mkdir(self: Path, *args: object, **kwargs: object) -> None:
        if ".dirtidy" in str(self):
            raise PermissionError(13, "Permission denied", str(self))
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fake_mkdir)
    with pytest.raises(PermissionError) as ei:
        create_run(root, quick=True, apply=False)
    assert "DIRTIDY_STATE_DIR" in str(ei.value)
