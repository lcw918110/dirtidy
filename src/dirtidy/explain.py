"""解释某个路径为什么会被（或不会被）动。

`dirtidy explain <root> <相对路径>` 把规则匹配、`when` 条件判定、保护判定逐条摊开。
存在的意义：几百万文件里，用户不可能靠猜知道「这条规则为什么没命中」。

实现上**复用** rules/engine 的匹配与条件函数（`match_items` / `passes_when` /
`match_rules`），而不是另写一份解释逻辑——否则解释结果迟早与真实执行结果漂移。
"""

from __future__ import annotations

import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from dirtidy.models import InventoryItem, resolve_under
from dirtidy.rules.engine import (
    PathIndex,
    effective_protect_rules,
    load_rule_pack,
    match_items,
    match_rules,
    passes_when,
)
from dirtidy.rules.protect import compile_protect_rules, evaluate_protect
from dirtidy.scan.scanner import SKIP_DIR_NAMES

#: 解释目录时最多枚举多少条后代（用于 source_free 守卫判定），防止在超大目录上卡住
MAX_EXPLAIN_DESCENDANTS = 20_000

DECISION_LABELS = {
    "protected": "受保护——不会被自动隔离，也不进候选",
    "auto_quarantine": "自动隔离——apply-rules / --apply 时会移入隔离区",
    "candidate_only": "仅候选——需要人工或模型确认",
    "no_match": "无规则命中——保持原样",
    "missing": "路径不存在",
}


@dataclass(slots=True)
class RuleTrace:
    """单条规则对目标路径的判定明细。"""

    rule_id: str
    action: str
    matched: bool
    conditions_passed: bool
    conditions: list[str] = field(default_factory=list)
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class Explanation:
    path: str
    exists: bool
    is_dir: bool
    size: int
    mtime: float
    decision: str
    rule_id: str | None = None
    reason: str = ""
    protected: bool = False
    protect_rule_id: str | None = None
    protect_reason: str | None = None
    protect_policy: list[dict[str, Any]] = field(default_factory=list)
    traces: list[RuleTrace] = field(default_factory=list)
    enumerated_descendants: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["decision_label"] = DECISION_LABELS.get(self.decision, self.decision)
        return d

    def to_text(self) -> str:
        from dirtidy.models import utc_now_iso

        lines = [
            f"路径: {self.path}",
            f"类型: {'目录' if self.is_dir else '文件'}   大小: {self.size} 字节",
        ]
        if self.mtime:
            age_days = (time.time() - self.mtime) / 86400.0
            lines.append(f"mtime: {self.mtime:.0f}（约 {age_days:.1f} 天前）")
        lines.append("")
        lines.append(f"结论: {DECISION_LABELS.get(self.decision, self.decision)}")

        if self.decision == "protected":
            lines.append(f"  保护规则: {self.protect_rule_id}（{self.protect_reason}）")
            lines.append(f"  本来想动它的规则: {self.rule_id}（{self.reason}）")
        elif self.rule_id:
            lines.append(f"  规则: {self.rule_id}")
            lines.append(f"  原因: {self.reason}")
        elif self.protected:
            # 没有规则想动它，但它仍受保护——说清楚，避免用户以为「没命中=可以随便动」
            lines.append(f"  该路径受保护: {self.protect_rule_id}（{self.protect_reason}）")
            lines.append("  即使以后有规则命中它，也不会被自动隔离")

        if self.is_dir and self.enumerated_descendants:
            suffix = "（已达枚举上限，source_free 判定基于已枚举部分）" if self.truncated else ""
            lines.append(f"  已枚举后代: {self.enumerated_descendants} 条{suffix}")

        lines.extend(["", f"生效的保护规则 {len(self.protect_policy)} 条，逐条规则判定如下：", ""])
        for tr in self.traces:
            mark = "命中" if tr.matched else "未命中"
            lines.append(f"[{mark}] {tr.rule_id}  action={tr.action}")
            if tr.matched:
                lines.append(f"    when 条件: {'全部通过' if tr.conditions_passed else '有未通过项'}")
                for c in tr.conditions:
                    lines.append(f"      - {c}")
            if tr.note:
                lines.append(f"    备注: {tr.note}")
        lines.append("")
        lines.append(f"（生成于 {utc_now_iso()}）")
        return "\n".join(lines)


def _item_for(root: Path, rel: str, is_dir: bool) -> InventoryItem:
    name = rel.rsplit("/", 1)[-1]
    suffix = ""
    if not is_dir:
        dot = name.rfind(".")
        suffix = name[dot:].lower() if dot > 0 else ""
    try:
        st = (root / rel).stat()
        size, mtime = (0 if is_dir else int(st.st_size)), float(st.st_mtime)
    except OSError:
        size, mtime = 0, 0.0
    return InventoryItem(path=rel, is_dir=is_dir, size=size, mtime=mtime, suffix=suffix)


def _collect_items(root: Path, rel: str, is_dir: bool) -> tuple[list[InventoryItem], int, bool]:
    """构造解释所需的 mini-inventory。

    文件只需要它自己；目录需要子树，因为 `source_free` 守卫和 `empty_dir` 都依赖后代信息。
    枚举有上限，超限时标记 truncated 并在输出里说明。
    """
    items = [_item_for(root, rel, is_dir)]
    if not is_dir:
        return items, 0, False

    count = 0
    truncated = False
    for dirpath, dirnames, filenames in os.walk(root / rel, topdown=True, followlinks=False):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        current = Path(dirpath)
        sub_rel = current.relative_to(root).as_posix()
        if sub_rel != rel:
            items.append(_item_for(root, sub_rel, True))
            count += 1
        for name in filenames:
            child = (current / name).relative_to(root).as_posix()
            items.append(_item_for(root, child, False))
            count += 1
            if count >= MAX_EXPLAIN_DESCENDANTS:
                truncated = True
                break
        if truncated:
            break
    return items, count, truncated


def explain_path(
    root: Path,
    rel: str,
    pack: dict[str, Any] | None = None,
    *,
    rules: str = "default",
    extra_protect_rules: list[dict[str, Any]] | None = None,
    now: float | None = None,
) -> Explanation:
    """解释 rel（相对 root 的路径）会被如何处理。

    rel 先经 `resolve_under` 校验，越界直接抛 ValueError——解释功能不该成为
    绕过路径不变量的一条缝。
    """
    root = Path(root).resolve()
    target = resolve_under(root, rel)
    rel = rel.replace(os.sep, "/").strip("/")
    if pack is None:
        pack = load_rule_pack(rules)
    now = time.time() if now is None else now

    if not target.exists():
        return Explanation(
            path=rel,
            exists=False,
            is_dir=False,
            size=0,
            mtime=0.0,
            decision="missing",
            protect_policy=[],
        )

    is_dir = target.is_dir()
    items, descendants, truncated = _collect_items(root, rel, is_dir)
    index = PathIndex(items)
    matchers = compile_protect_rules(effective_protect_rules(pack, extra_protect_rules))

    # 逐条规则的判定明细
    traces: list[RuleTrace] = []
    winner: str | None = None
    for rule in pack.get("rules") or []:
        rule_id = str(rule.get("id") or "unknown")
        action = str(rule.get("action", "candidate_only"))
        matched = any(it.path == rel for it in match_items(rule, items, root))
        trace = RuleTrace(rule_id=rule_id, action=action, matched=matched, conditions_passed=False)

        if matched:
            conds: list[str] = []
            passed = passes_when(items[0], rule.get("when") or {}, now, index, trace=conds)
            trace.conditions = conds
            trace.conditions_passed = passed
            if action == "protect":
                trace.note = "保护规则命中 → 该路径受保护"
            elif not passed:
                trace.note = "路径匹配，但 when 条件不满足 → 本条不生效"
            elif winner is None:
                winner = rule_id
                trace.note = "生效规则（同一路径只认第一条命中）"
            else:
                trace.note = f"被更靠前的规则 {winner} 抢先 → 本条不生效"
        traces.append(trace)

    # 权威结论仍由 match_rules 给出，保证与真实执行一致
    res = match_rules(root, items, pack, now=now, extra_protect_rules=extra_protect_rules)
    hit = next((h for h in res.hits if h.path == rel), None)
    cand = next((c for c in res.candidates if rel in c.paths), None)
    prot = next((p for p in res.protected if p.path == rel), None)

    explanation = Explanation(
        path=rel,
        exists=True,
        is_dir=is_dir,
        size=items[0].size,
        mtime=items[0].mtime,
        decision="no_match",
        protect_policy=res.protect_policy,
        traces=traces,
        enumerated_descendants=descendants,
        truncated=truncated,
    )
    if prot is not None:
        explanation.decision = "protected"
        explanation.rule_id = prot.intended_rule_id
        explanation.reason = f"原本动作 {prot.intended_action}"
        explanation.protect_rule_id = prot.protect_rule_id
        explanation.protect_reason = prot.protect_reason
    elif hit is not None:
        explanation.decision = "auto_quarantine"
        explanation.rule_id = hit.rule_id
        explanation.reason = hit.reason
    elif cand is not None:
        explanation.decision = "candidate_only"
        explanation.rule_id = cand.rule_id
        explanation.reason = cand.reason
    else:
        # 兜底说明：受保护但没有任何规则命中（不会被自动动，但也没规则想动它）
        guard = evaluate_protect(matchers, rel, is_dir)
        if guard is not None:
            explanation.protect_rule_id, explanation.protect_reason = guard
    explanation.protected = explanation.protect_rule_id is not None
    return explanation
