"""Read-only token usage accounting from Codex rollout files.

Only metadata and token counters are parsed; message bodies are skipped. Usage
is taken from ``token_count`` events (``last_token_usage`` for per-day/model
buckets, the final ``total_token_usage`` for a session's total). Subagent usage
is folded into its parent session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from cx.sessions import SessionInfo, _rollout_files, list_sessions


@dataclass
class Totals:
    input: int = 0
    cached: int = 0
    output: int = 0
    reasoning: int = 0
    events: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output

    @property
    def cache_rate(self) -> float:
        return (self.cached / self.input) if self.input else 0.0

    def add(self, other: Totals) -> None:
        self.input += other.input
        self.cached += other.cached
        self.output += other.output
        self.reasoning += other.reasoning
        self.events += other.events


@dataclass
class SessionUsage:
    info: SessionInfo
    totals: Totals = field(default_factory=Totals)


@dataclass
class UsageReport:
    total: Totals = field(default_factory=Totals)
    by_day: dict[str, Totals] = field(default_factory=dict)
    by_model: dict[str, Totals] = field(default_factory=dict)
    sessions: list[SessionUsage] = field(default_factory=list)


def _counters(value: object) -> Totals:
    if not isinstance(value, dict):
        return Totals()
    def n(key: str) -> int:
        v = value.get(key)
        return int(v) if isinstance(v, (int, float)) else 0

    return Totals(
        input=n("input_tokens"),
        cached=n("cached_input_tokens"),
        output=n("output_tokens"),
        reasoning=n("reasoning_output_tokens"),
        events=1,
    )


def _date_of(record: dict) -> str:
    ts = record.get("timestamp") or ""
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(
            UTC
        ).strftime("%Y-%m-%d")
    except ValueError:
        return "unknown"


def _scan_file(path: Path, model: str) -> tuple[Totals, str, dict[str, Totals]]:
    """Return (session totals, final model, per-day totals) for one rollout."""
    session = Totals()
    by_day: dict[str, Totals] = {}
    last_total: Totals | None = None
    current_model = model or "unknown"
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return session, current_model, by_day
    with fh:
        for line in fh:
            if '"token_count"' not in line and '"turn_context"' not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            rtype = record.get("type")
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            if rtype == "turn_context":
                m = payload.get("model")
                if isinstance(m, str) and m:
                    current_model = m
            elif rtype == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info")
                if not isinstance(info, dict):
                    info = payload
                last = _counters(info.get("last_token_usage"))
                total = _counters(info.get("total_token_usage"))
                if last.events:
                    session.add(last)
                    day = _date_of(record)
                    by_day.setdefault(day, Totals()).add(last)
                if total.events:
                    last_total = total
    if last_total is not None:
        session = last_total
    return session, current_model, by_day


def scan_usage() -> UsageReport:
    report = UsageReport()
    sessions = {s.id: s for s in list_sessions()}
    by_id: dict[str, SessionUsage] = {}

    for tid, path in _rollout_files().items():
        info = sessions.get(tid) or SessionInfo(id=tid)
        totals, model, per_day = _scan_file(path, info.model)
        info.model = model or info.model
        by_id[tid] = SessionUsage(info=info, totals=totals)
        report.total.add(totals)
        for day, t in per_day.items():
            report.by_day.setdefault(day, Totals()).add(t)
        report.by_model.setdefault(model, Totals()).add(totals)

    # fold subagents into their parent session
    for usage in by_id.values():
        parent = usage.info.parent_id
        if parent and parent in by_id:
            by_id[parent].totals.add(usage.totals)
        elif not usage.info.is_subagent:
            report.sessions.append(usage)

    report.sessions.sort(key=lambda s: s.totals.total, reverse=True)
    return report
