"""隔离区：移动（非删除）、还原、彻底清除。

默认只 shutil.move 到 `.dirtidy/quarantine/<run_id>/`，保留相对路径结构以便还原。
彻底清除必须走 purge，且 CLI 要求 --yes。
"""

from __future__ import annotations

import os
import shutil
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path

from dirtidy.models import QuarantineEntry, RuleHit, read_json, resolve_under, utc_now_iso, write_json
from dirtidy.scan.scanner import file_sha256

#: 保护判定签名：给定相对路径与是否目录，命中返回 (保护规则 ID, 原因)，否则 None。
#: 与 `rules.protect.evaluate_protect` 同形，便于把判定器直接传进来而不必包一层。
ProtectCheck = Callable[[str, bool], "tuple[str, str] | None"]


def nothing_protected(rel: str, is_dir: bool) -> None:
    """「此处不可能有受保护内容」的显式声明。

    只在一处使用：`sweep_empty_dirs` 处理的是**当场确认为空**的目录，子树里没有东西，
    漏洞在数学上不成立。写成有名字的函数而不是 `lambda: None`，是为了让这个例外
    在调用处一眼可见——而不是看起来像「忘了传」。
    """
    return None


def drop_covered_descendants(hits: Sequence[RuleHit]) -> list[RuleHit]:
    """丢弃「已被某个目录命中覆盖」的后代条目。

    目录整棵搬走时，它内部的文件自然跟着走。若把父子都当成独立条目：
      * 清单会多出一条**永远还原不回去**的记录——还原时目标路径已被父目录的还原
        创建出来，`restore_entries` 遇到「目标已存在」会跳过，于是这条记录永久卡住；
      * 隔离区里留下一个死空目录；
      * dry-run 的条数与真实产物对不上。

    纯集合运算，不访问文件系统。
    """
    dirs = {h.path for h in hits if h.is_dir}
    if not dirs:
        return list(hits)
    return [h for h in hits if not any(h.path.startswith(f"{d}/") for d in dirs)]


def _first_protected_under(
    root: Path, base: Path, is_protected: ProtectCheck
) -> tuple[str, str, str] | None:
    """在 base 子树里找第一条受保护路径。

    返回 (受保护路径, 保护规则 ID, 原因)；找不到返回 None。发现即早退。
    带上具体路径是为了让解释与 protection.json 能说出「是哪个文件挡的」——
    「子树里有受保护内容」这种说法查不到责任人。
    """
    unreadable: list[OSError] = []
    for dirpath, dirnames, filenames in os.walk(base, onerror=unreadable.append):
        here = Path(dirpath)
        for name, is_dir in ((n, True) for n in dirnames):
            found = _check_rel(root, here / name, is_dir, is_protected)
            if found is not None:
                return found
        for name, is_dir in ((n, False) for n in filenames):
            found = _check_rel(root, here / name, is_dir, is_protected)
            if found is not None:
                return found
    if unreadable:
        # 读不动的子树无法证明「里面没有受保护内容」。保护优先于清理，
        # 因此宁可不动，也不赌它安全。
        return ("", "__unreadable__", f"子树有 {len(unreadable)} 处不可读，无法确认其中没有受保护内容")
    return None


def _check_rel(
    root: Path, path: Path, is_dir: bool, is_protected: ProtectCheck
) -> tuple[str, str, str] | None:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return None
    if not rel or rel == ".":
        return None
    guard = is_protected(rel, is_dir)
    if guard is None:
        return None
    return (rel, guard[0], guard[1])


def find_protected_descendants(
    root: Path,
    hits: Sequence[RuleHit],
    is_protected: ProtectCheck,
) -> dict[str, tuple[str, str, str]]:
    """找出「子树内含受保护路径」的目录命中 → {命中路径: (受保护路径, 保护规则 ID, 原因)}。

    保护清单的最高优先级语义是「命中即永不自动隔离」。只校验命中路径**本身**是不够的：
    一个命中目录会把内部受保护的密钥 / 版本库文件一起搬走，保护就形同虚设。
    —— `node_modules` 命中 dep 规则，`node_modules/.env`、`node_modules/id_rsa`
    就会被连锅端走，而这两条本该由内置清单兜底。

    这里在**实盘**上遍历该目录，而不是查 scan 时的 inventory 快照：
      * 快照会过期——扫描之后新增的文件不在里面；
      * `.git` / `.dirtidy` 这类被扫描器跳过的目录本就不在快照里。
    代价只与「将要被搬走的目录」大小成正比，且发现第一条即早退，不随全树规模增长。

    **不设枚举上限**：这里的结论会被当作「会不会拦下」的依据，截断就等于给出错误答案。
    调用方（含 explain）应当接受这个成本——它与真正执行一次该目录的隔离相当。
    """
    blocks: dict[str, tuple[str, str, str]] = {}
    for hit in hits:
        if not hit.is_dir or hit.path in blocks:
            continue
        try:
            base = resolve_under(root, hit.path)
        except ValueError:
            continue
        if not base.is_dir():
            continue
        found = _first_protected_under(root, base, is_protected)
        if found is not None:
            blocks[hit.path] = found
    return blocks


def plan_quarantine(
    root: Path,
    hits: Sequence[RuleHit],
    *,
    is_protected: ProtectCheck | None = None,
) -> tuple[list[RuleHit], list[dict]]:
    """把「命中列表」拆成「可执行列表 + 被保护拦下列表」。

    dry-run 预测与真实执行**共用这一个函数**，否则两条路迟早漂移——
    而「预览说会搬、实跑不搬」或反过来的工具，比没有预览更危险。

    顺序不可颠倒：
      1. 先按保护拦下「子树内含受保护路径」的目录命中（保护优先）；
      2. 再丢弃被覆盖的后代条目——若先丢弃，被拦下目录里的子文件仍会留在列表里。
    """
    blocked: list[dict] = []
    survivors: list[RuleHit] = list(hits)

    if is_protected is not None:
        blocks = find_protected_descendants(root, survivors, is_protected)
        if blocks:
            rest: list[RuleHit] = []
            for hit in survivors:
                found = blocks.get(hit.path)
                if found is None:
                    rest.append(hit)
                    continue
                descendant, protect_rule_id, protect_reason = found
                blocked.append(
                    {
                        "path": hit.path,
                        "protect_rule_id": protect_rule_id,
                        "protect_reason": (
                            f"子树内含受保护路径 {descendant}（{protect_reason}）"
                            if descendant
                            else f"子树内含受保护内容（{protect_reason}）"
                        ),
                        "blocking_path": descendant,
                        "intended_rule_id": hit.rule_id,
                        "intended_action": "auto_quarantine",
                        "blocked_auto": True,
                        "is_dir": hit.is_dir,
                        "blocked_at": "protected_descendant",
                    }
                )
            survivors = rest

    return drop_covered_descendants(survivors), blocked


def _safe_dest(quarantine_dir: Path, original_rel: str) -> Path:
    """在隔离区内生成不冲突的目标路径；冲突时加 .__qN 后缀。

    先经 resolve_under 校验，保证隔离目标不会因 `..` 逃出隔离目录。
    """
    dest = resolve_under(quarantine_dir, original_rel)
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest
    stem = dest.name
    parent = dest.parent
    i = 1
    while True:
        candidate = parent / f"{stem}.__q{i}"
        if not candidate.exists():
            candidate.parent.mkdir(parents=True, exist_ok=True)
            return candidate
        i += 1


def quarantine_hits(
    root: Path,
    run_id: str,
    hits: list[RuleHit],
    quarantine_dir: Path,
    manifest_path: Path,
    *,
    is_protected: ProtectCheck,
    known_hashes: dict[str, str | None] | None = None,
    blocked: list[dict] | None = None,
) -> list[QuarantineEntry]:
    """将命中项移入隔离区并追加/更新 manifest。

    known_hashes: 可选 path→sha256（来自 inventory），避免移动后再算。

    **is_protected 是必填的**，不是可选优化。曾经它是可选的，于是 `scan --apply`
    这条调用路径漏传，`node_modules/.env`、`node_modules/id_rsa` 被连锅端走——
    必填让「忘了传」变成 TypeError，而不是一次静默的保护失效。
    确实不存在受保护内容的场景（`sweep_empty_dirs` 的空目录）显式传 `nothing_protected`。

    命中目录若子树内含受保护路径则整条拦下，其后代条目也不再单独搬。传入 `blocked`
    可把拦下明细回传给调用方写进 protection.json（不传则只静默丢弃，不推荐）。

    过滤后剩余的条目已互不嵌套，但**仍按路径深度倒序处理**：隔离区内已有同名
    残留时会走 `.__qN` 改名，顺序固定能让产物可复现。
    """
    del run_id  # 隔离目录已由调用方按 run_id 选好；保留参数便于 API 稳定
    executable, blocks = plan_quarantine(root, hits, is_protected=is_protected)
    if blocked is not None:
        blocked.extend(blocks)

    existing: list[dict] = []
    if manifest_path.exists():
        existing = read_json(manifest_path)
    entries = [QuarantineEntry(**e) for e in existing]
    seen_orig = {e.original_path for e in entries}
    known_hashes = known_hashes or {}

    ordered = sorted(executable, key=lambda h: h.path.count("/"), reverse=True)

    try:
        for hit in ordered:
            if hit.path in seen_orig:
                continue
            try:
                src = resolve_under(root, hit.path)
                dest = _safe_dest(quarantine_dir, hit.path)
            except ValueError:
                # 越界路径（外部输入的 .. / 绝对路径）直接跳过，不搬动任何根外文件
                continue
            if not src.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            size = 0
            digest = known_hashes.get(hit.path)
            if dest.is_file():
                size = dest.stat().st_size
                if digest is None:
                    try:
                        digest = file_sha256(dest)
                    except OSError:
                        digest = None
            state = root / ".dirtidy"
            try:
                qrel = str(dest.relative_to(state))
            except ValueError:
                qrel = str(dest)
            entry = QuarantineEntry(
                id=uuid.uuid4().hex[:12],
                original_path=hit.path,
                quarantine_path=qrel,
                rule_id=hit.rule_id,
                reason=hit.reason,
                size=size,
                sha256=digest,
                quarantined_at=utc_now_iso(),
                is_dir=hit.is_dir,
            )
            entries.append(entry)
            seen_orig.add(hit.path)
    finally:
        # 即使中途抛错（含 Ctrl-C 的 KeyboardInterrupt），也必须把**已经搬走**的条目落盘。
        # 否则会出现「文件已离开原位、清单里却查不到」——用户不知道它去哪了，也无从还原，
        # 这是本工具最不能出的错。写在 finally 里，成功路径也仍然只写一次。
        #
        # 覆盖范围：Python 异常与 SIGINT。被 SIGKILL / OOM 硬杀仍会丢掉最后几条，
        # 彻底解决需要 append-only 日志，已登记在 `docs/08-稳定性与性能设计.md`。
        write_json(manifest_path, [e.to_dict() for e in entries])

    return entries


def restore_entries(
    root: Path,
    manifest_path: Path,
    *,
    entry_ids: set[str] | None = None,
    restore_all: bool = False,
) -> list[str]:
    """按 id 或全部还原；若原路径已存在文件则跳过该条（避免覆盖）。"""
    if not manifest_path.exists():
        return []
    raw = read_json(manifest_path)
    remaining: list[dict] = []
    restored: list[str] = []
    state = root / ".dirtidy"

    for item in raw:
        eid = item["id"]
        if not restore_all and (entry_ids is None or eid not in entry_ids):
            remaining.append(item)
            continue
        qpath = Path(item["quarantine_path"])
        src = qpath if qpath.is_absolute() else state / qpath
        try:
            dest = resolve_under(root, item["original_path"])
        except ValueError:
            # 原始路径越界（历史脏数据/被篡改的 manifest）：拒绝还原，保留在清单里
            remaining.append(item)
            continue
        if not src.exists():
            remaining.append(item)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.exists():
            remaining.append(item)
            continue
        shutil.move(str(src), str(dest))
        restored.append(item["original_path"])

    write_json(manifest_path, remaining)
    return restored


def purge_quarantine_dir(quarantine_dir: Path) -> None:
    """物理删除隔离区目录树；不可恢复。"""
    if quarantine_dir.exists():
        shutil.rmtree(quarantine_dir)


def _state_bases(root: Path) -> list[Path]:
    """隔离清单里的 `quarantine_path` 可能基于哪几个目录。

    正常只有 `<root>/.dirtidy` 一个；但 `DIRTIDY_STATE_DIR` 覆盖后实体落在别处，
    而清单里的相对路径仍是按 `<root>/.dirtidy` 写的（写入端如此）。两个都认，
    否则一旦设了环境变量，purge 就会因为「路径不在允许范围内」而全部拒绝。
    """
    from dirtidy.models import state_root

    out: list[Path] = []
    for base in (state_root(root), root / ".dirtidy"):
        try:
            resolved = base.resolve()
        except OSError:
            continue
        if resolved not in out:
            out.append(resolved)
    return out


def quarantine_entry_path(root: Path, entry: dict) -> Path | None:
    """把清单里的 quarantine_path 解析成绝对路径；越界则返回 None。

    历史清单里可能存过绝对路径，也可能是被手改过的脏数据。这里守住
    「不碰状态目录之外的东西」——purge 是物理删除，一次越界就是删掉用户的
    真实文件，代价不可逆。界面统计占用空间时也走这里，两边口径一致。
    """
    raw = str(entry.get("quarantine_path") or "")
    if not raw:
        return None
    bases = _state_bases(root)
    if not bases:
        return None
    p = Path(raw)
    if not p.is_absolute():
        for base in bases:
            candidate = base / p
            if candidate.exists():
                p = candidate
                break
        else:
            p = bases[0] / p
    try:
        p_abs = p.resolve()
    except OSError:
        return None
    if not any(p_abs == b or str(p_abs).startswith(str(b) + os.sep) for b in bases):
        return None
    return p


def tree_size(path: Path) -> int:
    """目录树的真实占用（字节）。单个条目读不到就按 0 计。

    清单里的 `size` 只对文件有值（目录恒为 0），直接汇总会显示「0 B」——
    而用户想知道的恰恰是「隔离区占了多少空间」。所以这里实算。

    不 follow symlink：与扫描侧一致，避免顺着链接跑到根外面去统计。
    """
    try:
        if path.is_file():
            return path.stat().st_size
    except OSError:
        return 0
    if not path.is_dir():
        return 0
    total = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(Path(entry.path))
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


def purge_entries(
    root: Path,
    manifest_path: Path,
    *,
    entry_ids: set[str] | None = None,
    purge_all: bool = False,
) -> list[str]:
    """物理删除隔离区中的条目（按 id 或全部），并从 manifest 移除。**不可恢复。**

    与 `purge_quarantine_dir` 的区别：那个删的是整个隔离目录、清单留在原地（于是
    清单里的条目全部指向不存在的位置）；这个按条删，**清单与磁盘同时收敛**，
    界面上「已隔离」列表因此始终是可信的——用户不必猜某一条还在不在。

    - `entry_ids=None` 且 `purge_all=False`：什么都不做（防「忘了传参就删光」）。
    - 目录条目用 rmtree、文件用 unlink；条目指向的实体已不存在时仍从清单移除，
      否则界面会留下一条永远删不掉的幽灵记录。
    - 越出状态目录的路径**拒绝删除**，但**保留在清单里**，让问题可见。
    """
    if not manifest_path.exists():
        return []
    raw = read_json(manifest_path)
    if not isinstance(raw, list):
        return []

    remaining: list[dict] = []
    purged: list[str] = []

    for item in raw:
        if not isinstance(item, dict):
            continue
        eid = str(item.get("id") or "")
        if not purge_all and (entry_ids is None or eid not in entry_ids):
            remaining.append(item)
            continue

        target = quarantine_entry_path(root, item)
        if target is None:
            # 路径越界或缺失：不动磁盘，也不从清单里抹掉——这条需要人看一眼
            remaining.append(item)
            continue
        try:
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
        except OSError:
            # 删不掉（占用中 / 权限）就留在清单里，下次还能重试
            remaining.append(item)
            continue
        purged.append(str(item.get("original_path") or ""))

    write_json(manifest_path, remaining)
    return purged


def _read_manifest(manifest_path: Path) -> list[dict]:
    if not manifest_path.exists():
        return []
    data = read_json(manifest_path)
    return data if isinstance(data, list) else []


def _parent_rel(rel: str) -> str | None:
    """父目录相对路径；顶层条目返回 None（目标根本身不处理）。"""
    idx = rel.rfind("/")
    return rel[:idx] if idx > 0 else None


def _is_skipped_rel(rel: str) -> bool:
    from dirtidy.scan.scanner import SKIP_DIR_NAMES

    parts = rel.split("/")
    return any(p in SKIP_DIR_NAMES for p in parts)


def sweep_empty_dirs(
    root: Path,
    run_id: str,
    quarantine_dir: Path,
    manifest_path: Path,
    *,
    max_rounds: int = 8,
    is_protected: ProtectCheck | None = None,
) -> list[QuarantineEntry]:
    """隔离子项后，多轮把新变成空的父目录也隔离，直到稳定或达到轮数上限。

    扫描阶段的 empty_dir 规则看不到「子项搬走之后才空」的目录，所以在隔离之后补这一步。

    实现上只在**被隔离条目的祖先目录**上展开，不再整棵树 `os.walk`：
    原实现最多 8 轮全树遍历，几百万文件时开销是灾难性的；现在代价与隔离条目数成正比。
    """
    quarantine_dir.mkdir(parents=True, exist_ok=True)
    manifest = _read_manifest(manifest_path)
    if not manifest:
        return []

    done = {e.get("original_path", "") for e in manifest}
    frontier: set[str] = set()
    for entry in manifest:
        parent = _parent_rel(str(entry.get("original_path", "")))
        if parent:
            frontier.add(parent)

    for _ in range(max_rounds):
        empties: list[str] = []
        for rel in sorted(frontier):
            if rel in done or _is_skipped_rel(rel):
                continue
            if is_protected is not None and is_protected(rel, True) is not None:
                continue
            p = root / rel
            try:
                if not p.is_dir() or next(p.iterdir(), None) is not None:
                    continue
            except OSError:
                continue
            empties.append(rel)
        if not empties:
            break

        hits = [
            RuleHit(
                path=rel,
                rule_id="empty_dir_after_move",
                action="auto_quarantine",
                reason="隔离子项后留下的空目录",
                is_dir=True,
            )
            for rel in empties
        ]
        before = len(_read_manifest(manifest_path))
        # 命中项都是**当场确认为空**的目录，子树里没有东西，
        # 「目录命中带走受保护后代」在此不成立——显式声明而不是漏传。
        quarantine_hits(
            root,
            run_id,
            hits,
            quarantine_dir,
            manifest_path,
            is_protected=is_protected or nothing_protected,
        )
        after = len(_read_manifest(manifest_path))
        done.update(empties)
        frontier = {p for p in (_parent_rel(r) for r in empties) if p and p not in done}
        if after == before:
            break

    return [QuarantineEntry(**e) for e in _read_manifest(manifest_path)]
