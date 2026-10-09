# dirtidy

跨平台本地目录整理工具：YAML 规则清理（默认进隔离区）→ 可配置模型分析残留文件 → 本地 Web / CLI 人工终审留删与归类。

过程文档见 [docs/](docs/01-愿景与范围.md)；缺口清单见 [docs/06-已知缺口与后续.md](docs/06-已知缺口与后续.md)。

## 安装

本仓位于 `~/Desktop/git/dirtidy`（独立 Git）。需要 Python >=3.11（若系统默认 python3 低于 3.11，如 3.9，请改用 3.11+ 的解释器）。建议在本目录建虚拟环境：

```bash
cd ~/Desktop/git/dirtidy
python3 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

## 快速开始

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

# 本地 Web 审阅：可看「待自动隔离」并勾选保留后直接执行，也能看受保护清单与已隔离清单
dirtidy serve --root /path/to/root --port 8765
```

产物目录：`<root>/.dirtidy/runs/<run_id>/`；隔离区：`<root>/.dirtidy/quarantine/<run_id>/`。未改动文件的内容 hash 缓存在 `<root>/.dirtidy/hash_cache.sqlite`。扫描可加 `--workers 8` 并行算 hash。

只读介质或不想污染目标目录时，可用环境变量 `DIRTIDY_STATE_DIR` 把整个 `.dirtidy/` 重定位到外部可写目录。

规则包含「空目录自动隔离」时，搬走子项后新空出来的父目录会再清几轮。

## 规则与保护

规则包有三个 action：`protect`（**最高优先级**，命中即永不自动隔离，也不进候选）、`auto_quarantine`、`candidate_only`。
保护清单内置在 `src/dirtidy/rules/protect.py`，规则包关不掉：版本控制元数据、凭据目录、系统保留目录、密钥文件（`.env*`/`*.pem`/`id_rsa` 等）。

想额外护住某个目录：

```bash
dirtidy scan /path/to/root --protect path/to/keep --protect another/dir
```

规则还支持 `when` 条件（`min_age_days`、`min/max_size_bytes`、`under`/`not_under`、`min/max_depth`、`require: [source_free]`），
用来把「目录名」这类粗糙匹配收窄——比如「`logs` 目录且 3 天没动过且里面没有源码」才自动隔离。详见 [docs/04-规则包说明.md](docs/04-规则包说明.md)。

## 安全约定

- 默认隔离，不直接抹盘；`purge-quarantine --yes` 才彻底删隔离副本
- 重复文件与解压残留（hash 对照）默认仅候选
- 外发模型默认只传安全摘要，且 `summary.max_targets` 给单次外发封顶
- `apply-rules` 在搬文件前会用当前生效的保护清单**再复核一遍**，防止 pending 被改过就绕过保护
- `scan` / `apply-rules` / `apply-decisions` 都支持 `--dry-run`：先看「会动哪些、哪些被保护拦下」再决定执行
- 自动隔离可在执行前人工保留（CLI `--keep 相对路径` / 网页勾选「保留」）；保留项本轮不动并记入 `protection.json`，可审计追溯
- 非 quick 扫描将文件 `path + size + mtime + sha256` 缓存在 `<root>/.dirtidy/hash_cache.sqlite`；处理敏感目录时可加 `--quick` 跳过内容 hash 与缓存

## 规模

面向百万文件 / T 级数据设计，500 万条条目常驻内存预算约 1.0–1.5 GB。
内存预算模型、采用的手段与实测数据见 [docs/07-规模与性能.md](docs/07-规模与性能.md)；

```bash
python scripts/bench_scale.py --files 1200   # 可复现基准：条目内存 / 扫描吞吐 / inventory 读写峰值
python scripts/smoke_e2e.py                  # 真实目录跑通 scan → apply-rules → analyze，并断言保护生效
```

## 测试

```bash
pytest -q            # 106 个用例
ruff check src tests
mypy
```

三平台矩阵（ubuntu / macos / windows × Python 3.11、3.12）由 `.github/workflows/ci.yml` 覆盖，推送到 GitHub 后生效。

## 许可证

[MIT](LICENSE) © 2026 lichaowei

本工具会移动/隔离目标目录下的文件，请在使用前确认规则与决策；软件按「原样」提供，不附带任何明示或暗示的担保。
