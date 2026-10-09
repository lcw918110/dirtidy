"""规模层回归：流式 JSON、SQLite 哈希缓存、扫尾范围、各类预算上限。

这些用例守护的是「百万文件 / T 级数据下不会因为实现方式而炸掉」：
峰值内存与规模解耦、扫尾代价与隔离条目数成正比、外发与解析都有硬上限。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from dirtidy.hashing import content as hashing_content
from dirtidy.hashing.content import find_duplicate_groups, iter_archive_members
from dirtidy.models import (
    InventoryItem,
    iter_json_array,
    select_hashes,
    write_json_stream,
)
from dirtidy.pipeline import create_run
from dirtidy.quarantine.store import sweep_empty_dirs
from dirtidy.scan.hashcache import HashCache
from dirtidy.scan.scanner import hash_cache_path, scan_tree

DAY = 86400.0


# ------------------------------------------------------------------ 流式 JSON

def test_stream_roundtrip_unicode_and_nesting(tmp_path: Path) -> None:
    rows = [
        {"path": f"目录/文件{i}.txt", "is_dir": False, "size": i, "sha256": None, "nested": {"a": [1, 2]}}
        for i in range(500)
    ]
    out = tmp_path / "inv.json"
    assert write_json_stream(out, rows) == 500

    # 输出必须是合法 JSON 数组（可用标准 json 读）
    assert json.loads(out.read_text(encoding="utf-8")) == rows
    assert list(iter_json_array(out)) == rows


def test_stream_reader_handles_chunk_boundaries(tmp_path: Path) -> None:
    """故意用极小的 chunk_size，逼出「半条记录跨块」的分支。"""
    rows = [{"path": f"p{i}", "note": "x" * (i % 17)} for i in range(200)]
    out = tmp_path / "inv.json"
    write_json_stream(out, rows)
    assert list(iter_json_array(out, chunk_size=7)) == rows


def test_stream_reader_empty_and_whitespace(tmp_path: Path) -> None:
    empty = tmp_path / "empty.json"
    write_json_stream(empty, [])
    assert list(iter_json_array(empty)) == []
    assert json.loads(empty.read_text(encoding="utf-8")) == []

    padded = tmp_path / "padded.json"
    padded.write_text("\n\n  [ \n {\"a\":1} , {\"a\":2} \n ] \n", encoding="utf-8")
    assert list(iter_json_array(padded)) == [{"a": 1}, {"a": 2}]


@pytest.mark.parametrize("bad", ["", "{\"a\":1}", "[{\"a\":1}", "not json at all"])
def test_stream_reader_rejects_bad_input(tmp_path: Path, bad: str) -> None:
    p = tmp_path / "bad.json"
    p.write_text(bad, encoding="utf-8")
    with pytest.raises(ValueError):
        list(iter_json_array(p))


def test_stream_write_does_not_materialize_list(tmp_path: Path) -> None:
    """生成器入参：即使有 5 万条，也不允许调用方先构造等长列表。"""
    seen = {"n": 0}

    def gen():
        for i in range(50_000):
            seen["n"] += 1
            yield {"path": f"p{i}"}

    out = tmp_path / "big.json"
    assert write_json_stream(out, gen()) == 50_000
    assert seen["n"] == 50_000
    assert out.stat().st_size > 0


def test_select_hashes_only_returns_wanted() -> None:
    items = [
        InventoryItem(path=f"f{i}", is_dir=False, size=1, mtime=1.0, sha256=f"h{i}")
        for i in range(100)
    ]
    assert select_hashes(items, {"f3", "f7"}) == {"f3": "h3", "f7": "h7"}
    assert select_hashes(items, set()) == {}


# ------------------------------------------------------------------ 哈希缓存

def test_hashcache_hit_and_miss_on_size_or_mtime(tmp_path: Path) -> None:
    cache = HashCache(tmp_path / "c.sqlite", "scan1")
    assert cache.open() is True
    try:
        cache.upsert_many([("a.txt", 10, 100.0, "abc")])
        assert cache.lookup_many([("a.txt", 10, 100.0)]) == {"a.txt": "abc"}
        # size 变了 → 视为未命中，必须重算
        assert cache.lookup_many([("a.txt", 11, 100.0)]) == {}
        # mtime 变了 → 同上
        assert cache.lookup_many([("a.txt", 10, 101.0)]) == {}
        assert cache.lookup_many([("missing.txt", 1, 1.0)]) == {}
    finally:
        cache.close()


def test_hashcache_prune_drops_stale_rows(tmp_path: Path) -> None:
    path = tmp_path / "c.sqlite"
    first = HashCache(path, "scan1")
    assert first.open()
    first.upsert_many([("keep.txt", 1, 1.0, "h1"), ("gone.txt", 1, 1.0, "h2")])
    first.close()

    second = HashCache(path, "scan2")
    assert second.open()
    try:
        second.upsert_many([("keep.txt", 1, 1.0, "h1")])
        assert second.prune() == 1
        assert second.stats()["entries"] == 1
    finally:
        second.close()


def test_hashcache_migrates_legacy_json(tmp_path: Path) -> None:
    legacy = tmp_path / "hash_cache.json"
    legacy.write_text(json.dumps({"a.txt": {"size": 3, "mtime": 9.0, "sha256": "old"}}), encoding="utf-8")
    cache = HashCache(tmp_path / "hash_cache.sqlite", "s")
    assert cache.open()
    try:
        assert cache.migrate_legacy(legacy) == 1
        assert cache.lookup_many([("a.txt", 3, 9.0)]) == {"a.txt": "old"}
    finally:
        cache.close()
    assert not legacy.exists(), "迁移后旧文件应改名，避免下次重复导入"
    assert (tmp_path / "hash_cache.json.migrated").exists()


def test_hashcache_open_failure_is_silent(tmp_path: Path) -> None:
    """缓存路径不可用时返回 False，由调用方降级，而不是把整次扫描炸掉。"""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    cache = HashCache(blocked, "s")  # 路径是个目录 → sqlite 打不开
    assert cache.open() is False
    assert cache.usable is False
    assert cache.lookup_many([("a", 1, 1.0)]) == {}
    assert cache.upsert_many([("a", 1, 1.0, "h")]) == 0
    assert cache.stats() == {"entries": 0, "bytes": 0}
    cache.close()


def test_scan_uses_sqlite_cache_on_second_pass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    for i in range(20):
        (root / f"f{i}.txt").write_text(f"content-{i}", encoding="utf-8")

    calls = {"n": 0}
    real = hashing_content.hashlib.sha256  # noqa: F841  (占位，避免误改模块常量)

    from dirtidy.scan import scanner

    original = scanner.file_sha256

    def counting(path: Path, chunk_size: int = 1024 * 1024) -> str:
        calls["n"] += 1
        return original(path, chunk_size)

    monkeypatch.setattr(scanner, "file_sha256", counting)
    scan_tree(root, quick=False)
    first = calls["n"]
    assert first == 20
    calls["n"] = 0
    scan_tree(root, quick=False)
    assert calls["n"] == 0, "第二轮应全部命中 SQLite 缓存"
    assert hash_cache_path(root).name == "hash_cache.sqlite"
    assert hash_cache_path(root).exists()


def test_progress_callback_is_throttled(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    for i in range(400):
        (root / f"f{i}.txt").write_text("x", encoding="utf-8")

    seen: list[tuple[int, int]] = []
    scan_tree(root, quick=False, use_hash_cache=False, on_progress=lambda d, t: seen.append((d, t)))

    assert seen, "应当至少上报一次进度"
    assert len(seen) <= 101, f"进度回调未节流，被调用 {len(seen)} 次"
    assert seen[-1] == (400, 400), "最后一次必须是完成态"


# ------------------------------------------------------------------ 扫尾范围

def test_sweep_only_touches_ancestors_of_quarantined(tmp_path: Path) -> None:
    """扫尾只处理「被隔离条目的祖先」，不碰无关目录。"""
    root = tmp_path / "ws"
    nested = root / "tree" / "inner"
    nested.mkdir(parents=True)
    (nested / "x.txt").write_text("data", encoding="utf-8")
    unrelated = root / "unrelated_empty"
    unrelated.mkdir()

    meta = create_run(root, rules="default", quick=True, apply=False)
    from dirtidy.models import RuleHit, quarantine_root, run_dir

    rdir = run_dir(root, meta["run_id"])
    manifest = rdir / "quarantine_manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    qdir = quarantine_root(root, meta["run_id"])
    from dirtidy.quarantine.store import quarantine_hits

    quarantine_hits(
        root,
        meta["run_id"],
        [RuleHit(path="tree/inner/x.txt", rule_id="t", action="auto_quarantine", reason="t")],
        qdir,
        manifest,
    )
    entries = sweep_empty_dirs(root, meta["run_id"], qdir, manifest)

    swept = {e.original_path for e in entries}
    assert "tree/inner" in swept and "tree" in swept
    assert "unrelated_empty" not in swept, "无关空目录不应被扫尾顺手搬走"
    assert unrelated.exists()


def test_sweep_respects_protection(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    protected = root / "keepme" / "inner"
    protected.mkdir(parents=True)
    (protected / "x.txt").write_text("data", encoding="utf-8")

    from dirtidy.models import RuleHit, quarantine_root, run_dir
    from dirtidy.quarantine.store import quarantine_hits

    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    manifest = rdir / "quarantine_manifest.json"
    manifest.write_text("[]", encoding="utf-8")
    qdir = quarantine_root(root, meta["run_id"])
    quarantine_hits(
        root,
        meta["run_id"],
        [RuleHit(path="keepme/inner/x.txt", rule_id="t", action="auto_quarantine", reason="t")],
        qdir,
        manifest,
    )
    entries = sweep_empty_dirs(
        root, meta["run_id"], qdir, manifest, is_protected=lambda rel: rel.startswith("keepme")
    )
    # 返回的是完整清单；关键是被保护目录没有被追加进去
    assert {e.original_path for e in entries} == {"keepme/inner/x.txt"}
    assert protected.exists(), "受保护目录不应被扫尾搬走"
    assert (root / "keepme").exists()


# ------------------------------------------------------------------ 预算上限

def test_duplicate_group_paths_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hashing_content, "MAX_DUPLICATE_PATHS", 5)
    items = [
        InventoryItem(path=f"f{i}", is_dir=False, size=1, mtime=1.0, sha256="same")
        for i in range(50)
    ]
    groups = find_duplicate_groups(items)
    assert len(groups) == 1
    assert len(groups[0].paths) == 5
    assert groups[0].meta == {"sha256": "same", "count": 50, "truncated": True}


def test_archive_member_budget_stops_early(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import zipfile

    archive = tmp_path / "big.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for i in range(50):
            zf.writestr(f"m{i}.txt", f"payload-{i}")

    members, err = iter_archive_members(archive, max_members=5)
    assert err is not None and "预算" in err
    assert len(members) == 5

    members, err = iter_archive_members(archive, max_bytes=10)
    assert err is not None and "预算" in err


def test_archive_file_size_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hashing_content, "MAX_ARCHIVE_FILE_BYTES", 10)
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.zip").write_bytes(b"PK\x03\x04" + b"x" * 100)
    items = [InventoryItem(path="a.zip", is_dir=False, size=104, mtime=1.0, suffix=".zip")]
    out = hashing_content.find_extracted_archives(root, items)
    assert out and out[0].rule_id == "archive_too_large"


def test_find_extracted_archives_skips_hash_set_when_no_archives(tmp_path: Path) -> None:
    items = [InventoryItem(path="a.txt", is_dir=False, size=1, mtime=1.0, suffix=".txt", sha256="h")]
    assert hashing_content.find_extracted_archives(tmp_path, items) == []


def test_analyze_respects_max_targets(tmp_path: Path) -> None:
    from dirtidy.ai.providers import analyze_run
    from dirtidy.models import read_json, run_dir

    root = tmp_path / "ws"
    root.mkdir()
    for i in range(20):
        (root / f"f{i}.txt").write_text(f"unique-{i}", encoding="utf-8")

    cfg = tmp_path / "models.yaml"
    cfg.write_text(
        "provider: mock\nsummary:\n  max_targets: 5\n  max_depth: 3\n",
        encoding="utf-8",
    )
    meta = create_run(root, rules="default", quick=True, apply=False)
    rdir = run_dir(root, meta["run_id"])
    results = analyze_run(root, rdir, config_path=cfg)
    assert len(results) == 5

    ai_meta = read_json(rdir / "ai_meta.json")
    assert ai_meta["max_targets"] == 5
    assert ai_meta["truncated"] is True


def test_inventory_write_is_streaming_and_readable(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    (root / "keep").mkdir(parents=True)
    (root / "keep" / "a.txt").write_text("x", encoding="utf-8")
    old = time.time() - 10 * DAY
    (root / "old.log").write_text("y", encoding="utf-8")
    os.utime(root / "old.log", (old, old))

    meta = create_run(root, rules="default", quick=True, apply=False)
    from dirtidy.models import run_dir

    inv = run_dir(root, meta["run_id"]) / "inventory.json"
    rows = list(iter_json_array(inv))
    assert {r["path"] for r in rows} >= {"keep", "keep/a.txt", "old.log"}
    assert json.loads(inv.read_text(encoding="utf-8")) == rows
