"""内容 hash：重复文件分组、归档成员 hash 对照（解压残留判定）。

解压残留禁止靠「同名目录」猜测，只认：归档内成员内容 sha256 与磁盘文件 sha256 的重合比例。

归档解析被拆成**独立的一步**（`probe_archives`），因为成员的解压后字节数要先拿到，
才能决定磁盘上哪些文件值得算 hash（见 `scanner.hash_items`）。
"""

from __future__ import annotations

import hashlib
import tarfile
import zipfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

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

#: 归档解析预算：单个归档最多读这么多成员 / 这么多解压后字节。
#: 没有上限时，一个 50 GB 的 tar 会把内存和 IO 全吃掉——而它顶多是个「候选」。
MAX_ARCHIVE_MEMBERS = 20_000
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
#: 单个归档文件本身超过这个大小就跳过成员对照（标为待人工确认）。
MAX_ARCHIVE_FILE_BYTES = 4 * 1024 * 1024 * 1024
#: 单个重复组最多登记多少条路径，避免极端情况下候选文件无限膨胀。
MAX_DUPLICATE_PATHS = 1000

#: 认得出的归档后缀（小写比较）。
ARCHIVE_SUFFIXES = (".zip", ".tar", ".tar.gz", ".tgz")


@dataclass(slots=True)
class ArchiveProbe:
    """一个归档的成员解析结果。

    members 是 `[(成员路径, 解压后字节数, 内容 sha256), ...]`。
    `too_large` / `error` 二者之一成立时 members 不可信（超预算时为已读到的部分）。
    """

    path: str
    size: int
    members: list[tuple[str, int, str]]
    error: str | None = None
    too_large: bool = False


def is_archive_path(path: str) -> bool:
    """按后缀判断是否当作归档解析（只看后缀，不做内容嗅探）。"""
    return path.lower().endswith(ARCHIVE_SUFFIXES)


def iter_archive_members(
    archive: Path,
    *,
    max_members: int = MAX_ARCHIVE_MEMBERS,
    max_bytes: int = MAX_ARCHIVE_BYTES,
) -> tuple[list[tuple[str, int, str]], str | None]:
    """读取归档各文件成员，返回 [(成员路径, 解压后字节数, 内容sha256), ...]；失败时第二项为错误信息。

    超过 max_members / max_bytes 时停止并返回错误串：调用方会把它标成
    `archive_review`（待人工），而不是继续把一个巨型归档读完。

    字节数按**实际读到的解压后字节**计（不是归档里的声明值）：这样它与磁盘文件
    的 `st_size` 是同一把尺子，可以直接拿来决定「哪些磁盘文件值得算 hash」。
    """
    members: list[tuple[str, int, str]] = []
    consumed = 0
    truncated = False
    name = archive.name.lower()

    def _digest(fp: Any) -> tuple[int, str]:
        nonlocal consumed, truncated
        h = hashlib.sha256()
        size = 0
        while True:
            chunk = fp.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            consumed += len(chunk)
            if consumed > max_bytes:
                truncated = True
                break
            h.update(chunk)
        return size, h.hexdigest()

    try:
        if name.endswith(".zip"):
            with zipfile.ZipFile(archive, "r") as zf:
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    if len(members) >= max_members:
                        truncated = True
                        break
                    with zf.open(info, "r") as fp:
                        size, digest = _digest(fp)
                    members.append((info.filename, size, digest))
                    if truncated:
                        break
            if truncated:
                return members, f"归档超出解析预算（成员 {len(members)}，已读 {consumed} 字节）"
            return members, None

        if name.endswith(".tar.gz") or name.endswith(".tgz") or name.endswith(".tar"):
            mode: Literal["r:gz", "r:"] = (
                "r:gz" if (name.endswith(".tar.gz") or name.endswith(".tgz")) else "r:"
            )
            with tarfile.open(archive, mode) as tf:
                for member in tf.getmembers():
                    if not member.isfile():
                        continue
                    if len(members) >= max_members:
                        truncated = True
                        break
                    f = tf.extractfile(member)
                    if f is None:
                        continue
                    size, digest = _digest(f)
                    members.append((member.name, size, digest))
                    if truncated:
                        break
            if truncated:
                return members, f"归档超出解析预算（成员 {len(members)}，已读 {consumed} 字节）"
            return members, None
    except (zipfile.BadZipFile, tarfile.TarError, OSError, RuntimeError, NotImplementedError) as e:
        return [], str(e)

    return [], "unsupported_archive"


def probe_archives(
    root: Path,
    items: Iterable[InventoryItem],
    *,
    max_file_bytes: int | None = None,
) -> list[ArchiveProbe]:
    """解析所有 zip/tar 的成员；没有归档时返回空列表（调用方据此跳过整段逻辑）。

    单独成一步的原因：成员的解压后字节数必须先拿到，才能算出「磁盘上哪些 size
    值得算 hash」。这一步是**只读**的，不改动任何用户文件。

    `max_file_bytes` 默认 `None` 而不是直接写 `MAX_ARCHIVE_FILE_BYTES`：
    默认参数在函数定义时就绑定，写成常量会让运行期改模块常量（测试与调参都这么做）失效。
    """
    limit = MAX_ARCHIVE_FILE_BYTES if max_file_bytes is None else max_file_bytes
    out: list[ArchiveProbe] = []
    for item in items:
        if item.is_dir or not is_archive_path(item.path):
            continue
        if item.size > limit:
            out.append(ArchiveProbe(path=item.path, size=item.size, members=[], too_large=True))
            continue
        members, err = iter_archive_members(root / item.path)
        out.append(ArchiveProbe(path=item.path, size=item.size, members=members, error=err))
    return out


def archive_member_sizes(probes: Iterable[ArchiveProbe]) -> set[int]:
    """所有归档成员的解压后字节数集合——用于决定磁盘上哪些文件需要算 hash。

    「同内容必然同字节数」：一个能与成员 hash 对上的磁盘文件，其 size 一定在这个集合里。
    因此按这个集合选文件算 hash，归档残留检测**不会漏检**。
    """
    return {size for p in probes for _name, size, _digest in p.members}


def candidate_duplicate_sizes(items: Iterable[InventoryItem]) -> set[int]:
    """可能出现重复组的 size 集合：同一 size 至少两个文件。

    0 字节被排除：`find_duplicate_groups` 本就跳过空文件，给它们算 hash 纯属浪费
    （一个全空文件的树会把 sha256 字符串白算几百万份）。

    只用两个 `set[int]`，且装的是 `items` 里已有的 int 对象引用，不新造整数；
    第一个 set 在返回前释放，峰值与文件数同阶但不与 `items` 叠加成第二份等长结构。
    """
    seen: set[int] = set()
    dups: set[int] = set()
    for it in items:
        if it.is_dir or it.error or it.size == 0:
            continue
        if it.size in seen:
            dups.add(it.size)
        else:
            seen.add(it.size)
    return dups


def find_duplicate_groups(items: list[InventoryItem]) -> list[Candidate]:
    """相同内容 hash 且份数≥2 → duplicate_group；保留哪份一律人工选。

    单个组超过 MAX_DUPLICATE_PATHS 时截断并标记，避免一个「全盘同内容」的组
    把候选文件和审计报告撑爆。

    0 字节文件被排除：它们的 hash 天然全都相同，凑成的「重复组」没有任何整理价值，
    还会把 `.env` / `id_rsa` 这类空占位文件混进来，诱导用户点「其余标记隔离」。
    空文件自有 `empty_file` 规则处理（进候选）。
    """
    groups: dict[str, list[str]] = defaultdict(list)
    overflow: dict[str, int] = defaultdict(int)  # 只对超限的组计数，避免多背一个全量字典
    for item in items:
        if item.is_dir or not item.sha256 or item.error or item.size == 0:
            continue
        bucket = groups[item.sha256]
        if len(bucket) < MAX_DUPLICATE_PATHS:
            bucket.append(item.path)
        else:
            overflow[item.sha256] += 1
    out: list[Candidate] = []
    for digest, paths in groups.items():
        if len(paths) < 2:
            continue
        total = len(paths) + overflow.get(digest, 0)
        truncated = total > len(paths)
        out.append(
            Candidate(
                kind="duplicate_group",
                paths=sorted(paths),
                rule_id="duplicate_content",
                reason="内容逐字节相同，只是散在不同位置"
                + ("（该组过大，列表已截断）" if truncated else ""),
                meta={"sha256": digest, "count": total, "truncated": truncated},
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
    probes: list[ArchiveProbe] | None = None,
    threshold: float = 0.8,
) -> list[Candidate]:
    """对 zip/tar 做成员 hash↔磁盘 hash 对照；达阈值标 archive_extracted。

    未达阈值时仍标 archive_content（含成员结构启发式），默认仅候选。

    `probes` 由调用方传入可复用已解析的成员（编排层先 `probe_archives` 再算 hash，
    正是靠它才知道该给哪些文件算 hash）；不传则就地解析，方便单独调用。

    **调用方只需保证「size 与归档成员相同的磁盘文件都算过 hash」**，无需全量算：
    能与成员 hash 对上的磁盘文件必然同 size。只算一部分而漏检的情形在数学上不成立。
    """
    if probes is None:
        probes = probe_archives(root, items)
    if not probes:
        # 没有归档就不必建磁盘 hash 集合：5M 文件下这是一份约 500 MB 的 set
        return []

    disk_hashes = {i.sha256 for i in items if i.sha256 and not i.is_dir}
    out: list[Candidate] = []

    for probe in probes:
        if probe.too_large:
            out.append(
                Candidate(
                    kind="archive_review",
                    paths=[probe.path],
                    rule_id="archive_too_large",
                    reason=f"归档体积 {probe.size} 字节，超过成员对照上限，交人工确认",
                    meta={"size": probe.size},
                )
            )
            continue
        if probe.error:
            out.append(
                Candidate(
                    kind="archive_review",
                    paths=[probe.path],
                    rule_id="archive_unreadable",
                    reason=f"无法读取归档成员: {probe.error}",
                    meta={},
                )
            )
            continue
        if not probe.members:
            continue
        digests = [d for _n, _s, d in probe.members]
        names = [n for n, _s, _d in probe.members]
        hits = sum(1 for d in digests if d in disk_hashes)
        ratio = hits / len(digests)
        kind_guess = _probe_archive_kind(names)
        if hits >= 1 and ratio >= threshold:
            out.append(
                Candidate(
                    kind="archive_extracted",
                    paths=[probe.path],
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
                    paths=[probe.path],
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
