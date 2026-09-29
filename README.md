# dirtidy

跨平台本地目录整理工具：YAML 规则清理（默认进隔离区）→ 可配置模型分析残留文件 → 本地 Web / CLI 人工终审留删与归类。

过程文档见 [docs/](docs/01-愿景与范围.md)；缺口清单见 [docs/06-已知缺口与后续.md](docs/06-已知缺口与后续.md)。

## 安装

建议使用工作区根 `.venv`（若在 AI 办公目录下）。若 `.venv` 同时存在 3.13/3.14，请与默认 `python` 对齐安装：

```bash
cd dirtidy
../.venv/bin/python -m pip install -e ".[dev]"
```

或本机 Python 3.11+：

```bash
python3 -m venv .venv   # 仅当不在办公仓共享环境时
pip install -e ".[dev]"
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

# 本地 Web 审阅
dirtidy serve --root /path/to/root --port 8765
```

产物目录：`<root>/.dirtidy/runs/<run_id>/`；隔离区：`<root>/.dirtidy/quarantine/<run_id>/`。未改动文件的内容 hash 缓存在 `<root>/.dirtidy/hash_cache.json`。扫描可加 `--workers 8` 并行算 hash。

规则包含「空目录自动隔离」时，搬走子项后新空出来的父目录会再清几轮。

## 安全约定

- 默认隔离，不直接抹盘；`purge-quarantine --yes` 才彻底删隔离副本
- 重复文件与解压残留（hash 对照）默认仅候选
- 外发模型默认只传安全摘要

## 测试

```bash
pytest -q
```
