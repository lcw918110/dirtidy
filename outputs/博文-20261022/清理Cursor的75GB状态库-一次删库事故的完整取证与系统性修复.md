# 清理 Cursor 的 75GB 状态库：一次删库事故的完整取证与系统性修复

> 一次「看起来完全正确」的清理，为什么会让一条长会话永远打不开？
> 这篇文章记录我从现象到根因的完整取证过程、踩过的坑，以及最终的系统性修复方案。
> 所有结论都来自真机只读取证，不猜。

## 一、事故现场

2026-10-22 下午，在一条从 10/16 持续使用的长会话里提交消息，Cursor 弹出：

```text
This conversation's data is missing and can't be restored. Start a new chat to continue.
(3 missing blobs: 0203ebc74590, 00a8f0e42850, 0120dc7d37b2)
```

整条会话 UI 判死。而前一天（10/21），我跑过一个清理脚本，删了约 5413 个
「无引用」的 `agentKv:blob`。时间线对得上，但**证据链对不上**：

- 报错里的 3 个 ID 是 **12 位 hex**；
- 被删的 5413 个 blob 的 key 是 **64 位 hex**（`agentKv:blob:000033ae5049a833…`）。

两套标识，长度都不一样，怎么对上号？这是整场取证的起点。

## 二、背景：state.vscdb 里到底装了什么

Cursor 把 Agent / Composer 的全部运行时数据塞进一张巨大的键值表
`cursorDiskKV`（本机实测 845 万行 / 72 GB）：

| 键形态 | 内容 | 体积占比（实测） |
|---|---|---|
| `bubbleId:<cid>:<uuid>` | 会话气泡（对话正文） | **67.9%** ← 真正的大头 |
| `agentKv:blob:<64位hex>` | agent 运行时大对象（工具结果/附件/快照） | 29.1% |
| `checkpointId:` / `composerData:` | 检查点与会话元数据 | 极小 |
| `ItemTable`（独立表） | 用户设置（插件/主题/快捷键） | 永不删 |

清理逻辑看起来无懈可击：**顺序扫一遍所有非 blob 行的 value，用正则提取
全部 64 位哈希，得到「被引用集合」；不在集合里的 blob 就是孤儿，可删**。

我甚至修过参考实现的一个坑：它只扫 `bubbleId` / `checkpointId` /
`composerData` 三个前缀，而真机库里 `ofsContent:`、
`codeBlockPartialInlineDiffFates:` 等前缀同样引用 blob——漏扫即误删活数据。
我改成扫**全部**非 blob 前缀，真机对照实验证明少扫会误删 1 个活 blob。

然后 10/22，事故还是来了。**扫得再全，也没救。**

## 三、取证：五步锁定根因

### 第一步：12 位 ID 是不是 64 位 key 的前缀？

用 10/21 清理前的冷备份 `state.vscdb.pre-cleanup`（75 GB）做只读查询。
坑：对 845 万行的表跑 `LIKE 'agentKv:blob:0203%'` 会全表扫描，直接超时被杀。
改用 `GLOB` 点查（key 是主键，走索引）：

```sql
SELECT key FROM cursorDiskKV WHERE key GLOB 'agentKv:blob:0203ebc74590*'
```

三个 12 位 ID 全部精确命中：

```text
0203ebc74590 → agentKv:blob:0203ebc74590b84d436b09c637d104dc26e2d086a358f62e5cd3311889ed5e07
00a8f0e42850 → agentKv:blob:00a8f0e42850ab1952f5325764f9fd276ae17989c7ada925d574eb52517bf954
0120dc7d37b2 → agentKv:blob:0120dc7d37b20f100eca8c627629219fbbd815f445200e0fc7fdc5c91a81d7fd
```

**结论 1：12 位 ID 就是被删 64 位 blob key 的前缀。**「两套无关标识」的
初步判断被推翻。三个 blob 在 pre-cleanup 库存在、在当前库不存在——
确系清理删除。

### 第二步：blob key 是怎么生成的？

对三个 blob 的 value 算哈希：

```python
hashlib.sha256(value).hexdigest() == key   # 三个全部命中
```

**结论 2：blob key = sha256(value)，内容寻址。** 这意味着 key 与
toolCallId、会话 ID 都没有哈希关系——**你无法从 key 反推它属于谁**。

### 第三步：blob value 里是什么？

```json
{"role":"tool","content":[{"type":"tool-result",
  "toolCallId":"call_iiqiugnjzpx5oeluicabqrt7","toolName":"Read",
  "result":"package com.ada.jykjcloudx.admin.bus.service;..."}]}
```

是 agent 工具调用的结果。三个 blob 的 value 里各有一个 `toolCallId`。

### 第四步：会话怎么引用这些 blob？（关键一步）

拿着 toolCallId 去会话气泡里找，发现引用藏在**气泡的 key** 里：

```text
bubbleId:c86e8b80-…:2efba5d4-…/t:call_iiqiugnjzpx5oeluicabqrt7
```

而气泡的 value 是标准气泡 JSON（`_v:3, type:2`），**不含任何 64 位哈希**；
blob 的 value 里也没有 64 位哈希。

**结论 3（根因定论）：引用协议在 key 里，不在 value 里。**
「扫全部 value 找 64 位哈希」对这类 tool-result blob **一个引用都找不到**——
它们全部被判成孤儿。这不是「扫得不够全」的问题，是**结构性失明**：
任何 value 扫描的改进都覆盖不了这套协议。

### 第五步：为什么有一个 blob 的气泡也找不到？

三个 toolCallId 里，两个的 `/t:` 气泡还在库里，第三个
（`call_3jp2ae9z…`）的气泡**全库都找不到**。结合 Cursor 的子代理机制
（`composerHeaders` 有 `isSubagent` 字段），最合理的解释：该工具调用发生在
子代理/云端侧，气泡不在本地库里——**本地无法枚举它的引用**。

## 四、踩过的坑（按血泪程度排序）

1. **在线写库**：Cursor 运行时第三方连接写 `state.vscdb` 会抢 WAL 锁，
   Cursor 报 `storage file corrupted/moved/permission` 拒绝启动。
   批间 sleep / 锁退避**不能**当成「可以在线清」的承诺。
2. **引用扫描范围过窄**：只扫三个会话前缀会漏 `ofsContent:` 等前缀，
   误删活 blob（真机对照实验证实）。
3. **把「value 里扫不到引用」当成「删了不影响」**：本次事故根因。
   引用在 key 协议里，value 扫描结构性失明。
4. **`LIKE` 前缀查询在 845 万行上全表扫**：取证脚本被 SIGTERM 杀掉，
   换 `GLOB` 点查（主键索引）秒出。
5. **SQLite `LENGTH()` 对 TEXT 返回字符数不是字节数**：中文内容体积
   被严重低估，量化分析必须 `CAST(value AS BLOB)`。
6. **测试断言恒真**：`assert x == x or True` 这种笔误会让守卫静默失效，
   写完要重读一遍。
7. **备份目录写死用户 HOME**：测试用临时库调用 `clean()`，结果往真实
   Cursor 应用目录塞了 17 个备份目录。备份必须跟着被清理的库走。
8. **`ps` 取不到进程列表时当「没在跑」**：fail-open 等于在 Cursor 正写库时
   拆它的库。必须 fail-closed（判断不了就按「在跑」拒绝）。

## 五、系统性修复

修复原则：**判定只做一处，默认值向安全倾斜，破坏性动作要逐字确认。**

1. **默认不删 blob**。`clean()` / `analyze()` 的 `include_blob` 默认 `False`。
   「无 64 位引用」证明不了「会话还原不需要它」——在 Cursor 公开还原协议
   之前，唯一安全的默认就是不删。
2. **删 blob 需要两道闸**：`--delete-blobs` 显式开关 +
   `--blob-confirm "我接受旧会话可能打不开"` 逐字确认。只给开关不给确认 →
   拒绝执行并记 `blob_refused` 审计，**绝不静默降级**（用户以为删了实际
   没删，比报错更糟）。
3. **toolCallId 守卫**：即使全确认删 blob，判定（`plan_blob_cleanup`）也内置
   守卫——只要库里存在 `/t:` 工具结果气泡，tool-result 形态的孤儿 blob
   一律保留（计入 `blob_guarded`）。blob key 是 sha256(value)，无法从 key
   反推归属，只能按 value 形态归类后整类保留。
4. **近 N 天会话完整保留**：header、composerData、气泡、checkpoint 一个不删。
5. **范围硬边界**：只碰 `state.vscdb` 及其伴生文件；`agent-transcripts/`、
   `chats/`、Chromium `blob_storage` / IndexedDB 一律不碰（源码层守卫测试）。
6. **真红验证**：9 条事故回归用例，三组注入回退（默认改回删 blob、守卫置空、
   CLI 确认检查短路）分别精确报红 → 字节级还原 → 全绿。**没证明会红的
   守卫等于没写。**

## 六、「不删 blob，库会不会越来越大越来越慢？」

实测回答（72 GB 库，字节口径）：

- **库增长的大头是会话气泡（67.9% / 48.9 GB），不是 blob（29.1%）**；
- 气泡高度集中：全库仅 2 个会话气泡数 >10 万，合计 44.89 GB；一个会话
  独占 42.88 GB / 694 万条气泡（工具调用反复重放所致）；
- 按 `--days` 清过期会话（安全、无需确认）才是主要回收杠杆；
- 性能上 SQLite 按 key 点查对 845 万行仍是 O(log n)，「慢」主要来自磁盘
  压力与 WAL checkpoint 停顿，不是查询退化。

**回收路径按收益排序**：删历史备份（如 `state.vscdb.pre-cleanup`，75 GB，
零风险）> `--days` 会话清理 > 确认后删 blob（约 17.9 GB，tool-result 已被
守卫整类保护）。

## 七、给用户的建议

- 真要清理：**完全退出 Cursor** 再 `--apply`；
- 只想保会话、接受库大：干跑即可，不要删 blob；
- 某条对话已报 missing blobs：**不要再往那条里发消息**，到原工作区新开
  对话；需要旧结论读 `agent-transcripts/*.jsonl`（逐字稿一直在，清理程序
  没碰它）。

---

*取证方法、量化数据与修复代码均来自真实事故处理过程（dirtidy 项目
`cursor-cleanup` 模块，9 条事故回归用例已入库）。本文只写查证过的事实。*
