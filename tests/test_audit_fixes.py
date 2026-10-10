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


# ---------------------------------------------------------------- 保护清单不可绕过

def test_apply_decisions_blocks_quarantine_of_protected(tmp_path: Path) -> None:
    """人工决策隔离也不能绕过保护清单。

    曾经 `apply-rules` 有保护复核而 `apply-decisions` 没有，后者就成了绕过保护的一条缝：
    在网页上把 id_rsa 标成 quarantine 就能把它搬进隔离区。两条路径必须同一套语义。
    """
    root, _ = _ws(tmp_path)
    (root / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
    (root / "junk.log").write_text("x", encoding="utf-8")

    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    save_decisions(
        rdir,
        [
            Decision(path="id_rsa", action="quarantine", note="web"),
            Decision(path="junk.log", action="quarantine", note="web"),
        ],
    )
    res = apply_decisions(root, meta["run_id"], rdir, quarantine_root(root, meta["run_id"]))

    assert res["blocked_by_protect"] == 1
    assert res["quarantined"] == 1
    assert (root / "id_rsa").exists(), "受保护文件不得被决策隔离"
    assert not (root / "junk.log").exists()
    assert any("受保护" in s["reason"] for s in res["skipped"])

    records = read_json(rdir / "protection.json")
    assert any(
        r["path"] == "id_rsa" and r.get("blocked_at") == "apply_decisions" for r in records
    ), "被决策路径拦下的也应登记，便于审计追溯"


def test_apply_decisions_blocks_quarantine_of_protected_dir_subtree(tmp_path: Path) -> None:
    """目录命中保护时，其子树也不能被决策隔离（与规则路径同语义）。"""
    root, _ = _ws(tmp_path)
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "objects" / "ab12").write_text("blob", encoding="utf-8")

    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    save_decisions(
        rdir, [Decision(path=".git/objects/ab12", action="quarantine", note="web")]
    )
    res = apply_decisions(root, meta["run_id"], rdir, quarantine_root(root, meta["run_id"]))

    assert res["blocked_by_protect"] == 1
    assert (root / ".git" / "objects" / "ab12").exists()


def test_apply_decisions_dry_run_reports_protected_block(tmp_path: Path) -> None:
    root, _ = _ws(tmp_path)
    (root / ".env").write_text("SECRET=1", encoding="utf-8")
    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    save_decisions(rdir, [Decision(path=".env", action="quarantine", note="t")])

    res = apply_decisions(
        root, meta["run_id"], rdir, quarantine_root(root, meta["run_id"]), dry_run=True
    )
    assert res["blocked_by_protect"] == 1
    assert res["blocked_paths"] == [".env"]
    assert res["would_quarantine"] == []
    assert (root / ".env").exists()


def test_apply_decisions_archive_still_allowed_for_protected(tmp_path: Path) -> None:
    """`archive` 不拦：它是显式指定目标的归类，文件仍在根内可找回，与「清理」语义不冲突。"""
    root, _ = _ws(tmp_path)
    (root / "README.md").write_text("docs", encoding="utf-8")
    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    save_decisions(
        rdir,
        [Decision(path="README.md", action="archive", target_relpath="docs/README.md", note="t")],
    )
    res = apply_decisions(root, meta["run_id"], rdir, quarantine_root(root, meta["run_id"]))

    assert res["archived"] == 1
    assert res["blocked_by_protect"] == 0
    assert (root / "docs" / "README.md").exists()


def test_empty_files_are_not_duplicate_group(tmp_path: Path) -> None:
    """0 字节文件的 hash 天然相同，不该凑成「重复组」诱导用户误点「其余标记隔离」。"""
    root, _ = _ws(tmp_path)
    (root / ".env").touch()
    (root / "id_rsa").touch()
    (root / "a.txt").write_text("same", encoding="utf-8")
    (root / "b.txt").write_text("same", encoding="utf-8")

    meta = create_run(root, rules="default", quick=False, apply=False)
    rdir = run_dir(root, meta["run_id"])
    candidates = read_json(rdir / "candidates.json")
    dup_groups = [c for c in candidates if c["kind"] == "duplicate_group"]

    assert len(dup_groups) == 1, "应只有 a.txt/b.txt 这一组真重复"
    assert sorted(dup_groups[0]["paths"]) == ["a.txt", "b.txt"]
    assert ".env" not in dup_groups[0]["paths"]
    assert "id_rsa" not in dup_groups[0]["paths"]
