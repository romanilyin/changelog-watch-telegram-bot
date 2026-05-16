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
import os
import re
import signal
from collections import defaultdict
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from bs4 import BeautifulSoup
from dotenv import load_dotenv

LOG = logging.getLogger("changelog-watch-bot")

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


@dataclass(frozen=True)
class SummarySchedule:
    mode: str
    time: str
    weekday: int | None = None

    @classmethod
    def immediate(cls) -> "SummarySchedule":
        return cls(mode="immediate", time="00:00", weekday=None)


def normalize_alias(value: Any) -> str | None:
    alias = normalize_string(value)
    if not alias:
        return None
    return alias.lower()


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
    summary_schedule: SummarySchedule = field(default_factory=SummarySchedule.immediate)
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
        with db_connect(self.db_path) as conn:
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
    if routing_has_data(conn):
        return

    if source_config_path is None:
        raise RuntimeError("routing state is empty and ROUTING_CONFIG_PATH is not set")

    source_path = Path(source_config_path)
    if not source_path.exists():
        raise RuntimeError(f"routing seed file not found: {source_config_path}")

    route_config = load_routing_config(source_path, source_ids)
    import_routing_config_to_db(conn, route_config)


def import_routing_config_to_db(conn: sqlite3.Connection, routing: RoutingConfig) -> None:
    admin_alias_lookup: dict[str, str] = {admin_id: alias for alias, admin_id in routing.admin_aliases.items()}
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
                chat_id, title, enabled, send_summary, alias,
                summary_mode, summary_time, summary_weekday
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id) DO UPDATE SET
                title = excluded.title,
                enabled = excluded.enabled,
                send_summary = excluded.send_summary,
                alias = excluded.alias,
                summary_mode = excluded.summary_mode,
                summary_time = excluded.summary_time,
                summary_weekday = excluded.summary_weekday
            """,
            (
                chat.chat_id,
                chat.title,
                int(chat.enabled),
                int(chat.send_summary),
                chat.alias,
                chat.summary_schedule.mode,
                chat.summary_schedule.time,
                chat.summary_schedule.weekday,
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
            "INSERT OR IGNORE INTO routing_chats(chat_id, title, enabled, send_summary) VALUES (?, ?, 1, 1)",
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


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


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
        return SummarySchedule.immediate()

    if isinstance(raw_schedule, str):
        mode = normalize_string(raw_schedule).lower()
        if not mode:
            return SummarySchedule.immediate()
        if mode not in {"immediate", "on", "enabled", "true"}:
            raise ValueError(f"{context} summary schedule mode is invalid: {mode!r}")
        return SummarySchedule.immediate()

    if not isinstance(raw_schedule, dict):
        raise ValueError(f"{context} summary_schedule must be an object")

    mode = normalize_string(raw_schedule.get("mode") or raw_schedule.get("kind") or raw_schedule.get("frequency"))
    if not mode:
        mode = "immediate"
    mode = mode.lower()
    if mode not in {"immediate", "daily", "weekly"}:
        raise ValueError(f"{context} summary_schedule.mode must be one of immediate|daily|weekly: {mode!r}")

    if mode == "immediate":
        return SummarySchedule.immediate()

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
    summary_mode = normalize_string(row["summary_mode"]) if "summary_mode" in row.keys() else "immediate"
    summary_time = normalize_string(row["summary_time"]) if "summary_time" in row.keys() else "00:00"
    weekday = row["summary_weekday"] if "summary_weekday" in row.keys() else None

    try:
        return parse_summary_schedule(
            {
                "mode": summary_mode or "immediate",
                "time": summary_time,
                "weekday": weekday,
            },
            f"chat {chat_id}",
        )
    except ValueError as exc:
        LOG.warning("chat %s has invalid summary schedule in DB, fallback to immediate: %s", chat_id, exc)
        return SummarySchedule.immediate()


def parse_sqlite_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def is_summary_due(schedule: SummarySchedule, now: datetime, last_sent_at: str | None) -> bool:
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
            return True
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


def load_routing_config(path: str | Path, source_ids: set[str]) -> RoutingConfig:
    data = load_yaml_file(path)

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
        enabled = bool(raw_chat.get("enabled", True))
        send_summary = bool(raw_chat.get("send_summary", True))
        summary_schedule = parse_summary_schedule(raw_chat.get("summary_schedule"), f"chats[{idx}]")

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
            summary_schedule=summary_schedule,
        )

    return RoutingConfig(admins=admins, admin_aliases=admin_aliases, source_groups=source_groups, chats=chats)


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
        alias = normalize_alias(raw_chat["alias"]) if raw_chat["alias"] is not None else None
        summary_schedule = parse_summary_schedule_from_db(raw_chat, chat_id)
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
            summary_schedule=summary_schedule,
            last_summary_sent_at=last_summary_sent_at,
        )

    return RoutingConfig(
        admins=admins,
        admin_aliases=admin_aliases,
        source_groups=source_groups,
        chats=chats,
    )


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
        conn.execute("ALTER TABLE routing_chats ADD COLUMN summary_mode TEXT NOT NULL DEFAULT 'immediate'")
    if "summary_time" not in chat_columns:
        conn.execute("ALTER TABLE routing_chats ADD COLUMN summary_time TEXT NOT NULL DEFAULT '00:00'")
    if "summary_weekday" not in chat_columns:
        conn.execute("ALTER TABLE routing_chats ADD COLUMN summary_weekday INTEGER")
    if "last_summary_sent_at" not in chat_columns:
        conn.execute("ALTER TABLE routing_chats ADD COLUMN last_summary_sent_at TEXT")

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


def db_connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
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
            send_summary INTEGER NOT NULL DEFAULT 1
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
    ensure_routing_columns(conn)
    conn.commit()
    return conn


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


def load_summary_queue_items(
    conn: sqlite3.Connection,
    chat_id: str,
) -> list[tuple[str, ChangelogEntry]]:
    rows = conn.execute(
        """
        SELECT source_id, item_id, item_title, item_version, item_date, item_url, item_is_prerelease
        FROM summary_queue
        WHERE chat_id = ?
        ORDER BY created_at, rowid
        """,
        (chat_id,),
    ).fetchall()

    result: list[tuple[str, ChangelogEntry]] = []
    for row in rows:
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
    return result


def clear_summary_queue(conn: sqlite3.Connection, chat_id: str) -> None:
    conn.execute("DELETE FROM summary_queue WHERE chat_id = ?", (chat_id,))
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
    response = await client.get(api_url, headers={"Accept": "application/vnd.github+json"})
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
            date = to_gmt3(published_at)

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


def to_gmt3(date_text: str) -> str:
    # Convert only explicit UTC timestamps to readable GMT+3.
    date_text = date_text.strip()
    explicit_utc = bool(
        re.search(r"(?:\sUTC$|T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$|[+\-]\d{2}:\d{2}$)", date_text)
    )
    if not explicit_utc:
        return date_text

    try:
        dt = datetime.fromisoformat(date_text.replace(" UTC", "+00:00").replace("Z", "+00:00"))
    except ValueError:
        return date_text

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone(timedelta(hours=3))).strftime("%Y-%m-%d %H:%M:%S GMT+3")


def format_date_with_tz(date_text: str) -> str:
    text = date_text.strip()
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


def format_summary_entry(source: dict[str, Any], entry: ChangelogEntry) -> str:
    product = html.escape(str(source.get("product") or source["id"]))
    version = html.escape(entry.version)
    date = html.escape(format_date_with_tz(entry.date)) if entry.date else "не указана"
    url = html.escape(entry.url, quote=True)
    return (
        f"🔹 <b>{product}</b> · <code>{version}</code>\n"
        f"<b>Дата:</b> {date}\n"
        f"<a href=\"{url}\">Открыть</a>"
    )


def build_aggregate_summary(entries: list[tuple[dict[str, Any], ChangelogEntry]]) -> str:
    lines: list[str] = ["📌 <b>Сводка новых релизов</b>"]
    for source, entry in entries:
        lines.append("")
        lines.append(format_summary_entry(source, entry))
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
                updates = await telegram_api_get_result(client, telegram_token, "getUpdates", params=params)
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

                    source_ids = load_source_ids(config_path)
                    routing = routing_state.get(source_ids)
                    if not is_authorized_admin(routing.admins, (message.get("from") or {}).get("id")):
                        chat_id_raw = message.get("chat", {}).get("id")
                        if chat_id_raw is not None:
                            await send_telegram_message(
                                client,
                                telegram_token,
                                str(int(str(chat_id_raw).strip())),
                                "🚫 Нет доступа. Команда доступна только админам.",
                            )
                        continue

                    reply_chat_id_raw = message.get("chat", {}).get("id")
                    if reply_chat_id_raw is None:
                        continue

                    reply_chat_id = str(int(str(reply_chat_id_raw).strip()))

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

                    if command in {"subscribe", "unsubscribe"}:
                        if not args:
                            await send_telegram_message(
                                client,
                                telegram_token,
                                reply_chat_id,
                                "Использование: /subscribe <source_id> [chat_id|alias] или /unsubscribe <source_id> [chat_id|alias]",
                            )
                            continue

                        source_id = normalize_source_id(args[0])
                        if source_id not in source_ids:
                            await send_telegram_message(
                                client,
                                telegram_token,
                                reply_chat_id,
                                f"Источник {source_id!r} отсутствует в products.yaml",
                            )
                            continue

                        target_chat_token = args[1] if len(args) > 1 else reply_chat_id
                        target_chat_id = resolve_chat_identifier(target_chat_token, routing)
                        if target_chat_id is None:
                            await send_telegram_message(
                                client,
                                telegram_token,
                                reply_chat_id,
                                f"Чат {target_chat_token!r} не найден. Укажи chat_id или alias из routing DB.",
                            )
                            continue

                        if command == "subscribe":
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
                        await send_telegram_message(client, telegram_token, reply_chat_id, result)
                        continue

                    if command in {"start", "help"}:
                        await send_telegram_message(
                            client,
                            telegram_token,
                            reply_chat_id,
                            "Доступные команды: /reload, /subscribe <source_id> [chat_id|alias], /unsubscribe <source_id> [chat_id|alias]",
                        )
                        continue

                    await send_telegram_message(
                        client,
                        telegram_token,
                        reply_chat_id,
                        "Неизвестная команда. Используйте /help.",
                    )

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


async def check_source(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    source: dict[str, Any],
    telegram_token: str,
    telegram_chat_ids: list[str],
    dry_run: bool,
) -> list[ChangelogEntry]:
    source_id = source["id"]
    entries = await parse_source(client, source)
    if not entries:
        LOG.warning("[%s] no entries found", source_id)
        return []

    initialized = is_source_initialized(conn, source_id)

    if not initialized and not source.get("post_on_first_run", False):
        mark_many_posted(conn, source_id, entries)
        mark_source_initialized(conn, source_id)
        LOG.info("[%s] initialized with %d existing entries; nothing posted", source_id, len(entries))
        return []

    new_entries = [entry for entry in entries if not is_posted(conn, source_id, entry.item_id)]

    if not initialized:
        first_run_limit = int(source.get("first_run_limit", 1))
        new_entries = new_entries[:first_run_limit]

    if not new_entries:
        LOG.info("[%s] no new entries", source_id)
        return []

    posted_entries: list[ChangelogEntry] = []

    # Sources normally return newest first. Send oldest first if multiple appeared between polls.
    for entry in reversed(new_entries):
        msg = format_message(source, entry)
        if not telegram_chat_ids:
            LOG.info("[%s] no target chats configured for %s", source_id, entry.item_id)
        for chat_id in telegram_chat_ids:
            if dry_run:
                LOG.info("[%s] DRY RUN would post %s to %s:\n%s", source_id, entry.item_id, chat_id, msg)
            else:
                await send_telegram_message(client, telegram_token, chat_id, msg)
                LOG.info("[%s] posted %s to %s", source_id, entry.item_id, chat_id)
        mark_posted(conn, source_id, entry.item_id)
        posted_entries.append(entry)

    if not initialized:
        mark_many_posted(conn, source_id, entries)
        mark_source_initialized(conn, source_id)

    return posted_entries


async def send_summary(
    client: httpx.AsyncClient,
    telegram_token: str,
    chat_id: str,
    entries: list[tuple[dict[str, Any], ChangelogEntry]],
    dry_run: bool,
) -> bool:
    if not entries:
        return False

    msg = build_aggregate_summary(entries)
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
    dry_run: bool,
) -> None:
    if not entries_by_chat:
        return
    for chat_id, entries in entries_by_chat.items():
        await send_summary(client, telegram_token, chat_id, entries, dry_run)


async def check_all(
    config_path: str,
    db_path: str,
    dry_run: bool = False,
    routing_state: RoutingState | None = None,
    force_routing_reload: bool = False,
) -> None:
    load_dotenv()
    config = load_config(config_path)
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    routing_config_path = os.getenv("ROUTING_CONFIG_PATH", "admin-routing.yaml")
    routing_sources = config["sources"]
    source_ids = collect_source_ids(routing_sources)
    if routing_state is None:
        routing_state = RoutingState(
            db_path=db_path,
            source_config_path=routing_config_path,
            ttl_seconds=0,
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

    summary_items_by_chat: defaultdict[str, list[tuple[dict[str, Any], ChangelogEntry]]] = defaultdict(list)
    now = datetime.now(timezone.utc)
    accessible_chat_ids = {chat_id for chat_id in enabled_chat_ids}

    conn = db_connect(db_path)
    headers = {
        "User-Agent": "changelog-watch-telegram-bot/1.0",
        "Accept": "text/html,text/markdown,text/plain,application/json,*/*",
    }
    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
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

                posted_entries = await check_source(
                    conn,
                    client,
                    source,
                    telegram_token,
                    target_chat_ids,
                    dry_run=dry_run,
                )
                for chat_id in target_chat_ids:
                    chat = routing.chats.get(chat_id)
                    if not chat or not chat.send_summary:
                        continue
                    if chat.summary_schedule.mode == "immediate":
                        summary_items_by_chat[chat_id].extend((source, entry) for entry in posted_entries)
                    elif not dry_run:
                        enqueue_summary_items(conn, chat_id, source["id"], posted_entries)
            except Exception:
                LOG.exception("[%s] failed", source.get("id", "unknown"))

        for chat in routing.chats.values():
            if not chat.enabled or not chat.send_summary:
                continue
            if not dry_run and chat.chat_id not in accessible_chat_ids:
                continue

            queued = load_summary_queue_items(conn, chat.chat_id)
            entries_for_chat: list[tuple[dict[str, Any], ChangelogEntry]] = []

            for source_id, entry in queued:
                source = source_lookup.get(source_id, {"id": source_id})
                entries_for_chat.append((source, entry))

            if chat.summary_schedule.mode == "immediate":
                entries_for_chat.extend(summary_items_by_chat.get(chat.chat_id, []))
                if not entries_for_chat:
                    continue

                if dry_run:
                    await send_summary(client, telegram_token, chat.chat_id, entries_for_chat, dry_run=True)
                else:
                    try:
                        sent = await send_summary(
                            client,
                            telegram_token,
                            chat.chat_id,
                            entries_for_chat,
                            dry_run=False,
                        )
                        if sent:
                            clear_summary_queue(conn, chat.chat_id)
                    except Exception:
                        LOG.exception("failed to send immediate summary to %s", chat.chat_id)
                continue

            if not entries_for_chat:
                continue

            if not is_summary_due(chat.summary_schedule, now, chat.last_summary_sent_at):
                continue

            try:
                sent = await send_summary(client, telegram_token, chat.chat_id, entries_for_chat, dry_run=dry_run)
            except Exception:
                LOG.exception("failed to send summary to %s", chat.chat_id)
                continue

            if sent and not dry_run:
                clear_summary_queue(conn, chat.chat_id)
                mark_chat_summary_sent(conn, chat.chat_id, now)
    conn.close()


async def run_scheduler(config_path: str, db_path: str, dry_run: bool) -> None:
    config = load_config(config_path)
    poll_minutes = int(config.get("poll_minutes", 30))
    poll_seconds = max(1, poll_minutes * 60)
    routing_ttl_seconds = int(os.getenv("ROUTING_RELOAD_TTL_SECONDS", "0") or "0")
    if routing_ttl_seconds < 0:
        raise RuntimeError("ROUTING_RELOAD_TTL_SECONDS must be >= 0")
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")

    routing_state = RoutingState(
        db_path=db_path,
        source_config_path=os.getenv("ROUTING_CONFIG_PATH", "admin-routing.yaml"),
        ttl_seconds=routing_ttl_seconds,
    )

    loop = asyncio.get_running_loop()
    reload_requested = asyncio.Event()

    def _request_routing_reload() -> None:
        if not reload_requested.is_set():
            LOG.info("received hot-reload signal, forcing routing state reload")
            reload_requested.set()

    for sig_name in ("SIGHUP", "SIGUSR1"):
        sig = getattr(signal, sig_name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_routing_reload)
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
                )
            except Exception:
                LOG.exception("cycle failed")

            try:
                await asyncio.wait_for(reload_requested.wait(), timeout=poll_seconds)
            except asyncio.TimeoutError:
                pass
    finally:
        if admin_command_task is not None:
            admin_command_task.cancel()
            try:
                await admin_command_task
            except asyncio.CancelledError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Telegram changelog watcher")
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "products.yaml"))
    parser.add_argument("--db", default=os.getenv("DB_PATH", "data/posted.sqlite3"))
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    parser.add_argument("--dry-run", action="store_true", help="Do not send Telegram messages; log what would be posted")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    setup_logging()
    args = parse_args()

    if args.once:
        asyncio.run(check_all(args.config, args.db, dry_run=args.dry_run))
    else:
        asyncio.run(run_scheduler(args.config, args.db, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
