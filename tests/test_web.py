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
