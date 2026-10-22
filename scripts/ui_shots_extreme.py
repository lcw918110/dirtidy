"""用「极端数据」样本跑一遍界面并截图。

为什么单独一个脚本：`ui_shots.py` 用的是 `tests/fixtures/sample_workspace`，
只有 9 个文件、路径都很短。目视审查要覆盖的是**真实数据量级下才会露馅**的形态：
超长路径、中文文件名、多层嵌套、大量同类条目。

这些形态在示例数据下永远看不到——静态检查也看不出来（DOM 里都是合法的字符串），
只有把图看下来才知道有没有被挤成竖排单字、有没有撑破表格。

用法：
    ./.venv/bin/python scripts/ui_shots_extreme.py
产出：
    outputs/ui-extreme-20261022/<page>.png
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _extreme_workspace import build_extreme_workspace  # noqa: E402

from dirtidy.pipeline import create_run  # noqa: E402
from dirtidy.web.app import create_app  # noqa: E402

OUT = ROOT / "outputs" / "ui-extreme-20261022"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="dirtidy-extreme-")) / "ws"
    build_extreme_workspace(work)

    meta = create_run(work, rules="default", quick=False, apply=False)
    run_id = meta["run_id"]
    print(f"run_id={run_id}  root={work}")

    app = create_app(default_root=work, default_run_id=run_id)
    config = uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
    server = uvicorn.Server(config)

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
        r = subprocess.run(
            [
                CHROME,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--hide-scrollbars",
                "--window-size=1440,1100",
                "--virtual-time-budget=8000",
                f"--screenshot={png}",
                f"{BASE}/#{tab}",
            ],
            capture_output=True,
            text=True,
        )
        ok = png.exists() and png.stat().st_size > 0
        print(f"{tab:11s} screenshot={'OK' if ok else 'FAIL'} rc={r.returncode}")
        if not ok:
            rc = 1
            print(r.stderr[-500:])

    # 截图前先确认页面**真的渲染出了数据**。
    # 否则截出来的是空壳页：图片合法、rc=0，但审阅页一个候选都没有，
    # 这种图拿去「目视审查」等于什么都没看。审阅页必须有行才算数。
    dom = subprocess.run(
        [
            CHROME,
            "--headless=new",
            "--no-sandbox",
            "--disable-gpu",
            "--virtual-time-budget=8000",
            "--dump-dom",
            f"{BASE}/#review",
        ],
        capture_output=True,
        text=True,
    ).stdout
    m = re.search(r'id="rows"[^>]*>(.*?)</tbody>', dom, re.S)
    rows = m.group(1).count("<tr") if m else 0
    longest = 0
    # 注意别用 t 当循环变量——上面 t 是服务线程，循环一覆盖它，
    # 结尾的 t.join(...) 就变成 str.join()，报错还很难看出是哪来的。
    for cell in re.findall(r"<td[^>]*>(.*?)</td>", m.group(1) if m else "", re.S):
        longest = max(longest, len(re.sub(r"<[^>]+>", "", cell)))
    print(f"review 表格行数={rows} 最长单元格字符数={longest}")
    if rows == 0:
        print("❌ 审阅页表格 0 行——截到的是空壳，本次截图不算数")
        rc = 1
    elif longest < 100:
        print(f"⚠️  最长单元格只有 {longest} 字符，超长路径没进表格，样本没压到")
        rc = 1

    # 窄屏也截一份：极端路径在窄屏下更容易把布局撑破
    for tab in ("review", "execute", "overview"):
        png = OUT / f"narrow-{tab}.png"
        if png.exists():
            png.unlink()
        subprocess.run(
            [
                CHROME,
                "--headless=new",
                "--no-sandbox",
                "--disable-gpu",
                "--hide-scrollbars",
                "--window-size=500,1000",
                "--virtual-time-budget=8000",
                f"--screenshot={png}",
                f"{BASE}/#{tab}",
            ],
            capture_output=True,
            text=True,
        )
        print(f"narrow-{tab:6s} screenshot={'OK' if png.exists() else 'FAIL'}")

    server.should_exit = True
    t.join(timeout=5)
    print(json.dumps({"run_id": run_id, "root": str(work)}, ensure_ascii=False))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
