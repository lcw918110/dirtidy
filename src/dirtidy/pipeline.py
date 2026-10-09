"""一次整理运行的编排：扫描 → 规则 → hash 候选 →（可选）隔离 → 写审计产物。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from dirtidy.audit.writer import write_audit_md, write_run_bundle
from dirtidy.hashing.content import find_duplicate_groups, find_extracted_archives
from dirtidy.models import RuleHit, quarantine_root, read_json, run_dir, utc_now_iso, write_json
from dirtidy.quarantine.store import quarantine_hits, sweep_empty_dirs
from dirtidy.rules.engine import (
    effective_protect_rules,
    load_rule_pack,
    match_rules,
    protect_policy,
)
from dirtidy.rules.protect import compile_protect_rules, evaluate_protect
from dirtidy.scan.scanner import scan_tree


def pack_sweeps_empty(pack: dict[str, Any]) -> bool:
    """规则包把空目录定为自动隔离时，隔离后再清「搬走后才空」的父目录。"""
    for rule in pack.get("rules") or []:
        match = rule.get("match") or {}
        if match.get("type") == "empty_dir" and rule.get("action") == "auto_quarantine":
            return True
    return False


def protect_rules_from_paths(paths: Sequence[str]) -> list[dict[str, Any]]:
    """把 `--protect a/b` 这类路径转成 protect 规则（含整棵子树）。"""
    rules: list[dict[str, Any]] = []
    for i, raw in enumerate(paths or ()):
        rel = str(raw).strip().strip("/")
        if not rel:
            continue
        rules.append(
            {
                "id": f"cli_protect_{i}",
                "action": "protect",
                "match": {"type": "path_glob", "patterns": [rel, f"{rel}/**"]},
                "reason": "命令行 --protect 指定",
            }
        )
    return rules


def create_run(
    root: Path,
    *,
    rules: str = "default",
    quick: bool = False,
    apply: bool = False,
    workers: int = 4,
    on_progress: Any | None = None,
    protect: Sequence[str] = (),
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

    extra_protect = protect_rules_from_paths(protect)

    items, errors = scan_tree(root, quick=quick, workers=workers, on_progress=on_progress)
    pack = load_rule_pack(rules)
    match = match_rules(root, items, pack, extra_protect_rules=extra_protect)

    hits = match.hits
    candidates = list(match.candidates)
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
        "protect": list(protect or ()),
        "auto_hits": len(hits),
        "protected_blocked": len(match.protected),
        "protected_downgraded": len(match.downgraded),
    }
    write_run_bundle(
        rdir,
        meta=meta,
        inventory=[i.to_dict() for i in items],
        candidates=[c.to_dict() for c in candidates],
        quarantined=quarantined,
        errors=errors,
        protection=[p.to_dict() for p in match.protected],
        protect_policy=match.protect_policy,
    )
    return meta


def _append_protection_records(rdir: Path, records: list[dict]) -> list[dict]:
    """把新发现的「被保护拦下」记录并进 protection.json，保留历史。"""
    path = rdir / "protection.json"
    existing = read_json(path) if path.exists() else []
    if not isinstance(existing, list):
        existing = []
    known = {(r.get("path"), r.get("protect_rule_id")) for r in existing if isinstance(r, dict)}
    merged = list(existing)
    for r in records:
        key = (r.get("path"), r.get("protect_rule_id"))
        if key not in known:
            merged.append(r)
            known.add(key)
    write_json(path, merged)
    return merged


def apply_pending_rules(root: Path, run_id: str) -> dict[str, Any]:
    """对已有 run 的 pending_auto_hits 执行隔离，并刷新 audit.md。

    隔离前会**再用当前生效的保护清单复核一遍**：pending 列表可能是在旧规则包、
    或人工编辑过的状态下生成的，复核可以保证「保护」不会因为中间环节被绕过。
    """
    root = root.resolve()
    rdir = run_dir(root, run_id)
    pending_path = rdir / "pending_auto_hits.json"
    if not pending_path.exists():
        return {"quarantined": 0, "message": "无 pending_auto_hits"}

    hits = [RuleHit(**h) for h in read_json(pending_path)]
    inv = read_json(rdir / "inventory.json") if (rdir / "inventory.json").exists() else []
    hash_map = {i["path"]: i.get("sha256") for i in inv}
    meta_early = read_json(rdir / "meta.json")
    pack = load_rule_pack(meta_early.get("rules") or "default")

    extra = protect_rules_from_paths(meta_early.get("protect") or [])
    matchers = compile_protect_rules(effective_protect_rules(pack, extra))
    kept: list[RuleHit] = []
    blocked: list[dict] = []
    for hit in hits:
        guard = evaluate_protect(matchers, hit.path, hit.is_dir)
        if guard is None:
            kept.append(hit)
            continue
        blocked.append(
            {
                "path": hit.path,
                "protect_rule_id": guard[0],
                "protect_reason": guard[1],
                "intended_rule_id": hit.rule_id,
                "intended_action": "auto_quarantine",
                "is_dir": hit.is_dir,
                "blocked_at": "apply",
            }
        )
    if blocked:
        _append_protection_records(rdir, blocked)

    qdir = quarantine_root(root, run_id)
    qdir.mkdir(parents=True, exist_ok=True)
    manifest = rdir / "quarantine_manifest.json"
    entries = quarantine_hits(root, run_id, kept, qdir, manifest, known_hashes=hash_map)
    if pack_sweeps_empty(pack):
        entries = sweep_empty_dirs(root, run_id, qdir, manifest)
    pending_path.unlink(missing_ok=True)

    meta = meta_early
    meta["applied"] = True
    meta["protected_blocked_at_apply"] = len(blocked)
    write_json(rdir / "meta.json", meta)
    write_audit_md(
        rdir / "audit.md",
        meta,
        quarantined=[e.to_dict() for e in entries],
        candidates=read_json(rdir / "candidates.json"),
        errors=read_json(rdir / "errors.json") if (rdir / "errors.json").exists() else [],
        protection=read_json(rdir / "protection.json") if (rdir / "protection.json").exists() else [],
        protect_policy=read_json(rdir / "protect_policy.json")
        if (rdir / "protect_policy.json").exists()
        else protect_policy(pack, extra),
    )
    return {"quarantined": len(entries), "blocked_by_protect": len(blocked), "run_id": run_id}
