"""CLI 入口：把子命令映射到 pipeline / quarantine / ai / web。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from dirtidy import __version__
from dirtidy.ai.providers import analyze_run
from dirtidy.decision.store import apply_decisions, save_decisions
from dirtidy.models import Decision, iter_json_array, quarantine_root, read_json, run_dir, state_root
from dirtidy.pipeline import apply_pending_rules, create_run
from dirtidy.quarantine.store import purge_quarantine_dir, restore_entries


def _resolve_root(path: str) -> Path:
    return Path(path).expanduser().resolve()


def _existing_runs(root: Path) -> list[str]:
    """已有 run 的 id，最新的在前。"""
    runs_root = state_root(root) / "runs"
    if not runs_root.is_dir():
        return []
    return sorted((p.name for p in runs_root.iterdir() if p.is_dir()), reverse=True)


def cmd_start(args: argparse.Namespace) -> int:
    """一条命令起步：没有扫描结果就先扫一次，然后起界面并打开浏览器。

    这是给「不想记命令」的人用的最短路径：`dirtidy start <目录>`。
    其余子命令是它的可拆解步骤，需要脚本化时再单独用。
    """
    import sys

    from dirtidy.desktop import launch

    root = _resolve_root(args.root)
    if args.rescan or not _existing_runs(root):
        print(f"正在扫描 {root} …", file=sys.stderr)

        def _progress(done: int, total: int) -> None:
            print(f"\rhash {done}/{total}", end="", file=sys.stderr, flush=True)
            if done == total:
                print(file=sys.stderr)

        meta = create_run(
            root,
            rules=args.rules,
            quick=args.quick,
            apply=False,
            workers=args.workers,
            on_progress=None if args.quick else _progress,
            protect=args.protect,
        )
        run_id = str(meta.get("run_id", ""))
        cpath = run_dir(root, run_id) / "candidates.json"
        n_cand = sum(1 for _ in iter_json_array(cpath)) if cpath.exists() else 0
        print(
            f"扫描完成：规则命中 {meta.get('auto_hits', 0)} 项、候选 {n_cand} 项。",
            file=sys.stderr,
        )
    else:
        print("已有扫描结果，直接打开界面（想重新扫描加 --rescan）。", file=sys.stderr)
    return launch(
        root,
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        use_tray=not args.no_tray,
    )


def cmd_scan(args: argparse.Namespace) -> int:
    import sys

    if args.dry_run and args.apply:
        print("--dry-run 与 --apply 互斥：dry-run 只预览不落盘。")
        return 2

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
    if args.dry_run:
        meta = {**meta, "dry_run": True}
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    return 0


def cmd_apply_rules(args: argparse.Namespace) -> int:
    from dirtidy.settings import load_auto_purge_rules

    root = _resolve_root(args.root)
    # 「隔离后立即物理删除」的规则清单由状态目录里的 settings.json 提供，
    # 界面与命令行读的是同一份——否则会出现「网页里开了直删、命令行跑却只隔离」。
    result = apply_pending_rules(
        root,
        args.run_id,
        dry_run=args.dry_run,
        keep=args.keep or (),
        purge_rule_ids=load_auto_purge_rules(root),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_explain(args: argparse.Namespace) -> int:
    from dirtidy.explain import explain_path
    from dirtidy.pipeline import protect_rules_from_paths

    root = _resolve_root(args.root)
    try:
        exp = explain_path(
            root,
            args.path,
            rules=args.rules,
            extra_protect_rules=protect_rules_from_paths(args.protect),
        )
    except ValueError as e:
        print(f"无法解释该路径: {e}")
        return 2
    if args.json:
        print(json.dumps(exp.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(exp.to_text())
    return 0 if exp.exists else 1


def cmd_analyze(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    rdir = run_dir(root, args.run_id)
    cfg = Path(args.config).resolve() if args.config else None
    results = analyze_run(root, rdir, config_path=cfg, mock=args.mock)
    meta = read_json(rdir / "ai_meta.json") if (rdir / "ai_meta.json").exists() else {}
    print(
        json.dumps(
            {"suggestions": len(results), "run_id": args.run_id, **meta},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_apply_decisions(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    rdir = run_dir(root, args.run_id)
    qdir = quarantine_root(root, args.run_id)
    result = apply_decisions(root, args.run_id, rdir, qdir, dry_run=args.dry_run)
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


def cmd_desktop(args: argparse.Namespace) -> int:
    from dirtidy.desktop import launch

    return launch(
        _resolve_root(args.root),
        host=args.host,
        port=args.port,
        open_browser=not args.no_browser,
        use_tray=not args.no_tray,
    )


def cmd_watch(args: argparse.Namespace) -> int:
    from dirtidy.watch import run_watch

    return run_watch(
        _resolve_root(args.root),
        interval_minutes=args.interval,
        rules=args.rules,
        quick=not args.full,
        protect=args.protect,
        max_rounds=args.rounds,
        notify_enabled=not args.no_notify,
    )


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

    s = sub.add_parser("start", help="★ 一条命令起步：必要时先扫描，然后起界面并打开浏览器")
    s.add_argument("root", nargs="?", default=".", help="目标根目录（默认当前目录）")
    s.add_argument("--rules", default="default", help="规则包名或 YAML 路径")
    s.add_argument("--quick", action="store_true", help="跳过内容 hash / 解压对照（大目录更快）")
    s.add_argument("--workers", type=int, default=4, help="内容 hash 并行线程数")
    s.add_argument("--protect", action="append", default=[], metavar="REL_PATH",
                   help="额外保护某个相对路径（含其整棵子树），可重复")
    s.add_argument("--rescan", action="store_true", help="即使已有扫描结果也重新扫描")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765, help="首选端口；被占用会自动顺延")
    s.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    s.add_argument("--no-tray", action="store_true", help="不显示托盘图标")
    s.set_defaults(func=cmd_start)

    s = sub.add_parser("scan", help="扫描并生成 run；可选立即 apply 高置信规则")
    s.add_argument("root", help="目标根目录")
    s.add_argument("--rules", default="default", help="规则包名或 YAML 路径")
    s.add_argument("--quick", action="store_true", help="跳过内容 hash / 解压对照")
    s.add_argument("--apply", action="store_true", help="扫描后立即隔离 auto 规则命中项")
    s.add_argument("--workers", type=int, default=4, help="内容 hash 并行线程数")
    s.add_argument("--protect", action="append", default=[], metavar="REL_PATH",
                   help="额外保护某个相对路径（含其整棵子树），可重复；命中的路径永不自动隔离")
    s.add_argument("--dry-run", action="store_true", help="只预览不落盘（与 --apply 互斥）")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("explain", help="解释某个相对路径会被如何处理（规则/条件/保护逐条摊开）")
    s.add_argument("root")
    s.add_argument("path", help="相对目标根的路径")
    s.add_argument("--rules", default="default", help="规则包名或 YAML 路径")
    s.add_argument("--protect", action="append", default=[], metavar="REL_PATH",
                   help="按指定的额外保护路径来评估，可重复")
    s.add_argument("--json", action="store_true", help="以 JSON 输出（默认人类可读文本）")
    s.set_defaults(func=cmd_explain)

    s = sub.add_parser("apply-rules", help="对已有 run 的 pending auto 命中执行隔离")
    s.add_argument("root")
    s.add_argument("run_id")
    s.add_argument("--dry-run", action="store_true", help="只列出会搬走哪些、哪些被保护拦下")
    s.add_argument(
        "--keep",
        action="append",
        default=[],
        metavar="相对路径",
        help="本轮保留不隔离（可重复）；会登记进 protection.json 便于审计追溯",
    )
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
    s.add_argument("--dry-run", action="store_true", help="只校验并预览，不移动任何文件")
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

    s = sub.add_parser("desktop", help="薄壳启动器：起本地服务并打开浏览器（降低启动门槛）")
    s.add_argument("root", nargs="?", default=".", help="目标根目录（默认当前目录）")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765, help="首选端口；被占用会自动顺延")
    s.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    s.add_argument("--no-tray", action="store_true", help="不显示托盘图标")
    s.set_defaults(func=cmd_desktop)

    s = sub.add_parser("watch", help="定时监视：定期扫描并报告新增命中（只扫描，绝不执行隔离）")
    s.add_argument("root", nargs="?", default=".", help="目标根目录（默认当前目录）")
    s.add_argument("--rules", default="default", help="规则包名或 YAML 路径")
    s.add_argument("--interval", type=float, default=30.0, help="扫描间隔（分钟）")
    s.add_argument("--full", action="store_true", help="包含内容 hash（默认跳过，仅看规则命中）")
    s.add_argument("--rounds", type=int, default=None, help="只跑 N 轮后退出（默认无限）")
    s.add_argument("--no-notify", action="store_true", help="不发系统通知")
    s.add_argument("--protect", action="append", default=[], metavar="REL_PATH",
                   help="额外保护某个相对路径（含其整棵子树），可重复")
    s.set_defaults(func=cmd_watch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":  # pragma: no cover
    # 没有这个块时 `python -m dirtidy.cli scan ...` 只会导入模块、静默 exit 0，
    # 让人以为命令跑成功了。标准入口是 `python -m dirtidy`（见 __main__.py），
    # 这里补上以防误用。
    raise SystemExit(main())
