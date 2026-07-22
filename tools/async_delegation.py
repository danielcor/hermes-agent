#!/usr/bin/env python3
"""
Async (background) delegation registry.

Backs ``delegate_task(background=true)``: the parent agent dispatches a
subagent that runs on a module-level daemon executor and returns a handle
immediately, so the user and the model can keep working while the child runs.

When the child finishes, a completion event is pushed onto the SHARED
``process_registry.completion_queue`` with ``type="async_delegation"``. The
CLI (``cli.py`` process_loop) and gateway (``_run_process_watcher`` /
``completion_queue`` drain) already poll that queue while the agent is idle
and forge a fresh user/internal turn from each event. We deliberately reuse
that rail rather than reaching into a running agent loop:

  - completions surface as a NEW turn when the agent is idle, never spliced
    between a tool result and an assistant message. That keeps strict
    message-role alternation legal and the prompt cache intact (hard
    invariant: never mutate past context).
  - we inherit the queue's de-dup, crash-recovery checkpoint, and the
    existing CLI + gateway drain wiring for free — no new drain loops in the
    two largest files in the repo.

The completion payload carries a RICH, self-contained task-source block (the
original goal, the context the parent supplied, toolsets, model, dispatch
time, status, and the full result summary). When the result re-enters the
conversation the parent may be deep in unrelated context and won't remember
why the subagent existed; the block lets it either use the result or
re-dispatch if the world has moved on.

This module owns ONLY the async lifecycle. The actual child build + run is
delegated back to ``delegate_tool._run_single_child`` via an injected
runner, so all the credential leasing, heartbeat, timeout, and result-shaping
logic stays in one place.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

# Back-compat alias — the daemon executor now lives in tools.daemon_pool so
# other subsystems (tool_executor, memory_manager, delegate_tool, skills_hub)
# can share it. Existing imports of ``_DaemonThreadPoolExecutor`` keep working.
_DaemonThreadPoolExecutor = DaemonThreadPoolExecutor


# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
# A persistent daemon executor (NOT a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat the whole point of async). Workers are daemon
# threads so a hard process exit doesn't hang on an in-flight child.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

_records_lock = threading.Lock()
# delegation_id -> record dict. Kept for the lifetime of the run plus a short
# tail after completion so `list_async_delegations()` can show recent results.
_records: Dict[str, Dict[str, Any]] = {}

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# How many completed records to retain for status queries before pruning.
_MAX_RETAINED_COMPLETED = 50
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
_DELIVERY_CLAIM_LEASE_SECONDS = 300.0
_DB_LOCK = threading.Lock()


def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS async_delegations (
            delegation_id TEXT PRIMARY KEY,
            origin_session TEXT NOT NULL,
            origin_ui_session_id TEXT NOT NULL DEFAULT '',
            parent_session_id TEXT,
            state TEXT NOT NULL,
            dispatched_at REAL NOT NULL,
            completed_at REAL,
            updated_at REAL NOT NULL,
            event_json TEXT,
            result_json TEXT,
            delivery_state TEXT NOT NULL DEFAULT 'pending',
            delivery_attempts INTEGER NOT NULL DEFAULT 0,
            delivered_at REAL,
            owner_pid INTEGER,
            owner_started_at INTEGER,
            task_json TEXT,
            delivery_claim TEXT,
            delivery_claimed_at REAL
        )"""
    )
    columns = {row[1] for row in conn.execute("PRAGMA table_info(async_delegations)")}
    for name, sql_type in (
        ("owner_pid", "INTEGER"),
        ("owner_started_at", "INTEGER"),
        ("task_json", "TEXT"),
        ("delivery_claim", "TEXT"),
        ("delivery_claimed_at", "REAL"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE async_delegations ADD COLUMN {name} {sql_type}")
    return conn


def _persist_dispatch(record: Dict[str, Any]) -> None:
    now = time.time()
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(__import__("os").getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in (
            "goal", "goals", "context", "toolsets", "role", "lane",
            "provider", "model", "is_batch",
        )
        if key in record
    }
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?)""",
            (record["delegation_id"], record.get("session_key", ""),
             record.get("origin_ui_session_id", ""), record.get("parent_session_id"),
             record["dispatched_at"], now, __import__("os").getpid(),
             owner_started_at, json.dumps(task_payload)),
        )
    _prune_durable_records()


def _delete_durable_delegation(delegation_id: str) -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))


def _best_effort_delete_durable(
    delegation_id: str, operation: str, *, suppress_recovery: bool = False
) -> None:
    """Remove a durable row without letting rollback failure escape the API."""
    try:
        _delete_durable_delegation(delegation_id)
    except Exception:  # noqa: BLE001 — secondary rollback must not mask primary failure
        logger.error(
            "Async delegation %s durable cleanup failed during %s",
            delegation_id,
            operation,
            exc_info=True,
        )
        if suppress_recovery:
            try:
                now = time.time()
                with _DB_LOCK, _connect() as conn:
                    conn.execute(
                        """UPDATE async_delegations
                           SET state='failed', completed_at=?, updated_at=?,
                               delivery_state='delivered'
                           WHERE delegation_id=?""",
                        (now, now, delegation_id),
                    )
            except Exception:  # noqa: BLE001 — original rejection still wins
                logger.error(
                    "Async delegation %s durable tombstone failed during %s",
                    delegation_id,
                    operation,
                    exc_info=True,
                )


def _prune_durable_records() -> None:
    """Bound terminal history, preferring delivered records for deletion."""
    now = time.time()
    cutoff = now - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "DELETE FROM async_delegations WHERE delivery_state='delivered' AND updated_at < ?",
            (cutoff,),
        )
        delivered_count = conn.execute(
            """SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('running','finalizing')
                 AND delivery_state='delivered'"""
        ).fetchone()[0]
        excess = max(0, delivered_count - _MAX_RETAINED_COMPLETED)
        if excess:
            conn.execute(
                """DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('running','finalizing')
                       AND delivery_state='delivered'
                     ORDER BY updated_at ASC LIMIT ?
                   )""",
                (excess,),
            )
        # Undelivered completions are durable work, not terminal history. Never
        # prune them by count: doing so silently loses a result that no consumer
        # has acknowledged. Delivered rows above remain bounded and age-pruned.


def _persist_completion(event: Dict[str, Any], result: Dict[str, Any]) -> None:
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state='pending'
               WHERE delegation_id=?""",
            (event.get("status", "completed"), event.get("completed_at", now), now,
             json.dumps(event), json.dumps(result), event["delegation_id"]),
        )


def _note_delivery_attempt(delegation_id: str) -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "UPDATE async_delegations SET delivery_attempts=delivery_attempts+1, updated_at=? WHERE delegation_id=?",
            (time.time(), delegation_id),
        )


def recover_abandoned_delegations() -> int:
    """Fail abandoned records without publishing non-canonical terminal states."""
    try:
        from gateway.status import _pid_exists, get_process_start_time
    except Exception:
        return 0
    now = time.time()
    recovered = 0
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json, delivery_state
               FROM async_delegations WHERE state IN ('running','finalizing')"""
        ).fetchall()
        for row in rows:
            (
                delegation_id, session_key, origin_ui, parent_id, dispatched_at,
                pid, started, task_json, delivery_state,
            ) = row
            live = False
            if pid:
                live = _pid_exists(int(pid))
                if live and started is not None:
                    live = get_process_start_time(int(pid)) == int(started)
            if live:
                continue
            if delivery_state == "delivered":
                # The terminal event was accepted even though its durable
                # completion update failed. Drop the stale running row rather
                # than publishing a contradictory recovery event.
                conn.execute(
                    "DELETE FROM async_delegations WHERE delegation_id=?",
                    (delegation_id,),
                )
                recovered += 1
                continue
            try:
                task = json.loads(task_json or "{}")
                if not isinstance(task, dict):
                    raise TypeError("task payload is not an object")
            except (json.JSONDecodeError, TypeError):
                logger.error(
                    "Async delegation %s has malformed durable task metadata; "
                    "recovering without task identity",
                    delegation_id,
                )
                task = {}
                conn.execute(
                    "UPDATE async_delegations SET task_json='{}' "
                    "WHERE delegation_id=?",
                    (delegation_id,),
                )
            event = {
                "type": "async_delegation", "delegation_id": delegation_id,
                "session_key": session_key, "origin_ui_session_id": origin_ui,
                "parent_session_id": parent_id, "goal": task.get("goal", ""),
                "goals": task.get("goals"), "context": task.get("context"),
                "toolsets": task.get("toolsets"), "role": task.get("role"),
                "lane": task.get("lane"), "provider": task.get("provider"),
                "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
                "status": "failed", "summary": None,
                "error": "Delegation owner exited before recording a terminal result; outcome unknown.",
                "exit_reason": "owner_exit",
                "dispatched_at": dispatched_at, "completed_at": now,
            }
            result = {
                "status": "failed", "summary": None, "error": event["error"],
                "exit_reason": "owner_exit",
                "lane": task.get("lane"), "provider": task.get("provider"),
                "model": task.get("model"),
            }
            conn.execute(
                """UPDATE async_delegations SET state='failed', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                   WHERE delegation_id=?""",
                (now, now, json.dumps(event), json.dumps(result), delegation_id),
            )
            recovered += 1
    return recovered


def restore_undelivered_completions(target_queue) -> int:
    """Enqueue durable pending completions as fresh turns after process start.

    Every restored event is stamped ``restored=True`` (in-memory only — the
    stamp is added after the durable payload is deserialized and is never
    persisted). Restored events originate from a *previous* process, so no
    consumer in THIS process implicitly owns them: drain paths that run
    without an ownership filter (the legacy single-session behavior) must
    leave them queued for a consumer that can positively prove ownership,
    otherwise a brand-new session adopts a dead session's delegation
    results seconds after boot (#64484).
    """
    recover_abandoned_delegations()
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute(
            """SELECT delegation_id, event_json FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id"""
        ).fetchall()
        restored = 0
        for delegation_id, payload in rows:
            try:
                evt = json.loads(payload)
                if not isinstance(evt, dict):
                    raise TypeError("event payload is not an object")
            except (json.JSONDecodeError, TypeError):
                logger.error(
                    "Async delegation %s has malformed durable completion; "
                    "discarding corrupt row",
                    delegation_id,
                )
                conn.execute(
                    "DELETE FROM async_delegations WHERE delegation_id=?",
                    (delegation_id,),
                )
                continue
            if isinstance(evt, dict):
                evt["restored"] = True
            target_queue.put(evt)
            restored += 1
    return restored


def mark_completion_delivered(delegation_id: str) -> bool:
    """Atomically acknowledge successful injection of a durable completion."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state!='delivered'""",
            (now, now, delegation_id),
        )
        return cur.rowcount == 1


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (
                claim_id,
                now,
                now,
                delegation_id,
                now - _DELIVERY_CLAIM_LEASE_SECONDS,
            ),
        )
        return cur.rowcount == 1


def renew_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Renew a pending delivery lease only for its current fenced owner."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claimed_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        return cur.rowcount == 1


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events need no token."""
    if evt.get("type") != "async_delegation":
        return ""
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{__import__('os').getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def event_delivery_is_pending(evt: Dict[str, Any]) -> bool:
    """Return whether a blocked durable event still requires later delivery."""
    if evt.get("type") != "async_delegation":
        return False
    delegation_id = str(evt.get("delegation_id") or "")
    if not delegation_id:
        return False
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
    return row is not None and row[0] == "pending"


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry."""
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_claim=NULL,
                      delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (time.time(), delegation_id, claim_id),
        )
        return cur.rowcount == 1


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    now = time.time()
    with _DB_LOCK, _connect() as conn:
        cur = conn.execute(
            """UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""",
            (now, now, delegation_id, claim_id),
        )
        if cur.rowcount == 1:
            return True
        return conn.execute(
            "SELECT 1 FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone() is None


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        complete_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        release_completion_delivery(str(evt.get("delegation_id") or ""), claim_id)


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK, _connect() as conn:
        row = conn.execute(
            """SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,),
        ).fetchone()
    if row is None:
        return None
    return {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1],
        "dispatched_at": row[2], "completed_at": row[3],
        "result": json.loads(row[4]) if row[4] else None,
        "delivery_state": row[5], "delivery_attempts": row[6],
    }


def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow) the shared daemon executor.

    We never shrink — ThreadPoolExecutor can't resize — but if the configured
    cap grows between calls we rebuild a larger pool. Existing in-flight
    futures keep running on the old pool until it's garbage collected.
    """
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None or max_workers > _executor_max_workers:
            # Daemon threads: thread_name_prefix aids debugging in stack dumps.
            _executor = _DaemonThreadPoolExecutor(
                max_workers=max_workers,
                thread_name_prefix="async-delegate",
            )
            _executor_max_workers = max_workers
        return _executor


def _submit_after_acceptance(executor, worker):
    """Submit *worker* without executing an ambiguously queued work item."""
    accepted = threading.Event()
    cancelled = threading.Event()

    def _gated_worker():
        accepted.wait()
        if cancelled.is_set():
            return None
        return worker()

    try:
        future = executor.submit(propagate_context_to_thread(_gated_worker))
    except Exception:
        # ThreadPoolExecutor queues before starting a new worker. A submit
        # exception can therefore leave this wrapper available to an existing
        # worker; cancel it before releasing the gate.
        cancelled.set()
        accepted.set()
        raise
    accepted.set()
    return future


def active_count() -> int:
    """Number of async delegations currently running."""
    with _records_lock:
        return sum(1 for r in _records.values() if r.get("status") in {"running", "finalizing"})


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the retention cap.

    Caller must hold ``_records_lock``.
    """
    completed = [
        (rid, r)
        for rid, r in _records.items()
        if r.get("status") != "running"
    ]
    if len(completed) <= _MAX_RETAINED_COMPLETED:
        return
    # Oldest-first by completion time (fall back to dispatch time).
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: len(completed) - _MAX_RETAINED_COMPLETED]:
        _records.pop(rid, None)


def dispatch_async_delegation(
    *,
    goal: str,
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.

    Parameters
    ----------
    goal, context, toolsets, role, model
        The dispatch-time task spec, captured verbatim for the rich
        completion block.
    session_key
        The gateway session_key (from ``tools.approval.get_current_session_key``)
        captured on the parent thread BEFORE dispatch, because the daemon
        worker thread won't carry the contextvar. Used to route the
        completion back to the originating session.
    parent_session_id
        The durable ``state.db`` session id of the parent agent that spawned
        the delegation. Carried on the completion event so the gateway can
        pin routing to the spawning session instead of recovering the latest
        ``ended_at IS NULL`` row for the peer tuple (#57498).
    runner
        Zero-arg callable that builds + runs the child and returns the same
        result dict ``_run_single_child`` produces. Runs on the worker thread.
    interrupt_fn
        Optional callable to signal the child to stop (used on shutdown /
        explicit cancel).
    max_async_children
        Concurrency cap. When at capacity the dispatch is REJECTED (the caller
        should fall back to sync or tell the user) rather than queued, so a
        runaway model can't pile up unbounded background work.

    Returns
    -------
    dict
        ``{"status": "dispatched", "delegation_id": ...}`` on success, or
        ``{"status": "rejected", "error": ...}`` when at capacity.
    """
    delegation_id = _new_delegation_id()
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": goal,
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
    }
    # Capacity check and record insert under ONE lock hold — checking
    # active_count() separately would let two concurrent dispatches (e.g.
    # from different gateway sessions) both pass the check and exceed the cap.
    with _records_lock:
        running = sum(
            1 for r in _records.values() if r.get("status") == "running"
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or run this task synchronously "
                    f"(background=false). Raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background subagents."
                ),
            }
        _records[delegation_id] = record

    try:
        _persist_dispatch(record)
    except Exception as exc:  # noqa: BLE001 — dispatch must fail closed
        with _records_lock:
            _records.pop(delegation_id, None)
        _best_effort_delete_durable(
            delegation_id,
            "dispatch persistence failure",
            suppress_recovery=True,
        )
        logger.error(
            "Async delegation %s persistence failed (%s)",
            delegation_id,
            type(exc).__name__,
        )
        return {
            "status": "rejected",
            "error": (
                "Failed to persist async delegation "
                f"({type(exc).__name__})"
            ),
        }
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        try:
            result = runner() or {}
            status = result.get("status") or "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.error(
                "Async delegation %s crashed (%s)",
                delegation_id,
                type(exc).__name__,
            )
            result = {
                "status": "error",
                "summary": None,
                "error": f"{type(exc).__name__}: async delegation failed",
                "api_calls": 0,
                "duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize(delegation_id, result, status)

    try:
        # Propagate the dispatching profile so the detached child resolves
        # get_hermes_home() under the right profile.
        _submit_after_acceptance(executor, _worker)
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        with _records_lock:
            _records.pop(delegation_id, None)
        _best_effort_delete_durable(
            delegation_id, "submit rollback", suppress_recovery=True
        )
        return {
            "status": "rejected",
            "error": f"Failed to schedule async delegation ({type(exc).__name__})",
        }

    logger.info(
        "Dispatched async delegation %s (session_key=%s): %s",
        delegation_id, session_key or "<cli>", (goal or "")[:80],
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


_FAILED_PUBLIC_STATUSES = frozenset(
    {"error", "failed", "timeout", "interrupted", "cancelled"}
)
_SAFE_FAILED_ERROR = "DelegationError: subagent reported failure"
_SAFE_EXIT_REASONS = frozenset(
    {
        "cancelled",
        "error",
        "failed",
        "interrupted",
        "max_iterations",
        "owner_exit",
        "timeout",
    }
)


def _canonical_public_status(status: str) -> str:
    if status in {"interrupted", "cancelled"}:
        return "interrupted"
    if status in {"error", "failed", "timeout"}:
        return "failed"
    return status


def _trusted_timeout_diagnostic_path(value: Any) -> Optional[str]:
    """Return only diagnostics created in this profile's timeout-log directory."""
    if not isinstance(value, str) or not value:
        return None
    try:
        candidate = Path(value).resolve(strict=False)
        logs_dir = (get_hermes_home() / "logs").resolve(strict=False)
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate.parent != logs_dir:
        return None
    if not candidate.name.startswith("subagent-timeout-") or candidate.suffix != ".log":
        return None
    return str(candidate)


def _sanitize_completion_payload(
    result: Dict[str, Any], status: str
) -> tuple[Dict[str, Any], str]:
    """Fail closed before completion data reaches queues or durable storage."""
    safe = dict(result) if isinstance(result, dict) else {}
    if not isinstance(status, str):
        status = "error"

    child_results = safe.get("results")
    if isinstance(child_results, list):
        sanitized_children = []
        for child in child_results:
            if not isinstance(child, dict):
                continue
            child_status = child.get("status")
            if not isinstance(child_status, str):
                child_status = "error"
            sanitized_child, _ = _sanitize_completion_payload(child, child_status)
            sanitized_children.append(sanitized_child)
        safe["results"] = sanitized_children
        if sanitized_children and all(
            child.get("status") not in ("completed", "success")
            for child in sanitized_children
        ):
            status = (
                "interrupted"
                if all(
                    child.get("status") == "interrupted"
                    for child in sanitized_children
                )
                else "failed"
            )
            child_exit_reasons = {
                child.get("exit_reason")
                for child in sanitized_children
                if isinstance(child.get("exit_reason"), str)
                and child.get("exit_reason") in _SAFE_EXIT_REASONS
            }
            if len(child_exit_reasons) == 1:
                safe["exit_reason"] = child_exit_reasons.pop()

    reported_failure = (
        status in _FAILED_PUBLIC_STATUSES
        or safe.get("failed") is True
        or bool(safe.get("error"))
    )
    if reported_failure:
        original_exit_reason = safe.get("exit_reason")
        preserved_exit_reason = (
            original_exit_reason
            if isinstance(original_exit_reason, str)
            and original_exit_reason in _SAFE_EXIT_REASONS
            else None
        )
        diagnostic_path = (
            _trusted_timeout_diagnostic_path(safe.get("diagnostic_path"))
            if preserved_exit_reason == "timeout"
            else None
        )
        status = _canonical_public_status(status)
        if status not in {"failed", "interrupted"}:
            status = "failed"
        # Reconstruct from a strict allowlist. Failed child payloads are
        # untrusted provider/model output and may hide route details in any
        # arbitrary nested key, not only summary/error.
        allowed = {
            "task_index",
            "lane",
            "provider",
            "model",
            "api_calls",
            "duration_seconds",
            "total_duration_seconds",
            "dispatched_at",
            "completed_at",
            "live_transcript",
            "live_transcripts",
        }
        rebuilt = {key: safe[key] for key in allowed if key in safe}
        if "results" in safe:
            rebuilt["results"] = safe["results"]
        rebuilt.update(
            {
                "status": status,
                "summary": None,
                "error": _SAFE_FAILED_ERROR,
                "exit_reason": preserved_exit_reason or status,
            }
        )
        if diagnostic_path is not None:
            rebuilt["diagnostic_path"] = diagnostic_path
        safe = rebuilt

    return safe, status


def _finalize(delegation_id: str, result: Dict[str, Any], status: str) -> None:
    """Mark a record complete and push the completion event onto the queue."""
    result, status = _sanitize_completion_payload(result, status)
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        # Stay active until durable persistence and queue publication finish;
        # otherwise process shutdown can kill this daemon worker in the narrow
        # gap after status flips but before SQLite is committed.
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None  # drop the closure; child is done
        event_record = dict(record)

    try:
        _push_completion_event(event_record, result, status)
    finally:
        with _records_lock:
            record = _records.get(delegation_id)
            if record is not None:
                record["status"] = status
            _prune_completed_locked()


def _push_completion_event(
    record: Dict[str, Any], result: Dict[str, Any], status: str
) -> None:
    """Push a type='async_delegation' event onto the shared completion queue.

    Best-effort: a failure here must not crash the worker, but it WOULD mean a
    silently-lost result, so we log loudly.
    """
    summary = result.get("summary")
    error = result.get("error")
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()

    evt = {
        "type": "async_delegation",
        "delegation_id": record.get("delegation_id"),
        # session_key routes the completion back to the originating gateway
        # session; empty string => CLI (single-session) path.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""),
        "context": record.get("context"),
        "toolsets": record.get("toolsets"),
        "role": record.get("role"),
        "lane": record.get("lane"),
        "provider": record.get("provider"),
        "model": result.get("model") or record.get("model"),
        "status": status,
        "summary": summary,
        "error": error,
        "api_calls": result.get("api_calls", 0),
        "duration_seconds": result.get(
            "duration_seconds", round(completed_at - dispatched_at, 2)
        ),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
        "exit_reason": result.get("exit_reason"),
    }
    durable_retained = True
    try:
        _persist_completion(evt, result)
    except Exception as exc:  # noqa: BLE001 — queue delivery can still succeed
        durable_retained = False
        logger.error(
            "Async delegation %s: completion persistence failed (%s); "
            "delivering in-memory result",
            record.get("delegation_id"),
            type(exc).__name__,
        )
        _best_effort_delete_durable(
            str(record.get("delegation_id") or ""),
            "completion persistence rollback",
        )
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s finished but process_registry import failed; "
            "%s: %s",
            record.get("delegation_id"),
            "durable result retained" if durable_retained else "result unavailable",
            exc,
        )
        return
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation %s: failed to enqueue completion event; "
            "%s: %s",
            record.get("delegation_id"),
            "durable result retained" if durable_retained else "result unavailable",
            exc,
        )


def dispatch_async_delegation_batch(
    *,
    goals: List[str],
    context: Optional[str],
    toolsets: Optional[List[str]],
    role: str,
    model: Optional[str],
    session_key: str,
    lane: Optional[str] = None,
    provider: Optional[str] = None,
    parent_session_id: Optional[str] = None,
    runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "",
    interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN,
    delegation_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Dispatch a WHOLE fan-out batch as ONE background unit.

    Unlike ``dispatch_async_delegation`` (which backs a single subagent),
    ``runner`` here runs the entire batch — it builds and joins on every child
    in parallel and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict that the synchronous path would have
    returned. We occupy ONE async slot for the whole batch (the in-batch
    parallelism is bounded separately by ``max_concurrent_children``), so a
    single ``delegate_task`` fan-out never exhausts the async pool by itself.

    When the batch finishes, a SINGLE completion event is pushed onto the
    shared ``process_registry.completion_queue`` carrying the full per-task
    ``results`` list, so the consolidated summaries re-enter the conversation
    as one message once every child is done — the chat is never blocked while
    they run.

    Returns ``{"status": "dispatched", "delegation_id": ...}`` on success or
    ``{"status": "rejected", "error": ...}`` when the async pool is at
    capacity.
    """
    delegation_id = delegation_id or _new_delegation_id()
    dispatched_at = time.time()
    n = len(goals)
    # A combined goal label for status listings / the completion header.
    combined_goal = (
        goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in goals)
    )
    record: Dict[str, Any] = {
        "delegation_id": delegation_id,
        "goal": combined_goal,
        "goals": list(goals),
        "context": context,
        "toolsets": list(toolsets) if toolsets else None,
        "role": role,
        "lane": lane,
        "provider": provider,
        "model": model,
        "session_key": session_key,
        "origin_ui_session_id": origin_ui_session_id,
        "parent_session_id": parent_session_id,
        "status": "running",
        "dispatched_at": dispatched_at,
        "completed_at": None,
        "interrupt_fn": interrupt_fn,
        "is_batch": True,
    }
    with _records_lock:
        running = sum(
            1 for r in _records.values() if r.get("status") == "running"
        )
        if running >= max_async_children:
            return {
                "status": "rejected",
                "error": (
                    f"Async delegation capacity reached ({max_async_children} "
                    f"running). Wait for one to finish (its result will re-enter "
                    f"the chat), or raise delegation.max_concurrent_children in "
                    f"config.yaml to allow more concurrent background units."
                ),
            }
        _records[delegation_id] = record

    try:
        _persist_dispatch(record)
    except Exception as exc:  # noqa: BLE001 — dispatch must fail closed
        with _records_lock:
            _records.pop(delegation_id, None)
        _best_effort_delete_durable(
            delegation_id,
            "batch dispatch persistence failure",
            suppress_recovery=True,
        )
        logger.error(
            "Async delegation batch %s persistence failed (%s)",
            delegation_id,
            type(exc).__name__,
        )
        return {
            "status": "rejected",
            "error": (
                "Failed to persist async delegation batch "
                f"({type(exc).__name__})"
            ),
        }
    executor = _get_executor(max_async_children)

    def _worker() -> None:
        combined: Dict[str, Any] = {}
        status = "error"
        try:
            combined = runner() or {}
            # Batch status: completed unless every child errored/was interrupted.
            child_results = combined.get("results") or []
            if child_results and all(
                (r.get("status") not in ("completed", "success"))
                for r in child_results
            ):
                status = "error"
            else:
                status = "completed"
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.error(
                "Async delegation batch %s crashed (%s)",
                delegation_id,
                type(exc).__name__,
            )
            combined = {
                "results": [],
                "error": f"{type(exc).__name__}: async delegation batch failed",
                "lane": lane,
                "provider": provider,
                "model": model,
                "total_duration_seconds": round(time.time() - dispatched_at, 2),
            }
            status = "error"
        finally:
            _finalize_batch(delegation_id, combined, status)

    try:
        # Propagate the dispatching profile to the detached batch children.
        _submit_after_acceptance(executor, _worker)
    except Exception as exc:  # pragma: no cover
        with _records_lock:
            _records.pop(delegation_id, None)
        _best_effort_delete_durable(
            delegation_id, "batch submit rollback", suppress_recovery=True
        )
        return {
            "status": "rejected",
            "error": (
                f"Failed to schedule async delegation batch ({type(exc).__name__})"
            ),
        }

    logger.info(
        "Dispatched async delegation batch %s (%d task(s), session_key=%s)",
        delegation_id, n, session_key or "<cli>",
    )
    return {"status": "dispatched", "delegation_id": delegation_id}


def _finalize_batch(
    delegation_id: str, combined: Dict[str, Any], status: str
) -> None:
    """Mark a batch record complete and push ONE combined completion event."""
    combined, status = _sanitize_completion_payload(combined, status)
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None:
            return
        record["status"] = "finalizing"
        record["completed_at"] = time.time()
        record["interrupt_fn"] = None
        event_record = dict(record)

    dispatched_at = event_record.get("dispatched_at") or time.time()
    completed_at = event_record.get("completed_at") or time.time()
    evt = {
        "type": "async_delegation",
        "delegation_id": delegation_id,
        "session_key": event_record.get("session_key", ""),
        "origin_ui_session_id": event_record.get("origin_ui_session_id", ""),
        "parent_session_id": event_record.get("parent_session_id"),
        "goal": event_record.get("goal", ""),
        "goals": event_record.get("goals"),
        "context": event_record.get("context"),
        "toolsets": event_record.get("toolsets"),
        "role": event_record.get("role"),
        "lane": event_record.get("lane"),
        "provider": combined.get("provider") or event_record.get("provider"),
        "model": combined.get("model") or event_record.get("model"),
        "status": status,
        "is_batch": True,
        # The full per-task results list — the formatter renders a
        # consolidated multi-task block from this.
        "results": combined.get("results") or [],
        # Per-task live transcript log paths (cache/delegation/live/...).
        # They persist after completion and double as the full-fidelity
        # operational record of each child's run.
        "live_transcripts": combined.get("live_transcripts"),
        "error": combined.get("error"),
        "exit_reason": combined.get("exit_reason"),
        "total_duration_seconds": combined.get("total_duration_seconds"),
        "dispatched_at": dispatched_at,
        "completed_at": completed_at,
    }
    durable_retained = True
    try:
        _persist_completion(evt, combined)
    except Exception as exc:  # noqa: BLE001 — queue delivery can still succeed
        durable_retained = False
        logger.error(
            "Async delegation batch %s: completion persistence failed (%s); "
            "delivering in-memory result",
            delegation_id,
            type(exc).__name__,
        )
        _best_effort_delete_durable(
            delegation_id, "batch completion persistence rollback"
        )
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s finished but process_registry import "
            "failed; %s: %s",
            delegation_id,
            "durable result retained" if durable_retained else "result unavailable",
            exc,
        )
        with _records_lock:
            record = _records.get(delegation_id)
            if record is not None:
                record["status"] = status
            _prune_completed_locked()
        return
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error(
            "Async delegation batch %s: failed to enqueue completion event; "
            "%s: %s",
            delegation_id,
            "durable result retained" if durable_retained else "result unavailable",
            exc,
        )
    finally:
        with _records_lock:
            record = _records.get(delegation_id)
            if record is not None:
                record["status"] = status
            _prune_completed_locked()


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed).

    Safe to call from any thread. Excludes the non-serialisable interrupt_fn.
    """
    with _records_lock:
        return [
            {k: v for k, v in r.items() if k != "interrupt_fn"}
            for r in _records.values()
        ]


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every running async delegation to stop. Returns how many.

    Used on ``/stop`` and gateway shutdown so a dangling background subagent
    can't keep burning tokens with no one listening. The child still emits a
    completion event (status='interrupted') via the normal finalize path.
    """
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values() if r.get("status") == "running"
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_all: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info("Interrupted %d async delegation(s) (%s)", count, reason)
    return count


def interrupt_for_session(
    session_key: str = "",
    origin_ui_session_id: str = "",
    parent_session_id: str = "",
    reason: str = "session_end",
) -> int:
    """Signal running async delegations owned by ONE session to stop.

    A delegation's lifecycle is bound to the session that spawned it: when
    that session ends, its in-flight background subagents must end with it —
    a completed orphan would otherwise sit on the shared completion queue
    with no live owner, either leaking into another chat or burning tokens
    with no one listening (#55578).

    Selectors (any matching field claims the record):
    - ``origin_ui_session_id``: the live TUI tab/window that commissioned it.
    - ``session_key``: the durable routing key captured at dispatch.
    - ``parent_session_id``: the spawning agent's durable session-db id —
      the right selector for gateway chats, whose ``session_key`` (the
      platform conversation key) SURVIVES a ``/new`` reset while the
      session id rotates.

    Returns how many were interrupted.
    """
    if not session_key and not origin_ui_session_id and not parent_session_id:
        return 0
    count = 0
    with _records_lock:
        targets = [
            r for r in _records.values()
            if r.get("status") == "running"
            and (
                (origin_ui_session_id and str(r.get("origin_ui_session_id") or "") == origin_ui_session_id)
                or (session_key and str(r.get("session_key") or "") == session_key)
                or (parent_session_id and str(r.get("parent_session_id") or "") == parent_session_id)
            )
        ]
    for r in targets:
        fn = r.get("interrupt_fn")
        if callable(fn):
            try:
                fn()
                count += 1
            except Exception as exc:
                logger.debug(
                    "interrupt_for_session: %s interrupt failed: %s",
                    r.get("delegation_id"), exc,
                )
    if count:
        logger.info(
            "Interrupted %d async delegation(s) for ending session (%s)",
            count, reason,
        )
    return count


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor."""
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    with _records_lock:
        _records.clear()
