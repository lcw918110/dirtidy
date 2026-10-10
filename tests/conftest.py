"""把 git 存不下的 fixture 内容在本地补齐。

`tests/fixtures/sample_workspace` 是测试用的「目标目录」，其中有两样东西
没法随仓库走：

- `__pycache__/mod.pyc` —— 被 `.gitignore` 的 `__pycache__/` 规则屏蔽。
  那条规则本身是对的，不该为了一个夹具去放开全局忽略。
- `empty_dir/` —— 空目录，git 压根不能跟踪。

后果非常隐蔽：开发机磁盘上恰好留着这两样，于是本地全绿；CI 全新克隆没有，
所有依赖它们的用例一起红。第一次把「直删」推上去时，6 个 job 就是这么挂的。

所以在这里显式补齐，而不是靠「开发者本机碰巧有」。
`tests/test_docs_consistency.py::test_fixture_workspace_survives_fresh_clone`
守着这件事，缺了会在本地就红，不必等到 CI。
"""

from __future__ import annotations

from pathlib import Path

import pytest

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "sample_workspace"

# git 存不下的 fixture 内容：相对路径 → 字节内容；值为 None 表示空目录。
# 内容与仓库里一直以来的保持一致（mod.pyc 就是 b"y\n"）。
UNTRACKED_FIXTURE_ENTRIES: dict[str, bytes | None] = {
    "__pycache__/mod.pyc": b"y\n",
    "empty_dir": None,
}


def materialize_sample_workspace(root: Path = FIXTURE_SRC) -> list[str]:
    """幂等补齐 git 存不下的内容，返回补了哪些相对路径。"""
    done: list[str] = []
    for rel, data in UNTRACKED_FIXTURE_ENTRIES.items():
        target = root / rel
        if data is None:
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)  # 每次都写：防止被别的工具改坏后静默带过
        done.append(rel)
    return done


@pytest.fixture(scope="session", autouse=True)
def _materialize_sample_workspace() -> None:
    """任何用例开跑前，先把夹具补齐（会话级、自动生效）。"""
    materialize_sample_workspace()
