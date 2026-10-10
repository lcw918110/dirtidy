"""一次整理运行的编排：扫描 → 规则 → hash 候选 →（可选）隔离 → 写审计产物。"""

from __future__ import annotations

import uuid
from collections.abc import Collection, Sequence
from pathlib import Path
from typing import Any

from dirtidy.audit.writer import write_audit_md, write_run_bundle
from dirtidy.hashing.content import (
    archive_member_sizes,
    candidate_duplicate_sizes,
    find_duplicate_groups,
    find_extracted_archives,
    probe_archives,
)
from dirtidy.models import (
    RuleHit,
    iter_json_array,
    quarantine_root,
    read_json,
    run_dir,
    select_hashes,
    utc_now_iso,
    write_json,
)
from dirtidy.quarantine.store import (
    plan_quarantine,
    purge_entries,
    quarantine_hits,
    sweep_empty_dirs,
)
from dirtidy.rules.engine import (
    effective_protect_rules,
    load_rule_pack,
    match_rules,
    protect_policy,
)
from dirtidy.rules.protect import compile_protect_rules, evaluate_protect
from dirtidy.scan.scanner import hash_items, scan_tree


def protect_guard(pack: dict[str, Any], extra: list[dict[str, Any]]):
    """返回保护判定器：命中给出 (规则 ID, 原因)，否则 None。

    全项目**只有这一个**保护判定器来源。拦截受保护后代时要把规则 ID 与原因写进
    protection.json 供审计追溯，因此返回完整元组而不是 bool。
    """
    matchers = compile_protect_rules(effective_protect_rules(pack, extra))
    return lambda rel, is_dir: evaluate_protect(matchers, rel, is_dir)


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

    # 先只扫元数据（路径 / size / mtime），不算 hash——要等归档成员解析出来，
    # 才知道磁盘上哪些文件真的需要 hash（见下面 sizes 的构造）。
    items, errors = scan_tree(root, quick=True, workers=workers, on_progress=on_progress)
    pack = load_rule_pack(rules)
    match = match_rules(root, items, pack, extra_protect_rules=extra_protect)

    hits = match.hits
    candidates = list(match.candidates)
    if not quick:
        # 归档解析提到 hash 之前：成员的「解压后字节数」决定磁盘上哪些文件值得算 hash。
        probes = probe_archives(root, items)
        # 只算「size 出现 ≥2 次」与「size 命中某归档成员」的文件。重复组要求同 size，
        # 归档残留对照也要求同 size（同内容必然同字节数），因此没被算到的文件
        # 既不可能重复、也不可能与归档成员相同——结论与全量算一致。见 docs/07。
        sizes = candidate_duplicate_sizes(items) | archive_member_sizes(probes)
        errors.extend(hash_items(root, items, sizes=sizes, workers=workers, on_progress=on_progress))
        candidates.extend(find_duplicate_groups(items))
        candidates.extend(find_extracted_archives(root, items, probes=probes))

    hash_map = select_hashes(items, {h.path for h in hits})
    guard = protect_guard(pack, extra_protect)
    quarantined: list[dict] = []
    #: 命中目录因子树内含受保护路径而被拦下的记录。它们既不是「规则没命中」也不是
    #: 「用户保留」，必须与 match.protected 一起进审计，否则用户看到目录没被搬走却查不到原因。
    descendant_blocks: list[dict] = []
    if apply and hits:
        qdir.mkdir(parents=True, exist_ok=True)
        entries = quarantine_hits(
            root,
            run_id,
            hits,
            qdir,
            rdir / "quarantine_manifest.json",
            is_protected=guard,
            known_hashes=hash_map,
            blocked=descendant_blocks,
        )
        if pack_sweeps_empty(pack):
            entries = sweep_empty_dirs(
                root, run_id, qdir, rdir / "quarantine_manifest.json", is_protected=guard
            )
        quarantined = [e.to_dict() for e in entries]
    else:
        write_json(rdir / "pending_auto_hits.json", [h.to_dict() for h in hits])
        write_json(rdir / "quarantine_manifest.json", [])

    protection = [p.to_dict() for p in match.protected]
    protection.extend(descendant_blocks)

    meta = {
        "run_id": run_id,
        "root": str(root),
        "started_at": utc_now_iso(),
        "rules": rules,
        "quick": quick,
        "applied": apply,
        "protect": list(protect or ()),
        "auto_hits": len(hits),
        # 命中路径**自身**受保护（扫描阶段即可判定）
        "protected_blocked": len(match.protected),
        # 命中目录因子树内含受保护路径被整条拦下。只有真正执行隔离时才知道
        # （需要实盘遍历子树），因此单列一项而不是并进上一项——否则
        # `scan` 与 `scan --apply` 会为同一棵树报出不同的 protected_blocked。
        "protected_blocked_by_descendant": len(descendant_blocks),
        "protected_downgraded": len(match.downgraded),
    }
    write_run_bundle(
        rdir,
        meta=meta,
        # 生成器：交给写盘逻辑流式消费，不再先构造一份等长的 dict 列表
        inventory=(i.to_dict() for i in items),
        candidates=[c.to_dict() for c in candidates],
        quarantined=quarantined,
        errors=errors,
        protection=protection,
        protect_policy=match.protect_policy,
    )
    return meta


def append_protection_records(rdir: Path, records: list[dict]) -> list[dict]:
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


def append_purged_records(rdir: Path, records: list[dict]) -> list[dict]:
    """把「隔离后立即物理删除」的记录并进 purged_directly.json，保留历史。

    单独留一份审计是必要的：这些文件已经**不在磁盘上了**，隔离清单里也不会再有它们，
    不另记一笔，事后就完全查不到「这条是谁删的、什么时候删的」。
    """
    path = rdir / "purged_directly.json"
    existing = read_json(path) if path.exists() else []
    if not isinstance(existing, list):
        existing = []
    known = {r.get("id") for r in existing if isinstance(r, dict)}
    merged = list(existing)
    for r in records:
        if r.get("id") not in known:
            merged.append(r)
            known.add(r.get("id"))
    write_json(path, merged)
    return merged


def apply_pending_rules(
    root: Path,
    run_id: str,
    *,
    dry_run: bool = False,
    keep: Collection[str] = (),
    purge_rule_ids: Collection[str] = (),
) -> dict[str, Any]:
    """对已有 run 的 pending_auto_hits 执行隔离，并刷新 audit.md。

    隔离前会**再用当前生效的保护清单复核一遍**：pending 列表可能是在旧规则包、
    或人工编辑过的状态下生成的，复核可以保证「保护」不会因为中间环节被绕过。

    dry_run=True 时只报告「会搬走哪些、哪些被保护拦下」，不改动任何文件与清单。

    keep 是「人工保留」的路径集合（Web 审阅页勾选、或调用方指定）。这些路径
    本轮不隔离，并登记进 protection.json（protect_rule_id=user_kept）以便审计追溯
    —— 「用户不想动它」和「内置清单保护它」在结果上是同一件事。

    purge_rule_ids 是「隔离后立即物理删除」的规则 id 集合（默认空 = 只隔离）。
    走的是同一条路径：**先搬进隔离区、再删**，而不是规则直接 unlink。这样保护判定
    与清单写入一个环节都不少，删除明细另记 purged_directly.json。命中项仍会先
    经过 `keep` 与保护复核——「直删」不会成为绕过保护的缝。
    """
    root = root.resolve()
    rdir = run_dir(root, run_id)
    pending_path = rdir / "pending_auto_hits.json"
    if not pending_path.exists():
        return {"quarantined": 0, "message": "无 pending_auto_hits"}

    hits = [RuleHit(**h) for h in read_json(pending_path)]

    keep_set = {str(p).strip().strip("/") for p in (keep or ()) if str(p).strip()}
    if keep_set:
        # 保留一个**目录**时，它子树内的命中也不该动。否则：子项被搬走 → 目录变空
        # → `sweep_empty_dirs` 再把空壳收走。用户勾的「保留」等于没生效，而且事后
        # 能在隔离区里找到子项、却找不到那个目录，很难向人解释。
        keep_set = keep_set | {
            child.path
            for hit in hits
            if hit.is_dir and hit.path in keep_set
            for child in hits
            if child.path.startswith(hit.path + "/")
        }

    purge_set = {str(r) for r in (purge_rule_ids or ()) if str(r).strip()}
    user_kept: list[dict] = []
    if keep_set:
        survivors: list[RuleHit] = []
        for h in hits:
            # 反方向也要挡：保留项落在某个命中目录内部时，那个目录本轮同样不能动
            # ——搬走父目录等于把保留项一起搬走。
            if h.path in keep_set or any(k.startswith(h.path + "/") for k in keep_set):
                user_kept.append(
                    {
                        "path": h.path,
                        "protect_rule_id": "user_kept",
                        "protect_reason": "人工保留（审阅时选择不动它）",
                        "intended_rule_id": h.rule_id,
                        "intended_action": "auto_quarantine",
                        "is_dir": h.is_dir,
                        "blocked_at": "user_keep",
                    }
                )
            else:
                survivors.append(h)
        hits = survivors

    inv_path = rdir / "inventory.json"
    wanted = {h.path for h in hits}
    hash_map: dict[str, str | None] = {}
    if inv_path.exists():
        # 流式读 inventory，只留下待隔离条目的 hash，不再整份 json.loads
        for row in iter_json_array(inv_path):
            p = row.get("path")
            if p in wanted:
                hash_map[p] = row.get("sha256")
                if len(hash_map) == len(wanted):
                    break
    meta_early = read_json(rdir / "meta.json")
    pack = load_rule_pack(meta_early.get("rules") or "default")

    extra = protect_rules_from_paths(meta_early.get("protect") or [])
    guard = protect_guard(pack, extra)
    kept: list[RuleHit] = []
    blocked: list[dict] = []
    for hit in hits:
        guard_hit = guard(hit.path, hit.is_dir)
        if guard_hit is None:
            kept.append(hit)
            continue
        blocked.append(
            {
                "path": hit.path,
                "protect_rule_id": guard_hit[0],
                "protect_reason": guard_hit[1],
                "intended_rule_id": hit.rule_id,
                "intended_action": "auto_quarantine",
                "is_dir": hit.is_dir,
                "blocked_at": "apply",
            }
        )

    if dry_run:
        # 与实跑共用 plan_quarantine：预览的条数必须与真实产物一致，
        # 包括「子树内含受保护路径而整条被拦下」和「被目录覆盖的后代」这两类。
        executable, dry_blocks = plan_quarantine(root, kept, is_protected=guard)
        all_blocked = [*blocked, *dry_blocks]
        would_purge = [h for h in executable if h.rule_id in purge_set]
        return {
            "dry_run": True,
            "run_id": run_id,
            "would_quarantine": len(executable),
            "paths": [h.path for h in executable],
            "would_purge": len(would_purge),
            "would_purge_paths": [h.path for h in would_purge],
            "blocked_by_protect": len(all_blocked),
            "blocked_paths": [b["path"] for b in all_blocked],
            "kept_by_user": len(user_kept),
            "kept_paths": [u["path"] for u in user_kept],
        }

    if blocked or user_kept:
        append_protection_records(rdir, [*blocked, *user_kept])

    qdir = quarantine_root(root, run_id)
    qdir.mkdir(parents=True, exist_ok=True)
    manifest = rdir / "quarantine_manifest.json"
    # 记下本轮开始前清单里已有的 id：稍后判断「哪些是这一轮新搬进去的」，
    # 直删只作用于新条目，不动历史遗留。
    before_ids = {
        str(e.get("id"))
        for e in (read_json(manifest) if manifest.exists() else [])
        if isinstance(e, dict)
    }
    descendant_blocks: list[dict] = []
    entries = quarantine_hits(
        root,
        run_id,
        kept,
        qdir,
        manifest,
        known_hashes=hash_map,
        is_protected=guard,
        blocked=descendant_blocks,
    )
    if descendant_blocks:
        # 目录命中因子树内含受保护路径而被拦下——与直接命中保护同样是「保护生效」，
        # 必须留痕，否则用户看到目录没被搬走却查不到原因。
        blocked.extend(descendant_blocks)
        append_protection_records(rdir, descendant_blocks)
    if pack_sweeps_empty(pack):
        entries = sweep_empty_dirs(
            root, run_id, qdir, manifest, is_protected=guard
        )
    pending_path.unlink(missing_ok=True)

    # 「隔离后立即物理删除」：搬完再删，保护判定与清单写入一个环节都不少。
    # 只处理**本轮新搬进去的**条目——清单里可能还躺着历史条目，规则 id 恰好
    # 相同的话不该被这一轮顺手删掉，那是另一件事（界面上的「彻底删除」负责）。
    purged: list[str] = []
    if purge_rule_ids:
        purge_set = {str(r) for r in purge_rule_ids}
        fresh = [e for e in entries if e.id not in before_ids and e.rule_id in purge_set]
        if fresh:
            purge_entries(root, manifest, entry_ids={e.id for e in fresh})
            append_purged_records(rdir, [e.to_dict() for e in fresh])
            fresh_ids = {e.id for e in fresh}
            entries = [e for e in entries if e.id not in fresh_ids]
            purged = [e.original_path for e in fresh]

    meta = meta_early
    meta["applied"] = True
    meta["protected_blocked_at_apply"] = len(blocked)
    meta["purged_directly"] = len(purged)
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
    return {
        "quarantined": len(entries),
        "purged_directly": len(purged),
        "purged_paths": purged,
        "blocked_by_protect": len(blocked),
        "kept_by_user": len(user_kept),
        "run_id": run_id,
    }
