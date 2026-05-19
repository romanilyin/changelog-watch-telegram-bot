#!/usr/bin/env python3
"""Local web UI for AI summary model comparisons."""

from __future__ import annotations

import argparse
import asyncio
import copy
import html
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx
import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import bot  # noqa: E402


DEFAULT_MODELS_CONFIG = "scripts/model-summary-compare.local.yaml"
DEFAULT_MODEL_LISTS_DIR = "data/model-lists"
DEFAULT_MODEL_DECISIONS = "data/model-decisions.yaml"
DEFAULT_JOBS_DIR = "data/model-summary-admin"
HIDDEN_MODEL_ACTIONS = {"skip", "retry_later"}

JOBS: dict[str, dict[str, Any]] = {}
JOBS_LOCK = threading.Lock()


def resolve_path(path_text: str) -> Path:
    path = Path(path_text).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    return path


def read_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a YAML object")
    return data


def load_base_config(path: Path) -> dict[str, Any]:
    data = read_yaml(path)
    env_file = str(data.get("env_file") or "").strip()
    if env_file:
        load_dotenv(resolve_path(str(path.parent / env_file)), override=True)
    return data


def normalize_decision_model_id(provider_name: str, model_id: str) -> str:
    if provider_name == "google" and model_id.startswith("models/"):
        return model_id.split("/", 1)[1]
    return model_id


def model_decision_key(provider_name: str, model_id: str) -> str:
    return f"{provider_name}:{normalize_decision_model_id(provider_name, model_id)}"


def load_model_decisions(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = read_yaml(path)
    raw_models = data.get("models", {})
    if not isinstance(raw_models, dict):
        return {}
    return {str(key): value for key, value in raw_models.items() if isinstance(value, dict)}


def decision_hides_model(decision: dict[str, Any] | None) -> bool:
    action = str((decision or {}).get("action") or "").strip().lower()
    return action in HIDDEN_MODEL_ACTIONS


def read_recent_rows(db_path: Path, limit: int) -> list[dict[str, str]]:
    uri = db_path.resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT source_id, item_id, posted_at AS selected_at
            FROM posted_items
            ORDER BY posted_at DESC
            LIMIT ?
            """,
            (max(1, limit),),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


async def resolve_release_options(config_path: Path, db_path: Path, limit: int) -> list[dict[str, str]]:
    rows = read_recent_rows(db_path, limit)
    if not rows:
        return []

    config = bot.load_config(config_path)
    sources = {str(source["id"]): source for source in config["sources"]}
    rows_by_source: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        rows_by_source.setdefault(row["source_id"], []).append(row)

    entries: dict[tuple[str, str], bot.ChangelogEntry] = {}
    async with httpx.AsyncClient(
        timeout=60,
        headers={"User-Agent": "changelog-watch-telegram-bot/model-admin"},
        follow_redirects=True,
    ) as client:
        for source_id, source_rows in rows_by_source.items():
            source = sources.get(source_id)
            if not source:
                continue
            wanted = {row["item_id"] for row in source_rows}
            try:
                source_entries = await bot.parse_source(client, source)
            except Exception:
                source_entries = []
            for entry in source_entries:
                if entry.item_id in wanted:
                    entries[(source_id, entry.item_id)] = entry

    options: list[dict[str, str]] = []
    for row in rows:
        source_id = row["source_id"]
        item_id = row["item_id"]
        source = sources.get(source_id, {"id": source_id})
        entry = entries.get((source_id, item_id))
        product = str(source.get("product") or source_id)
        if entry:
            body = bot.compact_markdown_for_telegram(entry.body or "")
            description = bot.truncate(body, 700)
            title = entry.title or entry.version
            date = entry.date or ""
        else:
            description = ""
            title = item_id
            date = ""
        options.append(
            {
                "key": f"{source_id}:{item_id}",
                "source_id": source_id,
                "item_id": item_id,
                "product": product,
                "title": title,
                "date": date,
                "selected_at": str(row.get("selected_at") or ""),
                "description": description,
            }
        )
    return options


def parse_model_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line or line.startswith("#") or line.startswith("ERROR "):
        return None
    model_part, _, label = line.partition(" — ")
    flags: list[str] = []
    match = re.search(r"\s+\[([0-9A-Z_ -]+)\]$", model_part)
    if match:
        flags = [flag for flag in match.group(1).split() if flag]
        model_part = model_part[: match.start()].strip()
    if not model_part:
        return None
    return {"id": model_part, "flags": flags, "label": label.strip() or None, "line": line}


def read_model_lists(
    model_lists_dir: Path,
    providers: dict[str, Any],
    decisions: dict[str, dict[str, Any]],
    *,
    include_hidden_models: bool,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    provider_names = sorted(providers.keys() | {path.stem for path in model_lists_dir.glob("*.txt")})
    for provider_name in provider_names:
        txt_path = model_lists_dir / f"{provider_name}.txt"
        models: list[dict[str, Any]] = []
        error = None
        if txt_path.exists():
            for line in txt_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if line.startswith("ERROR "):
                    error = line
                parsed = parse_model_line(line)
                if parsed:
                    decision = decisions.get(model_decision_key(provider_name, str(parsed["id"])))
                    if decision_hides_model(decision) and not include_hidden_models:
                        continue
                    models.append(parsed)
        result.append({"name": provider_name, "models": models, "error": error, "path": str(txt_path)})
    return result


def safe_model_name(provider: str, model_id: str) -> str:
    text = f"{provider}:{model_id}"
    text = re.sub(r"[^0-9A-Za-z_.:/+-]+", "-", text).strip("-")
    return text[:120] or f"model-{uuid.uuid4().hex[:8]}"


def write_job_config(base_config: dict[str, Any], job_dir: Path, models: list[dict[str, str]], concurrent_models: int) -> Path:
    config = copy.deepcopy(base_config)
    config["output"] = str(job_dir / "comparison.md")
    config["concurrent_models"] = max(1, int(concurrent_models))
    config["models"] = [
        {
            "name": safe_model_name(model["provider"], model["id"]),
            "provider": model["provider"],
            "model": model["id"],
        }
        for model in models
    ]
    path = job_dir / "models.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def append_job_log(job: dict[str, Any], line: str) -> None:
    with JOBS_LOCK:
        job["log"].append(line.rstrip())
        job["updated_at"] = time.time()
        if line.startswith("  [done] "):
            job["done"] = min(job["total"], int(job.get("done", 0)) + 1)

    log_path = Path(job["dir"]) / "job.log"
    with log_path.open("a", encoding="utf-8") as f:
        f.write(line)


def save_job_meta(job: dict[str, Any]) -> None:
    meta = {key: value for key, value in job.items() if key != "log"}
    meta["log_tail"] = job.get("log", [])[-200:]
    (Path(job["dir"]) / "job.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def run_job(job: dict[str, Any], python_bin: Path, compare_script: Path) -> None:
    with JOBS_LOCK:
        job["status"] = "running"
        job["started_at"] = time.time()
    save_job_meta(job)

    command = [
        str(python_bin),
        str(compare_script),
        "--models-config",
        str(job["config_path"]),
        "--output",
        str(job["result_path"]),
        "--concurrent-models",
        str(job["concurrent_models"]),
    ]
    for item in job["items"]:
        command.extend(["--item", item])

    append_job_log(job, "+ " + " ".join(command) + "\n")
    try:
        process = subprocess.Popen(
            command,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            append_job_log(job, line)
        return_code = process.wait()
    except Exception as exc:
        append_job_log(job, f"ERROR {type(exc).__name__}: {exc}\n")
        return_code = 1

    with JOBS_LOCK:
        job["return_code"] = return_code
        job["finished_at"] = time.time()
        job["status"] = "done" if return_code == 0 else "failed"
        if return_code == 0:
            job["done"] = job["total"]
    save_job_meta(job)


def html_page() -> str:
    return """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>AI Summary Comparison Admin</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 24px; line-height: 1.35; }
    .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 24px; }
    .card { border: 1px solid #ddd; border-radius: 10px; padding: 16px; }
    label { display: block; margin: 8px 0; }
    small, .muted { color: #666; }
    button { padding: 8px 12px; border-radius: 8px; border: 1px solid #999; cursor: pointer; }
    pre { white-space: pre-wrap; background: #111; color: #eee; padding: 12px; border-radius: 8px; max-height: 360px; overflow: auto; }
    .provider { margin-bottom: 14px; }
    .free { color: #087f23; font-weight: 600; }
    .error { color: #b00020; }
    .release-desc { color: #444; margin-left: 24px; max-width: 800px; }
  </style>
</head>
<body>
  <h1>AI Summary Comparison Admin</h1>
  <p class="muted">Refresh model lists separately with <code>compare-model-summaries.py --refresh-model-lists</code>.</p>
  <div class="grid">
    <section class="card">
      <h2>Releases</h2>
      <div id="releases">Loading...</div>
    </section>
    <section class="card">
      <h2>Models</h2>
      <div id="models">Loading...</div>
    </section>
  </div>
  <section class="card" style="margin-top:24px">
    <label>Concurrent models <input id="concurrent" type="number" min="1" value="1" style="width:80px"></label>
    <button onclick="startJob()">Start comparison</button>
    <button onclick="loadOptions()">Reload options</button>
  </section>
  <section class="card" style="margin-top:24px">
    <h2>Jobs</h2>
    <div id="jobs">No jobs yet.</div>
    <pre id="log"></pre>
    <h3>Result</h3>
    <pre id="result"></pre>
  </section>
<script>
let currentJobId = null;

async function api(path, opts) {
  const res = await fetch(path, opts);
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

async function loadOptions() {
  const data = await api('/api/options?limit=30');
  document.getElementById('releases').innerHTML = data.releases.map((r, idx) => `
    <label><input type="checkbox" class="release" value="${esc(r.key)}" ${idx < 3 ? 'checked' : ''}> <b>${esc(r.product)} ${esc(r.item_id)}</b> <small>${esc(r.date || r.selected_at)}</small></label>
    <div class="release-desc">${esc(r.description).slice(0, 700)}</div>
  `).join('');
  document.getElementById('models').innerHTML = data.providers.map(p => `
    <div class="provider"><h3>${esc(p.name)} <small>${esc(p.path)}</small></h3>
    ${p.error ? `<div class="error">${esc(p.error)}</div>` : ''}
    ${p.models.map(m => `<label><input type="checkbox" class="model" data-provider="${esc(p.name)}" value="${esc(m.id)}"> ${esc(m.id)} ${m.flags.includes('FREE') ? '<span class="free">FREE</span>' : ''} ${m.flags.includes('LOCAL') ? '<span class="free">LOCAL</span>' : ''} <small>${esc(m.label || '')}</small></label>`).join('') || '<small>No cached models. Run refresh command.</small>'}
    </div>
  `).join('');
}

async function startJob() {
  const items = [...document.querySelectorAll('.release:checked')].map(e => e.value);
  const models = [...document.querySelectorAll('.model:checked')].map(e => ({provider: e.dataset.provider, id: e.value}));
  const concurrent_models = Number(document.getElementById('concurrent').value || 1);
  if (!items.length || !models.length) { alert('Select at least one release and one model.'); return; }
  const job = await api('/api/jobs', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({items, models, concurrent_models})});
  currentJobId = job.id;
  await loadJobs();
}

async function loadJobs() {
  const data = await api('/api/jobs');
  document.getElementById('jobs').innerHTML = data.jobs.map(j => `<button onclick="selectJob('${esc(j.id)}')">${esc(j.id)} ${esc(j.status)} ${j.done}/${j.total}</button>`).join(' ');
  if (!currentJobId && data.jobs.length) currentJobId = data.jobs[0].id;
  if (currentJobId) await selectJob(currentJobId);
}

async function selectJob(id) {
  currentJobId = id;
  const job = await api('/api/jobs/' + encodeURIComponent(id));
  document.getElementById('log').textContent = job.log.join('\n');
  document.getElementById('result').textContent = job.result || '';
}

setInterval(loadJobs, 2500);
loadOptions().then(loadJobs).catch(err => { document.body.insertAdjacentHTML('beforeend', '<pre class="error">' + esc(err) + '</pre>'); });
</script>
</body>
</html>
"""


class AdminHandler(BaseHTTPRequestHandler):
    server: "AdminServer"

    def send_json(self, payload: Any, status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> Any:
        length = int(self.headers.get("Content-Length", "0") or "0")
        data = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(data)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/":
                data = html_page().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if parsed.path == "/api/options":
                query = parse_qs(parsed.query)
                limit = int((query.get("limit") or ["30"])[0])
                releases = asyncio.run(resolve_release_options(self.server.products_config, self.server.db_path, limit))
                providers = read_model_lists(
                    self.server.model_lists_dir,
                    self.server.base_config.get("providers", {}),
                    self.server.model_decisions,
                    include_hidden_models=self.server.include_hidden_models,
                )
                self.send_json({"releases": releases, "providers": providers})
                return
            if parsed.path == "/api/jobs":
                with JOBS_LOCK:
                    jobs = sorted(JOBS.values(), key=lambda job: job["created_at"], reverse=True)
                    payload = [
                        {key: job[key] for key in ("id", "status", "created_at", "done", "total")}
                        for job in jobs
                    ]
                self.send_json({"jobs": payload})
                return
            match = re.fullmatch(r"/api/jobs/([^/]+)", parsed.path)
            if match:
                job_id = match.group(1)
                with JOBS_LOCK:
                    job = JOBS.get(job_id)
                    if not job:
                        self.send_json({"error": "job not found"}, status=404)
                        return
                    payload = dict(job)
                result_path = Path(payload["result_path"])
                payload["result"] = result_path.read_text(encoding="utf-8", errors="replace") if result_path.exists() else ""
                self.send_json(payload)
                return
            self.send_json({"error": "not found"}, status=404)
        except Exception as exc:
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path != "/api/jobs":
                self.send_json({"error": "not found"}, status=404)
                return
            payload = self.read_json()
            items = [str(item).strip() for item in payload.get("items", []) if str(item).strip()]
            models = payload.get("models", [])
            if not items or not isinstance(models, list) or not models:
                self.send_json({"error": "items and models are required"}, status=400)
                return
            selected_models = [
                {"provider": str(model.get("provider") or "").strip(), "id": str(model.get("id") or "").strip()}
                for model in models
                if isinstance(model, dict) and str(model.get("provider") or "").strip() and str(model.get("id") or "").strip()
            ]
            if not selected_models:
                self.send_json({"error": "valid models are required"}, status=400)
                return

            concurrent_models = max(1, int(payload.get("concurrent_models") or 1))
            job_id = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:8]
            job_dir = self.server.jobs_dir / job_id
            job_dir.mkdir(parents=True, exist_ok=True)
            config_path = write_job_config(self.server.base_config, job_dir, selected_models, concurrent_models)
            job = {
                "id": job_id,
                "status": "queued",
                "created_at": time.time(),
                "updated_at": time.time(),
                "items": items,
                "models": selected_models,
                "concurrent_models": concurrent_models,
                "dir": str(job_dir),
                "config_path": str(config_path),
                "result_path": str(job_dir / "comparison.md"),
                "done": 0,
                "total": len(items) * len(selected_models),
                "log": [],
            }
            with JOBS_LOCK:
                JOBS[job_id] = job
            save_job_meta(job)
            thread = threading.Thread(target=run_job, args=(job, self.server.python_bin, self.server.compare_script), daemon=True)
            thread.start()
            self.send_json({"id": job_id})
        except Exception as exc:
            self.send_json({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[admin] {self.address_string()} - {format % args}")


class AdminServer(ThreadingHTTPServer):
    def __init__(self, server_address: tuple[str, int], handler_cls: type[AdminHandler], args: argparse.Namespace) -> None:
        super().__init__(server_address, handler_cls)
        self.models_config = resolve_path(args.models_config)
        self.base_config = load_base_config(self.models_config)
        self.products_config = resolve_path(args.config)
        self.db_path = resolve_path(args.db)
        self.model_lists_dir = resolve_path(args.model_lists_dir)
        self.model_decisions = load_model_decisions(resolve_path(args.model_decisions))
        self.include_hidden_models = bool(args.include_hidden_models)
        self.jobs_dir = resolve_path(args.jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self.compare_script = PROJECT_ROOT / "scripts" / "compare-model-summaries.py"
        default_python = PROJECT_ROOT / ".venv" / "bin" / "python"
        self.python_bin = default_python if default_python.exists() else Path(sys.executable)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local web admin for AI summary model comparison")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--models-config", default=DEFAULT_MODELS_CONFIG)
    parser.add_argument("--model-lists-dir", default=DEFAULT_MODEL_LISTS_DIR)
    parser.add_argument("--model-decisions", default=DEFAULT_MODEL_DECISIONS)
    parser.add_argument("--include-hidden-models", action="store_true")
    parser.add_argument("--jobs-dir", default=DEFAULT_JOBS_DIR)
    parser.add_argument("--config", default=os.getenv("CONFIG_PATH", "products.yaml"))
    parser.add_argument("--db", default=os.getenv("DB_PATH", "data/posted.sqlite3"))
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_dotenv(PROJECT_ROOT / ".env")
    server = AdminServer((args.host, args.port), AdminHandler, args)
    print(f"model summary admin: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
