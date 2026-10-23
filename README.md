# dirtidy

跨平台本地目录整理工具：YAML 规则清理（默认进隔离区）→ 可配置模型分析残留文件 → 本地 Web / CLI 人工终审留删与归类。

定位是**单机、单用户的后台程序**：可以无人值守长期跑（`watch` 定时扫描、`desktop` 常驻起服务），
也可以被中断后重跑。稳定性与性能按后台程序的标准要求，见 [docs/08-稳定性与性能设计.md](docs/08-稳定性与性能设计.md)。

**想快速了解全部功能、用法与注意事项，直接看 [docs/09-功能与使用汇总.md](docs/09-功能与使用汇总.md)。**

过程文档见 [docs/](docs/01-愿景与范围.md)；缺口与待办见 [docs/06-已知缺口与后续.md](docs/06-已知缺口与后续.md)。

## 安装

本仓位于 `~/Desktop/git/dirtidy`（独立 Git）。需要 Python >=3.11（若系统默认 python3 低于 3.11，如 3.9，请改用 3.11+ 的解释器）。建议在本目录建虚拟环境：

```bash
cd ~/Desktop/git/dirtidy
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

**装完必须激活虚拟环境，`dirtidy` 命令才会在 PATH 里**（最容易漏的一步）：

```bash
source .venv/bin/activate     # 激活后提示符前会出现 (.venv)
dirtidy --version             # 能打印版本号才算装好
```

> 不想每次都激活，有两种办法：
>
> 1. **直接用 venv 里的可执行文件**——把下文所有 `dirtidy` 换成 `.venv/bin/dirtidy`
>    （等价写法是 `.venv/bin/python -m dirtidy`）。
> 2. **一次软链，到处能用**（指向本仓 venv，源码改了立刻生效）：
>    ```bash
>    ln -sf "$PWD/.venv/bin/dirtidy" /usr/local/bin/dirtidy
>    ```
>    提示权限不足就加 `sudo`，或改链到 `~/bin/`（需已在 PATH 里）。
>    **注意**：venv 是这条软链的依赖，别删掉本仓的 `.venv/`。

想要桌面壳的托盘图标，再装可选增强（不装也能用，只是没有托盘）：

```bash
.venv/bin/python -m pip install -e ".[desktop]"
```

## 快速开始

> 下面假设虚拟环境已激活（`source .venv/bin/activate`）；
> 没激活就把 `dirtidy` 换成 `.venv/bin/dirtidy`。

**最短路径——一条命令：**

```bash
dirtidy start ~/Downloads
```

它会：没有扫描结果就先扫一次 → 起本地服务 → 自动打开浏览器。
剩下的在界面上点——概览页会直接告诉你「现在该做什么」。
（想重新扫描加 `--rescan`；已有结果时它不会重复扫。）

**想拆开做（脚本化 / 精细控制）：**

```bash
# 扫描（不立刻隔离）
dirtidy scan /path/to/root --rules server_workspace

# 先问「这个路径为什么会被/不会被动」
dirtidy explain /path/to/root src/logs --rules server_workspace

# 对 pending 高置信项执行隔离（--dry-run 只预览；--keep 本轮保留某项）
dirtidy apply-rules /path/to/root <run_id> --dry-run
dirtidy apply-rules /path/to/root <run_id> --keep logs/keep-me.log
dirtidy apply-rules /path/to/root <run_id>

# 或一步：扫描并隔离
dirtidy scan /path/to/root --rules default --apply

# 模型分析（无密钥可用 mock）
dirtidy analyze /path/to/root <run_id> --mock
# 真实模型：复制包内 src/dirtidy/configs/models.example.yaml → models.yaml 后
# dirtidy analyze /path/to/root <run_id> --config models.yaml
# 也可放 ./models.yaml 或 ~/.config/dirtidy/models.yaml，或用 DIRTIDY_CONFIG 指定

# 本地 Web 控制台（`dirtidy serve` / `dirtidy desktop`）：
#   分 概览 / 扫描 / 审阅 / 执行 / 隔离区 / 规则 / 设置 / 监视 八页（Cursor 清理是独立 CLI 支线）。
#   审阅页看待自动隔离、候选与受保护清单并设保留/决策；执行页先预览再确认搬移；
#   每条路径旁有「为什么？」按钮，弹窗与 CLI explain 是同一份解释。
#   隔离区页显示每条与合计占用空间，可按条「还原」或「彻底删除」释放空间。
#  会动文件的按钮都有忙态（转圈 + 点不动），窄屏下表格横向滚动。
dirtidy serve --root /path/to/root --port 8765

# 桌面壳：起本地服务 + 自动打开浏览器（端口被占用会自动顺延；
# 装了 pystray 还会显示托盘图标，没装就按 Ctrl-C 退出）
dirtidy desktop /path/to/root

# 定时监视：定期扫描并报告新增/消失的命中项 —— 只扫描，绝不执行隔离
dirtidy watch /path/to/root --interval 30

# Cursor 状态库专项清理（独立 CLI，不走通用目录隔离流程）。
# 默认只读预览；--days 90 仅选择有时间证据且超过 90 天的会话，近期或时间未知的保留。
# 删除选中的历史会话会丢失对应历史；blob 删除已禁用，确认口令也不能放行。
# ★ 请先完全退出 Cursor：边跑边改状态库会抢 WAL 锁，可能让 Cursor 起不来。
dirtidy cursor-cleanup --days 90 --measure
dirtidy cursor-cleanup --days 90 --apply
# --measure 是候选行字节估算，不保证最终磁盘释放量；备份和数据库校验不证明业务完整。
# 可选 --schedule-hours 24 是定时轮询，检测到 Cursor 未运行才清理，不监听退出事件。
# 备份默认落在库旁边：<库所在目录>/CursorStateBackup-<时间戳>/
# 清理过程同时写 <备份目录>/dirtidy-cursor-cleanup.jsonl（每阶段一行，中途被杀也留痕）。
# 想放到别处用 --backup-dir；放在同一卷上才能走 clonefile，跨卷会退化成全量拷贝。
```

产物目录：`<root>/.dirtidy/runs/<run_id>/`；隔离区：`<root>/.dirtidy/quarantine/<run_id>/`。未改动文件的内容 hash 缓存在 `<root>/.dirtidy/hash_cache.sqlite`。扫描可加 `--workers 8` 并行算 hash。

跨 run 的设置（直删规则、模型配置）也在状态目录里，**命令行与界面读同一份**：
`<root>/.dirtidy/settings.json` 与 `<root>/.dirtidy/models.yaml`。

只读介质或不想污染目标目录时，可用环境变量 `DIRTIDY_STATE_DIR` 把整个 `.dirtidy/` 重定位到外部可写目录。

规则包含「空目录自动隔离」时，搬走子项后新空出来的父目录会再清几轮。

## 规则与保护

规则包有三个 action：`protect`（**最高优先级**，命中即永不自动隔离，也不进候选）、`auto_quarantine`、`candidate_only`。
保护清单内置在 `src/dirtidy/rules/protect.py`，规则包关不掉：版本控制元数据、凭据目录、系统保留目录、密钥文件（`.env*`/`*.pem`/`id_rsa` 等）。

保护判定是**双向**的：目录命中保护 ⇒ 其整棵子树受保护；反过来，一个命中自动隔离的目录若子树内含受保护路径，
**整条命中会被拦下**并记入 `protection.json`。少了后者，保护会被「搬走它的父目录」绕开——
`node_modules` 命中 dep 规则时，`node_modules/.env`、`node_modules/id_rsa` 就会被一并端走。

这份判定覆盖**所有会搬文件的执行路径**（`apply-rules` / `apply-decisions` / `scan --apply`），且共用同一份实现；
判定器是必填参数，调用方漏传会直接报错，而不是让保护静默失效。

`dirtidy explain <路径>` 与真实执行共用同一份判定，因此不会出现「解释说要动、实际不动」：

```
$ dirtidy explain . node_modules
结论: 整条拦下——子树内含受保护路径，apply-rules 时不会隔离
  本来想动它的规则: dep_node_modules（Node / 前端依赖目录，可用包管理器重建）
  子树内的受保护路径: node_modules/.npmrc
  保护规则: builtin_protect_secret_files（密钥 / 凭据文件，误删不可恢复）
  已枚举后代: 2 条
```

> 上为**节选**：结论段之后还有「逐条保护规则命中明细」与生成时间。

想额外护住某个目录：

```bash
dirtidy scan /path/to/root --protect path/to/keep --protect another/dir
```

规则还支持 `when` 条件（`min_age_days`、`min/max_size_bytes`、`under`/`not_under`、`min/max_depth`、`require: [source_free]`），
用来把「目录名」这类粗糙匹配收窄——比如「`logs` 目录且 3 天没动过且里面没有源码」才自动隔离。详见 [docs/04-规则包说明.md](docs/04-规则包说明.md)。

## 安全约定

- 默认隔离，不直接抹盘；`purge-quarantine --yes` 或界面上明确点「彻底删除」才真正删
- **「直删」不是绕过隔离的另一条路径**：命中项仍先过人工保留与保护复核、搬入隔离区、写清单，然后才物理删除，并另记 `purged_directly.json`。**默认一条都不开**，要开得自己在「规则」页逐条勾；开了之后那些条目不可还原
- 隔离区页显示每条与合计**实算磁盘占用**（清单里的 `size` 对目录恒为 0，直接汇总会显示成「0 B」）；按条彻底删除会同步收敛清单，界面上的「已隔离」列表因此始终可信
- 重复文件与解压残留（hash 对照）默认仅候选
- 外发模型默认只传安全摘要，且 `summary.max_targets` 给单次外发封顶
- `apply-rules`（规则隔离）、`apply-decisions`（人工决策隔离）、`scan --apply` 三条路径在搬文件前都用同一份保护判定**再复核一遍**，语义一致；受保护项被拦下并记入 `protection.json`
- 这份判定器的参数是必填的：调用方漏传会直接报错，保护不会因「忘了传」而静默失效
- 命中自动隔离的目录若子树内含受保护路径（或子树不可读），整条拦下、不执行隔离——保护不能被「搬走父目录」绕开
- 目录与其内文件同时命中时只保留父目录一条，避免清单出现永远还原不回去的记录
- 0 字节文件不参与重复组判定（否则 `.env`/`id_rsa` 这类空文件会被凑成「内容相同」诱导误操作）
- `scan` / `apply-rules` / `apply-decisions` 都支持 `--dry-run`：先看「会动哪些、哪些被保护拦下」再决定执行。Web 执行页复用相同后端预览；规则条数取实际计划而非命中数，候选预览使用尚未保存的当前决策，不改写 `decisions.json`
- 自动隔离可在执行前人工保留（CLI `--keep 相对路径` / 网页勾选「保留」）；保留项本轮不动并记入 `protection.json`，可审计追溯。**保留判定是双向的**：保留目录 ⇒ 子树保留（否则子项被搬走、目录变空后被清扫收走，保留等于没生效）；保留项落在命中目录里 ⇒ 该目录本轮不动（搬走父目录等于把它一起搬走）
- 模型分析**默认关闭**，不打开就完全不外发；密钥可走环境变量（不落盘），也可在界面直接填——后者以**明文**写进 `<root>/.dirtidy/models.yaml`，界面上会提示；密钥框留空表示保持原样，不是清空
- `watch`（定时监视）**只扫描不执行**：源码里不存在任何执行入口，并有静态守护用例守着这条不变量。CLI 与 Web 单轮默认超时均为 1800 秒，来自同一常量；需要不限时可显式设为 0
- `desktop` 只是启动器，不改变任何行为；就绪判定走 `GET /api/health`，避免「端口通了但页面打不开」
- 非 quick 扫描只对**必要文件**算内容 hash（size 有重复的、或 size 命中归档成员的），
  结果与全量算等价但 IO 小一个量级；算过的结果缓存在 `<root>/.dirtidy/hash_cache.sqlite`。
  处理敏感目录时可加 `--quick` 完全跳过内容 hash 与缓存
- 产物写盘是**原子**的（临时文件 + 重命名）：进程被杀也不会留下半个 JSON
- 隔离中途被打断时，**已经搬走**的条目一定在清单里有记录，`quarantine-restore` 能还原回去
- **父目录时间戳保全**：文件移入隔离区、还原、归档时，自动在移动前后快照并原样恢复所有受影响祖先目录的修改时间（`st_mtime`）与访问时间（`st_atime`），消除操作系统底层（APFS / POSIX）自动将父目录时间刷新为当前时刻的副作用，实现无痕整理

## 规模

面向百万文件 / T 级数据设计，500 万条条目常驻内存约 **1.07 GB**（上限 2 GB）。
内存预算模型、采用的手段与实测数据见 [docs/07-规模与性能.md](docs/07-规模与性能.md)；

```bash
python scripts/bench_scale.py --files 1200   # 可复现基准：条目内存 / 扫描吞吐 / inventory 读写峰值
python scripts/smoke_e2e.py                  # 真实目录跑通 scan → apply-rules → analyze，并断言保护生效
```

## 测试

```bash
pytest -q            # 当前收集 372 个用例（含参数化）
ruff check .         # 含 scripts/，CI 同此范围
mypy
```

三平台矩阵（ubuntu / macos / windows × Python 3.11、3.12，共 6 个 job）由 `.github/workflows/ci.yml` 覆盖，
另有两个独立 job：`package`（真构建 wheel 并装进干净 venv 验证）与 `smoke`（`scripts/smoke_e2e.py` 端到端）。推到 GitHub 后生效。

## 许可证

[MIT](LICENSE) © 2026 lichaowei

本工具会移动/隔离目标目录下的文件，请在使用前确认规则与决策；软件按「原样」提供，不附带任何明示或暗示的担保。
