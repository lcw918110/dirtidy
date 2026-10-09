"""CLI 入口：把子命令映射到 pipeline / quarantine / ai / web。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dirtidy import __version__
from dirtidy.ai.providers import analyze_run
from dirtidy.decision.store import apply_decisions, save_decisions
from dirtidy.models import Decision, quarantine_root, run_dir
from dirtidy.pipeline import apply_pending_rules, create_run
from dirtidy.quarantine.store import purge_quarantine_dir, restore_entries


def _resolve_root(path: str) -> Path:
    return Path(path).expanduser().resolve()


def cmd_scan(args: argparse.Namespace) -> int:
    import sys

    def _progress(done: int, total: int) -> None:
        print(f"\rhash {done}/{total}", end="", file=sys.stderr, flush=True)
        if done == total:
            print(file=sys.stderr)

    meta = create_run(
        _resolve_root(args.root),
        rules=args.rules,
        quick=args.quick,
        apply=args.apply,
        workers=args.workers,
        on_progress=None if args.quick else _progress,
        protect=args.protect,
    )
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


def cmd_apply_rules(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    result = apply_pending_rules(root, args.run_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    rdir = run_dir(root, args.run_id)
    cfg = Path(args.config).resolve() if args.config else None
    results = analyze_run(root, rdir, config_path=cfg, mock=args.mock)
    print(json.dumps({"suggestions": len(results), "run_id": args.run_id}, ensure_ascii=False, indent=2))
    return 0


def cmd_apply_decisions(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    rdir = run_dir(root, args.run_id)
    qdir = quarantine_root(root, args.run_id)
    result = apply_decisions(root, args.run_id, rdir, qdir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    rdir = run_dir(root, args.run_id)
    ids = set(args.id) if args.id else None
    restored = restore_entries(
        root,
        rdir / "quarantine_manifest.json",
        entry_ids=ids,
        restore_all=args.all,
    )
    print(json.dumps({"restored": restored}, ensure_ascii=False, indent=2))
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    # 硬删除必须显式确认，防止误清隔离区
    if not args.yes:
        print("需要 --yes 确认彻底清除隔离区")
        return 2
    root = _resolve_root(args.root)
    qdir = quarantine_root(root, args.run_id)
    purge_quarantine_dir(qdir)
    print(json.dumps({"purged": str(qdir)}, ensure_ascii=False, indent=2))
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from dirtidy.web.app import create_app

    root = _resolve_root(args.root) if args.root else Path.cwd().resolve()
    app = create_app(default_root=root, default_run_id=args.run_id)
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


def cmd_save_decisions(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    rdir = run_dir(root, args.run_id)
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    save_decisions(rdir, [Decision(**d) for d in data])
    print(json.dumps({"saved": len(data)}, ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="dirtidy", description="本地目录整理：规则隔离 + 模型建议 + 人工终审")
    p.add_argument("--version", action="version", version=f"dirtidy {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("scan", help="扫描并生成 run；可选立即 apply 高置信规则")
    s.add_argument("root", help="目标根目录")
    s.add_argument("--rules", default="default", help="规则包名或 YAML 路径")
    s.add_argument("--quick", action="store_true", help="跳过内容 hash / 解压对照")
    s.add_argument("--apply", action="store_true", help="扫描后立即隔离 auto 规则命中项")
    s.add_argument("--workers", type=int, default=4, help="内容 hash 并行线程数")
    s.add_argument(
        "--protect",
        action="append",
        default=[],
        metavar="REL_PATH",
        help="额外保护某个相对路径（含其整棵子树），可重复；命中的路径永不自动隔离",
    )
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("apply-rules", help="对已有 run 的 pending auto 命中执行隔离")
    s.add_argument("root")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_apply_rules)

    s = sub.add_parser("analyze", help="模型分析残留/候选")
    s.add_argument("root")
    s.add_argument("run_id")
    s.add_argument("--config", help="models.yaml 路径")
    s.add_argument("--mock", action="store_true", help="使用 mock provider")
    s.set_defaults(func=cmd_analyze)

    s = sub.add_parser("apply-decisions", help="执行 decisions.json")
    s.add_argument("root")
    s.add_argument("run_id")
    s.set_defaults(func=cmd_apply_decisions)

    s = sub.add_parser("save-decisions", help="从 JSON 文件写入 decisions")
    s.add_argument("root")
    s.add_argument("run_id")
    s.add_argument("file")
    s.set_defaults(func=cmd_save_decisions)

    s = sub.add_parser("quarantine-restore", help="从隔离区还原")
    s.add_argument("root")
    s.add_argument("run_id")
    s.add_argument("--all", action="store_true")
    s.add_argument("--id", action="append", default=[])
    s.set_defaults(func=cmd_restore)

    s = sub.add_parser("purge-quarantine", help="彻底删除隔离区副本")
    s.add_argument("root")
    s.add_argument("run_id")
    s.add_argument("--yes", action="store_true")
    s.set_defaults(func=cmd_purge)

    s = sub.add_parser("serve", help="启动本地 Web 审阅")
    s.add_argument("--root", default=".", help="默认目标根目录")
    s.add_argument("--run-id", dest="run_id", default=None)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(func=cmd_serve)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))
