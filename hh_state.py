"""Small, atomic local checkpoint for HH bump cycles.

No browser storage, credentials, or resume text is persisted here.
"""
from __future__ import annotations

import json
import math
import os
import re
import time
import uuid
from pathlib import Path

SCHEMA = 1
RESUME_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def valid_time(value, now):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 < value <= now + 60)


def read_json(path):
    if path.stat().st_size > 1_000_000:
        raise ValueError("oversized state")
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def quarantine(path):
    if not path.exists():
        return None
    target = path.with_name(path.name + ".invalid-" + uuid.uuid4().hex)
    os.replace(path, target)
    return target.name


def empty_state():
    return {"schema": SCHEMA, "last_run": None, "last_cycle": None,
            "recovery_until": None, "account_ids": [], "resumes": {},
            "failure_count": 0, "next_due": None}


def validate_state(data, now):
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise ValueError("unsupported state schema")
    for field in ("last_run", "last_cycle", "recovery_until", "next_due"):
        value = data.get(field)
        if value is not None:
            if field in ("recovery_until", "next_due"):
                valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                         and math.isfinite(value) and 0 < value <= now + 2 * 86400)
            else:
                valid = valid_time(value, now)
            if not valid:
                raise ValueError("invalid " + field)
    ids = data.get("account_ids")
    if not isinstance(ids, list) or len(ids) > 1000 or any(
        not isinstance(key, str) or not RESUME_ID.fullmatch(key) for key in ids
    ) or len(ids) != len(set(ids)):
        raise ValueError("invalid account baseline")
    resumes = data.get("resumes")
    if not isinstance(resumes, dict) or len(resumes) > 1000:
        raise ValueError("invalid resume progress")
    for key, progress in resumes.items():
        if not RESUME_ID.fullmatch(key) or not isinstance(progress, dict):
            raise ValueError("invalid resume key")
        if progress.get("status") not in ("pending", "confirmed", "ambiguous"):
            raise ValueError("invalid resume status")
        if not valid_time(progress.get("at"), now):
            raise ValueError("invalid resume time")
        deadline = progress.get("not_before")
        if (not isinstance(deadline, (int, float)) or isinstance(deadline, bool)
                or not math.isfinite(deadline) or deadline <= 0
                or deadline > now + 2 * 86400):
            raise ValueError("invalid quarantine deadline")
    count = data.get("failure_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 0 or count > 100000:
        raise ValueError("invalid failure count")
    return data


class Store:
    def __init__(self, directory, interval, now=None):
        self.directory = Path(directory)
        self.path = self.directory / "runtime-state.json"
        self.interval = interval
        self.now = time.time() if now is None else now
        self.invalid_files = []
        self.state = self._load()

    def _legacy_time(self, name):
        path = self.directory / name
        if not path.exists():
            return None
        try:
            value = read_json(path)["time"]
            if not valid_time(value, self.now):
                raise ValueError("invalid legacy timestamp")
            return value
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            self.invalid_files.append(quarantine(path))
            return None

    def _legacy_ids(self):
        path = self.directory / "inspection.json"
        if not path.exists():
            return []
        try:
            rows = read_json(path)["resumes"]
            if not isinstance(rows, list):
                raise ValueError("invalid inspection")
            ids = sorted({row["qa"].removeprefix("resume-card-link-")
                          for row in rows if isinstance(row, dict) and
                          isinstance(row.get("qa"), str) and
                          row["qa"].startswith("resume-card-link-") and
                          RESUME_ID.fullmatch(row["qa"].removeprefix("resume-card-link-"))})
            return ids
        except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
            self.invalid_files.append(quarantine(path))
            return []

    def _load(self):
        if self.path.exists():
            try:
                state = validate_state(read_json(self.path), self.now)
                if not state["account_ids"]:
                    prior_ids = self._legacy_ids()
                    if prior_ids:
                        state["account_ids"] = prior_ids
                        atomic_json(self.path, state)
                return state
            except (OSError, TypeError, ValueError, KeyError, json.JSONDecodeError):
                self.invalid_files.append(quarantine(self.path))
                recovered = empty_state()
                recovered["recovery_until"] = self.now + self.interval
                recovered["next_due"] = recovered["recovery_until"]
                recovered["account_ids"] = self._legacy_ids()
                atomic_json(self.path, recovered)
                return recovered
        state = empty_state()
        state["last_run"] = self._legacy_time("last-run.json")
        state["last_cycle"] = self._legacy_time("last-service-cycle.json")
        state["account_ids"] = self._legacy_ids()
        if self.invalid_files:
            state["recovery_until"] = self.now + self.interval
            state["next_due"] = state["recovery_until"]
        atomic_json(self.path, state)
        return state

    def save(self):
        validate_state(self.state, time.time())
        atomic_json(self.path, self.state)

    def due_at(self, now=None):
        now = time.time() if now is None else now
        values = [self.state.get("next_due"), self.state.get("recovery_until")]
        if self.state["last_run"] is not None:
            values.append(self.state["last_run"] + self.interval)
        if self.state["next_due"] is None and self.state["last_cycle"] is not None:
            values.append(self.state["last_cycle"] + self.interval)
        return max((value for value in values if value is not None), default=now)

    def set_baseline(self, keys):
        self.state["account_ids"] = sorted(set(keys))
        self.save()

    def account_matches(self, keys):
        baseline = set(self.state["account_ids"])
        return bool(baseline and baseline.intersection(keys))

    def progress(self, key):
        return self.state["resumes"].get(key)

    def mark_attempt(self, key, now=None):
        now = time.time() if now is None else now
        self.state["resumes"][key] = {"status": "pending", "at": now,
                                      "not_before": now + self.interval}
        self.save()

    def mark_confirmed(self, key, now=None):
        now = time.time() if now is None else now
        self.state["resumes"][key] = {"status": "confirmed", "at": now,
                                      "not_before": now + self.interval}
        self.save()

    def mark_ambiguous(self, key):
        progress = self.state["resumes"].get(key)
        if progress is None:
            raise ValueError("ambiguous action without persisted attempt")
        progress["status"] = "ambiguous"
        self.save()

    def may_send(self, key, now=None):
        now = time.time() if now is None else now
        progress = self.progress(key)
        if progress is None:
            return True
        deadline = progress["not_before"]
        return deadline is not None and now >= deadline

    def cycle_finished(self, *, success, now=None, delay=None):
        now = time.time() if now is None else now
        self.state["last_cycle"] = now
        self.state["failure_count"] = 0 if success else self.state["failure_count"] + 1
        self.state["next_due"] = now + (self.interval if delay is None else delay)
        if success:
            self.state["last_run"] = now
        self.save()
