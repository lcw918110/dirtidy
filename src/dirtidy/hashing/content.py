"""内容 hash：重复文件分组、归档成员 hash 对照（解压残留判定）。

解压残留禁止靠「同名目录」猜测，只认：归档内成员内容 sha256 与磁盘文件 sha256 的重合比例。
"""

from __future__ import annotations

import hashlib
import tarfile
import zipfile
from collections import defaultdict
from pathlib import Path
from typing import Literal

from dirtidy.models import Candidate, InventoryItem

# 成员名启发式：用于「旁路压缩包像不像安装分发」的内容探测（非自动隔离）
_INSTALLER_NAME_HINTS = (
    ".msi",
    ".dmg",
    ".exe",
    ".deb",
    ".rpm",
    ".pkg",
    "setup",
    "install",
    "uninstall",
    "debian-binary",
    "control.tar",
    "data.tar",
)
_SOURCE_NAME_HINTS = (
    ".py",
    ".js",
    ".ts",
    ".go",
    ".rs",
    ".java",
    ".c",
    ".h",
    "package.json",
    "pyproject.toml",
    "cargo.toml",
    "go.mod",
    "readme",
    "src/",
)
_INSTALLER_SUFFIXES = (".msi", ".dmg", ".exe", ".deb", ".rpm", ".pkg")


def iter_archive_members(archive: Path) -> tuple[list[tuple[str, str]], str | None]:
    """读取归档各文件成员，返回 [(成员路径, 内容sha256), ...]；失败时第二项为错误信息。"""
    members: list[tuple[str, str]] = []
    name = archive.name.lower()
    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    h = hashlib.sha256()
                    with zf.open(info, "r") as fp:
                        while True:
                            chunk = fp.read(1024 * 1024)
                            if not chunk:
                                break
                            h.update(chunk)
                    members.append((info.filename, h.hexdigest()))
            return members, None

        if name.endswith(".tar.gz") or name.endswith(".tgz") or name.endswith(".tar"):
            mode: Literal["r:gz", "r:"] = (
                "r:gz" if (name.endswith(".tar.gz") or name.endswith(".tgz")) else "r:"
            )
            with tarfile.open(archive, mode) as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    f = tf.extractfile(member)
                    if f is None:
                        continue
                    h = hashlib.sha256()
                    while True:
                        chunk = f.read(1024 * 1024)
                        if not chunk:
                            break
                        h.update(chunk)
                    members.append((member.name, h.hexdigest()))
            return members, None
    except (zipfile.BadZipFile, tarfile.TarError, OSError, RuntimeError, NotImplementedError) as e:
        return [], str(e)

    return [], "unsupported_archive"


def find_duplicate_groups(items: list[InventoryItem]) -> list[Candidate]:
    """相同内容 hash 且份数≥2 → duplicate_group；保留哪份一律人工选。"""
    groups: dict[str, list[str]] = defaultdict(list)
    for item in items:
        if item.is_dir or not item.sha256 or item.error:
            continue
        groups[item.sha256].append(item.path)
    out: list[Candidate] = []
    for digest, paths in groups.items():
        if len(paths) < 2:
            continue
        out.append(
            Candidate(
                kind="duplicate_group",
                paths=sorted(paths),
                rule_id="duplicate_content",
                reason="相同内容 hash，需人工选留",
                meta={"sha256": digest},
            )
        )
    return out


def _probe_archive_kind(member_names: list[str]) -> str:
    """根据成员路径名粗分：installer_like / source_like / mixed。

    顶层出现安装包后缀，且源码线索不多时，直接判为安装包类。
    """
    lower = [n.replace("\\", "/").lower() for n in member_names]
    inst = sum(1 for n in lower if any(h in n for h in _INSTALLER_NAME_HINTS))
    src = sum(1 for n in lower if any(h in n for h in _SOURCE_NAME_HINTS))
    has_installer_file = any(n.endswith(_INSTALLER_SUFFIXES) for n in lower)
    if has_installer_file and inst >= src:
        return "installer_like"
    if inst > src and inst > 0:
        return "installer_like"
    if src > inst and src > 0:
        return "source_like"
    return "mixed"


def find_extracted_archives(
    root: Path,
    items: list[InventoryItem],
    *,
    threshold: float = 0.8,
) -> list[Candidate]:
    """对 zip/tar 做成员 hash↔磁盘 hash 对照；达阈值标 archive_extracted。

    未达阈值时仍标 archive_content（含成员结构启发式），默认仅候选。
    """
    disk_hashes = {i.sha256 for i in items if i.sha256 and not i.is_dir}
    out: list[Candidate] = []

    for item in items:
        if item.is_dir:
            continue
        lower = item.path.lower()
        if not (
            lower.endswith(".zip")
            or lower.endswith(".tar")
            or lower.endswith(".tar.gz")
            or lower.endswith(".tgz")
        ):
            continue
        members, err = iter_archive_members(root / item.path)
        if err:
            out.append(
                Candidate(
                    kind="archive_review",
                    paths=[item.path],
                    rule_id="archive_unreadable",
                    reason=f"无法读取归档成员: {err}",
                    meta={},
                )
            )
            continue
        if not members:
            continue
        digests = [d for _, d in members]
        names = [n for n, _ in members]
        hits = sum(1 for d in digests if d in disk_hashes)
        ratio = hits / len(digests)
        kind_guess = _probe_archive_kind(names)
        if hits >= 1 and ratio >= threshold:
            out.append(
                Candidate(
                    kind="archive_extracted",
                    paths=[item.path],
                    rule_id="archive_hash_match",
                    reason=f"归档成员 hash 与磁盘文件对照命中 {hits}/{len(digests)} ({ratio:.0%})",
                    meta={
                        "matched": hits,
                        "members": len(digests),
                        "ratio": ratio,
                        "content_guess": kind_guess,
                    },
                )
            )
        else:
            out.append(
                Candidate(
                    kind="archive_content",
                    paths=[item.path],
                    rule_id="archive_content_probe",
                    reason=(
                        f"已按内容探测归档（成员 {len(digests)}，推断 {kind_guess}），"
                        f"hash 对照未达阈值 ({hits}/{len(digests)})"
                    ),
                    meta={
                        "matched": hits,
                        "members": len(digests),
                        "ratio": ratio,
                        "content_guess": kind_guess,
                    },
                )
            )
    return out
