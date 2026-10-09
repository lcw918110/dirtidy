"""一次整理运行的编排：扫描 → 规则 → hash 候选 →（可选）隔离 → 写审计产物。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from dirtidy.audit.writer import write_audit_md, write_run_bundle
from dirtidy.hashing.content import find_duplicate_groups, find_extracted_archives
from dirtidy.models import RuleHit, quarantine_root, read_json, run_dir, utc_now_iso, write_json
from dirtidy.quarantine.store import quarantine_hits, sweep_empty_dirs
from dirtidy.rules.engine import load_rule_pack, match_rules
from dirtidy.scan.scanner import scan_tree


def pack_sweeps_empty(pack: dict[str, Any]) -> bool:
    """规则包把空目录定为自动隔离时，隔离后再清「搬走后才空」的父目录。"""
    for rule in pack.get("rules") or []:
        match = rule.get("match") or {}
        if match.get("type") == "empty_dir" and rule.get("action") == "auto_quarantine":
            return True
    return False


def create_run(
    root: Path,
    *,
    rules: str = "default",
    quick: bool = False,
    apply: bool = False,
    workers: int = 4,
    on_progress: Any | None = None,
) -> dict[str, Any]:
    """创建新 run。apply=False 时 auto 命中写入 pending_auto_hits.json，待 apply-rules。"""
    root = root.resolve()
    run_id = utc_now_iso().replace(":", "").replace("+00:00", "Z") + "-" + uuid.uuid4().hex[:6]
    rdir = run_dir(root, run_id)
    qdir = quarantine_root(root, run_id)
    try:
        rdir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise PermissionError(
            f"无法在目标根下创建状态目录 {rdir}: {e}。"
            "目标目录可能只读；可设置环境变量 DIRTIDY_STATE_DIR 指向一个可写目录后重试。"
        ) from e

    items, errors = scan_tree(root, quick=quick, workers=workers, on_progress=on_progress)
    pack = load_rule_pack(rules)
    hits, rule_candidates = match_rules(root, items, pack)

    candidates = list(rule_candidates)
    if not quick:
        candidates.extend(find_duplicate_groups(items))
        candidates.extend(find_extracted_archives(root, items))

    hash_map = {i.path: i.sha256 for i in items}
    quarantined: list[dict] = []
    if apply and hits:
        qdir.mkdir(parents=True, exist_ok=True)
        entries = quarantine_hits(
            root,
            run_id,
            hits,
            qdir,
            rdir / "quarantine_manifest.json",
            known_hashes=hash_map,
        )
        if pack_sweeps_empty(pack):
            entries = sweep_empty_dirs(root, run_id, qdir, rdir / "quarantine_manifest.json")
        quarantined = [e.to_dict() for e in entries]
    else:
        write_json(rdir / "pending_auto_hits.json", [h.to_dict() for h in hits])
        write_json(rdir / "quarantine_manifest.json", [])

    meta = {
        "run_id": run_id,
        "root": str(root),
        "started_at": utc_now_iso(),
        "rules": rules,
        "quick": quick,
        "applied": apply,
    }
    write_run_bundle(
        rdir,
        meta=meta,
        inventory=[i.to_dict() for i in items],
        candidates=[c.to_dict() for c in candidates],
        quarantined=quarantined,
        errors=errors,
    )
    return meta


def apply_pending_rules(root: Path, run_id: str) -> dict[str, Any]:
    """对已有 run 的 pending_auto_hits 执行隔离，并刷新 audit.md。"""
    root = root.resolve()
    rdir = run_dir(root, run_id)
    pending_path = rdir / "pending_auto_hits.json"
    if not pending_path.exists():
        return {"quarantined": 0, "message": "无 pending_auto_hits"}

    hits = [RuleHit(**h) for h in read_json(pending_path)]
    inv = read_json(rdir / "inventory.json") if (rdir / "inventory.json").exists() else []
    hash_map = {i["path"]: i.get("sha256") for i in inv}
    qdir = quarantine_root(root, run_id)
    qdir.mkdir(parents=True, exist_ok=True)
    manifest = rdir / "quarantine_manifest.json"
    entries = quarantine_hits(root, run_id, hits, qdir, manifest, known_hashes=hash_map)
    meta_early = read_json(rdir / "meta.json")
    pack = load_rule_pack(meta_early.get("rules") or "default")
    if pack_sweeps_empty(pack):
        entries = sweep_empty_dirs(root, run_id, qdir, manifest)
    pending_path.unlink(missing_ok=True)

    meta = meta_early
    meta["applied"] = True
    write_json(rdir / "meta.json", meta)
    write_audit_md(
        rdir / "audit.md",
        meta,
        quarantined=[e.to_dict() for e in entries],
        candidates=read_json(rdir / "candidates.json"),
        errors=read_json(rdir / "errors.json") if (rdir / "errors.json").exists() else [],
    )
    return {"quarantined": len(entries), "run_id": run_id}
