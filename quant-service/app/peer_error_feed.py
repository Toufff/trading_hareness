"""Give an external database role its own errors back.

PostgreSQL hands every failure to the client that caused it, so in principle a
consumer always knows when its own SQL is wrong.  In practice a consumer that
catches the exception without logging it goes on failing silently: on
2026-09-19 the peer deployment raised the same ``syntax error`` 5123 times in
the owner cluster's log while its own container log contained nothing but INFO
access lines.  Nothing in that loop was visible from the side that could fix
it, and nothing on the owner side was visible to it at all -- the frequency,
and the fact that it lands in someone else's log, are knowable only here.

This module turns the owner log files into that missing feedback channel.  It
parses the line prefix written by ``postgres-managed-config.psm1``::

    %m [%p] %q%u@%d app=%a %e

``%q`` suppresses the session fields for background workers, so lines without a
role simply carry no attribution and are skipped: a caller can only ever be
shown entries belonging to the role it asked for, and the router restricts that
to an allowlist.  Grouping is by SQLSTATE plus the statement, because a feed is
only actionable if "this happened 5123 times" arrives as one row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

# The cluster logs in Asia/Shanghai (log_timezone in the managed config) and
# PostgreSQL writes the abbreviation, not the offset, so the reader supplies
# the offset rather than trying to resolve "CST" (which is ambiguous globally).
LOG_TIMEZONE = timezone(timedelta(hours=8), "CST")

LOG_FILENAME_PATTERN = "postgresql-*.log"

#: Levels that open a new entry.  ``WARNING`` is included because a warning is
#: still something the producer should see; ``LOG`` is not, because routine
#: server chatter is not the consumer's business.
PROBLEM_LEVELS = frozenset({"ERROR", "FATAL", "PANIC", "WARNING"})

#: Levels PostgreSQL emits as separate lines that belong to the entry above
#: them.  ``STATEMENT`` is the one that matters: it carries the failing SQL.
ATTACHMENT_LEVELS = frozenset({"DETAIL", "HINT", "CONTEXT", "STATEMENT", "QUERY", "LOCATION"})

_LINE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3}) \S+ "
    r"\[(?P<pid>\d+)\] "
    r"(?:(?P<user>[^@\s]+)@(?P<db>\S+) app=(?P<app>.*?) (?P<sqlstate>[0-9A-Z]{5}) )?"
    r"(?P<level>[A-Z][A-Z0-9_]*):\s{1,2}(?P<body>.*)$"
)

_FILENAME_DATE = re.compile(r"^postgresql-(\d{4})-(\d{2})-(\d{2})\.log$")

#: Numbers inside a message are almost always positional (a character offset, a
#: pid, a row count), so two entries that differ only there are one problem.
#: The raw text of the first occurrence is still reported as ``message``.
_DIGITS = re.compile(r"\d+")

#: A single request never reads more than this many bytes of log.  The feed is
#: meant to be polled; a consumer asking for a month of history gets a bounded
#: answer plus ``truncated: true`` rather than stalling the service.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

#: How many distinct failing statements one group carries before it stops
#: collecting them.  A group is a diagnosis, not a transcript.
MAX_STATEMENTS_PER_GROUP = 3


@dataclass
class LogEntry:
    """One problem line plus the DETAIL/STATEMENT lines PostgreSQL attached."""

    occurred_at: datetime
    pid: int
    role: str
    database: str
    application: str
    sqlstate: str
    level: str
    message: str
    attachments: dict[str, str] = field(default_factory=dict)
    _open_key: str | None = None

    @property
    def statement(self) -> str | None:
        return self.attachments.get("STATEMENT")


def _parse_timestamp(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=LOG_TIMEZONE)


def iter_entries(lines: Iterable[str]) -> Iterator[LogEntry]:
    """Yield attributed problem entries from raw log lines.

    Lines with no role (background workers, and anything logged before the
    prefix change) are dropped rather than guessed at: an entry that cannot be
    attributed must not be handed to a consumer as if it were theirs.
    """
    current: LogEntry | None = None
    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line:
            continue
        match = _LINE.match(line)
        if match is None:
            # A continuation line of a multi-line message or statement.  It is
            # indented by PostgreSQL and carries no prefix of its own.
            if current is not None and current._open_key is not None:
                if current._open_key == "MESSAGE":
                    current.message = f"{current.message}\n{line.strip()}"
                else:
                    current.attachments[current._open_key] += "\n" + line.strip()
            continue

        level = match.group("level")
        role = match.group("user")
        pid = int(match.group("pid"))

        if level in ATTACHMENT_LEVELS:
            if current is not None and current.pid == pid:
                current.attachments[level] = match.group("body")
                current._open_key = level
            continue

        if current is not None:
            yield current
            current = None

        if level not in PROBLEM_LEVELS or role is None:
            continue

        current = LogEntry(
            occurred_at=_parse_timestamp(match.group("ts")),
            pid=pid,
            role=role,
            database=match.group("db"),
            application=match.group("app") or "",
            sqlstate=match.group("sqlstate") or "",
            level=level,
            message=match.group("body"),
            _open_key="MESSAGE",
        )

    if current is not None:
        yield current


def _normalized(message: str) -> str:
    return _DIGITS.sub("#", message)


def group_entries(entries: Iterable[LogEntry], *, limit: int) -> tuple[list[dict[str, Any]], int]:
    """Collapse entries into one row per distinct failure, newest counts kept.

    Returns the groups (most frequent first) and the total number of entries
    seen, which is reported separately so a truncated group list still tells
    the consumer the true volume.
    """
    # The statement is deliberately NOT part of the key.  PostgreSQL omits the
    # STATEMENT line when the error is raised outside a statement, and the
    # extended protocol can log it only once per prepared statement, so keying
    # on it splits a single repeating failure into "with SQL" and "without SQL"
    # halves and destroys the count that makes the feed worth reading.
    buckets: dict[tuple[str, str, str], dict[str, Any]] = {}
    total = 0
    for entry in entries:
        total += 1
        key = (entry.level, entry.sqlstate, _normalized(entry.message))
        bucket = buckets.get(key)
        if bucket is None:
            buckets[key] = {
                "level": entry.level,
                "sqlstate": entry.sqlstate,
                "message": entry.message,
                "statements": [entry.statement] if entry.statement else [],
                "count": 1,
                "first_seen": entry.occurred_at,
                "last_seen": entry.occurred_at,
                "applications": {entry.application} if entry.application else set(),
                "detail": entry.attachments.get("DETAIL"),
                "hint": entry.attachments.get("HINT"),
            }
            continue
        bucket["count"] += 1
        if entry.occurred_at < bucket["first_seen"]:
            bucket["first_seen"] = entry.occurred_at
        if entry.occurred_at > bucket["last_seen"]:
            bucket["last_seen"] = entry.occurred_at
        if entry.application:
            bucket["applications"].add(entry.application)
        if entry.statement and entry.statement not in bucket["statements"]:
            # Two statements can normalize to the same message; keep a few so
            # the consumer can tell which call sites are involved.
            if len(bucket["statements"]) < MAX_STATEMENTS_PER_GROUP:
                bucket["statements"].append(entry.statement)
        if bucket["detail"] is None:
            bucket["detail"] = entry.attachments.get("DETAIL")
        if bucket["hint"] is None:
            bucket["hint"] = entry.attachments.get("HINT")

    # Two stable passes: datetimes cannot be negated, so order by recency first
    # and let the count sort reorder only where counts differ.
    ordered = sorted(buckets.values(), key=lambda item: item["last_seen"], reverse=True)
    ordered.sort(key=lambda item: item["count"], reverse=True)
    rows: list[dict[str, Any]] = []
    for bucket in ordered[: max(0, limit)]:
        rows.append({
            "level": bucket["level"],
            "sqlstate": bucket["sqlstate"],
            "message": bucket["message"],
            "statement": bucket["statements"][0] if bucket["statements"] else None,
            "statements": bucket["statements"],
            "detail": bucket["detail"],
            "hint": bucket["hint"],
            "count": bucket["count"],
            "first_seen": bucket["first_seen"].isoformat(),
            "last_seen": bucket["last_seen"].isoformat(),
            "applications": sorted(bucket["applications"]),
        })
    return rows, total


def log_files_for_range(log_dir: Path, since: datetime, until: datetime) -> list[Path]:
    """The daily log files whose name falls inside the requested window.

    ``postgresql-startup.log`` and any other unnamed file are skipped: only the
    rotated daily files carry a date that can be bounded cheaply.
    """
    first: date = since.astimezone(LOG_TIMEZONE).date()
    last: date = until.astimezone(LOG_TIMEZONE).date()
    selected: list[tuple[date, Path]] = []
    if not log_dir.is_dir():
        return []
    for path in log_dir.glob(LOG_FILENAME_PATTERN):
        match = _FILENAME_DATE.match(path.name)
        if match is None:
            continue
        stamp = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
        if first <= stamp <= last:
            selected.append((stamp, path))
    return [path for _, path in sorted(selected)]


def collect_role_errors(
    *,
    log_dir: Path,
    role: str,
    since: datetime,
    until: datetime,
    limit: int = 50,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> dict[str, Any]:
    """Build the feed payload for one role over one time window."""
    files = log_files_for_range(log_dir, since, until)
    budget = max_bytes
    truncated = False
    read_files: list[str] = []

    def _entries() -> Iterator[LogEntry]:
        nonlocal budget, truncated
        for path in files:
            size = path.stat().st_size
            if size > budget:
                truncated = True
                continue
            budget -= size
            read_files.append(path.name)
            with path.open("r", encoding="utf-8", errors="replace") as handle:
                for entry in iter_entries(handle):
                    if entry.role != role:
                        continue
                    if since <= entry.occurred_at <= until:
                        yield entry

    groups, total = group_entries(_entries(), limit=limit)
    return {
        "role": role,
        "since": since.isoformat(),
        "until": until.isoformat(),
        "log_timezone": "Asia/Shanghai",
        "total_entries": total,
        "distinct_problems": len(groups),
        "groups": groups,
        "scanned_files": read_files,
        "truncated": truncated,
        "attribution_available_since": "2026-09-20T11:19:33+08:00",
    }


__all__ = [
    "ATTACHMENT_LEVELS",
    "DEFAULT_MAX_BYTES",
    "LOG_TIMEZONE",
    "LogEntry",
    "PROBLEM_LEVELS",
    "collect_role_errors",
    "group_entries",
    "iter_entries",
    "log_files_for_range",
]
