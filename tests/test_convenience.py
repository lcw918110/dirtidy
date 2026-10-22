"""使用便利性：规则中文短名、列表筛选、批量勾选保留、扫描进度、开发热重载、
重复组报账、解释可复制。

这一批改动的共同点是「让界面少让人猜」：规则不再只显示 `dep_node_modules`，
几百项的列表能筛，拿不准的能一键留下，扫描时能看到进度而不是盯着「扫描中…」，
重复组直接说清「保留一份能释放多少」，解释能一键复制发给别人。

每条用例都既测「做了」，也测「没顺手做错」——尤其是
**筛选只改显示**（执行范围仍是全量）与 **批量勾选不碰任何文件**（本项目承诺
「自动只到候选/隔离区为止，执行必须人工」）。
"""

from __future__ import annotations

import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from dirtidy import cli
from dirtidy.advice import ADVICE_HINTS, ADVICE_LABELS, ADVICE_ORDER
from dirtidy.pipeline import create_run
from dirtidy.rules.engine import load_rule_pack
from dirtidy.web.app import _reload_target, create_app

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "sample_workspace"
ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "src" / "dirtidy" / "web" / "static"


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


def test_advice_order_matches_frontend(client: TestClient, monkeypatch) -> None:
    """建议顺序及措辞只由 advice.py 提供，前端不能再复制一份顺序。"""
    response = client.get("/api/meta")
    assert response.status_code == 200
    meta = response.json()
    assert meta["advice_order"] == list(ADVICE_ORDER)
    assert meta["advice_labels"] == ADVICE_LABELS
    assert meta["advice_hints"] == ADVICE_HINTS

    # 调整唯一来源后接口应立即反映变化，而不是恰好相等的另一份常量。
    monkeypatch.setattr("dirtidy.web.app.ADVICE_ORDER", tuple(reversed(ADVICE_ORDER)))
    monkeypatch.setitem(ADVICE_LABELS, "review", "测试建议标签")
    monkeypatch.setitem(ADVICE_HINTS, "review", "测试建议说明")
    changed = client.get("/api/meta").json()
    assert changed["advice_order"] == list(reversed(ADVICE_ORDER))
    assert changed["advice_labels"]["review"] == "测试建议标签"
    assert changed["advice_hints"]["review"] == "测试建议说明"

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assignments = re.findall(r"(?:const|let|var)\s+ADVICE_KINDS\s*=\s*([^;]+);", js)
    assert assignments == ["[]"], "前端不能硬编码建议档位及排序"
    body = _js_function(js, "loadRuns")
    assert 'await api("/api/meta")' in body
    assert "ADVICE_KINDS = meta.advice_order" in body
    assert body.index("ADVICE_KINDS = meta.advice_order") < body.index("await loadRun(")
    assert 'document.getElementById("adviceLegend")' in body
    assert 'document.getElementById("pendingAdvice")' in body
    assert "for (const kind of ADVICE_KINDS)" in body
    assert "text: meta.advice_labels[kind]" in body
    assert "meta.advice_hints[kind]" in body
    assert "value: kind" in body
    assert "adviceSelect.value = pendingFilter.advice" in body

    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert '<p class="legend" id="adviceLegend"></p>' in html
    select = html.split('id="pendingAdvice"')[1].split("</select>")[0]
    assert re.findall(r'<option value="([^"]*)">([^<]*)</option>', select) == [("", "全部")]
    for kind in ADVICE_ORDER:
        assert f'value="{kind}"' not in html
        assert f"advice-{kind}" not in html


def test_protection_uses_reason_and_keeps_ids_in_titles() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = _js_function(js, "renderProtection")
    assert 'text: ` ← ${r.protect_reason ||' in body
    assert 'title: r.protect_rule_id || ""' in body
    assert 'text: p.reason ||' in body
    assert 'title: p.id || ""' in body
    assert 'text: policy.map((p) => p.id)' not in body


def test_shared_styles_have_one_definition() -> None:
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    for selector in ("button.primary", ".lead"):
        assert len(re.findall(re.escape(selector) + r"\s*\{", css)) == 1
    assert "--header-h" not in css


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
    # 行构造已抽进 pendingRow()（排序 / 筛选 / 分组三种视图共用一份），
    # 所以守卫盯的是真正拼路径与规则名的那两个函数。
    for fn in ("pendingRow", "scanGroup"):
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
    assert "schedulePlanRefresh()" in body, "执行条数必须经后端全量 dry-run 核对"
    assert "currentPendingCount = all.length" not in body, "命中数不是实际执行数"
    assert "筛选只影响显示" in body, "界面必须把「筛选不改执行范围」说出来"


def test_filter_survives_rerender() -> None:
    """勾选「保留」会触发重渲染，筛选条件必须跟着留住，否则筛完点一下就回到全部。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "const pendingFilter" in js, "筛选状态不能放在渲染函数里重建"
    assert "const candFilter" in js


# ---------------------------- run 下拉说人话 ----------------------------


def test_run_list_carries_readable_fields(client: TestClient) -> None:
    """run 列表要能回答「这是什么时候扫的、扫出多少」。

    只给 id 的话，下拉框里全是 `2026-10-17T042716+0000-c9874f`，
    用户选不出哪个是刚才那次。
    """
    run_id = client.post("/api/scan", json={"quick": False, "workers": 2}).json()["run_id"]
    runs = client.get("/api/runs").json()["runs"]
    assert runs
    row = next(r for r in runs if r["id"] == run_id)
    assert row["started_at"], "没有开始时间，前端没法显示「10-17 12:27」"
    assert row["auto_hits"] >= 1
    # 候选数必须进 meta.json：否则要显示它就得去读可能几十 MB 的 candidates.json
    assert row["candidates"] >= 0, "候选数没进 meta"
    assert row["applied"] is False


def test_run_dropdown_uses_human_labels() -> None:
    """下拉用人话，不铺裸 run id；完整 id 另外摆在提示行，供命令行复制。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function runLabel(" in js
    assert "function shortTime(" in js
    body = _js_function(js, "loadRuns")
    assert "runLabel(r)" in body, "下拉必须用人话"
    assert "text: r," not in body, "还在把裸 id 当选项文字"
    assert "run ${meta.run_id" in js, "完整 run id 要在页面上露出来，命令行要用"


# ------------------------------ 表头排序 ------------------------------


def test_table_headers_are_sortable() -> None:
    """三列表头可点，当前列有箭头指示方向。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    review = html.split('id="tab-review"')[1].split('id="tab-quarantine"')[0]
    for key in ("path", "advice", "size"):
        assert f'class="sortable" data-sort="{key}"' in review, f"{key} 列不可排序"
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "const pendingSort" in js
    assert "const SORT_DEFAULT_DIR" in js
    assert "function markSortHeaders(" in js
    body = _js_function(js, "sortHits")
    assert "localeCompare" in body, "同值要按路径兜底，否则每次重渲染顺序都在跳"


# ------------------------------ 按建议分组 ------------------------------


def test_grouping_never_drops_unknown_advice() -> None:
    """分组视图不能把「未知档位」的行漏掉。

    分组键如果直接拿 `h.advice` 去比，未知值对不上任何一节 → 行凭空消失，
    而「显示 N / 共 M」的数字还停留在筛选阶段，看起来一切正常。
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function adviceKind(" in js
    assert '"review"' in _js_function(js, "adviceKind"), "未知档位必须兜底成「需要你判断」"
    body = _js_function(js, "renderPending")
    assert "adviceKind(h) === kind" in body, "分组必须按兜底后的档位分"


def test_sort_and_group_are_display_only() -> None:
    """排序与分组都只改显示：执行范围仍按全量报，且不调接口。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for fn in ("sortHits", "pendingGroupRow"):
        body = _js_function(js, fn)
        for token in ("api(", "fetch(", "confirm("):
            assert token not in body, f"{fn} 里出现了 {token}，排序/分组不该动手"
    body = _js_function(js, "renderPending")
    assert "schedulePlanRefresh()" in body, "执行条数必须经后端全量 dry-run 核对"
    assert "currentPendingCount = all.length" not in body, "命中数不是实际执行数"


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


# ------------------------- 重复组：把账算给人看 -------------------------


def test_duplicate_group_reports_per_file_size(tmp_path: Path) -> None:
    """重复组要给逐份大小，不能只给合计。

    「保留一份能释放多少」正是用户决定留哪份的唯一依据；只给合计的话，
    界面只能拿总数除以份数去猜——那是在编数字。
    """
    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    payload = b"x" * 2048
    for rel in ("a.bin", "b.bin", "sub/c.bin"):
        (root / rel).write_bytes(payload)

    client = TestClient(create_app(default_root=root))
    run_id = client.post("/api/scan", json={"quick": False, "workers": 2}).json()["run_id"]
    data = client.get(f"/api/runs/{run_id}").json()

    groups = [c for c in data["candidates"] if c["kind"] == "duplicate_group"]
    assert len(groups) == 1
    g = groups[0]
    assert g["path_sizes"] == {"a.bin": 2048, "b.bin": 2048, "sub/c.bin": 2048}
    assert g["size_bytes"] == 2048 * 3, "合计仍要保留：列表排序按它排"


def test_dup_group_box_states_what_can_be_freed() -> None:
    """面板标题要说清「几份 · 每份多大 · 保留一份可释放多少」。

    原先是「相同内容 hash，需人工选留」一句话——账全留给用户自己算。
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = _js_function(js, "dupGroupBox")

    assert "path_sizes" in body, "要用逐份大小，不能拿合计去除以份数"
    assert "保留一份可释放" in body
    assert "humanSize(" in body
    # 截断的组少算了几份，报出来的数会偏小——宁可不报
    assert "truncated" in body, "截断的组不该给出偏小的「可释放」数"
    assert "dup-size" in body and "dup-state" in body, "每份要带各自大小与当前状态"
    # 新建 run 里所有路径默认决策都是 keep，全勾等于没勾（radio 只留最后一个）
    assert "kept.length === 1" in body
    # 只改决策、只重绘；执行仍要用户去点「执行候选决策」
    assert "api(" not in body and "fetch(" not in body
    assert "apply-decisions" not in body
    assert "其余标记隔离" in body
    assert "dupGroupBox(c)" in _js_function(js, "renderReview")


def test_dup_state_distinguishes_undecided_from_keep() -> None:
    """没进过 decisions 的行是「未表态」，不能显示成「保留」。

    默认决策就是保留，但把它画成「保留」会让人以为这组已经选好了。
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = _js_function(js, "dupStateText")
    assert "未表态" in body
    assert "DECISION_LABELS" in body, "动作要翻人话，不能直接铺 quarantine"


def test_dup_group_pick_frees_exactly_what_ui_promised(tmp_path: Path) -> None:
    """界面承诺「保留一份可释放 X」，执行后就该正好移走 X。

    这条是「界面说的」与「实际做的」对账：`可释放 = 合计 − 被保留那份的大小`。
    数字对不上说明要么算错、要么执行的不是用户选的那一份。
    """
    from dirtidy.models import quarantine_root

    root = tmp_path / "ws"
    (root / "sub").mkdir(parents=True)
    payload = b"y" * 4096
    for rel in ("a.bin", "b.bin", "sub/c.bin"):
        (root / rel).write_bytes(payload)

    client = TestClient(create_app(default_root=root))
    run_id = client.post("/api/scan", json={"quick": False, "workers": 2}).json()["run_id"]
    data = client.get(f"/api/runs/{run_id}").json()
    g = [c for c in data["candidates"] if c["kind"] == "duplicate_group"][0]

    keep = "a.bin"
    promised = g["size_bytes"] - g["path_sizes"][keep]
    assert promised == 4096 * 2

    # 模拟界面「其余标记隔离」写进去的 decisions
    decs = [
        {
            "path": p,
            "action": "keep" if p == keep else "quarantine",
            "target_relpath": "",
            "note": "duplicate pick",
        }
        for p in g["paths"]
    ]
    client.put(f"/api/runs/{run_id}/decisions", json={"decisions": decs})
    out = client.post(f"/api/runs/{run_id}/apply-decisions").json()

    assert out["quarantined"] == 2, out
    assert (root / keep).exists(), "选中的那份必须留在原处"
    qdir = quarantine_root(root, run_id)
    moved = [p for p in qdir.rglob("*") if p.is_file()]
    assert sum(p.stat().st_size for p in moved) == promised, (
        "实际移走的字节数与界面承诺的「可释放」不一致"
    )


# ------------------------- 解释弹窗：能复制 -------------------------


def test_explain_dialog_offers_copy_button() -> None:
    """排查时要把结论发给别人，手选整段很别扭——给个复制按钮。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    assert 'id="explainCopy"' in html
    assert 'id="explainBody"' in html, "复制的是解释正文，不是别的"
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert 'getElementById("explainCopy").onclick' in js, "按钮要有绑定，不然是个摆设"


def test_copy_explain_falls_back_when_clipboard_unavailable() -> None:
    """剪贴板 API 只在 https / localhost 下可用（局域网 IP 打开网页就没有），
    必须留一条兜底，否则按钮在这类环境下永远失败。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    body = _js_function(js, "copyExplain")

    assert "clipboard.writeText" in body
    assert "execCommand" in body, "没有兜底的话，非安全上下文下复制必然失败"
    assert "加载中" in body, "正文还没回来时不该把「加载中…」复制出去"
    # 失败要有话说，不能静默
    assert "复制失败" in body


# ---------------------------- 视觉改造的守卫 ----------------------------
#
# 这一组守的是「样式表还是不是一个有纪律的样式表」。
# 起因：改造前 447 行里有 36 处硬编码色值（29 个不同值）、只有 7 个变量。同一语义
# （比如「危险红」）在五个地方各写一遍，改一处就会漂——而漂掉的那处
# 往往正好是「彻底删除」按钮，后果不可逆。


def test_styles_have_no_bare_colors_outside_root() -> None:
    """:root 之外不许出现裸色值。

    要新颜色就加 token。否则「同一语义两种颜色」必然发生，而这个项目
    反复栽在「同一个判定写两份」上——颜色只是它的外观版本。
    """
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert css.lstrip().startswith("/*"), "样式表应以说明性注释开头，先讲组织方式"
    root_end = css.index("}", css.index(":root"))
    body = css[root_end + 1 :]

    hexes = re.findall(r"#[0-9a-fA-F]{3,8}\b", body)
    rgbs = re.findall(r"\brgba?\(", body)
    assert not hexes, f":root 之外还有裸十六进制色值：{hexes}"
    assert not rgbs, f":root 之外还有裸 rgb()/rgba()：{len(rgbs)} 处"


def test_styles_define_the_tokens_they_use() -> None:
    """用到的每个 var(--x) 都必须有定义。

    拼错的变量名不会报错，只会静默失效——颜色退回继承值，
    看起来像「样式没生效」，很难查。
    """
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    root = css[: css.index("}", css.index(":root"))]
    defined = set(re.findall(r"(--[\w-]+)\s*:", root))
    used = set(re.findall(r"var\((--[\w-]+)", css))
    missing = used - defined
    assert not missing, f"这些变量用了但没定义：{sorted(missing)}"


def test_styles_have_no_unused_tokens() -> None:
    """定义了的 token 必须真的有人用。

    反过来的漏洞：`--shadow` / `--sp-6` 这种「定义了但没人引用」的变量
    会让人以为某一档间距或阴影已经在用，于是新写样式时照着它抄，实际
    渲染毫无变化——排查方向会被彻底带偏。要么用起来，要么删掉。
    """
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    root = css[: css.index("}", css.index(":root"))]
    defined = set(re.findall(r"(--[\w-]+)\s*:", root))
    used = set(re.findall(r"var\((--[\w-]+)", css))
    unused = defined - used
    assert not unused, f"这些 token 定义了但无人使用，应删除或接入：{sorted(unused)}"


def test_every_tab_uses_one_page_head_shape() -> None:
    """每个分页的首屏都长同一个样子：`.page-head` + `.page-head-main`。

    统一结构比统一措辞重要——位置一致，用户换页时眼睛不用重新找
    「这页是干什么的」。缺陷背景：改造前有的页是 `h2 + p.hint`，
    有的页直接一张表单打头，切换时标题忽高忽低、引导语时有时无。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    # 先抹掉 HTML 注释：分页开头的说明性注释是允许的，不算「内容」。
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    sections = re.findall(r'<section class="tab" id="tab-(\w+)">(.*?)</section>', html, re.S)
    assert len(sections) >= 8, "分页数量不对，切分可能失败"

    missing = []
    for name, body in sections:
        # 页头必须是这一页的第一个元素：中间不许先塞表单或表格，
        # 否则切页时标题忽高忽低——那正是改造前的样子。
        assert body.lstrip().startswith('<div class="page-head"'), (
            f"{name} 页的第一个元素不是页头：{body.strip()[:60]}"
        )
        if 'class="page-head-main"' not in body:
            missing.append(name)
    assert not missing, f"这些页的页头缺 .page-head-main：{missing}"


def test_page_lead_is_one_sentence_not_a_paragraph() -> None:
    """每个分页正好一句引导语，长说明收进 details。

    回归背景：审阅页原先首屏是 4 行引导语 + 3 段说明，用户滚两屏才看到表格。
    说明本身有用，但它的位置不对——先给结论，细节放可展开处。

    注意这里按**分页**统计而不是全文统计：只查总数的话，删掉一页的引导语
    仍然能凑够数量，守卫就白写了（这条自己去踩过一次）。
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    sections = re.findall(r'<section class="tab" id="tab-(\w+)">(.*?)</section>', html, re.S)
    assert len(sections) >= 8

    problems = []
    for name, body in sections:
        leads = re.findall(r'<p class="lead">(.*?)</p>', body, re.S)
        if len(leads) != 1:
            problems.append(f"{name} 有 {len(leads)} 句引导语（应恰好 1 句）")
            continue
        if leads[0].count("。") > 2:
            problems.append(f"{name} 的引导语超过三句，应把细节收进 details")
    assert not problems, "；".join(problems)

    # 长说明必须真的存在 details 里，而不是被删掉了
    assert html.count('class="page-note"') >= 4, "长说明应保留在 details.page-note 里"


def test_slow_buttons_report_busy_state() -> None:
    """会真动文件、或要等一阵的按钮，必须进忙态。

    回归背景：扫描按钮原先只 `disabled = true`，文案仍是「开始扫描」，
    用户看不出「在跑」，只能靠底部状态条猜；而重复点「清空隔离区」
    会把刚搬进去的东西全删掉——这些按钮必须点一下就点不动。
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for fn in ("busyOn", "busyOff", "withBusy"):
        assert f"function {fn}(" in js, f"缺少 {fn}"

    # 忙态要真的禁用 + 无障碍语义 + 文案替换
    body = _js_function(js, "busyOn")
    for token in ('aria-busy', "disabled = true", "dataset.busyLabel"):
        assert token in body, f"busyOn 里缺 {token}"

    # 收尾必须能被渲染接管：执行完 loadRun 会重画按钮文案与 disabled，
    # 忙态若强行还原，就会出现「文案说无可执行项、按钮却亮着可点」。
    off = _js_function(js, "busyOff")
    assert "if (takenOver) return;" in off, (
        "busyOff 必须在文案已被渲染改过时提前返回，不能无条件还原"
    )
    assert "removeAttribute" in off, "aria-busy 必须清掉，否则读屏会一直说「忙」"

    # 每个会动文件的按钮都要走忙态
    for btn in (
        "scanBtn",
        "applyRulesBtn",
        "applyBtn",
        "saveBtn",
        "purgeSelBtn",
        "purgeAllBtn",
        "restoreSelBtn",
        "restoreAllBtn",
        "watchStartBtn",
        "watchStopBtn",
    ):
        assert btn in js, f"找不到按钮 {btn}"
    assert js.count("withBusy(") + js.count("busyOn(") >= 8, "多数慢动作按钮没套忙态"


def test_candidate_decision_controls_are_styled() -> None:
    """候选表的「决策」下拉与归档路径框必须有样式。

    这两个控件原先完全没有对应规则，走全局默认：下拉被挤窄到只露出
    「保留」两个字，归档框的 placeholder 也看不全。而「决策」列恰恰是
    这一页唯一真正写状态的地方——它比旁边任何一列都该好点。
    静态检查与接口测试都看不出这个，只有渲染出来才知道。
    """
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    assert re.search(r"select\.act\s*\{", css), "select.act 没有样式"
    assert re.search(r"input\.tgt\s*\{", css), "input.tgt 没有样式"

    act = css[css.index("select.act {") :].split("}", 1)[0]
    assert "min-width" in act, "决策下拉要给下限宽度，否则选项文字被截断"

    js = (STATIC / "app.js").read_text(encoding="utf-8")
    # 类名必须真的是这两个，否则样式挂空
    assert 'cls: "act"' in js
    assert 'cls: "tgt"' in js


def test_mobile_layout_keeps_tables_scrollable() -> None:
    """窄屏下表格必须横向可滚，而不是挤成竖排单字。"""
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    narrow = css[css.index("@media (max-width: 720px)") :]
    assert "overflow-x: auto" in narrow, "窄屏表格要横向滚动"
    assert "grid-template-columns: 1fr" in narrow, "窄屏卡片要单列"


def test_reduced_motion_covers_the_spinner_too() -> None:
    """开了「减少动态效果」时，按钮的转圈也要停——否则这个设置形同虚设。"""
    css = (STATIC / "styles.css").read_text(encoding="utf-8")
    block = css[css.index("@media (prefers-reduced-motion: reduce)") :]
    assert 'button[aria-busy="true"]::after' in block, "转圈也要尊重减少动态效果"


def test_layout_audit_script_waits_for_real_rows() -> None:
    """布局审计脚本必须等到表格**真有行**再量。

    背景：这个脚本曾经一直假绿——它量的是页面加载完成前的空壳，
    表格 0 行，于是无论换什么样本都「全部通过」。踩过两次才定位到：
    ① 探针里的 `const name` 遮蔽外层 `for (const name of tabs)`，
       TDZ 报错让整个测量脚本挂掉，输出恒为空；
    ② 即使脚本不报错，`boot()` 也没把 run 内容加载完，表格仍是空的。

    所以守卫两条：探针必须**等**表格出现行；量完必须**报**出行数，
    审阅页 0 行直接判失败——宁可报红，也不能给一个空壳开绿灯。
    """
    src = (ROOT / "scripts" / "ui_layout_audit.py").read_text(encoding="utf-8")
    assert "await new Promise" in src, "探针必须异步等待表格渲染完成"
    assert "tbody tr" in src, "探针必须以表格行作为「已加载」的判据"
    assert "rowCount" in src, "量完必须报出每个分页的行数"
    assert "审阅页表格 0 行" in src, "审阅页 0 行必须判失败，不能放行空壳"


def test_extreme_sample_actually_fills_the_table() -> None:
    """极端样本必须真能灌满表格，否则「极端测试」等于没测。

    踩过的坑：① 全放在 `node_modules` 下 → 被一条规则吃掉，只剩 1 条命中；
    ② 目录名写成 `build1` → `build_dir_candidate` 匹配的是精确名 `build`，
    一个都匹配不上。两种情况下表格都是空的，测出来的「全部通过」毫无意义。

    守卫分两层：**布局层**（直接看文件系统，绕不过去）与**扫描层**
    （看真扫出来多少行、多少规则、多长的路径）。
    """
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        from _extreme_workspace import build_extreme_workspace  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    with tempfile.TemporaryDirectory() as td:
        work = Path(td) / "ws"
        build_extreme_workspace(work)

        # --- 布局层：顶层必须有多个独立条目，且存在精确名为 build 的目录 ---
        top_dirs = [p.name for p in work.iterdir() if p.is_dir()]
        assert len(top_dirs) >= 5, f"顶层只有 {top_dirs}，样本没铺开"
        builds = [p for p in work.rglob("build") if p.is_dir() and "node_modules" not in p.parts]
        assert builds, "没有名为 build 的目录——build_dir_candidate 匹配的是精确名"
        assert any(p.parent != work for p in builds), "build 目录全挤在顶层了"

        meta = create_run(work, rules="default", quick=False, apply=False)
        # 必须在临时目录还在的时候取数据：run 状态就落在 work 下面，
        # 目录一删就查不到 run 了。
        app = create_app(default_root=work, default_run_id=meta["run_id"])
        with TestClient(app) as client:
            data = client.get(f"/api/runs/{meta['run_id']}").json()

    assert "meta" in data, f"拿不到 run 数据：{data}"
    assert data["meta"]["auto_hits"] >= 10, (
        f"极端样本只命中 {data['meta']['auto_hits']} 条，命中表灌不满"
    )
    assert data["meta"]["candidates"] >= 20, (
        f"极端样本只有 {data['meta']['candidates']} 条候选，候选表灌不满"
    )
    # 必须覆盖多条不同规则：只命中一条说明整棵树被一条规则吃掉了
    rule_ids = {c["rule_id"] for c in data["candidates"]}
    assert len(rule_ids) >= 2, f"候选只来自 {rule_ids}，样本没铺开"
    longest = max(len(p) for c in data["candidates"] for p in c["paths"])
    assert longest > 150, f"最长路径只有 {longest} 字符，超长路径没进样本"

