# Claude-Driven Kanban Make-Runnable — Design

**Date:** 2026-07-31
**Status:** Proposed design, pending approval

## Problem

`/hermes-triage` promotes a task by calling `hermes kanban specify --assignee <implementer>`.
That call moves the task `triage → todo`, and because a freshly promoted task has no open
parents, `recompute_ready` runs in the same call and flips it straight to `ready`. A running
gateway claims any `ready` task carrying an assignee and spawns a worker within seconds.

So there is no gate between "Claude finished writing the spec" and "a worker is executing it".
Every precondition that has nothing to do with spec quality — a human step nobody has performed,
a missing workspace, an endpoint contract still reading `unresolved` — is discovered only after a
worker has spawned, consumed its budget, and failed.

This is not hypothetical. Board `ai-management` task `t_a3fc3ece` accumulated **four** crashed
workers (`worker exited cleanly (rc=0) without calling kanban_complete or kanban_block`) against a
blocker that was, in the end, a human running one interactive command. Four dispatches bought
nothing that reading the task body would not have said for free.

We want promotion to park the task, and a separate explicit step — `/hermes-make-runnable` — to
verify preconditions and release it.

## Non-goals

- **Auto-repairing blockers.** This design detects and reports; it does not edit profile config,
  refresh credentials, or mutate global state to make a task runnable. Those are the changes most
  likely to spend money or alter routing for unrelated profiles.
- **Running Claude headlessly.** Same constraint `/hermes-triage` operates under. The release step
  is human-invoked.
- **Replacing the dispatcher's own guards.** The circuit breaker, claim TTL, and crash detection
  stay exactly as they are. This adds a gate *before* first dispatch, not a replacement for the
  ones after it.
- **A new Hermes CLI subcommand.** Deliberately deferred — see Later.

## The holding mechanic

Establishing which column can actually hold a task required reading the dispatcher, because the
obvious answer is wrong.

**`todo` cannot hold.** `recompute_ready` (`hermes_cli/kanban_db.py`) promotes every parent-free
`todo` to `ready` on the next tick. A task parked in `todo` dispatches anyway; parking it there
just delays the problem by up to one tick.

**Sticky `blocked` holds, permanently.** `_has_sticky_block` (`hermes_cli/kanban_db.py:3959`)
reads the most recent `blocked` / `unblocked` event for a task and returns true when the latest is
`blocked`. `recompute_ready` skips those unconditionally, and its own comment names
`unblock_task` as "the only legitimate exit". A circuit-breaker block is *not* sticky — it emits
`gave_up` rather than `blocked`, and is designed to auto-recover — so only an explicit block holds.

That gives us both halves for free, with no code change:

| Step | Command |
|---|---|
| Hold | `hermes kanban --board <slug> block <task-id> --kind <kind> "<reason>"` |
| Release | `hermes kanban --board <slug> unblock <task-id> --reason "<evidence>"` |

`block` already takes `--kind {capability,dependency,needs_input,transient}`, which is a usable
taxonomy for *why* a task is held rather than a free-text guess.

## Architecture

Two components, each independently understandable and separately testable.

### 1. Triage skill change (hold on promote)

In both promotion lanes of `~/.claude/skills/hermes-triage/SKILL.md`, the `specify` call is
followed immediately by a `block` call. The task never sits in `ready` unattended.

The hold reason is the deliverable of this step: an explicit checklist of what must be true before
this task should run, written by the Claude session that just wrote the spec and therefore knows
what it assumed. A hold reason of "blocked" is a failure of this design; "requires §1.1 interactive
`agy` token refresh by a human; Resolved endpoint table must have no `unresolved` row" is the
product.

The junk lane is untouched — it archives, and an archived task never had an assignee to dispatch.

### 2. `/hermes-make-runnable` skill (release)

Input: a board slug, optionally specific task ids. For each sticky-blocked task on that board it
runs the precondition checks below, then either releases it or leaves it held with a comment
naming precisely what is still outstanding.

It never partially releases: a task is either runnable and unblocked, or held with a reason.

## Precondition checks

Version 1, each drawn from something that has actually failed rather than from imagination:

1. **Assignee set, and that profile exists** — `hermes profile list`. `_default_spawn` raises on a
   missing assignee, and `specify` accepts an unknown assignee without complaint, which strands the
   task in `ready` under a worker identity no gateway will claim.
2. **Board resolves to a real repo** — an existing directory containing `.git`, via
   `boards list --json` → `default_workdir`.
3. **Workspace path exists** — `_default_spawn` only pins `TERMINAL_CWD` when the workspace is an
   absolute existing directory; otherwise the worker silently anchors elsewhere.
4. **Every human prerequisite in the hold reason is confirmed done** — see Known weakness.
5. **Spec contract resolved** — if the task body names an `openspec/changes/<slug>/` directory,
   that proposal contains no `unresolved` marker, and `openspec validate <slug> --strict` passes.
6. **`consecutive_failures` below the effective failure limit** — otherwise `recompute_ready`
   refuses to promote and the unblock silently does nothing.
7. **A gateway is running** — with none up, unblocking produces a `ready` task and no dispatch,
   which reads as a hang.

**Fail closed.** A check that cannot be *run* — missing command, unreadable repo, ambiguous body —
is a failure, never a pass. The cost of a wrong hold is a human re-running one command; the cost of
a wrong release is another crashed worker, and we have four of those on the board already.

## Interfaces and dependencies

`/hermes-make-runnable` shells out to existing commands only: `kanban show`, `kanban list --json`,
`kanban boards list --json`, `kanban block`, `kanban unblock`, `kanban comment`, `profile list`,
and `openspec validate`. No Python change, no build, no gateway restart, and nothing that has to
land on a branch before it can be used.

## Later: promoting the checks into the CLI

Once the check list has proven itself against real tasks, port it to
`hermes kanban preflight <task-id> [--release]` in `hermes_cli/kanban.py`. That version is
invocable by cron and by the gateway itself, which is the actual end state — a human-invoked skill
cannot be automated. The check list is the hard part and it transfers unchanged; this design exists
to find out which checks matter before paying for a fork change, a build, and a gateway restart.

## Known weakness

Check 4 is judgment, not parsing. The hold reason is prose, so a skill reading "requires a human to
run `agy`" cannot verify that a human did. Version 1 handles this by requiring the operator to
confirm it explicitly at release time — the skill asks, and records the answer as the unblock
reason, which lands in the task's event history as evidence.

Automating check 4 means giving the hold reason structure (a machine-readable prerequisite block
rather than a paragraph). That is deliberately deferred: writing that schema before we have seen a
dozen real hold reasons would be guessing at the shape of a thing we have three examples of.

## Testing

- **Replay:** run the check list against `t_a3fc3ece` as it stood before 2026-07-31. Checks 4 and 5
  must both fail — that task was held on an unperformed human step *and* an `unresolved` contract
  table. A check list that would have released it is wrong.
- **Release path:** the same task as it stands now must pass all seven and release cleanly.
- **Fail-closed:** with `openspec` renamed out of `PATH`, check 5 must fail rather than skip.
- **Idempotence:** running the skill twice against an already-released task must be a no-op, not a
  second unblock event.
