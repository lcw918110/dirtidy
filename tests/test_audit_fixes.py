"""2026-10-09 审核修复的回归用例。

覆盖安全与成本边界：路径穿越、归档目标越界、凭据摘要泄漏、
候选为空时的外发目标选择、只读根扫描、决策跳过明细。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from dirtidy.ai.providers import build_summary
from dirtidy.decision.store import apply_decisions, save_decisions
from dirtidy.models import Decision, RuleHit, quarantine_root, read_json, resolve_under, run_dir
from dirtidy.pipeline import apply_pending_rules, create_run
from dirtidy.quarantine.store import drop_covered_descendants, restore_entries


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


# -------------------------------------------- 目录命中不得带走受保护后代
#
# 这是比「决策路径不查保护」更隐蔽的一条缝：保护判定只看命中路径**本身**，
# 于是命中一个目录就等于把它内部所有受保护文件一起搬走。
# 零配置即可触发——`node_modules` 命中 dep 规则，`node_modules/.env`、`node_modules/id_rsa`
# 本该由内置清单兜底，却会被连锅端走，且 blocked_by_protect 报 0，毫无痕迹。


def _seed_dep_dir_with_secrets(root: Path) -> None:
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (root / "node_modules" / ".env").write_text("SECRET=1", encoding="utf-8")
    (root / "node_modules" / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")


def test_dir_hit_blocked_when_subtree_holds_protected_file(tmp_path: Path) -> None:
    """命中目录的子树里若有受保护文件，整条命中必须被拦下——零配置（无 --protect）也要拦。"""
    root, _ = _ws(tmp_path)
    _seed_dep_dir_with_secrets(root)

    meta = create_run(root, rules="default", quick=True, apply=False)
    res = apply_pending_rules(root, meta["run_id"])

    assert res["quarantined"] == 0, "目录里有密钥文件时，整个目录都不该被搬"
    assert res["blocked_by_protect"] == 1
    assert (root / "node_modules" / ".env").exists()
    assert (root / "node_modules" / "id_rsa").exists()
    assert (root / "node_modules" / "pkg" / "index.js").exists()

    rdir = run_dir(root, meta["run_id"])
    records = read_json(rdir / "protection.json")
    rec = next(r for r in records if r["path"] == "node_modules")
    assert rec["protect_rule_id"] == "builtin_protect_secret_files"
    assert rec["blocked_at"] == "protected_descendant"
    assert "子树内含受保护路径" in rec["protect_reason"]


def test_dir_hit_without_protected_inside_is_still_quarantined(tmp_path: Path) -> None:
    """反向守卫：不能因为「目录可能含受保护内容」就一律不搬，否则清理能力归零。"""
    root, _ = _ws(tmp_path)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")

    meta = create_run(root, rules="default", quick=True, apply=False)
    res = apply_pending_rules(root, meta["run_id"])

    assert res["quarantined"] == 1
    assert res["blocked_by_protect"] == 0
    assert not (root / "node_modules").exists()


def test_dir_hit_not_blocked_by_protected_sibling_outside_it(tmp_path: Path) -> None:
    """受保护文件在命中目录**之外**时不得牵连——保护只覆盖自己的子树。"""
    root, _ = _ws(tmp_path)
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "node_modules" / "pkg" / "index.js").write_text("x", encoding="utf-8")
    (root / ".env").write_text("SECRET=1", encoding="utf-8")

    meta = create_run(root, rules="default", quick=True, apply=False)
    res = apply_pending_rules(root, meta["run_id"])

    assert res["quarantined"] == 1
    assert not (root / "node_modules").exists()
    assert (root / ".env").exists(), "根目录的 .env 与 node_modules 无关，应原地不动"


def test_dir_hit_dry_run_matches_real_run(tmp_path: Path) -> None:
    """预览与实跑必须同源：dry-run 说会搬几条，实跑就得搬几条。"""
    root, _ = _ws(tmp_path)
    _seed_dep_dir_with_secrets(root)

    meta = create_run(root, rules="default", quick=True, apply=False)
    preview = apply_pending_rules(root, meta["run_id"], dry_run=True)
    assert preview["would_quarantine"] == 0
    assert preview["blocked_by_protect"] == 1
    assert preview["blocked_paths"] == ["node_modules"]

    real = apply_pending_rules(root, meta["run_id"])
    assert real["quarantined"] == preview["would_quarantine"]
    assert real["blocked_by_protect"] == preview["blocked_by_protect"]


def test_apply_decisions_blocks_dir_holding_protected_descendant(tmp_path: Path) -> None:
    """人工决策把整个目录标 quarantine 时，同样不能把内部受保护文件带走。"""
    root, _ = _ws(tmp_path)
    _seed_dep_dir_with_secrets(root)

    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    save_decisions(rdir, [Decision(path="node_modules", action="quarantine", note="web")])
    res = apply_decisions(root, meta["run_id"], rdir, quarantine_root(root, meta["run_id"]))

    assert res["blocked_by_protect"] == 1
    assert res["quarantined"] == 0
    assert (root / "node_modules" / ".env").exists()
    assert (root / "node_modules" / "id_rsa").exists()
    assert any("子树内含受保护路径" in s["reason"] for s in res["skipped"])


def test_apply_decisions_dry_run_matches_real_for_dir_with_secrets(tmp_path: Path) -> None:
    root, _ = _ws(tmp_path)
    _seed_dep_dir_with_secrets(root)

    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    save_decisions(rdir, [Decision(path="node_modules", action="quarantine", note="web")])

    preview = apply_decisions(
        root, meta["run_id"], rdir, quarantine_root(root, meta["run_id"]), dry_run=True
    )
    assert preview["would_quarantine"] == []
    assert preview["blocked_paths"] == ["node_modules"]

    real = apply_decisions(root, meta["run_id"], rdir, quarantine_root(root, meta["run_id"]))
    assert real["quarantined"] == len(preview["would_quarantine"])
    assert real["blocked_by_protect"] == preview["blocked_by_protect"]


def test_unreadable_subtree_blocks_dir_hit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """子树读不动时无法证明「里面没有受保护内容」——保护优先，宁可不动。"""
    from dirtidy.quarantine import store as qstore

    root, _ = _ws(tmp_path)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "index.js").write_text("x", encoding="utf-8")

    meta = create_run(root, rules="default", quick=True, apply=False)

    def fake_walk(base, onerror=None, **kw):
        if onerror is not None:
            onerror(OSError(13, "Permission denied"))
        return iter(())

    # 只在 apply 阶段替换：qstore.os 就是 os 模块本身，全局替换会连扫描一起打掉
    with monkeypatch.context() as m:
        m.setattr(qstore.os, "walk", fake_walk)
        res = apply_pending_rules(root, meta["run_id"])

    assert res["quarantined"] == 0
    assert (root / "node_modules").exists()
    records = read_json(run_dir(root, meta["run_id"]) / "protection.json")
    assert any(r.get("protect_rule_id") == "__unreadable__" for r in records)


# -------------------------------------------- 目录与其内文件重复命中
#
# `src/__pycache__` 与 `src/__pycache__/main.cpython-311.pyc` 会同时命中规则。
# 若把父子都当独立条目搬：清单多出一条**永远还原不回去**的记录（还原时目标已被
# 父目录的还原创建出来，`restore_entries` 恒跳过），隔离区留下死空目录，
# dry-run 条数也与真实产物对不上。


def _seed_nested_pycache(root: Path) -> None:
    (root / "src" / "__pycache__").mkdir(parents=True)
    (root / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    (root / "src" / "__pycache__" / "main.cpython-311.pyc").write_text("BYTECODE", encoding="utf-8")


def test_nested_hits_deduped_to_parent_dir(tmp_path: Path) -> None:
    root, _ = _ws(tmp_path)
    _seed_nested_pycache(root)

    meta = create_run(root, rules="default", quick=True, apply=False)
    assert meta["auto_hits"] == 2, "目录与其中的 .pyc 应各自命中一条规则"

    preview = apply_pending_rules(root, meta["run_id"], dry_run=True)
    assert preview["would_quarantine"] == 1
    assert preview["paths"] == ["src/__pycache__"]

    real = apply_pending_rules(root, meta["run_id"])
    assert real["quarantined"] == 1

    manifest = read_json(run_dir(root, meta["run_id"]) / "quarantine_manifest.json")
    assert [e["original_path"] for e in manifest] == ["src/__pycache__"]
    assert not (root / "src" / "__pycache__").exists()


def test_nested_hit_round_trip_drains_manifest(tmp_path: Path) -> None:
    """去重后的清单必须能**彻底**还原干净——这正是原来卡死的地方。"""
    root, _ = _ws(tmp_path)
    _seed_nested_pycache(root)

    meta = create_run(root, rules="default", quick=True, apply=False)
    apply_pending_rules(root, meta["run_id"])
    manifest_path = run_dir(root, meta["run_id"]) / "quarantine_manifest.json"

    restored = restore_entries(root, manifest_path, restore_all=True)
    assert restored == ["src/__pycache__"]
    assert read_json(manifest_path) == [], "清单必须清空，不能留下永远还原不回去的记录"
    assert (root / "src" / "__pycache__" / "main.cpython-311.pyc").read_text(
        encoding="utf-8"
    ) == "BYTECODE"


def test_drop_covered_descendants_keeps_unrelated(tmp_path: Path) -> None:
    hits = [
        RuleHit(path="a/b", rule_id="r", action="auto_quarantine", reason="t", is_dir=True),
        RuleHit(path="a/b/c.txt", rule_id="r", action="auto_quarantine", reason="t", is_dir=False),
        RuleHit(path="a/bx", rule_id="r", action="auto_quarantine", reason="t", is_dir=False),
        RuleHit(path="z.txt", rule_id="r", action="auto_quarantine", reason="t", is_dir=False),
    ]
    kept = [h.path for h in drop_covered_descendants(hits)]
    # a/bx 只是前缀相同、并非 a/b 的后代，不能误删
    assert kept == ["a/b", "a/bx", "z.txt"]
