"""人工决策的读写与执行（keep / quarantine / archive）。"""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path

from dirtidy.models import Decision, RuleHit, read_json, resolve_under, write_json
from dirtidy.quarantine.store import quarantine_hits


def load_decisions(run_path: Path) -> list[Decision]:
    p = run_path / "decisions.json"
    if not p.exists():
        return []
    return [Decision(**d) for d in read_json(p)]


def save_decisions(run_path: Path, decisions: Sequence[Decision | dict]) -> None:
    out = []
    for d in decisions:
        out.append(d.to_dict() if isinstance(d, Decision) else d)
    write_json(run_path / "decisions.json", out)


def apply_decisions(root: Path, run_id: str, run_path: Path, quarantine_dir: Path) -> dict:
    """按 decisions.json 执行：保留计数、归档移动、或转入隔离区。

    所有路径先经 resolve_under 校验必须落在 root 内，越界条目记入 skipped 而不执行，
    避免外部输入的 `..` / 绝对路径把文件搬出目标根。
    """
    decisions = load_decisions(run_path)
    manifest_path = run_path / "quarantine_manifest.json"
    q_hits: list[RuleHit] = []
    archived = 0
    kept = 0
    skipped: list[dict] = []

    for d in decisions:
        try:
            src = resolve_under(root, d.path)
        except ValueError as e:
            skipped.append({"path": d.path, "action": d.action, "reason": str(e)})
            continue
        if d.action == "keep":
            kept += 1
            continue
        if d.action == "quarantine":
            if not src.exists():
                skipped.append({"path": d.path, "action": d.action, "reason": "源不存在"})
                continue
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
                skipped.append({"path": d.path, "action": d.action, "reason": "源不存在"})
                continue
            # 目标相对路径由 Web/CLI 指定；缺省落到 archive/<文件名>
            target = d.target_relpath or f"archive/{Path(d.path).name}"
            try:
                dest = resolve_under(root, target)
            except ValueError as e:
                skipped.append({"path": d.path, "action": d.action, "reason": f"归档目标非法: {e}"})
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                skipped.append({"path": d.path, "action": d.action, "reason": f"归档目标已存在: {target}"})
                continue
            shutil.move(str(src), str(dest))
            archived += 1

    if q_hits:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        quarantine_hits(root, run_id, q_hits, quarantine_dir, manifest_path)

    return {
        "kept": kept,
        "archived": archived,
        "quarantined": len(q_hits),
        "skipped": skipped,
    }
