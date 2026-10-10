"""本地 FastAPI：扫描 / 审阅 / 执行 / 隔离区管理 / 规则 / 设置 / 监视。

设计原则：**网页上每个动作都复用 CLI 的同一份实现**（`pipeline` / `quarantine` /
`ai` / `watch`），不在这里另写一套。否则「网页说的和命令行做的不一样」这类漂移
迟早出现，而它比没有界面更危险——用户会以为自己看的是真相。

仅绑定 127.0.0.1 使用（由 CLI serve 默认 host 保证）；不设鉴权，勿对公网暴露。
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from dirtidy.ai.providers import analyze_run
from dirtidy.decision.store import apply_decisions, save_decisions
from dirtidy.explain import explain_path
from dirtidy.models import (
    Decision,
    default_rules_dir,
    quarantine_root,
    read_json,
    run_dir,
    state_root,
)
from dirtidy.pipeline import apply_pending_rules, create_run, protect_rules_from_paths
from dirtidy.quarantine.store import (
    purge_entries,
    purge_quarantine_dir,
    quarantine_entry_path,
    restore_entries,
    tree_size,
)
from dirtidy.rules.engine import RulePackError, load_rule_pack, protect_policy
from dirtidy.rules.protect import builtin_protect_rules
from dirtidy.settings import (
    SELECTABLE_PROVIDERS,
    SettingsError,
    load_auto_purge_rules,
    load_model_settings,
    load_use_model,
    model_settings_path,
    model_settings_status,
    save_auto_purge_rules,
    save_model_settings,
    save_use_model,
)
from dirtidy.watch import run_watch


class DecisionsBody(BaseModel):
    decisions: list[dict] = Field(default_factory=list)


class ApplyRulesBody(BaseModel):
    """执行自动隔离时可带一组「人工保留」路径，这些路径本轮不动。"""

    keep_paths: list[str] = Field(default_factory=list)


class ScanBody(BaseModel):
    rules: str = "default"
    quick: bool = False
    workers: int = 4
    protect: list[str] = Field(default_factory=list)
    apply: bool = False


class EntryIdsBody(BaseModel):
    """按 id 操作隔离区条目；all=True 时作用于全部。"""

    ids: list[str] = Field(default_factory=list)
    all: bool = False


class AnalyzeBody(BaseModel):
    """模型分析。mock 只用于离线演示/排查，界面上不暴露给终端用户。"""

    mock: bool = False


class SettingsBody(BaseModel):
    auto_purge_rules: list[str] | None = None
    use_model: bool | None = None


class ModelBody(BaseModel):
    provider: str = "openai_compatible"
    openai_compatible: dict = Field(default_factory=dict)
    ollama: dict = Field(default_factory=dict)
    summary: dict = Field(default_factory=dict)


class WatchStartBody(BaseModel):
    interval_minutes: float = 30.0
    rules: str = "default"
    quick: bool = True
    protect: list[str] = Field(default_factory=list)
    notify: bool = False


def _model_form(root: Path) -> dict[str, Any]:
    """给界面预填的模型表单。**永不回显密钥**——只说「填没填过」。"""
    try:
        data = load_model_settings(root) or {}
    except SettingsError:
        data = {}
    oc = dict(data.get("openai_compatible") or {})
    api_key_env = str(oc.pop("api_key_env", "") or "DIRTIDY_API_KEY")
    oc.pop("api_key", None)
    raw_ollama = data.get("ollama")
    ol = dict(raw_ollama) if isinstance(raw_ollama, dict) else {}
    raw_summary = data.get("summary")
    return {
        "provider": str(data.get("provider") or "openai_compatible"),
        "openai_compatible": {
            "base_url": str(oc.get("base_url") or "https://api.openai.com/v1"),
            "model": str(oc.get("model") or "gpt-4o-mini"),
            "api_key_env": api_key_env,
        },
        "ollama": {
            "host": str(ol.get("host") or "http://127.0.0.1:11434"),
            "model": str(ol.get("model") or "llama3.2"),
        },
        "summary": dict(raw_summary) if isinstance(raw_summary, dict) else {},
    }


def _list_rule_packs() -> list[str]:
    packs_dir = default_rules_dir()
    if not packs_dir.exists():
        return []
    return sorted(p.stem for p in packs_dir.glob("*.yaml"))


def _pack_detail(pack_id: str) -> dict[str, Any]:
    try:
        pack = load_rule_pack(pack_id)
    except FileNotFoundError as e:
        raise HTTPException(404, f"找不到规则包: {pack_id}") from e
    except RulePackError as e:
        raise HTTPException(400, f"规则包非法: {e}") from e

    rules = list(pack.get("rules") or [])
    builtin_ids = {str(r.get("id")) for r in builtin_protect_rules()}
    policy = protect_policy(pack)
    for item in policy:
        item["builtin"] = item.get("id") in builtin_ids

    return {
        "pack_id": str(pack.get("pack_id") or pack_id),
        "version": pack.get("version"),
        "rules": rules,
        "protect_policy": policy,
        "purge_safe_ids": [str(r.get("id")) for r in rules if r.get("purge_safe")],
        "actions": sorted({str(r.get("action") or "candidate_only") for r in rules}),
    }


def create_app(*, default_root: Path, default_run_id: str | None = None) -> FastAPI:
    app = FastAPI(title="dirtidy", version="0.1.0")
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    # 后台监视：一个 app 实例最多一个线程。状态放闭包里，多实例互不干扰。
    watch_state: dict[str, Any] = {
        "thread": None,
        "stop": None,
        "running": False,
        "rounds": 0,
        "total": 0,
        "added": [],
        "removed": [],
        "last_at": "",
        "error": "",
        "interval_minutes": 0.0,
        "rules": "",
    }
    watch_lock = threading.Lock()

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
        return {
            "root": str(default_root),
            "run_id": default_run_id,
            "state_dir": str(state_root(default_root)),
        }

    @app.get("/api/health")
    def health() -> dict:
        """探活端点：桌面壳用它判断「路由已挂好」，而不是只判断端口通了。"""
        return {"status": "ok"}

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

    def _quarantine_view(rdir: Path) -> tuple[list[dict], dict[str, Any]]:
        """隔离区明细（逐条补齐真实占用）+ 汇总。

        逐条的 `size` 也**必须实算**：清单里存的那份对目录恒为 0，逐行显示「0 B」
        而合计显示「293 KB」会自相矛盾，用户不知道该信哪个。
        `exists` 用来标出「清单里有、磁盘上没了」的幽灵条目（被人手工删过之类），
        否则界面上会留着一条点不动也删不掉的记录。
        """
        manifest_path = rdir / "quarantine_manifest.json"
        raw = read_json(manifest_path) if manifest_path.exists() else []
        if not isinstance(raw, list):
            raw = []

        enriched: list[dict] = []
        by_rule: dict[str, dict[str, int]] = {}
        total = 0
        for e in raw:
            if not isinstance(e, dict):
                continue
            path = quarantine_entry_path(default_root, e)
            # 越界或已被外部删掉的条目：exists=False，界面据此标出来，
            # 否则会留下一条点不动也删不掉的记录，用户无从判断。
            on_disk = path is not None and path.exists()
            size = tree_size(path) if on_disk and path is not None else 0
            enriched.append({**e, "size_actual": size, "exists": on_disk})
            total += size
            slot = by_rule.setdefault(str(e.get("rule_id") or "-"), {"count": 0, "size": 0})
            slot["count"] += 1
            slot["size"] += size

        purged_path = rdir / "purged_directly.json"
        purged = read_json(purged_path) if purged_path.exists() else []
        summary = {
            "count": len(enriched),
            "size": total,
            "by_rule": [{"rule_id": k, **v} for k, v in sorted(by_rule.items())],
            "purged_directly": len(purged) if isinstance(purged, list) else 0,
        }
        return enriched, summary

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict:
        """聚合前端所需的候选、建议、已有决策、保护清单等 JSON。"""
        rdir = _run_path(run_id)
        out: dict[str, Any] = {"meta": read_json(rdir / "meta.json")}
        names = (
            "candidates.json",
            "ai_suggestions.json",
            "decisions.json",
            "protection.json",
            "protect_policy.json",
            "pending_auto_hits.json",
            "purged_directly.json",
        )
        for name in names:
            fp = rdir / name
            out[name.replace(".json", "")] = read_json(fp) if fp.exists() else []
        entries, summary = _quarantine_view(rdir)
        out["quarantine_manifest"] = entries
        out["quarantine_summary"] = summary
        out["auto_purge_rules"] = load_auto_purge_rules(default_root)
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

    @app.post("/api/runs/{run_id}/apply-rules")
    def post_apply_rules(run_id: str, body: ApplyRulesBody | None = None) -> dict:
        """执行规则自动隔离。body.keep_paths 里的路径本轮保留，并登记进 protection.json。

        「隔离后立即物理删除」的规则清单来自状态目录的设置，与 CLI 同源——
        否则会出现「网页里开了直删、命令行跑却只隔离」。
        """
        _run_path(run_id)
        keep = body.keep_paths if body else []
        return apply_pending_rules(
            default_root,
            run_id,
            keep=keep,
            purge_rule_ids=load_auto_purge_rules(default_root),
        )

    @app.get("/api/runs/{run_id}/explain")
    def get_explain(run_id: str, path: str) -> dict:
        """解释某路径为什么被/不被动。

        复用 CLI `explain` 的同一套判定（进而复用 engine 的匹配与条件函数），
        并直接返回 `to_text()` 的文本 —— 网页与命令行看到的是同一份解释，
        不会出现「网页说的和 CLI 说的不一样」。
        """
        rdir = _run_path(run_id)
        meta = read_json(rdir / "meta.json")
        try:
            exp = explain_path(
                default_root,
                path,
                rules=meta.get("rules") or "default",
                extra_protect_rules=protect_rules_from_paths(meta.get("protect") or []),
            )
        except ValueError as e:
            raise HTTPException(400, f"路径非法: {e}") from e
        return {
            "path": exp.path,
            "decision": exp.decision,
            "decision_label": exp.to_dict()["decision_label"],
            "text": exp.to_text(),
        }

    @app.post("/api/scan")
    def post_scan(body: ScanBody) -> dict:
        """新建一次扫描。apply=True 时顺带执行高置信规则的隔离。"""
        if body.workers < 1:
            raise HTTPException(400, "workers 必须 ≥ 1")
        try:
            return create_run(
                default_root,
                rules=body.rules,
                quick=body.quick,
                apply=body.apply,
                workers=body.workers,
                protect=body.protect,
            )
        except FileNotFoundError as e:
            raise HTTPException(400, f"找不到规则包: {e}") from e
        except RulePackError as e:
            raise HTTPException(400, f"规则包非法: {e}") from e
        except PermissionError as e:
            raise HTTPException(403, str(e)) from e

    @app.post("/api/runs/{run_id}/analyze")
    def post_analyze(run_id: str, body: AnalyzeBody | None = None) -> dict:
        """对某次 run 跑模型分析。

        界面上不暴露 mock（它是固定占位值的桩，输出等于「全都交人工」）；
        这里保留 `mock` 参数只是为了脚本化演示与排查管道。
        """
        rdir = _run_path(run_id)
        use_mock = bool(body and body.mock)
        cfg_path: Path | None = None
        if not use_mock:
            if not load_use_model(default_root):
                raise HTTPException(
                    400, "模型分析未启用：请在「设置」页打开开关并保存模型配置。"
                )
            configured = model_settings_path(default_root)
            if not configured.exists():
                raise HTTPException(400, f"未配置模型：请先在「设置」页保存配置（{configured}）")
            cfg_path = configured
        try:
            results = analyze_run(default_root, rdir, config_path=cfg_path, mock=use_mock)
        except (RuntimeError, FileNotFoundError) as e:
            raise HTTPException(400, str(e)) from e
        except Exception as e:  # noqa: BLE001 - 网络/模型侧异常统一转成可读的 502
            raise HTTPException(502, f"模型调用失败: {e}") from e
        meta_path = rdir / "ai_meta.json"
        ai_meta = read_json(meta_path) if meta_path.exists() else {}
        return {"suggestions": len(results), "run_id": run_id, **ai_meta}

    @app.post("/api/runs/{run_id}/restore")
    def post_restore(run_id: str, body: EntryIdsBody) -> dict:
        """从隔离区还原（按 id 或全部）。原路径已有文件则跳过，不覆盖。"""
        rdir = _run_path(run_id)
        ids = set(body.ids) if body.ids else None
        if not body.all and not ids:
            raise HTTPException(400, "需要 ids 或 all=true")
        restored = restore_entries(
            default_root,
            rdir / "quarantine_manifest.json",
            entry_ids=ids,
            restore_all=body.all,
        )
        return {"restored": restored, "count": len(restored)}

    @app.post("/api/runs/{run_id}/purge")
    def post_purge(run_id: str, body: EntryIdsBody) -> dict:
        """彻底删除隔离区条目（按 id 或全部）。**不可恢复。**

        与 CLI `purge-quarantine` 等价：all=true 时删光全部条目并顺手清掉空的
        隔离目录。按条删除会同步收敛清单，界面上「已隔离」列表因此始终可信。
        """
        rdir = _run_path(run_id)
        ids = set(body.ids) if body.ids else None
        if not body.all and not ids:
            raise HTTPException(400, "需要 ids 或 all=true")
        purged = purge_entries(
            default_root,
            rdir / "quarantine_manifest.json",
            entry_ids=ids,
            purge_all=body.all,
        )
        if body.all:
            purge_quarantine_dir(quarantine_root(default_root, run_id))
        return {"purged": purged, "count": len(purged)}

    @app.get("/api/rules")
    def list_rules() -> dict:
        return {"packs": _list_rule_packs(), "selected": "default"}

    @app.get("/api/rules/{pack_id}")
    def get_rules(pack_id: str) -> dict:
        return _pack_detail(pack_id)

    @app.get("/api/settings")
    def get_settings() -> dict:
        return {
            "auto_purge_rules": load_auto_purge_rules(default_root),
            "use_model": load_use_model(default_root),
            "model": model_settings_status(default_root),
            "model_form": _model_form(default_root),
            "providers": list(SELECTABLE_PROVIDERS),
            "packs": _list_rule_packs(),
        }

    @app.put("/api/settings")
    def put_settings(body: SettingsBody) -> dict:
        if body.auto_purge_rules is not None:
            save_auto_purge_rules(default_root, body.auto_purge_rules)
        if body.use_model is not None:
            save_use_model(default_root, body.use_model)
        return {
            "auto_purge_rules": load_auto_purge_rules(default_root),
            "use_model": load_use_model(default_root),
        }

    @app.put("/api/model")
    def put_model(body: ModelBody) -> dict:
        """保存模型配置到状态目录的 models.yaml。

        密钥是**明文落盘**的（用户明确选择这条路），因此返回里带上路径，
        让界面能把这件事说清楚，而不是悄悄写下去。

        密钥留空 = **保持原样**，不是「清空」：表单里回显密钥是坏做法，所以
        界面永远拿不到已保存的密钥，用户不动那个框时若被当成清空，密钥就悄悄没了。
        """
        raw = body.model_dump()
        oc = raw.setdefault("openai_compatible", {})
        if not str(oc.get("api_key") or "").strip():
            try:
                existing = load_model_settings(default_root) or {}
            except SettingsError:
                existing = {}
            prev = str((existing.get("openai_compatible") or {}).get("api_key") or "")
            if prev:
                oc["api_key"] = prev
        try:
            save_model_settings(default_root, raw)
        except SettingsError as e:
            raise HTTPException(400, str(e)) from e
        status = model_settings_status(default_root)
        status["form"] = _model_form(default_root)
        return status

    @app.get("/api/watch")
    def get_watch() -> dict:
        with watch_lock:
            return {
                k: v
                for k, v in watch_state.items()
                if k not in ("thread", "stop")
            }

    @app.post("/api/watch/start")
    def start_watch(body: WatchStartBody) -> dict:
        with watch_lock:
            if watch_state["running"]:
                raise HTTPException(400, "监视已在运行")
            if body.interval_minutes <= 0:
                raise HTTPException(400, "interval_minutes 必须 > 0")
            stop = threading.Event()

            def on_round(info: dict[str, Any]) -> None:
                with watch_lock:
                    watch_state.update(
                        rounds=info.get("rounds", 0),
                        total=info.get("total", 0),
                        added=list(info.get("added") or []),
                        removed=list(info.get("removed") or []),
                        last_at=str(info.get("at") or ""),
                    )

            def worker() -> None:
                try:
                    run_watch(
                        default_root,
                        interval_minutes=body.interval_minutes,
                        rules=body.rules,
                        quick=body.quick,
                        protect=body.protect,
                        notify_enabled=body.notify,
                        stop_event=stop,
                        on_round=on_round,
                    )
                except Exception as e:  # noqa: BLE001 - 线程里的异常必须落到状态里，否则静默死掉
                    with watch_lock:
                        watch_state["error"] = str(e)
                finally:
                    with watch_lock:
                        watch_state["running"] = False

            thread = threading.Thread(target=worker, name="dirtidy-watch", daemon=True)
            watch_state.update(
                thread=thread,
                stop=stop,
                running=True,
                rounds=0,
                total=0,
                added=[],
                removed=[],
                last_at="",
                error="",
                interval_minutes=body.interval_minutes,
                rules=body.rules,
            )
            thread.start()
        return {"running": True}

    @app.post("/api/watch/stop")
    def stop_watch() -> dict:
        with watch_lock:
            stop = watch_state.get("stop")
            running = bool(watch_state["running"])
        if stop is not None:
            stop.set()
        return {"running": running, "stopped": stop is not None}

    return app
