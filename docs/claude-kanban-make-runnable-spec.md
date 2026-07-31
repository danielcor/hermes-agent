# Claude-Driven Kanban Make-Runnable — Design

**Date:** 2026-07-31
**Status:** Implemented. `/hermes-triage` applies the hold; `/hermes-make-runnable`
releases it. Mechanism verified empirically 2026-07-31 (see Verification).

## Problem

`/hermes-triage` promotes a task by calling `hermes kanban specify --assignee <implementer>`.
That call moves the task `triage → todo`, and because a freshly promoted task has no open
parents, `recompute_ready` runs in the same call and flips it straight to `ready`. A running
gateway claims any `ready` task carrying an assignee and spawns a worker within seconds.

So there is no gate between "Claude finished writing the spec" and "a worker is executing it".
Every precondition that has nothing to do with spec quality — a human step nobody performed, a
missing workspace, an endpoint contract still reading `unresolved` — is discovered only after a
worker has spawned, consumed its budget, and failed.

Board `ai-management` task `t_a3fc3ece` accumulated **four** crashed workers
(`worker exited cleanly (rc=0) without calling kanban_complete or kanban_block`) against a
blocker that was, in the end, a human running one interactive command.

## Non-goals

- **Auto-repairing blockers.** This design detects and reports; it does not edit profile config,
  refresh credentials, or mutate global state. Those are the changes most likely to spend money
  or alter routing for unrelated profiles.
- **Running Claude headlessly.** The release step is human-invoked, same constraint
  `/hermes-triage` operates under.
- **Replacing the dispatcher's guards.** The circuit breaker, claim TTL, and crash detection are
  untouched. This adds a gate *before* first dispatch.
- **A new Hermes CLI subcommand.** Everything below uses verbs that already exist.

## Choosing the hold

Three candidate mechanisms were evaluated by reading the dispatcher. Two fail.

**`todo` cannot hold.** `recompute_ready` promotes every parent-free `todo` to `ready` on the
next tick. Parking a task there delays dispatch by at most one tick.

**Withholding the assignee cannot hold.** An unassigned `ready` task looks held — the ready
dispatch loop buckets it as `skipped_unassigned`. But only after first falling back to
`kanban.default_assignee`: when that config key is set, the dispatcher auto-assigns and
dispatches. The hold would silently evaporate the day someone configures a default.

**`review` cannot hold either, and is actively dangerous.** It is a real status, but the
dispatcher spawns a *review agent* for any `review` task with an assignee, which merges to `done`
or kicks it back to `running`. Unassigned-in-`review` is skipped and would hold — but nothing in
the codebase ever writes `status = 'review'`. There is no `hermes kanban review` verb; the only
producer is the dashboard's drag-drop `PATCH /tasks/:id`. It is consumer-only from the CLI.

**Sticky `blocked` holds.** `_has_sticky_block` (`hermes_cli/kanban_db.py:3959`) returns true when
a task's most recent block/unblock event is `blocked`; `recompute_ready` skips those
unconditionally and names `unblock_task` as "the only legitimate exit". A circuit-breaker block is
not sticky — it emits `gave_up` and auto-recovers — so only an explicit block holds. `blocked` is
not in the dispatch pool at all, so `default_assignee` cannot release it.

| Step | Command |
|---|---|
| Hold | `hermes kanban --board <slug> block <task-id> "<preconditions>" --kind capability` |
| Release | `hermes kanban --board <slug> unblock <task-id> --reason "<evidence>"` |

## The kind discipline

`--kind capability` is reserved exclusively for this hold, and that reservation is what makes the
design safe.

`block_task` computes `same_cause = prev_kind == kind`, then
`recurrences = prev_recurrences + 1 if same_cause else 1`. At `BLOCK_RECURRENCE_LIMIT` (2) the
task is routed to **`triage`** instead of `blocked`, emitting `block_loop_detected` — a loop
breaker for the cron-unblock ↔ worker-re-block cycle. `unblock_task` deliberately does *not*
reset the counter; the comment at `kanban_db.py:5807` calls that reset "exactly the amnesia" the
breaker exists to prevent.

This fired in production on `t_a3fc3ece`: it blocked `needs_input` (awaiting a human), was
unblocked, did its work, then blocked `needs_input` again (awaiting review) — two *different*
causes sharing one kind — and was bounced to `triage` mid-flight.

Because a differing kind resets the counter to 1, holding on `capability` while workers signal
with `needs_input` means the two never accumulate against each other. Reusing `needs_input` for
the hold would reintroduce the bug.

## Architecture

### 1. `/hermes-triage` — apply the hold

Both promotion lanes follow `specify` immediately with the `block` above. The junk lane is
untouched: it archives, and an archived task never had an assignee to dispatch.

The hold reason is the deliverable — an explicit checklist of what must be true before the task
runs, written by the session that just wrote the spec and therefore knows what it assumed. A hold
reading "blocked" is a failure of this step.

There is a live race between `specify` and `block`: the task is in `ready` in between. Run them
back to back.

### 2. `/hermes-make-runnable` — release

Selects `blocked` tasks whose `block_kind` is `capability` (a worker's `needs_input` block is
someone else's handoff, not ours to release), runs the preconditions, then unblocks or leaves the
task held with a comment naming what is outstanding. Never partially releases.

## Preconditions

Each drawn from something that actually failed:

1. **Assignee set and the profile exists** — `specify` accepts an unknown assignee silently;
   `_default_spawn` raises on a missing one.
2. **Board resolves to a real repo** — a directory containing `.git`.
3. **Workspace resolves** — `_default_spawn` pins `TERMINAL_CWD` only for an absolute existing
   directory; otherwise the worker writes files somewhere unrelated.
4. **Human prerequisites in the hold reason confirmed done** — judgment, not parsing. See Known
   weakness.
5. **Spec contract resolved** — no `unresolved` marker, `openspec validate --strict` clean.
6. **`consecutive_failures` below the effective limit** — otherwise `recompute_ready` refuses and
   the unblock silently does nothing.
7. **A gateway is running** — otherwise releasing produces a `ready` task and no dispatch, which
   reads as a hang.

**Fail closed.** A check that cannot be *run* is a failure, never a pass.

## Operational trap

`reason` is a positional with `nargs="*"`, so it must precede `--kind`. The natural-looking
`block <id> --kind capability "<reason>"` fails with
`hermes: error: unrecognized arguments: <reason>` — and the wrapper still exits `0`. A caller that
ignores stderr records a hold that never applied and leaves the task in `ready`. Always confirm
with `show <task-id>` that the status is really `blocked`.

## Verification

Run 2026-07-31 against a scratch task on the empty `default` board (which maps to
`~/.hermes/kanban.db`), archived afterward:

| Step | Observed |
|---|---|
| `block … --kind capability` from `ready` | `status=blocked kind=capability rec=1` |
| `unblock` | `status=ready` (kind persists, rec stays 1) |
| `block … --kind needs_input` (different kind) | `status=blocked kind=needs_input rec=1` — counter reset |
| `block … --kind needs_input` again (same kind) | `status=triage kind=needs_input rec=2` |

The last row reproduces the `t_a3fc3ece` incident exactly, and the third confirms the kind
discipline: a worker's handoff after a `capability` hold lands in `blocked`, not `triage`.

## Known weakness

Precondition 4 is judgment, not parsing. The hold reason is prose, so a skill reading "requires a
human to run `agy`" cannot verify that a human did. The release step asks the operator explicitly
and records the answer as the unblock reason, which lands in the task's event history as evidence.

Automating it means giving the hold reason structure — a machine-readable prerequisite block
rather than a paragraph. Deliberately deferred: writing that schema now would be guessing at the
shape of a thing we have three examples of.

## Later: promoting the checks into the CLI

Once the check list has proven itself, port it to `hermes kanban preflight <task-id> [--release]`.
That version is invocable by cron and by the gateway itself, which is the actual end state — a
human-invoked skill cannot be automated. The check list transfers unchanged; this design exists to
find out which checks matter before paying for a fork change, a build, and a gateway restart.
