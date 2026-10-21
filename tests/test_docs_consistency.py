"""文档一致性守护：docs/04 规则 ID 表 vs 包内置规则包 YAML、docs/03 CLI 表 vs argparse、
docs/05 用例表 vs 实际测试函数。

在文档与实现漂移时，此测试应真正失败（真红），而非恒真。
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
DOCS_02 = DOCS / "02-需求规格.md"
DOCS_03 = DOCS / "03-架构设计.md"
DOCS_04 = DOCS / "04-规则包说明.md"
DOCS_05 = DOCS / "05-测试与验收.md"
DOCS_06 = DOCS / "06-已知缺口与后续.md"
DOCS_07 = DOCS / "07-规模与性能.md"
DOCS_09 = DOCS / "09-功能与使用汇总.md"
SRC = REPO_ROOT / "src"
TESTS_DIR = REPO_ROOT / "tests"
FIXTURE_SRC = TESTS_DIR / "fixtures" / "sample_workspace"
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


def test_ci_lints_the_whole_repo_not_just_src_and_tests() -> None:
    """CI 的 ruff 范围必须覆盖 `scripts/`。

    回归背景：加了 `scripts/ui_shots.py` / `scripts/ui_layout_audit.py` 之后，
    workflow 里还是老写法 `ruff check src tests`，于是这两个文件**从未被 lint 过**；
    本机跑 `ruff check .` 才发现在里面躺了一条 142 字符的超长行。
    「CI 绿」和「代码干净」在这类缺省范围下是可以同时成立的假象。

    mypy 不在这里守：它按 `pyproject.toml` 的 `[tool.mypy]` 配置走，范围本就不同。
    """
    ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    lint_lines = [ln.strip() for ln in ci.splitlines() if "ruff check" in ln and not ln.strip().startswith("#")]
    assert lint_lines, "CI 里找不到 ruff 检查步骤"

    for line in lint_lines:
        assert re.search(r"ruff check\s+(\.|--)", line), (
            f"ruff 范围必须覆盖整个仓库，实际是：{line!r}。"
            "写成 `src tests` 会让 scripts/ 与根目录配置文件长期漏检。"
        )


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


def test_fixture_workspace_survives_fresh_clone() -> None:
    """夹具必须能只靠仓库重建，不能依赖「开发机磁盘上恰好留着」。

    回归背景：`__pycache__/mod.pyc` 被 .gitignore 的 `__pycache__/` 屏蔽，
    `empty_dir` 是空目录 git 根本存不下。开发机磁盘上恰好留着这两样 → 本地全绿；
    CI 全新克隆没有 → 6 个 job 一起红。这类问题本地怎么跑都看不出来，
    所以必须显式守：用 `git ls-files` 还原「克隆后能看到什么」，
    再确认 conftest 补齐之后测试真正依赖的条目一个不缺。
    """
    if shutil.which("git") is None:
        pytest.skip("环境里没有 git，无法枚举仓库内容")

    proc = subprocess.run(
        ["git", "ls-files", "tests/fixtures/sample_workspace"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        pytest.skip(f"git ls-files 不可用: {proc.stderr.strip()}")

    prefix = "tests/fixtures/sample_workspace/"
    tracked = {
        line.strip()[len(prefix) :]
        for line in proc.stdout.splitlines()
        if line.strip().startswith(prefix)
    }

    # 测试实际依赖的条目：扫描/规则/隔离区/重复组各取一个代表
    needed = {
        "app.log",
        "dup_a.txt",
        "payload.txt",
        "payload.zip",
        "zero.txt",
        "keep/readme.md",
        "node_modules/pkg/index.js",
        "sub/dup_b.txt",
        "empty_dir",
        "__pycache__/mod.pyc",
    }

    # 这两样 git 天生存不下，所以 conftest 的补齐是**必需**的、不是冗余代码
    assert "empty_dir" in needed - tracked, "空目录 git 存不下，应由 conftest 补"
    assert "__pycache__/mod.pyc" in needed - tracked, (
        "`__pycache__/` 被 .gitignore 屏蔽，该夹具文件应由 conftest 补"
    )

    for rel in sorted(needed):
        assert (FIXTURE_SRC / rel).exists(), (
            f"{rel} 不存在——既没进 git、conftest 也没补上。"
            "本地会因为磁盘残留而绿，CI 全新克隆必红。"
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


# ---------------------------------------------------------------------------
# 「读者会照着敲」的两类引用：命令 与 路径。
#
# 这两类错误与前面几个守卫性质不同：它们不会让任何用例变红，也不会让代码出错，
# 只会让照着文档做的人撞墙。而写文档的人几乎不会真去敲一遍、找一遍，
# 于是它们特别容易长期潜伏。下面把它们变成必跑的一步。
# ---------------------------------------------------------------------------


def _doc_markdown_files() -> list[Path]:
    """README + docs/ 下全部 md —— 用户会照着敲命令的全部地方。"""
    return [REPO_ROOT / "README.md", *sorted(DOCS.glob("*.md"))]


def _cli_subcommands() -> dict[str, argparse.ArgumentParser]:
    """argparse 实际注册的子命令 → 其子 parser。"""
    from dirtidy.cli import build_parser

    for action in build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    raise AssertionError("argparse 里没有注册任何子命令")  # pragma: no cover


def _sub_option_strings(sub: argparse.ArgumentParser) -> set[str]:
    return {opt for a in sub._actions for opt in a.option_strings}


# 行内命令：`dirtidy <子命令> …`。子命令限定 ASCII 小写 / 连字符，
# 于是「dirtidy 是本地后台程序」「dirtidy — 架构设计」这类中文正文不会被误判成命令。
_DOC_COMMAND_RE = re.compile(r"(?<![\w./-])dirtidy\s+([a-z][a-z0-9-]*)((?:\s+[^\n`|]*)?)")


def test_docs_commands_parse_against_cli() -> None:
    """文档里每条 `dirtidy <子命令> [--选项]` 都必须能被真实 parser 接受。

    回归背景：README 安装段让用户不激活 venv 就敲 `dirtidy`，用户照做直接
    `command not found`。那次的错在「前提没交代」，命令本身没问题；但同一类里
    更常见、也更难靠肉眼发现的是**文档写了不存在的子命令或选项**——读者照抄必然报错，
    而写文档的人谁也不会真去敲一遍。这里把「敲一遍」变成必跑的一步。
    """
    subs = _cli_subcommands()
    problems: list[str] = []
    seen = 0

    for path in _doc_markdown_files():
        rel = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _DOC_COMMAND_RE.finditer(line):
                sub, rest = m.group(1), (m.group(2) or "").split("#")[0]
                seen += 1
                if sub not in subs:
                    problems.append(f"{rel}:{lineno} 未知子命令 `dirtidy {sub}`")
                    continue
                valid = _sub_option_strings(subs[sub])
                for token in rest.split():
                    if token.startswith("--") and token.split("=")[0] not in valid:
                        problems.append(f"{rel}:{lineno} `dirtidy {sub}` 无选项 {token.split('=')[0]}")

    # 防止正则失效后「扫到 0 条」也恒绿
    assert seen >= 40, f"只扫到 {seen} 条命令，正则或文档结构可能已变"
    assert not problems, "文档命令与 argparse 不一致：\n  " + "\n  ".join(problems)


_DOC_PATH_RE = re.compile(r"`((?:src|tests|scripts|docs|\.github)/[^\s`*|（）()【】，。；：]+)`")

# 这些不是「仓库里该有的文件」，而是文档在描述别的东西：
#   - `::` 是 pytest 节点 ID / 函数引用（如 tests/test_scale.py::test_xxx）
#   - `__pycache__` 出现在「历史缺陷」的举例里，本就不该入库
_DOC_PATH_SKIP = ("::", "__pycache__")

# `docs/03/04/07` 这类连写简写
_DOC_COMBINED_SHORTHAND = re.compile(r"^docs/\d\d(/\d\d)+$")

# `docs/NN` 是文档在**说明简写约定本身**时用的占位符，不是某个具体文档的路径
_DOC_PLACEHOLDER = re.compile(r"^docs/(NN|nn)$")


def test_docs_referenced_repo_paths_exist() -> None:
    """文档里反引号引用的仓库内路径必须真实存在。

    回归背景：docs/08 把 fixture 写成 `tests/fixtures/payload.txt`，真实路径是
    `tests/fixtures/sample_workspace/payload.txt`——读者按图索骥找不到，也不会有人发现。
    这类错同样不会让任何用例变红，只能靠逐条核对。

    `docs/NN` 是本文档体系通用的简写（指 `docs/NN-xxx.md`），按其语义解析后校验。
    """
    problems: list[str] = []
    checked = 0

    for path in _doc_markdown_files():
        rel = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _DOC_PATH_RE.finditer(line):
                target = m.group(1)
                if (
                    any(s in target for s in _DOC_PATH_SKIP)
                    or _DOC_COMBINED_SHORTHAND.match(target)
                    or _DOC_PLACEHOLDER.match(target)
                ):
                    continue
                checked += 1
                shorthand = re.fullmatch(r"docs/(\d\d)", target)
                if shorthand:
                    if not list(DOCS.glob(f"{shorthand.group(1)}-*.md")):
                        problems.append(f"{rel}:{lineno} 简写 `{target}` 找不到对应文档")
                    continue
                if not (REPO_ROOT / target).exists():
                    problems.append(f"{rel}:{lineno} `{target}` 不存在")

    assert checked >= 20, f"只扫到 {checked} 条路径引用，正则或文档结构可能已变"
    assert not problems, "文档引用了不存在的路径：\n  " + "\n  ".join(problems)


def _parse_docs_09_command_table(doc_text: str) -> dict[str, set[str]]:
    """解析 docs/09 §3.2「命令一览」表，返回 {子命令: 表里声明的选项集合}。"""
    lines = doc_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("### 3.2"):
            start = i
            break
    assert start is not None, "未找到 docs/09 §3.2 标题"

    table: dict[str, set[str]] = {}
    for line in lines[start + 1 :]:
        if line.strip().startswith("### "):
            break
        m = re.match(r"^\|\s*`([a-z][a-z0-9-]*)", line.strip())
        if m:
            table[m.group(1)] = set(re.findall(r"`(--[a-z][a-z0-9-]*)`", line))
    return table


def test_docs_09_command_table_matches_parser() -> None:
    """docs/09 §3.2「命令一览」必须覆盖全部子命令，且选项与 parser 双向一致。

    docs/03 §5 是给实现者看的契约表，docs/09 §3.2 是给用户看的速查表，两张都要守：
    用户通常只读后者，漏登记一个子命令或选项，就等于那个功能不存在。
    `--help` 是 argparse 自动加的，不要求登记。
    """
    subs = _cli_subcommands()
    table = _parse_docs_09_command_table(DOCS_09.read_text(encoding="utf-8"))
    assert table, "docs/09 §3.2 应至少解析出一条命令"

    assert set(table) == set(subs), (
        f"docs/09 §3.2 与实际不符：文档多出 {sorted(set(table) - set(subs))}，"
        f"实现多出 {sorted(set(subs) - set(table))}"
    )

    for name, listed in sorted(table.items()):
        actual = {o for o in _sub_option_strings(subs[name]) if o.startswith("--")}
        actual.discard("--help")
        assert listed == actual, (
            f"docs/09 §3.2 中 `{name}` 的选项与实际不符："
            f"文档多出 {sorted(listed - actual)}，实现多出 {sorted(actual - listed)}"
        )


def _parse_docs_02_cli_list(doc_text: str) -> set[str]:
    """解析 docs/02 FR-9 里「CLI：a、b、c。」这条枚举，返回子命令名集合。"""
    for line in doc_text.splitlines():
        m = re.match(r"^-\s*CLI：(.+)$", line.strip())
        if m:
            return {t.strip().strip("`。. ") for t in re.split(r"[、,，]", m.group(1)) if t.strip()}
    raise AssertionError("未找到 docs/02 FR-9 的 CLI 列表")


def test_docs_02_fr9_cli_list_matches_parser() -> None:
    """docs/02 FR-9 枚举的 CLI 子命令必须与 argparse 一致。

    回归背景：这里曾把 `quarantine-restore` / `purge-quarantine` 简写成 `restore` / `purge`——
    需求文档里读着通顺，读者照着敲却是 `invalid choice`。该清单不归 docs/03 §5 的守卫管
    （那是表格、这是散文枚举），所以错了很久都没人发现。守卫的价值恰在于此：
    把「没人会去核对的地方」也变成必跑的一步。
    """
    subs = _cli_subcommands()
    listed = _parse_docs_02_cli_list(DOCS_02.read_text(encoding="utf-8"))
    assert listed, "docs/02 FR-9 应至少解析出一个子命令"
    assert listed == set(subs), (
        f"docs/02 FR-9 的 CLI 列表与实际不符：文档多出 {sorted(listed - set(subs))}，"
        f"实现多出 {sorted(set(subs) - listed)}"
    )


# 待办编号引用，如 `T-1`、**T-8**、T-1 ～ T-5。要求前后是词边界，
# 避免把 `T-1 ～ T-10` 里的连写或别的含 T- 的串误当成编号。
_DOC_TASK_ID_RE = re.compile(r"\bT-(\d+)\b")


def _parse_docs_06_backlog_ids(doc_text: str) -> set[str]:
    """解析 docs/06「待办台账」表，返回定义过的编号集合（如 {'T-1', ...}）。"""
    lines = doc_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.strip().startswith("## 待办台账"):
            start = i
            break
    assert start is not None, "未找到 docs/06「待办台账」标题"

    ids: set[str] = set()
    for line in lines[start + 1 :]:
        if line.strip().startswith("## "):
            break
        m = re.match(r"^\|\s*(T-\d+)\s*\|", line.strip())
        if m:
            ids.add(m.group(1))
    return ids


def test_docs_task_ids_resolve_to_backlog() -> None:
    """README 与 docs/ 里引用的每个 `T-n` 都必须在 docs/06 的「待办台账」里有定义行。

    回归背景：技术类开放项统一收在 docs/06「待办台账」（T-1 ～ T-10），而 docs/07 / docs/08 /
    docs/09 里散着「→ 待办 T-6」这类**引用**。台账一旦重编号或删条目，别处的引用就变成悬空编号：
    读者拿着 `T-9` 去台账里找，找不到，也不知道它到底还开不开放。这类错与之前修过的文档漂移同源
    ——不会让任何用例变红，只能靠机械核对。

    本用例只查「引用有没有落点」，不查「引用处的描述是否与台账内容一致」——后者无法机械化，
    强行做只会变成一条靠人肉维护的假守卫。
    """
    defined = _parse_docs_06_backlog_ids(DOCS_06.read_text(encoding="utf-8"))
    assert len(defined) >= 10, f"docs/06 台账只解析出 {len(defined)} 条，结构可能已变"

    problems: list[str] = []
    seen = 0
    for path in _doc_markdown_files():
        rel = path.relative_to(REPO_ROOT)
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for m in _DOC_TASK_ID_RE.finditer(line):
                tid = f"T-{m.group(1)}"
                seen += 1
                if tid not in defined:
                    problems.append(f"{rel}:{lineno} 引用了 `{tid}`，但台账里没有定义")

    assert seen >= 20, f"只扫到 {seen} 处待办编号引用，正则或文档结构可能已变"
    assert not problems, "文档引用了台账里不存在的待办编号：\n  " + "\n  ".join(problems)


def test_text_io_always_states_encoding() -> None:
    """所有文本读写都必须显式写 `encoding=`。

    Windows 的 `open()` / `Path.read_text()` 默认用系统代码页（cp1252 一类），
    不是 UTF-8。项目里到处都是中文（审计日志的事件、`notes`、文档），
    漏写 `encoding=` 在 macOS/Linux 上永远绿，到 Windows 上才炸：

        UnicodeDecodeError: 'charmap' codec can't decode byte 0x81 ...

    更麻烦的是它只在**那条路径被读到**时才炸——`cursor-cleanup` 的审计日志
    平时没人读，`--apply` 跑完一切正常，只有用例去核对内容时才红。

    用 AST 而不是正则：正则会把 `def open(self)` 当成调用（误报），
    也会漏掉参数换行写的情况（漏报）。这一步必须精确，否则守卫自己就成了噪音。
    """
    import ast

    offenders: list[str] = []
    for path in sorted([*SRC.rglob("*.py"), *TESTS_DIR.rglob("*.py")]):
        rel = path.relative_to(REPO_ROOT)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(rel))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # 形态一：xxx.read_text(...) / xxx.write_text(...)
            if isinstance(func, ast.Attribute):
                if func.attr not in {"read_text", "write_text"}:
                    continue
            # 形态二：裸 open(...)（内置函数）
            elif not (isinstance(func, ast.Name) and func.id == "open"):
                continue
            if any(kw.arg == "encoding" for kw in node.keywords):
                continue
            offenders.append(f"{rel}:{node.lineno}  缺 encoding=")

    assert not offenders, (
        "以下文本读写没写 encoding=，Windows 上会按系统代码页解码而炸：\n  "
        + "\n  ".join(offenders)
    )
