#!/usr/bin/env python3
"""Collect Cursor IDE/CLI activity into one Omarchy agents usage record.

Writes ~/.local/state/omarchy/agents/usage/cursor.json for the stock
omarchy.agents panel.

Local transcripts only have prompts/sessions. Token totals, per-model
breakdown, and included-usage limits come from Cursor's dashboard API
(authenticated via the IDE access token in state.vscdb).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

AGENT_ID = "cursor"
AGENT_NAME = "Cursor"
SCHEMA_VERSION = 1
AUTH_HELP = "Sign in to the Cursor IDE (or run `cursor-agent login`) so usage can be read."
USAGE_STATUS_FALLBACK = "Open cursor.com/dashboard/spending for live quota."
SHELL_JSON_GUARD = 64 * 1024 * 1024
ABOUT_TIMEOUT_S = 8
STATUS_TIMEOUT_S = 5
API_TIMEOUT_S = 20
DASHBOARD_BASE = "https://api2.cursor.sh/aiserver.v1.DashboardService"
EVENTS_PAGE_SIZE = 100
EVENTS_MAX_PAGES = 30

UUID_RE = re.compile(
  r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
  re.IGNORECASE,
)

ABOUT_LABELS = (
  "CLI Version",
  "Latest",
  "Model",
  "Subscription Tier",
  "OS",
  "Terminal",
  "Shell",
  "User Email",
)


def home() -> Path:
  return Path(os.path.expanduser("~"))


def cursor_home() -> Path:
  raw = os.environ.get("CURSOR_HOME") or os.environ.get("CURSOR_DIR")
  if raw:
    return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
  return (home() / ".cursor").resolve()


def cursor_config_dir() -> Path:
  raw = os.environ.get("CURSOR_CONFIG_DIR")
  if raw:
    return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
  xdg = os.environ.get("XDG_CONFIG_HOME")
  if xdg:
    return Path(xdg) / "Cursor"
  return home() / ".config" / "Cursor"


def state_vscdb_path() -> Path:
  return cursor_config_dir() / "User" / "globalStorage" / "state.vscdb"


def state_home() -> Path:
  return Path(os.environ.get("XDG_STATE_HOME", home() / ".local" / "state"))


def default_usage_dir() -> Path:
  return state_home() / "omarchy" / "agents" / "usage"


def shell_json_path() -> Path:
  return home() / ".config" / "omarchy" / "shell.json"


def log(msg: str) -> None:
  print(f"collect-cursor: {msg}", file=sys.stderr)


def date_string(value: dt.date) -> str:
  return value.strftime("%Y-%m-%d")


def local_today() -> dt.date:
  return dt.datetime.now().date()


def recent_date_strings(today: dt.date | None = None) -> list[str]:
  day = today or local_today()
  return [date_string(day - dt.timedelta(days=offset)) for offset in range(6, -1, -1)]


def timestamp_to_date(value: Any) -> str | None:
  if value is None:
    return None
  try:
    number = float(value)
  except (TypeError, ValueError):
    return None
  if number <= 0:
    return None
  seconds = number / 1000.0 if number > 10_000_000_000 else number
  try:
    return date_string(dt.datetime.fromtimestamp(seconds).date())
  except (OverflowError, OSError, ValueError):
    return None


def ms_to_iso(value: Any) -> str:
  try:
    number = float(value)
  except (TypeError, ValueError):
    return ""
  if number <= 0:
    return ""
  seconds = number / 1000.0 if number > 10_000_000_000 else number
  try:
    return dt.datetime.fromtimestamp(seconds, tz=dt.timezone.utc).isoformat()
  except (OverflowError, OSError, ValueError):
    return ""


def to_int(value: Any) -> int:
  try:
    return int(float(value))
  except (TypeError, ValueError):
    return 0


def provider_disabled(adapter_id: str = AGENT_ID) -> bool:
  path = shell_json_path()
  try:
    if path.stat().st_size > SHELL_JSON_GUARD:
      log(f"shell.json exceeds {SHELL_JSON_GUARD} bytes; ignored")
      return False
    cfg = json.loads(path.read_text())
  except (OSError, json.JSONDecodeError):
    return False

  def scan(node: Any) -> bool:
    if isinstance(node, dict):
      if node.get("id") in ("omarchy.agents", "wszaq.agents"):
        providers = node.get("providers") or {}
        entry = providers.get(adapter_id)
        return isinstance(entry, dict) and entry.get("enabled") is False
      return any(scan(value) for value in node.values())
    if isinstance(node, list):
      return any(scan(value) for value in node)
    return False

  return scan(cfg)


def cursor_present(cursor_root: Path) -> bool:
  return cursor_root.is_dir()


def resolve_agent_bin() -> str | None:
  for name in ("cursor-agent", "agent"):
    found = shutil.which(name)
    if found:
      return found
  return None


def run_text(command: list[str], timeout: float) -> tuple[int, str]:
  try:
    completed = subprocess.run(
      command,
      capture_output=True,
      text=True,
      timeout=timeout,
      check=False,
    )
  except (OSError, subprocess.TimeoutExpired) as exc:
    log(f"{command[0]}: {exc}")
    return 1, ""
  return completed.returncode, (completed.stdout or "") + (completed.stderr or "")


def parse_about(text: str) -> dict[str, str]:
  """Parse `cursor-agent about` columnar or `Key: value` lines."""
  fields: dict[str, str] = {}
  for line in text.splitlines():
    raw = line.strip()
    if not raw:
      continue
    if ":" in raw:
      key, _, value = raw.partition(":")
      key = key.strip().lower()
      value = value.strip()
      if key and value:
        fields[key] = value
      continue
    for label in ABOUT_LABELS:
      if raw.startswith(label):
        value = raw[len(label) :].strip()
        if value:
          fields[label.lower()] = value
        break
  return fields


def auth_and_tier() -> tuple[bool, str, str, str]:
  """Return ready, tierLabel, usageStatusText, authHelpText from the CLI."""
  binary = resolve_agent_bin()
  if binary is None:
    return False, "", USAGE_STATUS_FALLBACK, AUTH_HELP

  status_code, status_text = run_text([binary, "status"], STATUS_TIMEOUT_S)
  logged_in = status_code == 0 and (
    "logged in" in status_text.lower() or "✓" in status_text
  )

  about_code, about_text = run_text([binary, "about"], ABOUT_TIMEOUT_S)
  fields = parse_about(about_text) if about_code == 0 or about_text else {}
  tier = fields.get("subscription tier", "") or fields.get("subscription", "")

  if not logged_in and about_code == 0 and fields.get("user email"):
    logged_in = True

  if logged_in:
    return True, tier, "", ""
  return False, tier, "Not signed in to Cursor CLI.", AUTH_HELP


def read_access_token() -> str | None:
  db = state_vscdb_path()
  if not db.is_file():
    return None
  try:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
  except sqlite3.Error as exc:
    log(f"state.vscdb open failed: {exc}")
    return None
  try:
    row = con.execute(
      "SELECT value FROM ItemTable WHERE key = ?",
      ("cursorAuth/accessToken",),
    ).fetchone()
  except sqlite3.Error as exc:
    log(f"state.vscdb read failed: {exc}")
    return None
  finally:
    con.close()
  if not row or not row[0]:
    return None
  value = row[0]
  if isinstance(value, bytes):
    value = value.decode("utf-8", errors="replace")
  token = str(value).strip()
  return token or None


def dashboard_post(token: str, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
  url = f"{DASHBOARD_BASE}/{method}"
  body = json.dumps(payload or {}).encode("utf-8")
  request = urllib.request.Request(
    url,
    data=body,
    method="POST",
    headers={
      "Authorization": f"Bearer {token}",
      "Content-Type": "application/json",
      "Accept": "application/json",
      "User-Agent": "wszaq.cursor-agent/0.1",
    },
  )
  with urllib.request.urlopen(request, timeout=API_TIMEOUT_S) as response:
    raw = response.read()
  if not raw:
    return {}
  parsed = json.loads(raw)
  if not isinstance(parsed, dict):
    raise ValueError(f"{method} returned non-object JSON")
  return parsed


def empty_token_bucket() -> dict[str, int]:
  return {
    "inputTokens": 0,
    "outputTokens": 0,
    "cacheReadInputTokens": 0,
    "cacheCreationInputTokens": 0,
  }


def add_tokens(bucket: dict[str, int], usage: dict[str, Any] | None) -> int:
  if not isinstance(usage, dict):
    return 0
  inp = to_int(usage.get("inputTokens") or usage.get("input_tokens"))
  out = to_int(usage.get("outputTokens") or usage.get("output_tokens"))
  cread = to_int(
    usage.get("cacheReadTokens")
    or usage.get("cacheReadInputTokens")
    or usage.get("cache_read_tokens")
  )
  cwrite = to_int(
    usage.get("cacheWriteTokens")
    or usage.get("cacheCreationInputTokens")
    or usage.get("cache_creation_tokens")
  )
  bucket["inputTokens"] += inp
  bucket["outputTokens"] += out
  bucket["cacheReadInputTokens"] += cread
  bucket["cacheCreationInputTokens"] += cwrite
  return inp + out + cread + cwrite


def fetch_dashboard_usage(token: str, today: dt.date | None = None) -> dict[str, Any]:
  """Pull tokens, limits, and plan info from Cursor's dashboard API."""
  day = today or local_today()
  window = set(recent_date_strings(day))
  oldest = min(window)

  plan_info: dict[str, Any] = {}
  period: dict[str, Any] = {}
  aggregations: list[dict[str, Any]] = []
  events: list[dict[str, Any]] = []

  try:
    plan_info = dashboard_post(token, "GetPlanInfo")
  except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
    log(f"GetPlanInfo failed: {exc}")

  try:
    period = dashboard_post(token, "GetCurrentPeriodUsage")
  except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
    log(f"GetCurrentPeriodUsage failed: {exc}")

  try:
    agg = dashboard_post(token, "GetAggregatedUsageEvents")
    aggregations = list(agg.get("aggregations") or [])
  except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
    log(f"GetAggregatedUsageEvents failed: {exc}")

  try:
    for page in range(1, EVENTS_MAX_PAGES + 1):
      payload = dashboard_post(
        token,
        "GetFilteredUsageEvents",
        {"page": page, "pageSize": EVENTS_PAGE_SIZE},
      )
      batch = list(payload.get("usageEventsDisplay") or [])
      if not batch:
        break
      events.extend(batch)
      oldest_in_page = None
      for event in batch:
        event_day = timestamp_to_date(event.get("timestamp"))
        if event_day and (oldest_in_page is None or event_day < oldest_in_page):
          oldest_in_page = event_day
      if oldest_in_page is not None and oldest_in_page < oldest:
        break
      if len(batch) < EVENTS_PAGE_SIZE:
        break
  except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
    log(f"GetFilteredUsageEvents failed: {exc}")

  tokens_by_day: dict[str, int] = defaultdict(int)
  prompts_by_day: dict[str, int] = defaultdict(int)
  sessions_by_day: dict[str, set[str]] = defaultdict(set)
  today_tokens_by_model: dict[str, int] = defaultdict(int)
  today_str = date_string(day)

  for event in events:
    event_day = timestamp_to_date(event.get("timestamp"))
    if not event_day or event_day not in window:
      continue
    usage = event.get("tokenUsage") if isinstance(event.get("tokenUsage"), dict) else {}
    total = (
      to_int(usage.get("inputTokens"))
      + to_int(usage.get("outputTokens"))
      + to_int(usage.get("cacheReadTokens"))
      + to_int(usage.get("cacheWriteTokens"))
    )
    tokens_by_day[event_day] += total
    prompts_by_day[event_day] += 1
    conversation = event.get("conversationId")
    if conversation and str(conversation) not in ("", "null"):
      sessions_by_day[event_day].add(str(conversation))
    if event_day == today_str:
      model = str(event.get("model") or "default").strip() or "default"
      today_tokens_by_model[model] += total

  model_usage: dict[str, dict[str, int]] = {}
  for entry in aggregations:
    model = str(entry.get("modelIntent") or "default").strip() or "default"
    bucket = empty_token_bucket()
    add_tokens(
      bucket,
      {
        "inputTokens": entry.get("inputTokens"),
        "outputTokens": entry.get("outputTokens"),
        "cacheReadTokens": entry.get("cacheReadTokens"),
        "cacheWriteTokens": entry.get("cacheWriteTokens"),
      },
    )
    model_usage[model] = bucket

  plan = period.get("planUsage") if isinstance(period.get("planUsage"), dict) else {}
  resets_at = ms_to_iso(period.get("billingCycleEnd"))
  limits: list[dict[str, Any]] = []
  total_pct = float(plan.get("totalPercentUsed") or 0.0) / 100.0
  auto_pct = float(plan.get("autoPercentUsed") or 0.0) / 100.0
  api_pct = float(plan.get("apiPercentUsed") or 0.0) / 100.0
  if plan:
    limits.append(
      {
        "label": "Included usage",
        "percent": max(0.0, total_pct),
        "resetsAt": resets_at,
      }
    )
    limits.append(
      {
        "label": "Auto models",
        "percent": max(0.0, auto_pct),
        "resetsAt": resets_at,
      }
    )
    if api_pct > 0:
      limits.append(
        {
          "label": "API models",
          "percent": max(0.0, api_pct),
          "resetsAt": resets_at,
        }
      )

  balance = None
  included = to_int(plan.get("includedSpend") or plan.get("limit"))
  spent = to_int(plan.get("totalSpend"))
  if included > 0 or spent > 0:
    funded = included / 100.0
    spent_dollars = spent / 100.0
    balance = {
      "remaining": max(0.0, funded - spent_dollars),
      "funded": funded,
      "spent": spent_dollars,
      "currency": "USD",
      "estimated": False,
    }

  info = plan_info.get("planInfo") if isinstance(plan_info.get("planInfo"), dict) else {}
  tier = str(info.get("planName") or "").strip()
  status = ""
  if period.get("enabled") and period.get("displayMessage"):
    status = str(period.get("displayMessage"))
  elif period.get("autoModelSelectedDisplayMessage"):
    status = str(period.get("autoModelSelectedDisplayMessage"))

  return {
    "tier": tier,
    "usage_status": status,
    "limits": limits,
    "balance": balance,
    "model_usage": model_usage,
    "tokens_by_day": dict(tokens_by_day),
    "prompts_by_day": dict(prompts_by_day),
    "sessions_by_day": {d: set(s) for d, s in sessions_by_day.items()},
    "today_tokens_by_model": dict(today_tokens_by_model),
    "ok": bool(aggregations or events or limits or tier),
  }


def conversation_id_for(path: Path) -> str | None:
  parts = path.parts
  try:
    idx = parts.index("agent-transcripts")
  except ValueError:
    return None
  after = parts[idx + 1 :]
  if not after:
    return None
  candidate = after[0]
  return candidate if UUID_RE.match(candidate) else None


def scan_transcripts(cursor_root: Path) -> dict[str, Any]:
  projects = cursor_root / "projects"
  prompts_by_day: dict[str, int] = defaultdict(int)
  sessions_by_day: dict[str, set[str]] = defaultdict(set)
  all_sessions: set[str] = set()
  total_prompts = 0

  if not projects.is_dir():
    return {
      "prompts_by_day": prompts_by_day,
      "sessions_by_day": sessions_by_day,
      "all_sessions": all_sessions,
      "total_prompts": 0,
    }

  for jsonl in projects.glob("*/agent-transcripts/**/*.jsonl"):
    if not jsonl.is_file():
      continue
    session = conversation_id_for(jsonl)
    if not session:
      continue
    try:
      day = date_string(dt.datetime.fromtimestamp(jsonl.stat().st_mtime).date())
    except OSError:
      day = date_string(local_today())

    user_prompts = 0
    try:
      with jsonl.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
          line = line.strip()
          if not line:
            continue
          try:
            payload = json.loads(line)
          except json.JSONDecodeError:
            continue
          if isinstance(payload, dict) and payload.get("role") == "user":
            user_prompts += 1
    except OSError as exc:
      log(f"transcript read failed {jsonl}: {exc}")
      continue

    try:
      empty = jsonl.stat().st_size <= 0
    except OSError:
      empty = True
    if user_prompts <= 0 and empty:
      continue

    all_sessions.add(session)
    sessions_by_day[day].add(session)
    if user_prompts > 0:
      prompts_by_day[day] += user_prompts
      total_prompts += user_prompts

  return {
    "prompts_by_day": prompts_by_day,
    "sessions_by_day": sessions_by_day,
    "all_sessions": all_sessions,
    "total_prompts": total_prompts,
  }


def scan_tracking_db(cursor_root: Path) -> dict[str, Any]:
  db_path = cursor_root / "ai-tracking" / "ai-code-tracking.db"
  sessions_by_day: dict[str, set[str]] = defaultdict(set)
  requests_by_day: dict[str, set[str]] = defaultdict(set)

  if not db_path.is_file():
    return {"sessions_by_day": sessions_by_day, "requests_by_day": requests_by_day}

  try:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  except sqlite3.Error as exc:
    log(f"tracking db open failed: {exc}")
    return {"sessions_by_day": sessions_by_day, "requests_by_day": requests_by_day}

  try:
    try:
      rows = con.execute(
        "SELECT conversationId, requestId, timestamp, createdAt FROM ai_code_hashes"
      ).fetchall()
    except sqlite3.Error as exc:
      log(f"tracking db query failed: {exc}")
      rows = []
    for conversation_id, request_id, timestamp, created_at in rows:
      day = timestamp_to_date(timestamp) or timestamp_to_date(created_at)
      if not day:
        continue
      if conversation_id:
        sessions_by_day[day].add(str(conversation_id))
      if request_id:
        requests_by_day[day].add(str(request_id))
  finally:
    con.close()

  return {"sessions_by_day": sessions_by_day, "requests_by_day": requests_by_day}


def merge_stats(
  transcripts: dict[str, Any],
  tracking: dict[str, Any],
  dashboard: dict[str, Any] | None = None,
) -> dict[str, Any]:
  prompts_by_day: dict[str, int] = defaultdict(int, transcripts["prompts_by_day"])
  sessions_by_day: dict[str, set[str]] = defaultdict(set)
  tokens_by_day: dict[str, int] = defaultdict(int)

  for day, sessions in transcripts["sessions_by_day"].items():
    sessions_by_day[day] |= set(sessions)
  for day, sessions in tracking["sessions_by_day"].items():
    sessions_by_day[day] |= set(sessions)

  for day, requests in tracking["requests_by_day"].items():
    if day not in prompts_by_day or prompts_by_day[day] == 0:
      prompts_by_day[day] = max(prompts_by_day.get(day, 0), len(requests))

  model_usage: dict[str, dict[str, int]] = {}
  today_tokens_by_model: dict[str, int] = {}
  limits: list[dict[str, Any]] = []
  balance = None

  if dashboard:
    for day, value in (dashboard.get("tokens_by_day") or {}).items():
      tokens_by_day[day] += to_int(value)
    for day, value in (dashboard.get("prompts_by_day") or {}).items():
      prompts_by_day[day] = max(prompts_by_day.get(day, 0), to_int(value))
    for day, sessions in (dashboard.get("sessions_by_day") or {}).items():
      sessions_by_day[day] |= set(sessions)
    model_usage = {
      model: dict(bucket)
      for model, bucket in (dashboard.get("model_usage") or {}).items()
    }
    today_tokens_by_model = {
      model: to_int(total)
      for model, total in (dashboard.get("today_tokens_by_model") or {}).items()
    }
    limits = list(dashboard.get("limits") or [])
    balance = dashboard.get("balance")

  all_sessions: set[str] = set(transcripts["all_sessions"])
  for sessions in sessions_by_day.values():
    all_sessions |= sessions

  active_dates = sorted(
    {
      *tokens_by_day.keys(),
      *[day for day, sessions in sessions_by_day.items() if sessions],
      *[day for day, count in prompts_by_day.items() if count > 0],
    }
  )

  return {
    "prompts_by_day": dict(prompts_by_day),
    "sessions_by_day": {day: set(sessions) for day, sessions in sessions_by_day.items()},
    "tokens_by_day": dict(tokens_by_day),
    "all_sessions": all_sessions,
    "total_prompts": int(sum(prompts_by_day.values()) or transcripts["total_prompts"]),
    "active_dates": active_dates,
    "model_usage": model_usage,
    "today_tokens_by_model": today_tokens_by_model,
    "limits": limits,
    "balance": balance,
  }


def build_record(
  stats: dict[str, Any],
  ready: bool,
  tier_label: str,
  usage_status: str,
  auth_help: str,
  now: dt.datetime | None = None,
) -> dict[str, Any]:
  moment = now or dt.datetime.now().astimezone()
  today = date_string(moment.date())
  prompts_by_day = stats["prompts_by_day"]
  sessions_by_day = stats["sessions_by_day"]
  tokens_by_day = stats.get("tokens_by_day") or {}

  recent_days = []
  for day in recent_date_strings(moment.date()):
    recent_days.append(
      {
        "date": day,
        "messageCount": int(tokens_by_day.get(day, 0)),
      }
    )

  today_sessions = sessions_by_day.get(today, set())
  today_prompts = int(prompts_by_day.get(today, 0))
  today_total = int(tokens_by_day.get(today, 0))
  today_by_model = {
    model: int(total)
    for model, total in (stats.get("today_tokens_by_model") or {}).items()
    if int(total) > 0
  }

  record: dict[str, Any] = {
    "schemaVersion": SCHEMA_VERSION,
    "id": AGENT_ID,
    "name": AGENT_NAME,
    "updatedAt": moment.isoformat(),
    "ready": ready,
    "hasLocalStats": True,
    "hasPromptStats": True,
    "todayPrompts": today_prompts,
    "todaySessions": len(today_sessions),
    "todayTotalTokens": today_total,
    "todayTokensByModel": today_by_model,
    "recentDays": recent_days,
    "totalPrompts": int(stats["total_prompts"]),
    "totalSessions": len(stats["all_sessions"]),
    "activeDays": len(stats["active_dates"]),
    "activeDates": list(stats["active_dates"]),
    "modelUsage": stats.get("model_usage") or {},
    "limits": list(stats.get("limits") or []),
    "tierLabel": tier_label,
    "usageStatusText": usage_status,
    "authHelpText": auth_help,
  }
  if stats.get("balance"):
    record["balance"] = stats["balance"]
  return record


def write_record(record: dict[str, Any], usage_dir: Path) -> Path:
  usage_dir.mkdir(parents=True, exist_ok=True)
  target = usage_dir / f"{record['id']}.json"
  fd, tmp = tempfile.mkstemp(dir=usage_dir, prefix=f".{record['id']}.", suffix=".tmp")
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      json.dump(record, handle, indent=2)
      handle.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)
  except BaseException:
    try:
      os.unlink(tmp)
    except OSError:
      pass
    raise
  return target


def collect(
  cursor_root: Path | None = None,
  usage_dir: Path | None = None,
  *,
  write: bool = True,
  force: bool = False,
  access_token: str | None = None,
) -> dict[str, Any] | None:
  del force  # full rescan every run; kept for CLI compatibility
  root = cursor_root or cursor_home()
  # Explicit override/test roots must exist locally; do not silently use the
  # machine-wide IDE DB unless this is the default home path.
  if cursor_root is not None and not root.is_dir():
    log("Cursor not detected; nothing to write")
    return None
  if cursor_root is None and not root.is_dir() and not state_vscdb_path().is_file():
    log("Cursor not detected; nothing to write")
    return None

  if provider_disabled(AGENT_ID):
    log("cursor disabled in shell.json; skipping")
    return None

  transcripts = scan_transcripts(root) if root.is_dir() else {
    "prompts_by_day": {},
    "sessions_by_day": {},
    "all_sessions": set(),
    "total_prompts": 0,
  }
  tracking = scan_tracking_db(root) if root.is_dir() else {
    "sessions_by_day": {},
    "requests_by_day": {},
  }

  ready, tier, usage_status, auth_help = auth_and_tier()
  token = access_token if access_token is not None else read_access_token()
  dashboard: dict[str, Any] | None = None
  if token:
    dashboard = fetch_dashboard_usage(token)
    if dashboard.get("ok"):
      ready = True
      if dashboard.get("tier"):
        tier = str(dashboard["tier"])
      if dashboard.get("usage_status"):
        usage_status = str(dashboard["usage_status"])
      auth_help = ""
    else:
      log("dashboard API returned no usage payload")
      if not usage_status:
        usage_status = USAGE_STATUS_FALLBACK
  else:
    log("no Cursor IDE access token in state.vscdb")
    if ready and not usage_status:
      usage_status = USAGE_STATUS_FALLBACK
    if not ready and not auth_help:
      auth_help = AUTH_HELP

  stats = merge_stats(transcripts, tracking, dashboard)
  if (
    stats["total_prompts"] <= 0
    and len(stats["all_sessions"]) <= 0
    and len(stats["active_dates"]) <= 0
    and not stats.get("limits")
    and not stats.get("model_usage")
  ):
    log("no Cursor activity found")
    return None

  record = build_record(stats, ready, tier, usage_status, auth_help)
  if write:
    path = write_record(record, usage_dir or default_usage_dir())
    log(f"wrote {path}")
  return record


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--force", action="store_true", help="Accepted for compatibility; always rescans")
  parser.add_argument("--print", action="store_true", dest="print_json", help="Print the record JSON to stdout")
  parser.add_argument("--no-write", action="store_true", help="Do not write the usage file")
  parser.add_argument("--usage-dir", type=Path, default=None, help="Override usage directory")
  parser.add_argument("--cursor-home", type=Path, default=None, help="Override ~/.cursor")
  args = parser.parse_args(argv)

  record = collect(
    cursor_root=args.cursor_home,
    usage_dir=args.usage_dir,
    write=not args.no_write,
    force=args.force,
  )
  if record is None:
    return 0
  if args.print_json or args.no_write:
    json.dump(record, sys.stdout, indent=2)
    sys.stdout.write("\n")
  return 0


if __name__ == "__main__":
  sys.exit(main())
