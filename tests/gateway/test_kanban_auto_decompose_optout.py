"""Tests for the per-board ``auto_triage`` opt-out on the dispatcher's
embedded auto-decompose sweep (Task 9 of the kanban-triage plan).

Task 5 discovered that ``_auto_decompose_tick`` (nested inside
``_kanban_dispatcher_watcher``) sweeps every board unconditionally, so a
human-invoked triage skill always loses the race to claim a board's triage
column. Boards now carry an ``auto_triage`` flag (default ``True``); the
dispatcher must skip a board that has turned it off, while the manual
``hermes kanban decompose`` path (not exercised here) is unaffected.

``_auto_decompose_tick`` is a closure defined inline inside the async
``_kanban_dispatcher_watcher`` method and is not importable on its own, so
this test drives the whole watcher for exactly one tick — mirroring the
established pattern in ``test_kanban_notifier_watcher_dispatch_gate.py``
(fake ``asyncio.sleep``/``asyncio.to_thread`` to run one iteration
synchronously and then stop the loop).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch

import pytest

from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_decompose import DecomposeOutcome


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _make_runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    return runner


def _fake_config():
    return {
        "kanban": {
            "dispatch_in_gateway": True,
            # Small interval so the "sleep in 1s slices" trailing loop
            # needs exactly one asyncio.sleep call to complete a tick.
            "dispatch_interval_seconds": 1,
            "auto_decompose": True,
            "auto_decompose_per_tick": 10,
        }
    }


def test_auto_decompose_tick_skips_board_with_auto_triage_off(kanban_home):
    kb.create_board("on-board", name="On Board")
    kb.create_board("off-board", name="Off Board")
    kb.write_board_metadata("off-board", auto_triage=False)

    with kb.connect(board="on-board") as conn:
        on_tid = kb.create_task(conn, title="on task", triage=True)
    with kb.connect(board="off-board") as conn:
        off_tid = kb.create_task(conn, title="off task", triage=True)

    runner = _make_runner()
    sleep_calls = []

    async def fake_sleep(delay):
        sleep_calls.append(delay)
        # Initial 5s pre-loop delay is call #1; the trailing per-tick sleep
        # slice is call #2 — stop the loop right after the first full tick
        # body (which contains the auto-decompose call) has executed.
        if len(sleep_calls) >= 2:
            runner._running = False

    async def fake_to_thread(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    decompose_calls = []

    def fake_decompose_task(task_id, author=None):
        decompose_calls.append(task_id)
        return DecomposeOutcome(task_id=task_id, ok=True)

    with patch("hermes_cli.config.load_config", return_value=_fake_config()):
        # Isolate the assertion to the auto-decompose sweep: the regular
        # dispatch_once() fan-out is irrelevant to this test and our tasks
        # are in 'triage' status (not 'ready') so it would no-op anyway,
        # but stubbing it keeps the tick from touching real spawn logic.
        with patch("hermes_cli.kanban_db.dispatch_once", return_value=None):
            with patch(
                "hermes_cli.kanban_decompose.decompose_task",
                side_effect=fake_decompose_task,
            ):
                with patch("asyncio.sleep", side_effect=fake_sleep):
                    with patch("asyncio.to_thread", side_effect=fake_to_thread):
                        asyncio.run(runner._kanban_dispatcher_watcher())

    assert on_tid in decompose_calls
    assert off_tid not in decompose_calls
