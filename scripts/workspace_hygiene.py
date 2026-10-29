#!/usr/bin/env python3
"""工作区卫生检查：找出开发期掉在仓库根目录的垃圾文件。

为什么需要它（2026-10-29）：
    CPython 的 `tempfile` 在选临时目录时会逐个候选目录写一个探测文件（8 字符
    随机名、权限 0600），写完立刻删掉；沙箱的安全策略把这次 unlink 拦了下来，
    文件就留在**每个候选目录**里——最后一个是当前工作目录。除此之外沙箱自己
    还会在 TMPDIR 下留 `codebuddy-toybox.*` 目录，同样因为删不掉而堆积。
    这类文件不受 .gitignore 约束，也不会自己消失。攒两天就有十几个混进
    `git status`，把真正该看的改动盖住；更糟的是它们看起来像「莫名多出来的
    文件」，每次都要重新判断一遍是不是自己弄坏的。

    它们来自执行环境而不是项目代码：仓库源码、虚拟环境依赖、注入的
    sitecustomize 里都没有写入 `blat` 的代码，普通写操作（touch、python 写文件、
    正常跑测试）都不会产生。所以这里做的是**检测与归位**，不是去改业务代码。

用法：
    python scripts/workspace_hygiene.py            # 只报告，发现垃圾退出码 1
    python scripts/workspace_hygiene.py --clean    # 移入废纸篓
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from pathlib import Path

# 探测文件的指纹：三者同时成立才认定，避免误伤普通的短文件名。
# 内容有两种：`blat`（沙箱写入的哨兵）与空文件（CPython tempfile 的写权限探测）。
NAME_RE = re.compile(r"^[a-z0-9_]{8}$")
MARKERS = (b"blat", b"")
MAX_SIZE = 64

# 仓库里允许长期存在的本地目录（已被 .gitignore 覆盖），仅作提示，不参与清理。
# 注意不要放 `outputs`：那里既有入库的项目资产，也有临时产出的交付物，
# 后者需要人工决定去留，跳过就等于把它藏起来。
KNOWN_LOCAL = (".dirtidy", ".bench-tmp", "build", ".DS_Store")


def _git_ls(root: Path, name: str) -> bool:
    """该名字是否被 git 跟踪；跟踪的文件一律不碰。"""
    try:
        out = subprocess.run(
            ["git", "ls-files", "--", name],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return bool(out.stdout.strip())


def find_residue(root: Path) -> list[Path]:
    """按指纹找出环境探测残留：8 字符名 + 0600 + ≤64 字节 + 内容 blat + 未跟踪。"""
    hits: list[Path] = []
    for path in sorted(root.iterdir()):
        if path.is_symlink() or not path.is_file():
            continue
        if not NAME_RE.match(path.name):
            continue
        try:
            st = path.stat()
            if st.st_size > MAX_SIZE:
                continue
            if st.st_mode & 0o077:  # 只认 0600：普通文件通常 0644
                continue
            if path.read_bytes().strip() not in MARKERS:
                continue
        except OSError:
            continue
        if _git_ls(root, path.name):
            continue
        hits.append(path)
    return hits


def find_untracked(root: Path, skip: set[str] | None = None) -> list[str]:
    """未跟踪、也未被忽略的文件，供人工判断（不自动处理）。"""
    skip = skip or set()
    try:
        out = subprocess.run(
            # core.quotepath=false：否则中文路径会被转成 \344\275... 看不懂
            ["git", "-c", "core.quotepath=false", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names: list[str] = []
    for line in out.stdout.splitlines():
        if not line.startswith("?? "):
            continue
        name = line[3:].strip().strip('"')
        if name in skip or name.startswith(tuple(KNOWN_LOCAL)):
            continue
        names.append(name)
    return names


def _trash(path: Path) -> Path:
    """移入废纸篓（可还原），而不是直接删。"""
    trash = Path.home() / ".Trash"
    trash.mkdir(parents=True, exist_ok=True)
    dest = trash / f"{path.name}-{path.stat().st_mtime_ns}"
    while dest.exists():
        dest = dest.with_name(dest.name + "-x")
    shutil.move(str(path), str(dest))
    return dest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="检查仓库根目录的环境残留文件")
    ap.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="仓库根目录（默认：本脚本上一级）",
    )
    ap.add_argument("--clean", action="store_true", help="把命中的残留移入废纸篓")
    ap.add_argument("--quiet", action="store_true", help="没有残留时不输出")
    args = ap.parse_args(argv)

    root: Path = args.root.resolve()
    if not root.is_dir():
        print(f"目录不存在：{root}", file=sys.stderr)
        return 2

    residue = find_residue(root)
    if residue:
        print(f"发现 {len(residue)} 个环境探测残留（8 字符名 / 0600 / 内容 blat）：")
        for path in residue:
            print(f"  {path.name}")
        if args.clean:
            for path in residue:
                dest = _trash(path)
                print(f"  已移入废纸篓：{dest}")
        else:
            print("  未做处理；确认后加 --clean 移入废纸篓")
    elif not args.quiet:
        print("根目录没有环境探测残留。")

    others = find_untracked(root, skip={p.name for p in residue})
    if others and not args.quiet:
        print(f"\n另有 {len(others)} 个未跟踪文件（需人工判断，脚本不动它们）：")
        for name in others:
            print(f"  {name}")

    return 1 if residue and not args.clean else 0


if __name__ == "__main__":
    raise SystemExit(main())
