"""把「规则动作」翻译成用户能直接拿去做判断的建议。

界面上原先直接铺 `rule_id`（`dep_node_modules`、`empty_file`）与「本规则已开启直删」——
前者是内部编号，后者回答的是「规则怎么配的」，都不是用户此刻要的那个答案。
用户站在列表前只想问两件事：**这是什么**、**要不要删**。

所以这里把判定收敛成两个枚举，且**只在这一处算**：

- `advice`：这条该不该清（`safe_clean` / `clean` / `review` / `protected`）
- `effect`：按建议动它会发生什么（`quarantine` 可还原 / `purge` 不可还原 / `keep` 不动）

为什么不在前端算：这个项目已经因为「同一个判定写两份」出过四次漏洞
（`apply-decisions` 漏查保护、`plan_quarantine` 与实跑各算一遍、`explain` 自算结论、
`scan --apply` 漏查保护）。建议这种东西一旦前后端各算一份，界面说「建议清理」
而实际执行是「直接删除」，就是又一次「界面看起来做了、实际没做」。

CLI 的 `scan` 摘要与 Web 列表都从这里取措辞，保证同一件事只有一种说法。
"""

from __future__ import annotations

from typing import Final

# 建议档位。顺序即「越靠前越可以放心清理」，界面按此排序。
ADVICE_SAFE_CLEAN: Final = "safe_clean"
ADVICE_CLEAN: Final = "clean"
ADVICE_REVIEW: Final = "review"
ADVICE_PROTECTED: Final = "protected"

ADVICE_ORDER: Final[tuple[str, ...]] = (
    ADVICE_SAFE_CLEAN,
    ADVICE_CLEAN,
    ADVICE_REVIEW,
    ADVICE_PROTECTED,
)

# 界面上显示的字。**动词必须和后果对得上**：写「建议清理」就不能同时又标「不可还原」。
ADVICE_LABELS: Final[dict[str, str]] = {
    ADVICE_SAFE_CLEAN: "建议清理",
    ADVICE_CLEAN: "可以清理",
    ADVICE_REVIEW: "需要你判断",
    ADVICE_PROTECTED: "受保护",
}

# 一句话解释「凭什么这么建议」。用户不看规则源码也能懂。
ADVICE_HINTS: Final[dict[str, str]] = {
    ADVICE_SAFE_CLEAN: "重建成本为零，删了不影响使用",
    ADVICE_CLEAN: "能重建，但要重新安装或编译一次",
    ADVICE_REVIEW: "说不准还有没有用，需要你看一眼",
    ADVICE_PROTECTED: "受保护，永远不会自动动它",
}

EFFECT_QUARANTINE: Final = "quarantine"
EFFECT_PURGE: Final = "purge"
EFFECT_KEEP: Final = "keep"

# 后果措辞。直接写清「能不能反悔」，比「是 / 否」有用得多。
EFFECT_LABELS: Final[dict[str, str]] = {
    EFFECT_QUARANTINE: "搬进隔离区（可还原）",
    EFFECT_PURGE: "直接删除（不可还原）",
    EFFECT_KEEP: "不动",
}


def advice_for(*, action: str, purge_safe: bool = False) -> str:
    """规则动作 → 建议档位。

    `purge_safe` 是规则包上的标记（「重建成本为零、确定无用」），它不改变执行行为，
    只用来把「建议清理」和「可以清理」区分开——两者都能重建，但代价差一个量级。
    """
    if action == "protect":
        return ADVICE_PROTECTED
    if action == "auto_quarantine":
        return ADVICE_SAFE_CLEAN if purge_safe else ADVICE_CLEAN
    # candidate_only 以及任何没见过的动作：一律交人工，不猜。
    return ADVICE_REVIEW


def purge_allowed(*, purge_safe: bool, purge_enabled: bool) -> bool:
    """**真正会不会物理删除**——本项目里这个判定只此一处。

    规则必须同时满足两个条件：
    ① 规则包上标了 `purge_safe`（「重建成本为零、确定无用」）；
    ② 用户在设置里把这条规则列进了 `auto_purge_rules`。

    只查 ② 是漏洞：界面上会出现「动了会怎样：搬进隔离区（可还原）」，
    实际执行却把目录 `rmtree` 了。`effect_for` 与 `pipeline.apply_pending_rules`
    都必须调用这里，不允许任何一方自己写一份 `and` 条件——
    判定一旦写成两份，漂移的那一份就是用户数据消失的地方。
    """
    return bool(purge_safe and purge_enabled)


def effect_for(*, action: str, purge_safe: bool = False, purge_enabled: bool = False) -> str:
    """规则动作 + 用户是否对该规则开了直删 → 真正会发生什么。

    `purge_enabled` 来自用户设置（`auto_purge_rules`），与规则本身无关：
    同一条 `dep_python_cache`，没开直删时进隔离区可还原，开了就物理删除。
    这两者在界面上必须长得不一样，否则用户会以为随时能反悔。
    """
    if action == "protect":
        return EFFECT_KEEP
    if action != "auto_quarantine":
        # 候选只是「列出来给人看」，不会自己动；选了什么才发生什么。
        return EFFECT_KEEP
    if purge_allowed(purge_safe=purge_safe, purge_enabled=purge_enabled):
        return EFFECT_PURGE
    return EFFECT_QUARANTINE


def describe(*, action: str, purge_safe: bool = False, purge_enabled: bool = False) -> dict[str, str]:
    """一次给出界面需要的全部措辞，省得调用方各拼一份。"""
    advice = advice_for(action=action, purge_safe=purge_safe)
    effect = effect_for(action=action, purge_safe=purge_safe, purge_enabled=purge_enabled)
    return {
        "advice": advice,
        "advice_label": ADVICE_LABELS[advice],
        "advice_hint": ADVICE_HINTS[advice],
        "effect": effect,
        "effect_label": EFFECT_LABELS[effect],
    }
