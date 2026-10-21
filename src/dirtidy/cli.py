"""CLI 入口：把子命令映射到 pipeline / quarantine / ai / web。"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from dirtidy import __version__
from dirtidy.ai.providers import analyze_run
from dirtidy.decision.store import apply_decisions, save_decisions
from dirtidy.models import Decision, iter_json_array, quarantine_root, read_json, run_dir, state_root
from dirtidy.pipeline import apply_pending_rules, create_run
from dirtidy.quarantine.store import purge_entries, purge_quarantine_dir, restore_entries
from dirtidy.watch import WATCH_DEFAULT_TIMEOUT

#: 单次运行超时时的退出码。与 1（explain 找不到路径）、2（用法错误）区分开，
#: 脚本能凭它判断「这次是超时，重跑或调大时限即可」，而不是「跑失败了」。
EXIT_TIMEOUT = 3

_TIMEOUT_HELP = (
    "单次运行的时限（秒，默认 0 = 不限）。超时**不丢结果**：已扫到的部分照常写进 run 产物，"
    "但**不执行隔离**；退出码为 3"
)


def _resolve_root(path: str) -> Path:
    return Path(path).expanduser().resolve()


def timeout_phrase(meta: dict) -> str:
    """把 meta 里的超时信息翻成一句人话，供 CLI 与界面共用同一套措辞。"""
    info = meta.get("timeout") or {}
    phase = info.get("phase")
    seconds = info.get("seconds")
    processed = info.get("processed")
    bits = [f"{phase}阶段" if phase else "未知阶段"]
    if seconds:
        bits.append(f"超过 {seconds:g} 秒")
    if processed is not None:
        bits.append(f"已处理 {processed} 项")
    return "，".join(bits)


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
            timeout=args.timeout,
        )
        run_id = str(meta.get("run_id", ""))
        cpath = run_dir(root, run_id) / "candidates.json"
        n_cand = sum(1 for _ in iter_json_array(cpath)) if cpath.exists() else 0
        print(
            f"扫描完成：规则命中 {meta.get('auto_hits', 0)} 项、候选 {n_cand} 项。",
            file=sys.stderr,
        )
        if meta.get("timed_out"):
            # 超时的产物**看起来和完整产物一样**，不在这里说清楚，用户会以为目录里就这么多。
            print(
                f"注意：本次扫描超时（{timeout_phrase(meta)}），只覆盖了一部分目录，"
                "结果不完整。可把 --timeout 调大或设为 0 后重新扫描。",
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
        timeout=args.timeout,
    )
    if args.dry_run:
        meta = {**meta, "dry_run": True}
    print(json.dumps(meta, ensure_ascii=False, indent=2))
    if meta.get("timed_out"):
        print(
            f"运行超时（{timeout_phrase(meta)}）：已把扫到的部分写进 run 产物，"
            "**未执行隔离**。可把 --timeout 调大或设为 0 后重跑。",
            file=sys.stderr,
        )
        return EXIT_TIMEOUT
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
    rdir = run_dir(root, args.run_id)
    manifest = rdir / "quarantine_manifest.json"
    # 与 Web 端 `POST /api/runs/{run_id}/purge` **同一套语义**：先按条收敛清单，
    # 再清掉空壳目录。原先这里只调 `purge_quarantine_dir`，清单原封不动——
    # 实测「清前 3 条 → 清后仍是 3 条」，界面上「已隔离」列表全是幽灵条目，
    # 用户按它还原则得到「文件不存在」。同一个「彻底删除」写两份，必有偏差。
    purged = purge_entries(root, manifest, purge_all=True)
    purge_quarantine_dir(qdir)
    print(
        json.dumps(
            {"purged": str(qdir), "entries": len(purged), "paths": purged},
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from dirtidy.web.app import create_app

    root = _resolve_root(args.root) if args.root else Path.cwd().resolve()
    if args.reload:
        # uvicorn 的 reload 模式要在子进程里重新导入 app，因此 app 必须是 import string；
        # 而 create_app 需要 root / run_id，只能经环境变量传进去（见 web/app.py 的
        # `_reload_target`）。**只用于开发**：改 src/ 下的 Python 代码自动重启，
        # 省掉每次手动 Ctrl+C。默认关闭，不影响正常使用。
        os.environ["DIRTIDY_RELOAD_ROOT"] = str(root)
        if args.run_id:
            os.environ["DIRTIDY_RELOAD_RUN_ID"] = args.run_id
        else:
            os.environ.pop("DIRTIDY_RELOAD_RUN_ID", None)
        # 只盯包目录：盯整个 cwd 的话，扫一个几万文件的目录会不断触发重载。
        uvicorn.run(
            "dirtidy.web.app:_reload_target",
            factory=True,
            host=args.host,
            port=args.port,
            reload=True,
            reload_dirs=[str(Path(__file__).resolve().parent)],
        )
        return 0
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
        timeout=args.timeout,
    )


def cmd_save_decisions(args: argparse.Namespace) -> int:
    root = _resolve_root(args.root)
    rdir = run_dir(root, args.run_id)
    data = json.loads(Path(args.file).read_text(encoding="utf-8"))
    save_decisions(rdir, [Decision(**d) for d in data])
    print(json.dumps({"saved": len(data)}, ensure_ascii=False, indent=2))
    return 0


def cmd_cursor_cleanup(args: argparse.Namespace) -> int:
    """清理 Cursor 的全局状态库。

    默认只统计不改库（与 `scan` / `apply-rules` 的 `--dry-run` 语义一致），
    真正动手必须显式加 `--apply`。
    """
    from dirtidy.cursor_cleanup import (
        DEFAULT_DB,
        CursorCleanupError,
        analyze,
        clean,
        cursor_running,
    )

    db = Path(args.db).expanduser() if args.db else DEFAULT_DB
    try:
        if not args.apply:
            report = analyze(
                db,
                days=args.days,
                include_orphan=not args.no_orphan,
                include_blob=not args.no_blob,
                measure_blob_bytes=args.measure,
            )
            print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
            print()
            print("干跑结束（未改动任何东西）。确认无误后加 --apply 真正清理。")
            return 0

        # 写库前先确认 Cursor 已退出：在跑就改库会抢 WAL 锁，
        # Cursor 会报 storage file corrupted/moved/permission 而拒绝启动。
        if cursor_running():
            print(
                "检测到 Cursor 仍在运行。请先完全退出 Cursor（菜单栏 Cursor → Quit）"
                "再执行 —— 边跑边改状态库会抢锁，可能让 Cursor 起不来。",
                file=sys.stderr,
            )
            return 2

        report = clean(
            db,
            days=args.days,
            include_orphan=not args.no_orphan,
            include_blob=not args.no_blob,
            vacuum=not args.no_vacuum,
            backup_dir=Path(args.backup_dir).expanduser() if args.backup_dir else None,
            blob_pause=args.blob_pause,
        )
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
        print()
        print("清理完成。请重新打开 Cursor 验证。")
        return 0
    except CursorCleanupError as e:
        print(f"错误：{e}", file=sys.stderr)
        return 1


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
    s.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        metavar="秒",
        help=_TIMEOUT_HELP,
    )
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
    s.add_argument("--timeout", type=float, default=0.0, metavar="秒", help=_TIMEOUT_HELP)
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
    s.add_argument(
        "--reload",
        action="store_true",
        help="开发用：改了 src/ 下的 Python 代码自动重启服务（默认关闭；"
        "前端静态文件仍建议硬刷新浏览器）",
    )
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
    s.add_argument(
        "--timeout",
        type=float,
        default=WATCH_DEFAULT_TIMEOUT,
        metavar="秒",
        help="单轮扫描的时限（秒，默认 1800=30 分钟；0 = 不限）。"
        "监视是无人值守的，一轮卡住就等于整个监视静默停摆，所以这里默认有限时；"
        "超时只跳过这一轮并报出来，下一轮照常",
    )
    s.add_argument("--protect", action="append", default=[], metavar="REL_PATH",
                   help="额外保护某个相对路径（含其整棵子树），可重复")
    s.set_defaults(func=cmd_watch)

    s = sub.add_parser(
        "cursor-cleanup",
        help="清理 Cursor 全局状态库 state.vscdb（默认只统计，加 --apply 才动手）",
    )
    s.add_argument(
        "--days",
        type=int,
        default=90,
        metavar="N",
        help="保留最近 N 天内的会话（默认 90，约三个月）",
    )
    s.add_argument(
        "--apply",
        action="store_true",
        help="真正动手清理（默认只统计、不改库）",
    )
    s.add_argument("--db", default=None, help="state.vscdb 路径（默认本机 Cursor globalStorage）")
    s.add_argument(
        "--backup-dir",
        default=None,
        metavar="目录",
        help="冷备份目录（默认 Cursor globalStorage 下的 CursorStateBackup-<时间戳>）",
    )
    s.add_argument(
        "--blob-pause",
        type=float,
        default=3.0,
        metavar="秒",
        help="删 blob 时每批之间的间隔秒数（默认 3；越大越不影响 Cursor，但越慢）",
    )
    s.add_argument("--no-orphan", action="store_true", help="不清理孤儿会话正文")
    s.add_argument("--no-blob", action="store_true", help="不清理无引用的 agentKv:blob")
    s.add_argument("--no-vacuum", action="store_true", help="跳过 VACUUM（删除快，但文件体积暂不缩小）")
    s.add_argument(
        "--measure",
        action="store_true",
        help="干跑时额外估算将腾出多少空间（较慢：要读 blob 内容算体积）",
    )
    s.set_defaults(func=cmd_cursor_cleanup)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ValueError as exc:
        # 非法输入（典型：run_id 带 `..` 或路径分隔符）属于「用错了」，不是程序崩溃。
        # 直接抛 traceback 会让人以为是 bug；给一句可操作的话并返回非零。
        print(f"错误：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    # 没有这个块时 `python -m dirtidy.cli scan ...` 只会导入模块、静默 exit 0，
    # 让人以为命令跑成功了。标准入口是 `python -m dirtidy`（见 __main__.py），
    # 这里补上以防误用。
    raise SystemExit(main())
