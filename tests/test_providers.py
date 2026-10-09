"""模型适配：用假 HTTP 客户端走通 OpenAI 兼容与 Ollama 的请求/解析。"""

from __future__ import annotations

import json

import pytest

from dirtidy.ai.providers import OllamaProvider, OpenAICompatibleProvider, load_model_config
from dirtidy.hashing.content import _probe_archive_kind, iter_archive_members


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def test_openai_compatible_parses_json_array(monkeypatch: pytest.MonkeyPatch) -> None:
    body = [
        {
            "path": "a.txt",
            "asset_type": "doc",
            "value": "keep",
            "freshness": "unknown",
            "cluster_id": "c1",
            "cluster_name": "docs",
            "sensitivity": "low",
            "action": "keep",
            "suggested_path": "archive/docs/",
            "confidence": 0.7,
            "rationale": "说明",
        }
    ]

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def post(self, url: str, headers: dict | None = None, json: dict | None = None) -> _Resp:
            assert url.endswith("/chat/completions")
            assert headers and headers["Authorization"].startswith("Bearer ")
            assert json and json["model"] == "demo-model"
            return _Resp({"choices": [{"message": {"content": "```json\n" + json_dumps(body) + "\n```"}}]})

    monkeypatch.setattr("dirtidy.ai.providers.httpx.Client", FakeClient)
    monkeypatch.setenv("DIRTIDY_API_KEY", "test-key")
    provider = OpenAICompatibleProvider(
        {"base_url": "http://127.0.0.1:9/v1", "api_key_env": "DIRTIDY_API_KEY", "model": "demo-model"}
    )
    out = provider.analyze_batch([{"path": "a.txt", "ext": ".txt"}])
    assert out[0]["action"] == "keep"
    assert out[0]["cluster_name"] == "docs"


def json_dumps(data: list) -> str:
    return json.dumps(data, ensure_ascii=False)


def test_ollama_parses_response(monkeypatch: pytest.MonkeyPatch) -> None:
    body = [{"path": "b.py", "asset_type": "source", "value": "keep", "freshness": "recent",
             "cluster_id": "c2", "cluster_name": "code", "sensitivity": "low", "action": "archive",
             "suggested_path": "archive/code/", "confidence": 0.6, "rationale": "本机"}]

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def post(self, url: str, json: dict | None = None) -> _Resp:
            assert url.endswith("/api/generate")
            assert json and json["model"] == "llama-test"
            return _Resp({"response": json_dumps(body)})

    monkeypatch.setattr("dirtidy.ai.providers.httpx.Client", FakeClient)
    provider = OllamaProvider({"host": "http://127.0.0.1:11434", "model": "llama-test"})
    out = provider.analyze_batch([{"path": "b.py"}])
    assert out[0]["action"] == "archive"


def test_archive_member_names_mark_installer() -> None:
    assert _probe_archive_kind(["Setup/setup.exe", "Setup/readme.txt"]) == "installer_like"
    assert _probe_archive_kind(["src/main.py", "pyproject.toml", "README.md"]) == "source_like"


def test_iter_archive_members_returns_error_for_not_implemented(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """加密 zip 会抛 NotImplementedError：应返回错误而非冒泡使 scan 崩溃。"""
    import zipfile

    archive = tmp_path / "enc.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("a.txt", "hello")

    def _raise(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise NotImplementedError("That compression method is not supported")

    monkeypatch.setattr(zipfile.ZipFile, "open", _raise)
    members, err = iter_archive_members(archive)
    assert members == []
    assert err is not None
    assert "not supported" in err


def test_load_model_config_raises_when_only_example_exists(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """仅有 models.example.yaml 时不得静默回退：应抛 FileNotFoundError。"""
    example = tmp_path / "models.example.yaml"
    example.write_text("provider: mock\n", encoding="utf-8")

    monkeypatch.setattr("dirtidy.ai.providers.Path.cwd", lambda: tmp_path)
    monkeypatch.setattr("dirtidy.ai.providers.package_root", lambda: tmp_path)
    # 隔离用户级查找路径，保证结果与开发机上的 ~/.config/dirtidy 无关
    monkeypatch.delenv("DIRTIDY_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    with pytest.raises(FileNotFoundError):
        load_model_config()


def test_load_model_config_explicit_path(tmp_path) -> None:
    """显式传入路径时只认该路径。"""
    cfg = tmp_path / "m.yaml"
    cfg.write_text("provider: ollama\n", encoding="utf-8")
    assert load_model_config(cfg)["provider"] == "ollama"


def test_load_model_config_respects_dirtidy_config_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """$DIRTIDY_CONFIG 优先，且不依赖 cwd / 源码树。"""
    cfg = tmp_path / "custom.yaml"
    cfg.write_text("provider: mock\n", encoding="utf-8")
    monkeypatch.setenv("DIRTIDY_CONFIG", str(cfg))
    monkeypatch.setattr("dirtidy.ai.providers.Path.cwd", lambda: tmp_path / "nope")
    monkeypatch.setattr("dirtidy.ai.providers.package_root", lambda: tmp_path / "nope")
    assert load_model_config()["provider"] == "mock"


def test_load_model_config_finds_user_config_dir(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """非 editable 安装下可用 ~/.config/dirtidy/models.yaml。"""
    home = tmp_path / "home"
    (home / ".config" / "dirtidy").mkdir(parents=True)
    (home / ".config" / "dirtidy" / "models.yaml").write_text("provider: mock\n", encoding="utf-8")
    monkeypatch.delenv("DIRTIDY_CONFIG", raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr("dirtidy.ai.providers.Path.cwd", lambda: tmp_path / "nope")
    monkeypatch.setattr("dirtidy.ai.providers.package_root", lambda: tmp_path / "nope")
    assert load_model_config()["provider"] == "mock"
