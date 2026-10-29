"""`scripts/workspace_hygiene.py` 的守卫：只认指纹，不误伤普通文件。

这些残留来自执行环境（命令失败/被拦时掉在 cwd 的探测文件），不是项目代码
写出来的。所以这里不测「环境会不会产生残留」，只测「识别逻辑有没有走样」：
指纹松一点就会误删别人的文件，紧一点就永远报不出来。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "workspace_hygiene.py"


def _hygiene():
    spec = importlib.util.spec_from_file_location("workspace_hygiene", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_residue_fingerprint_needs_all_traits(tmp_path: Path) -> None:
    """三项特征缺一不认：8 字符名、0600、内容 blat。

    真实的探测文件是 mkstemp 造的，所以权限必然是 0600；普通人手写的短文件
    是 0644。只按「名字像随机串」去删，迟早删掉别人的东西。
    """
    mod = _hygiene()

    hit = tmp_path / "ab12cd34"
    hit.write_bytes(b"blat")
    hit.chmod(0o600)

    same_name_wrong_content = tmp_path / "ef56gh78"
    same_name_wrong_content.write_text("real", encoding="utf-8")
    same_name_wrong_content.chmod(0o600)

    right_content_wrong_mode = tmp_path / "ij90kl12"
    right_content_wrong_mode.write_bytes(b"blat")  # 默认 0644

    right_content_wrong_name = tmp_path / "notes.md"
    right_content_wrong_name.write_bytes(b"blat")
    right_content_wrong_name.chmod(0o600)

    assert [p.name for p in mod.find_residue(tmp_path)] == ["ab12cd34"]


def test_report_mode_exits_nonzero_without_touching_files(tmp_path: Path) -> None:
    """默认只报告：退出码 1 让 CI / 提交前能拦住，但一个文件都不动。"""
    mod = _hygiene()
    junk = tmp_path / "zz99yy88"
    junk.write_bytes(b"blat")
    junk.chmod(0o600)

    assert mod.main(["--root", str(tmp_path), "--quiet"]) == 1
    assert junk.exists(), "只报告时不许删文件"

    clean = tmp_path / "clean-root"
    clean.mkdir()
    assert mod.main(["--root", str(clean), "--quiet"]) == 0
