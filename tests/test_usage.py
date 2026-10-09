"""使用层回归：explain 解释、dry-run 预览。

守护的是「用户能自己看懂为什么」与「预览不等于执行」：
explain 的结论必须与真实 match_rules 一致，dry-run 必须一个文件都不动。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from dirtidy.decision.store import apply_decisions, save_decisions
from dirtidy.explain import explain_path
from dirtidy.models import Decision, quarantine_root, read_json, run_dir
from dirtidy.pipeline import apply_pending_rules, create_run
from dirtidy.rules.engine import load_rule_pack

DAY = 86400.0


def _ws(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    root.mkdir()
    return root


# ------------------------------------------------------------------ explain

def test_explain_no_match(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    (root / "notes.txt").write_text("hi", encoding="utf-8")
    exp = explain_path(root, "notes.txt")
    assert exp.exists and exp.decision == "no_match"
    assert exp.traces, "应给出逐条规则判定"
    assert any(t.matched is False for t in exp.traces)


def test_explain_auto_quarantine(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    (root / "node_modules").mkdir()
    exp = explain_path(root, "node_modules")
    assert exp.decision == "auto_quarantine"
    assert exp.rule_id == "dep_node_modules"
    assert exp.is_dir is True
    assert "dep_node_modules" in exp.to_text()


def test_explain_protected_beats_rule(tmp_path: Path) -> None:
    """自定义规则想动 id_rsa，内置保护清单必须压过去，并在解释里说清楚。"""
    root = _ws(tmp_path)
    (root / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
    pack = {
        "rules": [
            {
                "id": "junk_secret",
                "action": "auto_quarantine",
                "match": {"type": "name_glob", "globs": ["id_rsa"]},
                "reason": "测试用",
            }
        ]
    }
    exp = explain_path(root, "id_rsa", pack)
    assert exp.decision == "protected"
    assert exp.protect_rule_id == "builtin_protect_secret_files"
    assert exp.rule_id == "junk_secret"
    text = exp.to_text()
    assert "保护规则" in text and "junk_secret" in text


def test_explain_shows_when_condition_details(tmp_path: Path) -> None:
    """新鲜文件不满足 min_age_days：解释里要能看到「实际 0.0 天」这样的明细。"""
    root = _ws(tmp_path)
    (root / "fresh.log").write_text("x", encoding="utf-8")
    exp = explain_path(root, "fresh.log")
    assert exp.decision == "no_match"
    log_trace = next(t for t in exp.traces if t.rule_id == "log_suffix")
    assert log_trace.matched is True
    assert log_trace.conditions_passed is False
    assert any("min_age_days" in c and "不满足" in c for c in log_trace.conditions)


def test_explain_source_free_guard_visible(tmp_path: Path) -> None:
    import os

    root = _ws(tmp_path)
    logs = root / "logs"
    logs.mkdir()
    (logs / "handler.py").write_text("x = 1\n", encoding="utf-8")
    old = time.time() - 30 * DAY
    # 目录与文件都要回拨：min_age_days 看的是目录自身的 mtime，
    # 不一起回拨就会先卡在 min_age_days 上，看不到 source_free 那条判定
    os.utime(logs, (old, old))
    os.utime(logs / "handler.py", (old, old))

    exp = explain_path(root, "logs", rules="server_workspace")
    assert exp.decision == "no_match", "含源码的 logs 目录不应被自动隔离"
    trace = next(t for t in exp.traces if t.rule_id == "log_dir")
    assert trace.matched is True and trace.conditions_passed is False
    assert any("source_free" in c and "不满足" in c for c in trace.conditions)


def test_explain_old_log_is_auto(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    p = root / "old.log"
    p.write_text("x", encoding="utf-8")
    import os

    old = time.time() - 10 * DAY
    os.utime(p, (old, old))
    exp = explain_path(root, "old.log")
    assert exp.decision == "auto_quarantine"
    assert exp.rule_id == "log_suffix"


def test_explain_extra_protect_makes_it_protected(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    (root / "keep").mkdir()
    (root / "keep" / "a.log").write_text("x", encoding="utf-8")
    extra = [
        {
            "id": "cli_protect_0",
            "action": "protect",
            "match": {"type": "path_glob", "patterns": ["keep", "keep/**"]},
            "reason": "命令行 --protect 指定",
        }
    ]
    exp = explain_path(root, "keep/a.log", load_rule_pack("default"), extra_protect_rules=extra)
    # 没有规则想动它（日志太新），但保护信息必须如实呈现，
    # 否则用户会误以为「没命中 = 可以随便动」
    assert exp.decision == "no_match"
    assert exp.protected is True
    assert exp.protect_rule_id == "cli_protect_0"
    text = exp.to_text()
    assert "受保护" in text and "cli_protect_0" in text


def test_explain_missing_path(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    exp = explain_path(root, "nope/nothing.txt")
    assert exp.exists is False and exp.decision == "missing"


def test_explain_rejects_escaping_path(tmp_path: Path) -> None:
    """解释功能不得成为绕过路径不变量的缝。"""
    root = _ws(tmp_path)
    with pytest.raises(ValueError):
        explain_path(root, "../outside/secret.txt")
    with pytest.raises(ValueError):
        explain_path(root, "/etc/passwd")


def test_explain_json_is_serializable(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    (root / "node_modules").mkdir()
    payload = explain_path(root, "node_modules").to_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False))["decision"] == "auto_quarantine"
    assert payload["decision_label"]


def test_explain_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from dirtidy.cli import main

    root = _ws(tmp_path)
    (root / "node_modules").mkdir()
    assert main(["explain", str(root), "node_modules"]) == 0
    assert "dep_node_modules" in capsys.readouterr().out

    assert main(["explain", str(root), "missing.txt"]) == 1
    assert main(["explain", str(root), "../escape"]) == 2


def test_explain_lists_builtin_protect_rules(tmp_path: Path) -> None:
    """内置保护清单必须在逐条列表里出现。

    曾经的缺陷：结论区说「builtin_protect_credentials 保护了它」，但逐条列表只列了
    规则包的规则，用户照着列表去找根本找不到这条 —— 「谁保护的」查不到责任人。
    """
    root = _ws(tmp_path)
    (root / ".ssh").mkdir()
    (root / ".ssh" / ".cache").write_text("x", encoding="utf-8")

    exp = explain_path(root, ".ssh/.cache")
    assert exp.protect_rule_id == "builtin_protect_credentials"

    builtin = [t for t in exp.traces if t.source == "builtin"]
    assert builtin, "内置保护规则应出现在 traces 里"
    hit = next(t for t in builtin if t.rule_id == "builtin_protect_credentials")
    assert hit.matched is True, "保护了它的那条内置规则必须标记为命中"
    assert "内置保护清单命中" in hit.note

    text = exp.to_text()
    assert "builtin_protect_credentials" in text
    assert "内置保护" in text


def test_explain_traces_cover_every_effective_protect_rule(tmp_path: Path) -> None:
    """逐条列表里的保护规则条数必须等于生效保护策略的条数。

    否则列表与「生效的保护规则 N 条」这句自相矛盾，用户无法据此确认清单是否完整。
    """
    root = _ws(tmp_path)
    (root / "plain.txt").write_text("x", encoding="utf-8")

    extra = [
        {
            "id": "cli_protect_0",
            "action": "protect",
            "match": {"type": "path_glob", "patterns": ["whatever"]},
            "reason": "命令行 --protect 指定",
        }
    ]
    exp = explain_path(root, "plain.txt", load_rule_pack("default"), extra_protect_rules=extra)

    protect_traces = [t for t in exp.traces if t.action == "protect"]
    assert len(protect_traces) == len(exp.protect_policy)
    assert {t.rule_id for t in protect_traces} == {p["id"] for p in exp.protect_policy}
    assert any(t.source == "extra" for t in protect_traces), "额外指定的保护也要进列表"

    # 内置清单排在最前：它们是最高优先级，且与规则书写顺序无关
    assert exp.traces[0].source == "builtin"


# ------------------------------------------------------------------ dry-run

def test_apply_rules_dry_run_does_not_move(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    (root / "node_modules").mkdir()
    (root / "node_modules" / "index.js").write_text("x", encoding="utf-8")

    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])

    result = apply_pending_rules(root, meta["run_id"], dry_run=True)
    assert result["dry_run"] is True
    assert result["would_quarantine"] >= 1
    assert "node_modules" in result["paths"]
    assert (root / "node_modules").exists(), "dry-run 不得搬动任何文件"
    assert (rdir / "pending_auto_hits.json").exists(), "dry-run 不得消费 pending 列表"
    assert read_json(rdir / "quarantine_manifest.json") == []

    # 真跑一遍：这次才动
    real = apply_pending_rules(root, meta["run_id"])
    assert real["quarantined"] >= 1
    assert not (root / "node_modules").exists()


def test_apply_rules_dry_run_reports_protection_block(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    (root / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")
    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    (rdir / "pending_auto_hits.json").write_text(
        '[{"path": "id_rsa", "rule_id": "tampered", "action": "auto_quarantine", "reason": "t", "is_dir": false}]',
        encoding="utf-8",
    )
    result = apply_pending_rules(root, meta["run_id"], dry_run=True)
    assert result["would_quarantine"] == 0
    assert result["blocked_by_protect"] == 1
    assert result["blocked_paths"] == ["id_rsa"]
    assert (root / "id_rsa").exists()


def test_apply_decisions_dry_run_detects_duplicate_archive_target(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    (root / "a.txt").write_text("A", encoding="utf-8")
    (root / "b.txt").write_text("B", encoding="utf-8")

    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    save_decisions(
        rdir,
        [
            Decision(path="a.txt", action="archive", target_relpath="archive/same.txt"),
            Decision(path="b.txt", action="archive", target_relpath="archive/same.txt"),
        ],
    )
    res = apply_decisions(root, meta["run_id"], rdir, quarantine_root(root, meta["run_id"]), dry_run=True)

    assert res["dry_run"] is True
    assert res["archived"] == 1
    assert len(res["skipped"]) == 1
    assert "已存在" in res["skipped"][0]["reason"]
    assert "a.txt" in res["skipped"][0]["reason"], "冲突原因里应指出是谁先占了目标"
    assert (root / "a.txt").exists() and (root / "b.txt").exists()
    assert not (root / "archive").exists(), "dry-run 不得创建归档目录"


def test_scan_dry_run_conflicts_with_apply(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from dirtidy.cli import main

    root = _ws(tmp_path)
    (root / "node_modules").mkdir()
    assert main(["scan", str(root), "--dry-run", "--apply"]) == 2
    assert "互斥" in capsys.readouterr().out
    assert not (root / ".dirtidy").exists(), "参数冲突时不应产生任何 run 产物"


def test_scan_dry_run_still_writes_preview(tmp_path: Path) -> None:
    root = _ws(tmp_path)
    (root / "node_modules").mkdir()
    meta = create_run(root, rules="default", quick=True, apply=False)
    assert meta["applied"] is False
    assert (root / "node_modules").exists()
