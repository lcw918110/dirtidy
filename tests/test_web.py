"""本地 Web 审阅路由：加载 run、保存决策、执行决策。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dirtidy.pipeline import create_run
from dirtidy.web.app import create_app

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "sample_workspace"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    dest = tmp_path / "ws"
    shutil.copytree(FIXTURE_SRC, dest)
    return dest


def test_web_save_and_apply(workspace: Path) -> None:
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    app = create_app(default_root=workspace, default_run_id=meta["run_id"])
    client = TestClient(app)

    listed = client.get("/api/runs")
    assert listed.status_code == 200
    assert meta["run_id"] in listed.json()["runs"]

    detail = client.get(f"/api/runs/{meta['run_id']}")
    assert detail.status_code == 200
    assert "candidates" in detail.json()

    saved = client.put(
        f"/api/runs/{meta['run_id']}/decisions",
        json={
            "decisions": [
                {
                    "path": "zero.txt",
                    "action": "quarantine",
                    "target_relpath": None,
                    "note": "web",
                }
            ]
        },
    )
    assert saved.status_code == 200
    assert saved.json()["saved"] == 1

    applied = client.post(f"/api/runs/{meta['run_id']}/apply-decisions")
    assert applied.status_code == 200
    assert applied.json()["quarantined"] == 1
    assert not (workspace / "zero.txt").exists()


def test_security_headers_present(workspace: Path) -> None:
    """审阅页必须带严格 CSP 等安全响应头，兜底 XSS。"""
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    app = create_app(default_root=workspace, default_run_id=meta["run_id"])
    client = TestClient(app)

    resp = client.get("/")
    assert resp.status_code == 200
    csp = resp.headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert resp.headers.get("x-content-type-options") == "nosniff"


def test_frontend_avoids_innerhtml() -> None:
    """静态守护：前端不得用 innerHTML 拼文件名，防止 XSS 回归。"""
    js = (
        Path(__file__).resolve().parent.parent
        / "src"
        / "dirtidy"
        / "web"
        / "static"
        / "app.js"
    ).read_text(encoding="utf-8")
    assert "innerHTML" not in js, "app.js 不应再用 innerHTML 拼接外部数据"
    assert "textContent" in js


def test_web_detail_exposes_rule_and_protection_artifacts(workspace: Path) -> None:
    """审阅接口必须把「规则想动什么」「什么被保护」一并交给前端。

    后端早就返回了这些键，但前端曾完全不渲染 —— 用户在网页上看不到规则打算
    自动隔离哪些文件，终审闭环是断的。这里先把「接口给全」钉住。
    """
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    app = create_app(default_root=workspace, default_run_id=meta["run_id"])
    client = TestClient(app)

    detail = client.get(f"/api/runs/{meta['run_id']}").json()
    for key in ("pending_auto_hits", "protection", "protect_policy", "quarantine_manifest"):
        assert key in detail, f"审阅接口缺少 {key}"
    assert detail["pending_auto_hits"], "夹具应至少有一条待自动隔离项"
    assert detail["protect_policy"], "生效保护策略不应为空"


def test_web_apply_rules_with_keep(workspace: Path) -> None:
    """网页上「执行自动隔离」必须能带保留项，且保留项确实留在原地。"""
    meta = create_run(workspace, rules="default", quick=True, apply=False)
    app = create_app(default_root=workspace, default_run_id=meta["run_id"])
    client = TestClient(app)

    detail = client.get(f"/api/runs/{meta['run_id']}").json()
    pending = [h["path"] for h in detail["pending_auto_hits"]]
    assert pending, "夹具应至少有一条待自动隔离项"

    keep = pending[0]
    resp = client.post(f"/api/runs/{meta['run_id']}/apply-rules", json={"keep_paths": [keep]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["kept_by_user"] == 1
    assert (workspace / keep).exists(), "保留项不得被搬走"

    # 其余 pending 项应已被隔离
    for path in pending[1:]:
        assert not (workspace / path).exists(), f"{path} 未被隔离"


def test_web_page_has_pending_and_protection_sections() -> None:
    """静态守护：审阅页必须渲染「待自动隔离」与「受保护」两块。

    这是「不误删」的终审入口——只有候选表的话，用户在网页上无法确认规则要动什么。
    """
    static = Path(__file__).resolve().parent.parent / "src" / "dirtidy" / "web" / "static"
    html = (static / "index.html").read_text(encoding="utf-8")
    assert "pendingRows" in html
    assert "applyRulesBtn" in html
    assert 'id="protection"' in html

    js = (static / "app.js").read_text(encoding="utf-8")
    assert "renderPending" in js
    assert "renderProtection" in js
    assert "apply-rules" in js
