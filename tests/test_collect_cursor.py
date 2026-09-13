#!/usr/bin/env python3
"""Fixture-based tests for collect-cursor."""

from __future__ import annotations

import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import collect_cursor as cc  # noqa: E402  # type: ignore


def write_jsonl(path: Path, rows: list[dict]) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


class ParseAboutTests(unittest.TestCase):
  def test_columnar_subscription_tier(self) -> None:
    text = (
      "About Cursor CLI\n"
      "CLI Version         2026.09.10-fd3934a\n"
      "Subscription Tier   Pro\n"
      "User Email          someone@example.com\n"
    )
    fields = cc.parse_about(text)
    self.assertEqual(fields.get("subscription tier"), "Pro")
    self.assertEqual(fields.get("user email"), "someone@example.com")

  def test_colon_form(self) -> None:
    fields = cc.parse_about("Subscription Tier: Ultra\n")
    self.assertEqual(fields.get("subscription tier"), "Ultra")


class AggregationTests(unittest.TestCase):
  def setUp(self) -> None:
    self.tmp = tempfile.TemporaryDirectory()
    self.root = Path(self.tmp.name)
    self.usage = self.root / "usage"
    self.cursor = self.root / ".cursor"
    self.addCleanup(self.tmp.cleanup)

  def _add_transcript(self, conversation: str, prompts: int, day_offset: int = 0) -> Path:
    project = self.cursor / "projects" / "demo" / "agent-transcripts" / conversation
    path = project / f"{conversation}.jsonl"
    rows = [{"role": "user", "message": {"content": [{"type": "text", "text": f"p{i}"}]}} for i in range(prompts)]
    rows.append({"role": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}})
    write_jsonl(path, rows)
    when = datetime.now() - timedelta(days=day_offset)
    ts = when.timestamp()
    import os

    os.utime(path, (ts, ts))
    return path

  def _add_tracking(self, conversation: str, model: str = "composer-2") -> None:
    db_dir = self.cursor / "ai-tracking"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = db_dir / "ai-code-tracking.db"
    con = sqlite3.connect(db)
    con.execute(
      """
      CREATE TABLE ai_code_hashes (
        hash TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        fileExtension TEXT,
        fileName TEXT,
        requestId TEXT,
        conversationId TEXT,
        timestamp INTEGER,
        model TEXT,
        createdAt INTEGER NOT NULL
      )
      """
    )
    now_ms = int(datetime.now().timestamp() * 1000)
    con.execute(
      "INSERT INTO ai_code_hashes VALUES (?,?,?,?,?,?,?,?,?)",
      ("h1", "composer", "py", "a.py", "req-1", conversation, now_ms, model, now_ms),
    )
    con.commit()
    con.close()

  def test_collect_builds_stock_record(self) -> None:
    cid = "11111111-1111-1111-1111-111111111111"
    self._add_transcript(cid, prompts=3)
    self._add_tracking(cid, model="composer-2")

    record = cc.collect(
      cursor_root=self.cursor,
      usage_dir=self.usage,
      write=True,
    )
    self.assertIsNotNone(record)
    assert record is not None
    self.assertEqual(record["id"], "cursor")
    self.assertEqual(record["name"], "Cursor")
    self.assertEqual(record["schemaVersion"], 1)
    self.assertEqual(record["totalPrompts"], 3)
    self.assertEqual(record["totalSessions"], 1)
    self.assertGreaterEqual(record["todayPrompts"], 3)
    self.assertEqual(record["todayTotalTokens"], 0)
    self.assertEqual(record["limits"], [])
    self.assertEqual(len(record["recentDays"]), 7)
    self.assertTrue((self.usage / "cursor.json").is_file())
    self.assertIn("composer-2", record["modelUsage"])

  def test_provider_disabled_skips_write(self) -> None:
    cid = "22222222-2222-2222-2222-222222222222"
    self._add_transcript(cid, prompts=1)
    shell = self.root / "shell.json"
    shell.write_text(
      json.dumps(
        {
          "bar": {
            "layout": {
              "right": [{"id": "omarchy.agents", "providers": {"cursor": {"enabled": False}}}]
            }
          }
        }
      ),
      encoding="utf-8",
    )
    original = cc.shell_json_path
    cc.shell_json_path = lambda: shell  # type: ignore[assignment]
    self.addCleanup(lambda: setattr(cc, "shell_json_path", original))

    record = cc.collect(cursor_root=self.cursor, usage_dir=self.usage, write=True)
    self.assertIsNone(record)
    self.assertFalse((self.usage / "cursor.json").exists())

  def test_no_cursor_home_returns_none(self) -> None:
    missing = self.root / "missing-cursor"
    record = cc.collect(cursor_root=missing, usage_dir=self.usage, write=True)
    self.assertIsNone(record)


class BuildRecordTests(unittest.TestCase):
  def test_recent_days_are_zero_tokens(self) -> None:
    stats = {
      "prompts_by_day": {"2026-09-13": 2},
      "sessions_by_day": {"2026-09-13": {"abc"}},
      "all_sessions": {"abc"},
      "total_prompts": 2,
      "active_dates": ["2026-09-13"],
      "model_usage": {},
    }
    record = cc.build_record(
      stats,
      ready=True,
      tier_label="Pro",
      usage_status="status",
      auth_help="",
      now=datetime(2026, 9, 13, 12, 0, 0).astimezone(),
    )
    self.assertEqual(record["tierLabel"], "Pro")
    self.assertEqual(record["todayPrompts"], 2)
    self.assertTrue(all(day["messageCount"] == 0 for day in record["recentDays"]))


if __name__ == "__main__":
  unittest.main()
