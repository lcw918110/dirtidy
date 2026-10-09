"""YAML 规则包加载、校验与匹配。

规则 action（优先级从高到低）：

- ``protect``：**最高优先级**。命中路径（目录含整棵子树）永不自动隔离，
  也不会进入候选；只在 ``protected`` 清单里登记「哪条规则想动它、被哪条保护规则挡下」。
  保护判定不依赖规则书写顺序，因此「把 auto 规则写在 protect 前面」不会造成误删。
- ``auto_quarantine``：进入待隔离列表（``apply-rules`` / ``--apply`` 时移动）。
- ``candidate_only``：只进 ``candidates``，等人工或模型后再决策。

同一路径只认第一条**通过全部条件**的命中规则（claimed），避免重复处理。

匹配类型：
- ``dir_name``   目录名精确匹配（``names``）
- ``file_name``  文件名精确匹配，仅文件（``names``）
- ``name_glob``  basename 通配匹配（``globs``；可用 ``is_dir`` 限定只匹配目录或只匹配文件）
- ``suffix``     扩展名匹配，仅文件（``suffixes``）
- ``empty_dir``  空目录（以文件系统为准，不看 inventory）
- ``empty_file`` 0 字节且无读取错误的文件
- ``path_glob``  相对路径通配匹配（``patterns``）

条件块 ``when``（缺省即无条件）：
- ``min_age_days`` / ``max_age_days``  按 mtime 判断「多久没动过」
- ``min_size_bytes`` / ``max_size_bytes``  仅对文件生效（目录不聚合大小，故不适用）
- ``under`` / ``not_under``  路径前缀白/黑名单
- ``min_depth`` / ``max_depth``  相对根目录的层级
- ``require``  守卫列表，目前支持 ``source_free``：该目录子树内没有源码类文件
"""

from __future__ import annotations

import bisect
import fnmatch
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from dirtidy.models import (
    Candidate,
    InventoryItem,
    ProtectionRecord,
    RuleHit,
    RuleMatchResult,
    default_rules_dir,
)
from dirtidy.rules.protect import (
    builtin_protect_rules,
    compile_protect_rules,
    evaluate_protect,
)

ACTIONS: frozenset[str] = frozenset({"protect", "auto_quarantine", "candidate_only"})
MATCH_TYPES: frozenset[str] = frozenset(
    {"dir_name", "file_name", "name_glob", "suffix", "empty_dir", "empty_file", "path_glob"}
)
WHEN_KEYS: frozenset[str] = frozenset(
    {
        "min_age_days",
        "max_age_days",
        "min_size_bytes",
        "max_size_bytes",
        "under",
        "not_under",
        "min_depth",
        "max_depth",
        "require",
    }
)
GUARDS: frozenset[str] = frozenset({"source_free"})

#: 判定「这个目录里还有没有源码」用的扩展名集合。
#: 只用来**阻止自动隔离**，因此宁可宽一点：多认几个后缀只会更保守。
SOURCE_SUFFIXES: frozenset[str] = frozenset(
    {
        ".py", ".pyi", ".pyx", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts",
        ".go", ".rs", ".java", ".kt", ".kts", ".scala", ".groovy",
        ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx", ".m", ".mm", ".cs", ".fs",
        ".rb", ".php", ".pl", ".pm", ".lua", ".r", ".jl", ".dart", ".swift",
        ".ex", ".exs", ".erl", ".hrl", ".clj", ".cljs", ".hs", ".ml", ".mli",
        ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
        ".sql", ".vue", ".svelte", ".astro", ".proto", ".thrift", ".graphql", ".gql",
        ".tf", ".tfvars", ".hcl", ".nix", ".bzl", ".ipynb", ".rmd",
    }
)

#: 无扩展名但属于源码/构建定义的文件名
SOURCE_FILENAMES: frozenset[str] = frozenset(
    {
        "Makefile", "makefile", "GNUmakefile", "Dockerfile", "Containerfile",
        "CMakeLists.txt", "Rakefile", "Gemfile", "Justfile", "justfile",
        "go.mod", "go.sum", "Cargo.toml", "Cargo.lock", "pyproject.toml", "setup.py",
        "package.json", "build.gradle", "build.gradle.kts", "pom.xml", "build.sbt",
        "composer.json", "mix.exs", "stack.yaml", "pubspec.yaml", "Package.swift",
    }
)


class RulePackError(ValueError):
    """规则包结构非法（未知 action / match.type / 缺字段 / id 重复）。"""


def _load_yaml(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(f"规则文件必须是 mapping: {path}")
    return data


def _validate_rule(rule: Any, where: str, seen_ids: set[str]) -> None:
    if not isinstance(rule, dict):
        raise RulePackError(f"{where}: 规则必须是 mapping，实际为 {type(rule).__name__}")
    rid = rule.get("id")
    if not rid or not isinstance(rid, str):
        raise RulePackError(f"{where}: 规则缺少合法的 id")
    if rid in seen_ids:
        raise RulePackError(f"{where}: 规则 id 重复: {rid!r}")
    seen_ids.add(rid)

    action = rule.get("action", "candidate_only")
    if action not in ACTIONS:
        raise RulePackError(f"{where}/{rid}: 未知 action {action!r}，可选 {sorted(ACTIONS)}")

    match = rule.get("match")
    if not isinstance(match, dict):
        raise RulePackError(f"{where}/{rid}: 缺少 match 块")
    mtype = match.get("type")
    if mtype not in MATCH_TYPES:
        raise RulePackError(f"{where}/{rid}: 未知 match.type {mtype!r}，可选 {sorted(MATCH_TYPES)}")

    required_key = {
        "dir_name": "names",
        "file_name": "names",
        "name_glob": "globs",
        "suffix": "suffixes",
        "path_glob": "patterns",
    }.get(str(mtype))
    if required_key:
        values = match.get(required_key)
        if required_key == "globs" and not values:
            values = match.get("patterns")
        if not values:
            raise RulePackError(f"{where}/{rid}: match.type={mtype} 需要非空的 {required_key}")

    when = rule.get("when")
    if when is not None:
        if not isinstance(when, dict):
            raise RulePackError(f"{where}/{rid}: when 必须是 mapping")
        unknown = set(when) - WHEN_KEYS
        if unknown:
            raise RulePackError(f"{where}/{rid}: when 含未知字段 {sorted(unknown)}，可选 {sorted(WHEN_KEYS)}")
        for guard in when.get("require") or ():
            if guard not in GUARDS:
                raise RulePackError(f"{where}/{rid}: 未知 when.require 守卫 {guard!r}，可选 {sorted(GUARDS)}")


def validate_pack(data: dict[str, Any], *, source: str = "<pack>") -> None:
    """校验规则包结构；有笔误就早失败，不要等到扫完几百万文件才发现。"""
    rules = data.get("rules")
    if rules is None:
        return
    if not isinstance(rules, list):
        raise RulePackError(f"{source}: rules 必须是 list")
    seen: set[str] = set()
    for i, rule in enumerate(rules):
        _validate_rule(rule, f"{source}[{i}]", seen)


def load_rule_pack(name_or_path: str | Path) -> dict[str, Any]:
    """按包名（dirtidy/rules/packs/<name>.yaml）或绝对/相对路径加载；支持 extends 合并。"""
    p = Path(name_or_path)
    if not p.exists():
        p = default_rules_dir() / f"{name_or_path}.yaml"
    if not p.exists():
        raise FileNotFoundError(f"找不到规则包: {name_or_path}")

    data = _load_yaml(p)
    validate_pack(data, source=str(p))
    rules = list(data.get("rules") or [])
    extends = data.get("extends")
    if extends:
        # 基包规则在前，当前包追加，便于场景包覆盖/扩展
        base = load_rule_pack(extends)
        merged = list(base.get("rules") or [])
        merged.extend(rules)
        data = {**base, **data, "rules": merged, "pack_id": data.get("pack_id", base.get("pack_id"))}
        validate_pack(data, source=f"{p}(extends {extends})")
    return data


def effective_protect_rules(
    pack: dict[str, Any],
    extra: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """生效的保护规则 = 内置清单 + 规则包里的 protect 规则 + 调用方临时追加。"""
    pack_rules = [r for r in (pack.get("rules") or []) if r.get("action") == "protect"]
    return [*builtin_protect_rules(), *pack_rules, *(extra or [])]


def protect_policy(pack: dict[str, Any], extra: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """可落盘的「当前生效保护策略」摘要。"""
    out: list[dict[str, Any]] = []
    for rule in effective_protect_rules(pack, extra):
        match = rule.get("match") or {}
        out.append(
            {
                "id": rule.get("id", ""),
                "reason": rule.get("reason", ""),
                "match_type": match.get("type", ""),
                "values": list(
                    match.get("names")
                    or match.get("globs")
                    or match.get("suffixes")
                    or match.get("patterns")
                    or []
                ),
            }
        )
    return out


def _is_empty_dir(root: Path, rel: str) -> bool:
    """以文件系统为准判断空目录。

    刻意不看 inventory：扫描会跳过 `.git` 等目录，只看 inventory 会把
    「只剩 .git 的目录」误判成空目录。真正的 iterdir 才能看到全部子项。
    """
    try:
        p = Path(root) / rel
        return p.is_dir() and next(p.iterdir(), None) is None
    except OSError:
        return False


class _PathIndex:
    """按字典序排好的路径列表，用于 O(log n) 取某目录下的全部后代路径。

    比 `{path: item}` 全量字典省一个数量级的内存：5M 文件下字典要约 400MB，
    这里只多一份 8 字节/条的指针数组。
    """

    __slots__ = ("paths",)

    def __init__(self, items: list[InventoryItem]) -> None:
        self.paths = sorted(i.path for i in items)

    def descendants(self, prefix: str) -> list[str]:
        """返回 prefix 目录下的全部后代路径（不含 prefix 自身）。"""
        base = prefix + "/"
        lo = bisect.bisect_left(self.paths, base)
        # '/' 是 0x2F，比它大的最小可打印字符是 '0'(0x30)，故 prefix+"0" 是严格上界
        hi = bisect.bisect_left(self.paths, prefix + "0")
        return self.paths[lo:hi]


def _is_source_free(index: _PathIndex, path: str) -> bool:
    """守卫 source_free：该目录子树内没有任何源码/构建定义文件。"""
    for child in index.descendants(path):
        name = child.rsplit("/", 1)[-1]
        if name in SOURCE_FILENAMES:
            return False
        dot = name.rfind(".")
        if dot > 0 and name[dot:].lower() in SOURCE_SUFFIXES:
            return False
    return True


def _norm_prefixes(raw: Any) -> list[str]:
    out = []
    for p in raw or ():
        s = str(p).strip().strip("/")
        if s:
            out.append(s)
    return out


def _under_any(path: str, prefixes: list[str]) -> bool:
    return any(path == p or path.startswith(p + "/") for p in prefixes)


def _passes_when(
    item: InventoryItem,
    when: dict[str, Any],
    now: float,
    index: _PathIndex,
    trace: list[str] | None = None,
) -> bool:
    """条件过滤；任何一条不满足即不命中。

    trace 非 None 时把每条条件的判定明细追加进去（供 `dirtidy explain` 展示）；
    为 None 时一条字符串都不拼，避免在大规模扫描里为每次命中付格式化开销。
    """
    if not when:
        return True

    def note(text: str) -> None:
        if trace is not None:
            trace.append(text)

    path = item.path
    depth = path.count("/") + 1

    if "under" in when:
        prefixes = _norm_prefixes(when["under"])
        hit = _under_any(path, prefixes)
        note(f"under={prefixes} → {'通过' if hit else '不满足'}")
        if not hit:
            return False
    if "not_under" in when:
        prefixes = _norm_prefixes(when["not_under"])
        hit = _under_any(path, prefixes)
        note(f"not_under={prefixes} → {'不满足（命中黑名单）' if hit else '通过'}")
        if hit:
            return False

    if "min_depth" in when:
        limit = int(when["min_depth"])
        hit = depth >= limit
        note(f"min_depth={limit}（当前 {depth}）→ {'通过' if hit else '不满足'}")
        if not hit:
            return False
    if "max_depth" in when:
        limit = int(when["max_depth"])
        hit = depth <= limit
        note(f"max_depth={limit}（当前 {depth}）→ {'通过' if hit else '不满足'}")
        if not hit:
            return False

    # 目录大小在 inventory 里恒为 0（不聚合子树），故 size 条件只对文件生效
    if not item.is_dir:
        if "min_size_bytes" in when:
            limit = int(when["min_size_bytes"])
            hit = item.size >= limit
            note(f"min_size_bytes={limit}（当前 {item.size}）→ {'通过' if hit else '不满足'}")
            if not hit:
                return False
        if "max_size_bytes" in when:
            limit = int(when["max_size_bytes"])
            hit = item.size <= limit
            note(f"max_size_bytes={limit}（当前 {item.size}）→ {'通过' if hit else '不满足'}")
            if not hit:
                return False
    elif "min_size_bytes" in when or "max_size_bytes" in when:
        note("size 条件对目录不适用（inventory 不聚合目录大小）→ 跳过")

    if "min_age_days" in when or "max_age_days" in when:
        if not item.mtime:
            note("mtime 缺失（stat 失败）→ 无法证明够旧，保守判为不命中")
            return False
        age_days = (now - item.mtime) / 86400.0
        if "min_age_days" in when:
            limit_days = float(when["min_age_days"])
            hit = age_days >= limit_days
            note(f"min_age_days={limit_days}（实际 {age_days:.1f} 天）→ {'通过' if hit else '不满足'}")
            if not hit:
                return False
        if "max_age_days" in when:
            limit_days = float(when["max_age_days"])
            hit = age_days <= limit_days
            note(f"max_age_days={limit_days}（实际 {age_days:.1f} 天）→ {'通过' if hit else '不满足'}")
            if not hit:
                return False

    for guard in when.get("require") or ():
        if guard == "source_free":
            hit = _is_source_free(index, path)
            note(f"require: source_free（子树内无源码/构建定义文件）→ {'通过' if hit else '不满足'}")
            if not hit:
                return False
    return True


def _match_items(
    rule: dict[str, Any],
    items: list[InventoryItem],
    root: Path,
) -> Iterator[InventoryItem]:
    """产出该规则匹配到的 inventory 条目（不做条件过滤，也不去重）。"""
    match = rule.get("match") or {}
    mtype = match.get("type")

    if mtype == "dir_name":
        names = set(match.get("names") or ())
        for it in items:
            if it.is_dir and it.path.rsplit("/", 1)[-1] in names:
                yield it

    elif mtype == "file_name":
        names = set(match.get("names") or ())
        for it in items:
            if not it.is_dir and it.path.rsplit("/", 1)[-1] in names:
                yield it

    elif mtype == "name_glob":
        globs = match.get("globs") or match.get("patterns") or ()
        want_dir = match.get("is_dir")
        for it in items:
            if want_dir is not None and bool(it.is_dir) != bool(want_dir):
                continue
            if any(fnmatch.fnmatch(it.path.rsplit("/", 1)[-1], g) for g in globs):
                yield it

    elif mtype == "suffix":
        suffixes = {
            s.lower() if str(s).startswith(".") else f".{str(s).lower()}"
            for s in (match.get("suffixes") or ())
        }
        for it in items:
            if not it.is_dir and it.suffix.lower() in suffixes:
                yield it

    elif mtype == "empty_dir":
        for it in items:
            if it.is_dir and _is_empty_dir(root, it.path):
                yield it

    elif mtype == "empty_file":
        for it in items:
            if not it.is_dir and it.size == 0 and not it.error:
                yield it

    elif mtype == "path_glob":
        patterns = match.get("patterns") or ()
        for it in items:
            if any(fnmatch.fnmatch(it.path, p) for p in patterns):
                yield it

    else:
        # 正常情况下 load_rule_pack 已拦住；这里是防御性兜底
        raise RulePackError(f"未知 match.type: {mtype!r}（规则 {rule.get('id')!r}）")


def match_rules(
    root: Path,
    items: list[InventoryItem],
    pack: dict[str, Any],
    *,
    now: float | None = None,
    extra_protect_rules: list[dict[str, Any]] | None = None,
) -> RuleMatchResult:
    """对 inventory 应用规则包，返回命中 / 候选 / 被保护拦下的完整结果。"""
    root = Path(root)
    now = time.time() if now is None else now
    matchers = compile_protect_rules(effective_protect_rules(pack, extra_protect_rules))
    index = _PathIndex(items)

    hits: list[RuleHit] = []
    candidates: list[Candidate] = []
    protected: list[ProtectionRecord] = []
    claimed: set[str] = set()

    for rule in pack.get("rules") or []:
        action = rule.get("action", "candidate_only")
        if action == "protect":
            # 保护判定在每条规则命中时逐条校验，不依赖规则书写顺序
            continue
        rule_id = str(rule.get("id") or "unknown")
        reason = str(rule.get("reason", ""))
        when = rule.get("when") or {}

        for item in _match_items(rule, items, root):
            if item.path in claimed:
                continue
            if not _passes_when(item, when, now, index):
                continue
            claimed.add(item.path)

            guard = evaluate_protect(matchers, item.path, item.is_dir)
            if guard is not None:
                protected.append(
                    ProtectionRecord(
                        path=item.path,
                        protect_rule_id=guard[0],
                        protect_reason=guard[1],
                        intended_rule_id=rule_id,
                        intended_action=action,
                        is_dir=item.is_dir,
                    )
                )
                continue

            if action == "auto_quarantine":
                hits.append(
                    RuleHit(path=item.path, rule_id=rule_id, action=action, reason=reason, is_dir=item.is_dir)
                )
            else:
                candidates.append(
                    Candidate(
                        kind="rule",
                        paths=[item.path],
                        rule_id=rule_id,
                        reason=reason,
                        meta={"action": action},
                    )
                )

    return RuleMatchResult(
        hits=hits,
        candidates=candidates,
        protected=protected,
        protect_policy=protect_policy(pack, extra_protect_rules),
    )


# 供 `dirtidy explain` 复用同一套判定逻辑：绝不在这里复制一份实现，
# 否则解释结果会与真实执行结果漂移。
PathIndex = _PathIndex
match_items = _match_items
passes_when = _passes_when
