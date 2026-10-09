"""YAML 规则包加载与匹配。

规则 action：
- auto_quarantine：进入待隔离列表（apply-rules / --apply 时移动）
- candidate_only：只进 candidates，等人工或模型后再决策
同一路径只认第一条命中规则（claimed），避免重复处理。
"""

from __future__ import annotations

import fnmatch
from pathlib import Path
from typing import Any

import yaml

from dirtidy.models import Candidate, InventoryItem, RuleHit, default_rules_dir


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"规则文件必须是 mapping: {path}")
    return data


def load_rule_pack(name_or_path: str | Path) -> dict[str, Any]:
    """按包名（dirtidy/rules/packs/<name>.yaml）或绝对/相对路径加载；支持 extends 合并。"""
    p = Path(name_or_path)
    if not p.exists():
        p = default_rules_dir() / f"{name_or_path}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"找不到规则包: {name_or_path}")

    data = _load_yaml(p)
    rules = list(data.get("rules") or [])
    extends = data.get("extends")
    if extends:
        # 基包规则在前，当前包追加，便于场景包覆盖/扩展
        base = load_rule_pack(extends)
        merged = list(base.get("rules") or [])
        merged.extend(rules)
        data = {**base, **data, "rules": merged, "pack_id": data.get("pack_id", base.get("pack_id"))}
    return data


def _is_empty_dir(root: Path, rel: str) -> bool:
    p = root / rel
    try:
        return p.is_dir() and next(p.iterdir(), None) is None
    except OSError:
        return False


def match_rules(
    root: Path,
    items: list[InventoryItem],
    pack: dict[str, Any],
) -> tuple[list[RuleHit], list[Candidate]]:
    """对 inventory 应用规则包，返回 (自动隔离命中, 候选列表)。"""
    by_path = {i.path: i for i in items}
    hits: list[RuleHit] = []
    candidates: list[Candidate] = []
    claimed: set[str] = set()

    for rule in pack.get("rules") or []:
        rule_id = rule.get("id", "unknown")
        action = rule.get("action", "candidate_only")
        reason = rule.get("reason", "")
        match = rule.get("match") or {}
        mtype = match.get("type")

        matched_paths: list[str] = []

        if mtype == "dir_name":
            names = set(match.get("names") or [])
            for item in items:
                if not item.is_dir:
                    continue
                if Path(item.path).name in names:
                    matched_paths.append(item.path)

        elif mtype == "suffix":
            suffixes = {
                s.lower() if s.startswith(".") else f".{s.lower()}"
                for s in (match.get("suffixes") or [])
            }
            for item in items:
                if item.is_dir:
                    continue
                if item.suffix.lower() in suffixes:
                    matched_paths.append(item.path)

        elif mtype == "empty_dir":
            for item in items:
                if item.is_dir and item.path not in claimed and _is_empty_dir(root, item.path):
                    matched_paths.append(item.path)

        elif mtype == "empty_file":
            for item in items:
                if not item.is_dir and item.size == 0 and not item.error:
                    matched_paths.append(item.path)

        elif mtype == "path_glob":
            patterns = match.get("patterns") or []
            for item in items:
                if any(fnmatch.fnmatch(item.path, pat) for pat in patterns):
                    matched_paths.append(item.path)

        for path in matched_paths:
            if path in claimed:
                continue
            found = by_path.get(path)
            hit = RuleHit(
                path=path,
                rule_id=rule_id,
                action=action,
                reason=reason,
                is_dir=bool(found.is_dir) if found else False,
            )
            if action == "auto_quarantine":
                hits.append(hit)
                claimed.add(path)
            else:
                candidates.append(
                    Candidate(
                        kind="rule",
                        paths=[path],
                        rule_id=rule_id,
                        reason=reason,
                        meta={"action": action},
                    )
                )
                claimed.add(path)

    return hits, candidates
