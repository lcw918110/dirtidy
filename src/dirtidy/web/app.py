"""本地 FastAPI：加载 run 产物、保存决策、触发 apply-decisions。

仅绑定 127.0.0.1 使用（由 CLI serve 默认 host 保证）；不设鉴权，勿对公网暴露。
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dirtidy.decision.store import apply_decisions, save_decisions
from dirtidy.models import Decision, quarantine_root, read_json, run_dir, state_root


class DecisionsBody(BaseModel):
    decisions: list[dict] = Field(default_factory=list)


def create_app(*, default_root: Path, default_run_id: str | None = None) -> FastAPI:
    app = FastAPI(title="dirtidy", version="0.1.0")
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 安全响应头：审阅页不加载任何外部资源，用严格 CSP 兜底 XSS
    @app.middleware("http")
    async def _security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index() -> FileResponse:
        index_path = static_dir / "index.html"
        if not index_path.exists():
            raise HTTPException(500, "缺少 static/index.html")
        return FileResponse(index_path)

    @app.get("/api/meta")
    def meta() -> dict:
        return {"root": str(default_root), "run_id": default_run_id}

    @app.get("/api/runs")
    def list_runs() -> dict:
        runs_root = state_root(default_root) / "runs"
        if not runs_root.exists():
            return {"runs": []}
        runs = sorted([p.name for p in runs_root.iterdir() if p.is_dir()], reverse=True)
        return {"runs": runs}

    def _run_path(run_id: str) -> Path:
        p = run_dir(default_root, run_id)
        if not p.exists():
            raise HTTPException(404, f"run 不存在: {run_id}")
        return p

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        """聚合前端所需的候选、建议、已有决策、保护清单等 JSON。"""
        rdir = _run_path(run_id)
        out = {"meta": read_json(rdir / "meta.json")}
        names = (
            "candidates.json",
            "quarantine_manifest.json",
            "ai_suggestions.json",
            "decisions.json",
            "protection.json",
            "protect_policy.json",
            "pending_auto_hits.json",
        )
        for name in names:
            fp = rdir / name
            out[name.replace(".json", "")] = read_json(fp) if fp.exists() else []
        return out

    @app.put("/api/runs/{run_id}/decisions")
    def put_decisions(run_id: str, body: DecisionsBody) -> dict:
        rdir = _run_path(run_id)
        decisions = [Decision(**d) for d in body.decisions]
        save_decisions(rdir, decisions)
        return {"saved": len(decisions)}

    @app.post("/api/runs/{run_id}/apply-decisions")
    def post_apply(run_id: str) -> dict:
        rdir = _run_path(run_id)
        qdir = quarantine_root(default_root, run_id)
        return apply_decisions(default_root, run_id, rdir, qdir)

    return app
