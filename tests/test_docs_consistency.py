"""文档一致性守护：docs/04 规则 ID 表 vs 包内置规则包 YAML。

在文档与 YAML 漂移时，此测试应真正失败（真红），而非恒真。
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_04 = REPO_ROOT / "docs" / "04-规则包说明.md"
PACKS_DIR = REPO_ROOT / "src" / "dirtidy" / "rules" / "packs"
DEFAULT_YAML = PACKS_DIR / "default.yaml"
SERVER_WORKSPACE_YAML = PACKS_DIR / "server_workspace.yaml"

# 文档表格 action 简写 → YAML action 全名
ACTION_ALIASES = {
    "auto": "auto_quarantine",
    "candidate": "candidate_only",
}

# docs/04 §3 末段承诺存在的 server_workspace 规则 id
SERVER_WORKSPACE_IDS = ("log_dir", "dep_target_candidate", "dep_vendor")


def _strip_code(s: str) -> str:
    return s.strip().strip("`").strip()


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _parse_default_table(doc_text: str) -> dict[str, str]:
    """解析 docs/04 §3「内置规则 ID」表格，返回 {id: action简写}。"""
    lines = doc_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("## 3."):
            start = i
            break
    assert start is not None, "未找到 docs/04 §3 标题"

    rows: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if line.strip().startswith("## "):
            break
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        cid, _match, action = cells[0], cells[1], cells[2]
        if cid in ("id", "") or set(cid) <= {"-", ":"}:
            continue
        rows[_strip_code(cid)] = _strip_code(action)
    return rows


def test_docs_04_default_ids_match_yaml() -> None:
    doc_ids = _parse_default_table(DOCS_04.read_text(encoding="utf-8"))
    assert doc_ids, "docs/04 §3 表格应至少解析出一条规则行"

    default_rules = _load_yaml(DEFAULT_YAML).get("rules") or []
    yaml_by_id = {r["id"]: r["action"] for r in default_rules}

    # 文档列出的每个 id 都存在于 default.yaml，且 action（映射后）一致
    for cid, action_simplified in doc_ids.items():
        assert cid in yaml_by_id, f"docs/04 列出的 id {cid!r} 不在 default.yaml 中"
        expected = ACTION_ALIASES.get(action_simplified, action_simplified)
        assert yaml_by_id[cid] == expected, (
            f"id {cid!r} 的 action 不一致：文档={action_simplified!r}，YAML={yaml_by_id[cid]!r}"
        )

    # docs/04 未遗漏 default.yaml 中的任何 id（双向一致）
    missing = set(yaml_by_id) - set(doc_ids)
    assert not missing, f"default.yaml 中的 id 未在 docs/04 列出：{sorted(missing)}"


def test_docs_04_server_workspace_ids_exist() -> None:
    server_rules = _load_yaml(SERVER_WORKSPACE_YAML).get("rules") or []
    yaml_ids = {r["id"] for r in server_rules}
    for sid in SERVER_WORKSPACE_IDS:
        assert sid in yaml_ids, f"docs/04 提到的 server_workspace id {sid!r} 不在 server_workspace.yaml 中"
