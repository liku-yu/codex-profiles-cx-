"""Read and manage Codex local sessions (threads + rollout files).

Read-only by default. The one write operation, ``sync_provider``, only rewrites
the recorded ``model_provider`` in each session's rollout ``session_meta`` and in
the SQLite ``threads`` index so that old sessions stay usable after you switch
providers. Chat content is never touched, and everything is backed up first.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from cx import state
from cx.codex import codex_home

_SUBAGENT_SOURCE_MARKERS = ("subagent", "guardian_review", "memory_consolidation")


def sessions_dir() -> Path:
    return codex_home() / "sessions"


def archived_dir() -> Path:
    return codex_home() / "archived_sessions"


def state_dbs() -> list[Path]:
    home = codex_home()
    dbs = sorted(home.glob("state_*.sqlite"))
    return [p for p in dbs if p.is_file()]


def current_provider() -> str:
    """The provider currently configured in config.toml (default openai)."""
    from cx.profiles import read_base_config

    base = read_base_config() or {}
    return str(base.get("model_provider") or "openai")


def _is_subagent_source(value: str) -> bool:
    text = (value or "").lower()
    return any(marker in text for marker in _SUBAGENT_SOURCE_MARKERS)


@dataclass
class SessionInfo:
    id: str
    title: str = ""
    provider: str = ""
    model: str = ""
    effort: str = ""
    cwd: str = ""
    created_at: int = 0
    updated_at: int = 0
    archived: bool = False
    source: str = ""
    is_subagent: bool = False
    parent_id: str | None = None
    tokens_used: int = 0
    rollout_path: str | None = None
    needs_sync: bool = False

    @property
    def label(self) -> str:
        return self.title or self.id


def _read_db(db: Path) -> list[SessionInfo]:
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    conn.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(threads)")}
        if "id" not in cols:
            return []
        parents: dict[str, str] = {}
        try:
            for row in conn.execute(
                "SELECT parent_thread_id, child_thread_id FROM thread_spawn_edges"
            ):
                parents[row[1]] = row[0]
        except sqlite3.Error:
            pass

        def val(row: sqlite3.Row, name: str, default: object = "") -> object:
            return row[name] if name in cols else default

        out: list[SessionInfo] = []
        for row in conn.execute("SELECT * FROM threads"):
            tid = str(val(row, "id"))
            source = str(val(row, "thread_source") or val(row, "source"))
            parent = parents.get(tid)
            is_sub = bool(parent) or _is_subagent_source(source)
            out.append(
                SessionInfo(
                    id=tid,
                    title=str(val(row, "name") or val(row, "title") or val(row, "preview")),
                    provider=str(val(row, "model_provider")),
                    model=str(val(row, "model")),
                    effort=str(val(row, "reasoning_effort")),
                    cwd=str(val(row, "cwd")),
                    created_at=int(val(row, "created_at", 0) or 0),
                    updated_at=int(val(row, "updated_at", 0) or 0),
                    archived=bool(val(row, "archived", 0)),
                    source=source,
                    is_subagent=is_sub,
                    parent_id=parent,
                    tokens_used=int(val(row, "tokens_used", 0) or 0),
                    rollout_path=str(val(row, "rollout_path")) or None,
                )
            )
        return out
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def list_sessions() -> list[SessionInfo]:
    """All local sessions across every state DB, newest first."""
    target = current_provider()
    by_id: dict[str, SessionInfo] = {}
    for db in state_dbs():
        for info in _read_db(db):
            if info.id not in by_id:
                by_id[info.id] = info
    for info in by_id.values():
        info.needs_sync = (
            bool(info.provider)
            and info.provider != target
            and not info.archived
            and not info.is_subagent
        )
    return sorted(by_id.values(), key=lambda s: s.updated_at, reverse=True)


# --------------------------------------------------------------------------
# writes (backup first)
# --------------------------------------------------------------------------
def _backup_root() -> Path:
    return state.config_dir() / "session-backups"


def _backup(paths: list[Path]) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = _backup_root() / stamp
    dest.mkdir(parents=True, exist_ok=True)
    for src in paths:
        if not src.is_file():
            continue
        # flatten with a unique-ish name
        target = dest / f"{src.parent.name}__{src.name}"
        counter = 1
        while target.exists():
            target = dest / f"{src.parent.name}__{src.name}.{counter}"
            counter += 1
        shutil.copy2(src, target)
    return dest


def _rollout_files() -> dict[str, Path]:
    files: dict[str, Path] = {}
    for base in (sessions_dir(), archived_dir()):
        if not base.is_dir():
            continue
        for path in base.rglob("rollout-*.jsonl"):
            # rollout-<ts>-<thread_id>.jsonl  (or thread_id_rollout_id)
            name = path.name
            if not name.startswith("rollout-") or not name.endswith(".jsonl"):
                continue
            core = name[len("rollout-") : -len(".jsonl")]
            ids = core[20:] if len(core) > 20 else core
            thread_id = ids.split("_", 1)[0]
            files.setdefault(thread_id, path)
    return files


def sync_provider(ids: list[str], target: str | None = None) -> dict[str, object]:
    """Rebind the recorded provider of ``ids`` to ``target`` (backup first)."""
    target = (target or current_provider()).strip() or "openai"
    ids_set = set(ids)
    if not ids_set:
        return {"updated_threads": 0, "updated_rollouts": 0, "backup": None}

    rollouts = _rollout_files()
    to_backup = [db for db in state_dbs()]
    to_backup += [rollouts[i] for i in ids_set if i in rollouts]
    backup = _backup(to_backup)

    updated_threads = 0
    for db in state_dbs():
        try:
            conn = sqlite3.connect(db)
        except sqlite3.Error:
            continue
        try:
            for tid in ids_set:
                updated_threads += conn.execute(
                    "UPDATE threads SET model_provider=? WHERE id=?", (target, tid)
                ).rowcount
            conn.commit()
        finally:
            conn.close()

    updated_rollouts = 0
    for tid in ids_set:
        path = rollouts.get(tid)
        if path is not None and _rewrite_rollout_provider(path, target):
            updated_rollouts += 1

    return {
        "updated_threads": updated_threads,
        "updated_rollouts": updated_rollouts,
        "backup": str(backup),
        "target": target,
    }


def _rewrite_rollout_provider(path: Path, target: str) -> bool:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    changed = False
    out: list[str] = []
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        try:
            record = json.loads(line)
        except ValueError:
            out.append(line)
            continue
        if record.get("type") == "session_meta" and isinstance(record.get("payload"), dict):
            payload = record["payload"]
            if payload.get("model_provider") != target:
                payload["model_provider"] = target
                changed = True
                line = json.dumps(record, ensure_ascii=False)
        out.append(line)
    if changed:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        tmp.replace(path)
    return changed


def delete_sessions(ids: list[str]) -> dict[str, object]:
    """Permanently delete sessions (rollout + index). Backup first."""
    ids_set = set(ids)
    if not ids_set:
        return {"deleted": 0}
    rollouts = _rollout_files()
    home = codex_home()
    side_dbs = [
        (
            home / "thread_history_1.sqlite",
            (
                ("thread_turns", "thread_id"),
                ("thread_items", "thread_id"),
                ("thread_history_projection_state", "thread_id"),
                ("thread_realtime_items", "thread_id"),
            ),
        ),
        (home / "logs_2.sqlite", (("logs", "thread_id"),)),
    ]
    to_backup = list(state_dbs()) + [db for db, _ in side_dbs if db.is_file()]
    to_backup += [rollouts[i] for i in ids_set if i in rollouts]
    backup = _backup(to_backup)

    deleted = 0
    for db in state_dbs():
        try:
            conn = sqlite3.connect(db)
        except sqlite3.Error:
            continue
        try:
            for tid in ids_set:
                conn.execute("DELETE FROM threads WHERE id=?", (tid,))
                try:
                    conn.execute(
                        "DELETE FROM thread_spawn_edges WHERE parent_thread_id=? OR child_thread_id=?",
                        (tid, tid),
                    )
                except sqlite3.Error:
                    pass
            conn.commit()
        finally:
            conn.close()
    for db, tables in side_dbs:
        if not db.is_file():
            continue
        try:
            conn = sqlite3.connect(db)
        except sqlite3.Error:
            continue
        try:
            for table, column in tables:
                for tid in ids_set:
                    try:
                        conn.execute(f"DELETE FROM {table} WHERE {column}=?", (tid,))
                    except sqlite3.Error:
                        pass
            conn.commit()
        finally:
            conn.close()
    for tid in ids_set:
        path = rollouts.get(tid)
        if path is not None and path.is_file():
            path.unlink()
            deleted += 1
        for extra in (
            codex_home() / "thread-writer-locks" / f"{tid}.lock",
            codex_home() / "tui-thread-reference-capabilities" / tid,
        ):
            if extra.exists():
                extra.unlink()
    return {"deleted": deleted, "backup": str(backup)}
