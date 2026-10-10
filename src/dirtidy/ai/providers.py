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

from dirtidy.models import example_config_path, iter_json_array, read_json, write_json

# 这些后缀默认不抽样正文
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks"}
# 文件名敏感线索：命中即完全不抽样正文（凭据/密钥类文本文件，如 credentials.json、config.secret.yaml）
SENSITIVE_NAME_RE = re.compile(
    r"(?i)(?:^|[/\\_.-])("
    r"credential|credentials|secret|secrets|passwd|password|passwords|"
    r"token|tokens|apikey|api[_-]?key|id_rsa|id_ed25519|id_dsa|"
    r"netrc|pgpass|htpasswd|keystore|keychain|\.env|\.npmrc|\.aws|\.ssh"
    r")(?:[/\\_.-]|$)"
)
# 更宽的密钥行模式：覆盖 key=value、key: value、JSON "key": "value"
SECRET_LINE_RE = re.compile(
    r"(?i)(api[_-]?key|secret|token|passwd|password|credential|private[_-]?key|"
    r"access[_-]?key|auth)\s*[\"']?\s*[:=]"
)
# 密钥「键」部分，用于把对应的值替换掉（只截断是不行的——密钥常在开头 200 字内）
_SECRET_KEY = (
    r"(?:api[_-]?key|secret|token|passwd|password|credential|"
    r"private[_-]?key|access[_-]?key|auth)"
)
_SECRET_QUOTED_RE = re.compile(
    r"(?i)(" + _SECRET_KEY + r"[\"']?\s*[:=]\s*[\"'])[^\"'\n]*([\"'])"
)
_SECRET_BARE_RE = re.compile(
    r"(?i)(" + _SECRET_KEY + r"[\"']?\s*[:=]\s*)([^\s\"',;}\]\n]+)"
)


def redact_secrets(text: str) -> str:
    """把疑似密钥的「值」就地替换为 ***；只做截断是无效的，密钥往往就在开头。"""
    text = _SECRET_QUOTED_RE.sub(r"\1***\2", text)
    text = _SECRET_BARE_RE.sub(r"\1***", text)
    return text


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


def default_config_candidates() -> list[Path]:
    """models.yaml 的默认查找位置（按优先级）。

    顺序：$DIRTIDY_CONFIG → ./models.yaml → ~/.config/dirtidy/models.yaml

    刻意**不含**「包所在源码树里的 configs/」这类候选：wheel 安装到 site-packages 后
    它必然不存在，留着只会让报错信息里出现一条永远为假的路径。
    """
    out: list[Path] = []
    env = os.environ.get("DIRTIDY_CONFIG")
    if env:
        out.append(Path(env).expanduser())
    out.append(Path.cwd() / "models.yaml")
    out.append(Path.home() / ".config" / "dirtidy" / "models.yaml")
    return out


def load_model_config(path: Path | None = None) -> dict[str, Any]:
    """查找并加载 models.yaml。

    显式传入路径时只认该路径；否则按 default_config_candidates() 顺序查找。
    找不到就报错，不静默回退到 models.example.yaml（避免把示例当正式配置）。
    """
    if path is None:
        candidates = default_config_candidates()
        for candidate in candidates:
            if candidate.exists():
                path = candidate
                break
        else:
            searched = "、".join(str(p) for p in candidates)
            raise FileNotFoundError(
                f"未找到 models.yaml。已查找：{searched}。"
                f"可复制 {example_config_path()} 为 models.yaml，"
                "或设置环境变量 DIRTIDY_CONFIG 指向配置文件。"
            )
    if not path.exists():
        raise FileNotFoundError(f"未找到 models.yaml: {path}")
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
    if (
        p.is_file()
        and suffix not in SENSITIVE_SUFFIXES
        and not SENSITIVE_NAME_RE.search(rel_path)
    ):
        # 默认：仅文本类抽样；allow_full 时也只对可读文本尝试解码
        if allow_full or suffix in TEXT_SUFFIXES:
            try:
                raw = p.read_bytes()[: max_text_chars * 4]
                text = raw.decode("utf-8", errors="ignore")[:max_text_chars]
                if SECRET_LINE_RE.search(text):
                    text = redact_secrets(text)
                summary["text_sample"] = text
            except OSError:
                pass
    return summary


ANALYSIS_PROMPT = """你是目录整理助手。根据给定的文件安全摘要，为每个文件输出 JSON 数组。
每个元素字段：path, asset_type, value, freshness, cluster_id, cluster_name,
sensitivity, action, suggested_path, confidence, rationale。
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
        # 显式写的 api_key 优先（供网页设置页填写）；否则回落到环境变量。
        # 两条路都走不通才报错——报错信息要同时点出这两个来源，否则用户
        # 明明在网页里填了密钥，却只被告知「环境变量未设置」，无从下手。
        self.api_key = str(cfg.get("api_key") or "").strip() or os.environ.get(env, "")
        self.model = cfg.get("model", "gpt-4o-mini")
        if not self.api_key:
            raise RuntimeError(f"未配置模型密钥：请在设置页填写，或设置环境变量 {env}")

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
    cfg: dict[str, Any] = (
        load_model_config(config_path) if not mock else {"provider": "mock", "summary": {}}
    )
    summary_cfg = cfg.get("summary") or {}
    provider = get_provider(cfg, mock=mock)

    candidates_paths: set[str] = set()
    if (run_path / "candidates.json").exists():
        for c in read_json(run_path / "candidates.json"):
            candidates_paths.update(c.get("paths") or [])

    quarantined: set[str] = set()
    if (run_path / "quarantine_manifest.json").exists():
        quarantined = {e["original_path"] for e in read_json(run_path / "quarantine_manifest.json")}

    # 优先候选；非候选仅保留浅层路径，控制 token 成本。
    # 注意：不能用 `candidates_paths and ...` 短路——候选为空集时会让全量文件外发。
    #
    # 流式读 inventory 而不是 json.loads：5M 条 inventory 整份解析会带来约 2.6 倍放大。
    # 同时用 max_targets 给外发规模封顶——T 级数据下「全部发给模型」既慢又贵。
    max_depth = int(summary_cfg.get("max_depth") or 3)
    max_targets = int(summary_cfg.get("max_targets") or 500)
    targets: list[str] = []
    hash_by_path: dict[str, str | None] = {}
    truncated = False
    inv_path = run_path / "inventory.json"
    if inv_path.exists():
        for item in iter_json_array(inv_path):
            if item.get("is_dir"):
                continue
            path = item["path"]
            if path in quarantined:
                continue
            if path not in candidates_paths and path.count("/") > max_depth:
                continue
            if len(targets) >= max_targets:
                truncated = True
                break
            targets.append(path)
            hash_by_path[path] = item.get("sha256")

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
    write_json(
        run_path / "ai_meta.json",
        {
            "provider": cfg.get("provider", "mock") if not mock else "mock",
            "targets": len(targets),
            "max_targets": max_targets,
            "truncated": truncated,
            "max_depth": max_depth,
        },
    )
    if truncated:
        import sys

        print(
            f"警告: 外发目标已达上限 {max_targets} 条，剩余文件未提交模型；"
            "可调大 models.yaml 的 summary.max_targets 后重跑。",
            file=sys.stderr,
        )
    return results
