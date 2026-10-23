"""清理本机 Cursor 的全局状态库 `state.vscdb`。

背景
----
Cursor 把 Agent / Composer 的全部运行时数据塞进一张巨大的键值表
`cursorDiskKV`（约 838 万行）。其中：

- `agentKv:blob:<hash>` —— agent 运行时大对象（图片 / 附件 / 上下文快照），
  **体积占比 99% 以上**（实测约 75 GB）；
- `bubbleId:<cid>:<bid>` —— 会话气泡（对话正文），约 767 万行，体积很小；
- `checkpointId:` / `composerData:` —— 检查点与会话元数据，量级极小；
- `composerHeaders` 表 —— 会话索引（标题 / 时间 / 活跃度）；
- `ItemTable` 表 —— 用户设置（插件 / 主题 / 快捷键），**永久保留，永不删**。

所以只删会话正文几乎不缩库；真正能腾出空间的是**没人再引用的 blob**。

核心判定：blob 是不是孤儿
------------------------
blob 的 key 就是它的 64 位 hex 哈希（真机取证：key == sha256(value)，内容寻址）。
它被谁引用，藏在**其他行的 value 的 JSON 里**。
用 SQL `INSTR` 逐个哈希查等于全表扫（单个哈希实测 7 分钟无结果），不可行；
正确做法是**顺序读一遍 value，用正则一次性提取所有被引用的哈希**。

⚠️ 引用来源必须扫全
-------------------
只扫 `bubbleId` / `checkpointId` / `composerData` 三个前缀**是不够的**：
实测本机库中 `ofsContent:`（14 个）、`codeBlockPartialInlineDiffFates:`（35 个）、
`composerConvHeaders:`（2 个）等前缀也引用着 blob。漏扫它们会导致
**误删仍被引用的 blob**。因此本模块扫描**除 blob 自身以外的全部键前缀**，
宁可多扫几分钟，也不误删活数据。

⚠️⚠️ 2026-10-22 事故：value 扫描对 tool-result blob 结构性失明（必读）
--------------------------------------------------------------------
事故复盘（`docs/cursor-cleanup-integration/事故-20261022-会话无法还原.md`）：
Cursor 会话 UI 报 `3 missing blobs: 0203ebc74590, 00a8f0e42850, 0120dc7d37b2`，
整条会话判死。真机取证（`state.vscdb.pre-cleanup` 对照当前库）证明：

1. 这三个 12 位 ID 是三个被删 64 位 blob key 的**前缀**（key 空间对得上）。
2. blob key == sha256(value)（内容寻址），value 是 agent 工具调用结果
   （`{"role":"tool","content":[{"type":"tool-result","toolCallId":"call_…",…}]}`）。
3. 会话气泡通过 key 形态 `bubbleId:<cid>:<uuid>/t:call_<toolCallId>` 引用工具结果；
   **toolCallId 只出现在气泡 key 里，不出现在任何 value 里**。
4. 因此「扫全部 value 找 64 位哈希」对这类 blob **一个引用都找不到**——
   引用协议在 key 里，不在 value 里。扫得再全也救不了。
5. 三个 blob 在 10/21 预清理库存在、当前库不存在 → 确系清理删除。
   其中一个的 `/t:` 气泡也已不在库里（子代理/云端需求），**本地无法枚举**。

结论：**「无 64 位引用」证明不了「会话还原不需要它」**。在 Cursor 公开
还原协议之前，无法证明 blob 的年龄与完整归属，**执行路径不删除共享 blob**。
旧确认参数仅保留兼容，不再构成授权；候选扫描仅作只读诊断。

安全设计
--------
1. **默认干跑**。不传 `--apply` 只统计不改库——与 dirtidy 其他破坏性命令一致。
2. **绝不删 `ItemTable`**（用户设置）。
3. **执行路径不删共享 blob**：年龄与完整归属未知，确认语不能代替证明。
   只读候选统计仍保留 toolCallId 守卫，不等于可安全删除集合。
4. **在线模式绝不 VACUUM、绝不替换文件**（会锁死 Cursor）。
5. 写库用**小批次短事务 + 批间让路 + 锁退避重试**，尽量不影响 Cursor 使用。
6. 真正清理前强制检测 Cursor 是否在跑，**在跑就拒绝执行**；判断不了也按在跑。
7. 清理前做冷备份（优先 APFS clonefile），压缩后校验再原子替换。

已知坑（集成时踩过）
--------------------
- 任何对 `state.vscdb` 的第三方写连接都可能与 Cursor 抢 WAL 锁，导致 Cursor
  报 `storage file corrupted/moved/permission` 而拒绝启动。**退出 Cursor 后清理最稳妥。**
- 参考实现里 `blob_bytes` 恒为 0（删完从不量），本模块改为删前按 key 精确累加。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

#: blob 的 key 前缀（长度 13）。blob 哈希即 key 去掉此前缀后的 64 位 hex。
BLOB_PREFIX = "agentKv:blob:"
BLOB_PREFIX_LEN = len(BLOB_PREFIX)

#: blob 哈希格式：64 位小写 hex。
BLOB_HASH_RE = re.compile(r"[0-9a-f]{64}")

#: 工具结果气泡的 key 形态：`bubbleId:<cid>:<uuid>/t:call_<toolCallId>`。
#: toolCallId 只出现在 key 里、不出现在任何 value 里（2026-10-22 事故取证），
#: 所以「扫 value 找引用」对这类 blob 结构性失明——见模块头「事故」一节。
#: 形态刻意放宽（`.+/t:`）：只要气泡 key 带 `/t:call_` 后缀就算，宁可多认。
TOOL_RESULT_KEY_RE = re.compile(r"^bubbleId:.+/t:(call_[0-9a-z]+)$")

#: `--blob-confirm` 要求用户逐字输入的确认串。选它是因为它**无法被误敲**，
#: 且语义直白：用户必须承认「旧会话可能打不开」这一具体后果。
BLOB_CONFIRM_PHRASE = "我接受旧会话可能打不开"

#: 引用来源**不能**扫 blob 自身——那是 75 GB，读它只为找引用没有意义。
BLOB_SELF_PREFIX = BLOB_PREFIX

#: 会话正文类前缀：既是清理目标（删过期会话），也是 blob 的主要引用来源。
SESSION_PREFIXES = ("bubbleId:%", "checkpointId:%", "composerData:%")

#: key 前缀长度（含冒号）。
_PREFIX_BUBBLE = len("bubbleId:")
_PREFIX_CHECKPOINT = len("checkpointId:")
_PREFIX_COMPOSER_DATA = len("composerData:")

#: 删 blob 时每批的行数。批越小，锁窗口越短。
BLOB_BATCH = 500

#: 锁退避重试的等待序列（秒）。
_LOCK_BACKOFF = (1.0, 2.0, 4.0, 8.0, 16.0, 32.0)


class CursorCleanupError(RuntimeError):
    """清理过程中的可操作错误。CLI 捕获后转成退出码 1 + 人话提示。"""


def human_bytes(n: int) -> str:
    """按量级选单位。参考实现的 `human()` 只有 GB 一档，几十 MB 会显示成 `0.00 GB`。"""
    n = int(n)
    if n < 1024:
        return f"{n} B"
    if n < 1024**2:
        return f"{n / 1024:.1f} KB"
    if n < 1024**3:
        return f"{n / 1024**2:.1f} MB"
    return f"{n / 1024**3:.2f} GB"


def default_backup_dir(db: Path | None = None) -> Path:
    """冷备份目录：**与被清理的数据库同级**的 `CursorStateBackup-<时间戳>`。

    关键在「跟着 db 走」而不是写死 `~/.dirtidy/`：`clean()` 也会被测试和小工具
    用**临时库**调用，写死 HOME 就是往真实应用目录里塞东西（此前 13 个用例每个
    都在真实 `~/Library/Application Support/Cursor/User/globalStorage/` 下留了一个
    备份目录，共 17 个）。跟着 db 走，临时库的备份就落在临时目录里，自然隔离。

    同理必须同卷同目录：APFS `clonefile`（`cp -c`）只在同一卷内生效，跨卷就退化
    成真拷贝，75 GB 的库要写满另一块盘才动得了。

    不指定 `db` 时回落到 macOS 默认位置，仅为向后兼容（CLI 一定会传 db）。
    """
    base = db.parent if db is not None else DEFAULT_DB.parent
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return base / f"CursorStateBackup-{stamp}"


def current_platform() -> str:
    """当前平台标识：`darwin` / `linux` / `nt`。

    单独抽成函数是**为了可测**：用例要在本机验证另外两个平台的分支，
    最直接的做法是 `monkeypatch.setattr(cc.os, "name", "nt")`——但 `os` 是全局
    单例模块，改它会影响整个进程。

    实测代价：在 Windows 上跑这类用例，pathlib 会以为自己是 POSIX 而去构造
    `PosixPath`；一旦该用例失败，pytest 格式化失败信息时调 `Path(os.getcwd())`
    就抛 `NotImplementedError: cannot instantiate 'PosixPath' on your system`，
    整个 session 变成 `INTERNALERROR`——**真正的失败被自己的诊断代码吃掉了**。
    有个函数可以替换，就不必再去动全局状态。
    """
    if os.name == "nt":
        return "nt"
    return sys.platform if sys.platform in {"darwin", "linux"} else "linux"


def default_db_path() -> Path:
    """Cursor 全局状态库在各平台的标准位置。

    写死 macOS 路径会让 Linux/Windows 上 `dirtidy cursor-cleanup` 一上手就报
    「找不到数据库」——功能在这两个平台等于不存在。
    """
    if current_platform() == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / "Cursor" / "User" / "globalStorage" / "state.vscdb"
    if current_platform() == "darwin":
        return Path.home() / "Library/Application Support/Cursor/User/globalStorage/state.vscdb"
    # Linux：官方包是 ~/.config/Cursor
    return Path.home() / ".config" / "Cursor" / "User" / "globalStorage" / "state.vscdb"


#: 向后兼容：模块级常量按当前平台算。
DEFAULT_DB = default_db_path()

#: 各平台「Cursor 正在运行」的进程特征。
_PROCESS_MARKERS: dict[str, tuple[str, ...]] = {
    "darwin": (
        "/Applications/Cursor.app/Contents/MacOS/Cursor",
        "/Applications/Cursor.app/Contents/Frameworks/Cursor Helper",
    ),
    "linux": ("/usr/share/cursor/cursor", "/opt/cursor/", "cursor --no-sandbox"),
    "nt": ("Cursor.exe",),
}


def _process_args() -> list[str] | None:
    """列出所有进程的命令行；这个平台取不到时返回 None（**不要**当成「没在跑」）。"""
    try:
        if current_platform() == "nt":
            # Windows 没有 `ps`；`tasklist` 是系统自带的。
            out = subprocess.check_output(
                ["tasklist", "/FO", "CSV", "/NH"], text=True, errors="replace"
            )
            return [line.split('","')[0].strip('"') for line in out.splitlines() if line.strip()]
        flag = ["-Ax", "-o", "args="] if current_platform() == "darwin" else ["-eo", "args="]
        out = subprocess.check_output(["ps", *flag], text=True, errors="replace")
        return out.splitlines()
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None


def cursor_running() -> bool:
    """检测 Cursor 是否在跑。**无法判断时返回 True（fail-closed）**。

    这里的方向不能反。返回 False 的含义是「可以放心改库」，一旦判断不了却给出
    False，就等于在 Cursor 正在写库时拆它的库——轻则 Cursor 报
    `storage file corrupted` 起不来，重则丢会话。所以取不到进程列表时宁可误报。

    回归背景：原实现在 `ps` 不存在时直接 `return False`（典型是 Windows），
    让 `--apply` 在最需要保护的那个平台上畅通无阻。

    macOS 的 `pgrep -f` 按正则匹配，路径里的 `.` / `()` 会误伤，故用子串判断。
    """
    lines = _process_args()
    if lines is None:
        return True
    markers = _PROCESS_MARKERS.get(current_platform(), ())
    if not markers:
        # 没适配过的平台：同样按「在跑」处理，让人自己确认。
        return True
    for line in lines:
        if "cursor_cleanup" in line or "state.vscdb" in line:
            # 排除本清理器自身（命令行里会带这些字样）
            continue
        if any(m in line for m in markers):
            return True
    return False


# ---------------------------------------------------------------------------
# 引用收集
# ---------------------------------------------------------------------------


def _decode(val: object) -> str:
    if isinstance(val, bytes):
        return val.decode("utf-8", "ignore")
    return val if isinstance(val, str) else str(val or "")


def list_all_key_prefixes(cur: sqlite3.Cursor) -> list[str]:
    """库中除 blob 自身外的**全部**键前缀。

    只扫三个会话前缀会漏掉 `ofsContent:` 这类同样引用 blob 的键——漏扫即误删。
    所以这里从库里把前缀实际枚举出来，而不是硬编码一份可能过时的清单。
    """
    rows = cur.execute(
        """
        SELECT DISTINCT substr(key, 1, instr(key, ':')) AS p
        FROM cursorDiskKV
        WHERE instr(key, ':') > 0 AND key NOT LIKE ?
        """,
        (BLOB_SELF_PREFIX + "%",),
    ).fetchall()
    # `substr(key, 1, instr(key, ':'))` 已经含结尾冒号（"bubbleId:"），
    # 这里直接当 LIKE 模式用——再补一个冒号就成 "bubbleId::%"，一条都匹配不到。
    return [r[0] + "%" for r in rows if r[0]]


def collect_referenced_hashes(
    cur: sqlite3.Cursor,
    prefixes: Sequence[str] | None = None,
    *,
    progress_every: int = 500_000,
) -> set[str]:
    """顺序扫描 value，提取被引用的 64 位 hex 哈希（blob 哈希格式）。

    **必须扫全部非 blob 前缀**——漏一个前缀就可能误删它引用的 blob。
    速度约 5000 行/秒、40 MB/s；767 万行约 25 分钟，可接受。
    """
    referenced: set[str] = set()
    pats = list(prefixes) if prefixes is not None else list_all_key_prefixes(cur)
    scanned = 0
    for prefix in pats:
        for (val,) in cur.execute("SELECT value FROM cursorDiskKV WHERE key LIKE ?", (prefix,)):
            referenced.update(BLOB_HASH_RE.findall(_decode(val)))
            scanned += 1
            if progress_every and scanned % progress_every == 0:
                print(f"  [引用扫描] 已读 {scanned} 行，找到 {len(referenced)} 个引用哈希", flush=True)
    return referenced


def collect_blob_hashes(cur: sqlite3.Cursor) -> list[str]:
    """所有 blob 的 64 位 hex 哈希（只读 key 列，不加载 value）。"""
    rows = cur.execute(
        "SELECT key FROM cursorDiskKV WHERE key LIKE ?",
        (BLOB_SELF_PREFIX + "%",),
    ).fetchall()
    return [r[0][BLOB_PREFIX_LEN:] for r in rows]


# ---------------------------------------------------------------------------
# 孤儿判定
# ---------------------------------------------------------------------------


@dataclass
class BlobPlan:
    """blob 清理计划。判定只此一处——预览与实跑共用同一份结论。"""

    total: int = 0
    referenced: int = 0
    orphan: list[str] = field(default_factory=list)
    orphan_bytes: int = 0
    #: 因 toolCallId 守卫被额外保留下来的 blob 数（2026-10-22 事故回归）。
    #: 这些 blob 在 value 扫描里是「孤儿」，但它们是 tool-result，会话还原
    #: 可能仍需要——「无 64 位引用」证明不了「还原不需要」。
    guarded: int = 0

    @property
    def referenced_count(self) -> int:
        return self.referenced

    def summary(self) -> dict[str, int]:
        return {
            "blob_total": self.total,
            "blob_referenced": self.referenced,
            "blob_orphan": len(self.orphan),
            "blob_orphan_bytes": self.orphan_bytes,
            "blob_guarded": self.guarded,
        }


def collect_tool_result_call_ids(cur: sqlite3.Cursor) -> set[str]:
    """全库收集工具结果气泡 key 里的 toolCallId（只读 key 列，不读 value）。

    2026-10-22 事故取证：会话通过 `bubbleId:<cid>:<uuid>/t:call_<toolCallId>`
    引用工具结果 blob；toolCallId 只在 key 里、不在任何 value 里。所以
    「扫 value 找 64 位哈希」对这类 blob 一个引用都找不到——它们会被判成
    孤儿删掉，随后会话 UI 报 missing blobs、整条判死。

    这层守卫的语义是**保守保留**：凡 key 能对上 toolCallId 的 blob 都不删，
    哪怕 value 扫描说它是孤儿。它不是「引用判定」的替代，而是承认
    「我们证明不了它不被需要」时的 fail-safe。
    """
    call_ids: set[str] = set()
    for (key,) in cur.execute(
        "SELECT key FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'"
    ):
        m = TOOL_RESULT_KEY_RE.match(key)
        if m:
            call_ids.add(m.group(1))
    return call_ids


def _blob_is_tool_result(value: bytes | str) -> bool:
    """blob value 是否是工具调用结果（含 `toolCallId` 的 tool-result JSON）。

    只看头部一小段就够：tool-result 的 JSON 结构里 `toolCallId` 出现在
    `"content":[{"type":"tool-result",…}]` 附近，不必读完全部 75 GB。
    """
    head = value[:2048] if isinstance(value, (bytes, bytearray)) else str(value)[:2048]
    if isinstance(head, (bytes, bytearray)):
        head = head.decode("utf-8", "ignore")
    return '"tool-result"' in head and "toolCallId" in head


def plan_blob_cleanup(
    cur: sqlite3.Cursor,
    referenced: set[str],
    *,
    measure_bytes: bool = True,
) -> BlobPlan:
    """算出哪些 blob 是孤儿。

    `measure_bytes` 会额外扫一遍 blob 的 value 算体积——这是 75 GB 的活，
    所以只在用户明确要数字时才做。

    ⚠️ 这里的「孤儿」**只代表「value 扫描找不到 64 位引用」**。对 tool-result
    blob（引用在 key 协议里、不在 value 里），这个判定是盲的——所以本函数
    内置 toolCallId 守卫：**只要库里存在任何 `/t:` 工具结果气泡，所有
    tool-result 形态的孤儿 blob 一律保留**（计入 `guarded`）。blob key 是
    sha256(value)，与 toolCallId 无哈希关系，无法从 key 反推归属，只能按
    value 形态归类后整类保留。**调用方不得绕过守卫直接拿 orphan 名单删**。
    """
    all_hashes = collect_blob_hashes(cur)
    guarded_call_ids = collect_tool_result_call_ids(cur)
    orphans: list[str] = []
    guarded = 0
    for h in all_hashes:
        if h in referenced:
            continue
        # toolCallId 守卫：库里存在工具结果气泡 + 该孤儿 blob 本身是
        # tool-result 形态 → 保留。它可能正是某个会话还原要取的 blob
        # （2026-10-22 事故：3 个这样的 blob 被判孤儿删除，整条会话判死）。
        if guarded_call_ids and _blob_is_tool_result(_peek_blob_value(cur, h)):
            guarded += 1
            continue
        orphans.append(h)
    plan = BlobPlan(
        total=len(all_hashes),
        referenced=len(all_hashes) - len(orphans) - guarded,
        orphan=orphans,
        guarded=guarded,
    )
    if measure_bytes and orphans:
        plan.orphan_bytes = _sum_blob_bytes(cur, orphans)
    return plan


def _peek_blob_value(cur: sqlite3.Cursor, h: str) -> bytes | str:
    """读单个 blob 的 value 头部（守卫归类用；一次点查，成本可控）。"""
    row = cur.execute(
        "SELECT substr(value, 1, 2048) FROM cursorDiskKV WHERE key = ?",
        (BLOB_PREFIX + h,),
    ).fetchone()
    return row[0] if row else b""


def _sum_blob_bytes(cur: sqlite3.Cursor, hashes: Sequence[str]) -> int:
    """按哈希批量累加 blob 体积（分批，避免临时表过大）。"""
    total = 0
    batch = 5000
    for i in range(0, len(hashes), batch):
        chunk = hashes[i : i + batch]
        cur.execute("DROP TABLE IF EXISTS _cc_bytes")
        cur.execute("CREATE TEMP TABLE _cc_bytes (h TEXT PRIMARY KEY)")
        cur.executemany("INSERT OR IGNORE INTO _cc_bytes(h) VALUES (?)", ((h,) for h in chunk))
        n = cur.execute(
            """
            SELECT COALESCE(SUM(LENGTH(key) + LENGTH(value)), 0)
            FROM cursorDiskKV
            WHERE key LIKE ? AND substr(key, ?) IN (SELECT h FROM _cc_bytes)
            """,
            (BLOB_SELF_PREFIX + "%", BLOB_PREFIX_LEN + 1),
        ).fetchone()[0]
        total += int(n or 0)
    cur.execute("DROP TABLE IF EXISTS _cc_bytes")
    return total


# ---------------------------------------------------------------------------
# 会话（bubble / checkpoint / composerData / headers）
# ---------------------------------------------------------------------------


def _sql_extract_cid_exprs() -> tuple[str, str, str]:
    """从 key 抽出 composerId 的 SQL 表达式。"""
    bubble = (
        f"substr(key, {_PREFIX_BUBBLE + 1}, "
        f"instr(substr(key, {_PREFIX_BUBBLE + 1}), ':') - 1)"
    )
    checkpoint = (
        f"substr(key, {_PREFIX_CHECKPOINT + 1}, "
        f"instr(substr(key, {_PREFIX_CHECKPOINT + 1}), ':') - 1)"
    )
    composer_data = f"substr(key, {_PREFIX_COMPOSER_DATA + 1})"
    return bubble, checkpoint, composer_data


def list_old_composer_ids(cur: sqlite3.Cursor, cutoff_ms: int) -> list[str]:
    """`composerHeaders` 里最后活跃早于 cutoff 的会话 id。"""
    candidates = cur.execute(
        "SELECT composerId, recency FROM composerHeaders"
    ).fetchall()
    old: list[str] = []
    for cid, recency in candidates:
        ms = _parse_time_to_ms(recency)
        if ms is None or ms <= 0 or ms >= cutoff_ms:
            continue
        # 索引可能滞后：正文中存在近期活动即保留，不能只相信旧 header。
        prefix = f"bubbleId:{cid}:"
        recent = False
        for (value,) in cur.execute(
            "SELECT value FROM cursorDiskKV WHERE key = ? OR (key >= ? AND key < ?)",
            (f"composerData:{cid}", prefix, prefix + "\uffff"),
        ):
            activity = _latest_from_json_blob(value)
            if activity is not None and activity >= cutoff_ms:
                recent = True
                break
        if not recent:
            old.append(cid)
    return old


def list_orphan_composer_ids(cur: sqlite3.Cursor, keep: set[str]) -> list[str]:
    """一次 SQL 扫出孤儿 composerId（会话正文还在，但会话索引已没了）。"""
    bubble, checkpoint, composer_data = _sql_extract_cid_exprs()
    cur.execute("DROP TABLE IF EXISTS _cc_keep")
    cur.execute("CREATE TEMP TABLE _cc_keep (cid TEXT PRIMARY KEY)")
    cur.executemany("INSERT OR IGNORE INTO _cc_keep(cid) VALUES (?)", ((c,) for c in keep))
    rows = cur.execute(
        f"""
        SELECT DISTINCT cid FROM (
          SELECT {bubble} AS cid FROM cursorDiskKV
          WHERE key LIKE 'bubbleId:%' AND instr(substr(key, {_PREFIX_BUBBLE + 1}), ':') > 0
          UNION ALL
          SELECT {checkpoint} AS cid FROM cursorDiskKV
          WHERE key LIKE 'checkpointId:%' AND instr(substr(key, {_PREFIX_CHECKPOINT + 1}), ':') > 0
          UNION ALL
          SELECT {composer_data} AS cid FROM cursorDiskKV WHERE key LIKE 'composerData:%'
        )
        WHERE cid IS NOT NULL AND cid != ''
          AND cid NOT IN (SELECT cid FROM _cc_keep)
        """
    ).fetchall()
    cur.execute("DROP TABLE IF EXISTS _cc_keep")
    return [r[0] for r in rows]


def _parse_time_to_ms(raw: object) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        v = int(raw)
        return v * 1000 if v < 10_000_000_000 else v
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        if s.isdigit():
            return _parse_time_to_ms(int(s))
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return int(datetime.fromisoformat(s).timestamp() * 1000)
        except ValueError:
            return None
    return None


def _latest_from_json_blob(val: object) -> int | None:
    try:
        obj = json.loads(_decode(val))
    except (TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(obj, dict):
        return None
    latest = None
    for field_name in ("createdAt", "timestamp", "lastUpdatedAt", "updatedAt", "recency"):
        ms = _parse_time_to_ms(obj.get(field_name))
        if ms is not None and (latest is None or ms > latest):
            latest = ms
    return latest


def orphan_latest_activity_ms(cur: sqlite3.Cursor, cid: str) -> int | None:
    """检查该会话全部正文时间；未知时间不能作为过期证据。

    用精确 key 或前缀范围匹配，cid 内的 SQL 通配符不能扩大作用域。
    任一正文时间未知则保守保留，避免用一条旧气泡代表整个会话。
    """
    latest: int | None = None
    bubble_prefix = f"bubbleId:{cid}:"
    rows = cur.execute(
        "SELECT value FROM cursorDiskKV WHERE key = ? OR (key >= ? AND key < ?)",
        (f"composerData:{cid}", bubble_prefix, bubble_prefix + "\uffff"),
    )
    for (value,) in rows:
        ms = _latest_from_json_blob(value)
        if ms is None:
            return None
        latest = max(latest or ms, ms)
    return latest


def filter_stale_orphans(
    cur: sqlite3.Cursor, orphan_ids: list[str], cutoff_ms: int
) -> tuple[list[str], int]:
    """把孤儿按最后活动时间过滤：近 N 天的留着（可能刚被创建但索引没写），老的才删。"""
    stale: list[str] = []
    skipped_recent = 0
    total = len(orphan_ids)
    for i, cid in enumerate(orphan_ids, 1):
        latest = orphan_latest_activity_ms(cur, cid)
        if latest is not None and latest < cutoff_ms:
            stale.append(cid)
        else:
            skipped_recent += 1
        if total >= 50 and (i % 100 == 0 or i == total):
            print(f"  [孤儿年龄] {i}/{total}", flush=True)
    return stale, skipped_recent


def delete_by_composer_ids(
    con: sqlite3.Connection,
    cur: sqlite3.Cursor,
    ids: list[str],
    *,
    delete_headers: bool,
) -> dict[str, int]:
    """按会话 id 批量删除正文与元数据。"""
    stats = {"bubble": 0, "checkpoint": 0, "composerData": 0, "headers": 0}
    if not ids:
        return stats

    bubble, checkpoint, composer_data = _sql_extract_cid_exprs()
    cur.execute("DROP TABLE IF EXISTS _cc_targets")
    cur.execute("CREATE TEMP TABLE _cc_targets (cid TEXT PRIMARY KEY)")
    cur.executemany("INSERT OR IGNORE INTO _cc_targets(cid) VALUES (?)", ((c,) for c in ids))

    cur.execute(
        f"""
        DELETE FROM cursorDiskKV
        WHERE key LIKE 'bubbleId:%'
          AND instr(substr(key, {_PREFIX_BUBBLE + 1}), ':') > 0
          AND {bubble} IN (SELECT cid FROM _cc_targets)
        """
    )
    stats["bubble"] = cur.rowcount
    con.commit()

    cur.execute(
        f"""
        DELETE FROM cursorDiskKV
        WHERE key LIKE 'checkpointId:%'
          AND instr(substr(key, {_PREFIX_CHECKPOINT + 1}), ':') > 0
          AND {checkpoint} IN (SELECT cid FROM _cc_targets)
        """
    )
    stats["checkpoint"] = cur.rowcount
    con.commit()

    cur.execute(
        f"""
        DELETE FROM cursorDiskKV
        WHERE key LIKE 'composerData:%'
          AND {composer_data} IN (SELECT cid FROM _cc_targets)
        """
    )
    stats["composerData"] = cur.rowcount
    con.commit()

    if delete_headers:
        cur.execute("DELETE FROM composerHeaders WHERE composerId IN (SELECT cid FROM _cc_targets)")
        stats["headers"] = cur.rowcount
        con.commit()

    cur.execute("DROP TABLE IF EXISTS _cc_targets")
    return stats


# ---------------------------------------------------------------------------
# blob 删除
# ---------------------------------------------------------------------------


def delete_orphan_blobs(
    con: sqlite3.Connection,
    cur: sqlite3.Cursor,
    orphan_hashes: Sequence[str],
    *,
    pause: float = 3.0,
) -> dict[str, int]:
    """删除无引用的孤儿 blob。

    为「不影响 Cursor 使用」：
    - 每批之间主动 sleep `pause` 秒，给 Cursor 留出写窗口；
    - 每批用短事务（DELETE 后立即 commit），不长时间持锁；
    - 撞锁时指数退避重试，绝不抛异常中止。
    """
    total = len(orphan_hashes)
    deleted = 0
    if not total:
        return {"blob": 0}

    for i in range(0, total, BLOB_BATCH):
        chunk = list(orphan_hashes[i : i + BLOB_BATCH])
        cur.execute("DROP TABLE IF EXISTS _cc_blob")
        cur.execute("CREATE TEMP TABLE _cc_blob (h TEXT PRIMARY KEY)")
        cur.executemany("INSERT OR IGNORE INTO _cc_blob(h) VALUES (?)", ((h,) for h in chunk))

        done = False
        wait = 1.0
        for _ in range(len(_LOCK_BACKOFF)):
            try:
                cur.execute(
                    """
                    DELETE FROM cursorDiskKV
                    WHERE key LIKE ?
                      AND substr(key, ?) IN (SELECT h FROM _cc_blob)
                    """,
                    (BLOB_SELF_PREFIX + "%", BLOB_PREFIX_LEN + 1),
                )
                n = cur.rowcount
                con.commit()
                deleted += n
                done = True
                break
            except sqlite3.OperationalError as e:
                if "locked" in str(e).lower() or "busy" in str(e).lower():
                    time.sleep(wait)
                    wait *= 2
                    continue
                raise
        cur.execute("DROP TABLE IF EXISTS _cc_blob")
        if not done:
            print(f"  [blob] 第 {i // BLOB_BATCH + 1} 批持续撞锁，跳过", flush=True)
        if total >= 10_000 and (i + BLOB_BATCH) % 20_000 == 0:
            print(f"  [blob] 已删 {min(i + BLOB_BATCH, total)}/{total}", flush=True)
        if i + BLOB_BATCH < total:
            time.sleep(pause)

    return {"blob": deleted}


# ---------------------------------------------------------------------------
# 备份 / 压缩 / 校验
# ---------------------------------------------------------------------------


def _audit_log_path(db: Path, backup_dir: Path) -> Path:
    """清理过程的**实时**审计日志（JSONL，追加写）。

    为什么不用「跑完写一份报告」：真实库上删 blob 要分批做（每批 500 行、
    批间暂停），全程几十分钟。中途被 Ctrl-C / 被系统杀掉时，内存里的 report
    直接蒸发，用户既不知道删了多少、也无从查证——而这是**不可恢复**的删除。
    每阶段结束就追加一行，切断了也留着已发生的记录。
    """
    return backup_dir / "dirtidy-cursor-cleanup.jsonl"


def _audit_append(path: Path, event: str, **fields: Any) -> None:
    """追加一条审计记录。**写不进去不能中断清理**——审计是旁路，不该成为新的失败点。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        rec = {"at": datetime.now().isoformat(timespec="seconds"), "event": event, **fields}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
    except OSError:
        pass


def fast_copy(src: Path, dst: Path) -> None:
    """优先 APFS clonefile（`cp -c`，秒级且不占额外空间），失败再 shutil.copy2。"""
    try:
        subprocess.run(["cp", "-c", str(src), str(dst)], check=True, capture_output=True)
        return
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        shutil.copy2(src, dst)


def backup_database(db: Path, backup_dir: Path) -> Path:
    """冷备份主库与 WAL/SHM 边车文件。"""
    backup_dir.mkdir(parents=True, exist_ok=True)
    fast_copy(db, backup_dir / db.name)
    for side in (db.name + "-wal", db.name + "-shm"):
        side_path = db.with_name(side)
        if side_path.exists() and side_path.stat().st_size > 0:
            fast_copy(side_path, backup_dir / side)
    return backup_dir


def prune_backups(
    backup_root: Path,
    keep: int = 1,
    *,
    current: Path | None = None,
) -> list[Path]:
    """回收旧备份目录，只留最近 `keep` 份（默认 1）。

    这是「定期清理」闭环里**最容易被忽略却最贵的一环**：每次 `clean()` 都在库旁
    留一份几十 GB 的全量备份，如果只做不收，磁盘占用每轮翻倍——本机实测曾同时
    躺着 pre-cleanup（75 GB）+ backup（2.9 GB）+ 冷备份目录三份。回收发生在
    **本次备份已生成之后**，所以最坏情况也始终保有一份可回滚的完整备份。

    `current` 是本次刚建好的备份目录，永远跳过（绝不能被自己回收自己）。

    判新旧只看目录名的时间戳（`CursorStateBackup-YYYYmmdd-HHMMSS`），不查 mtime：
    clonefile 会保留源文件的时间戳，mtime 不可靠。名字不合规范的目录**不动**——
    那可能是用户手工放的东西，清理器没有权限替人做决定。
    """
    removed: list[Path] = []
    if not backup_root.is_dir():
        return removed
    dirs: list[tuple[str, Path]] = []
    for child in backup_root.iterdir():
        if not child.is_dir():
            continue
        m = re.fullmatch(r"CursorStateBackup-(\d{8}-\d{6})", child.name)
        if m:
            dirs.append((m.group(1), child))
    dirs.sort(key=lambda t: t[0], reverse=True)  # 名字时间戳降序：最新在前

    current_name = current.name if current is not None else None
    survivors: list[str] = []
    if current_name is not None:
        survivors.append(current_name)
    for _stamp, path in dirs:
        if len(survivors) >= max(keep, 0) + (1 if current_name else 0):
            break
        survivors.append(path.name)

    for _stamp, path in dirs:
        if path.name in survivors:
            continue
        try:
            shutil.rmtree(path)
            removed.append(path)
        except OSError:
            pass  # 回收失败不阻断主流程：多留一份备份不是灾难，删错才是。
    return removed


def verify_database(path: Path) -> None:
    """校验压缩后的库：表齐全、`ItemTable` 非空。任一不满足就中止替换。"""
    if not path.is_file() or path.stat().st_size < 1024:
        raise CursorCleanupError(f"压缩库异常（过小或不存在）：{path}")
    con = sqlite3.connect(str(path))
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = {"ItemTable", "cursorDiskKV", "composerHeaders"} - tables
        if missing:
            raise CursorCleanupError(f"压缩库缺表 {sorted(missing)}：{path}")
        n = con.execute("SELECT COUNT(*) FROM ItemTable").fetchone()[0]
        if n < 1:
            raise CursorCleanupError("压缩库 ItemTable 为空，疑似损坏，中止替换")
    finally:
        con.close()


def checkpoint_wal(db: Path) -> None:
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA busy_timeout=60000")
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 顶层动作
# ---------------------------------------------------------------------------


def open_readonly(db: Path) -> sqlite3.Connection:
    """只读连接。`mode=ro` 保证不改主库；TEMP 表仍可写（勿开 query_only）。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    con.execute("PRAGMA busy_timeout=60000")
    return con


@dataclass
class CleanupReport:
    """一次清理的完整结论。CLI 与界面共用这一份，不各算一遍。"""

    mode: str = "dry-run"
    days: int = 90
    db: str = ""
    db_bytes: int = 0
    old_sessions: int = 0
    orphan_sessions: int = 0
    orphan_skipped: int = 0
    old_stats: dict[str, int] = field(default_factory=dict)
    orphan_stats: dict[str, int] = field(default_factory=dict)
    blob: dict[str, int] = field(default_factory=dict)
    backup_dir: str = ""
    vacuumed: bool = False
    elapsed_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "mode": self.mode,
            "days": self.days,
            "db": self.db,
            "db_bytes": self.db_bytes,
            "old_sessions": self.old_sessions,
            "orphan_sessions": self.orphan_sessions,
            "orphan_skipped": self.orphan_skipped,
            "old_stats": self.old_stats,
            "orphan_stats": self.orphan_stats,
            "blob": self.blob,
            "backup_dir": self.backup_dir,
            "vacuumed": self.vacuumed,
            "elapsed_s": round(self.elapsed_s, 1),
            "notes": self.notes,
        }


def analyze(
    db: Path,
    *,
    days: int = 90,
    include_orphan: bool = True,
    include_blob: bool = False,
    measure_blob_bytes: bool = False,
) -> CleanupReport:
    """只统计，不改库。

    `include_blob` 默认 False（2026-10-22 事故后）：干跑也默认不算 blob
    孤儿名单——那要读 75 GB value、几十分钟，而结论又不可信（对 tool-result
    blob 结构性失明）。要预估就显式加 `--delete-blobs`（干跑时它只表示
    「我要看 blob 数字」，不动手）。
    """
    t0 = time.perf_counter()
    if days < 1:
        raise CursorCleanupError("--days 必须 >= 1")
    if not db.is_file():
        raise CursorCleanupError(f"找不到数据库：{db}")

    cutoff_ms = int((time.time() - days * 86400) * 1000)
    con = open_readonly(db)
    cur = con.cursor()
    report = CleanupReport(
        mode="dry-run",
        days=days,
        db=str(db),
        db_bytes=db.stat().st_size,
    )

    try:
        old_ids = list_old_composer_ids(cur, cutoff_ms)
        headers_n = cur.execute("SELECT COUNT(*) FROM composerHeaders").fetchone()[0]
        report.old_sessions = len(old_ids)
        report.notes.append(f"会话索引共 {headers_n} 条，超过 {days} 天未活跃 {len(old_ids)} 条")

        if include_orphan:
            keep = {r[0] for r in cur.execute("SELECT composerId FROM composerHeaders")}
            orphan_ids = list_orphan_composer_ids(cur, keep)
            stale, skipped = filter_stale_orphans(cur, orphan_ids, cutoff_ms)
            report.orphan_sessions = len(stale)
            report.orphan_skipped = skipped

        if include_blob:
            referenced = collect_referenced_hashes(cur)
            plan = plan_blob_cleanup(cur, referenced, measure_bytes=measure_blob_bytes)
            report.blob = plan.summary()
    finally:
        con.close()

    report.elapsed_s = time.perf_counter() - t0
    return report


def clean(
    db: Path,
    *,
    days: int = 90,
    include_orphan: bool = True,
    include_blob: bool = False,
    vacuum: bool = True,
    backup_dir: Path | None = None,
    blob_pause: float = 3.0,
    blob_ack: bool = False,
    keep_backups: int = 1,
) -> CleanupReport:
    """真正清理。**调用方必须已确认 Cursor 已退出**（`cursor_running()` 由 CLI 检查）。

    仅删除明确超过 days 保留窗的会话；未知年龄的孤儿保留。
    共享 blob 无可靠年龄及完整引用协议，include_blob=True 一律写入前拒绝。
    blob_ack/blob_pause 仅为旧调用兼容参数，不再授权共享 blob 删除。
    """
    t0 = time.perf_counter()
    # 共享 blob 没有可靠的年龄与完整归属协议，确认口令不能证明其在 days 范围内。
    # 必须在备份、审计及任何写库之前拒绝，不能先删会话再报告 blob 不支持。
    if include_blob:
        raise CursorCleanupError("拒绝删除共享 blob：无法证明其年龄和完整引用归属；--days 只支持会话级清理")
    if not db.is_file():
        raise CursorCleanupError(f"找不到数据库：{db}")
    if days < 1:
        raise CursorCleanupError("--days 必须 >= 1")

    gs = db.parent
    vacuumed_path = gs / "state.vscdb.vacuumed"
    old_path = gs / "state.vscdb.pre-cleanup"
    # 备份目录默认跟着**被清理的那个库**走，而不是写死用户 HOME 下的 Cursor 路径：
    # 一来 `clean()` 会被测试和小工具用临时库调用，写死 HOME 就是往真实应用目录里塞东西；
    # 二来同卷才能走 clonefile。
    backup = backup_dir or default_backup_dir(db)

    report = CleanupReport(
        mode="clean",
        days=days,
        db=str(db),
        db_bytes=db.stat().st_size,
        backup_dir=str(backup),
    )

    audit = _audit_log_path(db, backup)
    _audit_append(
        audit,
        "start",
        db=str(db),
        db_bytes=report.db_bytes,
        days=days,
        include_orphan=include_orphan,
        include_blob=include_blob,
        blob_ack=blob_ack,
        vacuum=vacuum,
        backup=str(backup),
    )

    checkpoint_wal(db)
    backup_database(db, backup)
    report.notes.append(f"冷备份完成：{backup}")
    _audit_append(audit, "backup", backup=str(backup))
    # 备份只做不收，磁盘每轮翻倍（本机曾三份 75 GB 并存）。冷备份落定后立刻
    # 回收超出配额的旧备份目录；`keep_backups` 计入本次刚建的一份。
    pruned = prune_backups(backup.parent, keep=keep_backups, current=backup)
    if pruned:
        report.notes.append(f"已回收 {len(pruned)} 个旧备份目录（保留最近 {keep_backups} 份）")
        _audit_append(
            audit, "backups_pruned", kept=keep_backups, removed=[str(p) for p in pruned]
        )

    cutoff_ms = int((time.time() - days * 86400) * 1000)
    con = sqlite3.connect(str(db))
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=60000")
    con.execute("PRAGMA temp_store=MEMORY")
    con.execute("PRAGMA cache_size=-200000")
    con.execute("PRAGMA synchronous=NORMAL")
    cur = con.cursor()

    try:
        old_ids = list_old_composer_ids(cur, cutoff_ms)
        report.old_sessions = len(old_ids)
        report.old_stats = delete_by_composer_ids(con, cur, old_ids, delete_headers=True)
        # 每阶段落一条：中途被杀时，这里就是「已经删了什么」的唯一凭据。
        _audit_append(
            audit, "old_sessions", count=len(old_ids), stats=report.old_stats
        )

        if include_orphan:
            keep = {r[0] for r in cur.execute("SELECT composerId FROM composerHeaders")}
            orphan_ids = list_orphan_composer_ids(cur, keep)
            stale, skipped = filter_stale_orphans(cur, orphan_ids, cutoff_ms)
            report.orphan_sessions = len(stale)
            report.orphan_skipped = skipped
            report.orphan_stats = delete_by_composer_ids(con, cur, stale, delete_headers=False)
            _audit_append(
                audit,
                "orphan_sessions",
                count=len(stale),
                skipped=skipped,
                stats=report.orphan_stats,
            )

        # 共享 blob 全部保留；include_blob 在任何写入前已被拒绝。
    except BaseException as exc:
        # 出错也要留一条：用户需要知道「在删到第几阶段时断的」。
        _audit_append(audit, "aborted", stage="clean", error=repr(exc))
        raise
    finally:
        cur.close()
        con.commit()
        con.close()

    checkpoint_wal(db)

    if vacuum:
        if vacuumed_path.exists():
            vacuumed_path.unlink()
        try:
            con2 = sqlite3.connect(str(db))
            try:
                con2.execute("PRAGMA temp_store=MEMORY")
                con2.execute("VACUUM INTO ?", (str(vacuumed_path),))
            finally:
                con2.close()
            _audit_append(audit, "vacuum_done", vacuumed=str(vacuumed_path))
            verify_database(vacuumed_path)
            if old_path.exists():
                old_path.unlink()
            # 这两行之间有窗口：db 已改名走、新库还没就位，`state.vscdb` 此刻不存在。
            # 所以先记一条 `replacing`，万一在窗口里被打断，日志能说明库去哪了。
            _audit_append(audit, "replacing", old=str(old_path), new=str(db))
            os.replace(db, old_path)
            os.replace(vacuumed_path, db)
        except BaseException as exc:
            # 校验不过 / 替换失败也要留痕：否则「库还在不在、在哪个文件」无从判断。
            _audit_append(audit, "aborted", stage="vacuum", error=repr(exc))
            raise
        for side in ("state.vscdb-wal", "state.vscdb-shm"):
            p = gs / side
            if p.exists():
                p.unlink()
        report.vacuumed = True
        # `old_path` 只是两步原子替换之间的中转安全垫。新库已校验通过并成功就位，
        # 完整原库就在冷备份 `CursorStateBackup-*/` 里——再留着它就是无意义的
        # 双份 75 GB（本机事故后同时躺着 pre-cleanup 75 GB + 冷备份 75 GB 的由来）。
        # 此时点删除：万一用户想回滚，从冷备份恢复即可，字节级等价。
        if old_path.exists():
            old_path.unlink()
            _audit_append(audit, "old_path_reclaimed", old=str(old_path))
            report.notes.append("替换成功，中转旧库已回收（完整原库在冷备份里）")
        else:
            report.notes.append(f"压缩后原库另存：{old_path}")
        _audit_append(audit, "replaced", old=str(old_path), new=str(db))
    else:
        report.notes.append("已跳过 VACUUM（文件体积暂不缩小）")

    report.elapsed_s = time.perf_counter() - t0
    report.notes.append(f"审计日志：{audit}")
    _audit_append(audit, "done", elapsed_s=round(report.elapsed_s, 1))
    return report


def iter_table_names(db: Path) -> Iterator[str]:
    """列出库里的表名（只读）。测试与诊断用。"""
    con = open_readonly(db)
    try:
        for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table'"):
            yield name
    finally:
        con.close()


def run_cursor_cleanup_schedule(
    db: Path,
    *,
    interval_hours: float = 24.0,
    days: int = 90,
    include_orphan: bool = True,
    vacuum: bool = True,
    keep_backups: int = 1,
    max_rounds: int | None = None,
    notify_enabled: bool = True,
    stop_event: Any | None = None,
    _sleep: Any = time.sleep,
) -> int:
    """无人值守的定期清理循环——「根治」的落点。

    定期治理的核心难题是：清理必须**离线**做（Cursor 在跑就拒绝），但没人愿意
    每月记得手动退出 Cursor 再敲命令。本循环把这件事自动化：

    - 每 `interval_hours` 小时醒一次；
    - Cursor 在跑 → **静默等待下一轮**（绝不催用户退出，更绝不在线写库）；
    - Cursor 恰好退出 → 执行 `clean()`（默认不删 blob，`include_blob` 在本函数里
      硬编码为 False，**不接受参数传入**——无人值守场景下高危动作必须不可达，
      这不是靠约定，是靠签名里没有这个口子）；
    - 清理完成后发一条系统通知报成果。

    返回执行的轮数（0 表示一次都没轮到 Cursor 退出）。
    """
    from dirtidy.watch import notify  # 局部导入避免模块级环依赖

    rounds = 0
    executed = 0
    print(
        f"开始定期清理 {db}：每 {interval_hours:g} 小时检查一次，"
        f"保留近 {days} 天会话，保留最近 {keep_backups} 份冷备份。"
    )
    print("仅当 Cursor 完全退出时才执行；blob 永不自动删除。按 Ctrl-C 停止。\n")

    def _wait(seconds: float) -> None:
        if stop_event is not None:
            stop_event.wait(seconds)
        else:
            _sleep(seconds)

    try:
        while max_rounds is None or rounds < max_rounds:
            if stop_event is not None and stop_event.is_set():
                break
            rounds += 1
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            if cursor_running():
                print(f"[{stamp}] 第 {rounds} 轮：Cursor 正在运行，本轮跳过（继续等）。", flush=True)
                _wait(interval_hours * 3600)
                continue
            print(f"[{stamp}] 第 {rounds} 轮：Cursor 已退出，开始清理……", flush=True)
            try:
                report = clean(
                    db,
                    days=days,
                    include_orphan=include_orphan,
                    include_blob=False,  # 硬编码：无人值守永不删 blob
                    vacuum=vacuum,
                    keep_backups=keep_backups,
                )
            except CursorCleanupError as e:
                print(f"[{stamp}] 清理失败：{e}", flush=True)
                if notify_enabled:
                    notify("dirtidy 定期清理失败", str(e))
                _wait(interval_hours * 3600)
                continue
            executed += 1
            after_bytes = db.stat().st_size
            freed = max(report.db_bytes - after_bytes, 0)
            summary = (
                f"清理过期会话 {report.old_sessions} 条"
                f"（孤儿 {report.orphan_sessions} 条），"
                + (f"释放约 {human_bytes(freed)}" if freed > 0 else "体积无明显变化")
            )
            print(f"[{stamp}] {summary}", flush=True)
            if notify_enabled:
                notify("dirtidy 定期清理完成", summary)
            if max_rounds is not None and rounds >= max_rounds:
                break
            _wait(interval_hours * 3600)
    except KeyboardInterrupt:
        print("\n已停止定期清理。")
    return executed
