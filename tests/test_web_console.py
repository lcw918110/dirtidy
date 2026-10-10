"""Web 控制台：扫描 / 规则 / 设置 / 模型 / 隔离区 / 监视 的接口行为。

与 `test_web.py` 的分工：那边守「审阅闭环」（决策、保留、解释一致性），
这边守「CLI 全部功能在界面上都有对应入口且真的能用」。
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dirtidy.models import read_json, run_dir
from dirtidy.web.app import create_app

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "sample_workspace"
STATIC = Path(__file__).resolve().parent.parent / "src" / "dirtidy" / "web" / "static"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    dest = tmp_path / "ws"
    shutil.copytree(FIXTURE_SRC, dest)
    return dest


@pytest.fixture()
def client(workspace: Path) -> TestClient:
    return TestClient(create_app(default_root=workspace))


# --------------------------------- 扫描 ---------------------------------


def test_web_scan_endpoint_creates_run(client: TestClient, workspace: Path) -> None:
    resp = client.post("/api/scan", json={"rules": "default", "quick": True, "workers": 2})
    assert resp.status_code == 200
    run_id = resp.json()["run_id"]
    assert (workspace / ".dirtidy" / "runs" / run_id).is_dir()
    assert run_id in client.get("/api/runs").json()["runs"]


def test_web_scan_rejects_unknown_pack(client: TestClient) -> None:
    resp = client.post("/api/scan", json={"rules": "no-such-pack"})
    assert resp.status_code == 400
    assert "no-such-pack" in resp.json()["detail"]


def test_web_scan_rejects_zero_workers(client: TestClient) -> None:
    assert client.post("/api/scan", json={"workers": 0}).status_code == 400


def test_web_scan_with_apply_moves_files(client: TestClient, workspace: Path) -> None:
    resp = client.post("/api/scan", json={"quick": True, "apply": True})
    assert resp.status_code == 200
    assert not (workspace / "node_modules").exists(), "apply=true 应已执行隔离"


# --------------------------------- 规则 ---------------------------------


def test_web_rules_lists_packs(client: TestClient) -> None:
    packs = client.get("/api/rules").json()["packs"]
    assert "default" in packs
    assert "server_workspace" in packs


def test_web_rules_detail_marks_purge_safe_and_builtin(client: TestClient) -> None:
    data = client.get("/api/rules/default").json()
    assert data["pack_id"] == "default"
    ids = {r["id"] for r in data["rules"]}
    assert "dep_python_cache" in ids
    # purge_safe 只是「建议直删」的标记，不改变默认行为
    assert "dep_python_cache" in data["purge_safe_ids"]
    assert "protect_vcs" not in data["purge_safe_ids"]

    # 生效保护清单必须标出哪些来自内置（关不掉的）
    by_id = {p["id"]: p for p in data["protect_policy"]}
    assert by_id["builtin_protect_vcs"]["builtin"] is True
    assert by_id["protect_vcs"]["builtin"] is False


def test_web_rules_unknown_pack_returns_404(client: TestClient) -> None:
    assert client.get("/api/rules/no-such-pack").status_code == 404


# --------------------------------- 设置 ---------------------------------


def test_web_settings_roundtrip_auto_purge(client: TestClient) -> None:
    assert client.get("/api/settings").json()["auto_purge_rules"] == []
    resp = client.put("/api/settings", json={"auto_purge_rules": ["dep_python_cache"]})
    assert resp.status_code == 200
    assert resp.json()["auto_purge_rules"] == ["dep_python_cache"]
    assert client.get("/api/settings").json()["auto_purge_rules"] == ["dep_python_cache"]


def test_web_settings_toggles_use_model(client: TestClient) -> None:
    assert client.get("/api/settings").json()["use_model"] is False
    client.put("/api/settings", json={"use_model": True})
    assert client.get("/api/settings").json()["use_model"] is True


def test_web_apply_rules_honours_auto_purge_setting(
    client: TestClient, workspace: Path
) -> None:
    """界面上开了直删，执行自动隔离时就要真的直删——两端读同一份设置。"""
    run_id = client.post("/api/scan", json={"quick": True}).json()["run_id"]
    client.put("/api/settings", json={"auto_purge_rules": ["dep_python_cache"]})

    body = client.post(f"/api/runs/{run_id}/apply-rules", json={}).json()
    assert body["purged_directly"] == 1
    assert body["purged_paths"] == ["__pycache__"]

    manifest = read_json(run_dir(workspace, run_id) / "quarantine_manifest.json")
    assert "__pycache__" not in {e["original_path"] for e in manifest}


def test_web_run_detail_exposes_auto_purge_rules(client: TestClient) -> None:
    """审阅页要能标出「这一条命中后会被直删」，否则用户看不到差别。"""
    run_id = client.post("/api/scan", json={"quick": True}).json()["run_id"]
    client.put("/api/settings", json={"auto_purge_rules": ["dep_python_cache"]})
    detail = client.get(f"/api/runs/{run_id}").json()
    assert detail["auto_purge_rules"] == ["dep_python_cache"]


# --------------------------------- 模型 ---------------------------------


def test_web_model_save_and_status(client: TestClient) -> None:
    resp = client.put(
        "/api/model",
        json={
            "provider": "openai_compatible",
            "openai_compatible": {
                "base_url": "https://example.test/v1",
                "model": "demo",
                "api_key_env": "DEMO_KEY",
                "api_key": "sk-plain",
            },
            "ollama": {},
            "summary": {"max_targets": 10},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["configured"] is True
    assert body["model"] == "demo"
    assert body["api_key_inline"] is True
    # 表单预填值不得回显密钥
    assert "sk-plain" not in str(body["form"])
    assert "sk-plain" not in resp.text


def test_web_model_blank_key_keeps_existing(client: TestClient) -> None:
    """密钥框留空 = 保持原样。表单不回显密钥，若当成清空，密钥会悄悄没了。"""
    client.put(
        "/api/model",
        json={
            "provider": "openai_compatible",
            "openai_compatible": {"base_url": "https://example.test/v1", "api_key": "sk-first"},
            "ollama": {},
            "summary": {},
        },
    )
    client.put(
        "/api/model",
        json={
            "provider": "openai_compatible",
            "openai_compatible": {
                "base_url": "https://example.test/v2",
                "api_key": "",
            },
            "ollama": {},
            "summary": {},
        },
    )
    assert client.get("/api/settings").json()["model"]["api_key_inline"] is True


def test_web_model_rejects_bad_provider(client: TestClient) -> None:
    resp = client.put("/api/model", json={"provider": "mock", "openai_compatible": {}, "ollama": {}})
    assert resp.status_code == 400


def test_web_analyze_requires_enabled_model(client: TestClient) -> None:
    run_id = client.post("/api/scan", json={"quick": True}).json()["run_id"]
    resp = client.post(f"/api/runs/{run_id}/analyze", json={})
    assert resp.status_code == 400
    assert "未启用" in resp.json()["detail"]


def test_web_analyze_mock_writes_suggestions(client: TestClient, workspace: Path) -> None:
    """mock 只留给脚本化演示与排查；界面上不暴露给终端用户。"""
    run_id = client.post("/api/scan", json={"quick": True}).json()["run_id"]
    resp = client.post(f"/api/runs/{run_id}/analyze", json={"mock": True})
    assert resp.status_code == 200
    assert resp.json()["provider"] == "mock"
    assert (run_dir(workspace, run_id) / "ai_suggestions.json").exists()


# -------------------------------- 隔离区 --------------------------------


def test_web_restore_by_id(client: TestClient, workspace: Path) -> None:
    run_id = client.post("/api/scan", json={"quick": True, "apply": True}).json()["run_id"]
    entries = read_json(run_dir(workspace, run_id) / "quarantine_manifest.json")
    target = entries[0]

    resp = client.post(f"/api/runs/{run_id}/restore", json={"ids": [target["id"]]})
    assert resp.status_code == 200
    assert resp.json()["restored"] == [target["original_path"]]
    assert (workspace / target["original_path"]).exists()


def test_web_purge_by_id_then_all(client: TestClient, workspace: Path) -> None:
    run_id = client.post("/api/scan", json={"quick": True, "apply": True}).json()["run_id"]
    manifest = run_dir(workspace, run_id) / "quarantine_manifest.json"
    entries = read_json(manifest)

    one = client.post(f"/api/runs/{run_id}/purge", json={"ids": [entries[0]["id"]]}).json()
    assert one["count"] == 1
    assert len(read_json(manifest)) == len(entries) - 1

    rest = client.post(f"/api/runs/{run_id}/purge", json={"all": True}).json()
    assert rest["count"] == len(entries) - 1
    assert read_json(manifest) == []


def test_web_purge_requires_a_selection(client: TestClient) -> None:
    """空 body 不能清空隔离区——「一键清空」和「手滑」之间的那道墙。"""
    run_id = client.post("/api/scan", json={"quick": True}).json()["run_id"]
    assert client.post(f"/api/runs/{run_id}/purge", json={}).status_code == 400
    assert client.post(f"/api/runs/{run_id}/restore", json={}).status_code == 400


def test_web_quarantine_summary_reports_real_disk_size(
    client: TestClient, workspace: Path
) -> None:
    """目录条目的 `size` 恒为 0，汇总必须实算磁盘占用——否则会显示成「0 B」。"""
    (workspace / "node_modules" / "big.bin").write_bytes(b"x" * 200_000)
    run_id = client.post("/api/scan", json={"quick": True, "apply": True}).json()["run_id"]

    summary = client.get(f"/api/runs/{run_id}").json()["quarantine_summary"]
    assert summary["count"] >= 1
    assert summary["size"] > 200_000, summary
    assert any(r["rule_id"] == "dep_node_modules" for r in summary["by_rule"])


def test_web_quarantine_rows_match_summary(client: TestClient, workspace: Path) -> None:
    """逐行占用也必须是实算值，且逐行之和 == 汇总。

    只修汇总不修逐行会出现最难解释的一种界面：每行写「0 B」，合计写「293 KB」，
    用户不知道该信哪个。
    """
    (workspace / "node_modules" / "big.bin").write_bytes(b"x" * 200_000)
    run_id = client.post("/api/scan", json={"quick": True, "apply": True}).json()["run_id"]

    detail = client.get(f"/api/runs/{run_id}").json()
    rows = {e["original_path"]: e for e in detail["quarantine_manifest"]}
    assert rows["node_modules"]["size_actual"] > 200_000
    assert rows["node_modules"]["exists"] is True
    assert sum(e["size_actual"] for e in detail["quarantine_manifest"]) == (
        detail["quarantine_summary"]["size"]
    )


def test_web_quarantine_marks_entries_missing_from_disk(
    client: TestClient, workspace: Path
) -> None:
    """清单里有、磁盘上没了的条目要标出来，否则界面上留着一条点不动也删不掉的记录。"""
    run_id = client.post("/api/scan", json={"quick": True, "apply": True}).json()["run_id"]
    manifest = run_dir(workspace, run_id) / "quarantine_manifest.json"
    entry = read_json(manifest)[0]

    # 模拟「有人绕过本工具手工删掉了隔离区里的文件」
    state = workspace / ".dirtidy"
    victim = state / entry["quarantine_path"]
    if victim.is_dir():
        shutil.rmtree(victim)
    else:
        victim.unlink()

    rows = {e["id"]: e for e in client.get(f"/api/runs/{run_id}").json()["quarantine_manifest"]}
    assert rows[entry["id"]]["exists"] is False
    assert rows[entry["id"]]["size_actual"] == 0


# --------------------------------- 监视 ---------------------------------


def test_web_watch_start_and_stop(client: TestClient) -> None:
    assert client.get("/api/watch").json()["running"] is False

    assert client.post(
        "/api/watch/start",
        json={"interval_minutes": 0.02, "quick": True, "notify": False},
    ).status_code == 200

    deadline = time.time() + 8
    while time.time() < deadline:
        st = client.get("/api/watch").json()
        if st["rounds"] >= 1:
            break
        time.sleep(0.2)
    assert st["running"] is True
    assert st["rounds"] >= 1
    assert st["total"] >= 1, "夹具里应有可清理项"

    client.post("/api/watch/stop")
    deadline = time.time() + 8
    while time.time() < deadline:
        if client.get("/api/watch").json()["running"] is False:
            break
        time.sleep(0.2)
    assert client.get("/api/watch").json()["running"] is False


def test_web_watch_rejects_double_start(client: TestClient) -> None:
    client.post("/api/watch/start", json={"interval_minutes": 0.05, "quick": True})
    try:
        assert client.post("/api/watch/start", json={"interval_minutes": 0.05}).status_code == 400
    finally:
        client.post("/api/watch/stop")


def test_web_watch_rejects_zero_interval(client: TestClient) -> None:
    assert client.post("/api/watch/start", json={"interval_minutes": 0}).status_code == 400


# ------------------------------ 界面覆盖度 ------------------------------


#: CLI 子命令 → 界面上的入口标记 (标记, 该标记必须出现的位置)。
#: 新增 CLI 子命令时必须在这里补一行（或进 CLI_UI_EXEMPT），否则用例会红——
#: 「命令行有的功能都应该界面化」这条要求，靠这份表来守，而不是靠自觉。
#:
#: 位置必须写准：按钮 id 要在 **html** 里（只写在 app.js 里是「引用了一个不存在的
#: 元素」，页面照样没有入口），函数名才查 js。
CLI_TO_UI: dict[str, tuple[str, str]] = {
    "scan": ("scanBtn", "html"),
    "explain": ("whyButton", "js"),
    "apply-rules": ("applyRulesBtn", "html"),
    "analyze": ("analyzeBtn", "html"),
    "apply-decisions": ("applyBtn", "html"),
    "save-decisions": ("saveBtn", "html"),
    "quarantine-restore": ("restoreAllBtn", "html"),
    "purge-quarantine": ("purgeAllBtn", "html"),
    "watch": ("watchStartBtn", "html"),
}

#: 这两个子命令本身就是「起界面」，不需要再给它们做界面入口。
CLI_UI_EXEMPT = {"serve", "desktop"}


def test_web_page_covers_every_cli_command() -> None:
    from dirtidy.cli import build_parser

    parser = build_parser()
    actual = set(parser._subparsers._group_actions[0].choices)  # type: ignore[attr-defined]
    mapped = set(CLI_TO_UI) | CLI_UI_EXEMPT
    assert mapped == actual, (
        f"界面覆盖表与 CLI 不一致：界面多出 {sorted(mapped - actual)}，"
        f"CLI 多出 {sorted(actual - mapped)}——新增子命令后请补 CLI_TO_UI 或 CLI_UI_EXEMPT"
    )

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for command, (marker, where) in CLI_TO_UI.items():
        source = html if where == "html" else js
        assert marker in source, (
            f"CLI `{command}` 对应的界面入口 {marker!r} 在 {where} 里找不到"
        )


def test_web_page_has_all_console_tabs() -> None:
    """界面必须按功能分页，而不是把所有东西堆在一屏。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    for tab in ("overview", "scan", "review", "quarantine", "rules", "settings", "watch"):
        assert f'id="tab-{tab}"' in html, f"缺少 {tab} 分页"
    for el_id in (
        "quarantineRows",
        "purgeSelBtn",
        "purgeAllBtn",
        "restoreAllBtn",
        "ruleRows",
        "protectRows",
        "scanBtn",
        "analyzeBtn",
        "watchStartBtn",
    ):
        assert f'id="{el_id}"' in html, f"缺少 {el_id}"


def test_web_page_explains_direct_delete_semantics() -> None:
    """直删是本项目最容易误解的能力，页面上必须把「先搬入隔离区再删」说清楚。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert "隔离后立即物理删除" in html or "先搬入隔离区、随即物理删除" in html
    assert "保护判定依然优先" in html


def test_frontend_console_avoids_innerhtml() -> None:
    """静态守护：控制台前端同样不得用 innerHTML 拼文件名（防 XSS 回归）。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "innerHTML" not in js, "app.js 不应再用 innerHTML 拼接外部数据"
    assert "textContent" in js


def test_web_status_bar_renders_human_text() -> None:
    """状态提示必须是人话，不能把后端返回体原样铺到页面上。

    回归背景：原先页面末尾是个 `<pre id="status">`，setStatus 直接
    `JSON.stringify` 整个返回体，用户看到的是 `{"loaded": "...", "candidates": 4}`；
    而且它在所有分页之外，每页都会漏出来；动作结果还会被随后的 loadRun 覆盖掉。
    现在改成底部状态条 + 字段名→人话的映射表。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'id="statusbar"' in html, "状态提示应是一个独立的状态条容器"
    assert 'id="status"' in html
    assert '<pre id="status"' not in html, "状态条不应退回成裸 JSON 的 <pre>"
    assert "JSON.stringify(obj" not in js, "setStatus 不应把整个返回体 stringify 出来"
    assert "humanStatus" in js, "应经 humanStatus 把字段名翻成人话"


def test_web_status_labels_cover_action_fields() -> None:
    """动作类接口返回的字段名，界面上都得有人话标签，否则会退化成裸字段名。

    这些键来自 apply-rules / apply-decisions / purge / restore / settings，
    是用户点按钮后最直接看到的反馈。
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    start = js.index("const STATUS_LABELS")
    block = js[start : js.index("};", start)]
    for key in (
        "quarantined",
        "purged_directly",
        "purged",
        "restored",
        "saved",
        "kept",
        "archived",
        "blocked_by_protect",
        "kept_by_user",
        "suggestions",
        "auto_purge_rules",
        "use_model",
    ):
        assert f"{key}:" in block, f"STATUS_LABELS 缺少 {key} 的人话标签"
