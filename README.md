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

# 对 pending 高置信项执行隔离
dirtidy apply-rules /path/to/root <run_id>

# 或一步：扫描并隔离
dirtidy scan /path/to/root --rules default --apply

# 模型分析（无密钥可用 mock）
dirtidy analyze /path/to/root <run_id> --mock
# 真实模型：复制 configs/models.example.yaml → models.yaml 后
# dirtidy analyze /path/to/root <run_id> --config models.yaml
# 也可放 ./models.yaml 或 ~/.config/dirtidy/models.yaml，或用 DIRTIDY_CONFIG 指定

# 本地 Web 审阅
dirtidy serve --root /path/to/root --port 8765
```

产物目录：`<root>/.dirtidy/runs/<run_id>/`；隔离区：`<root>/.dirtidy/quarantine/<run_id>/`。未改动文件的内容 hash 缓存在 `<root>/.dirtidy/hash_cache.json`。扫描可加 `--workers 8` 并行算 hash。

只读介质或不想污染目标目录时，可用环境变量 `DIRTIDY_STATE_DIR` 把整个 `.dirtidy/` 重定位到外部可写目录。

规则包含「空目录自动隔离」时，搬走子项后新空出来的父目录会再清几轮。

## 安全约定

- 默认隔离，不直接抹盘；`purge-quarantine --yes` 才彻底删隔离副本
- 重复文件与解压残留（hash 对照）默认仅候选
- 外发模型默认只传安全摘要
- 非 quick 扫描将文件 `path + size + mtime + sha256` 缓存在 `<root>/.dirtidy/hash_cache.json`；处理敏感目录时可加 `--quick` 跳过内容 hash 与缓存

## 测试

```bash
pytest -q            # 33 个用例
ruff check src tests
mypy
```

三平台矩阵（ubuntu / macos / windows × Python 3.11、3.12）由 `.github/workflows/ci.yml` 覆盖，推送到 GitHub 后生效。

## 许可证

[MIT](LICENSE) © 2026 lichaowei

本工具会移动/隔离目标目录下的文件，请在使用前确认规则与决策；软件按「原样」提供，不附带任何明示或暗示的担保。
