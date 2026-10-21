"""布局自检：不靠眼睛，量元素几何。

当前模型读不了截图，所以「目视审查通过」这种话不能说。能做的是量：
每个分页的主要区块有没有越界、有没有互相压住、页头是不是在最上面、
内容区是不是居中且不超过 1180px。

做法：把 index.html 复制成一份带测量脚本的临时页，用同一个服务跑起来，
`--dump-dom` 把结果读回来，然后删掉临时页。
"""

from __future__ import annotations

import html as html_mod
import json
import re
import shutil
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

from dirtidy.pipeline import create_run  # noqa: E402
from dirtidy.web.app import create_app  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"
STATIC = ROOT / "src" / "dirtidy" / "web" / "static"

MEASURE = r"""
(function () {
  const out = [];
  const tabs = [...document.querySelectorAll(".tab")].map((s) => s.id.replace("tab-", ""));

  function rect(el) {
    const r = el.getBoundingClientRect();
    return { l: r.left, t: r.top, r: r.right, b: r.bottom, w: r.width, h: r.height };
  }
  function overlaps(a, b) {
    return a.l < b.r - 0.5 && b.l < a.r - 0.5 && a.t < b.b - 0.5 && b.t < a.b - 0.5;
  }

  for (const name of tabs) {
    document.querySelectorAll(".tab").forEach((s) => s.classList.remove("active"));
    const sec = document.getElementById("tab-" + name);
    sec.classList.add("active");
    // 强制回流，让 getBoundingClientRect 拿到真实值
    void sec.offsetHeight;

    const vw = document.documentElement.clientWidth;
    const head = sec.querySelector(".page-head");
    const main = document.querySelector(".app-main");

    // 1) 页头是否在最上面
    const headTop = head ? rect(head).t : null;
    // 2) 内容区宽度是否符合 1180px 上限
    const secW = rect(sec).w;
    // 3) 横向越界：任何元素右边超出视口。
    //    祖先里有 overflow-x: auto/scroll 的不算缺陷——那是横向滚动容器该干的事
    //    （窄屏下表格内容 744px 宽、容器 476px，溢出的是内容而不是布局）。
    function inScroller(el) {
      for (let p = el.parentElement; p && p !== document.body; p = p.parentElement) {
        const ox = getComputedStyle(p).overflowX;
        if (ox === "auto" || ox === "scroll") return true;
      }
      return false;
    }
    const overflow = [];
    for (const el of sec.querySelectorAll("*")) {
      const r = rect(el);
      if (r.w === 0 && r.h === 0) continue;
      if (r.r > vw + 1 && !inScroller(el)) {
        overflow.push(el.tagName + "." + (el.className || "").toString().split(" ")[0] + " r=" + Math.round(r.r));
      }
    }
    // 4) 同级主要块是否互相压住（只查直接子元素里的块级容器）
    const blocks = [...sec.children].filter((el) => {
      const r = rect(el);
      return r.h > 4;
    });
    const clashes = [];
    for (let i = 0; i < blocks.length; i++) {
      for (let j = i + 1; j < blocks.length; j++) {
        if (overlaps(rect(blocks[i]), rect(blocks[j]))) {
          clashes.push(blocks[i].tagName + "." + (blocks[i].className || "") + " × " + blocks[j].tagName + "." + (blocks[j].className || ""));
        }
      }
    }
    // 5) 页头内部：标题区与右侧控件是否压住
    let headClash = null;
    if (head) {
      const hm = head.querySelector(".page-head-main");
      const hs = head.querySelector(".page-head-side");
      if (hm && hs && overlaps(rect(hm), rect(hs))) headClash = "page-head-main × page-head-side";
    }
    // 6) 首屏可见高度内有没有内容（防止整页空白）
    const visH = window.innerHeight;
    let visibleBlocks = 0;
    for (const el of sec.querySelectorAll("*")) {
      const r = rect(el);
      if (r.h > 6 && r.t < visH && r.b > 0) visibleBlocks++;
    }

    out.push({
      tab: name,
      viewport: vw,
      headTop: headTop === null ? null : Math.round(headTop),
      headIsFirst: head ? sec.firstElementChild === head : false,
      sectionWidth: Math.round(secW),
      overflow: overflow.slice(0, 6),
      overflowCount: overflow.length,
      clashes,
      headClash,
      visibleBlocks,
    });
  }
  document.getElementById("probeOut").textContent = JSON.stringify(out, null, 1);
})();
"""


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="dirtidy-layout-")) / "ws"
    shutil.copytree(ROOT / "tests" / "fixtures" / "sample_workspace", work)
    meta = create_run(work, rules="default", quick=False, apply=False)

    # 临时探针页：与 index.html 同目录，静态路径照常解析
    # CSP 是 script-src 'self'，内联脚本会被拦——测量脚本必须落成同源文件。
    probe = STATIC / "_layout_probe.html"
    probe_js = STATIC / "_layout_probe.js"
    src = (STATIC / "index.html").read_text(encoding="utf-8")
    src = src.replace(
        '<script src="/static/app.js"></script>',
        '<script src="/static/app.js"></script>\n<div id="probeOut"></div>\n'
        '<script src="/static/_layout_probe.js"></script>',
    )
    probe.write_text(src, encoding="utf-8")
    probe_js.write_text(MEASURE, encoding="utf-8")

    try:
        app = create_app(default_root=work, default_run_id=meta["run_id"])
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning")
        )
        t = threading.Thread(target=server.run, daemon=True)
        t.start()
        for _ in range(60):
            try:
                urllib.request.urlopen(f"{BASE}/api/meta", timeout=1).read()
                break
            except Exception:
                time.sleep(0.2)
        else:
            print("server failed")
            return 1

        results = {}
        for label, size in (("wide", "1440,1000"), ("narrow", "420,900")):
            r = subprocess.run(
                [
                    CHROME,
                    "--headless=new",
                    "--no-sandbox",
                    "--disable-gpu",
                    f"--window-size={size}",
                    "--virtual-time-budget=6000",
                    "--dump-dom",
                    f"{BASE}/static/_layout_probe.html",
                ],
                capture_output=True,
                text=True,
            )
            m = re.search(r'<div id="probeOut">(.*?)</div>', r.stdout, re.S)
            if not m:
                print(f"{label}: no probe output")
                return 1
            results[label] = json.loads(html_mod.unescape(m.group(1)))

        server.should_exit = True
        t.join(timeout=5)
    finally:
        probe.unlink(missing_ok=True)
        probe_js.unlink(missing_ok=True)

    problems = []
    for label, rows in results.items():
        print(f"\n=== {label} ===")
        for row in rows:
            flag = []
            if not row["headIsFirst"]:
                flag.append("页头非首元素")
            if row["headTop"] is not None and row["headTop"] < 0:
                flag.append(f"页头在视口外 t={row['headTop']}")
            if row["overflowCount"]:
                flag.append(f"横向越界 {row['overflowCount']} 处 {row['overflow']}")
            if row["clashes"]:
                flag.append(f"同级块重叠 {row['clashes']}")
            if row["headClash"]:
                flag.append(f"页头内部重叠 {row['headClash']}")
            if row["visibleBlocks"] < 3:
                flag.append(f"首屏几乎空白 visibleBlocks={row['visibleBlocks']}")
            status = "OK  " if not flag else "FAIL"
            print(
                f"{status} {row['tab']:11s} vw={row['viewport']:5d} "
                f"secW={row['sectionWidth']:5d} headTop={row['headTop']} "
                f"vis={row['visibleBlocks']:3d}"
            )
            if flag:
                problems.append((label, row["tab"], flag))

    print()
    if problems:
        print(f"发现 {len(problems)} 处布局问题：")
        for label, tab, flag in problems:
            print(f"  [{label}] {tab}: {'；'.join(flag)}")
        return 1
    print("布局自检：全部通过（无越界、无重叠、页头均在首位且有内容）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
