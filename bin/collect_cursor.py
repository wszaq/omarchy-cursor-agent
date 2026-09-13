#!/usr/bin/env python3
"""Collect Cursor IDE/CLI activity into one Omarchy agents usage record.

Writes ~/.local/state/omarchy/agents/usage/cursor.json for the stock
omarchy.agents panel. Cursor does not expose local token counters or a
rate-limit CLI, so this record carries sessions/prompts/active days and
subscription tier only — limits and token totals stay empty/zero.
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
from collections import defaultdict
from pathlib import Path
from typing import Any

AGENT_ID = "cursor"
AGENT_NAME = "Cursor"
SCHEMA_VERSION = 1
AUTH_HELP = "Run `cursor-agent login` to authenticate Cursor CLI."
USAGE_STATUS = "Quota lives on cursor.com/dashboard/spending (no local limits API)."
SHELL_JSON_GUARD = 64 * 1024 * 1024
ABOUT_TIMEOUT_S = 8
STATUS_TIMEOUT_S = 5

UUID_RE = re.compile(
  r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
  re.IGNORECASE,
)


def home() -> Path:
  return Path(os.path.expanduser("~"))


def cursor_home() -> Path:
  raw = os.environ.get("CURSOR_HOME") or os.environ.get("CURSOR_DIR")
  if raw:
    return Path(os.path.expandvars(os.path.expanduser(raw))).resolve()
  return (home() / ".cursor").resolve()


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
      if node.get("id") == "omarchy.agents":
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
  """Return ready, tierLabel, usageStatusText, authHelpText."""
  binary = resolve_agent_bin()
  if binary is None:
    return False, "", USAGE_STATUS, AUTH_HELP

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
    return True, tier, USAGE_STATUS, ""
  return False, tier, "Not signed in to Cursor CLI.", AUTH_HELP


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
  models: dict[str, dict[str, int]] = {}

  if not projects.is_dir():
    return {
      "prompts_by_day": prompts_by_day,
      "sessions_by_day": sessions_by_day,
      "all_sessions": all_sessions,
      "total_prompts": 0,
      "models": models,
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
          if not isinstance(payload, dict):
            continue
          if payload.get("role") == "user":
            user_prompts += 1
    except OSError as exc:
      log(f"transcript read failed {jsonl}: {exc}")
      continue

    if user_prompts <= 0 and jsonl.stat().st_size <= 0:
      continue

    all_sessions.add(session)
    sessions_by_day[day].add(session)
    if user_prompts > 0:
      prompts_by_day[day] += user_prompts
      total_prompts += user_prompts
    else:
      # Empty/assistant-only files still mark the session as active that day.
      prompts_by_day.setdefault(day, prompts_by_day.get(day, 0))

  return {
    "prompts_by_day": prompts_by_day,
    "sessions_by_day": sessions_by_day,
    "all_sessions": all_sessions,
    "total_prompts": total_prompts,
    "models": models,
  }


def scan_tracking_db(cursor_root: Path) -> dict[str, Any]:
  db_path = cursor_root / "ai-tracking" / "ai-code-tracking.db"
  sessions_by_day: dict[str, set[str]] = defaultdict(set)
  requests_by_day: dict[str, set[str]] = defaultdict(set)
  models_seen: dict[str, int] = defaultdict(int)

  if not db_path.is_file():
    return {
      "sessions_by_day": sessions_by_day,
      "requests_by_day": requests_by_day,
      "models_seen": models_seen,
    }

  try:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
  except sqlite3.Error as exc:
    log(f"tracking db open failed: {exc}")
    return {
      "sessions_by_day": sessions_by_day,
      "requests_by_day": requests_by_day,
      "models_seen": models_seen,
    }

  try:
    cur = con.cursor()
    try:
      rows = cur.execute(
        "SELECT conversationId, requestId, model, timestamp, createdAt "
        "FROM ai_code_hashes"
      ).fetchall()
    except sqlite3.Error as exc:
      log(f"tracking db query failed: {exc}")
      rows = []
    for conversation_id, request_id, model, timestamp, created_at in rows:
      day = timestamp_to_date(timestamp) or timestamp_to_date(created_at)
      if not day:
        continue
      if conversation_id:
        sessions_by_day[day].add(str(conversation_id))
      if request_id:
        requests_by_day[day].add(str(request_id))
      label = str(model or "default").strip() or "default"
      models_seen[label] += 1
  finally:
    con.close()

  return {
    "sessions_by_day": sessions_by_day,
    "requests_by_day": requests_by_day,
    "models_seen": models_seen,
  }


def merge_stats(transcripts: dict[str, Any], tracking: dict[str, Any]) -> dict[str, Any]:
  prompts_by_day: dict[str, int] = defaultdict(int, transcripts["prompts_by_day"])
  sessions_by_day: dict[str, set[str]] = defaultdict(set)
  for day, sessions in transcripts["sessions_by_day"].items():
    sessions_by_day[day] |= set(sessions)
  for day, sessions in tracking["sessions_by_day"].items():
    sessions_by_day[day] |= set(sessions)

  all_sessions: set[str] = set(transcripts["all_sessions"])
  for sessions in sessions_by_day.values():
    all_sessions |= sessions

  # Prefer transcript prompt counts. When a day only appears in the tracking
  # DB, approximate prompts from unique request ids so the panel still lights up.
  for day, requests in tracking["requests_by_day"].items():
    if day not in prompts_by_day or prompts_by_day[day] == 0:
      prompts_by_day[day] = max(prompts_by_day.get(day, 0), len(requests))

  total_prompts = sum(prompts_by_day.values())
  if total_prompts == 0:
    total_prompts = int(transcripts["total_prompts"])

  active_dates = sorted(
    day
    for day, sessions in sessions_by_day.items()
    if sessions or prompts_by_day.get(day, 0) > 0
  )

  model_usage = {
    model: {
      "inputTokens": 0,
      "outputTokens": 0,
      "cacheReadInputTokens": 0,
      "cacheCreationInputTokens": 0,
    }
    for model in sorted(tracking["models_seen"])
  }

  return {
    "prompts_by_day": dict(prompts_by_day),
    "sessions_by_day": {day: set(sessions) for day, sessions in sessions_by_day.items()},
    "all_sessions": all_sessions,
    "total_prompts": total_prompts,
    "active_dates": active_dates,
    "model_usage": model_usage,
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

  recent_days = []
  for day in recent_date_strings(moment.date()):
    recent_days.append(
      {
        "date": day,
        # Honest zero: Cursor does not store local token usage.
        "messageCount": 0,
      }
    )

  today_sessions = sessions_by_day.get(today, set())
  today_prompts = int(prompts_by_day.get(today, 0))

  return {
    "schemaVersion": SCHEMA_VERSION,
    "id": AGENT_ID,
    "name": AGENT_NAME,
    "updatedAt": moment.isoformat(),
    "ready": ready,
    "hasLocalStats": True,
    "hasPromptStats": True,
    "todayPrompts": today_prompts,
    "todaySessions": len(today_sessions),
    "todayTotalTokens": 0,
    "todayTokensByModel": {},
    "recentDays": recent_days,
    "totalPrompts": int(stats["total_prompts"]),
    "totalSessions": len(stats["all_sessions"]),
    "activeDays": len(stats["active_dates"]),
    "activeDates": list(stats["active_dates"]),
    "modelUsage": stats["model_usage"],
    "limits": [],
    "tierLabel": tier_label,
    "usageStatusText": usage_status,
    "authHelpText": auth_help,
  }


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
) -> dict[str, Any] | None:
  del force  # full rescan every run; kept for CLI compatibility
  root = cursor_root or cursor_home()
  if not cursor_present(root):
    log("Cursor not detected; nothing to write")
    return None

  if provider_disabled(AGENT_ID):
    log("cursor disabled in shell.json; skipping")
    return None

  transcripts = scan_transcripts(root)
  tracking = scan_tracking_db(root)
  stats = merge_stats(transcripts, tracking)
  ready, tier, usage_status, auth_help = auth_and_tier()

  if (
    stats["total_prompts"] <= 0
    and len(stats["all_sessions"]) <= 0
    and len(stats["active_dates"]) <= 0
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
