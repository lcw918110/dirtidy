"""内置保护清单（protect）：命中即永不自动隔离，也不进入候选。

为什么写在代码里而不是只放 YAML：保护清单是「不误删」的最后一道兜底，
必须不可被用户规则包意外覆盖或删掉。用户可以在自己的规则包里**追加**
`protect` 规则，也可以写 `protect` 规则覆盖同 id，但这几组内置清单始终生效。

判定语义（与 YAML 里的 protect 规则完全一致）：

- `dir_name` 命中：该目录**及其整棵子树**都受保护（祖先命中即保护后代）。
  这条语义很关键：`.git` 被保护，意味着 `.git/objects/...` 不可能被自动搬走。
- `file_name` / `name_glob` / `suffix` 命中：只保护命中的那个文件。
- `path_glob` 命中：保护命中的那条路径。

只做词法判断，不访问文件系统，因此可以在「扫描后人工改过 pending 列表」
或「换了规则包再 apply」的场景下于 apply 阶段重新校验一遍。
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from typing import Any

#: 版本控制元数据：删掉即丢失历史，且多数情况下无法从工作区内容还原
VCS_DIR_NAMES: tuple[str, ...] = (".git", ".hg", ".svn", ".bzr")

#: 工具自身状态目录：隔离区/清单都在里面
STATE_DIR_NAMES: tuple[str, ...] = (".dirtidy",)

#: 凭据与云/容器配置目录：删除可能直接导致凭据不可恢复
CREDENTIAL_DIR_NAMES: tuple[str, ...] = (
    ".ssh",
    ".gnupg",
    ".aws",
    ".gcloud",
    ".azure",
    ".kube",
    ".docker",
    ".cargo",
)

#: 操作系统 / 文件系统保留目录：不是普通业务目录
SYSTEM_DIR_NAMES: tuple[str, ...] = (
    "lost+found",
    "$RECYCLE.BIN",
    "System Volume Information",
    ".Trash",
    ".Spotlight-V100",
    ".fseventsd",
    ".DocumentRevisions-V100",
    ".TemporaryItems",
    ".vol",
)

#: 密钥 / 凭据文件（含扩展名通配）
SECRET_NAME_GLOBS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "*.kdbx",
    "*.ppk",
    "*.asc",
    "*.gpg",
    "*.age",
    ".netrc",
    ".npmrc",
    ".pgpass",
    ".git-credentials",
    "id_rsa",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "known_hosts",
    "authorized_keys",
)


def builtin_protect_rules() -> list[dict[str, Any]]:
    """内置保护规则（每次匹配都会隐式前置，不受规则包顺序影响）。"""
    return [
        {
            "id": "builtin_protect_vcs",
            "action": "protect",
            "match": {"type": "dir_name", "names": list(VCS_DIR_NAMES)},
            "reason": "版本控制元数据，删除即丢失历史",
        },
        {
            "id": "builtin_protect_state",
            "action": "protect",
            "match": {"type": "dir_name", "names": list(STATE_DIR_NAMES)},
            "reason": "dirtidy 自身状态目录",
        },
        {
            "id": "builtin_protect_credentials",
            "action": "protect",
            "match": {"type": "dir_name", "names": list(CREDENTIAL_DIR_NAMES)},
            "reason": "凭据 / 云与容器配置目录",
        },
        {
            "id": "builtin_protect_system_reserved",
            "action": "protect",
            "match": {"type": "dir_name", "names": list(SYSTEM_DIR_NAMES)},
            "reason": "操作系统 / 文件系统保留目录",
        },
        {
            "id": "builtin_protect_secret_files",
            "action": "protect",
            "match": {"type": "name_glob", "globs": list(SECRET_NAME_GLOBS)},
            "reason": "密钥 / 凭据文件，误删不可恢复",
        },
    ]


@dataclass(slots=True)
class _Matcher:
    """预编译后的保护判定器，避免每个路径都重新解析 YAML 结构。"""

    rule_id: str
    reason: str
    kind: str
    names: frozenset[str] = field(default_factory=frozenset)
    globs: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()


def _norm_suffixes(raw: Any) -> frozenset[str]:
    out = set()
    for s in raw or ():
        s = str(s).strip().lower()
        if not s:
            continue
        out.add(s if s.startswith(".") else f".{s}")
    return frozenset(out)


def compile_protect_rules(rules: list[dict[str, Any]]) -> list[_Matcher]:
    """把 protect 规则预编译成判定器列表。

    只接受「仅凭路径就能判定」的匹配类型；其余类型（empty_dir 等）在 protect
    里没有意义，直接忽略而不是报错，避免规则包复用同一个 match 结构时炸掉。
    """
    matchers: list[_Matcher] = []
    for rule in rules:
        match = rule.get("match") or {}
        mtype = match.get("type")
        if mtype == "dir_name":
            names = frozenset(str(n) for n in (match.get("names") or ()))
            if names:
                matchers.append(_Matcher(str(rule.get("id", "")), str(rule.get("reason", "")), mtype, names=names))
        elif mtype == "file_name":
            names = frozenset(str(n) for n in (match.get("names") or ()))
            if names:
                matchers.append(_Matcher(str(rule.get("id", "")), str(rule.get("reason", "")), mtype, names=names))
        elif mtype == "name_glob":
            globs = tuple(str(g) for g in (match.get("globs") or match.get("patterns") or ()))
            if globs:
                matchers.append(_Matcher(str(rule.get("id", "")), str(rule.get("reason", "")), mtype, globs=globs))
        elif mtype == "suffix":
            suffixes = _norm_suffixes(match.get("suffixes"))
            if suffixes:
                matchers.append(_Matcher(str(rule.get("id", "")), str(rule.get("reason", "")), mtype, names=suffixes))
        elif mtype == "path_glob":
            patterns = tuple(str(p) for p in (match.get("patterns") or ()))
            if patterns:
                matchers.append(
                    _Matcher(str(rule.get("id", "")), str(rule.get("reason", "")), mtype, patterns=patterns)
                )
    return matchers


def matcher_hits(m: _Matcher, rel: str, is_dir: bool) -> bool:
    """单条保护规则是否命中。

    从 `evaluate_protect` 抽出来单独暴露，是为了让 `explain` 能逐条摊开判定明细时
    复用**同一套**语义——若解释另写一份判定，两处迟早漂移，而漂移的解释比没有解释更危险。
    """
    if not rel:
        return False
    parts = rel.split("/")
    basename = parts[-1]
    ancestors = parts[:-1]

    if m.kind == "dir_name":
        # 祖先命中即保护后代：.git 被保护 ⇒ .git/objects/... 不可被自动搬走
        return any(a in m.names for a in ancestors) or (is_dir and basename in m.names)
    if m.kind == "file_name":
        return not is_dir and basename in m.names
    if m.kind == "name_glob":
        return not is_dir and any(fnmatch.fnmatchcase(basename, g) for g in m.globs)
    if m.kind == "suffix":
        if is_dir:
            return False
        dot = basename.rfind(".")
        return dot > 0 and basename[dot:].lower() in m.names
    if m.kind == "path_glob":
        return any(fnmatch.fnmatchcase(rel, p) for p in m.patterns)
    return False


def evaluate_protect(
    matchers: list[_Matcher],
    rel: str,
    is_dir: bool,
) -> tuple[str, str] | None:
    """判断相对路径是否受保护；命中返回 (保护规则 id, 原因)，否则 None。

    rel 必须是相对目标根的 POSIX 路径（与 inventory 中的 path 同形态）。
    语义是「先命中者胜」，因此调用方传入的 matchers 顺序即为优先级顺序。
    """
    if not rel:
        return None
    for m in matchers:
        if matcher_hits(m, rel, is_dir):
            return m.rule_id, m.reason
    return None
