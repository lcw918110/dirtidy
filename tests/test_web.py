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
