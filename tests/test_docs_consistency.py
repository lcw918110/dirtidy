"""文档一致性守护：docs/04 规则 ID 表 vs 包内置规则包 YAML、docs/03 CLI 表 vs argparse、
docs/05 用例表 vs 实际测试函数。

在文档与实现漂移时，此测试应真正失败（真红），而非恒真。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
DOCS_03 = DOCS / "03-架构设计.md"
DOCS_04 = DOCS / "04-规则包说明.md"
DOCS_05 = DOCS / "05-测试与验收.md"
DOCS_07 = DOCS / "07-规模与性能.md"
TESTS_DIR = REPO_ROOT / "tests"
PACKS_DIR = REPO_ROOT / "src" / "dirtidy" / "rules" / "packs"
DEFAULT_YAML = PACKS_DIR / "default.yaml"
SERVER_WORKSPACE_YAML = PACKS_DIR / "server_workspace.yaml"

# 文档表格 action 简写 → YAML action 全名
ACTION_ALIASES = {
    "auto": "auto_quarantine",
    "candidate": "candidate_only",
}

# docs/04 §3 末段承诺存在的 server_workspace 规则 id
SERVER_WORKSPACE_IDS = ("log_dir", "dep_target_candidate", "dep_vendor")


def _strip_code(s: str) -> str:
    return s.strip().strip("`").strip()


def _load_yaml(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _parse_default_table(doc_text: str) -> dict[str, str]:
    """解析 docs/04 §3「内置规则 ID」表格，返回 {id: action简写}。"""
    lines = doc_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("## 3."):
            start = i
            break
    assert start is not None, "未找到 docs/04 §3 标题"

    rows: dict[str, str] = {}
    for line in lines[start + 1 :]:
        if line.strip().startswith("## "):
            break
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        cid, _match, action = cells[0], cells[1], cells[2]
        if cid in ("id", "") or set(cid) <= {"-", ":"}:
            continue
        rows[_strip_code(cid)] = _strip_code(action)
    return rows


def test_docs_04_default_ids_match_yaml() -> None:
    doc_ids = _parse_default_table(DOCS_04.read_text(encoding="utf-8"))
    assert doc_ids, "docs/04 §3 表格应至少解析出一条规则行"

    default_rules = _load_yaml(DEFAULT_YAML).get("rules") or []
    yaml_by_id = {r["id"]: r["action"] for r in default_rules}

    # 文档列出的每个 id 都存在于 default.yaml，且 action（映射后）一致
    for cid, action_simplified in doc_ids.items():
        assert cid in yaml_by_id, f"docs/04 列出的 id {cid!r} 不在 default.yaml 中"
        expected = ACTION_ALIASES.get(action_simplified, action_simplified)
        assert yaml_by_id[cid] == expected, (
            f"id {cid!r} 的 action 不一致：文档={action_simplified!r}，YAML={yaml_by_id[cid]!r}"
        )

    # docs/04 未遗漏 default.yaml 中的任何 id（双向一致）
    missing = set(yaml_by_id) - set(doc_ids)
    assert not missing, f"default.yaml 中的 id 未在 docs/04 列出：{sorted(missing)}"


def test_docs_04_server_workspace_ids_exist() -> None:
    server_rules = _load_yaml(SERVER_WORKSPACE_YAML).get("rules") or []
    yaml_ids = {r["id"] for r in server_rules}
    for sid in SERVER_WORKSPACE_IDS:
        assert sid in yaml_ids, f"docs/04 提到的 server_workspace id {sid!r} 不在 server_workspace.yaml 中"


def _parse_cli_table(doc_text: str) -> set[str]:
    """解析 docs/03 §5「CLI 契约」表格首列，返回子命令名集合。"""
    lines = doc_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("## 5."):
            start = i
            break
    assert start is not None, "未找到 docs/03 §5 标题"

    names: set[str] = set()
    for line in lines[start + 1 :]:
        if line.strip().startswith("## "):
            break
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells or not cells[0].startswith("`"):
            continue
        tokens = cells[0].strip("`").split()
        if len(tokens) >= 2 and tokens[0] == "dirtidy":
            names.add(tokens[1])
    return names


def test_docs_03_cli_table_matches_parser() -> None:
    """CLI 契约表必须与 argparse 实际注册的子命令双向一致（防漏登记）。"""
    from dirtidy.cli import build_parser

    parser = build_parser()
    actual = {a for a in parser._subparsers._group_actions[0].choices}  # type: ignore[attr-defined]
    documented = _parse_cli_table(DOCS_03.read_text(encoding="utf-8"))

    assert documented, "docs/03 §5 表格应至少解析出一条命令"
    assert documented == actual, (
        f"docs/03 CLI 表与实际不符：文档多出 {sorted(documented - actual)}，"
        f"实现多出 {sorted(actual - documented)}"
    )


def _parse_docs_03_module_table(doc_text: str) -> set[str]:
    """解析 docs/03 §2「模块」表格首列，返回模块名集合。"""
    lines = doc_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("## 2."):
            start = i
            break
    assert start is not None, "未找到 docs/03 §2 标题"

    names: set[str] = set()
    for line in lines[start + 1 :]:
        if line.strip().startswith("## "):
            break
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        name = _strip_code(cells[0])
        if not name or name == "模块" or set(name) <= {"-", ":"}:
            continue
        names.add(name)
    return names


# docs/03 §2 只登记「核心链路 + 入口」；以下顶层模块在别处描述（§5 CLI 契约、§5.1 解释），
# 不要求出现在该表。名单是显式的：新增顶层模块时必须做一个决定——要么进表，要么进这里。
DOCS_03_MODULES_DESCRIBED_ELSEWHERE = {"cli", "explain", "models", "pipeline"}


def test_docs_03_module_table_matches_source() -> None:
    """docs/03 §2 模块表必须与 src/dirtidy 的真实模块对齐。

    这张表此前漏登记过 `desktop` / `watch`——它们只出现在 §5 CLI 表里，
    于是「模块表」变成半份旧账。守三端：
    - 表里每个名字都得对应真实模块（防幻影）；
    - 子包一个不漏（新增子包最容易忘）；
    - 顶层模块除「在别处描述」的那几个外，都必须进表。
    """
    src = REPO_ROOT / "src" / "dirtidy"
    documented = _parse_docs_03_module_table(DOCS_03.read_text(encoding="utf-8"))
    assert documented, "docs/03 §2 表格应至少解析出一条模块"

    for name in sorted(documented):
        assert (src / name).is_dir() or (src / f"{name}.py").exists(), (
            f"docs/03 §2 列出的模块 {name!r} 在 src/dirtidy 下不存在"
        )

    subpackages = {p.name for p in src.iterdir() if p.is_dir() and (p / "__init__.py").exists()}
    assert not (subpackages - documented), f"子包未在 docs/03 §2 登记：{sorted(subpackages - documented)}"

    top_modules = {p.stem for p in src.glob("*.py") if not p.stem.startswith("__")}
    required = top_modules - DOCS_03_MODULES_DESCRIBED_ELSEWHERE
    assert not (required - documented), f"顶层模块未在 docs/03 §2 登记：{sorted(required - documented)}"


def test_docs_07_scale_doc_references_existing_bench() -> None:
    """规模文档必须存在，且引用的基准脚本确实在仓库里。"""
    assert DOCS_07.exists(), "缺少 docs/07-规模与性能.md"
    text = DOCS_07.read_text(encoding="utf-8")
    assert "scripts/bench_scale.py" in text
    assert (REPO_ROOT / "scripts" / "bench_scale.py").exists()
    for nfr in ("NFR-6", "NFR-10"):
        assert nfr in text, f"docs/07 未覆盖 {nfr}"


def test_pyproject_declares_desktop_extra() -> None:
    """桌面壳的托盘被文档宣称为「可选增强」，那它就得真的装得到。

    否则 `pip install dirtidy[desktop]` 是张空头支票：文档说装了 pystray 就有托盘，
    实际 extra 根本不存在。反向也要守——托盘依赖不得进核心 `dependencies`，
    缺它必须仍然能跑（降级为 Ctrl-C 退出）。
    """
    import tomllib

    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = data["project"]["optional-dependencies"]
    assert "desktop" in extras, "pyproject 缺少 desktop extra，文档承诺的托盘增强装不到"
    declared = " ".join(extras["desktop"]).lower()
    for pkg in ("pystray", "pillow"):
        assert pkg in declared, f"desktop extra 未声明 {pkg}"

    core = " ".join(data["project"]["dependencies"]).lower()
    for pkg in ("pystray", "pillow"):
        assert pkg not in core, f"{pkg} 是可选增强，不能进核心依赖（缺它也要能跑）"


def test_gitattributes_pins_fixture_bytes() -> None:
    """fixture 必须关掉换行符转换，否则 Windows 上的归档残留用例会失败。

    这个坑很隐蔽：`payload.txt` 要与 `payload.zip` 里的同名成员做 sha256 对照，
    一旦 checkout 时被转成 CRLF，size（16 → 17）与内容**同时**变：
      - size 不在「待算 hash 的 size 集合」里 → 该文件根本不会被 hash
      - 内容不同 → 即便算了 hash 也对不上 zip 成员
    于是 Windows 上必然报「找不到归档残留」，而 macOS/Linux 默认不转换，一直看不出来。

    所以这里守的不是「文件在不在」，而是**规则有没有真的生效**——直接问 git。
    """
    assert (REPO_ROOT / ".gitattributes").exists(), (
        "缺少 .gitattributes：Windows 上 fixture 会被转成 CRLF，test_archive_hash_candidate 必然失败"
    )

    if shutil.which("git") is None:
        pytest.skip("环境里没有 git，无法验证 attribute 是否生效")

    target = "tests/fixtures/sample_workspace/payload.txt"
    proc = subprocess.run(
        ["git", "check-attr", "text", "--", target],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"git check-attr 不可用: {proc.stderr.strip()}")

    out = proc.stdout.strip()
    assert out.endswith(": unset"), (
        f"fixture 必须关掉换行符转换（-text），实际 attribute 为 {out!r}。"
        "注意 Git 是**后面的规则覆盖前面的**，-text 那条必须写在文件末尾。"
    )


def _parse_docs_05_test_list(doc_text: str) -> set[str]:
    """解析 docs/05 §3「自动化用例」表格首列，返回用例名集合。"""
    lines = doc_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("## 3."):
            start = i
            break
    assert start is not None, "未找到 docs/05 §3 标题"

    names: set[str] = set()
    for line in lines[start + 1 :]:
        if line.strip().startswith("## "):
            break
        if "|" not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if not cells:
            continue
        name = _strip_code(cells[0])
        if name.startswith("test_"):
            names.add(name)
    return names


def _actual_test_functions() -> set[str]:
    """收集 tests/ 下所有 test_* 函数名（含本文件自身）。"""
    found: set[str] = set()
    for path in sorted(TESTS_DIR.glob("test_*.py")):
        text = path.read_text(encoding="utf-8")
        found.update(re.findall(r"^def (test_\w+)", text, re.MULTILINE))
    return found


def test_docs_05_test_list_matches_actual() -> None:
    """docs/05 §3 用例表必须与实际测试函数双向一致（防漏登）。

    与 docs/03、docs/04 的守卫同构：文档漏登记新用例时应当真红，
    否则「测试清单」会慢慢变成一份不可信的旧账。
    """
    documented = _parse_docs_05_test_list(DOCS_05.read_text(encoding="utf-8"))
    actual = _actual_test_functions()

    assert documented, "docs/05 §3 表格应至少解析出一条用例"
    assert documented == actual, (
        f"docs/05 用例表与实际不符：文档多出 {sorted(documented - actual)}，"
        f"实现多出 {sorted(actual - documented)}"
    )
