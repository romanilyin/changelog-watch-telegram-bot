#!/usr/bin/env python3
"""Compare one-line AI summaries across multiple models.

The script takes release ids from SQLite state, reconstructs release notes from
products.yaml sources, calls configured OpenAI-compatible chat completion APIs,
and writes a Markdown comparison table.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import bot  # noqa: E402


DEFAULT_MODELS_CONFIG = "scripts/model-summary-compare.local.yaml"
DEFAULT_OUTPUT = "data/model-summary-comparison.md"
DEFAULT_MODEL_LISTS_DIR = "data/model-lists"
DEFAULT_MODEL_DECISIONS = "data/model-decisions.yaml"
HIDDEN_MODEL_ACTIONS = {"skip", "retry_later"}


@dataclass(frozen=True)
class ReleaseRow:
    source_id: str
    item_id: str
    selected_at: str | None = None


@dataclass(frozen=True)
class ReleaseCase:
    source: dict[str, Any]
    entry: bot.ChangelogEntry
    selected_at: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return str(self.source["id"]), self.entry.item_id


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    api_base: str
    api_key: str
    auth_type: str
    api_key_header: str
    api_key_query_param: str
    rpm: int
    timeout_seconds: int
    max_attempts: int
    rate_limit_wait_seconds: float
    max_retry_after_seconds: float
    rate_limit_total_wait_seconds: float
    extra_headers: dict[str, str]
    chat_completions_path: str
    models_path: str
    rate_limit_group: str
    concurrent_requests: int
    model_limits: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class ModelConfig:
    name: str
    model: str
    provider_name: str | None
    api_base: str
    api_key: str
    auth_type: str
    api_key_header: str
    api_key_query_param: str
    rpm: int
    rate_limit_group: str
    concurrent_group: str
    concurrent_requests: int
    timeout_seconds: int
    max_tokens: int
    max_output_chars: int
    max_input_chars: int
    target_language: str
    temperature: float
    max_attempts: int
    rate_limit_wait_seconds: float
    max_retry_after_seconds: float
    rate_limit_total_wait_seconds: float
    extra_headers: dict[str, str]
    limit_category: str
    tpm_limit: str
    rpd_limit: str
    chat_completions_path: str = "/chat/completions"

    @property
    def column_title(self) -> str:
        return self.name or self.model


@dataclass(frozen=True)
class ModelListing:
    model_id: str
    flags: tuple[str, ...] = ()
    label: str | None = None

    def format_line(self) -> str:
        flags = f" [{' '.join(self.flags)}]" if self.flags else ""
        label = f" — {self.label}" if self.label and self.label != self.model_id else ""
        return f"{self.model_id}{flags}{label}"

    def to_json(self) -> dict[str, Any]:
        return {"id": self.model_id, "flags": list(self.flags), "label": self.label}


@dataclass(frozen=True)
class SummaryResult:
    text: str
    usage: dict[str, int]

    def usage_text(self) -> str:
        if not self.usage:
            return ""
        labels = [
            ("total_tokens", "total"),
            ("prompt_tokens", "prompt"),
            ("completion_tokens", "completion"),
            ("input_tokens", "input"),
            ("output_tokens", "output"),
        ]
        parts = [f"{label}={self.usage[key]}" for key, label in labels if key in self.usage]
        return "tokens: " + ", ".join(parts) if parts else ""


class RpmLimiter:
    def __init__(self, rpm: int) -> None:
        self.rpm = max(1, int(rpm))
        self.timestamps: deque[float] = deque()
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        async with self._lock:
            now = monotonic()
            while self.timestamps and now - self.timestamps[0] >= 60:
                self.timestamps.popleft()

            if len(self.timestamps) >= self.rpm:
                delay = max(0.0, 60 - (now - self.timestamps[0])) + 0.1
                print(f"[rpm] limit {self.rpm}/min reached, sleeping {delay:.1f}s", flush=True)
                await asyncio.sleep(delay)
                now = monotonic()
                while self.timestamps and now - self.timestamps[0] >= 60:
                    self.timestamps.popleft()

            self.timestamps.append(monotonic())


def read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return data


def resolve_path(path_text: str, *, base_dir: Path = PROJECT_ROOT) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path


def as_int(value: Any, default: int) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def as_float(value: Any, default: float) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def as_non_negative_int(value: Any, default: int = 0) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def config_value(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def read_object(value: Any, context: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{context} must be an object")
    return value


def normalize_decision_model_id(provider_name: str | None, model_id: str) -> str:
    provider = (provider_name or "").strip()
    model = str(model_id or "").strip()
    if provider == "google" and model.startswith("models/"):
        return model.split("/", 1)[1]
    return model


def model_decision_key(provider_name: str | None, model_id: str) -> str:
    provider = (provider_name or "").strip() or "<none>"
    return f"{provider}:{normalize_decision_model_id(provider, model_id)}"


def load_model_decisions(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = read_yaml(path)
    raw_models = data.get("models", {})
    if raw_models is None:
        return {}
    if not isinstance(raw_models, dict):
        raise ValueError("model decisions must contain a models object")

    decisions: dict[str, dict[str, Any]] = {}
    for raw_key, raw_decision in raw_models.items():
        if not isinstance(raw_decision, dict):
            continue
        key = str(raw_key).strip()
        if not key:
            continue
        decisions[key] = raw_decision
    return decisions


def get_model_decision(decisions: dict[str, dict[str, Any]], provider_name: str | None, model_id: str) -> dict[str, Any] | None:
    key = model_decision_key(provider_name, model_id)
    return decisions.get(key)


def decision_action(decision: dict[str, Any] | None) -> str:
    if not decision:
        return ""
    return str(decision.get("action") or "").strip().lower()


def decision_hides_model(decision: dict[str, Any] | None) -> bool:
    return decision_action(decision) in HIDDEN_MODEL_ACTIONS


def decision_flags(decision: dict[str, Any] | None) -> tuple[str, ...]:
    if not decision:
        return ()
    action = decision_action(decision).upper()
    reason = str(decision.get("reason") or "").strip().upper().replace("-", "_")
    return tuple(part for part in (action, reason) if part)


def merge_headers(*values: Any) -> dict[str, str]:
    merged: dict[str, str] = {}
    for value in values:
        if value is None:
            continue
        if not isinstance(value, dict):
            raise ValueError("headers must be an object")
        for key, header_value in value.items():
            merged[str(key)] = str(header_value)
    return merged


def normalize_model_limits(provider_name: str, value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("model_limits must be an object keyed by model id")

    result: dict[str, dict[str, Any]] = {}
    for raw_model_id, raw_limit in value.items():
        model_id = normalize_decision_model_id(provider_name, str(raw_model_id).strip())
        if not model_id:
            continue
        if not isinstance(raw_limit, dict):
            raise ValueError(f"model_limits.{raw_model_id} must be an object")
        result[model_id] = raw_limit
    return result


def provider_model_limits(provider: ProviderConfig | None, model_id: str) -> dict[str, Any]:
    if provider is None:
        return {}
    normalized_model_id = normalize_decision_model_id(provider.name, model_id)
    return provider.model_limits.get(normalized_model_id) or provider.model_limits.get(model_id) or {}


def optional_limit_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def model_limit_summary(model: ModelConfig) -> str:
    parts = []
    if model.limit_category:
        parts.append(f"category={model.limit_category}")
    if model.tpm_limit:
        parts.append(f"tpm={model.tpm_limit}")
    if model.rpd_limit:
        parts.append(f"rpd={model.rpd_limit}")
    return " | limits " + " ".join(parts) if parts else ""


def parse_item_filter(raw_item: str) -> tuple[str, str]:
    if ":" not in raw_item:
        raise ValueError(f"item must look like source_id:item_id, got {raw_item!r}")
    source_id, item_id = raw_item.split(":", 1)
    source_id = source_id.strip()
    item_id = item_id.strip()
    if not source_id or not item_id:
        raise ValueError(f"item must look like source_id:item_id, got {raw_item!r}")
    return source_id, item_id


def pick_api_key(raw_config: dict[str, Any], *, require_key: bool, context: str) -> str:
    api_key = str(raw_config.get("api_key") or "").strip()
    api_key_env = str(raw_config.get("api_key_env") or "").strip()
    if not api_key and api_key_env:
        api_key = os.getenv(api_key_env, "").strip()
    if require_key and not api_key:
        raise ValueError(f"{context} has no api_key and api_key_env is empty")
    return api_key


def normalize_auth_type(value: Any) -> str:
    auth_type = str(value or "bearer").strip().lower().replace("_", "-")
    if auth_type in {"bearer", "api-key", "query-key", "none"}:
        return auth_type
    raise ValueError(f"auth_type must be one of bearer|api-key|query-key|none, got {auth_type!r}")


def build_auth_request_parts(config: Any, *, content_type: str | None = None) -> tuple[dict[str, str], dict[str, str]]:
    headers = dict(getattr(config, "extra_headers", {}) or {})
    params: dict[str, str] = {}
    api_key = str(getattr(config, "api_key", "") or "").strip()
    auth_type = normalize_auth_type(getattr(config, "auth_type", "bearer"))

    if content_type:
        headers.setdefault("Content-Type", content_type)

    if auth_type == "bearer" and api_key:
        headers.setdefault("Authorization", f"Bearer {api_key}")
    elif auth_type == "api-key" and api_key:
        header_name = str(getattr(config, "api_key_header", "x-api-key") or "x-api-key")
        headers.setdefault(header_name, api_key)
    elif auth_type == "query-key" and api_key:
        param_name = str(getattr(config, "api_key_query_param", "key") or "key")
        params[param_name] = api_key

    return headers, params


def auth_requires_key(auth_type: str) -> bool:
    return normalize_auth_type(auth_type) != "none"


def load_provider_configs(
    data: dict[str, Any],
    defaults: dict[str, Any],
    *,
    require_keys: bool,
) -> dict[str, ProviderConfig]:
    raw_providers = data.get("providers", {})
    if raw_providers is None:
        raw_providers = {}
    if not isinstance(raw_providers, dict):
        raise ValueError("providers must be an object keyed by provider name")

    providers: dict[str, ProviderConfig] = {}
    for provider_name, raw_provider_value in raw_providers.items():
        name = str(provider_name).strip()
        if not name:
            raise ValueError("provider name must be non-empty")
        raw_provider = read_object(raw_provider_value, f"providers.{name}")
        api_base = str(config_value(raw_provider.get("api_base"), defaults.get("api_base")) or "").strip().rstrip("/")
        if not api_base:
            raise ValueError(f"provider {name!r} requires api_base")

        providers[name] = ProviderConfig(
            name=name,
            api_base=api_base,
            api_key=pick_api_key(raw_provider, require_key=False, context=f"provider {name!r}"),
            auth_type=normalize_auth_type(config_value(raw_provider.get("auth_type"), defaults.get("auth_type"), "bearer")),
            api_key_header=str(config_value(raw_provider.get("api_key_header"), defaults.get("api_key_header"), "x-api-key")),
            api_key_query_param=str(config_value(raw_provider.get("api_key_query_param"), defaults.get("api_key_query_param"), "key")),
            rpm=as_int(config_value(raw_provider.get("rpm"), defaults.get("rpm")), 10),
            timeout_seconds=as_int(config_value(raw_provider.get("timeout_seconds"), defaults.get("timeout_seconds")), 60),
            max_attempts=as_int(config_value(raw_provider.get("max_attempts"), defaults.get("max_attempts")), 8),
            rate_limit_wait_seconds=as_float(
                config_value(raw_provider.get("rate_limit_wait_seconds"), defaults.get("rate_limit_wait_seconds")),
                60.0,
            ),
            max_retry_after_seconds=as_float(
                config_value(raw_provider.get("max_retry_after_seconds"), defaults.get("max_retry_after_seconds")),
                300.0,
            ),
            rate_limit_total_wait_seconds=as_float(
                config_value(raw_provider.get("rate_limit_total_wait_seconds"), defaults.get("rate_limit_total_wait_seconds")),
                300.0,
            ),
            extra_headers=merge_headers(defaults.get("headers"), raw_provider.get("headers")),
            chat_completions_path=normalize_api_path(
                config_value(raw_provider.get("chat_completions_path"), defaults.get("chat_completions_path"), "/chat/completions")
            ),
            models_path=normalize_api_path(config_value(raw_provider.get("models_path"), defaults.get("models_path"), "/models")),
            rate_limit_group=str(raw_provider.get("rate_limit_group") or name).strip() or name,
            concurrent_requests=as_non_negative_int(
                config_value(raw_provider.get("concurrent_requests"), defaults.get("concurrent_requests")),
                0,
            ),
            model_limits=normalize_model_limits(name, raw_provider.get("model_limits")),
        )
    return providers


def load_model_configs(
    config_path: Path,
    *,
    selected_models: set[str],
    require_keys: bool,
    require_models: bool = True,
    decisions: dict[str, dict[str, Any]] | None = None,
    include_hidden_models: bool = False,
) -> tuple[dict[str, ProviderConfig], list[ModelConfig], dict[str, Any], int]:
    data = read_yaml(config_path)
    env_file = str(data.get("env_file") or "").strip()
    if env_file:
        load_dotenv(resolve_path(env_file, base_dir=config_path.parent), override=True)

    defaults = read_object(data.get("defaults", {}), "defaults")
    providers = load_provider_configs(data, defaults, require_keys=require_keys)
    concurrent_models = as_int(config_value(data.get("concurrent_models"), defaults.get("concurrent_models")), 1)

    raw_models = data.get("models", [])
    if not isinstance(raw_models, list):
        raise ValueError("models config must contain a models list")
    if require_models and not raw_models:
        raise ValueError("models config must contain a non-empty models list")

    models: list[ModelConfig] = []
    for index, raw_model in enumerate(raw_models, start=1):
        if not isinstance(raw_model, dict):
            raise ValueError(f"models[{index}] must be an object")

        model = str(raw_model.get("model") or "").strip()
        if not model:
            raise ValueError(f"models[{index}].model is required")
        name = str(raw_model.get("name") or model).strip()
        provider_name = str(raw_model.get("provider") or "").strip() or None
        provider = providers.get(provider_name) if provider_name else None
        if provider_name and provider is None:
            raise ValueError(f"model {name!r} references unknown provider {provider_name!r}")

        if selected_models and name not in selected_models and model not in selected_models:
            continue

        decision = get_model_decision(decisions or {}, provider_name, model)
        if decision_hides_model(decision) and not include_hidden_models:
            print(
                f"[decision] skipping {model_decision_key(provider_name, model)}: "
                f"{decision_action(decision)} {decision.get('reason') or ''}",
                flush=True,
            )
            continue

        api_base = str(config_value(raw_model.get("api_base"), provider.api_base if provider else None, defaults.get("api_base")) or "").strip().rstrip("/")
        if not api_base:
            raise ValueError(f"model {name!r} requires provider or api_base")

        model_api_key = pick_api_key(raw_model, require_key=False, context=f"model {name!r}")
        api_key = model_api_key or (provider.api_key if provider else "")
        auth_type = normalize_auth_type(config_value(raw_model.get("auth_type"), provider.auth_type if provider else None, defaults.get("auth_type"), "bearer"))
        if require_keys and auth_requires_key(auth_type) and not api_key:
            raise ValueError(f"model {name!r} has no api_key/api_key_env and provider has no key")

        max_output_chars = as_int(
            config_value(raw_model.get("max_output_chars"), defaults.get("max_output_chars")),
            220,
        )
        max_tokens = as_int(
            config_value(raw_model.get("max_tokens"), defaults.get("max_tokens")),
            max(max_output_chars * 6, 1000),
        )
        model_limits = provider_model_limits(provider, model)
        provider_group = str(provider.rate_limit_group if provider else provider_name or name).strip() or name
        rate_limit_group = str(
            raw_model.get("rate_limit_group")
            or (f"{provider_group}:{normalize_decision_model_id(provider.name, model)}" if provider and model_limits.get("rpm") is not None else None)
            or provider_group
        ).strip() or name
        concurrent_group = str(raw_model.get("concurrent_group") or provider_group).strip() or rate_limit_group

        models.append(
            ModelConfig(
                name=name,
                model=model,
                provider_name=provider.name if provider else provider_name,
                api_base=api_base,
                api_key=api_key,
                auth_type=auth_type,
                api_key_header=str(
                    config_value(raw_model.get("api_key_header"), provider.api_key_header if provider else None, defaults.get("api_key_header"), "x-api-key")
                ),
                api_key_query_param=str(
                    config_value(
                        raw_model.get("api_key_query_param"),
                        provider.api_key_query_param if provider else None,
                        defaults.get("api_key_query_param"),
                        "key",
                    )
                ),
                rpm=as_int(
                    config_value(raw_model.get("rpm"), model_limits.get("rpm"), provider.rpm if provider else None, defaults.get("rpm")),
                    10,
                ),
                rate_limit_group=rate_limit_group,
                concurrent_group=concurrent_group,
                concurrent_requests=as_non_negative_int(
                    config_value(
                        raw_model.get("concurrent_requests"),
                        provider.concurrent_requests if provider else None,
                        defaults.get("concurrent_requests"),
                    ),
                    0,
                ),
                timeout_seconds=as_int(
                    config_value(raw_model.get("timeout_seconds"), provider.timeout_seconds if provider else None, defaults.get("timeout_seconds")),
                    60,
                ),
                max_tokens=max_tokens,
                max_output_chars=max_output_chars,
                max_input_chars=as_int(config_value(raw_model.get("max_input_chars"), defaults.get("max_input_chars")), 6000),
                target_language=str(config_value(raw_model.get("target_language"), defaults.get("target_language"), "ru")).strip() or "ru",
                temperature=as_float(config_value(raw_model.get("temperature"), defaults.get("temperature")), 0.2),
                max_attempts=as_int(
                    config_value(raw_model.get("max_attempts"), provider.max_attempts if provider else None, defaults.get("max_attempts")),
                    8,
                ),
                rate_limit_wait_seconds=as_float(
                    config_value(
                        raw_model.get("rate_limit_wait_seconds"),
                        provider.rate_limit_wait_seconds if provider else None,
                        defaults.get("rate_limit_wait_seconds"),
                    ),
                    60.0,
                ),
                max_retry_after_seconds=as_float(
                    config_value(
                        raw_model.get("max_retry_after_seconds"),
                        provider.max_retry_after_seconds if provider else None,
                        defaults.get("max_retry_after_seconds"),
                    ),
                    300.0,
                ),
                rate_limit_total_wait_seconds=as_float(
                    config_value(
                        raw_model.get("rate_limit_total_wait_seconds"),
                        provider.rate_limit_total_wait_seconds if provider else None,
                        defaults.get("rate_limit_total_wait_seconds"),
                    ),
                    300.0,
                ),
                extra_headers=merge_headers(defaults.get("headers"), provider.extra_headers if provider else None, raw_model.get("headers")),
                limit_category=optional_limit_text(model_limits.get("category")),
                tpm_limit=optional_limit_text(model_limits.get("tpm")),
                rpd_limit=optional_limit_text(model_limits.get("rpd")),
                chat_completions_path=normalize_api_path(
                    config_value(
                        raw_model.get("chat_completions_path"),
                        provider.chat_completions_path if provider else None,
                        defaults.get("chat_completions_path"),
                        "/chat/completions",
                    )
                ),
            )
        )

    if selected_models and not models:
        raise ValueError(f"selected model(s) not found: {', '.join(sorted(selected_models))}")
    return providers, models, data, concurrent_models


def normalize_api_path(value: Any) -> str:
    path = str(value or "/chat/completions").strip() or "/chat/completions"
    return path if path.startswith("/") else f"/{path}"


def load_release_rows_from_db(
    db_path: Path,
    *,
    limit: int,
    source_ids: set[str],
    item_filters: list[tuple[str, str]],
    chat_id: str | None,
) -> list[ReleaseRow]:
    if item_filters:
        return [ReleaseRow(source_id=source_id, item_id=item_id) for source_id, item_id in item_filters]

    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.row_factory = sqlite3.Row
        params: list[Any] = []
        source_filter = ""
        if source_ids:
            placeholders = ", ".join("?" for _ in source_ids)
            source_filter = f" AND source_id IN ({placeholders})"
            params.extend(sorted(source_ids))

        if chat_id:
            rows = conn.execute(
                f"""
                SELECT source_id, item_id, MAX(last_attempt_at) AS selected_at
                FROM deliveries
                WHERE chat_id = ? AND status = 'sent'{source_filter}
                GROUP BY source_id, item_id
                ORDER BY selected_at DESC
                LIMIT ?
                """,
                [chat_id, *params, limit],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""
                SELECT source_id, item_id, posted_at AS selected_at
                FROM posted_items
                WHERE 1 = 1{source_filter}
                ORDER BY posted_at DESC
                LIMIT ?
                """,
                [*params, limit],
            ).fetchall()
    finally:
        conn.close()

    return [ReleaseRow(str(row["source_id"]), str(row["item_id"]), str(row["selected_at"] or "")) for row in rows]


async def resolve_release_cases(config_path: Path, release_rows: list[ReleaseRow]) -> list[ReleaseCase]:
    source_config = bot.load_config(config_path)
    sources = {str(source["id"]): source for source in source_config["sources"]}

    missing_sources = sorted({row.source_id for row in release_rows if row.source_id not in sources})
    if missing_sources:
        raise ValueError(f"DB references source ids absent from products config: {', '.join(missing_sources)}")

    rows_by_source: dict[str, list[ReleaseRow]] = defaultdict(list)
    for row in release_rows:
        rows_by_source[row.source_id].append(row)

    entries_by_key: dict[tuple[str, str], bot.ChangelogEntry] = {}
    headers = {"User-Agent": "changelog-watch-telegram-bot/model-compare"}
    async with httpx.AsyncClient(timeout=60, headers=headers, follow_redirects=True) as client:
        for source_id, rows in rows_by_source.items():
            wanted_ids = {row.item_id for row in rows}
            source = sources[source_id]
            try:
                entries = await bot.parse_source(client, source)
            except Exception as exc:
                print(f"[source] failed to parse {source_id}: {type(exc).__name__}: {exc}", file=sys.stderr)
                entries = []
            for entry in entries:
                if entry.item_id in wanted_ids:
                    entries_by_key[(source_id, entry.item_id)] = entry

    cases: list[ReleaseCase] = []
    for row in release_rows:
        source = sources[row.source_id]
        entry = entries_by_key.get((row.source_id, row.item_id))
        if entry is None:
            print(f"[source] {row.source_id}:{row.item_id} not found in current source; using empty fallback", file=sys.stderr)
            entry = bot.ChangelogEntry(
                item_id=row.item_id,
                title=row.item_id,
                version=row.item_id,
                date=None,
                body="",
                url=str(source.get("source_url") or source.get("url") or ""),
            )
        cases.append(ReleaseCase(source=source, entry=entry, selected_at=row.selected_at))
    return cases


def build_messages(model: ModelConfig, release: ReleaseCase) -> list[dict[str, str]]:
    summary_input = bot.build_summary_input(release.source, release.entry, model.max_input_chars)
    product = str(release.source.get("product") or release.source["id"])
    version = release.entry.version or release.entry.item_id
    return [
        {
            "role": "system",
            "content": (
                "You summarize software release notes for model comparison. "
                f"Return only the final answer: exactly one short sentence in {model.target_language}. "
                "No reasoning. No markdown. No bullets. No quotes. Focus on practical changes. "
                "Do not include the product name, release name, or version in the answer. "
                "Start directly with the change, preferably with a passive verb such as "
                "'Добавлен', 'Добавлена', 'Добавлены', 'Исправлен', 'Улучшен'."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Make a concise one-line summary of changes in {model.target_language}. "
                f"Not longer than {model.max_output_chars} symbols. "
                "Avoid phrases such as 'this release' and 'the update includes'. "
                f"Do not mention or start with '{product}', '{version}', or '{product} {version}'; "
                "start with the actual change instead.\n\n"
                f"{summary_input}"
            ),
        },
    ]


def parse_retry_after(response: httpx.Response, fallback_seconds: float) -> float:
    raw_retry_after = response.headers.get("Retry-After", "").strip()
    if raw_retry_after:
        try:
            return max(1.0, float(raw_retry_after))
        except ValueError:
            pass
    return fallback_seconds


def value_is_zero(value: Any) -> bool:
    try:
        return float(str(value).strip()) == 0.0
    except (TypeError, ValueError):
        return False


def model_looks_free(item: Any, model_id: str, provider: ProviderConfig | None = None) -> bool:
    if provider and provider.name == "ollama" and "ollama.com" in provider.api_base.lower():
        return True

    lowered_id = model_id.lower()
    if lowered_id.endswith(":free") or lowered_id.endswith("-free") or ":free/" in lowered_id:
        return True
    if not isinstance(item, dict):
        return False

    for key in ("free", "is_free", "free_tier"):
        value = item.get(key)
        if isinstance(value, bool) and value:
            return True
        if isinstance(value, str) and value.strip().lower() in {"1", "true", "yes", "free"}:
            return True

    pricing = item.get("pricing")
    if isinstance(pricing, dict):
        price_values = [pricing.get(key) for key in ("prompt", "completion", "request", "input", "output")]
        present_values = [value for value in price_values if value is not None]
        if present_values and all(value_is_zero(value) for value in present_values):
            return True

    return False


def optional_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_token_usage(data: Any) -> dict[str, int]:
    if not isinstance(data, dict):
        return {}

    usage = data.get("usage")
    native_usage = data.get("usageMetadata")
    if not isinstance(usage, dict):
        usage = native_usage if isinstance(native_usage, dict) else {}

    result: dict[str, int] = {}
    key_pairs = [
        ("prompt_tokens", "prompt_tokens"),
        ("completion_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("promptTokenCount", "prompt_tokens"),
        ("candidatesTokenCount", "completion_tokens"),
        ("totalTokenCount", "total_tokens"),
    ]
    for source_key, target_key in key_pairs:
        parsed = optional_int(usage.get(source_key))
        if parsed is not None:
            result[target_key] = parsed

    if "total_tokens" not in result:
        prompt_tokens = result.get("prompt_tokens", result.get("input_tokens"))
        completion_tokens = result.get("completion_tokens", result.get("output_tokens"))
        if prompt_tokens is not None and completion_tokens is not None:
            result["total_tokens"] = prompt_tokens + completion_tokens
    return result


def model_looks_local(provider: ProviderConfig) -> bool:
    api_base = provider.api_base.lower()
    return provider.auth_type == "none" and (
        "localhost" in api_base or "127.0.0.1" in api_base or "host.docker.internal" in api_base
    )


def model_looks_cloud(provider: ProviderConfig) -> bool:
    return "ollama.com" in provider.api_base.lower()


def extract_model_listings(data: Any, provider: ProviderConfig) -> list[ModelListing]:
    if isinstance(data, dict):
        raw_models = data.get("data") or data.get("models") or data.get("items") or []
    else:
        raw_models = data

    if not isinstance(raw_models, list):
        return []

    listings: list[ModelListing] = []
    for item in raw_models:
        if isinstance(item, dict):
            model_id = item.get("id") or item.get("name") or item.get("model")
            label = item.get("name") or item.get("display_name") or item.get("description")
        else:
            model_id = item
            label = None
        model_text = str(model_id or "").strip()
        if model_text:
            flags: list[str] = []
            if model_looks_free(item, model_text, provider):
                flags.append("FREE")
            if model_looks_cloud(provider):
                flags.append("CLOUD")
            if model_looks_local(provider):
                flags.append("LOCAL")
            listings.append(ModelListing(model_id=model_text, flags=tuple(flags), label=str(label).strip() if label else None))

    unique: dict[str, ModelListing] = {}
    for listing in listings:
        unique.setdefault(listing.model_id, listing)
    return sorted(unique.values(), key=lambda listing: listing.model_id)


def apply_model_decisions_to_listings(
    provider: ProviderConfig,
    listings: list[ModelListing],
    decisions: dict[str, dict[str, Any]],
    *,
    include_hidden_models: bool,
) -> list[ModelListing]:
    filtered: list[ModelListing] = []
    for listing in listings:
        decision = get_model_decision(decisions, provider.name, listing.model_id)
        if decision_hides_model(decision) and not include_hidden_models:
            continue
        flags = tuple(dict.fromkeys([*listing.flags, *decision_flags(decision)]))
        filtered.append(ModelListing(model_id=listing.model_id, flags=flags, label=listing.label))
    return filtered


async def fetch_provider_model_ids(client: httpx.AsyncClient, provider: ProviderConfig) -> list[ModelListing]:
    api_url = f"{provider.api_base}{provider.models_path}"
    if auth_requires_key(provider.auth_type) and not provider.api_key:
        raise ValueError("provider requires an API key")
    headers, params = build_auth_request_parts(provider)
    headers.setdefault("Accept", "application/json")
    limiter = RpmLimiter(provider.rpm)

    rate_limit_waited = 0.0
    for attempt in range(1, provider.max_attempts + 1):
        await limiter.wait()
        response = await client.get(api_url, headers=headers, params=params, timeout=provider.timeout_seconds)
        if response.status_code == 429:
            delay = parse_retry_after(response, provider.rate_limit_wait_seconds)
            if delay > provider.max_retry_after_seconds or rate_limit_waited + delay > provider.rate_limit_total_wait_seconds:
                raise RuntimeError(
                    f"rate limited; retry_after={delay:.1f}s; "
                    f"waited={rate_limit_waited:.1f}s; budget={provider.rate_limit_total_wait_seconds:.1f}s; "
                    f"{compact_error(response.text)}"
                )
            print(
                f"[{provider.name}] 429 while listing models, sleeping {delay:.1f}s "
                f"(attempt {attempt}/{provider.max_attempts})",
                flush=True,
            )
            await asyncio.sleep(delay)
            rate_limit_waited += delay
            continue
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP {response.status_code}: {compact_error(response.text)}")

        return extract_model_listings(response.json(), provider)

    raise RuntimeError(f"rate limit after {provider.max_attempts} attempts")


async def list_provider_models(
    providers: dict[str, ProviderConfig],
    selected_providers: set[str],
    decisions: dict[str, dict[str, Any]],
    *,
    include_hidden_models: bool,
) -> None:
    if not providers:
        raise ValueError("config has no providers")

    provider_names = sorted(selected_providers or providers.keys())
    unknown = [name for name in provider_names if name not in providers]
    if unknown:
        raise ValueError(f"unknown provider(s): {', '.join(unknown)}")

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for provider_name in provider_names:
            provider = providers[provider_name]
            print(f"# {provider.name} ({provider.api_base})")
            try:
                model_listings = await fetch_provider_model_ids(client, provider)
            except Exception as exc:
                print(f"ERROR {type(exc).__name__}: {exc}")
                continue

            model_listings = apply_model_decisions_to_listings(
                provider,
                model_listings,
                decisions,
                include_hidden_models=include_hidden_models,
            )

            if not model_listings:
                print("<no models returned>")
                continue
            for listing in model_listings:
                print(listing.format_line())


async def refresh_provider_model_lists(
    providers: dict[str, ProviderConfig],
    selected_providers: set[str],
    output_dir: Path,
    decisions: dict[str, dict[str, Any]],
    *,
    include_hidden_models: bool,
) -> None:
    if not providers:
        raise ValueError("config has no providers")

    provider_names = sorted(selected_providers or providers.keys())
    unknown = [name for name in provider_names if name not in providers]
    if unknown:
        raise ValueError(f"unknown provider(s): {', '.join(unknown)}")

    output_dir.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for provider_name in provider_names:
            provider = providers[provider_name]
            txt_path = output_dir / f"{provider.name}.txt"
            json_path = output_dir / f"{provider.name}.json"
            print(f"[models] refreshing {provider.name} -> {txt_path}", flush=True)
            try:
                fetched_model_listings = await fetch_provider_model_ids(client, provider)
                model_listings = apply_model_decisions_to_listings(
                    provider,
                    fetched_model_listings,
                    decisions,
                    include_hidden_models=include_hidden_models,
                )
                txt_lines = [f"# {provider.name} ({provider.api_base})"]
                txt_lines.extend(listing.format_line() for listing in model_listings)
                txt_path.write_text("\n".join(txt_lines) + "\n", encoding="utf-8")
                json_path.write_text(
                    json.dumps(
                        {
                            "provider": provider.name,
                            "api_base": provider.api_base,
                            "models": [listing.to_json() for listing in model_listings],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                hidden_count = len(fetched_model_listings) - len(model_listings)
                hidden_text = f", hidden {hidden_count}" if hidden_count else ""
                print(f"[models] wrote {len(model_listings)} model(s) for {provider.name}{hidden_text}", flush=True)
            except Exception as exc:
                error_text = f"ERROR {type(exc).__name__}: {exc}"
                txt_path.write_text(
                    f"# {provider.name} ({provider.api_base})\n{error_text}\n",
                    encoding="utf-8",
                )
                json_path.write_text(
                    json.dumps(
                        {"provider": provider.name, "api_base": provider.api_base, "error": error_text, "models": []},
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                print(f"[models] {provider.name}: {error_text}", flush=True)


def extract_content(data: Any) -> tuple[str | None, str | None, list[str]]:
    if not isinstance(data, dict):
        return None, None, []

    choices = data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return None, None, []

    first_choice = choices[0]
    finish_reason = first_choice.get("finish_reason")
    message = first_choice.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        message_keys = sorted(str(key) for key in message.keys())
    else:
        content = first_choice.get("text")
        message_keys = []

    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text") or item.get("content")
                if text:
                    parts.append(str(text))
            elif item:
                parts.append(str(item))
        content = "\n".join(parts)

    if content is None:
        return None, str(finish_reason) if finish_reason is not None else None, message_keys
    return str(content), str(finish_reason) if finish_reason is not None else None, message_keys


def compact_error(text: str, limit: int = 220) -> str:
    return bot.truncate(re.sub(r"\s+", " ", text).strip(), limit)


def strip_closed_reasoning_blocks(text: str) -> str:
    cleaned = re.sub(r"(?is)<(?:think|thought)>.*?</(?:think|thought)>", " ", text).strip()
    return cleaned or text.strip()


def is_reasoning_only(text: str) -> bool:
    return text.lstrip().lower().startswith(("<think>", "<thought>"))


def strip_release_lead_in(summary: str, release: ReleaseCase) -> str:
    product = str(release.source.get("product") or release.source["id"])
    version = str(release.entry.version or release.entry.item_id or "")
    version_body = version[1:] if version.lower().startswith("v") else version
    product_pattern = re.escape(product)
    version_pattern = rf"v?{re.escape(version_body)}" if version_body else ""

    patterns = []
    if version_pattern:
        patterns.extend(
            [
                rf"(?i)^(?:{product_pattern}\s+)?{version_pattern}\s*(?:[:—–-]\s*|\s+)",
                rf"(?i)^в\s+{version_pattern}\s+",
            ]
        )
    patterns.append(rf"(?i)^{product_pattern}\s*(?:[:—–-]\s*|\s+)")

    cleaned = summary.strip()
    for pattern in patterns:
        cleaned = re.sub(pattern, "", cleaned, count=1).strip()

    verb_rewrites = {
        "добавляет": "Добавлен",
        "включает": "Добавлен",
        "улучшает": "Улучшен",
        "исправляет": "Исправлен",
        "обновляет": "Обновлен",
    }
    lowered = cleaned.lower()
    for active_verb, passive_verb in verb_rewrites.items():
        if lowered.startswith(active_verb + " "):
            return passive_verb + cleaned[len(active_verb):]

    return cleaned[:1].upper() + cleaned[1:] if cleaned else cleaned


async def generate_summary(
    client: httpx.AsyncClient,
    model: ModelConfig,
    release: ReleaseCase,
    limiter: RpmLimiter,
) -> SummaryResult:
    api_url = f"{model.api_base}{model.chat_completions_path}"
    if auth_requires_key(model.auth_type) and not model.api_key:
        return SummaryResult("ERROR missing API key", {})
    headers, params = build_auth_request_parts(model, content_type="application/json")
    payload = {
        "model": model.model,
        "stream": False,
        "temperature": model.temperature,
        "max_tokens": model.max_tokens,
        "messages": build_messages(model, release),
    }

    rate_limit_waited = 0.0
    for attempt in range(1, model.max_attempts + 1):
        await limiter.wait()
        try:
            response = await client.post(api_url, headers=headers, params=params, json=payload, timeout=model.timeout_seconds)
        except Exception as exc:
            if attempt >= model.max_attempts:
                return SummaryResult(f"ERROR request: {type(exc).__name__}: {exc}", {})
            delay = min(model.rate_limit_wait_seconds, 2 ** attempt)
            print(f"[{model.column_title}] request failed, sleeping {delay:.1f}s: {exc}", flush=True)
            await asyncio.sleep(delay)
            continue

        if response.status_code == 429:
            delay = parse_retry_after(response, model.rate_limit_wait_seconds)
            if delay > model.max_retry_after_seconds or rate_limit_waited + delay > model.rate_limit_total_wait_seconds:
                return SummaryResult(
                    f"RATE_LIMIT retry_after={delay:.1f}s waited={rate_limit_waited:.1f}s "
                    f"budget={model.rate_limit_total_wait_seconds:.1f}s: {compact_error(response.text)}",
                    {},
                )
            print(
                f"[{model.column_title}] 429 rate limit on {release.key[0]}:{release.key[1]}, "
                f"sleeping {delay:.1f}s (attempt {attempt}/{model.max_attempts})",
                flush=True,
            )
            await asyncio.sleep(delay)
            rate_limit_waited += delay
            continue

        if response.status_code >= 400:
            return SummaryResult(f"ERROR HTTP {response.status_code}: {compact_error(response.text)}", {})

        try:
            data = response.json()
        except ValueError:
            return SummaryResult(f"ERROR invalid JSON: {compact_error(response.text)}", {})

        usage = extract_token_usage(data)
        content, finish_reason, message_keys = extract_content(data)
        if content is None:
            return SummaryResult(f"EMPTY content; finish_reason={finish_reason!r}; message_keys={message_keys}", usage)

        content = strip_closed_reasoning_blocks(content)
        if is_reasoning_only(content):
            return SummaryResult(f"EMPTY reasoning-only content; raw={compact_error(content)!r}", usage)

        summary = bot.clean_one_line_summary(content, max_len=model.max_output_chars)
        summary = strip_release_lead_in(summary, release)
        if not summary:
            return SummaryResult(f"EMPTY cleaned content; finish_reason={finish_reason!r}; raw={compact_error(content)!r}", usage)
        return SummaryResult(summary, usage)

    return SummaryResult(f"ERROR rate limit after {model.max_attempts} attempts", {})


def markdown_cell(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    text = text.replace("|", "\\|")
    return text or "-"


def result_status_label(result: SummaryResult | None) -> str:
    if result is None:
        return "🔴 Error/Danger"

    text = result.text.strip()
    upper_text = text.upper()
    rate_limit_markers = (
        "RATE_LIMIT",
        "RATE LIMIT",
        "ERROR RATE LIMIT",
        "ERROR HTTP 429",
        "QUOTA",
        "TOO MANY CONCURRENT REQUESTS",
    )
    warning_markers = (
        "EMPTY ",
        "ERROR HTTP 402",
        "ERROR HTTP 503",
        "INSUFFICIENT CREDITS",
        "SPEND LIMIT",
        "BILLING",
        "SUBSCRIPTION",
        "UPGRADE",
        "HIGH DEMAND",
        "UNAVAILABLE",
    )
    if upper_text.startswith(rate_limit_markers) or any(marker in upper_text for marker in rate_limit_markers[3:]):
        return "⚠️ Rate Limit"
    if upper_text.startswith(warning_markers) or any(marker in upper_text for marker in warning_markers[3:]):
        return "🟡 Warning/Caution"
    if upper_text.startswith("ERROR"):
        return "🔴 Error/Danger"
    return "🔵 Active/Success"


def result_cell(result: SummaryResult | None) -> str:
    if result is None:
        return f"{result_status_label(result)}<br>-"

    status = result_status_label(result)
    usage_text = result.usage_text()
    if usage_text:
        return f"{status}<br>{markdown_cell(result.text)}<br><small>{markdown_cell(usage_text)}</small>"
    return f"{status}<br>{markdown_cell(result.text)}"


def release_label(release: ReleaseCase) -> str:
    product = str(release.source.get("product") or release.source["id"])
    version = release.entry.version or release.entry.item_id
    return f"{product} {version}"


def release_description(release: ReleaseCase, *, max_chars: int) -> str:
    source = release.source
    entry = release.entry
    product = str(source.get("product") or source["id"])
    title = entry.title if entry.title and entry.title != entry.version else ""
    body = bot.compact_markdown_for_telegram(entry.body or "")
    body = bot.truncate(body, max_chars)
    parts = [f"{product} {entry.version}"]
    if title:
        parts.append(title)
    if entry.date:
        parts.append(str(entry.date))
    if body:
        parts.append(body)
    return "<br>".join(markdown_cell(part) for part in parts)


def build_markdown_table(
    releases: list[ReleaseCase],
    models: list[ModelConfig],
    results: dict[tuple[tuple[str, str], str], SummaryResult],
    *,
    row_description_chars: int,
    db_path: Path,
) -> str:
    generated_at = datetime.now(timezone.utc).isoformat()
    lines = [
        "# AI Model Summary Comparison",
        "",
        f"Generated at: `{generated_at}`",
        f"DB: `{db_path}`",
        "",
        "## Описания релизов",
        "",
        "| Релиз | Описание |",
        "|---|---|",
    ]

    for release in releases:
        lines.append(
            "| "
            + markdown_cell(release_label(release))
            + " | "
            + release_description(release, max_chars=row_description_chars)
            + " |"
        )

    lines.extend(["", "## Ответы моделей", ""])
    release_headers = ["Ответ"] if len(releases) == 1 else [release_label(release) for release in releases]
    lines.extend(
        [
            "| Модель | " + " | ".join(markdown_cell(header) for header in release_headers) + " |",
            "|---|" + "---|" * len(releases),
        ]
    )

    for model in models:
        cells = [markdown_cell(model.column_title)]
        for release in releases:
            cells.append(result_cell(results.get((release.key, model.column_title))))
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")
    return "\n".join(lines)


async def run_comparison(
    releases: list[ReleaseCase],
    models: list[ModelConfig],
    *,
    concurrent_models: int,
) -> dict[tuple[tuple[str, str], str], SummaryResult]:
    results: dict[tuple[tuple[str, str], str], SummaryResult] = {}
    limiters: dict[str, RpmLimiter] = {}
    group_concurrent_requests: dict[str, int] = {}
    for model in models:
        existing = limiters.get(model.rate_limit_group)
        if existing is None or model.rpm < existing.rpm:
            limiters[model.rate_limit_group] = RpmLimiter(model.rpm)
        if model.concurrent_requests > 0:
            existing_concurrency = group_concurrent_requests.get(model.concurrent_group)
            if existing_concurrency is None or model.concurrent_requests < existing_concurrency:
                group_concurrent_requests[model.concurrent_group] = model.concurrent_requests

    semaphore = asyncio.Semaphore(max(1, concurrent_models))
    group_semaphores = {
        group: asyncio.Semaphore(max(1, value))
        for group, value in group_concurrent_requests.items()
    }

    async def run_one(client: httpx.AsyncClient, release: ReleaseCase, model: ModelConfig) -> tuple[tuple[str, str], str, SummaryResult]:
        async with semaphore:
            group_semaphore = group_semaphores.get(model.concurrent_group)
            if group_semaphore:
                async with group_semaphore:
                    print(f"  [model] {model.column_title}", flush=True)
                    summary = await generate_summary(client, model, release, limiters[model.rate_limit_group])
                    return release.key, model.column_title, summary
            print(f"  [model] {model.column_title}", flush=True)
            summary = await generate_summary(client, model, release, limiters[model.rate_limit_group])
            return release.key, model.column_title, summary

    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for release in releases:
            print(f"[release] {release.key[0]}:{release.key[1]}", flush=True)
            tasks = [asyncio.create_task(run_one(client, release, model)) for model in models]
            for task in asyncio.as_completed(tasks):
                release_key, model_title, summary = await task
                results[(release_key, model_title)] = summary
                usage_text = f" ({summary.usage_text()})" if summary.usage_text() else ""
                print(f"  [done] {model_title}: {bot.truncate(summary.text, 140)}{usage_text}", flush=True)
    return results


def print_release_list(releases: list[ReleaseCase]) -> None:
    for release in releases:
        product = str(release.source.get("product") or release.source["id"])
        print(f"{release.key[0]}:{release.key[1]} | {product} | {release.entry.title} | {release.selected_at or '-'}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare release-note summaries across AI models")
    parser.add_argument("--models-config", default=DEFAULT_MODELS_CONFIG, help="YAML config with model endpoints and keys")
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "products.yaml"), help="products.yaml path")
    parser.add_argument("--db", default=os.getenv("DB_PATH", "data/posted.sqlite3"), help="SQLite state DB path")
    parser.add_argument("--output", help=f"Markdown output path, default from config or {DEFAULT_OUTPUT}")
    parser.add_argument("--model-lists-dir", default=DEFAULT_MODEL_LISTS_DIR, help="Directory for cached provider model lists")
    parser.add_argument("--model-decisions", default=DEFAULT_MODEL_DECISIONS, help="YAML file with persistent model skip/retry decisions")
    parser.add_argument("--limit", type=int, help="Number of recent releases to compare")
    parser.add_argument("--source-id", action="append", default=[], help="Limit releases to source id; repeatable")
    parser.add_argument("--item", action="append", default=[], help="Explicit release as source_id:item_id; repeatable")
    parser.add_argument("--model", action="append", default=[], help="Run only model name/id from config; repeatable")
    parser.add_argument("--provider", action="append", default=[], help="Provider name for --list-provider-models; repeatable")
    parser.add_argument("--concurrent-models", type=int, help="Concurrent model calls; default from config, otherwise 1")
    parser.add_argument("--chat-id", help="Select recent sent releases for one chat from deliveries")
    parser.add_argument("--list-provider-models", action="store_true", help="List model ids returned by configured providers")
    parser.add_argument("--refresh-model-lists", action="store_true", help="Fetch provider model lists and write them to files")
    parser.add_argument("--include-hidden-models", action="store_true", help="Include models hidden by model decisions")
    parser.add_argument("--list-items", action="store_true", help="Only list selected DB releases; no model calls")
    parser.add_argument("--dry-run", action="store_true", help="Show selected releases/models without calling model APIs")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")

    models_config_path = resolve_path(args.models_config)
    if not models_config_path.exists() and not args.list_items:
        print(
            f"models config not found: {models_config_path}\n"
            "Copy scripts/model-summary-compare.example.yaml to "
            "scripts/model-summary-compare.local.yaml and fill model keys.",
            file=sys.stderr,
        )
        return 2

    model_data: dict[str, Any] = {}
    providers: dict[str, ProviderConfig] = {}
    models: list[ModelConfig] = []
    config_concurrent_models = 1
    decisions = load_model_decisions(resolve_path(args.model_decisions))
    if models_config_path.exists():
        providers, models, model_data, config_concurrent_models = load_model_configs(
            models_config_path,
            selected_models=set(args.model),
            require_keys=not args.dry_run and not args.list_items,
            require_models=not args.list_provider_models and not args.refresh_model_lists,
            decisions=decisions,
            include_hidden_models=args.include_hidden_models,
        )

    if args.list_provider_models:
        await list_provider_models(
            providers,
            set(args.provider),
            decisions,
            include_hidden_models=args.include_hidden_models,
        )
        return 0

    if args.refresh_model_lists:
        await refresh_provider_model_lists(
            providers,
            set(args.provider),
            resolve_path(args.model_lists_dir),
            decisions,
            include_hidden_models=args.include_hidden_models,
        )
        return 0

    items_config = model_data.get("items", {}) if isinstance(model_data.get("items", {}), dict) else {}
    default_limit = as_int(items_config.get("limit"), 5)
    limit = args.limit if args.limit and args.limit > 0 else default_limit
    source_ids = set(as_str_list(items_config.get("source_ids"))) | set(args.source_id)
    item_filters = [parse_item_filter(item) for item in [*as_str_list(items_config.get("item_ids")), *args.item]]
    chat_id = args.chat_id or str(items_config.get("chat_id") or "").strip() or None

    release_rows = load_release_rows_from_db(
        resolve_path(args.db),
        limit=limit,
        source_ids=source_ids,
        item_filters=item_filters,
        chat_id=chat_id,
    )
    if not release_rows:
        print("no release rows selected from DB", file=sys.stderr)
        return 1

    releases = await resolve_release_cases(resolve_path(args.config), release_rows)
    if args.list_items:
        print_release_list(releases)
        return 0

    if args.dry_run:
        print("Selected releases:")
        print_release_list(releases)
        concurrent_models = args.concurrent_models if args.concurrent_models and args.concurrent_models > 0 else config_concurrent_models
        print(f"Concurrent models: {concurrent_models}")
        if providers:
            print("Configured providers:")
            for provider in providers.values():
                print(f"{provider.name} | rpm={provider.rpm} | api_base={provider.api_base}")
        print("Selected models:")
        for model in models:
            provider_label = model.provider_name or "<model override>"
            print(
                f"{model.column_title} | {model.model} | provider={provider_label} | "
                f"rpm={model.rpm} | max_tokens={model.max_tokens} | "
                f"rate_limit_wait_budget={model.rate_limit_total_wait_seconds:.1f}s | "
                f"rate_group={model.rate_limit_group} | concurrent_group={model.concurrent_group} | "
                f"concurrent_requests={model.concurrent_requests or '-'}{model_limit_summary(model)} | api_base={model.api_base}"
            )
        return 0

    row_description_chars = as_int(model_data.get("row_description_chars"), 700)
    output_path = resolve_path(str(args.output or model_data.get("output") or DEFAULT_OUTPUT))

    concurrent_models = args.concurrent_models if args.concurrent_models and args.concurrent_models > 0 else config_concurrent_models
    results = await run_comparison(releases, models, concurrent_models=concurrent_models)
    output = build_markdown_table(
        releases,
        models,
        results,
        row_description_chars=row_description_chars,
        db_path=resolve_path(args.db),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(output, encoding="utf-8")
    print(f"wrote {output_path}")
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(async_main()))


if __name__ == "__main__":
    main()
