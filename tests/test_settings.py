"""状态目录设置（直删规则 / 模型配置）与「隔离后立即物理删除」的执行语义。

这组用例守的是两条底线：

1. **直删不是绕过隔离的缝**：命中项仍先过 `keep` 与保护复核、搬入隔离区、写清单，
   然后才物理删除。任何一步被跳过，这里应该红。
2. **密钥不回显**：模型配置状态接口只说「有没有填过」，不返回密钥本身。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from dirtidy.models import read_json, run_dir
from dirtidy.pipeline import apply_pending_rules, create_run
from dirtidy.quarantine.store import purge_entries
from dirtidy.settings import (
    SettingsError,
    load_auto_purge_rules,
    load_model_settings,
    load_use_model,
    model_settings_status,
    normalize_model_settings,
    save_auto_purge_rules,
    save_model_settings,
    save_use_model,
)

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "sample_workspace"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    dest = tmp_path / "ws"
    shutil.copytree(FIXTURE_SRC, dest)
    return dest


# ------------------------------- 设置读写 -------------------------------


def test_auto_purge_defaults_to_empty(workspace: Path) -> None:
    """默认一条直删规则都不开——这是「自动只到隔离区为止」的默认保证。"""
    assert load_auto_purge_rules(workspace) == []


def test_auto_purge_roundtrip_sorts_and_dedupes(workspace: Path) -> None:
    saved = save_auto_purge_rules(workspace, ["b_rule", "a_rule", "b_rule", "  "])
    assert saved == ["a_rule", "b_rule"]
    assert load_auto_purge_rules(workspace) == ["a_rule", "b_rule"]


def test_use_model_defaults_to_false(workspace: Path) -> None:
    """不配置就不外发任何内容：模型分析默认关闭。"""
    assert load_use_model(workspace) is False
    assert save_use_model(workspace, True) is True
    assert load_use_model(workspace) is True


def test_settings_file_corrupt_falls_back_to_defaults(workspace: Path) -> None:
    """设置文件被手改坏时退回保守默认，而不是让整个界面 500。"""
    from dirtidy.models import state_root

    state_root(workspace).mkdir(parents=True, exist_ok=True)
    (state_root(workspace) / "settings.json").write_text("{ 这不是 JSON", encoding="utf-8")
    assert load_auto_purge_rules(workspace) == []
    assert load_use_model(workspace) is False


# ------------------------------- 模型配置 -------------------------------


def test_model_settings_roundtrip(workspace: Path) -> None:
    save_model_settings(
        workspace,
        {
            "provider": "openai_compatible",
            "openai_compatible": {
                "base_url": "https://example.test/v1",
                "api_key_env": "MY_KEY",
                "model": "demo",
            },
        },
    )
    data = load_model_settings(workspace)
    assert data is not None
    assert data["provider"] == "openai_compatible"
    assert data["openai_compatible"]["model"] == "demo"
    # 未提供的 summary 项用默认值补齐，避免下游读不到键
    assert data["summary"]["max_targets"] == 500


def test_model_settings_status_never_echoes_key(workspace: Path) -> None:
    """状态接口只说「填没填过」，绝不回显密钥本身。"""
    save_model_settings(
        workspace,
        {
            "provider": "openai_compatible",
            "openai_compatible": {"api_key": "sk-super-secret", "model": "demo"},
        },
    )
    status = model_settings_status(workspace)
    assert status["configured"] is True
    assert status["api_key_inline"] is True
    assert "sk-super-secret" not in str(status)


def test_model_settings_status_reports_env_presence(workspace, monkeypatch) -> None:
    monkeypatch.delenv("DIRTIDY_TEST_KEY", raising=False)
    save_model_settings(
        workspace,
        {"provider": "openai_compatible", "openai_compatible": {"api_key_env": "DIRTIDY_TEST_KEY"}},
    )
    assert model_settings_status(workspace)["api_key_env_set"] is False
    monkeypatch.setenv("DIRTIDY_TEST_KEY", "x")
    assert model_settings_status(workspace)["api_key_env_set"] is True


def test_normalize_rejects_unknown_provider() -> None:
    with pytest.raises(SettingsError):
        normalize_model_settings({"provider": "mock"})


def test_normalize_rejects_bad_base_url() -> None:
    with pytest.raises(SettingsError):
        normalize_model_settings(
            {"provider": "openai_compatible", "openai_compatible": {"base_url": "ftp://x"}}
        )


def test_empty_api_key_is_not_written(workspace: Path) -> None:
    """空密钥不落盘：写出一个空 `api_key` 会让 provider 误判成「已配置」。"""
    save_model_settings(
        workspace,
        {"provider": "openai_compatible", "openai_compatible": {"api_key": "   "}},
    )
    data = load_model_settings(workspace) or {}
    assert "api_key" not in (data.get("openai_compatible") or {})


def test_model_settings_are_per_target_root(tmp_path: Path) -> None:
    """配置落在目标根的状态目录下，两个根互不干扰。"""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    save_use_model(a, True)
    assert load_use_model(a) is True
    assert load_use_model(b) is False


# --------------------------- 直删：执行语义 ---------------------------


def test_purge_rule_deletes_after_quarantine_and_leaves_audit(workspace: Path) -> None:
    """命中直删规则：清单里不再有它、磁盘上也没有、purged_directly.json 留痕。"""
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    result = apply_pending_rules(
        workspace, meta["run_id"], purge_rule_ids=["dep_python_cache"]
    )
    assert result["purged_directly"] == 1
    assert result["purged_paths"] == ["__pycache__"]

    rdir = run_dir(workspace, meta["run_id"])
    remaining = [e["original_path"] for e in read_json(rdir / "quarantine_manifest.json")]
    assert "__pycache__" not in remaining
    # 其它规则命中项仍按老样子留在隔离区里
    assert "node_modules" in remaining

    audit = read_json(rdir / "purged_directly.json")
    assert [a["original_path"] for a in audit] == ["__pycache__"]
    assert audit[0]["rule_id"] == "dep_python_cache"

    # 隔离区实体也真的没了
    qfiles = [
        p.as_posix()
        for p in (workspace / ".dirtidy" / "quarantine").rglob("*")
        if p.is_file()
    ]
    assert not any("__pycache__" in f for f in qfiles), qfiles
    assert not (workspace / "__pycache__").exists()


def test_purge_rule_only_touches_listed_rules(workspace: Path) -> None:
    """没被列进直删清单的规则，行为一点不变——仍然只隔离。"""
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    result = apply_pending_rules(
        workspace, meta["run_id"], purge_rule_ids=["dep_os_metadata"]
    )
    assert result["purged_directly"] == 0
    rdir = run_dir(workspace, meta["run_id"])
    remaining = {e["original_path"] for e in read_json(rdir / "quarantine_manifest.json")}
    assert "__pycache__" in remaining


def test_purge_rule_does_not_bypass_keep(workspace: Path) -> None:
    """人工保留优先于直删：勾了「保留」的项本轮不动，也不该被删掉。"""
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    result = apply_pending_rules(
        workspace,
        meta["run_id"],
        keep=["node_modules"],
        purge_rule_ids=["dep_node_modules"],
    )
    assert result["kept_by_user"] == 1
    assert result["purged_directly"] == 0
    assert (workspace / "node_modules" / "pkg" / "index.js").exists()


def test_keep_directory_also_keeps_its_hit_descendants(workspace: Path) -> None:
    """保留一个目录时，其子树内的命中也一并保留。

    否则会出现一个很难解释的结果：用户勾了「保留 __pycache__」，但里面的 `mod.pyc`
    仍被单独搬走 → 目录变空 → `sweep_empty_dirs` 再把空壳收走。
    用户勾的那一项最后还是没了，而且隔离区里只剩子项、没有那个目录。
    """
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    result = apply_pending_rules(workspace, meta["run_id"], keep=["__pycache__"])
    assert (workspace / "__pycache__" / "mod.pyc").exists(), "保留目录时子项也不该被搬走"
    assert (workspace / "__pycache__").exists(), "保留的目录不该被 sweep 当空壳收走"
    assert result["kept_by_user"] >= 2, "目录与其子项都应记为人工保留"


def test_keep_child_blocks_its_hit_ancestor(workspace: Path) -> None:
    """保留项落在某个命中目录内部时，那个父目录本轮也不能动——搬走父目录等于把它一起搬走。"""
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    # node_modules 是命中目录，pkg/index.js 在它内部
    result = apply_pending_rules(workspace, meta["run_id"], keep=["node_modules/pkg"])
    assert (workspace / "node_modules" / "pkg" / "index.js").exists()
    assert result["quarantined"] == 0 or (workspace / "node_modules").exists()


def test_purge_rule_does_not_bypass_protect(workspace: Path) -> None:
    """子树里躺着受保护路径时整条拦下，直删也不能把它带走。"""
    (workspace / "node_modules" / ".env").write_text("SECRET=1", encoding="utf-8")
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    result = apply_pending_rules(
        workspace, meta["run_id"], purge_rule_ids=["dep_node_modules"]
    )
    assert result["purged_directly"] == 0
    assert result["blocked_by_protect"] >= 1
    assert (workspace / "node_modules" / ".env").exists()


def test_dry_run_reports_would_purge_without_touching(workspace: Path) -> None:
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    result = apply_pending_rules(
        workspace, meta["run_id"], dry_run=True, purge_rule_ids=["dep_python_cache"]
    )
    assert result["dry_run"] is True
    assert result["would_purge"] == 1
    assert (workspace / "__pycache__").exists(), "dry-run 不得动任何文件"


# --------------------------- 隔离区按条删除 ---------------------------


def test_purge_entries_by_id_keeps_the_rest(workspace: Path) -> None:
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    apply_pending_rules(workspace, meta["run_id"])
    rdir = run_dir(workspace, meta["run_id"])
    manifest = rdir / "quarantine_manifest.json"
    entries = read_json(manifest)
    assert len(entries) >= 2

    target = entries[0]
    purged = purge_entries(workspace, manifest, entry_ids={target["id"]})
    assert purged == [target["original_path"]]
    left = read_json(manifest)
    assert target["id"] not in {e["id"] for e in left}
    assert len(left) == len(entries) - 1


def test_purge_entries_without_selection_is_a_noop(workspace: Path) -> None:
    """忘了传 ids 也不能删光——这是「一键清空」和「手滑」之间的那道墙。"""
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    apply_pending_rules(workspace, meta["run_id"])
    rdir = run_dir(workspace, meta["run_id"])
    manifest = rdir / "quarantine_manifest.json"
    before = read_json(manifest)

    assert purge_entries(workspace, manifest) == []
    assert read_json(manifest) == before


def test_purge_entries_refuses_paths_outside_state_dir(workspace: Path, tmp_path: Path) -> None:
    """清单被改成指向状态目录之外时，拒绝删除但保留记录——问题要可见，文件要安全。"""
    outside = tmp_path / "precious.txt"
    outside.write_text("don't delete me", encoding="utf-8")

    rdir = run_dir(workspace, "run-x")
    rdir.mkdir(parents=True, exist_ok=True)
    manifest = rdir / "quarantine_manifest.json"
    manifest.write_text(
        '[{"id": "e1", "original_path": "precious.txt", '
        f'"quarantine_path": "{outside}", "rule_id": null, "reason": "", '
        '"size": 0, "sha256": null, "quarantined_at": "", "is_dir": false}]',
        encoding="utf-8",
    )

    assert purge_entries(workspace, manifest, purge_all=True) == []
    assert outside.exists(), "越界路径绝不能被删"
    assert len(read_json(manifest)) == 1, "拒绝删除的条目要留在清单里，让人看得见"
