# Claude-Driven Kanban Triage — Design

**Date:** 2026-07-29
**Status:** Approved design, pending implementation plan

## Problem

Hermes parks raw ideas in the kanban `triage` column. Two existing paths clear that column —
`hermes kanban specify` and `hermes kanban decompose` — and both delegate the thinking to an
auxiliary LLM. Neither produces a reviewed artifact: `specify` rewrites the title and body,
`decompose` fans out child tasks. For ideas that deserve a real spec, the output is thin.

We want Claude Code to own the triage column for the `pmzbot` board: read each idea, turn the
proposal-shaped ones into a validated OpenSpec change proposal, have Codex review it to zero
findings, land it on `dev`, then promote the task to `todo` carrying the evidence.

Claude Code is invoked by a human (`/hermes-triage`). Nothing runs Claude headlessly — headless
runs bill API tokens, which is explicitly out of scope.

## Non-goals

- No autonomous dispatch. The Hermes dispatcher never spawns Claude.
- No implementation code. The proposal is the deliverable; implementing it is the `todo` task.
- No cross-board sweep. One board per run.

## Components

Three pieces, each independently useful.

### 1. Hermes profile `claude-triage`

An identity, not a running worker.

```bash
hermes profile create claude-triage --clone \
  --description "Turns raw ideas into validated OpenSpec change proposals for the pmzbot repo. Codex-reviewed to zero findings before promotion. Route idea-shaped, design-shaped, and speculative 'we should maybe' tasks here."
```

Three jobs:

1. The description feeds `kanban decompose` routing, so idea-shaped tasks land here by role
   rather than by name.
2. The assignee stamp gives the skill a work queue:
   `hermes kanban list --status triage --assignee claude-triage --json`.
3. `--author claude-triage` on audit comments distinguishes Claude's trail from the aux LLM's.

`--clone` copies `config.yaml` / `.env` / `SOUL.md` from the active profile so the profile *could*
run as a Hermes worker later without a rebuild. Nothing dispatches it today.

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

Touches `hermes_cli/kanban.py` (subparser + dispatch) and `hermes_cli/kanban_specify.py`
(a `specify_task_manual` sibling to `specify_task`). Roughly 40 lines plus tests.

This patch is a prerequisite: without it the skill cannot write its own spec into the task.

### 3. Skill `/hermes-triage`

Lives at `~/.claude/skills/hermes-triage/SKILL.md`. Reads the active board; accepts
`--board <slug>` to override. Operates on the `triage` column.

#### Prerequisite, one time

`pmzbot`'s `.gitignore:130` has `worktrees/`, which does **not** match `.worktrees/`. Add
`.worktrees/` and commit to `dev`, otherwise every task worktree shows up as untracked in the
parent worktree and pollutes `git status` and Codex's view of the tree.

#### Per-task flow

| Step | Action |
|---|---|
| 1 | `hermes kanban list --status triage --json` on the active board |
| 2 | Classify each task: proposal idea, or other (bug / question / chore / junk) |
| 3 | `git worktree add /opt/dev/peakbot/pmzbot-dev/.worktrees/<task-id> -b wt/<task-id> dev` |
| 4 | Write `openspec/changes/<slug>/{proposal.md, tasks.md, specs/…}` in that worktree |
| 5 | `openspec validate <slug> --strict --json` — hard gate, fix until clean |
| 6 | Commit, scoped to `openspec/changes/<slug>` only |
| 7 | `codex exec review --commit <sha>` → fix findings → `git commit --amend`. Max 5 rounds |
| 8 | In `pmzbot-dev` on `dev`: `git pull --rebase origin dev`, merge `wt/<task-id>`, push `origin dev`, `git worktree remove`, delete the branch |
| 9 | `hermes kanban specify <task-id> --title <refined> --body <spec> --assignee claude-triage` |

Non-proposal tasks run steps 2 and 9 only — refined title and body, flipped to `todo`, no
worktree and no Codex round.

`<task-id>` is the raw Hermes task id in both the worktree path and the branch name. No slug
suffix: exact correspondence to the board beats readability, and the proposal directory carries
the readable slug anyway.

#### Step 9 body contents

The promoted task must stand alone for whoever implements it:

- The refined problem statement and scope
- Path to the landed proposal: `openspec/changes/<slug>/`
- The `dev` commit SHA the proposal landed on
- Codex round count and a one-line summary of what the review changed
- Anything Claude deliberately left out of scope

Plus a `hermes kanban comment --author claude-triage` carrying the full review trail.

#### Repo resolution

The `pmzbot` board is scoped to the pmzbot repo (`primary: /opt/dev/peakbot/pmzbot-dev`, per
`hermes project show pmzbot`). The skill resolves the repo from the board, not from task text.
A task whose text clearly targets a different repo stops and asks rather than guessing.

## Abort paths

Each leaves the task in `triage`, records why as a comment, and moves to the next task. A single
bad idea never halts the sweep.

| Condition | Left behind |
|---|---|
| 5 Codex rounds still dirty | Worktree and branch intact, open findings in the comment |
| `openspec validate` unfixable | Worktree and branch intact, validator output in the comment |
| `pmzbot-dev` dirty at merge time | Worktree and branch intact; merge is the human's call |
| `git push origin dev` rejected | Merge commit local on `dev`, worktree removed |

Rationale for the dirty-worktree abort: step 8 merges into the `pmzbot-dev` working tree. Git
refuses a merge that would overwrite local modifications, and forcing past that risks the
human's in-flight work. Detect it up front with `git status --porcelain` and stop cleanly.

## Testing

- **CLI patch:** unit tests for manual specify — happy path, `--all` rejection, task not in
  triage, assignee composition, `--json` shape parity with the LLM path. No network, no aux
  client, so these run in the normal suite.
- **Skill:** exercised against a scratch board with seeded triage tasks. Verify a proposal task
  produces a validated `openspec/changes/` directory on `dev`, the worktree is gone, and the task
  is `todo` with the SHA in its body. Verify each abort path leaves its stated residue.

## Open risks

- **Codex round ping-pong.** Subjective findings can survive five rounds. The cap converts that
  from a hang into a reported stall, but a chronically stalling proposal type needs its review
  prompt narrowed.
- **Proposal quality is unmeasured.** `openspec validate --strict` checks structure, and Codex
  checks reasoning, but neither confirms the proposal solves the idea the human had. The `todo`
  handoff body is the human's checkpoint.
- **Concurrent runs.** Two `/hermes-triage` sessions on the same board would both claim the same
  triage tasks. Out of scope; run one at a time.
