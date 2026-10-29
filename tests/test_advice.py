"""「建议」这一层：把规则动作翻译成用户能直接拿去做判断的话。

回归背景：扫描页原先只写一行「已生成 run」就跳到审阅页；审阅页的「命中规则」列
直接铺 `dep_node_modules`，「本规则已开启直删」回答的是规则配置状态，
候选表的「建议」列干脆全是 `-`。用户站在列表前看不出「这是什么、要不要删」。

这一层的关键不是措辞好不好听，而是**判定只能有一处**：本项目已经四次栽在
「同一个判定写两份」上（见 docs/06）。所以这里既测后端算得对，也测前端没有
偷偷再算一遍。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from dirtidy.advice import (
    ADVICE_LABELS,
    EFFECT_LABELS,
    advice_for,
    describe,
    effect_for,
)
from dirtidy.rules.engine import load_rule_pack
from dirtidy.web.app import create_app

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "sample_workspace"
STATIC = Path(__file__).resolve().parent.parent / "src" / "dirtidy" / "web" / "static"


def _js_function(js: str, name: str) -> str:
    """粗切一个顶格函数体。用于「这段渲染代码里不许出现 X」这类守卫。"""
    start = js.index(f"function {name}(")
    end = js.index("\n}", start)
    return js[start:end]


# -------------------------------- 判定本身 --------------------------------


def test_advice_distinguishes_rebuild_cost() -> None:
    """「建议清理」与「可以清理」不是同义词：一个重建免费，一个要重装/重编译。"""
    assert advice_for(action="auto_quarantine", purge_safe=True) == "safe_clean"
    assert advice_for(action="auto_quarantine", purge_safe=False) == "clean"


def test_advice_never_guesses_for_candidates() -> None:
    """候选一律交人工。这里「猜一个建议」比不给建议更糟。"""
    assert advice_for(action="candidate_only") == "review"
    assert advice_for(action="something_new") == "review"


def test_advice_protect_is_protected() -> None:
    assert advice_for(action="protect") == "protected"


def test_effect_says_whether_it_can_be_undone() -> None:
    """后果必须说清能不能反悔：同一条规则，开没开直删是两回事。"""
    assert effect_for(action="auto_quarantine", purge_safe=True, purge_enabled=False) == "quarantine"
    assert effect_for(action="auto_quarantine", purge_safe=True, purge_enabled=True) == "purge"
    # 没标 purge_safe 的规则，即使误开了直删也不该物理删除
    assert effect_for(action="auto_quarantine", purge_safe=False, purge_enabled=True) == "quarantine"
    assert effect_for(action="candidate_only") == "keep"
    assert effect_for(action="protect") == "keep"


def test_describe_covers_every_label() -> None:
    """describe 一次给全，省得调用方各拼一份措辞。"""
    for action, purge_safe in [
        ("auto_quarantine", True),
        ("auto_quarantine", False),
        ("candidate_only", False),
        ("protect", False),
    ]:
        d = describe(action=action, purge_safe=purge_safe)
        assert d["advice"] in ADVICE_LABELS
        assert d["effect"] in EFFECT_LABELS
        assert d["advice_label"] == ADVICE_LABELS[d["advice"]]
        assert d["effect_label"] == EFFECT_LABELS[d["effect"]]


# ------------------------------ 接口是否带上了 ------------------------------


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    dest = tmp_path / "ws"
    shutil.copytree(FIXTURE_SRC, dest)
    return TestClient(create_app(default_root=dest))


def test_run_api_answers_what_it_is_and_should_i_delete(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    """列表要能回答两个问题：「这是什么」「要不要删」。"""
    run_id = client.post("/api/scan", json={"quick": False, "workers": 2}).json()["run_id"]
    data = client.get(f"/api/runs/{run_id}").json()

    hits = data["pending_auto_hits"]
    assert hits, "夹具里应当有 node_modules / __pycache__ 等命中"
    for h in hits:
        assert h["advice"] in ADVICE_LABELS
        assert h["advice_label"] and h["advice_hint"]
        assert h["effect"] in EFFECT_LABELS
        assert h["effect_label"]
        # 没有中文说明，用户就只能看到内部编号
        assert h["rule_reason"], f"{h['path']} 缺中文说明"

    by_rule = {h["rule_id"]: h for h in hits}
    assert by_rule["dep_python_cache"]["advice"] == "safe_clean"
    assert by_rule["dep_node_modules"]["advice"] == "clean"

    # 候选不再有「没有建议」这回事
    for c in data["candidates"]:
        assert c["advice"] == "review"
        assert c["rule_reason"], f"{c['rule_id']} 的候选缺中文说明"

    # 同一请求内相同路径只计算一次；下一次请求必须重新统计，不能留下过期尺寸。
    from dirtidy.web import app as web_app

    target = tmp_path / "size-test"
    target.mkdir()
    (target / "file").write_text("abc", encoding="utf-8")
    calls = 0
    original = web_app.tree_size

    def counted(path: Path) -> int:
        nonlocal calls
        calls += 1
        return original(path)

    monkeypatch.setattr(web_app, "tree_size", counted)
    cache: dict[str, int] = {}
    first = [{"path": "size-test"}, {"paths": ["size-test"]}]
    web_app._attach_advice(first, root=tmp_path, rules={}, purge_ids=set(), size_cache=cache)
    assert calls == 1 and [e["size_bytes"] for e in first] == [3, 3]
    (target / "file").write_text("abcdef", encoding="utf-8")
    second = [{"path": "size-test"}]
    web_app._attach_advice(second, root=tmp_path, rules={}, purge_ids=set())
    assert calls == 2 and second[0]["size_bytes"] == 6


def test_advice_falls_back_when_rule_pack_missing(tmp_path: Path) -> None:
    """规则包读不到时退化成「需要你判断」，而不是崩掉或瞎猜「建议清理」。"""
    dest = tmp_path / "ws"
    shutil.copytree(FIXTURE_SRC, dest)
    client = TestClient(create_app(default_root=dest))
    run_id = client.post("/api/scan", json={"quick": True, "workers": 2}).json()["run_id"]

    # 把 meta 里的规则包改成一个不存在的名字，模拟「规则包已删除」
    rdir = dest / ".dirtidy" / "runs" / run_id
    meta = json.loads((rdir / "meta.json").read_text(encoding="utf-8"))
    meta["rules"] = "no-such-pack"
    (rdir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")

    data = client.get(f"/api/runs/{run_id}").json()
    for h in data["pending_auto_hits"]:
        assert h["advice"] in ADVICE_LABELS, "规则包缺失时也要给出合法档位"
        assert h["advice"] != "safe_clean", "读不到规则就断言「重建免费」是在瞎猜"


# ------------------------------ 前端不许再算一遍 ------------------------------


def test_advice_is_not_recomputed_in_frontend() -> None:
    """建议只能由后端算。

    前端一旦按 `action` / `purge_safe` 自己判断，规则包一改就会
    「界面说可以清理、实际直接删除」——「界面看起来做了、实际没做」的又一次复现。
    """
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    for fn in ("renderPending", "renderReview", "renderScanSummary", "adviceTag"):
        body = _js_function(js, fn)
        for token in ("auto_quarantine", "candidate_only", "purge_safe"):
            assert token not in body, f"{fn} 里出现了判定依据 {token}，建议被算了两遍"


def test_scan_page_shows_results_instead_of_jumping_away() -> None:
    """扫完就跳走，会让扫描页永远只剩一行「已生成 run」，用户回头找不到结果。"""
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function renderScanSummary" in js
    assert "function scanGroup" in js
    assert "去审阅页逐条确认" in js

    start = js.index('document.getElementById("scanBtn").onclick')
    end = js.index("\n};", start)
    assert 'showTab("review")' not in js[start:end], "扫描完成后不应再强制跳转"


def test_review_tables_speak_user_language() -> None:
    """表头就是用户的问题：这是什么、建议怎么办、动了会怎样。"""
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    review = html.split('id="tab-review"')[1].split('id="tab-quarantine"')[0]
    assert "<th>这是什么</th>" in review
    assert "<th>建议 / 删了怎么办</th>" in review
    assert "<th>占用</th>" in review
    assert "<th>动了会怎样</th>" in review
    assert "<th>为什么在列表里</th>" in review
    assert "<th>命中规则</th>" not in review, "内部规则编号不该再占一整列"
    assert "<th>本规则已开启直删</th>" not in review, "那是规则配置状态，不是「该不该删」"
    # 颜色必须配图例，否则用户只能猜绿的好还是橙的好（WinDirStat 的做法）
    assert 'class="legend"' in review


def test_every_actionable_rule_explains_how_to_recover() -> None:
    """每条会动的规则都要说清「删了怎么恢复」。

    这是市面清理工具里最能让用户敢下手的一条信息：Windows 磁盘清理每类都带一句
    「这些是程序使用的临时信息」，BleachBit 每个 cleaner 都有 description。
    只说「可以清理」等于让用户凭信任点按钮。
    """
    pack = load_rule_pack("default")
    missing = [
        str(r.get("id"))
        for r in pack["rules"]
        if r.get("action") != "protect" and not r.get("recover")
    ]
    assert not missing, f"这些规则没写 recover（删了怎么恢复）：{missing}"


def test_run_api_carries_size_and_recover(client: TestClient) -> None:
    """列表要能排序与权衡：每项带占用大小，以及「删了怎么恢复」。"""
    run_id = client.post("/api/scan", json={"quick": False, "workers": 2}).json()["run_id"]
    data = client.get(f"/api/runs/{run_id}").json()

    hits = data["pending_auto_hits"]
    assert hits
    for h in hits:
        assert h["recover"], f"{h['path']} 没说删了怎么恢复"
        assert h["size_bytes"] >= 0, "占用必须算出来，否则用户没法按大小权衡"
    # 夹具里 node_modules 有内容，全 0 就说明 size 根本没算
    assert any(h["size_bytes"] > 0 for h in hits), "所有命中项大小都是 0，size 没算"


def test_frontend_shows_size_and_recover() -> None:
    js = (STATIC / "app.js").read_text(encoding="utf-8")
    assert "function sizeText" in js
    assert "删了怎么办" in js
    # 行构造抽进了 pendingRow()（排序 / 筛选 / 分组共用一份），占用列在那儿。
    assert "size_bytes" in _js_function(js, "pendingRow")
    # 默认排序键仍是占用降序：用户第一眼要看的是「谁最占地方」。
    assert 'const pendingSort = { key: "size", dir: -1 }' in js
    assert "sortHits(" in _js_function(js, "renderPending")
