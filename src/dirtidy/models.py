"""公共数据模型与路径约定。

状态目录约定（均在目标根下）：
- `.dirtidy/runs/<run_id>/`：一次整理运行的产物
- `.dirtidy/quarantine/<run_id>/`：隔离区实体文件
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """UTC ISO8601 时间戳（秒精度），用于 run_id / 审计字段。"""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


@dataclass
class InventoryItem:
    """扫描得到的单个路径条目（相对目标根）。"""

    path: str
    is_dir: bool
    size: int
    mtime: float
    suffix: str = ""
    sha256: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RuleHit:
    """YAML 规则命中；action=auto_quarantine 时进入待隔离列表。"""

    path: str
    rule_id: str
    action: str
    reason: str
    is_dir: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Candidate:
    """仅供人工/模型审阅的候选（默认不自动隔离）。"""

    kind: str
    paths: list[str]
    rule_id: str | None = None
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ProtectionRecord:
    """一条「规则本来想动它、但被保护清单拦下」的记录。

    intended_action 是**原本要执行**的动作（auto_quarantine / candidate_only）；
    blocked_auto 为真表示这条原本会进待隔离列表，被 protect 降级为「只看不动」。
    """

    path: str
    protect_rule_id: str
    protect_reason: str
    intended_rule_id: str
    intended_action: str
    is_dir: bool = False

    @property
    def blocked_auto(self) -> bool:
        return self.intended_action == "auto_quarantine"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["blocked_auto"] = self.blocked_auto
        return d


@dataclass
class RuleMatchResult:
    """规则匹配的完整结果。

    只有 hits 会被隔离；candidates 待人审；protected 是「想动但被保护拦下」的清单，
    既不隔离也不进候选，只用于审计与前端提示。
    """

    hits: list[RuleHit]
    candidates: list[Candidate]
    protected: list[ProtectionRecord] = field(default_factory=list)
    protect_policy: list[dict[str, Any]] = field(default_factory=list)

    @property
    def downgraded(self) -> list[ProtectionRecord]:
        """原本要自动隔离、被保护降级的条目。"""
        return [p for p in self.protected if p.blocked_auto]

    def to_dicts(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "hits": [h.to_dict() for h in self.hits],
            "candidates": [c.to_dict() for c in self.candidates],
            "protected": [p.to_dict() for p in self.protected],
            "protect_policy": list(self.protect_policy),
        }


@dataclass
class QuarantineEntry:
    """隔离区清单中的一条记录；quarantine_path 相对 `.dirtidy/`。"""

    id: str
    original_path: str
    quarantine_path: str
    rule_id: str | None
    reason: str
    size: int
    sha256: str | None
    quarantined_at: str
    is_dir: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Decision:
    """人工终审决策。action: keep | quarantine | archive。"""

    path: str
    action: str
    target_relpath: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def default_rules_dir() -> Path:
    return Path(__file__).resolve().parent / "rules" / "packs"


def example_config_path() -> Path:
    """包内自带的 models.example.yaml 路径。

    作为 package-data 随 wheel 一起安装，因此非 editable 安装的用户也能拿到一份
    可复制的模板；不再依赖「包位于源码树里」这一前提。
    """
    return Path(__file__).resolve().parent / "configs" / "models.example.yaml"


def _root_slug(target_root: Path) -> str:
    """为某个目标根生成稳定的短标识，用于外部状态目录下按根隔离。"""
    resolved = str(Path(target_root).resolve())
    digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]
    name = Path(resolved).name or "root"
    return f"{name}-{digest}"


def state_root(target_root: Path) -> Path:
    """状态目录。

    默认是目标根下的 `.dirtidy/`。若设置环境变量 `DIRTIDY_STATE_DIR`，
    则改为「该目录 / <根标识>/」，用于只读介质或不愿污染目标目录的场景；
    默认行为不变。
    """
    override = os.environ.get("DIRTIDY_STATE_DIR")
    if override:
        return Path(override).expanduser().resolve() / _root_slug(target_root)
    return target_root / ".dirtidy"


def run_dir(target_root: Path, run_id: str) -> Path:
    return state_root(target_root) / "runs" / run_id


def quarantine_root(target_root: Path, run_id: str) -> Path:
    return state_root(target_root) / "quarantine" / run_id


def resolve_under(root: Path, rel: str) -> Path:
    """把相对路径安全解析到 root 之下；越界（绝对路径 / `..` 逃逸）抛 ValueError。

    只做词法归一化（normpath），不 follow symlink：既能挡住路径穿越，
    又不会误伤指向外部的合法符号链接文件（扫描时 followlinks=False）。

    所有「用外部输入拼路径」的地方都必须走这里，保证不变量：结果永远落在 root 内。
    返回值沿用调用方传入的 root 形态（不做 resolve），避免 /var 与 /private/var
    之类的形态差异导致调用方比较失败。
    """
    if not rel or not isinstance(rel, str):
        raise ValueError(f"非法相对路径: {rel!r}")
    if os.path.isabs(rel) or Path(rel).is_absolute():
        raise ValueError(f"拒绝绝对路径: {rel!r}")
    root_abs = os.path.normpath(str(root.resolve()))
    joined = os.path.normpath(os.path.join(root_abs, rel))
    if joined != root_abs and not joined.startswith(root_abs + os.sep):
        raise ValueError(f"路径越界，超出目标根: {rel!r}")
    return Path(os.path.normpath(os.path.join(str(root), rel)))
