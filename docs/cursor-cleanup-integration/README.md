# Cursor 状态库清理 —— 历史交接资料

> **仅供追溯，勿按下文操作。** 这是集成前的方案与样本测量，不是当前使用指南。旧文中的「在线删 blob」「无引用即可删」「blob 占 99%」不能作为当前安全或容量结论。当前执行路径不删共享 blob；操作见 [使用手册](../09-功能与使用汇总.md)，安全要求见 [需求 FR-12](../02-需求规格.md#fr-12-cursor-状态库清理独立支线)，事故取证见 [事故记录](事故-20261022-会话无法还原.md)。

> **状态：已落地（2026-10-21）。** 能力已实现为 `dirtidy cursor-cleanup`
> （`src/dirtidy/cursor_cleanup.py`；用例数为当时记录），本文转为**历史交接材料**留档，
> 正文保留当时方案。当前行为的权威描述见上方使用手册与需求链接。
> 与下方建议不一致的地方以代码与那两份文档为准——集成时改过三处关键判定：
> ① 引用来源扫**全部**非 blob 前缀（只扫会话那三个会误删活数据）；
> ② `cursor_running()` fail-closed（判断不了也按「在跑」处理）；
> ③ 备份目录跟着被清理的库走，不写死用户 HOME。

> 本目录是「把 Cursor `state.vscdb` 清理能力集成进 dirtidy」的**交接材料**。
> 由 dirtidy 的开发会话接手，把参考脚本重构为 dirtidy 的一个子命令/模块。
> 参考脚本全文见同目录 `cursor_state_vscdb_cleanup_reference.py`。

---

## 一、要做什么

把「清理本机 Cursor `state.vscdb`」的能力，做成 dirtidy 的一个子命令（如 `dirtidy cursor-cleanup`），
复用 dirtidy 现有的 CLI 框架（`src/dirtidy/cli.py` 的 subparsers 结构）。

**目标形态**（用户已确认）：
- 完整功能：清理 90 天前会话 + 孤儿 + 无引用 blob
- 做成 dirtidy 包内的模块（不是独立包）
- 去掉对原办公仓 `_bootstrap` 的依赖，改用标准库

---

## 二、背景：state.vscdb 是什么

Cursor 的全局状态库，路径：
`~/Library/Application Support/Cursor/User/globalStorage/state.vscdb`

是一张巨大的键值表（`cursorDiskKV`，约 838 万行）。主要键类型：

| 键前缀 | 含义 | 行数（实测） | 体积占比 |
|---|---|---|---|
| `agentKv:blob:<hash>` | agent 运行时大对象（图片/附件/上下文快照，按 64 位 hex 哈希去重） | ~67.8 万 | **~75GB，占 99%+** |
| `bubbleId:<cid>:<bid>` | 会话气泡（对话正文） | ~767 万 | 很小 |
| `checkpointId:<cid>:<id>` | 检查点快照 | ~5 千 | 很小 |
| `composerData:<cid>` | 会话元数据 | ~770 | 很小 |
| `composerHeaders`（另一张表） | 会话索引（标题/时间/活跃度） | ~760 | 极小 |
| `ItemTable`（另一张表） | 用户设置（插件/主题/快捷键） | ~1080 | 极小，**永久保留** |

**关键结论**：体积几乎全在 `agentKv:blob`。只删会话正文（bubble/checkpoint/composerData）几乎不缩库。

---

## 三、踩过的坑（务必避开）

1. **在线删 blob 会抢 WAL 锁，导致 Cursor 报错**
   - 现象：清理脚本用写连接 DELETE 时，Cursor 正持有 WAL 写锁，触发 Cursor 保护性报错
     `storage file corrupted/moved/permission`，Cursor 拒绝启动。
   - 结论：**任何对 state.vscdb 的第三方写连接，都可能和 Cursor 抢锁**。
   - 缓解：小批次短事务 + 批间 sleep 让路 + 锁退避重试（参考脚本已实现），但**无法 100% 避免**。
   - 最稳妥仍是「退出 Cursor 后清理」。

2. **全量扫 value 找引用太慢**
   - 判断 blob 是否孤儿，需要知道它是否被会话 value 引用。
   - 引用藏在 bubble/checkpoint/composerData 的 JSON value 里（64 位 hex 字符串）。
   - 用 SQL `INSTR` 逐个哈希查 = 全表扫，单个哈希 7 分钟无结果，不可行。
   - 正确做法：**顺序读一遍 value，Python 正则 `[0-9a-f]{64}` 一次性提取所有被引用哈希**。
     实测 5250 行/秒、39MB/s，767 万行约 25 分钟（可接受）。

3. **清理量太小**
   - 早期只删会话正文，75GB 只释放 0.18GB。
   - 根因：没动占 99% 体积的 blob。

---

## 四、参考脚本的现状（已实现的能力）

`cursor_state_vscdb_cleanup_reference.py` 已包含：

1. **默认清理**（`--days 90`）：删 90 天前会话（bubble/checkpoint/composerData/headers）+ 过期孤儿，然后 VACUUM。要求 Cursor 完全退出。
2. **`--blob-only`**：在线只清理无引用 blob，Cursor 可继续工作，不 VACUUM、不替换文件。
   - 扫描引用：只读连接 `mode=ro` 顺序扫 value + 正则提取哈希（约 25 分钟）
   - 删除：写连接分批 DELETE（500/批），批间 sleep（`--blob-pause` 默认 3s），锁退避重试
3. **`--dry-run`**：只统计不改库
4. **`--no-vacuum` / `--no-orphan` / `--no-blob`**：各阶段开关
5. **备份**：APFS `cp -c` 冷备份 + `VACUUM INTO` 后原子替换 + integrity 校验

**实测数据**（2026-10-21）：
- blob 总数 67.9 万，被引用哈希仅 18836 个，**孤儿 blob 66.5 万个（占 98%）**
- 说明绝大多数 blob 是历史垃圾，清理价值极高

---

## 五、集成建议（给 dirtidy 会话）

1. **模块化**：把参考脚本重构为 `src/dirtidy/cursor_cleanup.py`，去掉 `_bootstrap` 依赖：
   - `wpath("_整理归档", ...)` → 改为 dirtidy 自己的状态目录或用户主目录下的备份路径
   - `ROOT`（仅 dry-run 打印用）→ 去掉或改用 `Path.cwd()`
2. **CLI 接入**：在 `src/dirtidy/cli.py` 的 `build_parser()` 里加 `cursor-cleanup` 子命令，
   复用参考脚本的参数（`--days`/`--dry-run`/`--blob-only`/`--blob-pause`/`--no-*`）。
3. **保留安全设计**：
   - 绝不删 `ItemTable`（用户设置）
   - blob 清理只删「无引用」的孤儿，被引用的保留
   - 在线模式绝不 VACUUM、绝不替换文件（会锁死 Cursor）
4. **测试**：参考脚本已有迷你库单元测试思路（构造 3 个 blob，1 个被引用 2 个孤儿，
   验证只删孤儿、保留引用和 ItemTable）。可移植成 dirtidy 的 pytest 用例。

---

## 六、参考脚本依赖

纯 Python 标准库（`argparse/json/re/sqlite3/subprocess/pathlib` 等），
唯一外部依赖是原办公仓的 `_bootstrap`（集成时需去掉）。
