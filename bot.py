#!/usr/bin/env python3
"""Telegram changelog watcher.

Checks configured product changelog/release sources and posts new entries to Telegram.
Supported source types:
  - html_changelog       generic HTML changelog with version headings/links
  - markdown_changelog   CHANGELOG.md with ## [version] - YYYY-MM-DD headings
  - github_releases      GitHub releases page / repository URL
"""

from __future__ import annotations

import argparse
import asyncio
import html
import logging
import errno
import hashlib
import os
import re
import secrets
import signal
import tempfile
from collections import defaultdict
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import fcntl

LOG = logging.getLogger("changelog-watch-bot")
PROJECT_ROOT = Path(__file__).resolve().parent

DEFAULT_VERSION_RE = r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$"
MD_VERSION_HEADING_RE = re.compile(
    r"^##\s+\[?(?P<version>[^\]\n]+)\]?\s*(?:-\s*(?P<date>\d{4}-\d{2}-\d{2}))?\s*$",
    re.MULTILINE,
)
SUMMARY_TIME_RE = re.compile(r"^(?P<hour>\d{1,2}):(?P<minute>[0-5]\d)$")
WEEKDAY_NAMES = {
    "monday": 0,
    "mon": 0,
    "понедельник": 0,
    "пн": 0,
    "tuesday": 1,
    "tue": 1,
    "вторник": 1,
    "вт": 1,
    "wednesday": 2,
    "wed": 2,
    "среда": 2,
    "ср": 2,
    "thursday": 3,
    "thu": 3,
    "thurs": 3,
    "четверг": 3,
    "чт": 3,
    "friday": 4,
    "fri": 4,
    "пятница": 4,
    "пт": 4,
    "saturday": 5,
    "sat": 5,
    "суббота": 5,
    "сб": 5,
    "sunday": 6,
    "sun": 6,
    "воскресенье": 6,
    "вс": 6,
}

DEFAULT_DISPLAY_TIMEZONE = "Europe/Amsterdam"
_display_tz_name = DEFAULT_DISPLAY_TIMEZONE
_display_tz = ZoneInfo(DEFAULT_DISPLAY_TIMEZONE)
_duplicate_instances_alert_sent = False
_single_instance_lock_fd: int | None = None
_single_instance_lock_path: Path | None = None
_DUPLICATE_INSTANCE_ALERT_COOLDOWN_SECONDS = 60
_TELEGRAM_BOT_TOKEN_IN_URL_RE = re.compile(r"/bot(?P<bot_id>\d+):(?P<secret>[^/\s\"']+)")


def mask_telegram_bot_token(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        bot_id = match.group("bot_id")
        visible_digits = bot_id[-4:] if len(bot_id) > 4 else bot_id
        return f"/bot***{visible_digits}:<redacted>"

    return _TELEGRAM_BOT_TOKEN_IN_URL_RE.sub(replace, text)


class SecretMaskingLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = mask_telegram_bot_token(record.msg)
        if record.args:
            record.args = self._mask_args(record.args)
        return True

    def _mask_args(self, args: Any) -> Any:
        if isinstance(args, tuple):
            return tuple(self._mask_args(arg) for arg in args)
        if isinstance(args, dict):
            return {key: self._mask_args(value) for key, value in args.items()}
        if isinstance(args, str):
            return mask_telegram_bot_token(args)
        text = str(args)
        if "/bot" in text:
            return mask_telegram_bot_token(text)
        return args


def env_text(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def parse_bool_value(value: Any, context: str, *, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{context} must be a boolean value")


def get_routing_config_path() -> str | None:
    return env_text("ROUTING_CONFIG_PATH")


def get_routing_seed_mode() -> str:
    mode = os.getenv("ROUTING_SEED_MODE", "once").strip().lower() or "once"
    if mode not in {"once", "sync", "off"}:
        raise RuntimeError("ROUTING_SEED_MODE must be one of once|sync|off")
    return mode


def lifecycle_notifications_are_enabled() -> bool:
    return env_bool("LIFECYCLE_NOTIFICATIONS_ENABLED", False)


def duplicate_instance_notifications_are_enabled() -> bool:
    return env_bool("DUPLICATE_INSTANCE_NOTIFICATIONS_ENABLED", True)


def ai_summary_dry_run_call_api_enabled() -> bool:
    return env_bool("AI_SUMMARY_DRY_RUN_CALL_API", False)


def ai_summary_in_digest_enabled() -> bool:
    return env_bool("AI_SUMMARY_IN_DIGEST", True)


def summary_queue_prune_stale_enabled() -> bool:
    return env_bool("SUMMARY_QUEUE_PRUNE_STALE", False)


def default_single_instance_lock_path() -> Path:
    script_path = Path(__file__).resolve()
    lock_suffix = hashlib.sha1(script_path.as_posix().encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / f"changelog-watch-telegram-bot-{lock_suffix}.lock"


def resolve_instance_lock_path(lock_path: str | None = None) -> Path:
    configured_path = (lock_path or "").strip()
    if not configured_path:
        return default_single_instance_lock_path()

    path = Path(configured_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def read_single_instance_lock_pid(lock_path: Path) -> int | None:
    try:
        content = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return None

    if not content:
        return None

    first_line = content.splitlines()[0].strip()
    try:
        return int(first_line)
    except ValueError:
        return None


def acquire_single_instance_lock(lock_path: str | None = None) -> tuple[bool, int | None]:
    global _single_instance_lock_fd, _single_instance_lock_path

    if _single_instance_lock_fd is not None:
        return True, os.getpid()

    path = resolve_instance_lock_path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    lock_fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(lock_fd)
        if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
            return False, read_single_instance_lock_pid(path)
        raise

    _single_instance_lock_fd = lock_fd
    _single_instance_lock_path = path
    os.ftruncate(lock_fd, 0)
    os.lseek(lock_fd, 0, os.SEEK_SET)
    os.write(lock_fd, f"{os.getpid()}\n".encode("utf-8"))
    os.fsync(lock_fd)
    return True, os.getpid()


def release_single_instance_lock() -> None:
    global _single_instance_lock_fd, _single_instance_lock_path

    if _single_instance_lock_fd is None:
        return

    lock_path = _single_instance_lock_path
    try:
        fcntl.flock(_single_instance_lock_fd, fcntl.LOCK_UN)
    except OSError:
        LOG.debug("failed to unlock single-instance lock file")

    try:
        os.close(_single_instance_lock_fd)
    except OSError:
        LOG.debug("failed to close single-instance lock fd")
    _single_instance_lock_fd = None

    if lock_path is not None and lock_path.exists():
        try:
            lock_path.unlink()
        except OSError:
            LOG.debug("failed to remove single-instance lock file")
    _single_instance_lock_path = None


def load_admin_ids_for_lock_alert(routing_config_path: str | None) -> set[str]:
    path = routing_config_path or get_routing_config_path()
    if not path:
        return set()
    try:
        routing_data = load_routing_yaml(path)
    except Exception:
        LOG.debug("failed to load routing config for lock alert", exc_info=True)
        return set()

    raw_admins = routing_data.get("admins", [])
    if not isinstance(raw_admins, list):
        return set()

    admins: set[str] = set()
    for idx, raw_admin in enumerate(raw_admins, start=1):
        try:
            admin_id, _ = parse_admin_entry(raw_admin, idx)
        except Exception:
            LOG.debug("invalid admin entry in routing config at index %d", idx, exc_info=True)
            continue
        admins.add(admin_id)
    return admins


def _instance_alert_state_file(kind: str, lock_path: str | None = None) -> Path:
    base_path = resolve_instance_lock_path(lock_path)
    return base_path.parent / f"{base_path.name}.{kind}.alert"


def _should_send_alert_with_cooldown(state_file: Path, marker: str, *, cooldown_seconds: int) -> bool:
    now_ts = int(datetime.now(timezone.utc).timestamp())

    try:
        raw = state_file.read_text(encoding="utf-8").strip()
    except OSError:
        raw = ""

    if raw:
        parts = raw.split(maxsplit=1)
        if len(parts) == 2:
            prev_marker, prev_ts_raw = parts
            try:
                prev_ts = int(prev_ts_raw)
            except ValueError:
                prev_ts = 0
            if prev_marker == marker and now_ts - prev_ts < cooldown_seconds:
                return False

    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(f"{marker} {now_ts}", encoding="utf-8")
    except OSError:
        LOG.debug("failed to persist duplicate-instance alert cooldown state at %s", state_file)

    return True


async def notify_single_instance_lock_conflict(
    telegram_token: str,
    routing_config_path: str | None,
    lock_owner_pid: int | None,
    lock_path: str | None = None,
) -> None:
    admin_ids = load_admin_ids_for_notifications(routing_config_path)
    if not telegram_token or not admin_ids:
        return

    headers = {
        "User-Agent": "changelog-watch-telegram-bot/1.0",
        "Accept": "application/json",
    }
    lock_owner = str(lock_owner_pid) if lock_owner_pid else "неизвестен"
    message = (
        "⚠️ Обнаружен запуск второго экземпляра changelog-watch-telegram-bot."
        f" Уже активный процесс: pid={lock_owner}."
    )

    state_file = _instance_alert_state_file("lock-conflict", lock_path)
    marker = str(lock_owner)
    if not _should_send_alert_with_cooldown(state_file, marker=marker, cooldown_seconds=_DUPLICATE_INSTANCE_ALERT_COOLDOWN_SECONDS):
        return

    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
        for admin_id in sorted(admin_ids):
            try:
                await send_telegram_message(client, telegram_token, admin_id, message)
            except Exception:
                LOG.exception("failed to notify admin %s about running instance conflict", admin_id)


def _load_admin_ids_from_env() -> set[str]:
    raw_admin_ids = os.getenv("ADMIN_IDS", "")
    admin_ids: set[str] = set()
    if not raw_admin_ids:
        return admin_ids

    for raw_admin_id in re.split(r"[\s,]+", raw_admin_ids.strip()):
        if not raw_admin_id:
            continue

        try:
            admin_ids.add(normalize_chat_id(raw_admin_id, "ADMIN_IDS"))
        except ValueError:
            LOG.warning("invalid ADMIN_IDS value %r", raw_admin_id)

    return admin_ids


def load_admin_ids_for_notifications(routing_config_path: str | None) -> set[str]:
    admin_ids = _load_admin_ids_from_env()
    if admin_ids:
        return admin_ids

    return load_admin_ids_for_lock_alert(routing_config_path)


async def notify_admin_lifecycle_event(
    telegram_token: str,
    routing_config_path: str | None,
    message: str,
) -> None:
    admin_ids = load_admin_ids_for_notifications(routing_config_path)
    if not telegram_token or not admin_ids:
        return

    headers = {
        "User-Agent": "changelog-watch-telegram-bot/1.0",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
        for admin_id in sorted(admin_ids):
            try:
                await send_telegram_message(client, telegram_token, admin_id, message)
            except Exception:
                LOG.exception("failed to notify admin %s about bot lifecycle event", admin_id)


@dataclass(frozen=True)
class SummarySchedule:
    mode: str
    time: str
    weekday: int | None = None

    @classmethod
    def immediate(cls) -> "SummarySchedule":
        return cls(mode="immediate", time="00:00", weekday=None)

    @classmethod
    def disabled(cls) -> "SummarySchedule":
        return cls(mode="none", time="00:00", weekday=None)


def normalize_alias(value: Any) -> str | None:
    alias = normalize_string(value)
    if not alias:
        return None
    return alias.lower()


def resolve_display_timezone() -> tuple[str, ZoneInfo]:
    global _display_tz_name, _display_tz

    configured_name = normalize_string(os.getenv("DISPLAY_TIMEZONE", _display_tz_name)) or DEFAULT_DISPLAY_TIMEZONE
    if configured_name == _display_tz_name:
        return configured_name, _display_tz

    try:
        resolved_tz = ZoneInfo(configured_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        LOG.warning("Invalid DISPLAY_TIMEZONE=%r: %s; fallback to %s", configured_name, exc, DEFAULT_DISPLAY_TIMEZONE)
        configured_name = DEFAULT_DISPLAY_TIMEZONE
        resolved_tz = ZoneInfo(configured_name)

    _display_tz_name = configured_name
    _display_tz = resolved_tz
    return configured_name, resolved_tz


def parse_delivery_mode(raw_mode: Any, context: str, *, send_summary: bool) -> str:
    mode = normalize_string(raw_mode).lower()

    if not mode:
        return "instant" if not send_summary else "both"

    if mode not in {"instant", "digest", "both", "none"}:
        raise ValueError(
            f"{context} delivery_mode must be one of instant|digest|both|none, got {mode!r}"
        )

    return mode


@dataclass(frozen=True)
class ChangelogEntry:
    item_id: str
    title: str
    version: str
    date: str | None
    body: str
    url: str
    is_prerelease: bool = False


@dataclass(frozen=True)
class ChatRouting:
    chat_id: str
    groups: set[str]
    source_ids: set[str]
    title: str | None = None
    alias: str | None = None
    enabled: bool = True
    send_summary: bool = True
    delivery_mode: str = "both"
    summary_schedule: SummarySchedule = field(default_factory=SummarySchedule.disabled)
    summary_on_startup: bool = False
    last_summary_sent_at: str | None = None


@dataclass(frozen=True)
class RoutingConfig:
    admins: set[str]
    admin_aliases: dict[str, str]
    source_groups: dict[str, set[str]]
    chats: dict[str, ChatRouting]


@dataclass(frozen=True)
class ChatAccessResult:
    chat_id: str
    accessible: bool
    reason: str | None = None


@dataclass
class RoutingState:
    db_path: str
    ttl_seconds: int = 0
    source_config_path: str | None = None
    dry_run: bool = False
    config: RoutingConfig | None = None
    loaded_at_monotonic: float | None = None

    def get(self, source_ids: set[str], *, force_reload: bool = False) -> RoutingConfig:
        if self.config is None:
            return self._reload(source_ids, reason="initial")

        if force_reload:
            return self._reload(source_ids, reason="forced")

        if self.ttl_seconds <= 0:
            return self._reload(source_ids, reason="poll-cycle")

        if self.ttl_seconds > 0:
            if self.loaded_at_monotonic is None:
                return self._reload(source_ids, reason="state-reset")
            if monotonic() - self.loaded_at_monotonic >= self.ttl_seconds:
                return self._reload(source_ids, reason="ttl-expired")

        return self.config

    def _reload(self, source_ids: set[str], *, reason: str) -> RoutingConfig:
        with db_connect_runtime(self.db_path, dry_run=self.dry_run) as conn:
            ensure_routing_state_seeded(conn, self.source_config_path, source_ids)
            config = load_routing_config_from_db(conn, source_ids)
        self.config = config
        self.loaded_at_monotonic = monotonic()
        LOG.info("routing state loaded from sqlite (%s)", reason)
        return config


def parse_command(text: str | None) -> tuple[str, list[str]] | None:
    if not text:
        return None

    line = text.strip().splitlines()[0] if text else ""
    if not line.startswith("/"):
        return None

    parts = line.split()
    if not parts:
        return None

    command_token = parts[0]
    if not command_token.startswith("/"):
        return None

    command = command_token[1:].split("@", 1)[0].strip().lower()
    if not command:
        return None

    return command, parts[1:]


def html_escape_value(value: Any) -> str:
    text = normalize_string(value)
    return html.escape(text if text else "-")


def html_escape_error(exc: BaseException) -> str:
    return html.escape(mask_telegram_bot_token(str(exc)))


def bool_status(value: bool) -> str:
    return "on" if value else "off"


def sort_telegram_ids(values: set[str] | list[str]) -> list[str]:
    return sorted(values, key=lambda item: int(item))


def format_admins_command(routing: RoutingConfig) -> str:
    lines = ["<b>Admins</b>"]
    if not routing.admins:
        lines.append("No admins in routing state.")
        return "\n".join(lines)

    admin_alias_lookup = {admin_id: alias for alias, admin_id in routing.admin_aliases.items()}
    for admin_id in sort_telegram_ids(routing.admins):
        alias = admin_alias_lookup.get(admin_id)
        suffix = f" @{html_escape_value(alias)}" if alias else ""
        lines.append(f"<code>{html.escape(admin_id)}</code>{suffix}")
    return "\n".join(lines)


def format_chats_command(routing: RoutingConfig) -> str:
    lines = ["<b>Chats</b>"]
    if not routing.chats:
        lines.append("No chats in routing state.")
        return "\n".join(lines)

    for chat in sorted(routing.chats.values(), key=lambda item: int(item.chat_id)):
        alias = f" @{html_escape_value(chat.alias)}" if chat.alias else ""
        title = f" {html_escape_value(chat.title)}" if chat.title else ""
        lines.append(
            f"<code>{html.escape(chat.chat_id)}</code>{alias}{title} "
            f"enabled={bool_status(chat.enabled)} mode={html.escape(chat.delivery_mode)} "
            f"groups={len(chat.groups)} sources={len(chat.source_ids)}"
        )
    return "\n".join(lines)


def format_pending_chats_command(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT chat_id, title, username, type, requested_by_user_id, requested_by_name, requested_alias, created_at
        FROM pending_chats
        ORDER BY created_at, chat_id
        """
    ).fetchall()
    lines = ["<b>Pending chats</b>"]
    if not rows:
        lines.append("No pending chats.")
        return "\n".join(lines)

    for row in rows:
        alias = normalize_alias(row["requested_alias"])
        alias_arg = f" {html.escape(alias)}" if alias else ""
        username = f" @{html_escape_value(row['username'])}" if row["username"] else ""
        title = html_escape_value(row["title"])
        requester = html_escape_value(row["requested_by_name"] or row["requested_by_user_id"])
        lines.append(
            f"<code>{html.escape(row['chat_id'])}</code>{username} {title} "
            f"type={html_escape_value(row['type'])} by={requester}\n"
            f"<code>/approvechat {html.escape(row['chat_id'])}{alias_arg}</code>\n"
            f"<code>/rejectchat {html.escape(row['chat_id'])}</code>"
        )
    return "\n".join(lines)


def format_pending_sources_command(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        """
        SELECT token, source_id, preview_text, requested_by_user_id, requested_by_name, action, created_at
        FROM pending_sources
        ORDER BY created_at, source_id
        """
    ).fetchall()
    lines = ["<b>Pending source changes</b>"]
    if not rows:
        lines.append("No pending source changes.")
        return "\n".join(lines)

    for row in rows:
        requester = html_escape_value(row["requested_by_name"] or row["requested_by_user_id"])
        lines.append(
            f"<code>{html.escape(row['token'])}</code> <code>{html.escape(row['source_id'])}</code> "
            f"action={html_escape_value(row['action'])} by={requester}\n"
            f"{row['preview_text']}\n"
            f"<code>/confirmsource {html.escape(row['token'])}</code>\n"
            f"<code>/rejectsource {html.escape(row['token'])}</code>"
        )
    return "\n".join(lines)


def format_sources_command(sources: list[dict[str, Any]]) -> str:
    lines = ["<b>Sources</b>"]
    if not sources:
        lines.append("No runtime sources in SQLite.")
        return "\n".join(lines)

    for source in sorted(sources, key=lambda item: str(item.get("id", ""))):
        source_id = html_escape_value(source.get("id"))
        product = html_escape_value(source.get("product"))
        source_type = html_escape_value(source.get("type"))
        enabled = bool_status(source.get("enabled", True) is not False)
        lines.append(f"<code>{source_id}</code> {product} type={source_type} enabled={enabled}")
    return "\n".join(lines)


def format_source_details_command(source_id: str, sources: list[dict[str, Any]], routing: RoutingConfig) -> str:
    source = next((item for item in sources if item.get("id") == source_id), None)
    if source is None:
        return f"Источник <code>{html.escape(source_id)}</code> не найден в runtime sources."

    groups = sorted(group_name for group_name, group_sources in routing.source_groups.items() if source_id in group_sources)
    direct_chats = sorted(
        (chat for chat in routing.chats.values() if source_id in chat.source_ids),
        key=lambda item: int(item.chat_id),
    )
    group_chats = sorted(
        (chat for chat in routing.chats.values() if any(group_name in chat.groups for group_name in groups)),
        key=lambda item: int(item.chat_id),
    )

    lines = [
        f"<b>Source</b> <code>{html.escape(source_id)}</code>",
        f"product={html_escape_value(source.get('product'))}",
        f"type={html_escape_value(source.get('type'))} enabled={bool_status(source.get('enabled', True) is not False)}",
        f"groups={html_escape_value(', '.join(groups))}",
    ]

    def chat_label(chat: ChatRouting) -> str:
        alias = f" @{html_escape_value(chat.alias)}" if chat.alias else ""
        title = f" {html_escape_value(chat.title)}" if chat.title else ""
        return f"<code>{html.escape(chat.chat_id)}</code>{alias}{title} enabled={bool_status(chat.enabled)} mode={html.escape(chat.delivery_mode)}"

    lines.append("direct chats:")
    lines.extend([chat_label(chat) for chat in direct_chats] or ["-"])
    lines.append("via groups:")
    lines.extend([chat_label(chat) for chat in group_chats] or ["-"])
    return "\n".join(lines)


def format_subscriptions_command(chat_id: str, routing: RoutingConfig) -> str:
    chat = routing.chats.get(chat_id)
    if chat is None:
        return f"Chat <code>{html.escape(chat_id)}</code> not found."

    group_sources: set[str] = set()
    for group_name in chat.groups:
        group_sources.update(routing.source_groups.get(group_name, set()))

    alias = f" @{html_escape_value(chat.alias)}" if chat.alias else ""
    explicit = ", ".join(f"<code>{html.escape(source_id)}</code>" for source_id in sorted(chat.source_ids)) or "-"
    groups = ", ".join(html.escape(group_name) for group_name in sorted(chat.groups)) or "-"
    derived = ", ".join(f"<code>{html.escape(source_id)}</code>" for source_id in sorted(group_sources)) or "-"
    return "\n".join(
        [
            f"<b>Subscriptions</b> <code>{html.escape(chat.chat_id)}</code>{alias}",
            f"explicit: {explicit}",
            f"groups: {groups}",
            f"group-derived: {derived}",
        ]
    )


def safe_db_path_label(db_path: str | Path) -> str:
    path = Path(db_path).expanduser()
    if path.is_absolute():
        try:
            return path.resolve().relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            return path.name
    return path.as_posix()


def format_status_command(
    sources: list[dict[str, Any]],
    routing: RoutingConfig,
    conn: sqlite3.Connection,
    *,
    poll_minutes: int,
    db_path: str | Path,
) -> str:
    enabled_sources = sum(1 for source in sources if source.get("enabled", True) is not False)
    disabled_sources = len(sources) - enabled_sources
    enabled_chats = sum(1 for chat in routing.chats.values() if chat.enabled)
    disabled_chats = len(routing.chats) - enabled_chats
    pending_chats = conn.execute("SELECT COUNT(*) FROM pending_chats").fetchone()[0]
    pending_sources = conn.execute("SELECT COUNT(*) FROM pending_sources").fetchone()[0]
    return "\n".join(
        [
            "<b>Status</b>",
            f"sources: enabled={enabled_sources} disabled={disabled_sources}",
            f"chats: enabled={enabled_chats} disabled={disabled_chats}",
            f"admins={len(routing.admins)} pending_chats={pending_chats} pending_sources={pending_sources}",
            f"poll_minutes={poll_minutes} db=<code>{html.escape(safe_db_path_label(db_path))}</code>",
        ]
    )


def format_source_preview(source: dict[str, Any], entries: list[ChangelogEntry]) -> str:
    lines = [
        f"<b>Source preview</b> {html_escape_value(source.get('product') or source.get('id'))}",
        f"id=<code>{html_escape_value(source.get('id'))}</code> type={html_escape_value(source.get('type'))}",
    ]
    for entry in entries[:3]:
        title = html_escape_value(entry.title or entry.version or entry.item_id)
        version = html_escape_value(entry.version or entry.item_id)
        url = html.escape(entry.url, quote=True)
        lines.append(f"- <b>{title}</b> <code>{version}</code> <a href=\"{url}\">link</a>")
    return "\n".join(lines)


def format_id_command(message: dict[str, Any]) -> str:
    raw_user_id = (message.get("from") or {}).get("id")
    raw_chat_id = (message.get("chat") or {}).get("id")
    user_id = "-" if raw_user_id is None else html.escape(str(raw_user_id))
    chat_id = "-" if raw_chat_id is None else html.escape(str(raw_chat_id))
    return f"user_id=<code>{user_id}</code>\nchat_id=<code>{chat_id}</code>"


def format_help_command() -> str:
    return "\n".join(
        [
            "<b>Commands</b>",
            "/id",
            "/requestchat [alias]",
            "/addme [alias]",
            "/admins",
            "/chats or /contacts",
            "/pending",
            "/sources or /projects",
            "/source &lt;source_id&gt; or /info &lt;source_id&gt;",
            "/testsource &lt;source_id&gt;",
            "/addrepo &lt;owner/repo|github_url&gt; [source_id] [product name...]",
            "/addsource &lt;source_id&gt; &lt;type&gt; &lt;url&gt; | &lt;product name&gt;",
            "/pendingsources /confirmsource &lt;token&gt; /rejectsource &lt;token&gt;",
            "/enablesource &lt;source_id&gt; /disablesource &lt;source_id&gt; /removesource &lt;source_id&gt;",
            "/reload",
            "/status",
            "/approvechat &lt;chat_id&gt; [alias]",
            "/rejectchat &lt;chat_id&gt;",
            "/addchat_here [alias]",
            "/removechat &lt;chat_id|alias&gt;",
            "/enablechat &lt;chat_id|alias&gt; /disablechat &lt;chat_id|alias&gt;",
            "/addadmin &lt;user_id&gt; [alias] /removeadmin &lt;user_id|alias&gt;",
            "/setchatalias &lt;chat_id|alias&gt; &lt;alias|-&gt;",
            "/setchattitle &lt;chat_id|alias&gt; &lt;title|-&gt;",
            "/setchatdelivery &lt;chat_id|alias&gt; &lt;instant|digest|both|none&gt;",
            "/subscribe &lt;source_id&gt; [chat_id|alias] /link &lt;source_id&gt; &lt;chat_id|alias&gt;",
            "/unsubscribe &lt;source_id&gt; [chat_id|alias] /unlink &lt;source_id&gt; &lt;chat_id|alias&gt;",
            "/subscribe_here &lt;source_id&gt; /unsubscribe_here &lt;source_id&gt;",
            "/subscriptions [chat_id|alias]",
        ]
    )


def chunk_telegram_html_message(text: str, max_chars: int = 3500) -> list[str]:
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in text.splitlines():
        line_len = len(line) + 1
        if line_len > max_chars:
            line = line[: max_chars - 20] + "\n... truncated"
            line_len = len(line) + 1
        if current and current_len + line_len > max_chars:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))
    return chunks


def is_authorized_admin(admins: set[str], raw_user_id: Any) -> bool:
    if not admins:
        return False

    if raw_user_id is None:
        return False

    try:
        user_id = str(int(str(raw_user_id).strip()))
    except (TypeError, ValueError):
        return False

    return user_id in admins


def load_source_ids(config_path: str | Path) -> set[str]:
    config = load_config(config_path)
    return collect_source_ids(config["sources"])


def source_store_has_data(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT COUNT(*) FROM runtime_sources").fetchone()[0] > 0


def source_config_to_yaml_text(source: dict[str, Any]) -> str:
    return yaml.safe_dump(source, allow_unicode=True, sort_keys=False)


def parse_source_config_text(source_id: str, config_text: str) -> dict[str, Any]:
    source = yaml.safe_load(config_text) or {}
    if not isinstance(source, dict):
        raise ValueError(f"runtime source {source_id} config must be an object")
    source["id"] = normalize_source_id(source.get("id", source_id))
    if source["id"] != source_id:
        raise ValueError(f"runtime source row id {source_id} does not match config id {source['id']}")
    return source


def load_sources_from_db(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for row in conn.execute("SELECT source_id, config_yaml FROM runtime_sources ORDER BY source_id").fetchall():
        sources.append(parse_source_config_text(row["source_id"], row["config_yaml"]))
    validate_source_config({"sources": sources})
    return sources


def import_sources_to_db(
    conn: sqlite3.Connection,
    sources: list[dict[str, Any]],
    *,
    replace: bool = False,
    commit: bool = True,
) -> None:
    validate_source_config({"sources": sources})
    if replace:
        conn.execute("DELETE FROM runtime_sources")

    now = datetime.now(timezone.utc).isoformat()
    for source in sources:
        source_id = source["id"]
        config_text = source_config_to_yaml_text(source)
        conn.execute(
            """
            INSERT INTO runtime_sources(source_id, config_yaml, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_id) DO UPDATE SET
                config_yaml = excluded.config_yaml,
                updated_at = excluded.updated_at
            """,
            (source_id, config_text, now, now),
        )

    if commit:
        conn.commit()


def generate_pending_source_token(conn: sqlite3.Connection) -> str:
    for _ in range(20):
        token = secrets.token_urlsafe(6).replace("-", "").replace("_", "")[:8]
        if not conn.execute("SELECT 1 FROM pending_sources WHERE token = ?", (token,)).fetchone():
            return token
    raise RuntimeError("failed to generate unique pending source token")


def stage_pending_source_db(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    preview_text: str,
    message: dict[str, Any],
    *,
    action: str,
) -> str:
    source_id = normalize_source_id(source.get("id"))
    validate_source_config({"sources": [dict(source)]})
    user = message.get("from") or {}
    user_id = normalize_chat_id(user.get("id"), "from.id") if user.get("id") is not None else None
    token = generate_pending_source_token(conn)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO pending_sources(
            token, source_id, config_yaml, preview_text, requested_by_user_id,
            requested_by_name, action, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            token,
            source_id,
            source_config_to_yaml_text(source),
            preview_text,
            user_id,
            requested_by_name(user),
            action,
            now,
        ),
    )
    conn.commit()
    return token


def apply_pending_source_db(conn: sqlite3.Connection, token: str) -> str:
    row = conn.execute("SELECT * FROM pending_sources WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise ValueError(f"pending source token '{token}' not found")
    source = parse_source_config_text(row["source_id"], row["config_yaml"])
    import_sources_to_db(conn, [source], replace=False, commit=False)
    conn.execute("DELETE FROM pending_sources WHERE token = ?", (token,))
    conn.commit()
    return row["source_id"]


def reject_pending_source_db(conn: sqlite3.Connection, token: str) -> str:
    row = conn.execute("SELECT source_id FROM pending_sources WHERE token = ?", (token,)).fetchone()
    if row is None:
        raise ValueError(f"pending source token '{token}' not found")
    conn.execute("DELETE FROM pending_sources WHERE token = ?", (token,))
    conn.commit()
    return row["source_id"]


def set_source_enabled_db(conn: sqlite3.Connection, source_id: str, enabled: bool) -> str:
    normalized_source_id = normalize_source_id(source_id)
    row = conn.execute("SELECT config_yaml FROM runtime_sources WHERE source_id = ?", (normalized_source_id,)).fetchone()
    if row is None:
        raise ValueError(f"source '{normalized_source_id}' not found")
    source = parse_source_config_text(normalized_source_id, row["config_yaml"])
    source["enabled"] = bool(enabled)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE runtime_sources SET config_yaml = ?, updated_at = ? WHERE source_id = ?",
        (source_config_to_yaml_text(source), now, normalized_source_id),
    )
    conn.commit()
    return normalized_source_id


def remove_source_db(conn: sqlite3.Connection, source_id: str) -> str:
    normalized_source_id = normalize_source_id(source_id)
    group_refs = conn.execute(
        "SELECT group_name FROM routing_source_group_sources WHERE source_id = ? ORDER BY group_name",
        (normalized_source_id,),
    ).fetchall()
    chat_refs = conn.execute(
        "SELECT chat_id FROM routing_chat_sources WHERE source_id = ? ORDER BY CAST(chat_id AS INTEGER)",
        (normalized_source_id,),
    ).fetchall()
    if group_refs or chat_refs:
        groups = ", ".join(row["group_name"] for row in group_refs) or "-"
        chats = ", ".join(row["chat_id"] for row in chat_refs) or "-"
        raise ValueError(
            f"source '{normalized_source_id}' is still referenced. "
            f"Unlink it first from groups: {groups}; chats: {chats}"
        )
    cursor = conn.execute("DELETE FROM runtime_sources WHERE source_id = ?", (normalized_source_id,))
    conn.commit()
    if cursor.rowcount == 0:
        raise ValueError(f"source '{normalized_source_id}' not found")
    return normalized_source_id


def build_github_release_source(repo_or_url: str, source_id: str | None = None, product: str | None = None) -> dict[str, Any]:
    repo_text = normalize_string(repo_or_url)
    if not repo_text:
        raise ValueError("repository must be provided")
    if repo_text.startswith("http://") or repo_text.startswith("https://"):
        owner, repo = github_repo_from_url(repo_text)
        url = f"https://github.com/{owner}/{repo}/releases"
    else:
        parts = [part for part in repo_text.strip("/").split("/") if part]
        if len(parts) != 2:
            raise ValueError("repository must be owner/repo or a github.com URL")
        owner, repo = parts
        url = f"https://github.com/{owner}/{repo}/releases"
    resolved_source_id = normalize_source_id(source_id or f"{owner}_{repo}_releases")
    return {
        "id": resolved_source_id,
        "product": normalize_string(product) or resolved_source_id,
        "type": "github_releases",
        "url": url,
        "include_prereleases": True,
        "post_on_first_run": False,
        "max_body_chars": 2500,
    }


def build_source_from_command(source_id: str, source_type: str, url: str, product: str | None = None) -> dict[str, Any]:
    normalized_source_id = normalize_source_id(source_id)
    normalized_type = normalize_string(source_type).lower()
    if normalized_type not in {"html_changelog", "markdown_changelog", "github_releases"}:
        raise ValueError("source type must be html_changelog, markdown_changelog or github_releases")
    source: dict[str, Any] = {
        "id": normalized_source_id,
        "product": normalize_string(product) or normalized_source_id,
        "type": normalized_type,
        "url": normalize_string(url),
        "post_on_first_run": False,
        "max_body_chars": 2500,
    }
    if normalized_type == "github_releases":
        owner, repo = github_repo_from_url(source["url"])
        source["url"] = f"https://github.com/{owner}/{repo}/releases"
        source["include_prereleases"] = True
    if normalized_type == "markdown_changelog":
        source["skip_unreleased"] = True
    validate_source_config({"sources": [source]})
    return source


def parse_addsource_args(args: list[str]) -> dict[str, str | None]:
    if len(args) < 3:
        raise ValueError("Usage: /addsource <source_id> <type> <url> | <product name>")
    source_id, source_type, url = args[:3]
    product_tokens = args[3:]
    if product_tokens and product_tokens[0] == "|":
        product_tokens = product_tokens[1:]
    elif product_tokens and product_tokens[0].startswith("|"):
        product_tokens[0] = product_tokens[0][1:]
    product = " ".join(token for token in product_tokens if token).strip() or None
    return {"source_id": source_id, "source_type": source_type, "url": url, "product": product}


async def validate_source_via_parser(client: httpx.AsyncClient, source: dict[str, Any]) -> list[ChangelogEntry]:
    validate_source_config({"sources": [dict(source)]})
    entries = await parse_source(client, source)
    if not entries:
        raise ValueError(f"source '{source['id']}' parsed successfully but returned no entries")
    return entries


def ensure_source_state_seeded(conn: sqlite3.Connection, config_path: str | Path) -> None:
    if source_store_has_data(conn):
        return
    config = load_config(config_path)
    validate_source_config(config)
    import_sources_to_db(conn, config["sources"], replace=True)


def load_runtime_config(conn: sqlite3.Connection, config_path: str | Path) -> dict[str, Any]:
    seed_config = load_config(config_path)
    ensure_source_state_seeded(conn, config_path)
    runtime_config = dict(seed_config)
    runtime_config["sources"] = load_sources_from_db(conn)
    return runtime_config


def load_runtime_source_ids(db_path: str | Path, config_path: str | Path) -> set[str]:
    with db_connect(db_path) as conn:
        ensure_source_state_seeded(conn, config_path)
        return collect_source_ids(load_sources_from_db(conn))


def load_routing_yaml(path: str | Path) -> dict[str, Any]:
    return load_yaml_file(path)


def routing_has_data(conn: sqlite3.Connection) -> bool:
    tables = (
        conn.execute("SELECT COUNT(*) FROM routing_admins").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM routing_source_groups").fetchone()[0],
        conn.execute("SELECT COUNT(*) FROM routing_chats").fetchone()[0],
    )
    return any(value > 0 for value in tables)


def ensure_routing_state_seeded(
    conn: sqlite3.Connection,
    source_config_path: str | None,
    source_ids: set[str],
) -> None:
    seed_mode = get_routing_seed_mode()
    has_routing_data = routing_has_data(conn)

    if seed_mode == "off":
        if not has_routing_data:
            raise RuntimeError("ROUTING_SEED_MODE=off and routing DB is empty; import routing config manually first")
        return

    if seed_mode == "once" and has_routing_data:
        return

    if source_config_path is None and seed_mode == "sync":
        raise RuntimeError("ROUTING_SEED_MODE=sync requires ROUTING_CONFIG_PATH")

    if source_config_path is None:
        raise RuntimeError(
            "ROUTING_CONFIG_PATH is not set and routing DB is empty. "
            "Copy admin-routing.example.yaml to admin-routing.yaml or set ROUTING_CONFIG_PATH."
        )

    source_path = Path(source_config_path)
    if not source_path.exists():
        raise RuntimeError(f"routing seed file not found: {source_config_path}")

    route_config = load_routing_config(source_path, source_ids)
    import_routing_config_to_db(conn, route_config, replace=seed_mode == "sync")


def replace_routing_tables(conn: sqlite3.Connection) -> None:
    conn.commit()
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        delete_routing_tables(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys_enabled else 'OFF'}")


def delete_routing_tables(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM routing_chat_sources")
    conn.execute("DELETE FROM routing_chat_groups")
    conn.execute("DELETE FROM routing_source_group_sources")
    conn.execute("DELETE FROM routing_source_groups")
    conn.execute("DELETE FROM routing_chats")
    conn.execute("DELETE FROM routing_admins")


def import_routing_config_to_db(
    conn: sqlite3.Connection,
    routing: RoutingConfig,
    *,
    replace: bool = False,
    commit: bool = True,
) -> None:
    if replace:
        if commit:
            replace_routing_tables(conn)
        else:
            delete_routing_tables(conn)

    admin_alias_lookup: dict[str, str] = {admin_id: alias for alias, admin_id in routing.admin_aliases.items()}
    if not replace:
        conn.execute("DELETE FROM routing_admins")
    for admin_id in routing.admins:
        admin_alias = admin_alias_lookup.get(admin_id)
        conn.execute(
            "INSERT OR IGNORE INTO routing_admins(user_id, alias) VALUES (?, ?)",
            (admin_id, admin_alias),
        )
        conn.execute(
            "UPDATE routing_admins SET alias = ? WHERE user_id = ?",
            (admin_alias, admin_id),
        )

    for group_name, source_ids in routing.source_groups.items():
        conn.execute(
            "INSERT OR IGNORE INTO routing_source_groups(group_name) VALUES (?)",
            (group_name,),
        )
        for source_id in sorted(source_ids):
            conn.execute(
                "INSERT OR REPLACE INTO routing_source_group_sources(group_name, source_id) VALUES (?, ?)",
                (group_name, source_id),
            )

    for chat in routing.chats.values():
        conn.execute(
            """
            INSERT INTO routing_chats(
                chat_id, title, enabled, send_summary, delivery_mode, alias,
                summary_mode, summary_time, summary_weekday, summary_on_startup
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                enabled = excluded.enabled,
                send_summary = excluded.send_summary,
                delivery_mode = excluded.delivery_mode,
                alias = excluded.alias,
                summary_mode = excluded.summary_mode,
                summary_time = excluded.summary_time,
                summary_weekday = excluded.summary_weekday,
                summary_on_startup = excluded.summary_on_startup
            """,
            (
                chat.chat_id,
                chat.title,
                int(chat.enabled),
                int(chat.send_summary),
                chat.delivery_mode,
                chat.alias,
                chat.summary_schedule.mode,
                chat.summary_schedule.time,
                chat.summary_schedule.weekday,
                int(chat.summary_on_startup),
            ),
        )

        for group_name in sorted(chat.groups):
            conn.execute(
                "INSERT OR IGNORE INTO routing_chat_groups(chat_id, group_name) VALUES (?, ?)",
                (chat.chat_id, group_name),
            )

        for source_id in sorted(chat.source_ids):
            conn.execute(
                "INSERT OR REPLACE INTO routing_chat_sources(chat_id, source_id) VALUES (?, ?)",
                (chat.chat_id, source_id),
            )

    if commit:
        conn.commit()


def apply_chat_subscription_change_db(
    conn: sqlite3.Connection,
    source_id: str,
    chat_id: str,
    *,
    add: bool,
    chat_title: str | None = None,
) -> str:
    if add:
        conn.execute(
            """
            INSERT OR IGNORE INTO routing_chats(
                chat_id, title, enabled, send_summary, delivery_mode, summary_mode, summary_on_startup
            ) VALUES (?, ?, 1, 0, 'instant', 'none', 0)
            """,
            (chat_id, chat_title or None),
        )
        if chat_title:
            conn.execute(
                "UPDATE routing_chats SET title = ? WHERE chat_id = ? AND (title IS NULL OR title = '')",
                (chat_title, chat_id),
            )

        cursor = conn.execute(
            "INSERT OR IGNORE INTO routing_chat_sources(chat_id, source_id) VALUES (?, ?)",
            (chat_id, source_id),
        )
        conn.commit()
        if cursor.rowcount == 0:
            return f"чат {chat_id} уже подписан на {source_id}"
        return f"чат {chat_id} подписан на {source_id}"

    if not conn.execute("SELECT 1 FROM routing_chats WHERE chat_id = ?", (chat_id,)).fetchone():
        return f"чат {chat_id} не найден в routing store"

    cursor = conn.execute(
        "DELETE FROM routing_chat_sources WHERE chat_id = ? AND source_id = ?",
        (chat_id, source_id),
    )
    conn.commit()
    if cursor.rowcount == 0:
        return f"чат {chat_id} не подписан на {source_id}"

    return f"чат {chat_id} отписан от {source_id}"


def apply_chat_subscription_change(db_path: str | Path, source_id: str, chat_id: str, *, add: bool, chat_title: str | None = None) -> str:
    with db_connect(db_path) as conn:
        return apply_chat_subscription_change_db(conn, source_id, chat_id, add=add, chat_title=chat_title)


def requested_by_name(user: dict[str, Any]) -> str | None:
    parts = [normalize_string(user.get("first_name")), normalize_string(user.get("last_name"))]
    full_name = " ".join(part for part in parts if part)
    username = normalize_string(user.get("username"))
    if username:
        return f"{full_name} (@{username})" if full_name else f"@{username}"
    return full_name or None


def chat_title_from_message(message: dict[str, Any]) -> str | None:
    chat = message.get("chat") or {}
    return normalize_string(chat.get("title") or chat.get("first_name") or chat.get("username")) or None


def upsert_pending_chat_db(conn: sqlite3.Connection, message: dict[str, Any], alias: str | None) -> str:
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    chat_id = normalize_chat_id(chat.get("id"), "chat.id")
    user_id = normalize_chat_id(user.get("id"), "from.id") if user.get("id") is not None else None
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO pending_chats(
            chat_id, title, username, type, requested_by_user_id, requested_by_name, requested_alias, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(chat_id) DO UPDATE SET
            title = excluded.title,
            username = excluded.username,
            type = excluded.type,
            requested_by_user_id = excluded.requested_by_user_id,
            requested_by_name = excluded.requested_by_name,
            requested_alias = excluded.requested_alias,
            created_at = excluded.created_at
        """,
        (
            chat_id,
            chat_title_from_message(message),
            normalize_string(chat.get("username")) or None,
            normalize_string(chat.get("type")) or None,
            user_id,
            requested_by_name(user),
            alias,
            now,
        ),
    )
    conn.commit()
    return chat_id


def resolve_admin_identifier_db(conn: sqlite3.Connection, value: str) -> str | None:
    normalized = normalize_string(value)
    if not normalized:
        return None
    alias = normalize_alias(normalized)
    if alias:
        row = conn.execute("SELECT user_id FROM routing_admins WHERE lower(alias) = ?", (alias,)).fetchone()
        if row:
            return normalize_chat_id(row["user_id"], "admin alias")
    try:
        return normalize_chat_id(normalized, "admin identifier")
    except ValueError:
        return None


def add_admin_db(conn: sqlite3.Connection, user_id: str, alias: str | None) -> str:
    normalized_user_id = normalize_chat_id(user_id, "user_id")
    if alias:
        existing = conn.execute(
            "SELECT user_id FROM routing_admins WHERE lower(alias) = ? AND user_id != ?",
            (alias, normalized_user_id),
        ).fetchone()
        if existing:
            raise ValueError(f"admin alias '{alias}' is already used")
    conn.execute(
        """
        INSERT INTO routing_admins(user_id, alias) VALUES (?, ?)
        ON CONFLICT(user_id) DO UPDATE SET alias = COALESCE(excluded.alias, routing_admins.alias)
        """,
        (normalized_user_id, alias),
    )
    conn.commit()
    return normalized_user_id


def remove_admin_db(conn: sqlite3.Connection, identifier: str) -> str:
    user_id = resolve_admin_identifier_db(conn, identifier)
    if user_id is None:
        raise ValueError(f"admin '{identifier}' not found")
    admin_count = conn.execute("SELECT COUNT(*) FROM routing_admins").fetchone()[0]
    if admin_count <= 1:
        raise ValueError("cannot remove the last admin")
    cursor = conn.execute("DELETE FROM routing_admins WHERE user_id = ?", (user_id,))
    conn.commit()
    if cursor.rowcount == 0:
        raise ValueError(f"admin '{identifier}' not found")
    return user_id


def upsert_chat_db(
    conn: sqlite3.Connection,
    chat_id: str,
    *,
    alias: str | None = None,
    title: str | None = None,
    enabled: bool = True,
    delivery_mode: str = "instant",
) -> str:
    normalized_chat_id = normalize_chat_id(chat_id, "chat_id")
    if alias:
        existing = conn.execute(
            "SELECT chat_id FROM routing_chats WHERE lower(alias) = ? AND chat_id != ?",
            (alias, normalized_chat_id),
        ).fetchone()
        if existing:
            raise ValueError(f"chat alias '{alias}' is already used")
    delivery_mode = parse_delivery_mode(delivery_mode, f"chat {normalized_chat_id}", send_summary=True)
    send_summary = delivery_mode in {"digest", "both"}
    conn.execute(
        """
        INSERT INTO routing_chats(
            chat_id, title, enabled, send_summary, delivery_mode, alias, summary_mode, summary_on_startup
        ) VALUES (?, ?, ?, ?, ?, ?, 'none', 0)
        ON CONFLICT(chat_id) DO UPDATE SET
            title = COALESCE(excluded.title, routing_chats.title),
            enabled = excluded.enabled,
            send_summary = routing_chats.send_summary,
            delivery_mode = routing_chats.delivery_mode,
            alias = COALESCE(excluded.alias, routing_chats.alias)
        """,
        (normalized_chat_id, title, int(enabled), int(send_summary), delivery_mode, alias),
    )
    return normalized_chat_id


def remove_chat_db(conn: sqlite3.Connection, identifier: str, routing: RoutingConfig) -> str:
    chat_id = resolve_chat_identifier(identifier, routing)
    if chat_id is None or chat_id not in routing.chats:
        raise ValueError(f"chat '{identifier}' not found")
    foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
    conn.commit()
    try:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM routing_chat_sources WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM routing_chat_groups WHERE chat_id = ?", (chat_id,))
        conn.execute("DELETE FROM routing_chats WHERE chat_id = ?", (chat_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys_enabled else 'OFF'}")
    return chat_id


def set_chat_enabled_db(conn: sqlite3.Connection, identifier: str, routing: RoutingConfig, enabled: bool) -> str:
    chat_id = resolve_chat_identifier(identifier, routing)
    if chat_id is None or chat_id not in routing.chats:
        raise ValueError(f"chat '{identifier}' not found")
    conn.execute("UPDATE routing_chats SET enabled = ? WHERE chat_id = ?", (int(enabled), chat_id))
    conn.commit()
    return chat_id


def set_chat_alias_db(conn: sqlite3.Connection, identifier: str, routing: RoutingConfig, alias: str | None) -> str:
    chat_id = resolve_chat_identifier(identifier, routing)
    if chat_id is None or chat_id not in routing.chats:
        raise ValueError(f"chat '{identifier}' not found")
    if alias:
        existing = conn.execute(
            "SELECT chat_id FROM routing_chats WHERE lower(alias) = ? AND chat_id != ?",
            (alias, chat_id),
        ).fetchone()
        if existing:
            raise ValueError(f"chat alias '{alias}' is already used")
    conn.execute("UPDATE routing_chats SET alias = ? WHERE chat_id = ?", (alias, chat_id))
    conn.commit()
    return chat_id


def set_chat_title_db(conn: sqlite3.Connection, identifier: str, routing: RoutingConfig, title: str | None) -> str:
    chat_id = resolve_chat_identifier(identifier, routing)
    if chat_id is None or chat_id not in routing.chats:
        raise ValueError(f"chat '{identifier}' not found")
    conn.execute("UPDATE routing_chats SET title = ? WHERE chat_id = ?", (title, chat_id))
    conn.commit()
    return chat_id


def set_chat_delivery_db(conn: sqlite3.Connection, identifier: str, routing: RoutingConfig, delivery_mode: str) -> str:
    chat_id = resolve_chat_identifier(identifier, routing)
    if chat_id is None or chat_id not in routing.chats:
        raise ValueError(f"chat '{identifier}' not found")
    mode = parse_delivery_mode(delivery_mode, f"chat {chat_id}", send_summary=True)
    conn.execute(
        "UPDATE routing_chats SET delivery_mode = ?, send_summary = ? WHERE chat_id = ?",
        (mode, int(mode in {"digest", "both"}), chat_id),
    )
    conn.commit()
    return chat_id


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger().addFilter(SecretMaskingLogFilter())
    for handler in logging.getLogger().handlers:
        handler.addFilter(SecretMaskingLogFilter())


def load_yaml_file(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain YAML object")
    return data


def load_config(path: str | Path) -> dict[str, Any]:
    data = load_yaml_file(path)
    if "sources" not in data or not isinstance(data["sources"], list):
        raise ValueError("products.yaml must contain a top-level 'sources' list")
    return data


def normalize_chat_id(value: Any, field: str) -> str:
    if value is None:
        raise ValueError(f"{field} must be defined")

    try:
        return str(int(str(value).strip()))
    except (TypeError, ValueError):
        raise ValueError(f"{field} must be a valid integer telegram id: {value!r}")


def normalize_source_id(value: Any) -> str:
    if value is None:
        raise ValueError("source_id must be defined")
    source_id = str(value).strip()
    if not source_id:
        raise ValueError("source_id must be non-empty")
    return source_id


def normalize_string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def table_columns(conn: sqlite3.Connection, table_name: str) -> set[str]:
    return {
        row[1]
        for row in conn.execute(f"PRAGMA table_info({table_name})").fetchall()
    }


def parse_admin_entry(raw_admin: Any, idx: int) -> tuple[str, str | None]:
    if isinstance(raw_admin, dict):
        raw_admin_id = (
            raw_admin.get("id")
            if raw_admin.get("id") is not None
            else (raw_admin.get("user_id") if raw_admin.get("user_id") is not None else raw_admin.get("chat_id"))
        )
        alias = normalize_alias(raw_admin.get("alias"))
        if raw_admin_id is None:
            raise ValueError(f"admins[{idx}] must include id (or user_id) when specified as object")
        return normalize_chat_id(raw_admin_id, f"admins[{idx}]"), alias

    return normalize_chat_id(raw_admin, f"admins[{idx}]"), None


def validate_summary_time(raw_time: Any, context: str) -> tuple[int, int]:
    time_text = normalize_string(raw_time)
    if not time_text:
        raise ValueError(f"{context} summary schedule requires time like HH:MM")

    match = SUMMARY_TIME_RE.match(time_text)
    if not match:
        raise ValueError(f"{context} summary schedule time is invalid: {time_text!r}")

    hour = int(match.group("hour"))
    minute = int(match.group("minute"))
    if hour < 0 or hour > 23:
        raise ValueError(f"{context} summary schedule hour must be 0..23: {time_text!r}")
    return hour, minute


def parse_weekday(raw_weekday: Any, context: str) -> int:
    weekday_text = normalize_string(raw_weekday)
    if not weekday_text:
        raise ValueError(f"{context} summary schedule requires weekday")

    if weekday_text.isdigit():
        weekday_value = int(weekday_text)
        if 0 <= weekday_value <= 6:
            return weekday_value
        if 1 <= weekday_value <= 7:
            return (weekday_value - 1)
        raise ValueError(f"{context} weekday is out of range (0-6 or 1-7): {weekday_text}")

    normalized = weekday_text.lower()
    if normalized in WEEKDAY_NAMES:
        return WEEKDAY_NAMES[normalized]
    raise ValueError(f"{context} summary schedule has unknown weekday: {weekday_text!r}")


def parse_summary_schedule(raw_schedule: Any, context: str) -> SummarySchedule:
    if raw_schedule is None:
        return SummarySchedule.disabled()

    if isinstance(raw_schedule, str):
        mode = normalize_string(raw_schedule).lower()
        if not mode:
            return SummarySchedule.disabled()
        if mode not in {"immediate", "on", "enabled", "true", "none"}:
            raise ValueError(f"{context} summary schedule mode is invalid: {mode!r}")
        if mode == "none":
            return SummarySchedule.disabled()
        return SummarySchedule.immediate()

    if not isinstance(raw_schedule, dict):
        raise ValueError(f"{context} summary_schedule must be an object")

    mode = normalize_string(raw_schedule.get("mode") or raw_schedule.get("kind") or raw_schedule.get("frequency"))
    if not mode:
        mode = "none"
    mode = mode.lower()
    if mode not in {"immediate", "daily", "weekly", "none"}:
        raise ValueError(f"{context} summary_schedule.mode must be one of immediate|daily|weekly|none: {mode!r}")

    if mode == "immediate":
        return SummarySchedule.immediate()
    if mode == "none":
        return SummarySchedule.disabled()

    hour, minute = validate_summary_time(
        raw_schedule.get("time") or raw_schedule.get("at"),
        context,
    )
    normalized_time = f"{hour:02d}:{minute:02d}"

    if mode == "daily":
        return SummarySchedule(mode="daily", time=normalized_time, weekday=None)

    weekday = parse_weekday(raw_schedule.get("weekday"), context)
    return SummarySchedule(mode="weekly", time=normalized_time, weekday=weekday)


def parse_summary_schedule_from_db(row: sqlite3.Row, chat_id: str) -> SummarySchedule:
    summary_mode = normalize_string(row["summary_mode"]) if "summary_mode" in row.keys() else "none"
    summary_time = normalize_string(row["summary_time"]) if "summary_time" in row.keys() else "00:00"
    weekday = row["summary_weekday"] if "summary_weekday" in row.keys() else None

    try:
        return parse_summary_schedule(
            {
                "mode": summary_mode or "none",
                "time": summary_time,
                "weekday": weekday,
            },
            f"chat {chat_id}",
        )
    except ValueError as exc:
        LOG.warning("chat %s has invalid summary schedule in DB, fallback to none: %s", chat_id, exc)
        return SummarySchedule.disabled()


def parse_sqlite_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def current_summary_schedule_boundary(schedule: SummarySchedule, now: datetime) -> datetime | None:
    if schedule.mode not in {"daily", "weekly"}:
        return None

    parsed_time = SUMMARY_TIME_RE.match(schedule.time)
    if not parsed_time:
        return None

    hour = int(parsed_time.group("hour"))
    minute = int(parsed_time.group("minute"))

    if schedule.mode == "daily":
        scheduled_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < scheduled_dt:
            scheduled_dt -= timedelta(days=1)
        return scheduled_dt

    if schedule.weekday is None or schedule.weekday != now.weekday():
        return None

    scheduled_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled_dt:
        return None
    return scheduled_dt


def should_suppress_summary_on_startup(
    chat: ChatRouting,
    now: datetime,
    started_at: datetime | None,
) -> tuple[bool, datetime | None]:
    if chat.summary_on_startup or started_at is None:
        return False, None
    if chat.summary_schedule.mode in {"none", "immediate"}:
        return False, None

    boundary = current_summary_schedule_boundary(chat.summary_schedule, now)
    if boundary is None:
        return False, None

    started_at_local = started_at.astimezone(now.tzinfo) if now.tzinfo else started_at
    if started_at_local <= boundary:
        return False, None

    last_sent_at = parse_sqlite_datetime(chat.last_summary_sent_at)
    if last_sent_at is not None and last_sent_at.astimezone(boundary.tzinfo) >= boundary:
        return False, None

    return True, boundary


def is_summary_due(schedule: SummarySchedule, now: datetime, last_sent_at: str | None) -> bool:
    if schedule.mode == "none":
        return False

    if schedule.mode == "immediate":
        return True

    parsed_time = SUMMARY_TIME_RE.match(schedule.time)
    if not parsed_time:
        return False

    hour = int(parsed_time.group("hour"))
    minute = int(parsed_time.group("minute"))

    if schedule.mode == "daily":
        scheduled_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if schedule.time and now < scheduled_dt:
            scheduled_dt -= timedelta(days=1)

        if not last_sent_at:
            return now >= scheduled_dt
        last_sent_dt = parse_sqlite_datetime(last_sent_at)
        if last_sent_dt is None:
            return True
        return last_sent_dt < scheduled_dt

    if schedule.mode == "weekly":
        if schedule.weekday is None:
            return False
        if schedule.weekday != now.weekday():
            return False

        scheduled_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if not last_sent_at:
            return now >= scheduled_dt

        last_sent_dt = parse_sqlite_datetime(last_sent_at)
        if last_sent_dt is None:
            return now >= scheduled_dt
        return last_sent_dt < scheduled_dt

    return False


def get_chat_alias_lookup(routing: RoutingConfig) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for chat in routing.chats.values():
        if not chat.alias:
            continue
        aliases[chat.alias] = chat.chat_id
    return aliases


def resolve_chat_identifier(value: str, routing: RoutingConfig) -> str | None:
    normalized = normalize_string(value)
    if not normalized:
        return None

    normalized_alias = normalized.lower()
    for chat in routing.chats.values():
        if chat.alias == normalized_alias:
            return chat.chat_id

    try:
        return normalize_chat_id(normalized, "chat identifier")
    except ValueError:
        return None


def load_routing_config_data(data: dict[str, Any], source_ids: set[str]) -> RoutingConfig:
    admins_raw = data.get("admins", [])
    if not isinstance(admins_raw, list):
        raise ValueError("routing config 'admins' must be a list")

    admins: set[str] = set()
    admin_aliases: dict[str, str] = {}
    for idx, raw_admin in enumerate(admins_raw, start=1):
        admin_id, admin_alias = parse_admin_entry(raw_admin, idx)
        if admin_id in admins:
            raise ValueError(f"duplicate admin id {admin_id} in routing config")

        if admin_alias:
            if admin_alias in admin_aliases:
                raise ValueError(f"duplicate admin alias '{admin_alias}' in routing config")
            admin_aliases[admin_alias] = admin_id

        admins.add(admin_id)

    source_groups_raw = data.get("source_groups", {})
    if not isinstance(source_groups_raw, dict):
        raise ValueError("routing config 'source_groups' must be an object")
    source_groups: dict[str, set[str]] = {}
    for group_name, raw_source_ids in source_groups_raw.items():
        if not isinstance(raw_source_ids, list):
            raise ValueError(f"source group '{group_name}' must be a list")
        group_sources: set[str] = set()
        for raw_source_id in raw_source_ids:
            source_id = normalize_source_id(raw_source_id)
            if source_id not in source_ids:
                raise ValueError(f"source group '{group_name}' references unknown source: {source_id}")
            group_sources.add(source_id)
        source_groups[str(group_name)] = group_sources

    chats_raw = data.get("chats", [])
    if not isinstance(chats_raw, list):
        raise ValueError("routing config 'chats' must be a list")

    chats: dict[str, ChatRouting] = {}
    for idx, raw_chat in enumerate(chats_raw, start=1):
        if not isinstance(raw_chat, dict):
            raise ValueError(f"chat entry #{idx} in routing config must be an object")

        chat_id_value = raw_chat.get("chat_id", raw_chat.get("id"))
        chat_id = normalize_chat_id(chat_id_value, f"chats[{idx}].chat_id")

        raw_groups = raw_chat.get("groups", [])
        if not isinstance(raw_groups, list):
            raise ValueError(f"chats[{idx}] groups must be a list")
        chat_groups: set[str] = set()
        for raw_group_name in raw_groups:
            group_name = normalize_string(raw_group_name)
            if not group_name:
                continue
            if group_name not in source_groups:
                raise ValueError(f"chat {chat_id} uses unknown source group '{group_name}'")
            chat_groups.add(group_name)

        raw_source_ids = raw_chat.get("sources", [])
        if not isinstance(raw_source_ids, list):
            raise ValueError(f"chats[{idx}] sources must be a list")
        chat_source_ids: set[str] = set()
        for raw_source_id in raw_source_ids:
            source_id = normalize_source_id(raw_source_id)
            if source_id not in source_ids:
                raise ValueError(f"chat {chat_id} uses unknown source '{source_id}'")
            chat_source_ids.add(source_id)

        title = normalize_string(raw_chat.get("title")) or None
        alias = normalize_alias(raw_chat.get("alias"))
        enabled = parse_bool_value(raw_chat.get("enabled"), f"chats[{idx}].enabled", default=True)
        send_summary = parse_bool_value(raw_chat.get("send_summary"), f"chats[{idx}].send_summary", default=True)
        summary_on_startup = parse_bool_value(
            raw_chat.get("summary_on_startup"),
            f"chats[{idx}].summary_on_startup",
            default=False,
        )
        summary_schedule = parse_summary_schedule(raw_chat.get("summary_schedule"), f"chats[{idx}]")
        delivery_mode = parse_delivery_mode(raw_chat.get("delivery_mode"), f"chats[{idx}]", send_summary=send_summary)

        for existing_chat in chats.values():
            if existing_chat.alias and alias and existing_chat.alias == alias:
                raise ValueError(f"duplicate chat alias '{alias}'")

        if chat_id in chats:
            raise ValueError(f"duplicate chat entry for chat_id {chat_id}")

        chats[chat_id] = ChatRouting(
            chat_id=chat_id,
            groups=chat_groups,
            source_ids=chat_source_ids,
            title=title,
            alias=alias,
            enabled=enabled,
            send_summary=send_summary,
            delivery_mode=delivery_mode,
            summary_schedule=summary_schedule,
            summary_on_startup=summary_on_startup,
        )

    return RoutingConfig(admins=admins, admin_aliases=admin_aliases, source_groups=source_groups, chats=chats)


def load_routing_config(path: str | Path, source_ids: set[str]) -> RoutingConfig:
    return load_routing_config_data(load_yaml_file(path), source_ids)


def load_routing_config_from_db(conn: sqlite3.Connection, source_ids: set[str]) -> RoutingConfig:
    admins: set[str] = set()
    admin_aliases: dict[str, str] = {}

    admin_cursor = conn.execute("SELECT * FROM routing_admins ORDER BY user_id")
    for row in admin_cursor.fetchall():
        admin_id = normalize_chat_id(row["user_id"], "admins")
        if admin_id in admins:
            raise ValueError(f"duplicate admin id {admin_id} in routing DB")
        admins.add(admin_id)

        alias = normalize_alias(row["alias"]) if "alias" in row.keys() and row["alias"] is not None else None
        if alias:
            if alias in admin_aliases:
                raise ValueError(f"duplicate admin alias '{alias}' in routing DB")
            admin_aliases[alias] = admin_id

    source_groups: dict[str, set[str]] = {}
    for raw_group_name in conn.execute("SELECT group_name FROM routing_source_groups ORDER BY group_name").fetchall():
        source_groups[str(raw_group_name[0])] = set()

    for group_name, raw_source_id in conn.execute(
        "SELECT group_name, source_id FROM routing_source_group_sources ORDER BY group_name, source_id"
    ).fetchall():
        source_id = normalize_source_id(raw_source_id)
        if source_id not in source_ids:
            raise ValueError(f"source group '{group_name}' references unknown source '{source_id}'")
        source_groups[str(group_name)].add(source_id)

    chats: dict[str, ChatRouting] = {}
    for idx, raw_chat in enumerate(conn.execute("SELECT * FROM routing_chats ORDER BY chat_id"), start=1):
        chat_id = normalize_chat_id(raw_chat["chat_id"], f"chats[{idx}].chat_id")
        raw_chat_groups = [
            normalize_string(row[0])
            for row in conn.execute(
                "SELECT group_name FROM routing_chat_groups WHERE chat_id = ? ORDER BY group_name",
                (chat_id,),
            ).fetchall()
        ]

        chat_groups: set[str] = set()
        for raw_group_name in raw_chat_groups:
            if not raw_group_name:
                continue
            if raw_group_name not in source_groups:
                raise ValueError(f"chat {chat_id} uses unknown source group '{raw_group_name}'")
            chat_groups.add(raw_group_name)

        raw_chat_sources = [
            normalize_source_id(row[0])
            for row in conn.execute(
                "SELECT source_id FROM routing_chat_sources WHERE chat_id = ? ORDER BY source_id",
                (chat_id,),
            ).fetchall()
        ]

        chat_source_ids: set[str] = set()
        for source_id in raw_chat_sources:
            if source_id not in source_ids:
                raise ValueError(f"chat {chat_id} uses unknown source '{source_id}'")
            chat_source_ids.add(source_id)

        title = normalize_string(raw_chat["title"]) or None
        enabled = bool(raw_chat["enabled"])
        send_summary = bool(raw_chat["send_summary"])
        delivery_mode = parse_delivery_mode(
            raw_chat["delivery_mode"] if "delivery_mode" in raw_chat.keys() else None,
            f"chat {chat_id}",
            send_summary=send_summary,
        )
        alias = normalize_alias(raw_chat["alias"]) if raw_chat["alias"] is not None else None
        summary_schedule = parse_summary_schedule_from_db(raw_chat, chat_id)
        summary_on_startup = bool(raw_chat["summary_on_startup"]) if "summary_on_startup" in raw_chat.keys() else False
        last_summary_sent_at = normalize_string(raw_chat["last_summary_sent_at"]) or None

        for existing_chat in chats.values():
            if existing_chat.alias and alias and existing_chat.alias == alias:
                raise ValueError(f"duplicate chat alias '{alias}'")

        if chat_id in chats:
            raise ValueError(f"duplicate chat entry for chat_id {chat_id}")

        chats[chat_id] = ChatRouting(
            chat_id=chat_id,
            groups=chat_groups,
            source_ids=chat_source_ids,
            title=title,
            alias=alias,
            enabled=enabled,
            send_summary=send_summary,
            delivery_mode=delivery_mode,
            summary_schedule=summary_schedule,
            summary_on_startup=summary_on_startup,
            last_summary_sent_at=last_summary_sent_at,
        )

    return RoutingConfig(
        admins=admins,
        admin_aliases=admin_aliases,
        source_groups=source_groups,
        chats=chats,
    )


def routing_config_to_yaml_data(routing: RoutingConfig) -> dict[str, Any]:
    admin_alias_lookup: dict[str, str] = {admin_id: alias for alias, admin_id in routing.admin_aliases.items()}
    admins: list[Any] = []
    for admin_id in sorted(routing.admins, key=int):
        alias = admin_alias_lookup.get(admin_id)
        admins.append({"id": admin_id, "alias": alias} if alias else admin_id)

    source_groups = {
        group_name: sorted(source_ids)
        for group_name, source_ids in sorted(routing.source_groups.items())
    }

    chats: list[dict[str, Any]] = []
    for chat in sorted(routing.chats.values(), key=lambda item: int(item.chat_id)):
        summary_schedule: dict[str, Any] = {"mode": chat.summary_schedule.mode}
        if chat.summary_schedule.mode in {"daily", "weekly"}:
            summary_schedule["time"] = chat.summary_schedule.time
        if chat.summary_schedule.mode == "weekly":
            summary_schedule["weekday"] = chat.summary_schedule.weekday

        chats.append(
            {
                "chat_id": chat.chat_id,
                "alias": chat.alias,
                "title": chat.title,
                "groups": sorted(chat.groups),
                "sources": sorted(chat.source_ids),
                "enabled": chat.enabled,
                "send_summary": chat.send_summary,
                "delivery_mode": chat.delivery_mode,
                "summary_on_startup": chat.summary_on_startup,
                "summary_schedule": summary_schedule,
            }
        )

    return {"admins": admins, "source_groups": source_groups, "chats": chats}


def build_source_to_chat_map(sources: list[dict[str, Any]], routing: RoutingConfig) -> dict[str, list[str]]:
    source_to_chat: dict[str, list[str]] = defaultdict(list)

    for source in sources:
        source_id = source["id"]
        source_to_chat[source_id] = []

    for chat in routing.chats.values():
        if not chat.enabled:
            continue

        target_sources = set(chat.source_ids)
        for group_name in chat.groups:
            target_sources.update(routing.source_groups.get(group_name, set()))

        if not target_sources:
            LOG.warning("chat %s has no subscriptions; skipping", chat.chat_id)
            continue

        for source_id in target_sources:
            if source_id not in source_to_chat:
                continue
            if chat.chat_id not in source_to_chat[source_id]:
                source_to_chat[source_id].append(chat.chat_id)

    return source_to_chat


def collect_source_ids(sources: list[dict[str, Any]]) -> set[str]:
    source_ids: set[str] = set()
    for index, source in enumerate(sources, start=1):
        if not isinstance(source, dict):
            raise ValueError(f"source #{index} must be an object")
        source_id = normalize_source_id(source.get("id"))
        if source_id in source_ids:
            raise ValueError(f"duplicate source id '{source_id}' in sources at position {index}")
        source_ids.add(source_id)
        source["id"] = source_id
    return source_ids


def ensure_routing_columns(conn: sqlite3.Connection) -> None:
    admin_columns = table_columns(conn, "routing_admins")
    if "alias" not in admin_columns:
        conn.execute("ALTER TABLE routing_admins ADD COLUMN alias TEXT")

    chat_columns = table_columns(conn, "routing_chats")
    if "alias" not in chat_columns:
        conn.execute("ALTER TABLE routing_chats ADD COLUMN alias TEXT")
    if "summary_mode" not in chat_columns:
        conn.execute("ALTER TABLE routing_chats ADD COLUMN summary_mode TEXT NOT NULL DEFAULT 'none'")
    if "summary_time" not in chat_columns:
        conn.execute("ALTER TABLE routing_chats ADD COLUMN summary_time TEXT NOT NULL DEFAULT '00:00'")
    if "summary_weekday" not in chat_columns:
        conn.execute("ALTER TABLE routing_chats ADD COLUMN summary_weekday INTEGER")
    if "last_summary_sent_at" not in chat_columns:
        conn.execute("ALTER TABLE routing_chats ADD COLUMN last_summary_sent_at TEXT")
    if "summary_on_startup" not in chat_columns:
        conn.execute("ALTER TABLE routing_chats ADD COLUMN summary_on_startup INTEGER NOT NULL DEFAULT 0")
    delivery_mode_added = "delivery_mode" not in chat_columns

    if delivery_mode_added:
        conn.execute("ALTER TABLE routing_chats ADD COLUMN delivery_mode TEXT NOT NULL DEFAULT 'both'")

    if delivery_mode_added:
        conn.execute(
            """
            UPDATE routing_chats
            SET delivery_mode = CASE
                WHEN COALESCE(send_summary, 0) = 0 THEN 'instant'
                ELSE 'both'
            END
            WHERE delivery_mode = 'both'
            """
        )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS deliveries (
            source_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            chat_id TEXT NOT NULL,
            status TEXT NOT NULL,
            sent_at TEXT,
            last_attempt_at TEXT NOT NULL,
            error TEXT,
            PRIMARY KEY (source_id, item_id, chat_id),
            FOREIGN KEY (chat_id) REFERENCES routing_chats(chat_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_chat_status ON deliveries(chat_id, status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_source_item ON deliveries(source_id, item_id)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS summary_queue (
            chat_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            item_title TEXT NOT NULL,
            item_version TEXT NOT NULL,
            item_date TEXT,
            item_url TEXT NOT NULL,
            item_is_prerelease INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            PRIMARY KEY (chat_id, source_id, item_id),
            FOREIGN KEY (chat_id) REFERENCES routing_chats(chat_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_summary_queue_chat_id ON summary_queue(chat_id)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_chats (
            chat_id TEXT PRIMARY KEY,
            title TEXT,
            username TEXT,
            type TEXT,
            requested_by_user_id TEXT,
            requested_by_name TEXT,
            requested_alias TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_chats_created_at ON pending_chats(created_at)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS pending_sources (
            token TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            config_yaml TEXT NOT NULL,
            preview_text TEXT NOT NULL,
            requested_by_user_id TEXT,
            requested_by_name TEXT,
            action TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_sources_created_at ON pending_sources(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_sources_source_id ON pending_sources(source_id)")


def initialize_database_schema(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS posted_items (
            source_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            posted_at TEXT NOT NULL,
            PRIMARY KEY (source_id, item_id)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_sources (
            source_id TEXT PRIMARY KEY,
            config_yaml TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_admins (
            user_id TEXT PRIMARY KEY
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_source_groups (
            group_name TEXT PRIMARY KEY
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_source_group_sources (
            group_name TEXT NOT NULL,
            source_id TEXT NOT NULL,
            PRIMARY KEY (group_name, source_id),
            FOREIGN KEY (group_name) REFERENCES routing_source_groups(group_name) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_chats (
            chat_id TEXT PRIMARY KEY,
            title TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            send_summary INTEGER NOT NULL DEFAULT 1,
            alias TEXT,
            delivery_mode TEXT NOT NULL DEFAULT 'both',
            summary_mode TEXT NOT NULL DEFAULT 'none',
            summary_time TEXT NOT NULL DEFAULT '00:00',
            summary_weekday INTEGER,
            summary_on_startup INTEGER NOT NULL DEFAULT 0,
            last_summary_sent_at TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_chat_groups (
            chat_id TEXT NOT NULL,
            group_name TEXT NOT NULL,
            PRIMARY KEY (chat_id, group_name),
            FOREIGN KEY (chat_id) REFERENCES routing_chats(chat_id) ON DELETE CASCADE,
            FOREIGN KEY (group_name) REFERENCES routing_source_groups(group_name)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS routing_chat_sources (
            chat_id TEXT NOT NULL,
            source_id TEXT NOT NULL,
            PRIMARY KEY (chat_id, source_id),
            FOREIGN KEY (chat_id) REFERENCES routing_chats(chat_id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS source_state (
            source_id TEXT PRIMARY KEY,
            initialized INTEGER NOT NULL DEFAULT 0,
            initialized_at TEXT
        )
        """
    )

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS ai_summaries (
            source_id TEXT NOT NULL,
            item_id TEXT NOT NULL,
            model TEXT NOT NULL,
            target_language TEXT NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (source_id, item_id, model, target_language)
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ai_summaries_source_item ON ai_summaries(source_id, item_id)")

    ensure_routing_columns(conn)
    conn.commit()


def db_connect(db_path: str | Path) -> sqlite3.Connection:
    if str(db_path) == ":memory:":
        conn = sqlite3.connect(":memory:")
    else:
        path = Path(db_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    initialize_database_schema(conn)
    return conn


def db_connect_for_dry_run(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    path = Path(db_path)
    if path.exists():
        source_conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        try:
            source_conn.backup(conn)
        finally:
            source_conn.close()

    initialize_database_schema(conn)
    return conn


def db_connect_runtime(db_path: str | Path, *, dry_run: bool) -> sqlite3.Connection:
    if dry_run:
        return db_connect_for_dry_run(db_path)
    return db_connect(db_path)


def is_source_initialized(conn: sqlite3.Connection, source_id: str) -> bool:
    row = conn.execute(
        "SELECT initialized FROM source_state WHERE source_id = ?",
        (source_id,),
    ).fetchone()
    return bool(row and row[0])


def mark_source_initialized(conn: sqlite3.Connection, source_id: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO source_state(source_id, initialized, initialized_at)
        VALUES(?, 1, ?)
        ON CONFLICT(source_id) DO UPDATE SET initialized = 1, initialized_at = excluded.initialized_at
        """,
        (source_id, now),
    )
    conn.commit()


def is_posted(conn: sqlite3.Connection, source_id: str, item_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM posted_items WHERE source_id = ? AND item_id = ?",
        (source_id, item_id),
    ).fetchone()
    return row is not None


def mark_posted(conn: sqlite3.Connection, source_id: str, item_id: str) -> None:
    conn.execute(
        """
        INSERT OR IGNORE INTO posted_items(source_id, item_id, posted_at)
        VALUES(?, ?, ?)
        """,
        (source_id, item_id, datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()


def mark_many_posted(conn: sqlite3.Connection, source_id: str, entries: list[ChangelogEntry]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT OR IGNORE INTO posted_items(source_id, item_id, posted_at)
        VALUES(?, ?, ?)
        """,
        [(source_id, entry.item_id, now) for entry in entries],
    )
    conn.commit()


def claim_new_posts(conn: sqlite3.Connection, source_id: str, entries: list[ChangelogEntry]) -> list[ChangelogEntry]:
    """Insert and return only entries not yet posted for this source.

    This protects against accidental double-start scenarios by making item marking
    atomic: only the first process successfully inserts an (source_id, item_id) pair.
    """
    if not entries:
        return []

    now = datetime.now(timezone.utc).isoformat()
    claimed: list[ChangelogEntry] = []

    for entry in entries:
        cursor = conn.execute(
            "INSERT OR IGNORE INTO posted_items(source_id, item_id, posted_at) VALUES (?, ?, ?)",
            (source_id, entry.item_id, now),
        )
        if cursor.rowcount == 1:
            claimed.append(entry)

    if claimed:
        conn.commit()

    return claimed


def ai_summary_enabled() -> bool:
    return os.getenv("AI_SUMMARY_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _parse_ai_summary_int(value: str | None, default: int) -> int:
    try:
        parsed = int((value or "").strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def get_ai_summary_settings() -> tuple[str, str, str, int, int, int]:
    api_base = os.getenv("AI_SUMMARY_API_BASE", "https://opencode.ai/zen/v1").strip().rstrip("/")
    if not api_base:
        api_base = "https://opencode.ai/zen/v1"

    model = os.getenv("AI_SUMMARY_MODEL", "minimax-m2.5-free").strip() or "minimax-m2.5-free"
    target_language = os.getenv("AI_SUMMARY_TARGET_LANGUAGE", "ru").strip() or "ru"
    max_input_chars = _parse_ai_summary_int(os.getenv("AI_SUMMARY_MAX_INPUT_CHARS"), 6000)
    timeout_seconds = _parse_ai_summary_int(os.getenv("AI_SUMMARY_TIMEOUT_SECONDS"), 30)
    max_output_chars = _parse_ai_summary_int(os.getenv("AI_SUMMARY_MAX_OUTPUT_CHARS"), 220)

    return api_base, model, target_language, max_input_chars, timeout_seconds, max_output_chars


def get_ai_summary_max_tokens(max_output_chars: int) -> int:
    default_tokens = max(max_output_chars * 6, 1000)
    return _parse_ai_summary_int(os.getenv("AI_SUMMARY_MAX_TOKENS"), default_tokens)


def clean_one_line_summary(text: str, max_len: int = 220) -> str:
    text = re.sub(r"\s+", " ", text).strip()
    text = text.strip('"\'«»`')

    prefixes = (
        "кратко:",
        "summary:",
        "итог:",
    )
    lowered = text.lower()
    for prefix in prefixes:
        if lowered.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    version_prefix = re.match(
        r"(?i)^(?:в\s+версии|версия)\s+v?\d+(?:\.\d+)*(?:[-+][0-9A-Za-z.-]+)?\s+",
        text,
    )
    if version_prefix:
        text = text[version_prefix.end():].strip()
        text = text[:1].upper() + text[1:] if text else text

    banned_starts = (
        "в этом релизе ",
        "это обновление ",
        "обновление содержит ",
        "релиз добавляет ",
    )
    lowered = text.lower()
    for start in banned_starts:
        if lowered.startswith(start):
            text = text[len(start):].strip()
            text = text[:1].upper() + text[1:] if text else text
            break

    if max_len <= 0:
        return ""
    if len(text) > max_len:
        text = text[: max_len - 1].rstrip() + "…"

    return text


def build_summary_input(source: dict[str, Any], entry: ChangelogEntry, max_chars: int) -> str:
    product = str(source.get("product") or source["id"])
    body = compact_markdown_for_telegram(entry.body or "")
    text = "\n".join(
        [
            f"Product: {product}",
            f"Version: {entry.version}",
            f"Title: {entry.title}",
            "",
            "Release notes:",
            body or "No release notes.",
        ]
    )
    return truncate(text, max_chars)


def load_ai_summary(
    conn: sqlite3.Connection,
    source_id: str,
    item_id: str,
    model: str,
    target_language: str,
) -> str | None:
    row = conn.execute(
        """
        SELECT summary
        FROM ai_summaries
        WHERE source_id = ? AND item_id = ? AND model = ? AND target_language = ?
        """,
        (source_id, item_id, model, target_language),
    ).fetchone()

    return str(row[0]) if row else None


def save_ai_summary(
    conn: sqlite3.Connection,
    source_id: str,
    item_id: str,
    model: str,
    target_language: str,
    summary: str,
) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO ai_summaries(
            source_id,
            item_id,
            model,
            target_language,
            summary,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            source_id,
            item_id,
            model,
            target_language,
            summary,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    conn.commit()


async def generate_ai_summary(
    client: httpx.AsyncClient,
    source: dict[str, Any],
    entry: ChangelogEntry,
) -> str | None:
    if not ai_summary_enabled():
        return None

    api_key = os.getenv("AI_SUMMARY_API_KEY", "").strip()
    if not api_key:
        LOG.warning("AI_SUMMARY_ENABLED=true but AI_SUMMARY_API_KEY is empty")
        return None

    api_base, model, target_language, max_input_chars, timeout_seconds, max_output_chars = get_ai_summary_settings()

    payload = {
        "model": model,
        "stream": False,
        "temperature": 0.2,
        "max_tokens": get_ai_summary_max_tokens(max_output_chars),
        "messages": [
            {
                "role": "system",
                "content": (
                    "You summarize software release notes for Telegram. "
                    f"Return exactly one short sentence in {target_language}. "
                    "No markdown. No bullets. No quotes. "
                    "Do not mention that this is a release. "
                    "Focus on practical changes."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Make a concise one-line summary of changes in {target_language}. "
                    f"Not longer than {max_output_chars} symbols. "
                    "Avoid phrases such as 'this release' and 'the update includes'.\n\n"
                    f"{build_summary_input(source, entry, max_input_chars)}"
                ),
            },
        ],
    }

    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.post(
                f"{api_base}/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=timeout_seconds,
            )
            response.raise_for_status()
            break
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code == 429 and attempt < max_attempts:
                retry_after = exc.response.headers.get("Retry-After", "").strip()
                try:
                    delay = max(1.0, min(float(retry_after), 30.0)) if retry_after else float(2 ** attempt)
                except ValueError:
                    delay = float(2 ** attempt)
                LOG.warning(
                    "[%s] AI summary rate limited for %s, retrying in %.1fs (attempt %d/%d)",
                    source["id"],
                    entry.item_id,
                    delay,
                    attempt,
                    max_attempts,
                )
                await asyncio.sleep(delay)
                continue
            LOG.exception("[%s] failed to generate AI summary for %s", source["id"], entry.item_id)
            return None
        except Exception:
            LOG.exception("[%s] failed to generate AI summary for %s", source["id"], entry.item_id)
            return None

    try:
        data = response.json()
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            LOG.warning("AI summary response for %s has no choices", source["id"])
            return None

        first_choice = choices[0]
        if not isinstance(first_choice, dict):
            LOG.warning("AI summary response for %s has invalid choice payload", source["id"])
            return None

        finish_reason = first_choice.get("finish_reason")

        message = first_choice.get("message")
        if not isinstance(message, dict):
            LOG.warning("AI summary response for %s has invalid message payload", source["id"])
            return None

        content = message.get("content")
        if content is None:
            LOG.warning(
                "AI summary response for %s/%s has empty content; finish_reason=%r message_keys=%s",
                source["id"],
                entry.item_id,
                finish_reason,
                sorted(str(key) for key in message.keys()),
            )
            return None

        summary = clean_one_line_summary(str(content), max_len=max_output_chars)
        if not summary:
            raw_content = re.sub(r"\s+", " ", str(content)).strip()
            LOG.warning(
                "AI summary response for %s/%s has empty cleaned content; raw_content=%r",
                source["id"],
                entry.item_id,
                truncate(raw_content, 200),
            )
            return None
        return summary
    except Exception:
        LOG.exception("[%s] failed to generate AI summary for %s", source["id"], entry.item_id)
        return None


async def get_or_generate_ai_summary(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    source: dict[str, Any],
    entry: ChangelogEntry,
    *,
    dry_run: bool = False,
    cycle_cache: dict[tuple[str, str, str, str], str | None] | None = None,
) -> str | None:
    if not ai_summary_enabled():
        return None

    _, model, target_language, _, _, _ = get_ai_summary_settings()
    cache_key = (source["id"], entry.item_id, model, target_language)
    if cycle_cache is not None and cache_key in cycle_cache:
        return cycle_cache[cache_key]

    cached = load_ai_summary(conn, source["id"], entry.item_id, model, target_language)
    if cached:
        if cycle_cache is not None:
            cycle_cache[cache_key] = cached
        return cached

    if dry_run and not ai_summary_dry_run_call_api_enabled():
        if cycle_cache is not None:
            cycle_cache[cache_key] = None
        return None

    summary = await generate_ai_summary(client, source, entry)
    if summary and not dry_run:
        save_ai_summary(conn, source["id"], entry.item_id, model, target_language, summary)
    if cycle_cache is not None:
        cycle_cache[cache_key] = summary

    return summary


def load_failed_delivery_item_ids(conn: sqlite3.Connection, chat_id: str, source_id: str) -> list[str]:
    rows = conn.execute(
        """
        SELECT item_id
        FROM deliveries
        WHERE source_id = ? AND chat_id = ? AND status = 'failed'
        ORDER BY rowid
        """,
        (source_id, chat_id),
    ).fetchall()
    return [str(row[0]) for row in rows]


def is_delivered_to_chat(conn: sqlite3.Connection, source_id: str, item_id: str, chat_id: str) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM deliveries
        WHERE source_id = ? AND item_id = ? AND chat_id = ? AND status = 'sent'
        """,
        (source_id, item_id, chat_id),
    ).fetchone()
    return row is not None


def mark_delivery_status(
    conn: sqlite3.Connection,
    source_id: str,
    item_id: str,
    chat_id: str,
    sent: bool,
    *,
    error: str | None = None,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    status = "sent" if sent else "failed"
    conn.execute(
        """
        INSERT INTO deliveries(
            source_id,
            item_id,
            chat_id,
            status,
            sent_at,
            last_attempt_at,
            error
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id, item_id, chat_id) DO UPDATE SET
            status = excluded.status,
            sent_at = COALESCE(excluded.sent_at, sent_at),
            last_attempt_at = excluded.last_attempt_at,
            error = excluded.error
        """,
        (
            source_id,
            item_id,
            chat_id,
            status,
            now if sent else None,
            now,
            error,
        ),
    )
    conn.commit()


def enqueue_summary_items(
    conn: sqlite3.Connection,
    chat_id: str,
    source_id: str,
    entries: list[ChangelogEntry],
) -> None:
    if not entries:
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT OR IGNORE INTO summary_queue(
            chat_id,
            source_id,
            item_id,
            item_title,
            item_version,
            item_date,
            item_url,
            item_is_prerelease,
            created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                chat_id,
                source_id,
                entry.item_id,
                entry.title,
                entry.version,
                entry.date,
                entry.url,
                1 if entry.is_prerelease else 0,
                now,
            )
            for entry in entries
        ],
    )
    conn.commit()


def get_summary_queue_max_age() -> timedelta | None:
    raw_value = os.getenv("SUMMARY_QUEUE_MAX_AGE_DAYS", "").strip()
    if not raw_value:
        return None
    try:
        days = int(raw_value)
    except ValueError:
        LOG.warning("SUMMARY_QUEUE_MAX_AGE_DAYS must be a positive integer; stale queue filtering disabled")
        return None
    if days <= 0:
        LOG.warning("SUMMARY_QUEUE_MAX_AGE_DAYS must be greater than zero; stale queue filtering disabled")
        return None
    return timedelta(days=days)


def load_summary_queue_items(
    conn: sqlite3.Connection,
    chat_id: str,
) -> list[tuple[str, ChangelogEntry]]:
    max_age = get_summary_queue_max_age()
    cutoff = datetime.now(timezone.utc) - max_age if max_age is not None else None
    rows = conn.execute(
        """
        SELECT source_id, item_id, item_title, item_version, item_date, item_url, item_is_prerelease, created_at
        FROM summary_queue
        WHERE chat_id = ?
        ORDER BY created_at, rowid
        """,
        (chat_id,),
    ).fetchall()

    result: list[tuple[str, ChangelogEntry]] = []
    skipped_stale = 0
    stale_keys: list[tuple[str, str, str]] = []
    for row in rows:
        created_at = parse_sqlite_datetime(row["created_at"])
        if cutoff is not None and created_at is not None and created_at < cutoff:
            skipped_stale += 1
            stale_keys.append((chat_id, row["source_id"], row["item_id"]))
            continue

        result.append(
            (
                row["source_id"],
                ChangelogEntry(
                    item_id=row["item_id"],
                    title=row["item_title"],
                    version=row["item_version"],
                    date=row["item_date"],
                    body="",
                    url=row["item_url"],
                    is_prerelease=bool(int(row["item_is_prerelease"])) if row["item_is_prerelease"] is not None else False,
                ),
            )
        )
    if skipped_stale:
        LOG.warning("summary queue for chat %s skipped %d stale item(s)", chat_id, skipped_stale)
        if summary_queue_prune_stale_enabled():
            delete_summary_queue_keys(conn, stale_keys)
            LOG.warning("summary queue for chat %s pruned %d stale item(s)", chat_id, skipped_stale)
    return result


def clear_summary_queue(conn: sqlite3.Connection, chat_id: str) -> None:
    conn.execute("DELETE FROM summary_queue WHERE chat_id = ?", (chat_id,))
    conn.commit()


def delete_summary_queue_keys(conn: sqlite3.Connection, keys: list[tuple[str, str, str]]) -> int:
    if not keys:
        return 0
    cursor = conn.executemany(
        "DELETE FROM summary_queue WHERE chat_id = ? AND source_id = ? AND item_id = ?",
        keys,
    )
    conn.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0


def clear_summary_queue_items(conn: sqlite3.Connection, chat_id: str, entries: list[tuple[str, ChangelogEntry]]) -> None:
    if not entries:
        return
    conn.executemany(
        "DELETE FROM summary_queue WHERE chat_id = ? AND source_id = ? AND item_id = ?",
        [(chat_id, source_id, entry.item_id) for source_id, entry in entries],
    )
    conn.commit()


def mark_chat_summary_sent(conn: sqlite3.Connection, chat_id: str, sent_at: datetime) -> None:
    conn.execute(
        "UPDATE routing_chats SET last_summary_sent_at = ? WHERE chat_id = ?",
        (sent_at.isoformat(), chat_id),
    )
    conn.commit()


async def fetch_text(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(url)
    response.raise_for_status()
    return response.text


def clean_lines(text: str) -> list[str]:
    lines: list[str] = []
    prev = None
    for raw in text.splitlines():
        line = re.sub(r"\s+", " ", raw).strip()
        if not line:
            continue
        if line == prev:
            continue
        lines.append(line)
        prev = line
    return lines


def looks_like_date(line: str) -> bool:
    return bool(
        re.match(
            r"^(\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2,8}\s+\d{1,2},\s+\d{4}|\d{1,2}\s+[A-Z][a-z]{2,8}\s+\d{2}:\d{2})$",
            line,
        )
    )


def dedupe_entries(entries: list[ChangelogEntry]) -> list[ChangelogEntry]:
    result: list[ChangelogEntry] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.item_id in seen:
            continue
        seen.add(entry.item_id)
        result.append(entry)
    return result


def parse_html_changelog(source: dict[str, Any], text: str) -> list[ChangelogEntry]:
    version_re = re.compile(source.get("version_regex") or DEFAULT_VERSION_RE)
    soup = BeautifulSoup(text, "html.parser")

    version_links: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        label = a.get_text(" ", strip=True)
        if version_re.fullmatch(label):
            version_links.setdefault(label, urljoin(source["url"], a["href"]))

    lines = clean_lines(soup.get_text("\n"))
    version_positions = [index for index, line in enumerate(lines) if version_re.fullmatch(line)]

    entries: list[ChangelogEntry] = []
    for pos_index, start in enumerate(version_positions):
        end = version_positions[pos_index + 1] if pos_index + 1 < len(version_positions) else len(lines)
        version = lines[start]
        chunk = lines[start + 1 : end]

        date = None
        if chunk and looks_like_date(chunk[0]):
            date = chunk[0]
            chunk = chunk[1:]

        body = "\n".join(chunk).strip()
        entries.append(
            ChangelogEntry(
                item_id=version,
                title=version,
                version=version,
                date=date,
                body=body,
                url=version_links.get(version, source.get("source_url") or source["url"]),
            )
        )

    return dedupe_entries(entries)


def parse_markdown_changelog(source: dict[str, Any], text: str) -> list[ChangelogEntry]:
    matches = list(MD_VERSION_HEADING_RE.finditer(text))
    entries: list[ChangelogEntry] = []

    for i, match in enumerate(matches):
        version = match.group("version").strip()
        version = version.strip("[]")
        date = match.group("date")
        body_start = match.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)

        if source.get("skip_unreleased", True) and version.lower() == "unreleased":
            continue

        body = text[body_start:body_end].strip()
        entries.append(
            ChangelogEntry(
                item_id=version,
                title=version,
                version=version,
                date=date,
                body=body,
                url=source.get("source_url") or source["url"],
            )
        )

    return dedupe_entries(entries)


def github_repo_from_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if len(parts) < 2 or parsed.netloc.lower() not in {"github.com", "www.github.com"}:
        raise ValueError(f"Cannot extract GitHub owner/repo from URL: {url}")
    return parts[0], parts[1]


async def parse_github_releases(client: httpx.AsyncClient, source: dict[str, Any]) -> list[ChangelogEntry]:
    owner, repo = github_repo_from_url(source["url"])
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases"
    headers = {"Accept": "application/vnd.github+json"}
    github_token = os.getenv("GITHUB_TOKEN", "").strip()
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    response = await client.get(api_url, headers=headers)
    response.raise_for_status()
    releases = response.json()

    include_prereleases = bool(source.get("include_prereleases", True))
    include_drafts = bool(source.get("include_drafts", False))
    entries: list[ChangelogEntry] = []

    for release in releases:
        if release.get("draft") and not include_drafts:
            continue
        if release.get("prerelease") and not include_prereleases:
            continue

        tag = release.get("tag_name") or release.get("name")
        if not tag:
            continue

        published_at = release.get("published_at") or release.get("created_at")
        date = None
        if published_at:
            date = to_display_timezone(published_at)

        title = release.get("name") or tag
        body = release.get("body") or ""
        entries.append(
            ChangelogEntry(
                item_id=str(tag),
                title=str(title),
                version=str(tag),
                date=date,
                body=body.strip(),
                url=release.get("html_url") or source["url"],
                is_prerelease=bool(release.get("prerelease")),
            )
        )

    return dedupe_entries(entries)


async def parse_source(client: httpx.AsyncClient, source: dict[str, Any]) -> list[ChangelogEntry]:
    source_type = source["type"]
    if source_type == "html_changelog":
        return parse_html_changelog(source, await fetch_text(client, source["url"]))
    if source_type == "markdown_changelog":
        return parse_markdown_changelog(source, await fetch_text(client, source["url"]))
    if source_type == "github_releases":
        return await parse_github_releases(client, source)
    raise ValueError(f"Unsupported source type: {source_type}")


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def compact_markdown_for_telegram(text: str) -> str:
    # Keep release notes readable in Telegram, while preserving content.
    if not text:
        return ""

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    # Strip markdown code blocks, keep the content to preserve details.
    def _code_block(match: re.Match[str]) -> str:
        body = (match.group(1) or "").strip("\n")
        if not body:
            return ""
        return "\n" + body + "\n"

    text = re.sub(r"```[a-zA-Z0-9_+-]*\n(.*?)\n```", _code_block, text, flags=re.DOTALL)
    text = re.sub(r"(?m)^```[a-zA-Z0-9_+-]*\s*$", "", text)

    # Convert markdown headings to readable labels, then make them bold-safe for HTML formatting.
    heading_open = "[[TG-HEADING-BOLD-OPEN]]"
    heading_close = "[[TG-HEADING-BOLD-CLOSE]]"

    def _heading_to_text(match: re.Match[str]) -> str:
        level = len(match.group(1))
        label = match.group(2).strip()
        if not label:
            return ""
        if level <= 2:
            return f"\n{heading_open}{label}{heading_close}\n"
        if level == 3:
            return f"\n{heading_open}— {label}{heading_close}\n"
        return f"\n{heading_open}◦ {label}{heading_close}\n"

    text = re.sub(r"(?m)^(#{1,6})\s+(.*?)\s*$", _heading_to_text, text)

    # Convert lists and task lists.
    def _task_list_to_text(match: re.Match[str]) -> str:
        indent = len(match.group(1))
        is_done = match.group(2).lower() == "x"
        item = match.group(3).strip()
        bullet = "◦ " if indent >= 2 else "• "
        status = "✅ " if is_done else "☐ "
        return f"{bullet}{status}{item}"

    text = re.sub(r"(?m)^([ \t]{0,3})[-*+]\s*\[([ xX])\]\s+(.*)$", _task_list_to_text, text)
    text = re.sub(r"(?m)^[ \t]{4,}(\d+)\.\s+(.*)$", r"    ◦ \1. \2", text)
    text = re.sub(r"(?m)^[ \t]{4,}[-*+]\s+(.*)$", r"    ◦ \1", text)
    text = re.sub(r"(?m)^[ \t]*(\d+)\.\s+(.*)$", r"\1. \2", text)
    text = re.sub(r"(?m)^[ \t]*[-*+]\s+(.*)$", r"• \1", text)

    # Remove quote markers.
    text = re.sub(r"(?m)^>\s?", "", text)

    # Strip common markdown emphasis/code markers, keep inner text.
    for pattern in (
        r"\*\*(.+?)\*\*",
        r"__(.+?)__",
        r"\*(.+?)\*",
        r"_(.+?)_",
        r"~~(.+?)~~",
        r"`([^`\n]+)`",
    ):
        text = re.sub(pattern, r"\1", text)

    # Convert markdown links to visible text, keep plain URL in a separate place if needed.
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", text)

    # Remove HTML fragments that sometimes appear in release notes.
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"</?(?:b|strong|i|em|s|strike|u|code|pre|blockquote|a|p|ul|ol|li|br)\b[^>]*>", "", text, flags=re.IGNORECASE)

    # Remove markdown table separators, keep only simple text lines.
    text = "\n".join(
        line
        for line in text.split("\n")
        if not re.fullmatch(r"\s*\|?(?:\s*:?-{3,}:?\s*\|)+\s*:?-{3,}:?\s*\|?\s*", line)
    )

    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return text.strip()


def to_display_timezone(date_text: str) -> str:
    """Convert explicit timestamps to configured display timezone."""
    date_text = date_text.strip()
    explicit_tz = bool(
        re.search(r"(?:\sUTC$|T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$|[+\-]\d{2}:\d{2}$)", date_text)
    )
    if not explicit_tz:
        return date_text

    try:
        dt = datetime.fromisoformat(date_text.replace(" UTC", "+00:00").replace("Z", "+00:00"))
    except ValueError:
        return date_text

    _, display_tz = resolve_display_timezone()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(display_tz)
    return f"{dt:%Y-%m-%d %H:%M:%S} ({dt.tzname() or _display_tz_name})"


def format_date_with_tz(date_text: str) -> str:
    text = date_text.strip()

    if text.endswith(")") and "(" in text:
        return text

    match = re.search(r"\s+(UTC|GMT[+-]\d{1,2}(?:\:?[0-9]{2})?|[+-]\d{2}:\d{2})$", text)
    if not match:
        return f"{text} (часовой пояс не указан)"

    tz = match.group(1)
    value = text[: -len(match.group(0))].strip()
    return f"{value} ({tz})"


def format_message(source: dict[str, Any], entry: ChangelogEntry) -> str:
    max_body_chars = int(source.get("max_body_chars", 2500))
    product = html.escape(str(source.get("product") or source["id"]))
    version = html.escape(entry.version)
    date = format_date_with_tz(entry.date) if entry.date else None
    body_raw = compact_markdown_for_telegram(entry.body or "Без описания.")
    body = html.escape(truncate(body_raw, max_body_chars))
    body = body.replace("[[TG-HEADING-BOLD-OPEN]]", "<b>").replace("[[TG-HEADING-BOLD-CLOSE]]", "</b>")
    url = html.escape(entry.url, quote=True)

    prerelease_mark = " <i>pre-release</i>" if entry.is_prerelease else ""
    parts = [f"🆕 <b>{product}</b>: <code>{version}</code>{prerelease_mark}"]
    if entry.title and entry.title != entry.version:
        parts.append(f"<b>{html.escape(entry.title)}</b>")
    if date:
        parts.append(f"<b>Дата:</b> {html.escape(date)}")
    parts.extend(["", body, "", f'<a href="{url}">Открыть источник</a>'])
    return "\n".join(parts)


def format_message_with_ai_summary(
    source: dict[str, Any],
    entry: ChangelogEntry,
    ai_summary: str | None,
) -> str:
    message = format_message(source, entry)
    if not ai_summary:
        return message

    lines = message.splitlines()
    if not lines:
        return message

    summary_line = f"<b>Кратко:</b> {html.escape(ai_summary)}"
    date_prefix = "<b>Дата:</b> "
    insertion_index = 0

    for index, line in enumerate(lines):
        if line.startswith(date_prefix):
            insertion_index = index
            break

    # Keep summary in the order: header -> (title/date) -> summary -> body.
    # Use visual separators for readability.
    lines[insertion_index + 1 : insertion_index + 1] = ["—", summary_line, "—"]
    return "\n".join(lines)


def format_summary_entry(
    source: dict[str, Any],
    entry: ChangelogEntry,
    ai_summary: str | None = None,
) -> str:
    product = html.escape(str(source.get("product") or source["id"]))
    version = html.escape(entry.version)
    date = html.escape(format_date_with_tz(entry.date)) if entry.date else "не указана"
    url = html.escape(entry.url, quote=True)
    lines: list[str] = [
        f"🔹 <b>{product}</b> · <code>{version}</code>",
        f"<b>Дата:</b> {date}",
    ]
    if ai_summary:
        lines.append(f"<b>Кратко:</b> {html.escape(ai_summary)}")
    lines.append(f"<a href=\"{url}\">Открыть</a>")
    return "\n".join(lines)


def should_include_ai_summary_in_digest() -> bool:
    return ai_summary_enabled() and ai_summary_in_digest_enabled()


async def build_aggregate_summary(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    entries: list[tuple[dict[str, Any], ChangelogEntry]],
    *,
    dry_run: bool,
    ai_summary_cycle_cache: dict[tuple[str, str, str, str], str | None] | None = None,
) -> str:
    lines: list[str] = ["📌 <b>Сводка новых релизов</b>"]
    include_ai_summary = should_include_ai_summary_in_digest()
    for source, entry in entries:
        ai_summary = None
        if include_ai_summary:
            ai_summary = await get_or_generate_ai_summary(
                conn,
                client,
                source,
                entry,
                dry_run=dry_run,
                cycle_cache=ai_summary_cycle_cache,
            )
        lines.append("")
        lines.append(format_summary_entry(source, entry, ai_summary))
    return "\n".join(lines)


async def send_telegram_message(client: httpx.AsyncClient, token: str, chat_id: str, text: str) -> None:
    api_url = f"https://api.telegram.org/bot{token}/sendMessage"
    response = await client.post(
        api_url,
        json={
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
    )
    response.raise_for_status()


async def send_telegram_message_chunks(client: httpx.AsyncClient, token: str, chat_id: str, text: str) -> None:
    for chunk in chunk_telegram_html_message(text):
        await send_telegram_message(client, token, chat_id, chunk)


async def telegram_api_get_result(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    *,
    params: dict[str, Any] | None = None,
) -> Any:
    response = await client.get(
        f"https://api.telegram.org/bot{token}/{method}",
        params=params or {},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("ok"):
        description = payload.get("description") if isinstance(payload, dict) else None
        raise RuntimeError(f"Telegram API method '{method}' failed: {description}")
    return payload.get("result")


async def telegram_api_post_result(
    client: httpx.AsyncClient,
    token: str,
    method: str,
    *,
    payload: dict[str, Any] | None = None,
) -> Any:
    response = await client.post(
        f"https://api.telegram.org/bot{token}/{method}",
        json=payload or {},
    )
    response.raise_for_status()
    result_payload = response.json()
    if not isinstance(result_payload, dict) or not result_payload.get("ok"):
        description = result_payload.get("description") if isinstance(result_payload, dict) else None
        raise RuntimeError(f"Telegram API method '{method}' failed: {description}")
    return result_payload.get("result")


async def get_bot_user_id(client: httpx.AsyncClient, token: str) -> str:
    result = await telegram_api_get_result(client, token, "getMe")
    if not isinstance(result, dict):
        raise RuntimeError("Telegram API getMe returned invalid payload")

    raw_id = result.get("id")
    if raw_id is None:
        raise RuntimeError("Telegram API getMe response does not include bot id")

    return str(int(raw_id))


def can_send_in_chat(member: dict[str, Any]) -> tuple[bool, str | None]:
    status = str(member.get("status") or "")

    if status in {"kicked", "left"}:
        return False, f"bot status in chat is '{status}'"

    if status not in {"creator", "administrator", "member", "restricted"}:
        return False, f"unsupported chat member status '{status}'"

    if status == "restricted":
        has_send_flags = any(
            isinstance(member.get(flag), bool) for flag in ("can_send_messages", "can_post_messages")
        )
        if not has_send_flags:
            return False, "restricted bot member has no send permission flags"

    for key in ("can_send_messages", "can_post_messages"):
        value = member.get(key)
        if isinstance(value, bool) and not value:
            return False, f"bot cannot send: {key}=false"

    return True, None


async def validate_chat_access(
    client: httpx.AsyncClient,
    token: str,
    bot_user_id: str,
    chat_id: str,
) -> ChatAccessResult:
    await telegram_api_get_result(client, token, "getChat", params={"chat_id": chat_id})

    member = await telegram_api_get_result(
        client,
        token,
        "getChatMember",
        params={"chat_id": chat_id, "user_id": bot_user_id},
    )

    if not isinstance(member, dict):
        return ChatAccessResult(chat_id=chat_id, accessible=False, reason="getChatMember returned invalid payload")

    can_send, reason = can_send_in_chat(member)
    return ChatAccessResult(chat_id=chat_id, accessible=can_send, reason=reason)


async def try_validate_chat_for_admin_command(
    client: httpx.AsyncClient,
    token: str,
    chat_id: str,
    *,
    allow_current_reply_chat: bool,
) -> tuple[bool, str | None]:
    try:
        bot_user_id = await get_bot_user_id(client, token)
        result = await validate_chat_access(client, token, bot_user_id, chat_id)
    except Exception as exc:
        if allow_current_reply_chat:
            return True, f"Telegram validation skipped after API error: {html_escape_error(exc)}"
        return False, f"Telegram validation failed; chat was not added: {html_escape_error(exc)}"
    if not result.accessible:
        return False, f"Telegram validation failed; chat was not added: {html_escape_value(result.reason)}"
    return True, None


async def validate_routing_chats(
    client: httpx.AsyncClient,
    token: str,
    routing: RoutingConfig,
) -> dict[str, bool]:
    bot_user_id = await get_bot_user_id(client, token)
    access: dict[str, bool] = {}

    for chat_id, chat in routing.chats.items():
        if not chat.enabled:
            continue

        try:
            result = await validate_chat_access(client, token, bot_user_id, chat_id)
            access[result.chat_id] = result.accessible
            if result.accessible:
                LOG.info("chat %s is accessible for bot", chat_id)
            else:
                LOG.warning("chat %s is not accessible for bot: %s", chat_id, result.reason)
        except Exception as exc:
            access[chat_id] = False
            LOG.warning("chat %s validation failed: %s", chat_id, exc)

    return access


async def run_admin_command_listener(
    telegram_token: str,
    db_path: str,
    config_path: str,
    routing_state: RoutingState,
    reload_requested: asyncio.Event,
) -> None:
    update_offset = 0
    poll_timeout = max(5, int(os.getenv("ADMIN_POLL_TIMEOUT", "25") or "25"))
    command_poll_timeout = max(0, int(os.getenv("ADMIN_COMMAND_POLL_SECONDS", "2") or "2"))
    headers = {
        "User-Agent": "changelog-watch-telegram-bot/1.0",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=60, headers=headers, follow_redirects=True) as client:
        while True:
            try:
                params = {
                    "offset": update_offset,
                    "timeout": poll_timeout,
                    "allowed_updates": ["message", "channel_post", "edited_message"],
                }
                updates = await telegram_api_post_result(client, telegram_token, "getUpdates", payload=params)
                if not isinstance(updates, list):
                    updates = []

                for update in updates:
                    if not isinstance(update, dict):
                        continue

                    update_offset_raw = update.get("update_id")
                    if isinstance(update_offset_raw, int):
                        update_offset = update_offset_raw + 1

                    message = update.get("message") or update.get("channel_post") or update.get("edited_message")
                    if not isinstance(message, dict):
                        continue

                    parsed = parse_command(message.get("text"))
                    if parsed is None:
                        continue

                    command, args = parsed
                    if command not in {
                        "reload",
                        "status",
                        "subscribe",
                        "unsubscribe",
                        "link",
                        "unlink",
                        "subscribe_here",
                        "unsubscribe_here",
                        "subscriptions",
                        "start",
                        "help",
                        "id",
                        "requestchat",
                        "addme",
                        "admins",
                        "chats",
                        "contacts",
                        "pending",
                        "approvechat",
                        "rejectchat",
                        "addchat_here",
                        "removechat",
                        "enablechat",
                        "disablechat",
                        "addadmin",
                        "removeadmin",
                        "setchatalias",
                        "setchattitle",
                        "setchatdelivery",
                        "sources",
                        "projects",
                        "source",
                        "info",
                        "testsource",
                        "addrepo",
                        "addsource",
                        "pendingsources",
                        "confirmsource",
                        "rejectsource",
                        "enablesource",
                        "disablesource",
                        "removesource",
                    }:
                        continue

                    reply_chat_id_raw = message.get("chat", {}).get("id")
                    if reply_chat_id_raw is None:
                        continue

                    reply_chat_id = str(int(str(reply_chat_id_raw).strip()))

                    if command == "id":
                        await send_telegram_message(client, telegram_token, reply_chat_id, format_id_command(message))
                        continue

                    if command in {"requestchat", "addme"}:
                        if command == "addme" and normalize_string(message.get("chat", {}).get("type")) != "private":
                            await send_telegram_message(client, telegram_token, reply_chat_id, "Use /addme only in a private chat.")
                            continue
                        try:
                            alias = normalize_alias(args[0]) if args else None
                            with db_connect(db_path) as conn:
                                pending_chat_id = upsert_pending_chat_db(conn, message, alias)
                            alias_text = f" alias=<code>{html.escape(alias)}</code>" if alias else ""
                            await send_telegram_message(
                                client,
                                telegram_token,
                                reply_chat_id,
                                f"Request saved for chat <code>{html.escape(pending_chat_id)}</code>{alias_text}.",
                            )
                        except Exception as exc:
                            await send_telegram_message(client, telegram_token, reply_chat_id, html_escape_error(exc))
                        continue

                    with db_connect(db_path) as conn:
                        runtime_config = load_runtime_config(conn, config_path)
                        sources = runtime_config["sources"]
                        poll_minutes = int(runtime_config.get("poll_minutes", 30))
                    source_ids = collect_source_ids(sources)
                    routing = routing_state.get(source_ids)
                    if not is_authorized_admin(routing.admins, (message.get("from") or {}).get("id")):
                        continue

                    if command == "reload":
                        routing_state.get(source_ids, force_reload=True)
                        reload_requested.set()
                        await send_telegram_message(
                            client,
                            telegram_token,
                            reply_chat_id,
                            "🔄 Routing config перезагружен.",
                        )
                        continue

                    if command == "pending":
                        with db_connect(db_path) as conn:
                            text = format_pending_chats_command(conn)
                        await send_telegram_message_chunks(client, telegram_token, reply_chat_id, text)
                        continue

                    if command == "status":
                        with db_connect(db_path) as conn:
                            text = format_status_command(
                                sources,
                                routing,
                                conn,
                                poll_minutes=poll_minutes,
                                db_path=db_path,
                            )
                        await send_telegram_message(client, telegram_token, reply_chat_id, text)
                        continue

                    if command == "approvechat":
                        if not args:
                            await send_telegram_message(client, telegram_token, reply_chat_id, "Usage: /approvechat &lt;chat_id&gt; [alias]")
                            continue
                        try:
                            target_chat_id = normalize_chat_id(args[0], "chat_id")
                            alias = normalize_alias(args[1]) if len(args) > 1 else None
                            with db_connect(db_path) as conn:
                                pending = conn.execute("SELECT * FROM pending_chats WHERE chat_id = ?", (target_chat_id,)).fetchone()
                                if pending is not None and alias is None:
                                    alias = normalize_alias(pending["requested_alias"])
                                title = normalize_string(pending["title"]) if pending is not None else None
                            allowed, note = await try_validate_chat_for_admin_command(
                                client,
                                telegram_token,
                                target_chat_id,
                                allow_current_reply_chat=target_chat_id == reply_chat_id,
                            )
                            if not allowed:
                                await send_telegram_message(client, telegram_token, reply_chat_id, note or "Telegram validation failed.")
                                continue
                            with db_connect(db_path) as conn:
                                upsert_chat_db(conn, target_chat_id, alias=alias, title=title, enabled=True)
                                conn.execute("DELETE FROM pending_chats WHERE chat_id = ?", (target_chat_id,))
                                conn.commit()
                            routing_state.get(source_ids, force_reload=True)
                            reload_requested.set()
                            suffix = f" {note}" if note else ""
                            await send_telegram_message(client, telegram_token, reply_chat_id, f"Chat <code>{html.escape(target_chat_id)}</code> approved.{suffix}")
                        except Exception as exc:
                            await send_telegram_message(client, telegram_token, reply_chat_id, html_escape_error(exc))
                        continue

                    if command == "rejectchat":
                        if not args:
                            await send_telegram_message(client, telegram_token, reply_chat_id, "Usage: /rejectchat &lt;chat_id&gt;")
                            continue
                        try:
                            target_chat_id = normalize_chat_id(args[0], "chat_id")
                            with db_connect(db_path) as conn:
                                cursor = conn.execute("DELETE FROM pending_chats WHERE chat_id = ?", (target_chat_id,))
                                conn.commit()
                            status = "rejected" if cursor.rowcount else "not found"
                            await send_telegram_message(client, telegram_token, reply_chat_id, f"Pending chat <code>{html.escape(target_chat_id)}</code> {status}.")
                        except Exception as exc:
                            await send_telegram_message(client, telegram_token, reply_chat_id, html_escape_error(exc))
                        continue

                    if command == "addchat_here":
                        try:
                            alias = normalize_alias(args[0]) if args else None
                            allowed, note = await try_validate_chat_for_admin_command(
                                client,
                                telegram_token,
                                reply_chat_id,
                                allow_current_reply_chat=True,
                            )
                            if not allowed:
                                await send_telegram_message(client, telegram_token, reply_chat_id, note or "Telegram validation failed.")
                                continue
                            with db_connect(db_path) as conn:
                                upsert_chat_db(conn, reply_chat_id, alias=alias, title=chat_title_from_message(message), enabled=True)
                                conn.execute("DELETE FROM pending_chats WHERE chat_id = ?", (reply_chat_id,))
                                conn.commit()
                            routing_state.get(source_ids, force_reload=True)
                            reload_requested.set()
                            suffix = f" {note}" if note else ""
                            await send_telegram_message(client, telegram_token, reply_chat_id, f"Chat <code>{html.escape(reply_chat_id)}</code> added.{suffix}")
                        except Exception as exc:
                            await send_telegram_message(client, telegram_token, reply_chat_id, html_escape_error(exc))
                        continue

                    if command in {"removechat", "enablechat", "disablechat", "setchatalias", "setchattitle", "setchatdelivery"}:
                        if not args:
                            await send_telegram_message(client, telegram_token, reply_chat_id, f"Usage: /{command} &lt;chat_id|alias&gt; ...")
                            continue
                        try:
                            with db_connect(db_path) as conn:
                                if command == "removechat":
                                    changed_chat_id = remove_chat_db(conn, args[0], routing)
                                    action = "removed"
                                elif command in {"enablechat", "disablechat"}:
                                    changed_chat_id = set_chat_enabled_db(conn, args[0], routing, command == "enablechat")
                                    action = "enabled" if command == "enablechat" else "disabled"
                                elif command == "setchatalias":
                                    if len(args) < 2:
                                        raise ValueError("Usage: /setchatalias <chat_id|alias> <alias|->")
                                    changed_chat_id = set_chat_alias_db(conn, args[0], routing, None if args[1] == "-" else normalize_alias(args[1]))
                                    action = "alias updated"
                                elif command == "setchattitle":
                                    if len(args) < 2:
                                        raise ValueError("Usage: /setchattitle <chat_id|alias> <title|->")
                                    title = None if args[1] == "-" else " ".join(args[1:]).strip()
                                    changed_chat_id = set_chat_title_db(conn, args[0], routing, title)
                                    action = "title updated"
                                else:
                                    if len(args) < 2:
                                        raise ValueError("Usage: /setchatdelivery <chat_id|alias> <instant|digest|both|none>")
                                    changed_chat_id = set_chat_delivery_db(conn, args[0], routing, args[1])
                                    action = "delivery updated"
                            routing_state.get(source_ids, force_reload=True)
                            reload_requested.set()
                            await send_telegram_message(client, telegram_token, reply_chat_id, f"Chat <code>{html.escape(changed_chat_id)}</code> {html.escape(action)}.")
                        except Exception as exc:
                            await send_telegram_message(client, telegram_token, reply_chat_id, html_escape_error(exc))
                        continue

                    if command in {"addadmin", "removeadmin"}:
                        if not args:
                            await send_telegram_message(client, telegram_token, reply_chat_id, f"Usage: /{command} &lt;user_id|alias&gt; [alias]")
                            continue
                        try:
                            with db_connect(db_path) as conn:
                                if command == "addadmin":
                                    changed_user_id = add_admin_db(conn, args[0], normalize_alias(args[1]) if len(args) > 1 else None)
                                    action = "added"
                                else:
                                    changed_user_id = remove_admin_db(conn, args[0])
                                    action = "removed"
                            routing_state.get(source_ids, force_reload=True)
                            reload_requested.set()
                            await send_telegram_message(client, telegram_token, reply_chat_id, f"Admin <code>{html.escape(changed_user_id)}</code> {action}.")
                        except Exception as exc:
                            await send_telegram_message(client, telegram_token, reply_chat_id, html_escape_error(exc))
                        continue

                    if command == "testsource":
                        if not args:
                            await send_telegram_message(client, telegram_token, reply_chat_id, "Usage: /testsource &lt;source_id&gt;")
                            continue
                        try:
                            source_id = normalize_source_id(args[0])
                            source = next((item for item in sources if item.get("id") == source_id), None)
                            if source is None:
                                raise ValueError(f"source '{source_id}' not found")
                            entries = await validate_source_via_parser(client, source)
                            await send_telegram_message_chunks(client, telegram_token, reply_chat_id, format_source_preview(source, entries))
                        except Exception as exc:
                            await send_telegram_message(client, telegram_token, reply_chat_id, html_escape_error(exc))
                        continue

                    if command == "addrepo":
                        if not args:
                            await send_telegram_message(
                                client,
                                telegram_token,
                                reply_chat_id,
                                "Usage: /addrepo &lt;owner/repo|github_url&gt; [source_id] [product name...]",
                            )
                            continue
                        try:
                            source_id = args[1] if len(args) > 1 and re.fullmatch(r"[A-Za-z0-9_.:-]+", args[1]) else None
                            product_start = 2 if source_id else 1
                            product = " ".join(args[product_start:]).strip() or None
                            source = build_github_release_source(args[0], source_id, product)
                            entries = await validate_source_via_parser(client, source)
                            preview = format_source_preview(source, entries)
                            with db_connect(db_path) as conn:
                                token = stage_pending_source_db(
                                    conn,
                                    source,
                                    preview,
                                    message,
                                    action="upsert",
                                )
                            await send_telegram_message_chunks(
                                client,
                                telegram_token,
                                reply_chat_id,
                                f"{preview}\n\nStaged. Apply with <code>/confirmsource {html.escape(token)}</code>",
                            )
                        except Exception as exc:
                            await send_telegram_message(client, telegram_token, reply_chat_id, html_escape_error(exc))
                        continue

                    if command == "addsource":
                        try:
                            parsed_source_args = parse_addsource_args(args)
                            source = build_source_from_command(
                                str(parsed_source_args["source_id"]),
                                str(parsed_source_args["source_type"]),
                                str(parsed_source_args["url"]),
                                parsed_source_args["product"],
                            )
                            entries = await validate_source_via_parser(client, source)
                            preview = format_source_preview(source, entries)
                            with db_connect(db_path) as conn:
                                token = stage_pending_source_db(conn, source, preview, message, action="upsert")
                            await send_telegram_message_chunks(
                                client,
                                telegram_token,
                                reply_chat_id,
                                f"{preview}\n\nStaged. Apply with <code>/confirmsource {html.escape(token)}</code>",
                            )
                        except Exception as exc:
                            await send_telegram_message(client, telegram_token, reply_chat_id, html_escape_error(exc))
                        continue

                    if command == "pendingsources":
                        with db_connect(db_path) as conn:
                            text = format_pending_sources_command(conn)
                        await send_telegram_message_chunks(client, telegram_token, reply_chat_id, text)
                        continue

                    if command in {"confirmsource", "rejectsource"}:
                        if not args:
                            await send_telegram_message(client, telegram_token, reply_chat_id, f"Usage: /{command} &lt;token&gt;")
                            continue
                        try:
                            token = args[0]
                            if command == "confirmsource":
                                with db_connect(db_path) as conn:
                                    pending = conn.execute("SELECT source_id, config_yaml FROM pending_sources WHERE token = ?", (token,)).fetchone()
                                if pending is None:
                                    raise ValueError(f"pending source token '{token}' not found")
                                await validate_source_via_parser(
                                    client,
                                    parse_source_config_text(pending["source_id"], pending["config_yaml"]),
                                )
                            with db_connect(db_path) as conn:
                                changed_source_id = apply_pending_source_db(conn, token) if command == "confirmsource" else reject_pending_source_db(conn, token)
                            if command == "confirmsource":
                                reload_requested.set()
                                action = "applied"
                            else:
                                action = "rejected"
                            await send_telegram_message(
                                client,
                                telegram_token,
                                reply_chat_id,
                                f"Source <code>{html.escape(changed_source_id)}</code> {action}.",
                            )
                        except Exception as exc:
                            await send_telegram_message(client, telegram_token, reply_chat_id, html_escape_error(exc))
                        continue

                    if command in {"enablesource", "disablesource", "removesource"}:
                        if not args:
                            await send_telegram_message(client, telegram_token, reply_chat_id, f"Usage: /{command} &lt;source_id&gt;")
                            continue
                        try:
                            with db_connect(db_path) as conn:
                                if command == "removesource":
                                    changed_source_id = remove_source_db(conn, args[0])
                                    action = "removed"
                                else:
                                    changed_source_id = set_source_enabled_db(conn, args[0], command == "enablesource")
                                    action = "enabled" if command == "enablesource" else "disabled"
                            reload_requested.set()
                            await send_telegram_message(
                                client,
                                telegram_token,
                                reply_chat_id,
                                f"Source <code>{html.escape(changed_source_id)}</code> {action}.",
                            )
                        except Exception as exc:
                            await send_telegram_message(client, telegram_token, reply_chat_id, html_escape_error(exc))
                        continue

                    if command == "subscriptions":
                        target_chat_token = args[0] if args else reply_chat_id
                        target_chat_id = resolve_chat_identifier(target_chat_token, routing)
                        if target_chat_id is None:
                            await send_telegram_message(
                                client,
                                telegram_token,
                                reply_chat_id,
                                f"Chat <code>{html.escape(target_chat_token)}</code> not found.",
                            )
                            continue
                        await send_telegram_message_chunks(
                            client,
                            telegram_token,
                            reply_chat_id,
                            format_subscriptions_command(target_chat_id, routing),
                        )
                        continue

                    if command in {"subscribe", "unsubscribe", "link", "unlink", "subscribe_here", "unsubscribe_here"}:
                        if not args:
                            await send_telegram_message(
                                client,
                                telegram_token,
                                reply_chat_id,
                                "Usage: /subscribe &lt;source_id&gt; [chat_id|alias], /link &lt;source_id&gt; &lt;chat_id|alias&gt;, /subscribe_here &lt;source_id&gt;",
                            )
                            continue

                        source_id = normalize_source_id(args[0])
                        if source_id not in source_ids:
                            await send_telegram_message(
                                client,
                                telegram_token,
                                reply_chat_id,
                                f"Источник <code>{html.escape(source_id)}</code> отсутствует в runtime sources",
                            )
                            continue

                        target_chat_token = reply_chat_id if command.endswith("_here") else (args[1] if len(args) > 1 else reply_chat_id)
                        target_chat_id = resolve_chat_identifier(target_chat_token, routing)
                        if target_chat_id is None:
                            await send_telegram_message(
                                client,
                                telegram_token,
                                reply_chat_id,
                                f"Чат <code>{html.escape(target_chat_token)}</code> не найден. Укажи chat_id или alias из routing DB.",
                            )
                            continue

                        add_subscription = command in {"subscribe", "link", "subscribe_here"}
                        if add_subscription:
                            result = apply_chat_subscription_change(
                                db_path,
                                source_id,
                                target_chat_id,
                                add=True,
                                chat_title=str(message.get("chat", {}).get("title", "")).strip() or None,
                            )
                        else:
                            result = apply_chat_subscription_change(
                                db_path,
                                source_id,
                                target_chat_id,
                                add=False,
                            )

                        reload_requested.set()
                        routing_state.get(source_ids, force_reload=True)
                        await send_telegram_message(client, telegram_token, reply_chat_id, html.escape(result))
                        continue

                    if command == "admins":
                        await send_telegram_message_chunks(
                            client,
                            telegram_token,
                            reply_chat_id,
                            format_admins_command(routing),
                        )
                        continue

                    if command in {"chats", "contacts"}:
                        await send_telegram_message_chunks(
                            client,
                            telegram_token,
                            reply_chat_id,
                            format_chats_command(routing),
                        )
                        continue

                    if command in {"sources", "projects"}:
                        await send_telegram_message_chunks(
                            client,
                            telegram_token,
                            reply_chat_id,
                            format_sources_command(sources),
                        )
                        continue

                    if command in {"source", "info"}:
                        if not args:
                            await send_telegram_message(
                                client,
                                telegram_token,
                                reply_chat_id,
                                "Использование: /source &lt;source_id&gt; или /info &lt;source_id&gt;",
                            )
                            continue
                        await send_telegram_message_chunks(
                            client,
                            telegram_token,
                            reply_chat_id,
                            format_source_details_command(normalize_source_id(args[0]), sources, routing),
                        )
                        continue

                    if command in {"start", "help"}:
                        await send_telegram_message(
                            client,
                            telegram_token,
                            reply_chat_id,
                            format_help_command(),
                        )
                        continue

                if updates:
                    await asyncio.sleep(0)
                    continue

                if command_poll_timeout > 0:
                    await asyncio.sleep(command_poll_timeout)

            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception("admin command listener error")
                await asyncio.sleep(5)


def list_running_bot_pids() -> list[int]:
    project_root = Path(__file__).resolve().parent
    project_path_marker = str(project_root)
    pids: set[int] = set()

    proc_root = Path("/proc")
    if not proc_root.exists():
        return []

    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue

        try:
            pid = int(entry.name)
        except ValueError:
            continue

        try:
            with open(entry / "cmdline", "rb") as f:
                raw_cmd = f.read()
            if not raw_cmd:
                continue
            args = [part for part in raw_cmd.split(b"\x00") if part]
            if not args:
                continue
        except OSError:
            continue

        has_bot_script = any(part.endswith(b"bot.py") for part in args)
        if not has_bot_script:
            continue

        try:
            cwd = Path(os.readlink(entry / "cwd")).resolve()
        except OSError:
            continue

        if project_path_marker not in str(cwd):
            continue

        has_repo_script = False
        for part in args:
            if b"changelog-watch-telegram-bot" in part:
                has_repo_script = True
                break
            if part in {b"bot.py"}:
                has_repo_script = True
                break

        if not has_repo_script:
            continue

        pids.add(pid)

    return sorted(pids)


async def notify_if_multiple_instances(
    client: httpx.AsyncClient,
    telegram_token: str,
    admin_ids: set[str],
    lock_path: str | None = None,
) -> None:
    global _duplicate_instances_alert_sent
    if not telegram_token or not admin_ids or _duplicate_instances_alert_sent:
        return

    pids = list_running_bot_pids()
    if len(pids) <= 1:
        return

    current_pid = os.getpid()
    if current_pid not in pids:
        return

    if current_pid != min(pids):
        return

    state_file = _instance_alert_state_file("multiple-instances", lock_path)
    marker = ",".join(map(str, pids))
    if not _should_send_alert_with_cooldown(
        state_file,
        marker=marker,
        cooldown_seconds=_DUPLICATE_INSTANCE_ALERT_COOLDOWN_SECONDS,
    ):
        return

    message = (
        "⚠️ Обнаружено несколько запущенных экземпляров changelog-watch-telegram-bot. "
        f"Сейчас запущено: {len(pids)} (pid: {', '.join(map(str, pids))})."
    )

    for admin_id in sorted(admin_ids):
        try:
            await send_telegram_message(client, telegram_token, admin_id, message)
        except Exception:
            LOG.exception("failed to notify admin %s about duplicate bot processes", admin_id)

    _duplicate_instances_alert_sent = True


async def check_source(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    source: dict[str, Any],
    *,
    dry_run: bool = False,
) -> tuple[list[ChangelogEntry], list[ChangelogEntry]]:
    source_id = source["id"]
    entries = await parse_source(client, source)
    if not entries:
        LOG.warning("[%s] no entries found", source_id)
        return [], []

    initialized = is_source_initialized(conn, source_id)

    if not initialized and not source.get("post_on_first_run", False):
        if not dry_run:
            claimed_entries = claim_new_posts(conn, source_id, entries)
            if len(claimed_entries) != len(entries):
                LOG.debug(
                    "[%s] first-run seed had already posted %d/%d items",
                    source_id,
                    len(entries) - len(claimed_entries),
                    len(entries),
                )
            mark_source_initialized(conn, source_id)
        LOG.info("[%s] initialized with %d existing entries; nothing posted", source_id, len(entries))
        return entries, []

    if dry_run:
        new_entries = [entry for entry in entries if not is_posted(conn, source_id, entry.item_id)]
    else:
        new_entries = claim_new_posts(conn, source_id, entries)

    if not initialized:
        first_run_limit = int(source.get("first_run_limit", 1))
        new_entries = new_entries[:first_run_limit]

    if not new_entries:
        LOG.info("[%s] no new entries", source_id)
        if not initialized and not dry_run:
            mark_source_initialized(conn, source_id)
        return entries, []

    if not dry_run and not initialized:
        # For a first run we still keep state consistent by storing all seen entries,
        # but deliver only up to first_run_limit.
        mark_source_initialized(conn, source_id)

    return entries, new_entries


async def send_summary(
    client: httpx.AsyncClient,
    telegram_token: str,
    chat_id: str,
    entries: list[tuple[dict[str, Any], ChangelogEntry]],
    conn: sqlite3.Connection,
    dry_run: bool,
    ai_summary_cycle_cache: dict[tuple[str, str, str, str], str | None] | None = None,
) -> bool:
    if not entries:
        return False

    msg = await build_aggregate_summary(
        conn,
        client,
        entries,
        dry_run=dry_run,
        ai_summary_cycle_cache=ai_summary_cycle_cache,
    )
    if dry_run:
        LOG.info("[summary] DRY RUN would post aggregate:")
        LOG.info("%s", msg)
        return True

    await send_telegram_message(client, telegram_token, chat_id, msg)
    return True


async def send_summaries(
    client: httpx.AsyncClient,
    telegram_token: str,
    entries_by_chat: dict[str, list[tuple[dict[str, Any], ChangelogEntry]]],
    conn: sqlite3.Connection,
    dry_run: bool,
    ai_summary_cycle_cache: dict[tuple[str, str, str, str], str | None] | None = None,
) -> None:
    if not entries_by_chat:
        return
    for chat_id, entries in entries_by_chat.items():
        await send_summary(client, telegram_token, chat_id, entries, conn, dry_run, ai_summary_cycle_cache)


def finalize_sent_summary_queue(
    conn: sqlite3.Connection,
    chat: ChatRouting,
    queued: list[tuple[str, ChangelogEntry]],
    sent_at: datetime,
) -> None:
    clear_summary_queue_items(conn, chat.chat_id, queued)
    for source_id, entry in queued:
        mark_delivery_status(
            conn,
            source_id,
            entry.item_id,
            chat.chat_id,
            sent=True,
        )
    if chat.summary_schedule.mode != "immediate":
        mark_chat_summary_sent(conn, chat.chat_id, sent_at)


async def check_all(
    config_path: str,
    db_path: str,
    dry_run: bool = False,
    routing_state: RoutingState | None = None,
    force_routing_reload: bool = False,
    lock_path: str | None = None,
    started_at: datetime | None = None,
) -> None:
    load_dotenv()
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    routing_config_path = get_routing_config_path()
    conn = db_connect_runtime(db_path, dry_run=dry_run)
    config = load_runtime_config(conn, config_path)
    routing_sources = config["sources"]
    source_ids = collect_source_ids(routing_sources)
    if routing_state is None:
        routing_state = RoutingState(
            db_path=db_path,
            source_config_path=routing_config_path,
            ttl_seconds=0,
            dry_run=dry_run,
        )

    routing = routing_state.get(source_ids, force_reload=force_routing_reload)
    source_to_chat = build_source_to_chat_map(routing_sources, routing)
    source_lookup = {source["id"]: source for source in routing_sources}

    if not routing.chats:
        raise RuntimeError("routing config does not define any chats")

    enabled_chat_ids = {chat_id for chat_id, chat in routing.chats.items() if chat.enabled}
    if not enabled_chat_ids:
        raise RuntimeError("routing config has no enabled chats")

    if not dry_run and (not telegram_token):
        raise RuntimeError("TELEGRAM_BOT_TOKEN must be set in .env")

    now = datetime.now(timezone.utc)
    _, schedule_tz = resolve_display_timezone()
    schedule_now = now.astimezone(schedule_tz)
    accessible_chat_ids = {chat_id for chat_id in enabled_chat_ids}

    headers = {
        "User-Agent": "changelog-watch-telegram-bot/1.0",
        "Accept": "text/html,text/markdown,text/plain,application/json,*/*",
    }
    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
        if not dry_run and duplicate_instance_notifications_are_enabled():
            duplicate_admin_ids = _load_admin_ids_from_env() or routing.admins
            await notify_if_multiple_instances(client, telegram_token, duplicate_admin_ids, lock_path=lock_path)

        summary_items_by_chat: defaultdict[str, list[tuple[dict[str, Any], ChangelogEntry]]] = defaultdict(list)
        instant_posts_by_chat: defaultdict[str, list[tuple[dict[str, Any], ChangelogEntry]]] = defaultdict(list)
        ai_summary_cycle_cache: dict[tuple[str, str, str, str], str | None] = {}

        if not dry_run:
            chat_access = await validate_routing_chats(client, telegram_token, routing)
            accessible_chat_ids = {
                chat_id for chat_id in enabled_chat_ids if chat_access.get(chat_id)
            }

            if accessible_chat_ids != enabled_chat_ids:
                skipped_chat_ids = sorted(enabled_chat_ids - accessible_chat_ids)
                for skipped_chat_id in skipped_chat_ids:
                    LOG.warning("chat %s is disabled for this run due to access validation", skipped_chat_id)

            if not accessible_chat_ids:
                LOG.warning("no enabled chats passed Telegram access validation; messages will not be sent")

        for source in config["sources"]:
            if source.get("enabled", True) is False:
                LOG.info("[%s] disabled; skipping", source.get("id", "unknown"))
                continue
            try:
                configured_chat_ids = source_to_chat.get(source["id"], [])
                target_chat_ids = (
                    configured_chat_ids
                    if dry_run
                    else [chat_id for chat_id in configured_chat_ids if chat_id in accessible_chat_ids]
                )

                if configured_chat_ids != target_chat_ids and configured_chat_ids:
                    skipped_chat_ids = [chat_id for chat_id in configured_chat_ids if chat_id not in accessible_chat_ids]
                    LOG.warning(
                        "[%s] skipped %d chat(s) without send rights: %s",
                        source.get("id", "unknown"),
                        len(skipped_chat_ids),
                        ", ".join(skipped_chat_ids),
                    )

                source_entries, new_entries = await check_source(conn, client, source, dry_run=dry_run)
                if not source_entries:
                    continue

                for chat_id in target_chat_ids:
                    chat = routing.chats.get(chat_id)
                    if not chat:
                        continue

                    should_send_instant = chat.delivery_mode in {"instant", "both"}
                    should_queue = (
                        chat.delivery_mode in {"digest", "both"}
                        and chat.summary_schedule.mode != "none"
                    )
                    if not should_send_instant and not should_queue:
                        continue

                    candidate_ids = {entry.item_id for entry in new_entries}
                    if should_send_instant:
                        candidate_ids.update(load_failed_delivery_item_ids(conn, chat_id, source["id"]))

                    if not candidate_ids:
                        continue

                    ordered_candidate_entries: list[ChangelogEntry] = []
                    for entry in reversed(source_entries):
                        if entry.item_id not in candidate_ids:
                            continue
                        ordered_candidate_entries.append(entry)

                    if should_send_instant:
                        instant_posts_by_chat[chat_id].extend(
                            (source, entry)
                            for entry in ordered_candidate_entries
                            if not is_delivered_to_chat(conn, source["id"], entry.item_id, chat_id)
                        )

                    if should_queue:
                        if dry_run:
                            summary_items_by_chat[chat_id].extend((source, entry) for entry in ordered_candidate_entries)
                        else:
                            enqueue_summary_items(conn, chat_id, source["id"], ordered_candidate_entries)
            except Exception:
                LOG.exception("[%s] failed", source.get("id", "unknown"))

        for chat in routing.chats.values():
            if not chat.enabled or chat.delivery_mode == "none":
                continue

            if not dry_run and chat.chat_id not in accessible_chat_ids:
                continue

            should_send_summary = (
                chat.delivery_mode in {"digest", "both"}
                and chat.summary_schedule.mode != "none"
            )
            queued = load_summary_queue_items(conn, chat.chat_id) if should_send_summary else []
            entries_for_chat: list[tuple[dict[str, Any], ChangelogEntry]] = []
            seen_for_chat: set[tuple[str, str]] = set()

            if should_send_summary:
                for source_id, entry in queued:
                    source = source_lookup.get(source_id, {"id": source_id})
                    seen_for_chat.add((str(source_id), entry.item_id))
                    entries_for_chat.append((source, entry))

                if dry_run:
                    for source, entry in summary_items_by_chat.get(chat.chat_id, []):
                        source_id = str(source.get("id", ""))
                        key = (source_id, entry.item_id)
                        if key in seen_for_chat:
                            continue
                        seen_for_chat.add(key)
                        entries_for_chat.append((source, entry))

            summary_sent = False
            if should_send_summary and entries_for_chat:
                summary_due = chat.summary_schedule.mode == "immediate" or is_summary_due(
                    chat.summary_schedule,
                    schedule_now,
                    chat.last_summary_sent_at,
                )
                if summary_due:
                    suppress_summary, suppressed_boundary = should_suppress_summary_on_startup(
                        chat,
                        schedule_now,
                        started_at,
                    )
                    if suppress_summary:
                        LOG.info(
                            "summary for chat %s suppressed on startup until next schedule boundary",
                            chat.chat_id,
                        )
                        if not dry_run and suppressed_boundary is not None:
                            mark_chat_summary_sent(conn, chat.chat_id, suppressed_boundary)
                        summary_due = False

                if summary_due:
                    try:
                        summary_sent = await send_summary(
                            client,
                            telegram_token,
                            chat.chat_id,
                            entries_for_chat,
                            conn,
                            dry_run=dry_run,
                            ai_summary_cycle_cache=ai_summary_cycle_cache,
                        )
                    except Exception:
                        LOG.exception("failed to send summary to %s", chat.chat_id)

            finalize_summary_queue = summary_sent and not dry_run

            if chat.delivery_mode not in {"instant", "both"}:
                if finalize_summary_queue:
                    finalize_sent_summary_queue(conn, chat, queued, now)
                continue

            for source, entry in instant_posts_by_chat.get(chat.chat_id, []):
                if not dry_run:
                    if is_delivered_to_chat(conn, source["id"], entry.item_id, chat.chat_id):
                        continue

                ai_summary = await get_or_generate_ai_summary(
                    conn,
                    client,
                    source,
                    entry,
                    dry_run=dry_run,
                    cycle_cache=ai_summary_cycle_cache,
                )
                msg = format_message_with_ai_summary(source, entry, ai_summary)
                if dry_run:
                    LOG.info(
                        "[%s] DRY RUN would post %s to %s:\n%s",
                        source["id"],
                        entry.item_id,
                        chat.chat_id,
                        msg,
                    )
                    continue

                try:
                    await send_telegram_message(client, telegram_token, chat.chat_id, msg)
                    mark_delivery_status(conn, source["id"], entry.item_id, chat.chat_id, sent=True)
                    LOG.info("[%s] posted %s to %s", source["id"], entry.item_id, chat.chat_id)
                except Exception as exc:
                    mark_delivery_status(
                        conn,
                        source["id"],
                        entry.item_id,
                        chat.chat_id,
                        sent=False,
                        error=str(exc),
                    )
                    LOG.exception("[%s] failed to post %s to %s", source["id"], entry.item_id, chat.chat_id)

            if finalize_summary_queue:
                finalize_sent_summary_queue(conn, chat, queued, now)
    conn.close()


async def run_scheduler(
    config_path: str,
    db_path: str,
    dry_run: bool,
    lock_path: str | None = None,
    started_at: datetime | None = None,
) -> None:
    config = load_config(config_path)
    poll_minutes = int(config.get("poll_minutes", 30))
    poll_seconds = max(1, poll_minutes * 60)
    routing_ttl_seconds = int(os.getenv("ROUTING_RELOAD_TTL_SECONDS", "0") or "0")
    if routing_ttl_seconds < 0:
        raise RuntimeError("ROUTING_RELOAD_TTL_SECONDS must be >= 0")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    routing_state = RoutingState(
        db_path=db_path,
        source_config_path=get_routing_config_path(),
        ttl_seconds=routing_ttl_seconds,
        dry_run=dry_run,
    )

    loop = asyncio.get_running_loop()
    reload_requested = asyncio.Event()
    shutdown_requested = asyncio.Event()

    def _request_routing_reload() -> None:
        if not reload_requested.is_set():
            LOG.info("received hot-reload signal, forcing routing state reload")
            reload_requested.set()

    def _request_scheduler_shutdown() -> None:
        if not shutdown_requested.is_set():
            LOG.info("received termination signal, stopping scheduler loop")
            shutdown_requested.set()

    for sig_name, handler in (
        ("SIGHUP", _request_routing_reload),
        ("SIGUSR1", _request_routing_reload),
        ("SIGINT", _request_scheduler_shutdown),
        ("SIGTERM", _request_scheduler_shutdown),
    ):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, handler)
        except (RuntimeError, OSError):
            LOG.debug("signal handler for %s is not supported", sig_name)

    LOG.info("scheduler loop started; interval=%s minutes, routing_ttl=%s seconds", poll_minutes, routing_ttl_seconds)

    admin_command_task = None
    if not dry_run and telegram_token:
        admin_command_task = asyncio.create_task(
            run_admin_command_listener(
                telegram_token=telegram_token,
                db_path=db_path,
                config_path=config_path,
                routing_state=routing_state,
                reload_requested=reload_requested,
            )
        )

    try:
        while True:
            force = reload_requested.is_set()
            if force:
                reload_requested.clear()

            try:
                await check_all(
                    config_path,
                    db_path,
                    dry_run=dry_run,
                    routing_state=routing_state,
                    force_routing_reload=force,
                    lock_path=lock_path,
                    started_at=started_at,
                )
            except Exception:
                LOG.exception("cycle failed")

            if shutdown_requested.is_set():
                break

            deadline = monotonic() + poll_seconds
            while monotonic() < deadline:
                if shutdown_requested.is_set() or reload_requested.is_set():
                    break

                wait_timeout = min(1.0, deadline - monotonic())
                try:
                    await asyncio.wait_for(reload_requested.wait(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    continue

            if shutdown_requested.is_set():
                break
    finally:
        if admin_command_task is not None:
            admin_command_task.cancel()
            try:
                await admin_command_task
            except asyncio.CancelledError:
                pass


def validate_source_config(config: dict[str, Any]) -> set[str]:
    sources = config["sources"]
    source_ids = collect_source_ids(sources)
    supported_types = {"html_changelog", "markdown_changelog", "github_releases"}

    for source in sources:
        source_id = source["id"]
        source_type = normalize_string(source.get("type"))
        if source_type not in supported_types:
            raise ValueError(f"source {source_id} has unsupported type: {source_type!r}")

        source_url = normalize_string(source.get("url"))
        if not source_url:
            raise ValueError(f"source {source_id} must define url")

        if source_type == "github_releases":
            github_repo_from_url(source_url)

    return source_ids


def validate_config_files(config_path: str, db_path: str, *, migrate_db: bool = False) -> None:
    errors: list[str] = []
    source_ids: set[str] = set()

    try:
        config = load_config(config_path)
        source_ids = validate_source_config(config)
    except Exception as exc:
        errors.append(f"products config: {exc}")

    try:
        get_routing_seed_mode()
    except Exception as exc:
        errors.append(str(exc))

    try:
        db_factory = db_connect if migrate_db else db_connect_for_dry_run
        with db_factory(db_path) as conn:
            ensure_source_state_seeded(conn, config_path)
            source_ids = collect_source_ids(load_sources_from_db(conn))
            routing_config_path = get_routing_config_path()
            if routing_config_path:
                load_routing_config(routing_config_path, source_ids)
            elif routing_has_data(conn):
                load_routing_config_from_db(conn, source_ids)
            else:
                errors.append(
                    "ROUTING_CONFIG_PATH is not set and routing DB is empty. "
                    "Copy admin-routing.example.yaml to admin-routing.yaml or set ROUTING_CONFIG_PATH."
                )
    except Exception as exc:
        errors.append(f"routing/DB validation: {exc}")

    if errors:
        raise RuntimeError("config validation failed:\n- " + "\n- ".join(errors))

    migration_note = "with DB migration" if migrate_db else "without DB writes"
    LOG.info("config validation passed (%s)", migration_note)


def load_validated_routing_seed(conn: sqlite3.Connection, config_path: str) -> RoutingConfig:
    ensure_source_state_seeded(conn, config_path)
    source_ids = collect_source_ids(load_sources_from_db(conn))
    routing_config_path = get_routing_config_path()
    if not routing_config_path:
        raise RuntimeError("ROUTING_CONFIG_PATH is required for --import-routing")
    return load_routing_config(routing_config_path, source_ids)


def import_routing_from_seed(config_path: str, db_path: str, *, replace: bool) -> None:
    with db_connect(db_path) as conn:
        routing = load_validated_routing_seed(conn, config_path)
        import_routing_config_to_db(conn, routing, replace=replace)
    mode = "replaced" if replace else "merged"
    LOG.info(
        "routing %s from seed: admins=%d groups=%d chats=%d",
        mode,
        len(routing.admins),
        len(routing.source_groups),
        len(routing.chats),
    )


def export_settings_to_yaml(config_path: str, db_path: str, export_path: str) -> None:
    with db_connect(db_path) as conn:
        ensure_source_state_seeded(conn, config_path)
        sources = load_sources_from_db(conn)
        source_ids = collect_source_ids(sources)
        routing = load_routing_config_from_db(conn, source_ids)

    path = Path(export_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            {"sources": sources, **routing_config_to_yaml_data(routing)},
            f,
            allow_unicode=True,
            sort_keys=False,
        )
    LOG.info(
        "settings exported to %s: sources=%d admins=%d groups=%d chats=%d",
        path,
        len(sources),
        len(routing.admins),
        len(routing.source_groups),
        len(routing.chats),
    )


def import_settings_from_yaml(config_path: str, db_path: str, import_path: str, *, replace: bool) -> None:
    settings_data = load_yaml_file(import_path)
    sources = settings_data.get("sources")
    if not isinstance(sources, list):
        raise ValueError("settings import must contain top-level 'sources' list")
    source_ids = validate_source_config({"sources": sources})
    routing = load_routing_config_data(settings_data, source_ids)
    with db_connect(db_path) as conn:
        foreign_keys_enabled = bool(conn.execute("PRAGMA foreign_keys").fetchone()[0])
        try:
            if replace:
                conn.execute("PRAGMA foreign_keys = OFF")
            conn.execute("BEGIN")
            import_sources_to_db(conn, sources, replace=replace, commit=False)
            import_routing_config_to_db(conn, routing, replace=replace, commit=False)
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if replace:
                conn.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys_enabled else 'OFF'}")
    mode = "replaced" if replace else "merged"
    LOG.info(
        "settings %s from %s: sources=%d admins=%d groups=%d chats=%d",
        mode,
        import_path,
        len(sources),
        len(routing.admins),
        len(routing.source_groups),
        len(routing.chats),
    )


def clear_summary_queue_rows(conn: sqlite3.Connection, chat_id: str | None = None) -> int:
    if chat_id:
        normalized_chat_id = normalize_chat_id(chat_id, "--chat-id")
        cursor = conn.execute("DELETE FROM summary_queue WHERE chat_id = ?", (normalized_chat_id,))
    else:
        cursor = conn.execute("DELETE FROM summary_queue")
    conn.commit()
    return cursor.rowcount if cursor.rowcount is not None else 0


def clear_summary_queue_command(db_path: str, chat_id: str | None = None) -> None:
    with db_connect(db_path) as conn:
        removed_rows = clear_summary_queue_rows(conn, chat_id)
    target = f"chat {normalize_chat_id(chat_id, '--chat-id')}" if chat_id else "all chats"
    LOG.info("summary queue cleared for %s: removed %d row(s)", target, removed_rows)


def warn_legacy_telegram_chat_id() -> None:
    if env_text("TELEGRAM_CHAT_ID"):
        LOG.warning(
            "TELEGRAM_CHAT_ID is legacy and ignored by routing mode. "
            "Add this chat_id to admin-routing.yaml."
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram changelog watcher")
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "products.yaml"))
    parser.add_argument("--db", default=os.getenv("DB_PATH", "data/posted.sqlite3"))
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram messages; log what would be posted")
    parser.add_argument("--validate-config", action="store_true", help="Validate local config and DB schema without network calls")
    parser.add_argument("--migrate-db", action="store_true", help="Allow --validate-config to migrate the real DB")
    parser.add_argument("--import-routing", action="store_true", help="Import routing seed from ROUTING_CONFIG_PATH")
    parser.add_argument("--export-settings", help="Export runtime routing settings to YAML")
    parser.add_argument("--import-settings", help="Import runtime routing settings from YAML")
    parser.add_argument("--replace", action="store_true", help="Replace routing tables during --import-routing or --import-settings")
    parser.add_argument("--clear-summary-queue", action="store_true", help="Clear digest summary queue without network calls")
    parser.add_argument("--chat-id", help="Limit --clear-summary-queue to one Telegram chat id")
    args = parser.parse_args()

    if args.migrate_db and not args.validate_config:
        parser.error("--migrate-db requires --validate-config")
    if args.replace and not (args.import_routing or args.import_settings):
        parser.error("--replace requires --import-routing or --import-settings")
    if args.chat_id and not args.clear_summary_queue:
        parser.error("--chat-id requires --clear-summary-queue")
    if args.validate_config and (args.import_routing or args.export_settings or args.import_settings or args.clear_summary_queue):
        parser.error("--validate-config cannot be combined with import/export/clear commands")
    if args.import_routing and (args.export_settings or args.import_settings):
        parser.error("--import-routing cannot be combined with --export-settings or --import-settings")
    if args.export_settings and args.import_settings:
        parser.error("--export-settings cannot be combined with --import-settings")

    return args


def main() -> None:
    load_dotenv()
    setup_logging()
    args = parse_args()
    warn_legacy_telegram_chat_id()
    if args.validate_config:
        validate_config_files(args.config, args.db, migrate_db=args.migrate_db)
        return
    if args.import_routing:
        import_routing_from_seed(args.config, args.db, replace=args.replace)
        if args.clear_summary_queue:
            clear_summary_queue_command(args.db, args.chat_id)
        return
    if args.export_settings:
        export_settings_to_yaml(args.config, args.db, args.export_settings)
        return
    if args.import_settings:
        import_settings_from_yaml(args.config, args.db, args.import_settings, replace=args.replace)
        if args.clear_summary_queue:
            clear_summary_queue_command(args.db, args.chat_id)
        return
    if args.clear_summary_queue:
        clear_summary_queue_command(args.db, args.chat_id)
        return

    lock_path = env_text("BOT_INSTANCE_LOCK_PATH")
    lock_held = False
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    routing_config_path = get_routing_config_path()
    lifecycle_notifications_enabled = lifecycle_notifications_are_enabled()
    lifecycle_notifications_active = False
    startup_at = datetime.now(timezone.utc)
    lifecycle_error: BaseException | None = None

    if not args.dry_run:
        lock_acquired, lock_owner_pid = acquire_single_instance_lock(lock_path)
        if not lock_acquired:
            message_owner = str(lock_owner_pid) if lock_owner_pid else "неизвестен"
            LOG.error("single-instance lock is already held; another bot instance is running (pid=%s)", message_owner)
            try:
                telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
                routing_config_path = get_routing_config_path()
                if telegram_token and duplicate_instance_notifications_are_enabled():
                    asyncio.run(
                        notify_single_instance_lock_conflict(
                            telegram_token=telegram_token,
                            routing_config_path=routing_config_path,
                            lock_owner_pid=lock_owner_pid,
                            lock_path=lock_path,
                        )
                    )
            except Exception:
                LOG.exception("failed to notify admins about duplicate instance launch")
            return
        lock_held = True

    if not args.dry_run and lifecycle_notifications_enabled:
        try:
            mode = "--once" if args.once else "continuous"
            startup_message = (
                "✅ Бот changelog-watch-telegram-bot запущен."
                f" Режим: {mode}."
                f" PID: {os.getpid()}."
                f" Config: {args.config}."
                f" DB: {args.db}."
                f" Время запуска: {startup_at.strftime('%Y-%m-%d %H:%M:%S UTC')}."
            )
            asyncio.run(
                notify_admin_lifecycle_event(
                    telegram_token=telegram_token,
                    routing_config_path=routing_config_path,
                    message=startup_message,
                )
            )
            lifecycle_notifications_active = bool(telegram_token and load_admin_ids_for_notifications(routing_config_path))
        except Exception:
            LOG.exception("failed to send bot startup admin notification")

    try:
        if args.once:
            asyncio.run(check_all(args.config, args.db, dry_run=args.dry_run, lock_path=lock_path, started_at=startup_at))
        else:
            asyncio.run(run_scheduler(args.config, args.db, dry_run=args.dry_run, lock_path=lock_path, started_at=startup_at))
    except BaseException as exc:
        lifecycle_error = exc
        raise
    finally:
        if lifecycle_notifications_active and not args.dry_run:
            stopped_at = datetime.now(timezone.utc)
            elapsed_seconds = max(0.0, (stopped_at - startup_at).total_seconds())

            if lifecycle_error is None:
                stop_reason = "остановлен"
            elif isinstance(lifecycle_error, KeyboardInterrupt):
                stop_reason = "получен сигнал остановки"
            elif isinstance(lifecycle_error, SystemExit):
                stop_reason = "завершение по команде"
            else:
                stop_reason = f"ошибка: {type(lifecycle_error).__name__}"

            stop_message = (
                "⛔ Бот changelog-watch-telegram-bot остановлен."
                f" Причина: {stop_reason}."
                f" Режим: {'--once' if args.once else 'continuous'}."
                f" Время остановки: {stopped_at.strftime('%Y-%m-%d %H:%M:%S UTC')}."
                f" Время работы: {elapsed_seconds:.1f} сек."
                f" PID: {os.getpid()}."
            )
            try:
                asyncio.run(
                    notify_admin_lifecycle_event(
                        telegram_token=telegram_token,
                        routing_config_path=routing_config_path,
                        message=stop_message,
                    )
                )
            except Exception:
                LOG.exception("failed to send bot stop admin notification")

        if lock_held:
            release_single_instance_lock()


if __name__ == "__main__":
    main()
