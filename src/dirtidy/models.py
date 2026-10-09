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


def package_root() -> Path:
    """项目根目录（含 configs/），editable 安装时指向源码仓根。"""
    return Path(__file__).resolve().parents[2]


def default_rules_dir() -> Path:
    return Path(__file__).resolve().parent / "rules" / "packs"


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
