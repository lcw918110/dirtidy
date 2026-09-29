"""模型适配：用假 HTTP 客户端走通 OpenAI 兼容与 Ollama 的请求/解析。"""

from __future__ import annotations

import json

import pytest

from dirtidy.ai.providers import OllamaProvider, OpenAICompatibleProvider
from dirtidy.hashing.content import _probe_archive_kind


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
