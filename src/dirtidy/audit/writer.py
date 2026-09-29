"""审计产物：JSON 束 + 人类可读 audit.md。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from dirtidy.models import write_json


def write_audit_md(
    path: Path,
    meta: dict[str, Any],
    *,
    quarantined: list[dict],
    candidates: list[dict],
    errors: list[dict],
) -> None:
    """生成可回放的 Markdown 审计摘要。"""
    lines = [
        f"# dirtidy audit — {meta.get('run_id', '')}",
        "",
        f"- root: `{meta.get('root', '')}`",
        f"- started: {meta.get('started_at', '')}",
        f"- rules: {meta.get('rules', '')}",
        f"- quick: {meta.get('quick', False)}",
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
) -> None:
    """一次写入 run 目录下的标准产物集合。"""
    write_json(run_path / "meta.json", meta)
    write_json(run_path / "inventory.json", inventory)
    write_json(run_path / "candidates.json", candidates)
    write_json(run_path / "quarantine_manifest.json", quarantined)
    write_json(run_path / "errors.json", errors)
    write_audit_md(
        run_path / "audit.md",
        meta,
        quarantined=quarantined,
        candidates=candidates,
        errors=errors,
    )
