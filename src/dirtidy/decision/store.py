"""人工决策的读写与执行（keep / quarantine / archive）。"""

from __future__ import annotations

import posixpath
import shutil
from collections.abc import Sequence
from pathlib import Path

from dirtidy.models import Decision, RuleHit, read_json, resolve_under, write_json
from dirtidy.pipeline import append_protection_records, protect_rules_from_paths
from dirtidy.quarantine.store import (
    drop_covered_descendants,
    plan_quarantine,
    preserve_parent_times,
    quarantine_hits,
)
from dirtidy.rules.engine import effective_protect_rules, load_rule_pack
from dirtidy.rules.protect import compile_protect_rules, evaluate_protect


def _protect_matchers(run_path: Path):
    """按该 run 当时的规则包与 `--protect` 参数编译保护判定器。

    与 `apply_pending_rules` 用**同一套来源**，保证「规则自动隔离」和「人工决策隔离」
    两条执行路径的保护语义完全一致——不一致本身就是可被绕过的一条缝。
    """
    meta_path = run_path / "meta.json"
    meta = read_json(meta_path) if meta_path.exists() else {}
    pack = load_rule_pack(meta.get("rules") or "default")
    extra = protect_rules_from_paths(meta.get("protect") or [])
    return compile_protect_rules(effective_protect_rules(pack, extra))


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


def apply_decisions(
    root: Path,
    run_id: str,
    run_path: Path,
    quarantine_dir: Path,
    *,
    dry_run: bool = False,
    decisions: Sequence[Decision] | None = None,
) -> dict:
    """按 decisions.json 执行：保留计数、归档移动、或转入隔离区。

    所有路径先经 resolve_under 校验必须落在 root 内，越界条目记入 skipped 而不执行，
    避免外部输入的 `..` / 绝对路径把文件搬出目标根。

    **隔离前会用当前生效的保护清单复核**：保护清单的最高优先级语义是「命中即永不
    自动隔离」，这条不因走规则路径还是人工决策路径而不同。被拦下的记入 skipped 并
    写入 protection.json，而不是静默丢弃。`archive` 不拦——它是显式指定目标的归类，
    文件仍在根内可找回，与「清理」语义不冲突。

    dry_run=True 时只做完整校验与冲突检测并报告，不动任何文件；
    同批次内两个决策落到同一归档目标的情况，也会在 dry-run 里被指出。
    """
    # 预览可使用尚未保存的输入；绝不为了预览改写 decisions.json。
    decisions = load_decisions(run_path) if decisions is None else decisions
    manifest_path = run_path / "quarantine_manifest.json"
    matchers = _protect_matchers(run_path)

    def guard(rel: str, is_dir: bool) -> tuple[str, str] | None:
        return evaluate_protect(matchers, rel, is_dir)

    q_hits: list[RuleHit] = []
    archived = 0
    kept = 0
    skipped: list[dict] = []
    blocked: list[dict] = []
    planned_targets: dict[str, str] = {}  # 目标相对路径 → 来源路径（用于同批次冲突检测）

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
            is_dir = src.is_dir()
            rel = posixpath.normpath(str(d.path).replace("\\", "/")).lstrip("/")
            guard_hit = guard(rel, is_dir)
            if guard_hit is not None:
                blocked.append(
                    {
                        "path": rel,
                        "protect_rule_id": guard_hit[0],
                        "protect_reason": guard_hit[1],
                        "intended_rule_id": "decision",
                        "intended_action": "auto_quarantine",
                        "is_dir": is_dir,
                        "blocked_at": "apply_decisions",
                    }
                )
                skipped.append(
                    {
                        "path": d.path,
                        "action": d.action,
                        "reason": f"受保护（{guard_hit[0]}：{guard_hit[1]}），未执行隔离",
                    }
                )
                continue
            q_hits.append(
                RuleHit(
                    path=d.path,
                    rule_id="decision",
                    action="auto_quarantine",
                    reason=d.note or "人工决策隔离",
                    is_dir=is_dir,
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
            if dest.exists() or target in planned_targets:
                holder = planned_targets.get(target, "磁盘上已存在")
                skipped.append(
                    {"path": d.path, "action": d.action, "reason": f"归档目标已存在: {target}（{holder}）"}
                )
                continue
            planned_targets[target] = d.path
            if dry_run:
                archived += 1
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            with preserve_parent_times(root, [src, dest]):
                shutil.move(str(src), str(dest))
            archived += 1

    if dry_run:
        # 与实跑共用 plan_quarantine：人工决策把整个目录标 quarantine 时，
        # 若目录子树内含受保护路径同样必须拦下，预览与实跑不能有分歧。
        executable, dry_blocks = plan_quarantine(root, q_hits, is_protected=guard)
        all_blocked = [*blocked, *dry_blocks]
        return {
            "kept": kept,
            "archived": archived,
            "quarantined": len(executable),
            "blocked_by_protect": len(all_blocked),
            "skipped": skipped,
            "dry_run": True,
            "would_archive": [p for p in planned_targets.values()],
            "would_quarantine": [h.path for h in executable],
            "blocked_paths": [b["path"] for b in all_blocked],
        }

    descendant_blocks: list[dict] = []
    if q_hits:
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        quarantine_hits(
            root,
            run_id,
            q_hits,
            quarantine_dir,
            manifest_path,
            is_protected=guard,
            blocked=descendant_blocks,
        )

    if descendant_blocks:
        blocked.extend(descendant_blocks)
        skipped.extend(
            {
                "path": b["path"],
                "action": "quarantine",
                "reason": f"子树内含受保护路径（{b['protect_reason']}），未执行隔离",
            }
            for b in descendant_blocks
        )

    if blocked:
        append_protection_records(run_path, blocked)

    # 与 plan_quarantine 同源的纯集合运算（不再走一次实盘遍历），保证报数与实搬一致
    blocked_paths = {b["path"] for b in descendant_blocks}
    executed = drop_covered_descendants([h for h in q_hits if h.path not in blocked_paths])

    return {
        "kept": kept,
        "archived": archived,
        "quarantined": len(executed),
        "blocked_by_protect": len(blocked),
        "skipped": skipped,
    }
