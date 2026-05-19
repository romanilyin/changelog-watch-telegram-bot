#!/usr/bin/env python3
"""Compare one-line AI summaries across multiple models.

The script takes release ids from SQLite state, reconstructs release notes from
products.yaml sources, calls configured OpenAI-compatible chat completion APIs,
and writes a Markdown comparison table.
"""

from __future__ import annotations

import argparse
import asyncio
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
class ModelConfig:
    name: str
    model: str
    api_base: str
    api_key: str
    rpm: int
    rate_limit_group: str
    timeout_seconds: int
    max_tokens: int
    max_output_chars: int
    max_input_chars: int
    target_language: str
    temperature: float
    max_attempts: int
    rate_limit_wait_seconds: float
    extra_headers: dict[str, str]
    chat_completions_path: str = "/chat/completions"

    @property
    def column_title(self) -> str:
        return self.name or self.model


class RpmLimiter:
    def __init__(self, rpm: int) -> None:
        self.rpm = max(1, int(rpm))
        self.timestamps: deque[float] = deque()

    async def wait(self) -> None:
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


def as_str_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    return [text] if text else []


def parse_item_filter(raw_item: str) -> tuple[str, str]:
    if ":" not in raw_item:
        raise ValueError(f"item must look like source_id:item_id, got {raw_item!r}")
    source_id, item_id = raw_item.split(":", 1)
    source_id = source_id.strip()
    item_id = item_id.strip()
    if not source_id or not item_id:
        raise ValueError(f"item must look like source_id:item_id, got {raw_item!r}")
    return source_id, item_id


def pick_api_key(raw_model: dict[str, Any], *, require_key: bool) -> str:
    api_key = str(raw_model.get("api_key") or "").strip()
    api_key_env = str(raw_model.get("api_key_env") or "").strip()
    if not api_key and api_key_env:
        api_key = os.getenv(api_key_env, "").strip()
    if require_key and not api_key:
        name = raw_model.get("name") or raw_model.get("model") or "<unnamed>"
        raise ValueError(f"model {name!r} has no api_key and api_key_env is empty")
    return api_key


def load_model_configs(config_path: Path, *, selected_models: set[str], require_keys: bool) -> tuple[list[ModelConfig], dict[str, Any]]:
    data = read_yaml(config_path)
    env_file = str(data.get("env_file") or "").strip()
    if env_file:
        load_dotenv(resolve_path(env_file, base_dir=config_path.parent), override=True)

    defaults = data.get("defaults", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ValueError("defaults must be an object")

    raw_models = data.get("models", [])
    if not isinstance(raw_models, list) or not raw_models:
        raise ValueError("models config must contain a non-empty models list")

    models: list[ModelConfig] = []
    for index, raw_model in enumerate(raw_models, start=1):
        if not isinstance(raw_model, dict):
            raise ValueError(f"models[{index}] must be an object")

        model = str(raw_model.get("model") or "").strip()
        if not model:
            raise ValueError(f"models[{index}].model is required")
        name = str(raw_model.get("name") or model).strip()

        if selected_models and name not in selected_models and model not in selected_models:
            continue

        api_base = str(raw_model.get("api_base") or defaults.get("api_base") or "").strip().rstrip("/")
        if not api_base:
            raise ValueError(f"model {name!r} requires api_base")

        extra_headers = raw_model.get("headers") or defaults.get("headers") or {}
        if not isinstance(extra_headers, dict):
            raise ValueError(f"model {name!r} headers must be an object")

        max_output_chars = as_int(raw_model.get("max_output_chars", defaults.get("max_output_chars")), 220)
        max_tokens = as_int(
            raw_model.get("max_tokens", defaults.get("max_tokens")),
            max(max_output_chars * 6, 1000),
        )

        models.append(
            ModelConfig(
                name=name,
                model=model,
                api_base=api_base,
                api_key=pick_api_key(raw_model, require_key=require_keys),
                rpm=as_int(raw_model.get("rpm", defaults.get("rpm")), 10),
                rate_limit_group=str(raw_model.get("rate_limit_group") or raw_model.get("provider") or name).strip() or name,
                timeout_seconds=as_int(raw_model.get("timeout_seconds", defaults.get("timeout_seconds")), 60),
                max_tokens=max_tokens,
                max_output_chars=max_output_chars,
                max_input_chars=as_int(raw_model.get("max_input_chars", defaults.get("max_input_chars")), 6000),
                target_language=str(raw_model.get("target_language", defaults.get("target_language", "ru"))).strip() or "ru",
                temperature=as_float(raw_model.get("temperature", defaults.get("temperature")), 0.2),
                max_attempts=as_int(raw_model.get("max_attempts", defaults.get("max_attempts")), 8),
                rate_limit_wait_seconds=as_float(
                    raw_model.get("rate_limit_wait_seconds", defaults.get("rate_limit_wait_seconds")),
                    60.0,
                ),
                extra_headers={str(key): str(value) for key, value in extra_headers.items()},
                chat_completions_path=normalize_api_path(
                    raw_model.get("chat_completions_path", defaults.get("chat_completions_path", "/chat/completions"))
                ),
            )
        )

    if selected_models and not models:
        raise ValueError(f"selected model(s) not found: {', '.join(sorted(selected_models))}")
    return models, data


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
    return [
        {
            "role": "system",
            "content": (
                "You summarize software release notes for model comparison. "
                f"Return only the final answer: exactly one short sentence in {model.target_language}. "
                "No reasoning. No markdown. No bullets. No quotes. Focus on practical changes."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Make a concise one-line summary of changes in {model.target_language}. "
                f"Not longer than {model.max_output_chars} symbols. "
                "Avoid phrases such as 'this release' and 'the update includes'.\n\n"
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


async def generate_summary(
    client: httpx.AsyncClient,
    model: ModelConfig,
    release: ReleaseCase,
    limiter: RpmLimiter,
) -> str:
    api_url = f"{model.api_base}{model.chat_completions_path}"
    headers = {
        "Authorization": f"Bearer {model.api_key}",
        "Content-Type": "application/json",
        **model.extra_headers,
    }
    payload = {
        "model": model.model,
        "stream": False,
        "temperature": model.temperature,
        "max_tokens": model.max_tokens,
        "messages": build_messages(model, release),
    }

    for attempt in range(1, model.max_attempts + 1):
        await limiter.wait()
        try:
            response = await client.post(api_url, headers=headers, json=payload, timeout=model.timeout_seconds)
        except Exception as exc:
            if attempt >= model.max_attempts:
                return f"ERROR request: {type(exc).__name__}: {exc}"
            delay = min(model.rate_limit_wait_seconds, 2 ** attempt)
            print(f"[{model.column_title}] request failed, sleeping {delay:.1f}s: {exc}", flush=True)
            await asyncio.sleep(delay)
            continue

        if response.status_code == 429:
            delay = parse_retry_after(response, model.rate_limit_wait_seconds)
            print(
                f"[{model.column_title}] 429 rate limit on {release.key[0]}:{release.key[1]}, "
                f"sleeping {delay:.1f}s (attempt {attempt}/{model.max_attempts})",
                flush=True,
            )
            await asyncio.sleep(delay)
            continue

        if response.status_code >= 400:
            return f"ERROR HTTP {response.status_code}: {compact_error(response.text)}"

        try:
            data = response.json()
        except ValueError:
            return f"ERROR invalid JSON: {compact_error(response.text)}"

        content, finish_reason, message_keys = extract_content(data)
        if content is None:
            return f"EMPTY content; finish_reason={finish_reason!r}; message_keys={message_keys}"

        summary = bot.clean_one_line_summary(content, max_len=model.max_output_chars)
        if not summary:
            return f"EMPTY cleaned content; finish_reason={finish_reason!r}; raw={compact_error(content)!r}"
        return summary

    return f"ERROR rate limit after {model.max_attempts} attempts"


def markdown_cell(text: str) -> str:
    text = re.sub(r"\s+", " ", str(text)).strip()
    text = text.replace("|", "\\|")
    return text or "-"


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
    results: dict[tuple[tuple[str, str], str], str],
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
        "| Описание релиза | " + " | ".join(markdown_cell(model.column_title) for model in models) + " |",
        "|---|" + "---|" * len(models),
    ]

    for release in releases:
        cells = [release_description(release, max_chars=row_description_chars)]
        for model in models:
            cells.append(markdown_cell(results.get((release.key, model.column_title), "")))
        lines.append("| " + " | ".join(cells) + " |")

    lines.append("")
    return "\n".join(lines)


async def run_comparison(
    releases: list[ReleaseCase],
    models: list[ModelConfig],
) -> dict[tuple[tuple[str, str], str], str]:
    results: dict[tuple[tuple[str, str], str], str] = {}
    limiters: dict[str, RpmLimiter] = {}
    for model in models:
        existing = limiters.get(model.rate_limit_group)
        if existing is None or model.rpm < existing.rpm:
            limiters[model.rate_limit_group] = RpmLimiter(model.rpm)
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for release in releases:
            print(f"[release] {release.key[0]}:{release.key[1]}", flush=True)
            for model in models:
                print(f"  [model] {model.column_title}", flush=True)
                summary = await generate_summary(client, model, release, limiters[model.rate_limit_group])
                results[(release.key, model.column_title)] = summary
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
    parser.add_argument("--limit", type=int, help="Number of recent releases to compare")
    parser.add_argument("--source-id", action="append", default=[], help="Limit releases to source id; repeatable")
    parser.add_argument("--item", action="append", default=[], help="Explicit release as source_id:item_id; repeatable")
    parser.add_argument("--model", action="append", default=[], help="Run only model name/id from config; repeatable")
    parser.add_argument("--chat-id", help="Select recent sent releases for one chat from deliveries")
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
    models: list[ModelConfig] = []
    if models_config_path.exists():
        models, model_data = load_model_configs(
            models_config_path,
            selected_models=set(args.model),
            require_keys=not args.dry_run and not args.list_items,
        )

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
        print("Selected models:")
        for model in models:
            print(f"{model.column_title} | {model.model} | rpm={model.rpm} | api_base={model.api_base}")
        return 0

    row_description_chars = as_int(model_data.get("row_description_chars"), 700)
    output_path = resolve_path(str(args.output or model_data.get("output") or DEFAULT_OUTPUT))

    results = await run_comparison(releases, models)
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
