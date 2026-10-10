"""规则层回归：保护优先级、条件过滤、守卫、规则包校验。

这些用例守护的是「既不误删、也不留无效」这条底线：
- 保护必须压过自动隔离，且与规则书写顺序无关；
- 目录名这类粗糙匹配必须能被 when 条件收窄；
- 隔离阶段必须重新复核保护，防止 pending 列表被改过就绕过去。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from dirtidy.models import InventoryItem, read_json, run_dir
from dirtidy.pipeline import apply_pending_rules, create_run
from dirtidy.rules.engine import RulePackError, load_rule_pack, match_rules, validate_pack
from dirtidy.rules.protect import builtin_protect_rules, compile_protect_rules, evaluate_protect

NOW = 1_800_000_000.0
DAY = 86400.0


def _item(path: str, *, is_dir: bool = False, size: int = 0, age_days: float = 0.0) -> InventoryItem:
    # 不传 suffix：条目不再存它，规则匹配侧用 suffix_of(path) 现算
    return InventoryItem(
        path=path,
        is_dir=is_dir,
        size=size,
        mtime=NOW - age_days * DAY,
    )


def test_suffix_of_matches_path_suffix() -> None:
    """`suffix_of` 必须与 `Path.suffix` 逐例一致。

    它是被去掉的 `suffix` 字段的替代品；一旦行为漂移，规则匹配就会与第一期不一样
    （最典型的是 `.env` 突然「有了后缀」，或者 `a.` 被判成后缀 `.`）。
    """
    from dirtidy.models import suffix_of

    cases = [
        "a.py",
        "a.tar.gz",
        ".env",
        "dir/.env",
        "noext",
        "dir/noext",
        "a.",
        "x.",
        "..",
        ".",
        ".a.b",
        "a..b",
        "x.PY",
        "dir.d/file",
    ]
    for case in cases:
        assert suffix_of(case) == Path(case).suffix.lower(), f"与 Path.suffix 不一致: {case!r}"


def _empty_root(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    return root


# ------------------------------------------------------------ protect 优先级

def test_protect_beats_auto_regardless_of_order(tmp_path: Path) -> None:
    root = _empty_root(tmp_path)
    items = [_item("keepme", is_dir=True), _item("junk", is_dir=True)]
    base = [
        {"id": "auto_all", "action": "auto_quarantine", "match": {"type": "dir_name", "names": ["keepme", "junk"]}},
        {"id": "protect_keepme", "action": "protect", "match": {"type": "dir_name", "names": ["keepme"]}},
    ]
    reversed_pack = list(reversed(base))

    for rules in (base, reversed_pack):
        res = match_rules(root, items, {"rules": rules}, now=NOW)
        assert [h.path for h in res.hits] == ["junk"], "受保护路径不得进入待隔离列表"
        assert [p.path for p in res.protected] == ["keepme"]
        assert res.protected[0].protect_rule_id == "protect_keepme"
        assert res.protected[0].blocked_auto is True
        assert not res.candidates, "受保护路径也不应进入候选"


def test_protect_dir_covers_whole_subtree(tmp_path: Path) -> None:
    root = _empty_root(tmp_path)
    items = [
        _item(".git", is_dir=True),
        _item(".git/objects", is_dir=True),
        _item(".git/objects/ab12", size=10),
        _item("normal", is_dir=True),
    ]
    pack = {"rules": [{"id": "auto_all", "action": "auto_quarantine", "match": {"type": "name_glob", "globs": ["*"]}}]}
    res = match_rules(root, items, pack, now=NOW)

    assert [h.path for h in res.hits] == ["normal"]
    blocked = {p.path for p in res.protected}
    assert blocked == {".git", ".git/objects", ".git/objects/ab12"}
    assert all(p.protect_rule_id == "builtin_protect_vcs" for p in res.protected)


def test_builtin_protection_covers_secrets_and_system_dirs() -> None:
    matchers = compile_protect_rules(builtin_protect_rules())
    for rel, is_dir in [
        ("id_rsa", False),
        ("certs/server.pem", False),
        (".env.production", False),
        (".ssh", True),
        ("lost+found", True),
        ("a/b/.git", True),
    ]:
        assert evaluate_protect(matchers, rel, is_dir) is not None, rel
    assert evaluate_protect(matchers, "notes.txt", False) is None


def test_pack_level_protect_rule_is_honoured(tmp_path: Path) -> None:
    root = _empty_root(tmp_path)
    items = [_item("data/keep.db", size=100)]
    pack = {
        "rules": [
            {"id": "auto_db", "action": "auto_quarantine", "match": {"type": "suffix", "suffixes": [".db"]}},
            {"id": "protect_keep", "action": "protect", "match": {"type": "name_glob", "globs": ["keep.db"]}},
        ]
    }
    res = match_rules(root, items, pack, now=NOW)
    assert res.hits == []
    assert res.protected[0].protect_rule_id == "protect_keep"


# ------------------------------------------------------------ when 条件

def test_min_age_days_blocks_fresh_items(tmp_path: Path) -> None:
    root = _empty_root(tmp_path)
    items = [_item("fresh.log", age_days=0.1), _item("old.log", age_days=30)]
    pack = {
        "rules": [
            {
                "id": "log_old",
                "action": "auto_quarantine",
                "match": {"type": "suffix", "suffixes": [".log"]},
                "when": {"min_age_days": 7},
            }
        ]
    }
    res = match_rules(root, items, pack, now=NOW)
    assert [h.path for h in res.hits] == ["old.log"]


def test_missing_mtime_is_conservative(tmp_path: Path) -> None:
    root = _empty_root(tmp_path)
    it = InventoryItem(path="x.log", is_dir=False, size=1, mtime=0.0)
    pack = {
        "rules": [
            {
                "id": "log_old",
                "action": "auto_quarantine",
                "match": {"type": "suffix", "suffixes": [".log"]},
                "when": {"min_age_days": 1},
            }
        ]
    }
    assert match_rules(root, [it], pack, now=NOW).hits == []


def test_size_and_path_conditions(tmp_path: Path) -> None:
    root = _empty_root(tmp_path)
    items = [_item("a/small.tmp", size=10), _item("a/big.tmp", size=5000), _item("other/mid.tmp", size=100)]
    pack = {
        "rules": [
            {
                "id": "tmp_under_a",
                "action": "auto_quarantine",
                "match": {"type": "suffix", "suffixes": [".tmp"]},
                "when": {"under": ["a"], "min_size_bytes": 1000, "max_size_bytes": 10000},
            }
        ]
    }
    res = match_rules(root, items, pack, now=NOW)
    assert [h.path for h in res.hits] == ["a/big.tmp"]


def test_max_depth_condition(tmp_path: Path) -> None:
    root = _empty_root(tmp_path)
    items = [_item("logs/x.tmp", size=1), _item("a/b/logs/y.tmp", size=1)]
    pack = {
        "rules": [
            {
                "id": "tmp_shallow",
                "action": "auto_quarantine",
                "match": {"type": "suffix", "suffixes": [".tmp"]},
                "when": {"max_depth": 2},
            }
        ]
    }
    assert [h.path for h in match_rules(root, items, pack, now=NOW).hits] == ["logs/x.tmp"]


def test_source_free_guard_protects_code_dirs(tmp_path: Path) -> None:
    root = _empty_root(tmp_path)
    items = [
        _item("logs", is_dir=True),
        _item("logs/app.log", size=10),
        _item("src/logs", is_dir=True),
        _item("src/logs/handler.py", size=10),
    ]
    pack = {
        "rules": [
            {
                "id": "log_dir",
                "action": "auto_quarantine",
                "match": {"type": "dir_name", "names": ["logs"]},
                "when": {"require": ["source_free"]},
            }
        ]
    }
    res = match_rules(root, items, pack, now=NOW)
    assert [h.path for h in res.hits] == ["logs"], "含源码的 logs 目录不得自动隔离"


def test_name_glob_is_dir_filter(tmp_path: Path) -> None:
    root = _empty_root(tmp_path)
    items = [_item("build", is_dir=True), _item("build", size=5)]
    pack = {
        "rules": [
            {
                "id": "build_dir",
                "action": "candidate_only",
                "match": {"type": "name_glob", "is_dir": True, "globs": ["build"]},
            }
        ]
    }
    res = match_rules(root, items, pack, now=NOW)
    assert [c.paths[0] for c in res.candidates] == ["build"]
    assert res.candidates[0].meta["action"] == "candidate_only"


# ------------------------------------------------------------ 规则包校验

_LOG_RULE = {"id": "x", "action": "auto_quarantine", "match": {"type": "suffix", "suffixes": [".log"]}}


@pytest.mark.parametrize(
    "rules",
    [
        [{"id": "x", "action": "delete_everything", "match": {"type": "dir_name", "names": ["a"]}}],
        [{"id": "x", "action": "auto_quarantine", "match": {"type": "regex", "names": ["a"]}}],
        [{"id": "x", "action": "auto_quarantine", "match": {"type": "dir_name"}}],
        [{**_LOG_RULE, "when": {"min_age": 3}}],
        [{**_LOG_RULE, "when": {"require": ["no_such_guard"]}}],
        [
            {"id": "dup", "action": "auto_quarantine", "match": {"type": "empty_file"}},
            {"id": "dup", "action": "auto_quarantine", "match": {"type": "empty_dir"}},
        ],
    ],
)
def test_validate_pack_rejects_bad_rules(rules: list[dict]) -> None:
    with pytest.raises(RulePackError):
        validate_pack({"rules": rules})


def test_shipped_packs_are_valid_and_protect_first_class() -> None:
    for name in ("default", "server_workspace"):
        pack = load_rule_pack(name)
        actions = [r["action"] for r in pack["rules"]]
        assert "protect" in actions
        assert actions.count("protect") >= 5, f"{name} 的保护规则太少，兜底不足"


# ------------------------------------------------------------ apply 阶段复核

def test_apply_pending_rechecks_protection(tmp_path: Path) -> None:
    """pending 列表被人工/旧规则包写成「隔离 id_rsa」时，apply 必须拦住。"""
    root = tmp_path / "ws"
    root.mkdir()
    (root / "id_rsa").write_text("PRIVATE KEY", encoding="utf-8")

    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    (rdir / "pending_auto_hits.json").write_text(
        '[{"path": "id_rsa", "rule_id": "tampered", "action": "auto_quarantine", "reason": "t", "is_dir": false}]',
        encoding="utf-8",
    )

    result = apply_pending_rules(root, meta["run_id"])
    assert result["quarantined"] == 0
    assert result["blocked_by_protect"] == 1
    assert (root / "id_rsa").exists(), "密钥文件不得被搬走"

    protection = read_json(rdir / "protection.json")
    assert protection and protection[0]["protect_rule_id"] == "builtin_protect_secret_files"


# ------------------------------------------------------------ 端到端

def test_cli_protect_path_option_end_to_end(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "keep").mkdir(parents=True)
    (root / "keep" / "notes.log").write_text("keep me\n", encoding="utf-8")
    (root / "other.log").write_text("drop me\n", encoding="utf-8")
    old = time.time() - 30 * DAY
    for p in root.rglob("*.log"):
        import os

        os.utime(p, (old, old))

    meta = create_run(root, rules="default", quick=True, apply=True, protect=["keep"])
    assert (root / "keep" / "notes.log").exists(), "--protect 指定的路径不得被隔离"
    assert not (root / "other.log").exists()

    rdir = run_dir(root, meta["run_id"])
    blocked = {p["path"] for p in read_json(rdir / "protection.json")}
    assert blocked == {"keep/notes.log"}
    assert meta["protect"] == ["keep"]


def test_fresh_log_survives_old_log_goes(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "new.log").write_text("writing...\n", encoding="utf-8")
    (root / "old.log").write_text("done\n", encoding="utf-8")
    import os

    old = time.time() - 10 * DAY
    os.utime(root / "old.log", (old, old))

    create_run(root, rules="default", quick=True, apply=True)
    assert (root / "new.log").exists(), "正在写的日志不得被搬走"
    assert not (root / "old.log").exists()


def test_run_bundle_records_protection_policy(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    (root / "app.log").write_text("x\n", encoding="utf-8")

    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    policy = read_json(rdir / "protect_policy.json")
    assert {p["id"] for p in policy} >= {"builtin_protect_vcs", "builtin_protect_secret_files", "protect_secrets"}
    assert "## Protection" in (rdir / "audit.md").read_text(encoding="utf-8")
