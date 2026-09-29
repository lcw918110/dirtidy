"""人工决策的读写与执行（keep / quarantine / archive）。"""

from __future__ import annotations

import shutil
from pathlib import Path

from dirtidy.models import Decision, RuleHit, read_json, write_json
from dirtidy.quarantine.store import quarantine_hits


def load_decisions(run_path: Path) -> list[Decision]:
    p = run_path / "decisions.json"
    if not p.exists():
        return []
    return [Decision(**d) for d in read_json(p)]


def save_decisions(run_path: Path, decisions: list[Decision | dict]) -> None:
    out = []
    for d in decisions:
        out.append(d.to_dict() if isinstance(d, Decision) else d)
    write_json(run_path / "decisions.json", out)


def apply_decisions(root: Path, run_id: str, run_path: Path, quarantine_dir: Path) -> dict:
    """按 decisions.json 执行：保留计数、归档移动、或转入隔离区。"""
    decisions = load_decisions(run_path)
    manifest_path = run_path / "quarantine_manifest.json"
    q_hits: list[RuleHit] = []
    archived = 0
    kept = 0

    for d in decisions:
        src = root / d.path
        if d.action == "keep":
            kept += 1
            continue
        if d.action == "quarantine":
            if src.exists():
                q_hits.append(
                    RuleHit(
                        path=d.path,
                        rule_id="decision",
                        action="auto_quarantine",
                        reason=d.note or "人工决策隔离",
                        is_dir=src.is_dir(),
                    )
                )
            continue
        if d.action == "archive":
            if not src.exists():
                continue
            # 目标相对路径由 Web/CLI 指定；缺省落到 archive/<文件名>
            target = d.target_relpath or f"archive/{Path(d.path).name}"
            dest = root / target
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                continue
            shutil.move(str(src), str(dest))
            archived += 1

    if q_hits:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        quarantine_hits(root, run_id, q_hits, quarantine_dir, manifest_path)

    return {"kept": kept, "archived": archived, "quarantined": len(q_hits)}
