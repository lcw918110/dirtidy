"""使用便利性：规则中文短名、列表筛选、批量勾选保留、扫描进度、开发热重载。

这一批改动的共同点是「让界面少让人猜」：规则不再只显示 `dep_node_modules`，
几百项的列表能筛，拿不准的能一键留下，扫描时能看到进度而不是盯着「扫描中…」。

每条用例都既测「做了」，也测「没顺手做错」——尤其是
**筛选只改显示**（执行范围仍是全量）与 **批量勾选不碰任何文件**（本项目承诺
「自动只到候选/隔离区为止，执行必须人工」）。
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dirtidy import cli
from dirtidy.rules.engine import load_rule_pack
from dirtidy.web.app import _reload_target, create_app

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "sample_workspace"
STATIC = Path(__file__).resolve().parent.parent / "src" / "dirtidy" / "web" / "static"


def _js_function(js: str, name: str) -> str:
    """粗切一个顶格函数体（含 `async function`）。用于「这段代码里不许出现 X」这类守卫。"""
    start = js.index(f"function {name}(")
    end = js.index("\n}", start)
    return js[start:end]


def _route_endpoint(app: FastAPI, path: str):
    """按路径取回路由函数本体。

    扫描进度只存在于 `create_app` 的闭包里，HTTP 之外读不到；直接调路由函数
    才能在不并发请求的前提下验证「扫描中状态确实被写进去了」。
    """
    for route in app.routes:
        if getattr(route, "path", None) == path:
            return route.endpoint
    raise AssertionError(f"没有注册路由 {path}")


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    dest = tmp_path / "ws"
    shutil.copytree(FIXTURE_SRC, dest)
    return TestClient(create_app(default_root=dest))


# ----------------------------- 规则的中文短名 -----------------------------


def test_every_rule_has_chinese_label() -> None:
    """每条规则都要有中文短名。

    界面靠它回答「这是什么规则」。原先这一列铺的是 `dep_node_modules`，
    用户只能猜；而规则包里其实早就有一条中文 `reason`，只是没人把它摆到该摆的位置。
    """
    pack = load_rule_pack("default")
    missing = [
        str(r.get("id")) for r in pack["rules"] if not str(r.get("label") or "").strip()
    ]
    assert not missing, f"这些规则没写 label（中文短名）：{missing}"
    for r in pack["rules"]:
        label = str(r["label"])
        assert label != r["id"], f"{r['id']} 的 label 直接抄了 id，等于没写"
        assert "_" not in label, f"{r['id']} 的 label 是内部编号风格：{label}"


def test_run_api_carries_rule_label(client: TestClient) -> None:
    """接口要带上中文短名，前端才有东西可显示（不许自己按 id 猜）。"""
    run_id = client.post("/api/scan", json={"quick": False, "workers": 2}).json()["run_id"]
    data = client.get(f"/api/runs/{run_id}").json()

    hits = data["pending_auto_hits"]
    assert hits
    for h in hits:
        assert h["rule_label"], f"{h['rule_id']} 没带中文短名"
        assert "_" not in h["rule_label"], f"{h['rule_id']} 的短名仍是内部编号"
    by_rule = {h["rule_id"]: h for h in hits}
    assert by_rule["dep_node_modules"]["rule_label"] == "Node 依赖"

    # 由 hashing 生成的候选在规则包里查不到，同样要有名字——
    # 否则候选表又会退回「一堆内部编号」。
    for c in data["candidates"]:
        assert c["rule_label"], f"{c['rule_id']} 的候选没带中文短名"


def test_quarantine_rows_show_rule_label(tmp_path: Path) -> None:
    """已隔离的条目也要有中文短名。

    隔离清单是扫描当时就落盘的，里面只有 `rule_id`；不补的话隔离区
    「哪条规则搬的」一列又变回 `dep_node_modules`——和审阅页刚改掉的是同一个毛病。
    """
    dest = tmp_path / "ws"
    shutil.copytree(FIXTURE_SRC, dest)
    client = TestClient(create_app(default_root=dest))
    run_id = client.post(
        "/api/scan", json={"quick": True, "workers": 2, "apply": True}
    ).json()["run_id"]

    entries = client.get(f"/api/runs/{run_id}").json()["quarantine_manifest"]
    assert entries, "夹具里应有 node_modules 等被隔离"
    for e in entries:
        assert e["rule_label"], f"{e['rule_id']} 的隔离条目没带中文短名"
        assert "_" not in e["rule_label"]


def test_frontend_shows_rule_label_not_internal_id() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function ruleLabel(" in js, "前端应统一走 ruleLabel()，而不是各处拼 rule_id"
    for fn in ("renderPending", "scanGroup"):
        body = _js_function(js, fn)
        assert "ruleLabel(" in body, f"{fn} 没有用中文短名"
        assert "规则 ${h.rule_id}" not in body, f"{fn} 还在直接铺内部编号"
        assert "规则 ${g.rule_id}" not in body, f"{fn} 还在直接铺内部编号"


def test_rule_page_translates_names_and_actions() -> None:
    """规则页要能看懂：名称用中文短名，动作也不该是 `auto_quarantine`。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    # 只切「规则清单」那张表：下面的「生效保护清单」是内置 id，本来就该露原始编号
    rule_table = html.split("<h3>规则清单</h3>")[1].split("<h3>生效保护清单</h3>")[0]
    assert "<th>名称</th>" in rule_table
    assert "<th>id</th>" not in rule_table, "规则页不该再把内部编号当第一列"
    body = _js_function(js, "loadRules")
    assert "r.label" in body
    assert "ACTION_LABELS" in body, "动作要翻成人话（自动隔离 / 保护 / 只进候选）"


# ------------------------------- 列表筛选 -------------------------------


def test_pending_filter_only_changes_display() -> None:
    """筛选只影响显示，执行范围仍是全部命中。

    否则「筛了 3 项却搬走 30 项」会像丢了东西——而这正是清理工具最容易
    让人失去信任的地方。
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="pendingAdvice"' in html
    assert 'id="pendingSearch"' in html
    assert 'id="candSearch"' in html

    body = _js_function(js, "renderPending")
    assert "all.filter(" in body, "应先保留全量，再筛出要显示的行"
    assert "currentPendingCount = all.length" in body, "执行条数必须按全量报"
    assert "筛选只影响显示" in body, "界面必须把「筛选不改执行范围」说出来"


def test_filter_survives_rerender() -> None:
    """勾选「保留」会触发重渲染，筛选条件必须跟着留住，否则筛完点一下就回到全部。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "const pendingFilter" in js, "筛选状态不能放在渲染函数里重建"
    assert "const candFilter" in js


# ---------------------------- 批量勾选「保留」 ----------------------------


def test_batch_keep_never_touches_files() -> None:
    """批量勾选只改「保留」状态，不联网、不弹确认、不调接口。

    「自动只到候选/隔离区为止，执行必须人工」——一键按钮很容易不小心变成
    「一键执行」，这里把它钉死：真正搬文件只发生在「执行自动隔离」里。
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = _js_function(js, "keepBatch")
    assert "keptPaths" in body
    for token in ("api(", "fetch(", "confirm("):
        assert token not in body, f"keepBatch 里出现了 {token}，一键按钮不该动手"
    for kind in ('"all"', '"none"', '"review"'):
        assert kind in body
    for btn in ("keepAllBtn", "keepReviewBtn", "keepNoneBtn"):
        assert f'getElementById("{btn}").onclick' in js, f"{btn} 没绑事件"


# ------------------------------ 扫描进度 ------------------------------


def test_scan_progress_reports_idle_when_not_scanning(client: TestClient) -> None:
    p = client.get("/api/scan/progress").json()
    assert p["running"] is False
    assert p["total"] == 0


def test_scan_progress_exposes_hash_progress_while_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """扫描中必须能读到 running 与 hash 进度——界面靠它判断「还在动，别重点」。"""
    dest = tmp_path / "ws"
    shutil.copytree(FIXTURE_SRC, dest)
    app = create_app(default_root=dest)
    client = TestClient(app)
    progress = _route_endpoint(app, "/api/scan/progress")
    seen: dict[str, Any] = {}

    def fake_create_run(root: Path, **kwargs: Any) -> dict[str, Any]:
        # 模拟扫描跑到一半：此时进度状态应当已经是「进行中 + 7/9」。
        kwargs["on_progress"](7, 9)
        seen.update(progress())
        return {"run_id": "fake-run"}

    monkeypatch.setattr("dirtidy.web.app.create_run", fake_create_run)
    assert client.post("/api/scan", json={"quick": False, "workers": 2}).status_code == 200

    assert seen["running"] is True
    assert (seen["done"], seen["total"]) == (7, 9)
    assert seen["phase"] == "内容 hash"
    # 结束后必须复位，否则界面会一直转圈，下一次扫描也会被误判成「已在跑」
    after = client.get("/api/scan/progress").json()
    assert after["running"] is False
    assert after["total"] == 0


def test_scan_button_polls_progress_and_stops() -> None:
    """前端要真的轮询，并且结束时停掉——否则状态条永远停在「扫描中…」。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "/api/scan/progress" in js
    assert "function startScanPolling(" in js
    assert "function stopScanPolling(" in js
    start = js.index('document.getElementById("scanBtn").onclick')
    end = js.index("\n};", start)
    body = js[start:end]
    assert "startScanPolling()" in body
    assert "stopScanPolling()" in body, "扫描结束必须停轮询（放在 finally 里）"


# ---------------------------- 开发热重载 ----------------------------


def test_serve_offers_reload_for_development() -> None:
    """`serve --reload` 默认关闭：它是开发便利，不该改变正常使用的行为。"""
    parser = cli.build_parser()
    assert parser.parse_args(["serve", "--reload"]).reload is True
    assert parser.parse_args(["serve"]).reload is False


def test_serve_reload_passes_import_string(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """uvicorn 的 reload 模式要求 app 是 import string + factory。

    直接传 app 对象会在启动时报错——这条把契约钉住，免得以后「优化」成传对象。
    """
    import uvicorn

    captured: dict[str, Any] = {}

    def fake_run(app: Any, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    rc = cli.main(["serve", "--root", str(tmp_path), "--reload"])

    assert rc == 0
    assert captured["app"] == "dirtidy.web.app:_reload_target"
    assert captured["factory"] is True
    assert captured["reload"] is True
    assert captured["reload_dirs"], "不指定 reload_dirs 会盯 cwd，扫大目录时反复重启"


def test_reload_target_builds_app_from_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """reload 模式下参数只能经环境变量传进来；读不到就退回当前目录，不该起不来。"""
    monkeypatch.setenv("DIRTIDY_RELOAD_ROOT", str(tmp_path))
    monkeypatch.delenv("DIRTIDY_RELOAD_RUN_ID", raising=False)
    meta = TestClient(_reload_target()).get("/api/meta").json()
    assert meta["root"] == str(tmp_path.resolve())
