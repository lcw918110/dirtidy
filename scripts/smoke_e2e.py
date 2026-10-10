"""构造冒烟测试目录并跑通 scan → apply-rules → analyze → 审计 全流程。

用法：python scripts/smoke_e2e.py [--keep]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WS = REPO / ".smoke" / "ws"


def build() -> None:
    shutil.rmtree(WS.parent, ignore_errors=True)
    for d in (
        "app/node_modules/pkg",
        "app/__pycache__",
        "app/logs",
        "app/.git/objects",
        "src/build",
        "src/logs",
        # 下面这个目录名 `.cache` 本来会命中 dep_tool_cache 被自动隔离，
        # 但它落在 `.ssh` 里 → 应当被内置保护清单挡下（降级为「只看不动」）
        ".ssh/.cache",
    ):
        (WS / d).mkdir(parents=True, exist_ok=True)

    files = {
        "app/main.py": "print(1)\n",
        "app/node_modules/pkg/index.js": "x\n",
        # 目录命中 dep_node_modules 会被自动隔离，但它子树里有 .npmrc（密钥文件）→
        # 整条命中必须被拦下。这是很现实的组合：npm 常把带 token 的 .npmrc 放在这里。
        "app/node_modules/.npmrc": "//registry.example.com/:_authToken=SHOULD-NOT-BE-TOUCHED\n",
        "app/__pycache__/main.cpython-312.pyc": "y\n",
        "app/logs/app.log": "old log\n",
        "app/.git/objects/ab12": "HEAD\n",
        "src/build/main.go": "package main\n",
        "src/logs/handler.py": "def handle(): ...\n",
        ".ssh/.cache/ssh_cashe_entry": "cached\n",
        ".env": "TOKEN=should-not-be-touched\n",
        "id_rsa": "PRIVATE KEY\n",
        "server.pem": "cert\n",
        "notes.txt": "hello\n",
        "old.tmp": "junk\n",
        "README.md": "readme\n",
    }
    for rel, content in files.items():
        p = WS / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    old = time.time() - 30 * 86400
    # 目录自身的 mtime 也要回拨：min_age_days 看的是目录 mtime，
    # 只回拨文件会先卡在 min_age_days 上，看不到 source_free 守卫那一层
    for rel in ("app/logs", "src/logs", "app/logs/app.log", "src/logs/handler.py", "old.tmp"):
        os.utime(WS / rel, (old, old))


def run(*args: str) -> str:
    cmd = [sys.executable, "-m", "dirtidy", *args]
    print(f"\n$ {' '.join(cmd)}")
    out = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, check=False)
    if out.returncode != 0:
        print(out.stdout)
        print(out.stderr, file=sys.stderr)
        raise SystemExit(f"命令失败（exit {out.returncode}）")
    print(out.stdout.strip()[:2000])
    return out.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="跑完保留目录，便于人工检查")
    args = ap.parse_args()

    build()
    print(f"工作区：{WS}")

    meta = json.loads(run("scan", str(WS), "--rules", "server_workspace"))
    run_id = meta["run_id"]
    rdir = WS / ".dirtidy" / "runs" / run_id

    print("\n--- meta ---")
    for k in ("auto_hits", "protected_blocked", "protected_downgraded"):
        print(f"  {k} = {meta.get(k)}")

    print("\n--- protection.json（想动但被保护拦下） ---")
    for r in json.loads((rdir / "protection.json").read_text(encoding="utf-8")):
        print(f"  {r['path']:<28} 想动={r['intended_rule_id']:<22} 保护={r['protect_rule_id']}")

    print("\n--- pending_auto_hits.json（待隔离） ---")
    for h in json.loads((rdir / "pending_auto_hits.json").read_text(encoding="utf-8")):
        print(f"  {h['path']:<40} {h['rule_id']}")

    run("apply-rules", str(WS), run_id)
    run("analyze", str(WS), run_id, "--mock")

    failed: list[str] = []

    print("\n--- 隔离后仍在原地的东西（保护生效的证据） ---")
    for rel in (
        ".env",
        "id_rsa",
        "server.pem",
        "README.md",
        "app/.git/objects/ab12",
        "src/logs/handler.py",
        ".ssh/.cache",
        # 目录命中自动隔离，但子树里有 .npmrc → 整条被拦下
        "app/node_modules",
        "app/node_modules/.npmrc",
    ):
        ok = (WS / rel).exists()
        print(f"  {'存在' if ok else '已消失!!'}  {rel}")
        if not ok:
            failed.append(f"应留在原地却消失了: {rel}")

    print("\n--- 应该被隔离掉的东西 ---")
    for rel in ("app/__pycache__", "old.tmp", "app/logs/app.log"):
        gone = not (WS / rel).exists()
        print(f"  {'仍在!!' if not gone else '已隔离'}  {rel}")
        if not gone:
            failed.append(f"应被隔离却还在: {rel}")

    print("\n--- 保护拦下明细（protection.json） ---")
    for r in json.loads((rdir / "protection.json").read_text(encoding="utf-8")):
        print(f"  {r['path']:<28} {r.get('blocked_at', '-'):<22} 保护={r['protect_rule_id']}")
    blocked_at = {
        r.get("blocked_at")
        for r in json.loads((rdir / "protection.json").read_text(encoding="utf-8"))
    }
    if "protected_descendant" not in blocked_at:
        failed.append("protection.json 缺少 protected_descendant 记录（目录含受保护后代未留痕）")

    print(f"\n审计摘要：{rdir / 'audit.md'}")
    if not args.keep:
        shutil.rmtree(WS.parent, ignore_errors=True)
        print("已清理 .smoke/")

    if failed:
        print("\n冒烟失败：", file=sys.stderr)
        for f in failed:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("\n冒烟通过：保护生效，预期项均已隔离。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
