"""起服务 + 截图 + dump-dom，一次性跑完。

服务在命令结束时会被回收，所以「起服务」与「截图」必须在同一个进程里。
先真扫一次（产生 run），再逐页截图；同时用 --dump-dom 把渲染后的文案抓下来，
因为当前模型不支持读图，布局只能靠 DOM 与文本核对，截图留作人工复核。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from dirtidy.pipeline import create_run  # noqa: E402
from dirtidy.web.app import create_app  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sample_workspace"
OUT = ROOT / "outputs" / "ui-integration-20261021"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 8791
BASE = f"http://127.0.0.1:{PORT}"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="dirtidy-shot-")) / "ws"
    shutil.copytree(FIXTURE, work)

    meta = create_run(work, rules="default", quick=False, apply=False)
    run_id = meta["run_id"]
    print(f"run_id={run_id}")

    app = create_app(default_root=work, default_run_id=run_id)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    import threading

    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(60):
        try:
            urllib.request.urlopen(f"{BASE}/api/meta", timeout=1).read()
            break
        except Exception:
            time.sleep(0.2)
    else:
        print("server failed to start")
        return 1

    tabs = ["overview", "scan", "review", "execute", "quarantine", "rules", "settings", "watch"]
    rc = 0
    for tab in tabs:
        png = OUT / f"{tab}.png"
        if png.exists():
            png.unlink()
        url = f"{BASE}/#{tab}"
        r = subprocess.run(
            [
                CHROME,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--hide-scrollbars",
                "--window-size=1440,1000",
                "--virtual-time-budget=4000",
                f"--screenshot={png}",
                url,
            ],
            capture_output=True,
            text=True,
        )
        ok = png.exists() and png.stat().st_size > 0
        print(f"{tab:11s} screenshot={'OK' if ok else 'FAIL'} rc={r.returncode}")
        if not ok:
            rc = 1
            print(r.stderr[-500:])

    # dump-dom：核验渲染后的真实文案（模型读不了图，这是唯一能自动核的途径）
    dom = OUT / "render-dump.html"
    if dom.exists():
        dom.unlink()
    r = subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--virtual-time-budget=5000",
            "--dump-dom",
            f"{BASE}/#execute",
        ],
        capture_output=True,
        text=True,
    )
    if r.returncode == 0 and r.stdout:
        dom.write_text(r.stdout, encoding="utf-8")
        print(f"dump-dom OK -> {dom} ({len(r.stdout)} chars)")
    else:
        rc = 1
        print("dump-dom FAIL", r.stderr[-400:])

    server.should_exit = True
    t.join(timeout=5)
    print(json.dumps({"run_id": run_id, "root": str(work)}, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
