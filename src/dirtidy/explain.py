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
from functools import partial
from pathlib import Path
from typing import Any

from dirtidy.models import InventoryItem, RuleHit, resolve_under
from dirtidy.quarantine.store import find_protected_descendants
from dirtidy.rules.engine import (
    PathIndex,
    effective_protect_rules,
    load_rule_pack,
    match_items,
    match_rules,
    passes_when,
)
from dirtidy.rules.protect import (
    builtin_protect_rules,
    compile_protect_rules,
    evaluate_protect,
    matcher_hits,
)
from dirtidy.scan.scanner import SKIP_DIR_NAMES

#: 解释目录时最多枚举多少条后代（用于 source_free 守卫判定），防止在超大目录上卡住
MAX_EXPLAIN_DESCENDANTS = 20_000

DECISION_LABELS = {
    "protected": "受保护——不会被自动隔离，也不进候选",
    "blocked_by_descendant": "整条拦下——子树内含受保护路径，apply-rules 时不会隔离",
    "auto_quarantine": "自动隔离——apply-rules / --apply 时会移入隔离区",
    "candidate_only": "仅候选——需要人工或模型确认",
    "no_match": "无规则命中——保持原样",
    "missing": "路径不存在",
}

#: 规则来源标记。内置保护清单写在代码里、规则包关不掉，因此在解释里必须与
#: 规则包的规则区分开——否则用户看到「结论说是 builtin_xxx 保护的，列表里却找不到」。
SOURCE_LABELS = {
    "builtin": "内置保护",
    "pack": "规则包",
    "extra": "额外指定",
}


def _human_size(n: int) -> str:
    """字节数 → 人读的「12.3 MB」。目录的 size 恒为 0，此时由调用方决定是否显示。"""
    if n <= 0:
        return "0 B"
    units = ("B", "KB", "MB", "GB", "TB")
    x = float(n)
    i = 0
    while x >= 1024 and i < len(units) - 1:
        x /= 1024
        i += 1
    if i == 0:
        return f"{int(x)} B"
    return f"{x:.1f} {units[i]}"


#: 每个 decision 的人话回答。四问顺序即「用户站在这条路径前会按什么顺序想」：
#: 这是什么 → 系统会怎么办 → 为什么 → 删了怎么恢复。
_WHAT: dict[str, str] = {
    "protected": "受保护，不能动",
    "blocked_by_descendant": "这个目录整体动不了",
    "auto_quarantine": "会被搬进隔离区",
    "candidate_only": "先列出来，等你决定",
    "no_match": "保持原样",
    "missing": "这个路径不存在",
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
    source: str = "pack"

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
    #: 命中规则的「中文短名」与「删了怎么恢复」。人话摘要要回答「这是什么 / 删了怎么办」，
    #: 而 `rule_id`（`dep_node_modules`）是内部编号，不能直接给人看。
    rule_label: str = ""
    recover: str = ""
    protected: bool = False
    protect_rule_id: str | None = None
    protect_reason: str | None = None
    #: 「整条拦下」时说明是子树里的哪条路径、被哪条保护规则挡住。
    #: 只说「子树里有受保护内容」等于没给责任人，用户查不到该动哪个文件。
    blocking_path: str = ""
    blocking_rule_id: str | None = None
    blocking_reason: str = ""
    protect_policy: list[dict[str, Any]] = field(default_factory=list)
    traces: list[RuleTrace] = field(default_factory=list)
    enumerated_descendants: int = 0
    truncated: bool = False

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["decision_label"] = DECISION_LABELS.get(self.decision, self.decision)
        return d

    #: 每个 decision 的人话回答见模块级 `_WHAT`，这里不再重复定义类字段。

    def human_summary(self) -> dict[str, str]:
        """四段自然语言结论，供网页弹窗直接渲染，也供 `to_text()` 复用。

        刻意**不**在网页前端再拼一份措辞——同一个语义散两处迟早漂移（本项目已四次
        栽在「同一个判定写两份」上）。前端只负责显示这里算好的四段字。
        """
        what = _WHAT.get(self.decision, self.decision)
        kind = "目录" if self.is_dir else "文件"

        if self.decision == "missing":
            return {
                "what": what,
                "action": "没有这个路径，谈不上动不动。",
                "why": "它要么已被移走，要么路径写错了。",
                "recover": "无需处理。",
            }

        # 这是什么：优先用规则的中文短名，没有就退到「内部规则编号」。
        what_detail = self.rule_label or (f"内部规则 {self.rule_id}" if self.rule_id else "")
        if self.decision == "protected":
            action = "系统不会自动动它，也不把它列进候选。"
            why = (
                f"它命中了保护规则「{self.protect_reason or self.protect_rule_id}」；"
                "保护优先级最高，任何规则或直删都压不过它。"
            )
            if self.rule_id:
                why += f"本来有条规则（{what_detail}）想动它，被保护拦下了。"
            recover = "不适用——它根本不会被删。"
        elif self.decision == "blocked_by_descendant":
            action = "这条目录不会被搬走，整条拦下。"
            blocker = self.blocking_path or "子树里的某个受保护文件"
            why = (
                f"规则（{what_detail}）本来要搬走它，但它的子树里躺着受保护文件 "
                f"「{blocker}」（{self.blocking_reason or self.blocking_rule_id}）。"
                "搬走父目录等于把受保护文件一起搬走，所以整条不动。"
            )
            recover = "不适用——它这次不会被删。"
        elif self.decision == "auto_quarantine":
            action = "系统会在「执行」时把它搬进隔离区（可还原，不会直接删除）。"
            why = f"它命中了规则「{what_detail}」——{self.reason or '按规则判定可清理'}。"
            recover = self.recover or "从隔离区还原即可找回。"
        elif self.decision == "candidate_only":
            action = "它进了候选，但不会自动动；要你在审阅页明确选「隔离 / 归档」才搬。"
            why = f"它命中了候选规则「{what_detail}」——{self.reason or '需要人工确认'}。"
            recover = self.recover or "不删就还在原位，什么都不用做。"
        elif self.protected:
            # 没有规则想动它、但它仍受保护——说清楚，避免「没命中 = 可以随便动」
            action = "系统不会动它；即便以后有规则命中，也不会自动隔离。"
            why = f"它受保护（{self.protect_reason or self.protect_rule_id}），只是本轮没有规则命中它。"
            recover = "不适用——它不会被自动删。"
        else:  # no_match
            action = "系统不会动它，保持原样。"
            why = "没有规则命中它：既不是可清理的依赖/缓存，也不是需要确认的候选。"
            recover = "不适用——它不会被删。"

        return {
            "what": f"{what}（{kind}）",
            "action": action,
            "why": why,
            "recover": recover,
        }

    def _human_lines(self) -> list[str]:
        """人话结论段：四问分层，不含逐条规则日志。"""
        s = self.human_summary()
        lines = [
            f"这是什么：{s['what']}",
            f"系统会怎么办：{s['action']}",
            f"为什么：{s['why']}",
            f"删了怎么办：{s['recover']}",
        ]
        if self.path:
            lines.insert(0, f"路径：{self.path}")
        if self.is_dir and self.enumerated_descendants:
            suffix = "（已达枚举上限）" if self.truncated else ""
            lines.append(f"已枚举后代：{self.enumerated_descendants} 条{suffix}")
        return lines

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
        elif self.decision == "blocked_by_descendant":
            # 路径自身不受保护，是它**里面**的受保护路径导致整条不能动。
            # 必须说清是哪个文件挡的，否则用户只会看到「目录没被搬走」却无从下手。
            lines.append(f"  本来想动它的规则: {self.rule_id}（{self.reason}）")
            if self.blocking_path:
                lines.append(f"  子树内的受保护路径: {self.blocking_path}")
            lines.append(f"  保护规则: {self.blocking_rule_id}（{self.blocking_reason}）")
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

        protect_count = sum(1 for tr in self.traces if tr.action == "protect")
        lines.extend(
            [
                "",
                f"生效的保护规则 {protect_count} 条（内置清单 + 规则包 + 额外指定），逐条规则判定如下：",
                "",
            ]
        )
        for tr in self.traces:
            mark = "命中" if tr.matched else "未命中"
            src = SOURCE_LABELS.get(tr.source, tr.source)
            lines.append(f"[{mark}] {tr.rule_id}  action={tr.action}  ({src})")
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
    try:
        st = (root / rel).stat()
        size, mtime = (0 if is_dir else int(st.st_size)), float(st.st_mtime)
    except OSError:
        size, mtime = 0, 0.0
    # 不再预算 suffix：规则匹配侧会用 suffix_of(path) 现算，这里保持一致
    return InventoryItem(path=rel, is_dir=is_dir, size=size, mtime=mtime)


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

    # 逐条规则的判定明细。
    # 顺序必须与真实判定的优先级一致：内置保护清单 → 规则包 → 额外指定。
    # 内置清单排在最前且单独标注来源，否则会出现「结论说是 builtin_xxx 保护的，
    # 但逐条列表里根本找不到这条规则」这种查不到责任人的情况。
    traces: list[RuleTrace] = []
    for m in compile_protect_rules(builtin_protect_rules()):
        bhit = matcher_hits(m, rel, is_dir)
        traces.append(
            RuleTrace(
                rule_id=m.rule_id,
                action="protect",
                matched=bhit,
                conditions_passed=True,
                note="内置保护清单命中 → 该路径受保护（规则包关不掉）" if bhit else "",
                source="builtin",
            )
        )

    winner: str | None = None
    for rule in pack.get("rules") or []:
        rule_id = str(rule.get("id") or "unknown")
        action = str(rule.get("action", "candidate_only"))
        matched = any(it.path == rel for it in match_items(rule, items, root))
        trace = RuleTrace(
            rule_id=rule_id, action=action, matched=matched, conditions_passed=False, source="pack"
        )

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

    for m in compile_protect_rules(list(extra_protect_rules or [])):
        ehit = matcher_hits(m, rel, is_dir)
        traces.append(
            RuleTrace(
                rule_id=m.rule_id,
                action="protect",
                matched=ehit,
                conditions_passed=True,
                note="调用方 --protect 额外指定的保护，命中即生效" if ehit else "",
                source="extra",
            )
        )

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
        if is_dir:
            # 命中目录但子树里躺着受保护路径时，apply 会**整条拦下**（见 plan_quarantine）。
            # 解释若还报「会隔离」，用户就会照着一份错的说明做决策——
            # 这与「网页说的和 CLI 说的不一样」是同一类问题，只是发生在解释与执行之间。
            blocks = find_protected_descendants(
                root,
                [
                    RuleHit(
                        path=rel,
                        rule_id=hit.rule_id,
                        action="auto_quarantine",
                        reason=hit.reason,
                        is_dir=True,
                    )
                ],
                partial(evaluate_protect, matchers),
            )
            found = blocks.get(rel)
            if found is not None:
                blocking_path, blocking_rule_id, blocking_reason = found
                explanation.decision = "blocked_by_descendant"
                explanation.blocking_path = blocking_path
                explanation.blocking_rule_id = blocking_rule_id
                explanation.blocking_reason = blocking_reason
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

    # 命中规则的中文短名与「删了怎么恢复」，从规则包**同源**查（不另写一份措辞）。
    # 找不到就留空：human_summary 会回落到内部编号或通用说法，而不是编一个。
    if explanation.rule_id:
        for rule in pack.get("rules") or []:
            if str(rule.get("id")) == explanation.rule_id:
                explanation.rule_label = str(rule.get("label") or "")
                explanation.recover = str(rule.get("recover") or "")
                break
    return explanation
