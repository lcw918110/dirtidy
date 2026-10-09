# dirtidy 项目审核报告

- 审核对象：`/Users/Licw/Desktop/git/dirtidy`（跨平台本地目录整理工具）
- 审核时间：2026-10-09
- 审核范围：过程资料（`docs/` 6 份 + `README.md` + 修订记录）与结果资料（`src/` 21 个模块、`tests/` 4 个文件 20 用例、`build/`、`configs/`）
- 审核方式：全量代码通读 + 实际运行（pytest 20 用例全绿）+ 针对可疑点编写复现脚本实证
- 结论一句话：**架构分层清晰、安全默认（默认隔离不抹盘）方向正确、文档体系完整；但存在 1 个高危数据安全缺陷（路径穿越）与 3 个高优先缺陷（凭据外发泄漏、候选为空时全量外发、只读根扫描崩溃），且"第一期缺口已补完"的结论过于乐观。**
- 处理情况：P0/P1 缺陷与绝大部分 P2 项**已修复并补回归用例**；最终 `ruff` 零告警、`mypy` 21 文件零问题、`pytest -W error` **33 用例全绿**。仅剩「提交 git」与「选定 LICENSE」两项需项目所有者决定。详见第五节与 `修复说明-20261009.md`。

---

## 一、项目概览

| 维度 | 情况 |
|------|------|
| 定位 | 规则隔离 → 模型分析 → 人工终审的本地目录整理引擎；CLI + 本地 Web 双界面，共用同一套 run 产物 |
| 技术栈 | Python ≥3.11；pyyaml / httpx / fastapi / uvicorn；pytest |
| 代码规模 | `src/dirtidy/` 21 个 `.py`；`tests/` 4 文件 20 用例；规则包 2 个 YAML |
| 模块划分 | `scan` / `rules` / `hashing` / `quarantine` / `audit` / `ai` / `decision` / `web` |
| 运行环境实测 | `.venv` = Python 3.12.13；`pytest -q` → **20 passed in 4.36s** |
| Git 状态 | 仅 2 次提交；**本轮改动全部未提交**（`configs/rules/*.yaml` 显示为已删除、多个源码与文档 M 状态） |

**整体评价**：这是一个完成度较高的个人工具项目。模块边界干净、审计产物齐全（`inventory/candidates/quarantine_manifest/ai_suggestions/decisions/audit.md`）、有真红文档一致性测试，工程质量明显高于一般个人脚本。问题集中在**边界校验缺失**与**文档承诺与实现不符**两类。

---

## 二、过程资料审核（docs / README）

### 2.1 做得好的部分

1. **文档体系分层合理**：愿景→需求→架构→规则→测试→缺口，六份递进，`docs/01` 明确"做什么/不做什么"，非目标（桌面壳、定时监视、多租户）写死，避免了范围蔓延。
2. **修订记录带"是否已并入正文"列**——这是很专业的做法，能看出每轮改动是否真正落到正文，而不是只记流水账。
3. **`tests/test_docs_consistency.py` 是真红测试**：它解析 `docs/04` 的规则 ID 表格，与内置 YAML 双向比对（文档有的 YAML 必须有、YAML 有的文档必须列全），文档漂移会真的失败。这一点值得肯定——大多数项目的"文档测试"是恒真的。
4. **FR-4 的算法取舍写得很清楚**：明确"禁止仅凭压缩包文件名或同名目录判定"，只认成员内容 hash 重合比例，并在 `docs/04 §4` 给出阈值（0.8、命中≥1）与加密归档的降级路径。

### 2.2 过程资料的问题

| 编号 | 位置 | 问题 | 影响 |
|------|------|------|------|
| D-1 | `docs/03-架构设计.md` §5 CLI 契约表 | 5 条命令漏写 `root` 位置参数：写成 `dirtidy apply-rules <run_id>`、`analyze <run_id>`、`apply-decisions <run_id>`、`quarantine-restore <run_id>`、`purge-quarantine <run_id>`。实际 CLI 全部要求 `<root> <run_id>`（`dirtidy apply-rules --help` → `usage: ... root run_id`） | 文档不可直接照抄执行；且与 `README.md` 快速开始（写的是 `apply-rules /path/to/root <run_id>`，**正确**）自相矛盾。同一项目两处口径不一 |
| D-2 | `docs/05-测试与验收.md` §1 | 平台矩阵称 Windows"实机 GUI 未在本环境重复"，Linux 为"或 CI"——但仓库**没有任何 CI 配置**（无 `.github/workflows`） | NFR-1 的三平台承诺实际只验证了 macOS + posix 路径断言，缺少自动化兜底 |
| D-3 | `docs/06-已知缺口与后续.md` | 开篇断言"第一期缺口已补完"，"仍开放/待办"仅列 Starlette 弃用警告一条 | 与本次审核发现（路径穿越、凭据泄漏、全量外发、只读根崩溃）不符，会给后续维护者错误的"已收口"信号 |
| D-4 | 全项目 | 无 `LICENSE` 文件，`pyproject.toml` 无 `license` 字段 | 若计划分发/开源，许可状态不明 |
| D-5 | `docs/05` §3 用例表 | 列了 20 条用例，但**未覆盖**安全边界（路径穿越、凭据脱敏）与成本边界（外发目标选择） | 测试清单给人一种"覆盖充分"的印象，实际安全维度是空白 |

---

## 三、结果资料审核（源码 / 测试 / 构建产物）

### 3.1 架构与实现亮点

- **安全默认正确**：`quarantine` 只 `shutil.move` 不删除；`purge-quarantine` 必须 `--yes`，无 `--yes` 返回码 2 且不动文件（有测试 `test_purge_requires_yes_then_deletes` 守护）。
- **跨平台路径统一**：`scanner` 一律用 `Path.relative_to(root).as_posix()`，产物不含反斜杠，`inventory` 可跨平台比对。
- **大目录友好**：内容 hash 走 `ThreadPoolExecutor`（`--workers`）；`hash_cache.json` 按 `path+size+mtime` 复用，且会清理已消失的旧键防止无限膨胀；单文件超 `max_hash_bytes`（64MB）跳过。
- **解压残留判定克制**：`hashing/content.py` 只认成员内容 sha256 与磁盘 hash 的重合比例，且默认仅候选不自动隔离——与文档承诺一致。
- **`quarantine_hits` 按路径深度倒序处理**（`key=lambda h: h.path.count("/"), reverse=True`），避免父目录先被移走后子路径找不到，细节考虑到位。
- **`sweep_empty_dirs` 多轮清理**（上限 8 轮）且有 `after == before` 的收敛判断，不会死循环。

### 3.2 问题清单

严重度定义：**P0** = 数据安全/可造成根外破坏；**P1** = 功能正确性或成本失控；**P2** = 健壮性/可维护性/文档。

---

#### P0-1 路径穿越：决策可把 root 之外的文件搬进/搬出（已实证）

**现象**：`decision/store.py` 与 `quarantine/store.py` 用裸拼接构造路径，未校验是否仍在目标根内：

- `decision/store.py:35` `src = root / d.path`
- `decision/store.py:56` `dest = root / target`（`target` 来自 `target_relpath`）
- `quarantine/store.py:121` `dest = root / item["original_path"]`

而 `decision` 的来源包括 Web 接口 `PUT /api/runs/{run_id}/decisions`，其 body 是 `list[dict]`，**无任何路径校验**。

**实证**（脚本复现）：

```
决策 path = "../outside/victim.txt"，action = quarantine
→ apply 结果 {'quarantined': 1}
→ root 外的 /tmp/dirtidy_audit/outside/victim.txt 被移入 root/.dirtidy/quarantine/outside/victim.txt

决策 path = "a.txt"，action = archive，target_relpath = "../outside/escaped.txt"
→ apply 结果 {'archived': 1}
→ 文件被移出 root，落到 /tmp/dirtidy_audit/outside/escaped.txt
```

**影响**：这是本项目最严重的问题。工具的核心承诺是"根内、可回滚、安全"，而一个 `..` 就能让隔离/归档操作越出目标根，造成**根外文件被静默移动**。虽然默认只监听 127.0.0.1，但：(1) 决策文件也可能由 `save-decisions` 从外部 JSON 导入，同样无校验；(2) 用户手写 `decisions.json` 写错一个路径就会误伤根外数据；(3) 任何未来把 Web 暴露到局域网/加多用户的改动都会把它变成真实攻击面。

**修复建议**：新增统一的 `resolve_under(root, rel)` 工具函数——拒绝绝对路径，用 `os.path.normpath` 做**词法**归一化（不 follow symlink，避免误伤符号链接文件），断言结果仍在 `root` 前缀内，越界抛 `ValueError`；在 `decision/store.py`（2 处）、`quarantine/store.py`（`quarantine_hits` 源路径、`_safe_dest` 目标、`restore_entries` 还原目标）全部改用它，并在 `apply_decisions` 中把被拒绝的条目记入 `skipped` 明细而非静默崩溃。

---

#### P1-1 模型外发摘要泄漏凭据（已实证）

**现象**：`build_summary` 默认对文本后缀（含 `.json`/`.yaml`/`.yml`/`.ini`/`.cfg`/`.toml`）抽样最多 800 字符，脱敏只靠一条正则：

```python
if re.search(r"(?i)(api[_-]?key|secret|password)\s*=", text):
    text = text[:200] + "\n[redacted possible secrets]"
```

该正则只认 `key=` 形式，**不认** JSON 的 `"key": "..."`、YAML 的 `key: value`、以及 `aws_secret_access_key` / `access_key` / `token` / `credential` 等命名。

**实证**：

| 文件名 | 默认是否抽样 | 实际外发内容 |
|--------|--------------|--------------|
| `credentials.json` | **是** | `{"aws_access_key_id":"AKIAXXXX","aws_secret_access_key":"supersecretvalue"}` |
| `config.yaml` | **是** | `db:\n  password: hunter2\n` |
| `.npmrc` / `id_rsa` / `.env` | 否（无后缀，不在 TEXT_SUFFIXES） | — |

**影响**：文档 `docs/02 FR-7`、`docs/03 §7`、`README` 安全约定均宣称"密钥类/二进制不抽样""默认只传安全摘要"，但实现只靠后缀黑名单（`.pem/.key/.p12/.pfx/.jks`），**凭据类文本文件（credentials/secrets/config/.env 变体）会被原样外发到外部模型端点**。在 `provider=openai_compatible` 时这是真实的数据外泄路径，与文档承诺直接冲突。

**修复建议**：三重加固——(1) 增加**文件名**敏感黑名单（`credential|secret|passwd|password|token|\.env$|id_rsa|id_ed25519|\.npmrc$|\.netrc$|\.pgpass$|\.aws|\.ssh|keystore`），命中即完全不抽样；(2) 把脱敏正则扩展为同时覆盖 `:`/`=`/JSON 引号形式，并纳入 `token|credential|access[_-]?key|private[_-]?key|auth`；(3) 长远建议把"文本抽样"从默认开启改为**显式 opt-in**（`summary.sample_text: true`），默认只发路径/大小/时间/hash 前缀。

---

#### P1-2 候选为空时 `analyze` 会把全部文件发给模型（已实证）

**现象**：`ai/providers.py` 的目标筛选逻辑：

```python
if candidates_paths and path not in candidates_paths:
    if path.count("/") > 3:
        continue
```

当 `candidates.json` 存在但**为空**（`candidates_paths` 为空集）时，`candidates_paths and ...` 短路为假，整个深度过滤被跳过。

**实证**：构造 7 个内容唯一、无重复、无归档、无空文件、无 log 的目录（`candidates` 数量 = 0）→ `analyze_run` 实际发送 **7/7 个文件**，其中包含深度 5 的 `a/b/c/d/e/f*.txt`。

**影响**：与代码注释"非候选仅保留浅层路径，控制 token 成本"的意图完全相反。真实场景中"规则没命中任何候选"很常见（例如一个干净的数据目录），此时会把**全量文件**送进模型：token 成本随目录规模线性爆炸，且配合 P1-1 会成倍放大隐私外发面。

**修复建议**：去掉短路条件，改为 `if path not in candidates_paths and path.count("/") > 3: continue`；同时建议把深度阈值从硬编码 `3` 提为 `summary.max_depth` 可配项。

---

#### P1-3 只读根目录：扫描成果在最后一步全部丢失（已实证）

**现象**：`scan_tree` 末尾无条件写缓存：

```python
if use_hash_cache and not quick:
    ...
    write_json(cache_path, cache)   # 无异常保护
```

`hash_cache.json` 落在 `<root>/.dirtidy/`，即**被扫描的根目录内部**。

**实证**：`chmod 555` 的只读根 → `create_run` 抛 `PermissionError: EACCES ... mkdir '/private/tmp/dirtidy_audit/ro/.dirtidy'`。

**影响**：扫描一个只读挂载点/受限目录时，整个 run 在**做完全部枚举与 hash 之后**才失败，前面所有工作白做，且报错点在用户意料之外。`--quick` 可绕过，但用户不会预知。同时"往被扫描目录里写文件"这一设计对只读介质天然不友好。

**修复建议**：(1) 把缓存写入包 `try/except OSError` 降级为 stderr 警告，不阻断主流程；(2) 增加 `--no-cache` / `DIRTIDY_CACHE_DIR` 环境变量，允许把缓存放到用户目录（`~/.cache/dirtidy/`），彻底避免污染与权限问题。

---

#### P2-1 前端 XSS 向量 ✅ 已修

`web/static/app.js` 全程用 `innerHTML` 拼接**文件名/路径**（如 `tr.innerHTML = \`<td><code>${path}</code></td>\`、`value="${d.target_relpath...}"`、radio `value="${p}"`），无转义；`index.html` 未设 CSP。文件名是攻击者可控的（例如从压缩包解出的文件、下载目录），可构造 `<img src=x onerror=...>` 型注入。本地单用户场景降低了严重度，但既是零成本修复，建议一并处理：改用 `textContent` / `createElement` 赋值，并加 `Content-Security-Policy` 响应头。

#### P2-2 `package_root()` 依赖 editable 安装 ✅ 已修

`models.py` 的 `package_root()` 用 `Path(__file__).resolve().parents[2]` 定位仓库根，`load_model_config` 据此找 `configs/models.yaml`。这在 `pip install -e .` 下成立，但**非 editable 安装（wheel 到 site-packages）时 `parents[2]` 指向 site-packages**，且 `configs/` 并未进 `package-data`（只有 `web/static/*` 与 `rules/packs/*.yaml`）。结果是正常安装后 `dirtidy analyze` 找不到配置。建议配置查找改为 `cwd` → `$DIRTIDY_CONFIG` → `~/.config/dirtidy/models.yaml`，不依赖源码树相对路径。

#### P2-3 构建产物与空骨架未清理 ✅ 已修

- `build/`（140K，内含 `build/lib/dirtidy/` 的**完整源码副本**，与 `src/` 当前一致）既未被 `.gitignore` 忽略，也未被提交，长期会与 `src/` 漂移造成"改了不生效"的排查陷阱。`.gitignore` 里有 `dist/` 但漏了 `build/`。
- `apps/web/static/` 是**空目录骨架**，与 `src/dirtidy/web/static/` 功能重复且无任何文件，属历史残留。

#### P2-4 `apply_decisions` 静默丢弃 ✅ 已修

`archive` 目标已存在、或 `quarantine` 源文件不存在时，代码 `continue` 静默跳过，返回值只统计成功数（`kept/archived/quarantined`）。用户无法知道哪条决策没被执行、为什么。建议返回 `skipped: [{path, action, reason}]` 明细。

#### P2-5 缺工程配套 ⚠️ 部分已修（CI/ruff/mypy 已加；LICENSE 待所有者决定）

无 `LICENSE`、无 CI、无 lint/类型检查配置（ruff/mypy/black 均未配置，`pyproject.toml` 无 `[tool.ruff]`/`[tool.mypy]`）。对于一个自述"跨平台"的项目，缺 CI 意味着 Windows/Linux 的回归只能靠人工。

#### P2-6 依赖声明可疑 ✅ 已复核并移除

`pyproject.toml` 的 `dev` 额外依赖里写了 `httpx2>=2.0.0`。实测环境中 `httpx 0.28.1`（fastapi TestClient 所需）与 `httpx2 2.13.1` 并存，是**两个不同的包**。测试实际用的是 `httpx`（主依赖已声明），`httpx2` 疑为误加，建议确认其必要性，避免依赖树无谓膨胀（`docs/06` 提到的 Starlette 弃用提示也可能与此相关）。

---

## 四、测试覆盖缺口

现有 20 用例全绿，且覆盖了主流程（扫描/隔离/还原/重复/归档/缓存/决策/Web/purge 确认）。**但安全与成本边界完全没有用例**：

| 缺失用例 | 对应缺陷 |
|----------|----------|
| 决策路径含 `..` / 绝对路径应被拒绝 | P0-1 |
| `target_relpath` 越界应被拒绝 | P0-1 |
| `analyze` 在 candidates 为空时不应外发深层文件 | P1-2 |
| 只读根扫描不应崩溃 | P1-3 |
| `build_summary` 对 credentials/secret 类文件不抽样 | P1-1 |
| Web 决策 body 含非法路径应 400 | P0-1 |

---

## 五、改进建议路线图

### 第一优先（本次已修复并补测，见附录 B）

| 序号 | 动作 | 对应 |
|------|------|------|
| 1 | 新增 `resolve_under()` 并接入 decision/quarantine 全部路径拼接 | P0-1 |
| 2 | `apply_decisions` 返回 `skipped` 明细 | P2-4 |
| 3 | `analyze_run` 去掉短路条件 | P1-2 |
| 4 | 缓存写入加异常保护 + `--no-cache` | P1-3 |
| 5 | `build_summary` 增加文件名黑名单与更强脱敏正则 | P1-1 |
| 6 | 订正 `docs/03 §5` CLI 表；`.gitignore` 加 `build/` | D-1、P2-3 |
| 7 | 补 6 条回归用例 | 四 |

**验证结果**：修复后 **28 个用例全绿**（原有 20 + 新增 8，见 `tests/test_audit_fixes.py`），另有 12 点直接行为验证全部通过。逐条改动见 `修复说明-20261009.md`。

### 第二优先（已处理 / 待处理）

| 序号 | 动作 | 状态 |
|------|------|------|
| 1 | **提交本轮改动** | ⏳ 待项目所有者执行（改动混有此前遗留的未提交变更，建议分两笔提交） |
| 2 | **补 CI**（三平台 × pytest） | ✅ 已加 `.github/workflows/ci.yml`（三平台 × 3.11/3.12，跑 ruff + mypy + pytest） |
| 3 | **配置查找去源码树依赖**（P2-2） | ✅ 已改为 `$DIRTIDY_CONFIG` → `./models.yaml` → `~/.config/dirtidy/` → `configs/` 兜底 |
| 4 | **前端转义 + CSP**（P2-1） | ✅ `app.js` 全面去 `innerHTML`；`app.py` 加严格 CSP / nosniff / no-referrer |
| 5 | **清理 `build/` 与空 `apps/`**（P2-3） | ✅ `build/` 已 gitignore；空 `apps/` 已删除 |
| 6 | **文本抽样改默认关闭**（P1-1 第三层） | ⏭️ 未做（当前是"敏感文件名不抽样 + 值抹除"，已是安全默认；再收紧需产品决策） |
| 7 | **加 ruff + mypy，复核 `httpx2`** | ✅ 两者均已配置且本机零告警；`httpx2` 复核为未引用，已移除 |
| 8 | **修正 `docs/06` 的"已补完"结论**（D-3） | ✅ 已订正并登记待办 |

**第二轮验证**：`ruff` 零告警、`mypy` 21 文件零问题、`pytest -W error` **33 用例全绿**。

---

## 六、总体结论

- **过程资料**：结构专业、修订记录规范、有真红一致性测试，**质量偏上**；主要问题是 `docs/03` CLI 表与实现不符、`docs/06` 结论过于乐观、缺 CI/许可证。
- **结果资料**：分层干净、安全默认正确、细节（深度倒序隔离、缓存收敛、跨平台 posix 路径）考虑到位，**实现质量中上**；主要问题是**边界校验缺失**——路径不做根内校验、凭据脱敏形同虚设、外发目标筛选有短路缺陷、只读根会崩在最后一步。
- **一句话建议**：这个项目"骨架和默认值都对"，短板全在**输入边界的防御**上。把 `resolve_under` 这类"所有路径必须落在根内"的不变量补齐、把"默认不抽样正文"作为隐私默认，再补上安全回归用例，它就从"能用的个人工具"变成"敢交付给别人的工具"。

---

## 附录 A：复现命令

```bash
# 全量测试（本沙箱需指定工作区内临时目录）
TMPDIR="$PWD/.pyt1" .venv/bin/python -m pytest -q -p no:cacheprovider   # → 28 passed

# P0-1 路径穿越、P1-1 凭据脱敏、P1-2 外发目标、P1-3 只读根：
#   见 tests/test_audit_fixes.py，8 条用例覆盖
```

## 附录 B：本次实际修复与新增用例

见同目录 `修复说明-20261009.md`（含逐条改动、新增 8 条回归用例与验证记录）。
