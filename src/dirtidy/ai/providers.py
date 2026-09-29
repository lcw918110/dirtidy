"""模型适配：OpenAI 兼容 API、Ollama、以及离线 mock。

外发默认仅「安全摘要」；完整正文需显式配置，且完整外发仅建议在本机 Ollama 开启。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

import httpx
import yaml

from dirtidy.models import package_root, read_json, write_json

# 这些后缀默认不抽样正文
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks"}
TEXT_SUFFIXES = {
    ".txt",
    ".md",
    ".py",
    ".js",
    ".ts",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".csv",
    ".log",
    ".sh",
    ".rs",
    ".go",
    ".java",
    ".c",
    ".cpp",
    ".h",
    ".html",
    ".css",
    ".xml",
}


def load_model_config(path: Path | None = None) -> dict[str, Any]:
    """查找 models.yaml：显式路径 → cwd → 项目 configs。"""
    if path is None:
        for candidate in (
            Path.cwd() / "models.yaml",
            package_root() / "configs" / "models.yaml",
            package_root() / "configs" / "models.example.yaml",
        ):
            if candidate.exists():
                path = candidate
                break
    if path is None or not path.exists():
        raise FileNotFoundError("未找到 models.yaml；请复制 configs/models.example.yaml")
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def build_summary(
    root: Path,
    rel_path: str,
    *,
    max_text_chars: int = 800,
    redact_path: bool = False,
    allow_full: bool = False,
    hash_full: str | None = None,
) -> dict[str, Any]:
    """构造发给模型的单文件摘要；hash_prefix 优先用 inventory 已有 hash。"""
    p = root / rel_path
    suffix = p.suffix.lower()
    st = p.stat() if p.exists() else None
    path_out = rel_path
    if redact_path:
        parts = Path(rel_path).parts
        path_out = "/".join(["…"] * (len(parts) - 1) + [parts[-1]]) if parts else rel_path

    summary: dict[str, Any] = {
        "path": path_out,
        "ext": suffix,
        "size": int(st.st_size) if st else 0,
        "mtime": float(st.st_mtime) if st else 0.0,
        "kind": "dir" if p.is_dir() else "file",
        "hash_prefix": (hash_full[:16] if hash_full else None),
        "text_sample": None,
    }
    if p.is_file() and suffix not in SENSITIVE_SUFFIXES:
        # 默认：仅文本类抽样；allow_full 时也只对可读文本尝试解码
        if allow_full or suffix in TEXT_SUFFIXES:
            try:
                raw = p.read_bytes()[: max_text_chars * 4]
                text = raw.decode("utf-8", errors="ignore")[:max_text_chars]
                if re.search(r"(?i)(api[_-]?key|secret|password)\s*=", text):
                    text = text[:200] + "\n[redacted possible secrets]"
                summary["text_sample"] = text
            except OSError:
                pass
    return summary


ANALYSIS_PROMPT = """你是目录整理助手。根据给定的文件安全摘要，为每个文件输出 JSON 数组。
每个元素字段：path, asset_type, value, freshness, cluster_id, cluster_name, sensitivity, action, suggested_path, confidence, rationale。
asset_type: source|doc|config|data|media|build|secret_risk|other
value: keep|archive|discardable|needs_human
freshness: recent|stale|unknown
sensitivity: high|medium|low
action: keep|archive|quarantine|review
只输出 JSON 数组，不要 Markdown。
"""


class ModelProvider:
    """模型后端统一接口。"""

    def analyze_batch(self, summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        raise NotImplementedError


class MockProvider(ModelProvider):
    """无网络时的桩实现，用于测试与演示。"""

    def analyze_batch(self, summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for s in summaries:
            ext = (s.get("ext") or "").lower()
            asset = "doc" if ext in {".md", ".txt"} else "source" if ext in {".py", ".js"} else "other"
            out.append(
                {
                    "path": s.get("path"),
                    "asset_type": asset,
                    "value": "needs_human",
                    "freshness": "unknown",
                    "cluster_id": "c1",
                    "cluster_name": "misc",
                    "sensitivity": "low",
                    "action": "review",
                    "suggested_path": f"archive/{asset}/",
                    "confidence": 0.4,
                    "rationale": "mock provider",
                }
            )
        return out


class OpenAICompatibleProvider(ModelProvider):
    """任意 OpenAI Chat Completions 兼容端点。"""

    def __init__(self, cfg: dict[str, Any]):
        self.base_url = cfg.get("base_url", "").rstrip("/")
        env = cfg.get("api_key_env", "DIRTIDY_API_KEY")
        self.api_key = os.environ.get(env, "")
        self.model = cfg.get("model", "gpt-4o-mini")
        if not self.api_key:
            raise RuntimeError(f"环境变量 {env} 未设置")

    def analyze_batch(self, summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": ANALYSIS_PROMPT},
                {"role": "user", "content": json.dumps(summaries, ensure_ascii=False)},
            ],
            "temperature": 0.2,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        with httpx.Client(timeout=120.0) as client:
            r = client.post(f"{self.base_url}/chat/completions", headers=headers, json=payload)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
        return _parse_json_array(content)


class OllamaProvider(ModelProvider):
    """本机 Ollama /api/generate。"""

    def __init__(self, cfg: dict[str, Any]):
        self.host = cfg.get("host", "http://127.0.0.1:11434").rstrip("/")
        self.model = cfg.get("model", "llama3.2")

    def analyze_batch(self, summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prompt = ANALYSIS_PROMPT + "\n\n" + json.dumps(summaries, ensure_ascii=False)
        with httpx.Client(timeout=180.0) as client:
            r = client.post(
                f"{self.host}/api/generate",
                json={"model": self.model, "prompt": prompt, "stream": False},
            )
            r.raise_for_status()
            content = r.json().get("response", "")
        return _parse_json_array(content)


def _parse_json_array(content: str) -> list[dict[str, Any]]:
    """容忍模型外包一层 ```json 代码块。"""
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\n?", "", content)
        content = re.sub(r"\n?```$", "", content)
    data = json.loads(content)
    if isinstance(data, dict) and "items" in data:
        data = data["items"]
    if not isinstance(data, list):
        raise ValueError("模型输出不是 JSON 数组")
    return data


def get_provider(cfg: dict[str, Any], *, mock: bool = False) -> ModelProvider:
    if mock or cfg.get("provider") == "mock":
        return MockProvider()
    name = cfg.get("provider", "openai_compatible")
    if name == "ollama":
        return OllamaProvider(cfg.get("ollama") or {})
    return OpenAICompatibleProvider(cfg.get("openai_compatible") or {})


def analyze_run(
    root: Path,
    run_path: Path,
    *,
    config_path: Path | None = None,
    mock: bool = False,
) -> list[dict[str, Any]]:
    """对某次 run 的残留/候选文件跑模型，写入 ai_suggestions.json。"""
    cfg = load_model_config(config_path) if not mock else {"provider": "mock", "summary": {}}
    summary_cfg = cfg.get("summary") or {}
    provider = get_provider(cfg, mock=mock)
    inventory = read_json(run_path / "inventory.json")
    hash_by_path = {i["path"]: i.get("sha256") for i in inventory}

    candidates_paths: set[str] = set()
    if (run_path / "candidates.json").exists():
        for c in read_json(run_path / "candidates.json"):
            candidates_paths.update(c.get("paths") or [])

    quarantined: set[str] = set()
    if (run_path / "quarantine_manifest.json").exists():
        quarantined = {e["original_path"] for e in read_json(run_path / "quarantine_manifest.json")}

    # 优先候选；非候选仅保留浅层路径，控制 token 成本
    targets: list[str] = []
    for item in inventory:
        if item.get("is_dir"):
            continue
        path = item["path"]
        if path in quarantined:
            continue
        if candidates_paths and path not in candidates_paths:
            if path.count("/") > 3:
                continue
        targets.append(path)

    allow_full = bool(summary_cfg.get("allow_ollama_full_content")) and cfg.get("provider") == "ollama"
    if summary_cfg.get("allow_external_content") and cfg.get("provider") == "openai_compatible":
        allow_full = True

    batch_size = int(summary_cfg.get("batch_size") or 20)
    results: list[dict[str, Any]] = []
    for i in range(0, len(targets), batch_size):
        batch_paths = targets[i : i + batch_size]
        summaries = [
            build_summary(
                root,
                p,
                max_text_chars=int(summary_cfg.get("max_text_chars") or 800),
                redact_path=bool(summary_cfg.get("redact_path")),
                allow_full=allow_full,
                hash_full=hash_by_path.get(p),
            )
            for p in batch_paths
        ]
        results.extend(provider.analyze_batch(summaries))

    write_json(run_path / "ai_suggestions.json", results)
    return results
