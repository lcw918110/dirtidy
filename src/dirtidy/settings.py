"""界面与命令行共用的本地设置。

两处设置都落在**状态目录**（默认 `<目标根>/.dirtidy/`）下，因此同一份配置对
CLI 与 Web 同时生效——不会出现「网页里开了直删、命令行跑却还是只隔离」这种
两边口径不一致的情况。

- `settings.json`：界面偏好。目前两个键：
  - `auto_purge_rules`：允许「隔离后立即物理删除」的规则 id 列表（默认空 = 全部走隔离区）。
  - `use_model`：是否启用模型分析（默认 False = 不调用任何模型）。
- `models.yaml`：模型接入配置，格式与项目原有 `models.yaml` 完全一致，
  额外允许直接写 `api_key`（明文，供界面填写）。

**密钥说明**：`api_key` 明文落在状态目录里，与用户既有的 `models.yaml` 同一量级的
风险。`models.yaml` 文件名已在仓库 `.gitignore` 中，但目标根若另有版本控制，
仍可能被提交——界面在启用时会显式提示这一点，不靠用户自己想到。
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from dirtidy.models import read_json, state_root, write_json, write_text_atomic

#: 界面允许选择的 provider。`mock` 刻意不在其中：它是测试与离线演示用的桩，
#: 输出是固定占位值（全 `needs_human`），拿来做决策等于「全都交人工」。
SELECTABLE_PROVIDERS: tuple[str, ...] = ("openai_compatible", "ollama")

#: 与 `configs/models.example.yaml` 保持一致的默认值，供界面表单预填。
DEFAULT_SUMMARY: dict[str, Any] = {
    "max_text_chars": 800,
    "redact_path": False,
    "allow_external_content": False,
    "allow_ollama_full_content": False,
    "batch_size": 20,
    "max_depth": 3,
    "max_targets": 500,
}


class SettingsError(ValueError):
    """设置内容非法（provider 不认识、字段类型不对等）。"""


def settings_path(root: Path) -> Path:
    return state_root(root) / "settings.json"


def model_settings_path(root: Path) -> Path:
    return state_root(root) / "models.yaml"


def _load_settings(root: Path) -> dict[str, Any]:
    p = settings_path(root)
    if not p.exists():
        return {}
    try:
        data = read_json(p)
    except (ValueError, OSError):
        # 设置文件被手改坏时退回默认值，而不是让整个界面 500。
        # 这是「偏好」不是「数据」——坏掉顶多回到保守默认（不直删、不用模型）。
        return {}
    return data if isinstance(data, dict) else {}


def load_auto_purge_rules(root: Path) -> list[str]:
    """返回允许「隔离后立即物理删除」的规则 id 列表（默认空）。"""
    raw = _load_settings(root).get("auto_purge_rules") or []
    return [str(x) for x in raw if str(x).strip()]


def save_auto_purge_rules(root: Path, rule_ids: Iterable[str]) -> list[str]:
    cleaned = sorted({str(r).strip() for r in rule_ids if str(r).strip()})
    data = _load_settings(root)
    data["auto_purge_rules"] = cleaned
    write_json(settings_path(root), data)
    return cleaned


def load_use_model(root: Path) -> bool:
    """是否启用模型分析。**默认 False**：不配置就不外发任何内容。"""
    return bool(_load_settings(root).get("use_model", False))


def save_use_model(root: Path, enabled: bool) -> bool:
    data = _load_settings(root)
    data["use_model"] = bool(enabled)
    write_json(settings_path(root), data)
    return bool(enabled)


def load_model_settings(root: Path) -> dict[str, Any] | None:
    """读状态目录里的 models.yaml；不存在返回 None（不是空 dict）。"""
    p = model_settings_path(root)
    if not p.exists():
        return None
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise SettingsError(f"模型配置必须是 mapping: {p}")
    return data


def normalize_model_settings(raw: dict[str, Any]) -> dict[str, Any]:
    """把界面提交的表单归一成一份完整的 models.yaml 结构。

    只做结构归一与白名单取值，不做「密钥是否有效」这类需要联网的校验——
    那是 `POST /api/runs/{id}/analyze` 的事，报错要出现在真正调用模型的那一步。
    """
    if not isinstance(raw, dict):
        raise SettingsError("模型配置必须是对象")

    provider = str(raw.get("provider") or "openai_compatible")
    if provider not in SELECTABLE_PROVIDERS:
        raise SettingsError(
            f"未知 provider {provider!r}，可选 {list(SELECTABLE_PROVIDERS)}"
        )

    summary_in = raw.get("summary") or {}
    if not isinstance(summary_in, dict):
        raise SettingsError("summary 必须是对象")
    summary = dict(DEFAULT_SUMMARY)
    for key, default in DEFAULT_SUMMARY.items():
        if key not in summary_in or summary_in[key] is None:
            continue
        if isinstance(default, bool):
            summary[key] = bool(summary_in[key])
        else:
            try:
                summary[key] = int(summary_in[key])
            except (TypeError, ValueError) as e:
                raise SettingsError(f"summary.{key} 必须是整数") from e

    out: dict[str, Any] = {"provider": provider, "summary": summary}

    oc = raw.get("openai_compatible") or {}
    if not isinstance(oc, dict):
        raise SettingsError("openai_compatible 必须是对象")
    base_url = str(oc.get("base_url") or "https://api.openai.com/v1").strip()
    if not base_url.startswith(("http://", "https://")):
        raise SettingsError(f"base_url 必须以 http:// 或 https:// 开头：{base_url!r}")
    api_key = str(oc.get("api_key") or "").strip()
    out["openai_compatible"] = {
        "base_url": base_url,
        "api_key_env": str(oc.get("api_key_env") or "DIRTIDY_API_KEY").strip(),
        "model": str(oc.get("model") or "gpt-4o-mini").strip(),
        # 只在真的填了密钥时才落盘，避免写出一个空的 api_key 让 provider 误判
        **({"api_key": api_key} if api_key else {}),
    }

    ol = raw.get("ollama") or {}
    if not isinstance(ol, dict):
        raise SettingsError("ollama 必须是对象")
    host = str(ol.get("host") or "http://127.0.0.1:11434").strip()
    if not host.startswith(("http://", "https://")):
        raise SettingsError(f"ollama host 必须以 http:// 或 https:// 开头：{host!r}")
    out["ollama"] = {"host": host, "model": str(ol.get("model") or "llama3.2").strip()}

    return out


def save_model_settings(root: Path, raw: dict[str, Any]) -> Path:
    """归一后写入状态目录的 models.yaml，返回落盘路径。"""
    data = normalize_model_settings(raw)
    p = model_settings_path(root)
    write_text_atomic(p, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
    return p


def model_settings_status(root: Path) -> dict[str, Any]:
    """给界面看的模型配置状态。**永远不回显密钥本身**，只说「有没有」。"""
    import os

    p = model_settings_path(root)
    out: dict[str, Any] = {
        "configured": False,
        "path": str(p),
        "provider": None,
        "model": "",
        "endpoint": "",
        "api_key_env": "",
        "api_key_env_set": False,
        "api_key_inline": False,
        "use_model": load_use_model(root),
        "summary": dict(DEFAULT_SUMMARY),
    }
    try:
        data = load_model_settings(root)
    except SettingsError:
        return out
    if not data:
        return out

    provider = str(data.get("provider") or "")
    out["configured"] = provider in SELECTABLE_PROVIDERS
    out["provider"] = provider or None
    out["summary"] = {**DEFAULT_SUMMARY, **(data.get("summary") or {})}

    if provider == "ollama":
        ol = data.get("ollama") or {}
        out["model"] = str(ol.get("model") or "")
        out["endpoint"] = str(ol.get("host") or "")
    else:
        oc = data.get("openai_compatible") or {}
        out["model"] = str(oc.get("model") or "")
        out["endpoint"] = str(oc.get("base_url") or "")
        env_name = str(oc.get("api_key_env") or "")
        out["api_key_env"] = env_name
        out["api_key_env_set"] = bool(env_name and os.environ.get(env_name))
        out["api_key_inline"] = bool(str(oc.get("api_key") or "").strip())
    return out
