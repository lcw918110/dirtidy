"""审计产物：JSON 束 + 人类可读 audit.md。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dirtidy.models import write_json


def _fmt_values(values: list[Any], limit: int = 8) -> str:
    items = [str(v) for v in values]
    if len(items) > limit:
        return ", ".join(items[:limit]) + f", …（共 {len(items)} 项）"
    return ", ".join(items)


def write_audit_md(
    path: Path,
    meta: dict[str, Any],
    *,
    quarantined: list[dict],
    candidates: list[dict],
    errors: list[dict],
    protection: list[dict] | None = None,
    protect_policy: list[dict] | None = None,
) -> None:
    """生成可回放的 Markdown 审计摘要。"""
    protection = protection or []
    protect_policy = protect_policy or []
    lines = [
        f"# dirtidy audit — {meta.get('run_id', '')}",
        "",
        f"- root: `{meta.get('root', '')}`",
        f"- started: {meta.get('started_at', '')}",
        f"- rules: {meta.get('rules', '')}",
        f"- quick: {meta.get('quick', False)}",
        f"- 自动隔离命中: {meta.get('auto_hits', len(quarantined))}",
        f"- 被保护拦下: {len(protection)}（其中原本会自动隔离 {sum(1 for p in protection if p.get('blocked_auto'))}）",
        "",
        "## Quarantined",
        "",
    ]
    if not quarantined:
        lines.append("（无）")
    else:
        lines.append("| id | original | rule | size | reason |")
        lines.append("|----|----------|------|------|--------|")
        for e in quarantined:
            lines.append(
                f"| {e.get('id','')} | `{e.get('original_path','')}` | {e.get('rule_id','')} "
                f"| {e.get('size',0)} | {e.get('reason','')} |"
            )

    lines.extend(["", "## Protection", ""])
    if not protect_policy:
        lines.append("（无生效的保护规则）")
    else:
        lines.append("生效的保护规则（命中即永不自动隔离）：")
        lines.append("")
        lines.append("| id | 匹配类型 | 取值 | 原因 |")
        lines.append("|----|----------|------|------|")
        for p in protect_policy:
            lines.append(
                f"| {p.get('id','')} | {p.get('match_type','')} | {_fmt_values(p.get('values') or [])} "
                f"| {p.get('reason','')} |"
            )
    lines.append("")
    if not protection:
        lines.append("本次没有任何规则命中受保护路径。")
    else:
        lines.append("被拦下的路径（**未隔离、未进候选**）：")
        lines.append("")
        lines.append("| path | 想动的规则 | 原本动作 | 保护规则 | 保护原因 |")
        lines.append("|------|------------|----------|----------|----------|")
        for p in protection:
            lines.append(
                f"| `{p.get('path','')}` | {p.get('intended_rule_id','')} "
                f"| {p.get('intended_action','')} | {p.get('protect_rule_id','')} "
                f"| {p.get('protect_reason','')} |"
            )

    lines.extend(["", "## Candidates", ""])
    if not candidates:
        lines.append("（无）")
    else:
        for c in candidates:
            paths = ", ".join(f"`{p}`" for p in c.get("paths", []))
            lines.append(f"- **{c.get('kind')}** ({c.get('rule_id')}): {paths} — {c.get('reason','')}")

    lines.extend(["", "## Errors", ""])
    if not errors:
        lines.append("（无）")
    else:
        for e in errors:
            lines.append(f"- `{e.get('path')}`: {e.get('error')}")
    lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def write_run_bundle(
    run_path: Path,
    *,
    meta: dict[str, Any],
    inventory: list[dict],
    candidates: list[dict],
    quarantined: list[dict],
    errors: list[dict],
    protection: list[dict] | None = None,
    protect_policy: list[dict] | None = None,
) -> None:
    """一次写入 run 目录下的标准产物集合。"""
    protection = protection or []
    protect_policy = protect_policy or []
    write_json(run_path / "meta.json", meta)
    write_json(run_path / "inventory.json", inventory)
    write_json(run_path / "candidates.json", candidates)
    write_json(run_path / "quarantine_manifest.json", quarantined)
    write_json(run_path / "errors.json", errors)
    write_json(run_path / "protection.json", protection)
    write_json(run_path / "protect_policy.json", protect_policy)
    write_audit_md(
        run_path / "audit.md",
        meta,
        quarantined=quarantined,
        candidates=candidates,
        errors=errors,
        protection=protection,
        protect_policy=protect_policy,
    )
