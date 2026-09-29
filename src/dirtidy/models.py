"""公共数据模型与路径约定。

状态目录约定（均在目标根下）：
- `.dirtidy/runs/<run_id>/`：一次整理运行的产物
- `.dirtidy/quarantine/<run_id>/`：隔离区实体文件
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now_iso() -> str:
    """UTC ISO8601 时间戳（秒精度），用于 run_id / 审计字段。"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
    return package_root() / "configs" / "rules"


def state_root(target_root: Path) -> Path:
    return target_root / ".dirtidy"


def run_dir(target_root: Path, run_id: str) -> Path:
    return state_root(target_root) / "runs" / run_id


def quarantine_root(target_root: Path, run_id: str) -> Path:
    return state_root(target_root) / "quarantine" / run_id
