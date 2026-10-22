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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _extreme_workspace import build_extreme_workspace  # noqa: E402

from dirtidy.pipeline import create_run  # noqa: E402
from dirtidy.web.app import create_app  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 8793
BASE = f"http://127.0.0.1:{PORT}"
STATIC = ROOT / "src" / "dirtidy" / "web" / "static"

MEASURE = r"""
(async function () {
  const out = [];
  const tabs = [...document.querySelectorAll(".tab")].map((s) => s.id.replace("tab-", ""));

  // 关键：必须等 boot() 把 run 真正载入、表格渲染完再量。
  // 否则量到的是**空壳页面**——表格里 0 行，布局当然「全部通过」，
  // 换什么样本都是一样的结果（这正是第一版脚本一直假绿的原因）。
  // 判据用「页面出现表格行」，而不是固定等待时长。
  for (let i = 0; i < 100; i++) {
    if (document.querySelectorAll("#tab-review tbody tr").length > 0) break;
    await new Promise((r) => setTimeout(r, 100));
  }

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
    // 注意：这里**不能**叫 name——外层 for (const name of tabs) 已经占了这个名字，
    // 块内再来 const name 会触发 TDZ，把整个测量脚本打成 ReferenceError，
    // 探针输出恒为空（这个坑踩过一次：脚本一直在报「找不到 probe 输出」，
    // 却被误当成环境问题）。
    const elName = (el) => el.tagName + "." + (el.className || "");
    for (let i = 0; i < blocks.length; i++) {
      for (let j = i + 1; j < blocks.length; j++) {
        if (overlaps(rect(blocks[i]), rect(blocks[j]))) {
          clashes.push(elName(blocks[i]) + " × " + elName(blocks[j]));
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
    // 7) 文字被挤成竖排单字：单元格/段落宽度小于一个字身时，
    //    中文会一个一个字往下掉。这在「超长路径 + 窄表格」下才会现形，
    //    示例数据（路径都很短）永远看不到。
    const squeezed = [];
    for (const el of sec.querySelectorAll("td, th, .path, .cell, p, span, code, a")) {
      if (!el.textContent || !el.textContent.trim()) continue;
      const r = rect(el);
      if (r.w === 0 || r.h === 0) continue;
      const fs = parseFloat(getComputedStyle(el).fontSize) || 14;
      // 一个字身的宽度；低于它就意味着中文必然竖排
      if (r.w < fs * 1.05 && el.getBoundingClientRect().height > fs * 2.2) {
        squeezed.push(el.tagName + "." + (el.className || "").toString().split(" ")[0] +
                     " w=" + Math.round(r.w) + " text=" + el.textContent.trim().slice(0, 12));
      }
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
      rowCount: sec.querySelectorAll("tbody tr").length,
      squeezed: squeezed.slice(0, 6),
      squeezedCount: squeezed.length,
    });
  }
  document.getElementById("probeOut").textContent = JSON.stringify(out, null, 1);
})();
"""


def main() -> int:
    # --extreme 换成「长路径 / 中文名 / 多条目」的样本库。
    # 默认用示例库——它只有 9 个文件、路径都很短，
    # 「文字被挤成竖排单字」「表格被撑破」这类问题在它上面永远量不出来。
    extreme = "--extreme" in sys.argv
    work = Path(tempfile.mkdtemp(prefix="dirtidy-layout-")) / "ws"
    if extreme:
        build_extreme_workspace(work)
    else:
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
            if row["squeezedCount"]:
                flag.append(f"文字被挤窄 {row['squeezedCount']} 处 {row['squeezed']}")
            # 审阅页必须有行：它一行都没有就说明量的是空壳，
            # 「布局全部通过」是假绿。其它页允许空（隔离区/监视本来就可能是空的）。
            if row["tab"] == "review" and row["rowCount"] == 0:
                flag.append("审阅页表格 0 行——量到的是空壳，结论不算数")
            status = "OK  " if not flag else "FAIL"
            print(
                f"{status} {row['tab']:11s} vw={row['viewport']:5d} "
                f"secW={row['sectionWidth']:5d} headTop={row['headTop']} "
                f"vis={row['visibleBlocks']:3d} rows={row['rowCount']:3d}"
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
