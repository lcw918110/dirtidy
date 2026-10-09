"""隔离区：移动（非删除）、还原、彻底清除。

默认只 shutil.move 到 `.dirtidy/quarantine/<run_id>/`，保留相对路径结构以便还原。
彻底清除必须走 purge，且 CLI 要求 --yes。
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

from dirtidy.models import QuarantineEntry, RuleHit, read_json, resolve_under, utc_now_iso, write_json
from dirtidy.scan.scanner import file_sha256


def _safe_dest(quarantine_dir: Path, original_rel: str) -> Path:
    """在隔离区内生成不冲突的目标路径；冲突时加 .__qN 后缀。

    先经 resolve_under 校验，保证隔离目标不会因 `..` 逃出隔离目录。
    """
    dest = resolve_under(quarantine_dir, original_rel)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest
    stem = dest.name
    parent = dest.parent
    i = 1
    while True:
        candidate = parent / f"{stem}.__q{i}"
        if not candidate.exists():
            candidate.parent.mkdir(parents=True, exist_ok=True)
            return candidate
        i += 1


def quarantine_hits(
    root: Path,
    run_id: str,
    hits: list[RuleHit],
    quarantine_dir: Path,
    manifest_path: Path,
    *,
    known_hashes: dict[str, str | None] | None = None,
) -> list[QuarantineEntry]:
    """将命中项移入隔离区并追加/更新 manifest。

    known_hashes: 可选 path→sha256（来自 inventory），避免移动后再算。
    先处理路径更深的条目，减少父目录先移导致子路径找不到的问题。
    """
    del run_id  # 隔离目录已由调用方按 run_id 选好；保留参数便于 API 稳定
    existing: list[dict] = []
    if manifest_path.exists():
        existing = read_json(manifest_path)
    entries = [QuarantineEntry(**e) for e in existing]
    seen_orig = {e.original_path for e in entries}
    known_hashes = known_hashes or {}

    ordered = sorted(hits, key=lambda h: h.path.count("/"), reverse=True)

    for hit in ordered:
        if hit.path in seen_orig:
            continue
        try:
            src = resolve_under(root, hit.path)
            dest = _safe_dest(quarantine_dir, hit.path)
        except ValueError:
            # 越界路径（外部输入的 .. / 绝对路径）直接跳过，不搬动任何根外文件
            continue
        if not src.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dest))
        size = 0
        digest = known_hashes.get(hit.path)
        if dest.is_file():
            size = dest.stat().st_size
            if digest is None:
                try:
                    digest = file_sha256(dest)
                except OSError:
                    digest = None
        state = root / ".dirtidy"
        try:
            qrel = str(dest.relative_to(state))
        except ValueError:
            qrel = str(dest)
        entry = QuarantineEntry(
            id=uuid.uuid4().hex[:12],
            original_path=hit.path,
            quarantine_path=qrel,
            rule_id=hit.rule_id,
            reason=hit.reason,
            size=size,
            sha256=digest,
            quarantined_at=utc_now_iso(),
            is_dir=hit.is_dir,
        )
        entries.append(entry)
        seen_orig.add(hit.path)

    write_json(manifest_path, [e.to_dict() for e in entries])
    return entries


def restore_entries(
    root: Path,
    manifest_path: Path,
    *,
    entry_ids: set[str] | None = None,
    restore_all: bool = False,
) -> list[str]:
    """按 id 或全部还原；若原路径已存在文件则跳过该条（避免覆盖）。"""
    if not manifest_path.exists():
        return []
    raw = read_json(manifest_path)
    remaining: list[dict] = []
    restored: list[str] = []
    state = root / ".dirtidy"

    for item in raw:
        eid = item["id"]
        if not restore_all and (entry_ids is None or eid not in entry_ids):
            remaining.append(item)
            continue
        qpath = Path(item["quarantine_path"])
        src = qpath if qpath.is_absolute() else state / qpath
        try:
            dest = resolve_under(root, item["original_path"])
        except ValueError:
            # 原始路径越界（历史脏数据/被篡改的 manifest）：拒绝还原，保留在清单里
            remaining.append(item)
            continue
        if not src.exists():
            remaining.append(item)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            remaining.append(item)
            continue
        shutil.move(str(src), str(dest))
        restored.append(item["original_path"])

    write_json(manifest_path, remaining)
    return restored


def purge_quarantine_dir(quarantine_dir: Path) -> None:
    """物理删除隔离区目录树；不可恢复。"""
    if quarantine_dir.exists():
        shutil.rmtree(quarantine_dir)


def sweep_empty_dirs(
    root: Path,
    run_id: str,
    quarantine_dir: Path,
    manifest_path: Path,
    *,
    max_rounds: int = 8,
) -> list[QuarantineEntry]:
    """隔离子项后，多轮把新变成空的父目录也隔离，直到稳定或达到轮数上限。

    扫描阶段的 empty_dir 规则看不到「子项搬走之后才空」的目录，所以在隔离之后补这一步。
    不会处理目标根本身，也不会进入 `.dirtidy`。
    """
    import os

    from dirtidy.scan.scanner import SKIP_DIR_NAMES

    quarantine_dir.mkdir(parents=True, exist_ok=True)
    for _ in range(max_rounds):
        empties: list[str] = []
        for dirpath, dirnames, _filenames in os.walk(root, topdown=False, followlinks=False):
            current = Path(dirpath)
            if current == root or current.name in SKIP_DIR_NAMES:
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
                continue
            if ".dirtidy" in current.parts:
                continue
            try:
                if any(current.iterdir()):
                    continue
            except OSError:
                continue
            empties.append(current.relative_to(root).as_posix())
        if not empties:
            break
        hits = [
            RuleHit(
                path=rel,
                rule_id="empty_dir_after_move",
                action="auto_quarantine",
                reason="隔离子项后留下的空目录",
                is_dir=True,
            )
            for rel in empties
        ]
        before = 0
        if manifest_path.exists():
            before = len(read_json(manifest_path))
        quarantine_hits(root, run_id, hits, quarantine_dir, manifest_path)
        after = len(read_json(manifest_path)) if manifest_path.exists() else before
        if after == before:
            break

    if not manifest_path.exists():
        return []
    return [QuarantineEntry(**e) for e in read_json(manifest_path)]
