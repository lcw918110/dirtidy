# Cursor 本地存储为什么会涨到 160GB？从全景拆解、删库踩坑到可定期运行的治理方案

重度使用 Cursor 的开发者，迟早会遇到同一个问题：磁盘空间莫名其妙见底，一查发现 `Application Support/Cursor` 吞掉了几十甚至上百 GB。网上常见的建议要么是“删掉 `Cache` 和 `logs`”（实测只能腾出几百 MB，完全隔靴搔痒），要么是“删掉 `state.vscdb`”（连历史对话、工作区状态、插件登录态一锅端）。

如果我们的目标是**既不丢失工作上下文，又能通过定期清理阻止它无限膨胀**，就必须先搞清楚两个问题：Cursor 到底把数据写在了哪里？哪些数据在失控增长，它们的引用关系是什么？

这篇文章基于我本机 167 GB Cursor 目录的实测拆解、一次因误判引用协议导致长会话永久损坏的真实取证，最终给出了一套可定期执行的分层清理方案。

---

## 一、全景拆解：Cursor 的本地数据都装在哪？

在动手写任何清理逻辑之前，我先对本机 Cursor 的两个根目录做了全量体积测算。把所有子目录摊开后，一个反直觉的事实立刻浮现出来：**98% 的体积集中在一个目录里，而网上教程让你删的那些缓存与日志，加起来不到 2%。**

```text
~/Library/Application Support/Cursor/          合计约 167 GB
├── User/globalStorage/
│   ├── state.vscdb                            76.5 GB  ← 正在使用的全局状态库（核心）
│   ├── state.vscdb.pre-cleanup                75.4 GB  ← 上一次清理留下的原库备份
│   └── state.vscdb.backup                      2.9 GB  ← 更早期的历史备份
├── CachedData/                                 1.1 GB  ← V8 字节码编译缓存
├── Partitions/ & WebStorage/                   622 MB  ← 内置浏览器分区存储
├── logs/                                       160 MB  ← 运行时滚动日志
├── User/History/ & workspaceStorage/           236 MB  ← 本地文件修改历史与工作区索引
└── Cache/ & GPUCache/ & Crashpad/              ~65 MB  ← Chromium 渲染与崩溃缓存

~/.cursor/                                     合计约 900 MB
├── projects/*/agent-transcripts/               565 MB  ← Agent 对话纯文本逐字稿（.jsonl）
└── extensions/                                 277 MB  ← 已安装插件本体
```

从这张全景图可以看出，Cursor 的本地文件按性质分为四层，定期治理的策略完全不同：

1. **纯缓存与运行日志层（`CachedData`、`Cache`、`GPUCache`、`Crashpad`、`logs`）**：合计约 1.4 GB。特点是删了会自动重建，零业务风险；但它们自身有滚动或淘汰机制，并不会无限膨胀，只适合顺手清理。
2. **历史备份遗留层（`state.vscdb.pre-cleanup`、`state.vscdb.backup`）**：合计 78.3 GB。很多清理工具在做 `VACUUM` 压缩后，会把旧库改名留在原地充当回滚保险。如果定期清理流程不回收上一次的备份，磁盘占用就会直接翻倍。
3. **兜底档案层（`~/.cursor/projects/*/agent-transcripts/`）**：565 MB。这里存着每个对话的 `.jsonl` 纯文本逐字稿。体积很小，但价值极高——当 UI 状态库损坏时，这是找回历史对话结论的唯一救命稻草，**任何清理程序都绝对不能碰它**。
4. **核心状态库（`state.vscdb`）**：单文件 76.5 GB，这才是真正无限膨胀的根源。

要阻止 Cursor 无限变大，主战场只有一个：搞懂 `state.vscdb` 内部在涨什么，以及怎么安全地给它定期瘦身。

---

## 二、深挖核心：`state.vscdb` 为什么会无限膨胀？

`state.vscdb` 是一个 SQLite 数据库（WAL 模式），里面只有两张表：一张是存用户配置、插件设置、快捷键的 `ItemTable`（体积很小，绝对不能删）；另一张是塞满了 Agent 和 Composer 运行时数据的键值表 `cursorDiskKV`。

在我的机器上，`cursorDiskKV` 有 **845 万行**。注意一个度量陷阱：如果直接用 SQLite 的 `LENGTH(value)` 统计体积，它对 TEXT 类型返回的是“字符数”而不是“字节数”，遇到中文内容会严重低估；必须用 `SUM(LENGTH(CAST(value AS BLOB)))` 才能算出真实字节。按真实字节口径拆解这 72 GB 的键值内容，结构如下：

| 键前缀（Key 形态） | 存储内容 | 真实体积 | 占比 |
|---|---|---|---|
| `bubbleId:<会话ID>:<气泡UUID>` | 会话气泡（对话每一轮的正文与状态） | **48.92 GB** | **67.9%** |
| `agentKv:blob:<64位hex>` | Agent 运行时大对象（工具输出、文件快照、附件） | 20.95 GB | 29.1% |
| `checkpointId:` / `composerData:` 等 | 检查点快照与会话元数据头 | 2.16 GB | 3.0% |

这个拆解打破了两个常见的错误假设。

### 发现一：真正撑爆数据库的头号元凶是“会话气泡”，不是 blob

很多人（包括最初的我）以为 `agentKv:blob` 占了九成以上体积，但字节级统计显示：**对话气泡 `bubbleId:` 占了将近七成（48.92 GB），才是膨胀的第一推手。**

更惊人的是气泡体积的分布极度偏斜：全库近 800 个会话里，只有 2 个超长会话的气泡数超过 10 万条，仅仅这 2 个会话就吃掉了 **44.89 GB**。其中最大的一个会话独占 **42.88 GB（694 万条气泡）**——排查发现，当我们在同一个长对话里反复让 Agent 执行全局搜索（如 ripgrep）、大范围读写文件时，会话状态会被高频重写和重放（同一个 `toolCallId` 在单日内重复出现了 614 次，单日新增 36.3 万条气泡）。

这也直接回答了“为什么状态库越用越快地变大”：**只要你习惯在一个对话窗口里连续工作好几天而不开新窗口，气泡表就会呈指数级膨胀。**

### 发现二：数据库变大后，为什么 Cursor 会感觉变慢？

800 多万行的 SQLite 表，按主键做 B-tree 点查依然是 $O(\log n)$，而 Cursor 启动时只读取 `composerHeaders`（不到 800 行）。所以“变慢”并不是 SQL 查询退化，而是两个物理层面的代价：
一是超长会话在加载和追加时，单次写入的 WAL（预写日志）动辄上百 MB（实测日常挂着 121 MB 的 `-wal` 文件），触发 WAL checkpoint 合并回主库时会产生明显的 I/O 停顿；二是 76 GB 的单文件加上等大的备份，直接把磁盘剩余空间挤干，拖慢了操作系统的交换与缓存。

因此，要想让 `state.vscdb` 不再无限扩大，清理方案的杠杆非常明确：**主力杠杆是按时间窗口（如保留最近 30 天）定期清理过期会话的气泡与检查点，顺带清理无人引用的孤儿数据，并通过 `VACUUM` 把释放的页真正归还给操作系统。**

听起来很清晰。但正是在“顺带清理无人引用的孤儿 blob”这一步上，我踩了最深的一个坑。

---

## 三、前车之鉴：一次“看似严谨”的清理为何搞崩了长会话？

在第一版清理脚本里，我对 `agentKv:blob:<64位hex>` 的清理逻辑设计得非常谨慎：
既然每个 blob 的 key 都是一个 64 位十六进制哈希，那我就顺序扫描库里所有非 blob 行的 `value`，用正则把出现过的全部 64 位哈希提取出来，作为“被引用集合”；任何不在集合里的 blob，自然就是没人要的孤儿，可以安全删除。

为了防止漏扫，我还专门做了一次 34 分钟的全库只读对照实验：早期参考脚本只扫 `bubbleId:`、`checkpointId:`、`composerData:` 三个前缀，扫出 13,866 个被引用 blob；而我改成动态枚举库内**全部**非 blob 前缀（把 `ofsContent:`、`codeBlockPartialInlineDiffFates:` 等冷门前缀也纳入扫描），扫出了 13,867 个——成功抓出了 1 个会被三前缀方案误删的活 blob。

我以为这套“全前缀 value 扫描”已经滴水不漏。然而在清理掉约 5,413 个“孤儿 blob”的第二天，当我打开一条持续使用了一周的核心工作会话并发送消息时，Cursor 直接弹窗判死：

```text
This conversation's data is missing and can't be restored. Start a new chat to continue.
(3 missing blobs: 0203ebc74590, 00a8f0e42850, 0120dc7d37b2)
```

整条会话彻底打不开。看着这行报错，我陷入了困惑：我删的明明是 **64 位** hex 的 `agentKv:blob`，为什么报错说缺的是 3 个 **12 位** hex 的 ID？而且我已经扫遍了全库所有的 `value`，为什么还会误删活会话依赖的数据？

### 五步真机取证：揭开隐式引用协议

靠猜测写不出可靠的清理器。我挂载了清理前留下的 75 GB 原库 `state.vscdb.pre-cleanup`，用只读模式走完了一条完整的证据链：

**第一步：12 位 ID 与 64 位 key 是什么关系？**
最初在 800 多万行上跑 `WHERE key LIKE 'agentKv:blob:0203ebc74590%'`，因为 `LIKE` 不走主键索引触发全表扫描，查询进程直接超时被系统 `SIGTERM` 杀掉，造成“库里查不到”的假象。改用走主键 B-tree 索引的 `GLOB` 点查后，毫秒级返回结果：

```sql
SELECT key FROM cursorDiskKV WHERE key GLOB 'agentKv:blob:0203ebc74590*';
```

```text
0203ebc74590 → agentKv:blob:0203ebc74590b84d436b09c637d104dc26e2d086a358f62e5cd3311889ed5e07
00a8f0e42850 → agentKv:blob:00a8f0e42850ab1952f5325764f9fd276ae17989c7ada925d574eb52517bf954
0120dc7d37b2 → agentKv:blob:0120dc7d37b20f100eca8c627629219fbbd815f445200e0fc7fdc5c91a81d7fd
```

真相大白：**UI 报错里的 12 位 ID，就是 64 位 blob key 的前 12 位前缀。** 这三个 blob 在清理前的原库里完好无损，在当前库里消失了——百分之百是清理脚本删掉的。

**第二步：这 64 位 key 是怎么算出来的？**
把这三个 blob 的 `value` 取出来算 SHA-256：

```python
hashlib.sha256(value).hexdigest() == key_hex  # 三个全部精确相等
```

`agentKv:blob` 采用的是纯粹的**内容寻址（Content-Addressable）**，key 就是 `sha256(value)`。这意味着 key 本身与会话 ID、工具调用 ID 没有任何哈希派生关系，你不可能仅凭 key 反推出它属于哪个会话。

**第三步：被删的 blob 里面装的是什么？**
解码三个 blob 的 `value`（以下源码内容已脱敏）：

```json
{
  "role": "tool",
  "content": [{
    "type": "tool-result",
    "toolCallId": "call_iiqiugnjzpx5oeluicabqrt7",
    "toolName": "Read",
    "result": "package com.example.demo.service;..."
  }]
}
```

它们全部是 Agent 工具调用的返回结果（`tool-result`），每个 JSON 里都带着一个 `toolCallId`，但**没有任何 64 位哈希**。

**第四步：会话到底是怎么引用它们的？（根因水落石出）**
拿着 `call_iiqiugnjzpx5oeluicabqrt7` 去全库搜索，终于在会话气泡表里抓到了引用方——但引用并不在气泡的 `value` 里，而是编码在**气泡的 `key` 名字里**：

```text
Key:   bubbleId:c86e8b80-a181-...:2efba5d4-.../t:call_iiqiugnjzpx5oeluicabqrt7
Value: {"_v":3,"type":2,...}   <-- 标准气泡 JSON，里面完全不包含那个 64 位 blob 哈希！
```

这就是整个事故的根本原因：**Cursor 对工具结果的引用协议写在 `key` 的 `/t:call_<id>` 后缀里，而不在任何行的 `value` 里。**

当我们用“扫描全部 `value` 提取 64 位哈希”的方法去寻找被引用集合时，这套算法对所有的 `tool-result` 类 blob 是**结构性失明**的——哪怕你把全库每一行的 `value` 扫上一百遍，也找不出任何一个指向它们的 64 位哈希，于是它们被悉数当成“孤儿”删掉了。

**第五步：更深一层的边界——子代理气泡甚至不在本地库**
顺着三个 `toolCallId` 往下查，前两个都能在库里找到对应的 `/t:call_...` 气泡 key，但第三个 `call_3jp2ae9z...` 连气泡 key 都找不到。结合 `composerHeaders` 表中的 `isSubagent` 字段分析，这类工具调用来自子代理或云端恢复协议，还原会话时由客户端按需索取，本地气泡表里根本没有显式记录。

这次取证彻底扭转了清理器的设计前提：**“在本地 `value` 里扫不到 64 位哈希引用”，绝不等于“删掉它不会影响会话还原”。**

---

## 四、整体治理方案：如何安全、定期地控制体积增长？

搞清了全景分布、膨胀主因和隐式引用协议后，我们就可以跳出“头痛医头”的零散脚本，设计一套真正能定期运行、不会把库搞崩的治理体系。这套方案已经完整落地在 `dirtidy cursor-cleanup` 模块中（含 8 条事故回归测试守卫）。

### 1. 管住真正的增长源头：按时间窗口清理过期会话，而非盲目删 blob

既然 `bubbleId:` 会话气泡占了 67.9% 的体积，且单条超长会话就能吃掉 42 GB，控制状态库体积的正道就是**会话生命周期管理**：

- **日常使用习惯（预防膨胀）**：一个独立任务结束就新开对话窗口（New Chat），避免在同一个会话里连续几周堆积数百万人次的工具重放气泡。这样旧窗口会自然进入非活跃状态，从而能被定期清理回收。
- **定期清理主干（默认安全模式）**：按 `composerHeaders` 的最后活跃时间（`recency`）划定保留窗口（例如保留近 30 天）。对超过窗口的过期会话，以及没有 header 的陈旧孤儿会话，连同它的 `composerData`、`bubbleId:` 气泡、`checkpointId:` 检查点整套清除；而窗口内的活跃会话，四类数据完整保留，一个字节不动。
- **默认不碰 `agentKv:blob`**：把 `include_blob` 的默认值设为 `False`。日常定期跑会话清理 + `VACUUM`，就能安全回收大部分历史气泡空间，彻底规避误伤活跃会话的风险。

### 2. 如果确实需要回收那 29% 的 blob：双闸确认 + `toolCallId` 形态守卫

对于攒了大半年、确实想额外腾出那十几个 GB 非活跃 blob 的场景，清理器提供了纵深防御：

第一道防线是**“双闸显式确认”**。必须同时传入 `--delete-blobs` 开关与逐字确认参数 `--blob-confirm "我接受旧会话可能打不开"` 才会进入 blob 清理阶段；只传开关不传确认语会立即拒绝执行，并写入 `blob_refused` 审计事件，绝不静默降级。

第二道防线是**内置在唯一判定函数 `plan_blob_cleanup()` 里的 `toolCallId` 守卫**。既然 `blob key = sha256(value)` 无法从 key 反推归属，我们就反过来检查孤儿候选的 `value` 头部：只要库里还存在任何 `/t:call_` 形态的工具气泡，所有 `value` 呈现 `"type":"tool-result"` + `"toolCallId"` 形态的孤儿 blob 一律强制保留（记入 `blob_guarded`）。在 72 GB 真实库上实测：
- `tool-result` 形态的 blob 总共只占全库体积的 **4.3%（3.02 GB）**——全部保留下来代价极小，却能保住会话还原链条；
- 真正占大头的其他孤儿 blob（图片、历史快照等，占 **25.4% / 17.92 GB**）仍然可以在确认后被清理回收。

### 3. 工程底线：为什么绝不能在 Cursor 运行时“在线清理”？

很多人希望“边用 Cursor 边在后台静默清理状态库”，我早期也尝试过在线分批 `DELETE`（每批 500 条、短事务提交、批间 `sleep` 让路）。真机实测证明**这条路完全走不通**：

Cursor 运行时持续持有 `state.vscdb-wal` 的写锁与共享内存索引（`-shm`）。第三方进程一旦介入写库，哪怕事务再短，也会与 Electron 主进程争抢 WAL 锁，轻则抛 `database table is locked`，重则触发 Cursor 自身的一致性保护，直接弹窗 `storage file corrupted/moved/permission` 拒绝工作。更关键的是，如果不做 `VACUUM`，SQLite 删掉的行只会变成文件内部的 free pages，**磁盘上的 `.vscdb` 文件一字节都不会缩小**；而 `VACUUM INTO` + 原子替换原库，必须在没有任何进程打开该库的前提下才能安全完成。

因此，可靠的清理器必须遵守四条工程铁律：

1. **进程检测 Fail-Closed（闭锁失败）**：清理前先查系统进程表，只要发现 Cursor 正在运行就拒绝加 `--apply`；如果连进程表都读不到（例如在缺乏 `ps` 命令的受限环境或 Windows 特殊环境），也一律按“正在运行”拒绝，绝不盲目放行。
2. **同卷冷备份 + 校验后原子替换**：动手删数据前，先在数据库同级目录用 APFS `cp -c`（Copy-on-Write 克隆，75 GB 瞬间完成且不额外占空间）生成冷备份；清理结束后执行 `VACUUM INTO state.vscdb.vacuumed`，对新库跑完整性校验并确认 `ItemTable` 完好，最后用 `os.replace` 原子替换原库。
3. **崩溃留痕的 JSONL 审计流**：几十 GB 的库做扫描和 `VACUUM` 耗时较长，如果中途被系统或用户中断，内存里的报告会瞬间蒸发。尤其在 `os.replace(db, old_path)` 与 `os.replace(vacuumed_path, db)` 之间存在极短的替换窗口。清理器在每个阶段（包括进入替换窗口前）都会向磁盘追加一行 `audit.jsonl`，即便中途断电也能一眼查清当前库停留在哪一步。
4. **严格的作用域红线**：清理器只允许操作 `state.vscdb` 及其伴生文件，源码级测试守卫禁止出现 `agent-transcripts`、`chats`、`blob_storage`、`IndexedDB` 等路径——确保万一 UI 会话出问题，用户的 `.jsonl` 纯文本逐字稿永远在原地兜底。

---

## 五、实操指南：一套可定期执行的维护闭环

把上面的分析落到实处，如果你现在正面对一个几十甚至上百 GB 的 Cursor 目录，按以下三步建立定期维护习惯，就能彻底告别磁盘告警：

### 第一步：清理陈旧备份与外围缓存（零风险，立竿见影）

检查 `globalStorage` 下是否有历史清理留下的 `state.vscdb.pre-cleanup` 或 `state.vscdb.backup`，以及同级的 `CursorStateBackup-*` 目录。只要当前的 Cursor 已经正常使用了几天，这些动辄几十 GB 的历史快照就可以安全移除；顺手清空 `CachedData`（V8 编译缓存）和过期 `logs`：

```bash
# 1. 查看 globalStorage 下所有状态库与备份体积
ls -lh ~/Library/Application\ Support/Cursor/User/globalStorage/state.vscdb*

# 2. 确认当前 Cursor 正常使用几天后，删除上一次遗留的旧库快照（本机直接释放 75 GB）
rm -f ~/Library/Application\ Support/Cursor/User/globalStorage/state.vscdb.pre-cleanup*
```

### 第二步：每月一次会话级定期瘦身（主力手段，默认安全）

每月找一个休息间隙，**完全退出 Cursor（`Cmd + Q`）**，用默认安全模式清理超过 30 天的非活跃会话并压缩数据库。这一步只清过期会话正文与检查点，不碰 `ItemTable` 设置，也不碰 `agentKv:blob`：

```bash
# 1. 先干跑预览（默认只读，不改任何数据，加上 --measure 估算可释放空间）
dirtidy cursor-cleanup --days 30 --measure

# 2. 确认无误后，完全退出 Cursor，执行清理与 VACUUM 压缩（默认不删 blob）
dirtidy cursor-cleanup --days 30 --apply

# 3. 重新打开 Cursor 正常使用一两天后，删掉本次生成的冷备份（CursorStateBackup-* 与 .pre-cleanup）完成空间回收
```

### 第三步：低频深度回收孤儿 blob（按需可选）

如果清理完过期会话后体积仍然很大，且你明确接受“极少数依赖特殊子代理协议的旧会话可能打不开 UI（但 `.jsonl` 逐字稿仍在）”，再启用双闸开关清理非 `tool-result` 的孤儿 blob：

```bash
# 完全退出 Cursor 后执行；内置守卫会自动保留占 4.3% 的 tool-result 形态 blob，只清其余 25.4% 孤儿
dirtidy cursor-cleanup --days 30 --apply \
  --delete-blobs \
  --blob-confirm "我接受旧会话可能打不开"
```

---

## 小结

回看从 167 GB 磁盘告警到删库事故、再到系统性治理的全过程，核心脉络其实非常清楚：

1. **看清全貌再动手**：Cursor 98% 的膨胀集中在 `state.vscdb`（及其历史备份），而库内 67.9% 的体积是对话气泡（尤以不关窗口的超长会话为甚），外围日志与缓存只占不到 2%。
2. **管住源头而非盲目全删**：阻止无限膨胀的正道，是“日常及时新开对话窗口 + 定期按 `--days` 清理过期会话并 `VACUUM` + 验证后回收旧备份”，而不是在线硬删正在使用的库。
3. **对未知的内部协议保持敬畏**：清理工具本质上是在替用户做不可逆的删除决策。像 `/t:call_` 这样藏在 `key` 里的隐式引用协议证明了，凡是没用真机数据证明过“100% 等价安全”的清理动作，都必须默认关闭，并用显式确认与形态守卫兜底。
