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
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import yaml
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from bs4 import BeautifulSoup
from dotenv import load_dotenv

LOG = logging.getLogger("changelog-watch-bot")

DEFAULT_VERSION_RE = r"^v?\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?$"
MD_VERSION_HEADING_RE = re.compile(
    r"^##\s+\[?(?P<version>[^\]\n]+)\]?\s*(?:-\s*(?P<date>\d{4}-\d{2}-\d{2}))?\s*$",
    re.MULTILINE,
)


@dataclass(frozen=True)
class ChangelogEntry:
    item_id: str
    title: str
    version: str
    date: str | None
    body: str
    url: str
    is_prerelease: bool = False


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def load_config(path: str | Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if "sources" not in data or not isinstance(data["sources"], list):
        raise ValueError("products.yaml must contain a top-level 'sources' list")
    return data


def db_connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
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
        CREATE TABLE IF NOT EXISTS source_state (
            source_id TEXT PRIMARY KEY,
            initialized INTEGER NOT NULL DEFAULT 0,
            initialized_at TEXT
        )
        """
    )
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
    # Keep release notes readable, but avoid huge Markdown artifacts.
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)
    text = re.sub(r"\n{3,}", "\n\n", text)
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


def parse_chat_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [chat_id.strip() for chat_id in value.split(",") if chat_id.strip()]


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


async def check_source(
    conn: sqlite3.Connection,
    client: httpx.AsyncClient,
    source: dict[str, Any],
    telegram_token: str,
    telegram_chat_id: str,
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
        if dry_run:
            LOG.info("[%s] DRY RUN would post %s:\n%s", source_id, entry.item_id, msg)
        else:
            await send_telegram_message(client, telegram_token, telegram_chat_id, msg)
            LOG.info("[%s] posted %s", source_id, entry.item_id)
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
) -> None:
    if not entries:
        return

    msg = build_aggregate_summary(entries)
    if dry_run:
        LOG.info("[summary] DRY RUN would post aggregate:")
        LOG.info("%s", msg)
        return

    await send_telegram_message(client, telegram_token, chat_id, msg)


async def send_summaries(
    client: httpx.AsyncClient,
    telegram_token: str,
    chat_ids: list[str],
    entries: list[tuple[dict[str, Any], ChangelogEntry]],
    dry_run: bool,
) -> None:
    if not entries:
        return
    for chat_id in chat_ids:
        await send_summary(client, telegram_token, chat_id, entries, dry_run)


async def check_all(config_path: str, db_path: str, dry_run: bool = False) -> None:
    load_dotenv()
    config = load_config(config_path)
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_ids = parse_chat_ids(os.getenv("TELEGRAM_CHAT_ID", ""))
    summary_chat_ids = parse_chat_ids(os.getenv("SUMMARY_CHAT_IDS", ""))

    if not summary_chat_ids:
        summary_chat_ids = telegram_chat_ids

    if not dry_run and (not telegram_token or not telegram_chat_ids):
        raise RuntimeError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in .env")

    # Keep backwards compatibility: one list for all full messages as before.
    main_chat_id = telegram_chat_ids[0] if telegram_chat_ids else ""

    summary_items: list[tuple[dict[str, Any], ChangelogEntry]] = []

    conn = db_connect(db_path)
    headers = {
        "User-Agent": "changelog-watch-telegram-bot/1.0",
        "Accept": "text/html,text/markdown,text/plain,application/json,*/*",
    }
    async with httpx.AsyncClient(timeout=30, headers=headers, follow_redirects=True) as client:
        for source in config["sources"]:
            if source.get("enabled", True) is False:
                LOG.info("[%s] disabled; skipping", source.get("id", "unknown"))
                continue
            try:
                posted_entries = await check_source(
                    conn,
                    client,
                    source,
                    telegram_token,
                    main_chat_id,
                    dry_run=dry_run,
                )
                summary_items.extend((source, entry) for entry in posted_entries)
            except Exception:
                LOG.exception("[%s] failed", source.get("id", "unknown"))

        await send_summaries(client, telegram_token, summary_chat_ids, summary_items, dry_run=dry_run)
    conn.close()


async def run_scheduler(config_path: str, db_path: str, dry_run: bool) -> None:
    config = load_config(config_path)
    poll_minutes = int(config.get("poll_minutes", 30))

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(
        check_all,
        trigger="interval",
        minutes=poll_minutes,
        args=[config_path, db_path, dry_run],
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    LOG.info("scheduler started; interval=%s minutes", poll_minutes)
    await check_all(config_path, db_path, dry_run=dry_run)

    stop_event = asyncio.Event()
    await stop_event.wait()


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
