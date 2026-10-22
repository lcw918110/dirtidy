"""造一份「极端数据」样本库，供截图与布局审计共用。

为什么需要它：`ui_shots.py` 用的是 `tests/fixtures/sample_workspace`，
只有 9 个文件、路径都很短，扫出来只有 4 条命中 + 4 条候选。
真实数据量级下才会露馅的形态是：**表格里几十行、每行路径都很长、名字是中文**。

这些形态在示例数据下永远看不到——静态检查也看不出来（DOM 里都是合法的字符串），
只有把图看下来、或把几何量出来才知道有没有被挤成竖排单字、有没有撑破表格。

**样本必须真的能灌满表格**。踩过的两个坑都记在这里：

1. 第一版把文件都放在 `node_modules` 下面 → 整棵树被一条 `dep_node_modules`
   吃掉，只剩 1 条命中、0 条候选，表格是空的，测什么等于没测。
2. 第二版把构建目录命名成 `build1..build12` → 规则 `build_dir_candidate`
   匹配的是**精确目录名** `build`，`build1` 匹配不上，一个都没命中。

现在的形态（每类都实测过命中数）：

  - `第N批/build/构建产物/子目录N/`（12 个）：命中 `build_dir_candidate` → 候选表 12 行
  - `打包结果N.zip`（11 个）+ 一个 >200 字符超长路径的 zip：命中 `archive_suffix`
  - `日志归档/第N批/更深的目录名/运行记录N号.log`（12 个）：命中 `log_suffix` → 命中表 12 行
    （`log_suffix` 带 `min_age_days: 1`，正在写的日志不动，所以必须把 mtime 往前推）

放在独立模块里，是为了让 `ui_shots_extreme.py`（出图）与
`ui_layout_audit.py`（量几何）用**同一份**样本——两边数据不同就没法对照。
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def build_extreme_workspace(base: Path) -> None:
    """造一份「行数多、路径长、名字中文」的样本。"""
    base.mkdir(parents=True, exist_ok=True)

    # 1) 候选表：12 个构建产物目录。
    #    目录名必须**精确**是 build——`build_dir_candidate` 匹配的是精确名，
    #    写成 build1 / build_1 都匹配不上。
    for i in range(1, 13):
        d = base / f"第{i}批" / "build" / "构建产物" / f"子目录{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "编译输出.js").write_text(f"out {i}\n", encoding="utf-8")

    # 2) 候选表：11 个压缩包 + 1 个 >200 字符超长路径的压缩包
    for i in range(1, 12):
        (base / f"打包结果{i}.zip").write_bytes(b"PK\x03\x04" + bytes(str(i), "utf-8") * 8)
    deep = base
    for chunk in ("a" * 50, "b" * 50, "c" * 50):
        deep = deep / chunk
    deep.mkdir(parents=True, exist_ok=True)
    (deep / f"{'d' * 60}.zip").write_bytes(b"PK\x03\x04" + b"z" * 16)

    # 3) 命中表：12 个中文名日志文件，散在不同深度的目录下。
    #    `log_suffix` 带 `min_age_days: 1`（正在写的日志不动），
    #    新造的文件 mtime 是「现在」，所以必须把 mtime 往前推，
    #    否则一条都命中不了，命中表会是空的。
    old = time.time() - 30 * 86400
    for i in range(1, 13):
        d = base / "日志归档" / f"第{i}批" / "更深的目录名"
        d.mkdir(parents=True, exist_ok=True)
        f = d / f"运行记录第{i}号.log"
        f.write_text(f"log {i}\n", encoding="utf-8")
        os.utime(f, (old, old))

    # 4) 一份普通源码树，用来验证「不是所有东西都是垃圾」
    src = base / "src" / "组件"
    src.mkdir(parents=True, exist_ok=True)
    (src / "主要功能.ts").write_text("export const x = 1;\n", encoding="utf-8")
