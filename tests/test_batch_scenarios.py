"""批量决策的场景测试：真搬文件，不看源码字符串。

这里的用例对应网页「审阅 → 批量设决策 → 执行」的实际后果。它们和
`test_convenience.py` 里的静态守卫是两层：那边保证前端没写错字，这边保证
后端真的按用户看到的意思搬，且搬不动的必须报出来而不是静默消失。
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

import pytest

from dirtidy.decision.store import apply_decisions, save_decisions
from dirtidy.models import Decision, quarantine_root, read_json, run_dir
from dirtidy.pipeline import create_run

FIXTURE_SRC = Path(__file__).parent / "fixtures" / "sample_workspace"
OLD_MTIME = time.time() - 30 * 86400


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    dest = tmp_path / "ws"
    shutil.copytree(FIXTURE_SRC, dest)
    for p in dest.rglob("*.log"):
        os.utime(p, (OLD_MTIME, OLD_MTIME))
    return dest


def _run(workspace: Path, *, quick: bool = True) -> tuple[str, Path]:
    meta = create_run(workspace, rules="default", quick=quick, apply=False)
    rdir = run_dir(workspace, meta["run_id"])
    return meta["run_id"], rdir


def _apply(workspace: Path, run_id: str, rdir: Path, decisions: list[Decision]) -> dict:
    save_decisions(rdir, decisions)
    return apply_decisions(
        workspace, run_id, rdir, quarantine_root(workspace, run_id)
    )


def test_batch_archive_keeps_origin_relative_layout(workspace: Path) -> None:
    """批量归档按「archive/原相对路径」落位，同名不同目录不会互相顶掉。

    这是网页批量归档应有的默认行为：用户在候选表里一次勾十项，其中三个叫
    notes.txt。若目标只按文件名拼，后两个会被判「目标已存在」而跳过——用户
    看到勾了十项、实际只归档七项，还以为都成了。

    这里刻意不用 README.md：它命中 `protect_project_markers`，被拦下才是对的。
    """
    (workspace / "a").mkdir()
    (workspace / "b").mkdir()
    for rel in ("a/notes.txt", "b/notes.txt", "keep/other.txt"):
        (workspace / rel).parent.mkdir(parents=True, exist_ok=True)
        (workspace / rel).write_text("x", encoding="utf-8")

    run_id, rdir = _run(workspace)
    picks = ["a/notes.txt", "b/notes.txt", "keep/other.txt"]
    result = _apply(
        workspace,
        run_id,
        rdir,
        [Decision(path=p, action="archive", target_relpath=f"archive/{p}") for p in picks],
    )

    assert result["archived"] == 3
    assert result["skipped"] == []
    for p in picks:
        assert not (workspace / p).exists()
        assert (workspace / "archive" / p).is_file()

    # 挪走之后要能查账：归档不写清单的话，进程一退就没人知道文件去哪了。
    manifest = read_json(rdir / "archive_manifest.json")
    moved = {e["original_path"]: e["target_relpath"] for e in manifest}
    assert moved == {p: f"archive/{p}" for p in picks}


def test_archive_of_protected_path_is_refused(workspace: Path) -> None:
    """受保护路径不能被 `archive` 绕过——归档不是删除，但同样会让文件离开原位。

    `.env` 命中 protect_secrets。规则路径永远不会碰它；人工决策路径如果也不检查，
    「批量归档」就成了绕过保护清单的后门：勾一批候选时顺手把 .env 挪进 archive/，
    程序还报告成功。
    """
    (workspace / ".env").write_text("SECRET=1\n", encoding="utf-8")
    run_id, rdir = _run(workspace)

    result = _apply(
        workspace,
        run_id,
        rdir,
        [Decision(path=".env", action="archive", target_relpath="archive/.env")],
    )

    assert (workspace / ".env").is_file(), "受保护文件被归档走了"
    assert not (workspace / "archive" / ".env").exists()
    assert result["archived"] == 0
    assert result["blocked_by_protect"] == 1
    assert any(s["path"] == ".env" for s in result["skipped"])


def test_unknown_action_is_reported_not_swallowed(workspace: Path) -> None:
    """动作值不认识时必须报出来，不能既不执行也不计数。

    决策来自 HTTP body 或手改的 decisions.json，写错一个字母（`quarantin`）时
    当前实现直接从 if 链里掉出去：文件没动、archived/quarantined/skipped 都没有
    它。用户以为执行成功，实际那一项被吞了。
    """
    run_id, rdir = _run(workspace)

    result = _apply(
        workspace,
        run_id,
        rdir,
        [Decision(path="zero.txt", action="quarantin")],
    )

    assert result["quarantined"] == 0
    assert result["archived"] == 0
    assert any(
        s["path"] == "zero.txt" and "quarantin" in s["reason"] for s in result["skipped"]
    ), f"未知动作没有进 skipped：{result['skipped']}"
    assert (workspace / "zero.txt").exists()


def test_archive_parent_with_child_quarantine_counts_honestly(workspace: Path) -> None:
    """父目录归档 + 子路径隔离：报出来的隔离数必须等于真正搬走的数。

    归档在循环里立刻执行，隔离排在循环之后。父目录被搬走后，子路径的源已经不存在，
    `quarantine_hits` 会跳过它，但返回的计数是按「进入隔离清单的条数」算的，于是
    报 1 项已隔离、磁盘上却找不到那一项。
    """
    (workspace / "bundle").mkdir()
    (workspace / "bundle" / "inner.txt").write_text("i", encoding="utf-8")
    run_id, rdir = _run(workspace)

    result = _apply(
        workspace,
        run_id,
        rdir,
        [
            Decision(path="bundle", action="archive", target_relpath="archive/bundle"),
            Decision(path="bundle/inner.txt", action="quarantine"),
        ],
    )

    assert result["archived"] == 1
    assert result["quarantined"] == 0, "子路径已被父目录带走，不该再算一次已隔离"
    assert any(s["path"] == "bundle/inner.txt" for s in result["skipped"]), (
        f"被父目录带走的子路径没有报告：{result['skipped']}"
    )


def test_duplicate_group_keep_one_moves_the_rest(workspace: Path) -> None:
    """重复组「留一个、其余隔离」在磁盘上的真实结果。"""
    run_id, rdir = _run(workspace, quick=False)
    dup_paths = ["dup_a.txt", "sub/dup_b.txt"]
    for p in dup_paths:
        assert (workspace / p).is_file()

    result = _apply(
        workspace,
        run_id,
        rdir,
        [Decision(path="dup_a.txt", action="keep"), Decision(path="sub/dup_b.txt", action="quarantine")],
    )

    assert result["kept"] == 1
    assert result["quarantined"] == 1
    assert (workspace / "dup_a.txt").is_file()
    assert not (workspace / "sub/dup_b.txt").exists()
    manifest = read_json(rdir / "quarantine_manifest.json")
    assert any(e["original_path"] == "sub/dup_b.txt" for e in manifest)


def test_batch_quarantine_leaves_unmarked_files_alone(workspace: Path) -> None:
    """批量隔离只动被标记的那些，没勾的一个字节都不碰。

    批量功能的全部风险都在这条上：作用范围一旦从「已选项」漂到「筛选结果」或
    「全部候选」，用户会丢掉没打算动的文件。
    """
    run_id, rdir = _run(workspace, quick=False)
    marked = ["zero.txt", "payload.txt"]
    untouched = ["dup_a.txt", "sub/dup_b.txt", "keep/readme.md"]

    result = _apply(
        workspace,
        run_id,
        rdir,
        [Decision(path=p, action="quarantine") for p in marked],
    )

    assert result["quarantined"] == len(marked)
    for p in marked:
        assert not (workspace / p).exists()
    for p in untouched:
        assert (workspace / p).is_file(), f"{p} 没被勾选却被搬走了"
