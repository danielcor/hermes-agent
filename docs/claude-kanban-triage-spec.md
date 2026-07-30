# Claude-Driven Kanban Triage — Design

**Date:** 2026-07-29
**Status:** Approved design, pending implementation plan

## Problem

Hermes parks raw ideas in the kanban `triage` column. Two existing paths clear that column —
`hermes kanban specify` and `hermes kanban decompose` — and both delegate the thinking to an
auxiliary LLM. Neither produces a reviewed artifact: `specify` rewrites the title and body,
`decompose` fans out child tasks. For ideas that deserve a real spec, the output is thin.

We want Claude Code to own the triage column: read each idea, turn the proposal-shaped ones into
a validated OpenSpec change proposal, have Codex review it to zero findings, land it on the
board's base branch, then promote the task to `todo` carrying the evidence.

Claude Code is invoked by a human (`/hermes-triage`). Nothing runs Claude headlessly — headless
runs bill API tokens, which is explicitly out of scope.

## Non-goals

- No autonomous dispatch. The Hermes dispatcher never spawns Claude.
- No implementation code. The proposal is the deliverable; implementing it is the `todo` task.
- No cross-board sweep. One board per run.

## Board topology

Hermes kanban supports multiple boards. Each board is fully isolated: its own SQLite DB
(`~/.hermes/kanban/boards/<slug>/kanban.db`), its own workspaces directory, and its own
dispatcher loop. Tasks on one board cannot collide with tasks on another. The `default` board
always exists and cannot be removed.

**The convention here is one board per repository.** `pmzbot` is the first instance —
description "pmzbot repo work only", backed by the pmzbot repo at `/opt/dev/peakbot/pmzbot-dev`
on `dev`. Further repos get further boards.

Two consequences for this design:

1. **The board identifies the repo.** The skill never infers a target repository from task text;
   it resolves board → repo. A board is a repo scope, so a task on the `pmzbot` board is pmzbot
   work by construction.
2. **The skill is board-agnostic.** It runs against the active board
   (`hermes kanban boards show`) with a `--board <slug>` override, and takes worktree root, base
   branch, and proposal layout from that board's repo rather than hardcoding pmzbot paths.
   pmzbot is the first board to use it, not the only one it can serve.

The board → repo mapping comes from `hermes project show <slug>` (`primary:` folder), with
`hermes kanban boards set-default-workdir <slug> <path>` as the fallback when a board has no
matching project. A board that resolves to neither is a hard error — the skill refuses to guess.

## Components

Three pieces, each independently useful.

### 1. Hermes profile `claude-triage`

An identity, not a running worker.

```bash
hermes profile create claude-triage --clone \
  --description "Turns raw ideas into validated OpenSpec change proposals for the board's repo. Codex-reviewed to zero findings before promotion. Route idea-shaped, design-shaped, and speculative 'we should maybe' tasks here."
```

Three jobs:

1. The description feeds `kanban decompose` routing, so idea-shaped tasks land here by role
   rather than by name.
2. The assignee stamp gives the skill a work queue:
   `hermes kanban list --status triage --assignee claude-triage --json`.
3. `--author claude-triage` on audit comments distinguishes Claude's trail from the aux LLM's.

`--clone` copies `config.yaml` / `.env` / `SOUL.md` from the active profile so the profile *could*
run as a Hermes worker later without a rebuild. Nothing dispatches it today.

One profile serves every board — profiles are global to the Hermes home, boards are not.

### 2. CLI patch — manual specify

`kanban_db.specify_triage_task(conn, task_id, *, title, body, assignee, author)` already does
exactly what the skill needs: atomically sets title/body/assignee and flips
`status: triage -> todo` in one write transaction, recording an audit comment only when a field
actually changed. No CLI path reaches it without going through the auxiliary LLM.

Add a manual mode to the existing subcommand:

```bash
hermes kanban specify <task-id> --title "..." --body "..." [--assignee claude-triage]
```

Behavior:

- When `--title` or `--body` is present, skip `agent.auxiliary_client` entirely and call
  `specify_triage_task` directly with the supplied values.
- `--assignee` and `--author` compose with manual mode. `--author` keeps its existing default
  (`$HERMES_PROFILE` or `specifier`).
- Reject `--title`/`--body` combined with `--all` — a bulk sweep cannot share one body.
- A task not in `triage` returns the same `ok=False, "task is not in triage"` outcome the LLM
  path returns. Manual mode introduces no new error surface.
- `--json` emits the same `SpecifyOutcome` shape, so callers cannot tell the modes apart.
- Honors the existing `--board` selector on `hermes kanban`, like every other subcommand.

Touches `hermes_cli/kanban.py` (subparser + dispatch) and `hermes_cli/kanban_specify.py`
(a `specify_task_manual` sibling to `specify_task`). Roughly 40 lines plus tests.

This patch is a prerequisite: without it the skill cannot write its own spec into the task.

### 3. Skill `/hermes-triage`

Lives at `~/.claude/skills/hermes-triage/SKILL.md`. Runs against the active board; accepts
`--board <slug>` to override. Operates on the `triage` column.

#### Prerequisite, one time per repo

`pmzbot`'s `.gitignore:130` has `worktrees/`, which does **not** match `.worktrees/`. Add
`.worktrees/` and commit to `dev`, otherwise every task worktree shows up as untracked in the
parent worktree and pollutes `git status` and Codex's view of the tree. Each new board's repo
needs the same check.

#### Per-task flow

Below, `<repo>` is the board's resolved repo root and `<base>` its base branch — for the
`pmzbot` board, `/opt/dev/peakbot/pmzbot-dev` and `dev`.

| Step | Action |
|---|---|
| 1 | `hermes kanban list --status triage --json` on the selected board |
| 2 | Classify each task: proposal idea, or other (bug / question / chore / junk) |
| 3 | `git worktree add <repo>/.worktrees/<task-id> -b wt/<task-id> <base>` |
| 4 | Write `openspec/changes/<slug>/{proposal.md, tasks.md, specs/…}` in that worktree |
| 5 | `openspec validate <slug> --strict --json` — structural gate on `specs/` deltas only, fix until clean |
| 6 | Commit, scoped to `openspec/changes/<slug>` only |
| 7 | `codex exec review --commit <sha>` → fix findings → `git commit --amend`. Max 5 rounds |
| 8 | In `<repo>` on `<base>`: `git pull --rebase origin <base>`, merge `wt/<task-id>`, push `origin <base>`, `git worktree remove`, delete the branch |
| 9 | `hermes kanban specify <task-id> --title <refined> --body <spec> --assignee claude-triage` |

Non-proposal tasks run steps 2 and 9 only — refined title and body, flipped to `todo`, no
worktree and no Codex round.

`<task-id>` is the raw Hermes task id in both the worktree path and the branch name. No slug
suffix: exact correspondence to the board beats readability, and the proposal directory carries
the readable slug anyway. Task ids are unique within a board, and each board maps to a distinct
repo, so `wt/<task-id>` cannot collide across boards.

#### Step 9 body contents

The promoted task must stand alone for whoever implements it:

- The refined problem statement and scope
- Path to the landed proposal: `openspec/changes/<slug>/`
- The `<base>` commit SHA the proposal landed on
- Codex round count and a one-line summary of what the review changed
- Anything Claude deliberately left out of scope

Plus a `hermes kanban comment --author claude-triage` carrying the full review trail.

## Abort paths

Each leaves the task in `triage`, records why as a comment, and moves to the next task. A single
bad idea never halts the sweep.

| Condition | Left behind |
|---|---|
| Board resolves to no repo | Nothing done; the run stops before step 1 |
| 5 Codex rounds still dirty | Worktree and branch intact, open findings in the comment |
| `openspec validate` unfixable | Worktree and branch intact, validator output in the comment |
| `<repo>` dirty at merge time | Worktree and branch intact; merge is the human's call |
| `git push origin <base>` rejected | Merge commit local on `<base>`, worktree removed |

Rationale for the dirty-worktree abort: step 8 merges into the `<repo>` working tree. Git
refuses a merge that would overwrite local modifications, and forcing past that risks the
human's in-flight work. Detect it up front with `git status --porcelain` and stop cleanly.

## Testing

- **CLI patch:** unit tests for manual specify — happy path, `--all` rejection, task not in
  triage, assignee composition, `--json` shape parity with the LLM path, correct board when
  `--board` is passed. No network, no aux client, so these run in the normal suite.
- **Skill:** exercised against a scratch board with seeded triage tasks. Verify a proposal task
  produces a validated `openspec/changes/` directory on `<base>`, the worktree is gone, and the
  task is `todo` with the SHA in its body. Verify each abort path leaves its stated residue.
  Verify a second board with a different repo routes to that repo's paths.

## Open risks

- **Codex round ping-pong.** Subjective findings can survive five rounds. The cap converts that
  from a hang into a reported stall, but a chronically stalling proposal type needs its review
  prompt narrowed.
- **Proposal quality is unmeasured, and the validator is narrower than it looks.**
  `openspec validate <name> --strict` calls only `validateChangeDeltaSpecs` — verified in the
  installed validator at `dist/commands/validate.js:143-149`, where `proposal.md` is never
  opened. It structurally checks `specs/**/spec.md` deltas and nothing else, so a garbage
  `proposal.md` passes. Narrative quality rests entirely on Claude at write time and on the
  Codex loop; neither confirms the proposal solves the idea the human had. The `todo` handoff
  body is the human's checkpoint.
- **Board/repo drift.** Nothing enforces the one-board-per-repo convention; a board whose project
  `primary:` folder moves resolves to a stale path. The hard-error-on-unresolvable rule catches a
  missing folder, not a wrong one.
- **Concurrent runs.** Two `/hermes-triage` sessions on the same board would both claim the same
  triage tasks. Out of scope; run one at a time. Different boards are safe — separate DBs.
- **Non-OpenSpec repos.** Steps 4 and 5 assume the board's repo has an `openspec/` tree. A future
  board without one needs a different proposal layout, unspecified here.
