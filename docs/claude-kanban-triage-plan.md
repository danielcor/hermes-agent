# Claude-Driven Kanban Triage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a human-invoked Claude Code skill clear the Hermes kanban `triage` column by turning proposal-shaped ideas into Codex-reviewed OpenSpec proposals and promoting every task to `todo`.

**Architecture:** Three independent pieces. A `hermes kanban specify --title/--body` manual mode gives Claude a way to write its own spec into a task without the auxiliary LLM. A `claude-triage` Hermes profile provides the routing description and assignee queue. A `~/.claude/skills/hermes-triage/SKILL.md` skill drives the loop: resolve board → repo, classify each triage task, and for proposal ideas produce a validated OpenSpec change in a per-task git worktree, review it with Codex to zero findings, merge to the board's base branch, and promote.

**Tech Stack:** Python 3 / argparse / pytest (Hermes CLI), SQLite (kanban DB), git worktrees, `openspec` CLI, `codex exec review`, Claude Code skill markdown.

**Spec:** `docs/claude-kanban-triage-spec.md` in this repo.

## Global Constraints

- Claude is never run headlessly. No `claude -p` anywhere in this work — headless runs bill API tokens.
- Hermes work happens in the worktree `/opt/dev/danielcor/hermes-kanban-specify` on branch `feat/kanban-manual-specify`. Do not edit `~/.hermes/hermes-agent`.
- Codex review loop cap is exactly 5 rounds. On the 6th dirty state, abort.
- Worktree path is `<repo>/.worktrees/<task-id>`; branch is `wt/<task-id>`. `<task-id>` is the raw Hermes task id — no slug suffix.
- Every abort path leaves the task in `triage`, records the reason as a `hermes kanban comment --author claude-triage`, and continues to the next task.
- The skill resolves the target repo from the board, never from task text. A board that resolves to no repo is a hard error.
- The skill hardcodes no pmzbot paths. `<repo>` and `<base>` are resolved at runtime.
- Manual specify must produce the same `SpecifyOutcome` shape as the LLM path so callers cannot tell the modes apart.
- `docs/superpowers/**` is gitignored in this repo (`.gitignore:134`). Plans and specs go directly in `docs/`.

---

### Task 1: Manual specify — module function

**Files:**
- Modify: `hermes_cli/kanban_specify.py` (append after `specify_task`, before `list_triage_ids`)
- Test: `tests/hermes_cli/test_kanban_specify.py` (append)

**Interfaces:**
- Consumes: `kanban_db.specify_triage_task(conn, task_id, *, title, body, assignee, author) -> bool` (exists at `hermes_cli/kanban_db.py:5823`); `SpecifyOutcome(task_id, ok, reason="", new_title=None)`; `_profile_author() -> str`; `kb.get_task`, `kb.connect_closing`.
- Produces: `specify_task_manual(task_id, *, title=None, body=None, assignee=None, author=None) -> SpecifyOutcome` — consumed by Task 2's CLI wiring.

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_kanban_specify.py`:

```python
# ---------------------------------------------------------------------------
# Manual specify — caller supplies the spec, no auxiliary LLM
# ---------------------------------------------------------------------------

def test_manual_specify_sets_title_body_assignee_and_promotes(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)

    out = spec.specify_task_manual(
        tid,
        title="clean title",
        body="clean body",
        assignee="claude-triage",
    )

    assert out.ok is True
    assert out.new_title == "clean title"
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "todo"
    assert task.title == "clean title"
    assert task.body == "clean body"
    assert task.assignee == "claude-triage"


def test_manual_specify_never_calls_the_aux_llm(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)

    with patch("agent.auxiliary_client.call_llm") as call_llm:
        out = spec.specify_task_manual(tid, body="body only")

    assert out.ok is True
    call_llm.assert_not_called()


def test_manual_specify_body_only_leaves_title_untouched(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="original", triage=True)

    out = spec.specify_task_manual(tid, body="body only")

    assert out.ok is True
    assert out.new_title is None
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.title == "original"
    assert task.body == "body only"


def test_manual_specify_requires_title_or_body(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)

    out = spec.specify_task_manual(tid)

    assert out.ok is False
    assert "needs --title or --body" in out.reason
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_manual_specify_rejects_blank_title(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)

    out = spec.specify_task_manual(tid, title="   ", body="body")

    assert out.ok is False
    assert "title cannot be blank" in out.reason
    with kb.connect() as conn:
        assert kb.get_task(conn, tid).status == "triage"


def test_manual_specify_unknown_task_id(kanban_home):
    out = spec.specify_task_manual("t_does_not_exist", body="body")
    assert out.ok is False
    assert out.reason == "unknown task id"


def test_manual_specify_non_triage_reason_matches_llm_path(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already todo")

    manual = spec.specify_task_manual(tid, body="body")

    assert manual.ok is False
    assert "task is not in triage" in manual.reason
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify && python -m pytest tests/hermes_cli/test_kanban_specify.py -k manual -v
```

Expected: 7 failures, `AttributeError: module 'hermes_cli.kanban_specify' has no attribute 'specify_task_manual'`.

- [ ] **Step 3: Implement `specify_task_manual`**

In `hermes_cli/kanban_specify.py`, insert between the end of `specify_task` and `def list_triage_ids`:

```python
def specify_task_manual(
    task_id: str,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    assignee: Optional[str] = None,
    author: Optional[str] = None,
) -> SpecifyOutcome:
    """Promote a triage task to ``todo`` using caller-supplied text.

    Same contract as :func:`specify_task` — identical ``SpecifyOutcome``
    shape, same "expected failures return ok=False rather than raise"
    tolerance — but the auxiliary LLM is never touched. The caller (a
    human, or Claude Code driving ``/hermes-triage``) already wrote the
    spec, so there is nothing to generate.

    ``title=None`` leaves the existing title alone; same for ``body``.
    At least one of the two must be supplied — a call that changes
    neither would be a bare status flip, which ``promote`` already does.
    """
    if title is None and body is None:
        return SpecifyOutcome(
            task_id, False, "manual specify needs --title or --body"
        )
    if title is not None and not title.strip():
        # specify_triage_task raises on a blank title; catch it here so
        # the caller gets an outcome instead of a traceback.
        return SpecifyOutcome(task_id, False, "title cannot be blank")

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None:
        return SpecifyOutcome(task_id, False, "unknown task id")
    if task.status != "triage":
        return SpecifyOutcome(
            task_id, False, f"task is not in triage (status={task.status!r})"
        )

    with kb.connect_closing() as conn:
        ok = kb.specify_triage_task(
            conn,
            task_id,
            title=title,
            body=body,
            assignee=assignee,
            author=author or _profile_author(),
        )
    if not ok:
        # Race: promoted or archived between the read above and this write.
        return SpecifyOutcome(
            task_id, False, "task moved out of triage before promotion"
        )
    return SpecifyOutcome(task_id, True, "specified", new_title=title)
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify && python -m pytest tests/hermes_cli/test_kanban_specify.py -v
```

Expected: all pass, including the pre-existing LLM-path tests.

- [ ] **Step 5: Commit**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify
git add hermes_cli/kanban_specify.py tests/hermes_cli/test_kanban_specify.py
git commit -m "feat(kanban): add specify_task_manual for LLM-free triage promotion

Callers that already have a spec — a human, or Claude Code driving the
triage column — can now flip triage -> todo with their own title and
body instead of paying an auxiliary LLM to invent one.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: Manual specify — CLI surface

**Files:**
- Modify: `hermes_cli/kanban.py:853-887` (the `p_specify` subparser) and `hermes_cli/kanban.py:2869-2938` (`_cmd_specify`)
- Test: `tests/hermes_cli/test_kanban_specify.py` (append)

**Interfaces:**
- Consumes: `specify_task_manual` from Task 1; the existing `_run_cli(*argv)` helper at `tests/hermes_cli/test_kanban_specify.py:218`.
- Produces: `hermes kanban specify <id> --title T --body B [--assignee A]` — consumed by the skill in Tasks 5-7.

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_kanban_specify.py`:

```python
def test_cli_manual_specify_promotes_without_aux_llm(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)

    with patch("agent.auxiliary_client.call_llm") as call_llm:
        rc = _run_cli(
            "specify", tid,
            "--title", "clean title",
            "--body", "clean body",
            "--assignee", "claude-triage",
        )

    assert rc == 0
    call_llm.assert_not_called()
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task.status == "todo"
    assert task.title == "clean title"
    assert task.assignee == "claude-triage"


def test_cli_manual_specify_rejects_all(kanban_home, capsys):
    rc = _run_cli("specify", "--all", "--body", "shared body")
    assert rc == 2
    assert "cannot be combined with --all" in capsys.readouterr().err


def test_cli_assignee_requires_manual_mode(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)
    rc = _run_cli("specify", tid, "--assignee", "claude-triage")
    assert rc == 2
    assert "--assignee requires --title or --body" in capsys.readouterr().err


def test_cli_manual_specify_json_shape_matches_llm_path(kanban_home, capsys):
    with kb.connect() as conn:
        manual_id = kb.create_task(conn, title="m", triage=True)
        llm_id = kb.create_task(conn, title="l", triage=True)

    rc = _run_cli("specify", manual_id, "--body", "manual body", "--json")
    assert rc == 0
    manual_payload = jsonlib.loads(capsys.readouterr().out.strip())

    p, _ = _patch_aux_client(jsonlib.dumps({"title": "t", "body": "b"}))
    with p:
        rc = _run_cli("specify", llm_id, "--json")
    assert rc == 0
    llm_payload = jsonlib.loads(capsys.readouterr().out.strip())

    assert manual_payload.keys() == llm_payload.keys()
    assert manual_payload["ok"] is True
    assert manual_payload["task_id"] == manual_id


def test_cli_manual_specify_author_recorded(kanban_home, capsys):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)

    rc = _run_cli(
        "specify", tid, "--body", "b", "--author", "claude-triage"
    )

    assert rc == 0
    with kb.connect() as conn:
        comments = kb.list_comments(conn, tid)
    assert any(c.author == "claude-triage" for c in comments)
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify && python -m pytest tests/hermes_cli/test_kanban_specify.py -k "cli_manual or assignee_requires" -v
```

Expected: failures on `unrecognized arguments: --title` / `--assignee`.

If `test_cli_manual_specify_author_recorded` fails because `kb.list_comments` does not exist under that name, find the real accessor with `grep -n "def list_comments\|def get_comments" hermes_cli/kanban_db.py` and use it. Do not delete the assertion — the audit comment is the whole point of passing `--author`.

- [ ] **Step 3: Add the argparse flags**

In `hermes_cli/kanban.py`, immediately after the `p_specify.add_argument("--json", …)` block (around line 883), add:

```python
    p_specify.add_argument(
        "--title",
        default=None,
        help="Manual mode: set this exact title and skip the auxiliary "
             "LLM entirely. Pair with --body.",
    )
    p_specify.add_argument(
        "--body",
        default=None,
        help="Manual mode: set this exact body and skip the auxiliary LLM.",
    )
    p_specify.add_argument(
        "--assignee",
        default=None,
        help="Assign the task while promoting it (manual mode only)",
    )
```

Then update the subparser's own `help=` string (line 855-859) so `hermes kanban --help` advertises the mode:

```python
    p_specify = sub.add_parser(
        "specify",
        help="Flesh out a triage-column task into a concrete spec "
             "(title + body) and promote it to todo. Uses the auxiliary "
             "LLM configured under auxiliary.triage_specifier, or your "
             "own text with --title/--body.",
    )
```

- [ ] **Step 4: Route `_cmd_specify` to manual mode**

In `hermes_cli/kanban.py`, inside `_cmd_specify`, insert this block directly after the existing `if args.task_id and all_flag:` guard returns:

```python
    manual_title = getattr(args, "title", None)
    manual_body = getattr(args, "body", None)
    manual_assignee = getattr(args, "assignee", None)
    manual = manual_title is not None or manual_body is not None

    if manual and all_flag:
        print(
            "kanban: --title/--body cannot be combined with --all "
            "(a sweep cannot share one body)",
            file=sys.stderr,
        )
        return 2
    if manual_assignee is not None and not manual:
        print(
            "kanban: --assignee requires --title or --body",
            file=sys.stderr,
        )
        return 2
```

Then replace the single call inside the `for tid in ids:` loop:

```python
        if manual:
            outcome = spec.specify_task_manual(
                tid,
                title=manual_title,
                body=manual_body,
                assignee=manual_assignee,
                author=author,
            )
        else:
            outcome = spec.specify_task(tid, author=author)
```

- [ ] **Step 5: Run the full specify suite**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify && python -m pytest tests/hermes_cli/test_kanban_specify.py tests/hermes_cli/test_kanban_cli.py -v
```

Expected: all pass. `test_kanban_cli.py` is included because it asserts on the subcommand help surface.

- [ ] **Step 6: Verify the real CLI end to end**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify && python cli.py kanban specify --help
```

Expected: `--title`, `--body`, `--assignee` all listed.

Board routing needs no code here — `--board` is a global on `hermes kanban`, and
manual mode inherits it unchanged. It is covered end to end in Task 5 Step 4,
which promotes a task with `--board triage-test` against a second board.

- [ ] **Step 7: Commit**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify
git add hermes_cli/kanban.py tests/hermes_cli/test_kanban_specify.py
git commit -m "feat(kanban): wire --title/--body/--assignee into specify

Manual mode on the existing subcommand. Presence of --title or --body
skips the auxiliary LLM; --all is rejected in that mode because a sweep
cannot share one body. JSON output shape is unchanged.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: `claude-triage` profile

**Files:**
- Creates: `~/.hermes/profiles/claude-triage/` (via CLI, not by hand)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: profile name `claude-triage`, used as `--assignee` and `--author` by the skill in Tasks 5-7.

- [ ] **Step 1: Create the profile**

```bash
hermes profile create claude-triage --clone --description "Turns raw ideas into validated OpenSpec change proposals for the board's repo. Codex-reviewed to zero findings before promotion. Route idea-shaped, design-shaped, and speculative 'we should maybe' tasks here."
```

`--clone` copies `config.yaml` / `.env` / `SOUL.md` from the active profile so the profile could run as a Hermes worker later. Nothing dispatches it today.

- [ ] **Step 2: Verify it exists and carries the description**

```bash
hermes profile list && hermes profile describe claude-triage
```

Expected: `claude-triage` in the list; the description printed back verbatim.

- [ ] **Step 3: Verify the assignee queue query works**

```bash
hermes kanban --board pmzbot list --status triage --assignee claude-triage --json
```

Expected: `[]` — the board is empty. A non-zero exit or an "unknown assignee" error means the profile is not visible to the board; re-check step 1.

No commit — profiles live outside any repo.

---

### Task 4: `.worktrees/` gitignore fix in pmzbot

**Files:**
- Modify: `/opt/dev/peakbot/pmzbot-dev/.gitignore:130`

**Interfaces:**
- Consumes: nothing.
- Produces: a clean `git status` in pmzbot-dev once Task 6 starts creating `.worktrees/<task-id>`.

- [ ] **Step 1: Confirm the gap**

```bash
cd /opt/dev/peakbot/pmzbot-dev && mkdir -p .worktrees && git status --porcelain | grep worktrees; rmdir .worktrees
```

Expected: `.worktrees/` shows as untracked. Line 130's `worktrees/` pattern does not match a leading-dot directory.

- [ ] **Step 2: Confirm the tree is clean enough to commit into**

```bash
cd /opt/dev/peakbot/pmzbot-dev && git status --short --branch
```

If there are unrelated staged changes, stop and ask the human — do not sweep their work into this commit.

- [ ] **Step 3: Add the pattern**

Edit `/opt/dev/peakbot/pmzbot-dev/.gitignore`, changing line 130 from:

```
worktrees/
```

to:

```
worktrees/
.worktrees/
```

- [ ] **Step 4: Verify**

```bash
cd /opt/dev/peakbot/pmzbot-dev && mkdir -p .worktrees && git status --porcelain | grep -c worktrees; rmdir .worktrees
```

Expected: `0` — `.gitignore` itself will show as modified, but no `.worktrees` entry.

- [ ] **Step 5: Commit and push**

```bash
cd /opt/dev/peakbot/pmzbot-dev
git add .gitignore
git commit -m "chore: ignore .worktrees/ for per-task kanban worktrees"
git pull --rebase origin dev && git push origin dev
```

---

### Task 5: Skill scaffold — board resolution and the non-proposal lane

**Files:**
- Create: `~/.claude/skills/hermes-triage/SKILL.md`
- Create (test fixture, throwaway): `/tmp/hermes-triage-fixture/repo`

**Interfaces:**
- Consumes: `hermes kanban specify --title/--body/--assignee` (Task 2); profile `claude-triage` (Task 3).
- Produces: the skill file that Tasks 6 and 7 extend. Section headings created here and appended to later: `## Resolve the board`, `## Classify`, `## Non-proposal lane`.

- [ ] **Step 1: Build the test fixture**

```bash
mkdir -p /tmp/hermes-triage-fixture/repo && cd /tmp/hermes-triage-fixture/repo
git init -q -b dev && openspec init . && git add -A
git -c user.email=t@t -c user.name=t commit -q -m "chore: openspec init"
hermes kanban boards create triage-test --name "Triage Test" --default-workdir /tmp/hermes-triage-fixture/repo
```

Expected: board `triage-test` created. Verify with `hermes kanban boards show` after `hermes kanban boards switch triage-test`, then switch back with `hermes kanban boards switch pmzbot`.

- [ ] **Step 2: Write the skill file**

Create `~/.claude/skills/hermes-triage/SKILL.md`:

```markdown
---
name: hermes-triage
description: Clear the Hermes kanban triage column. Use when asked to triage the Hermes board, process new triage tasks, turn triage ideas into OpenSpec proposals, or when the user says "triage the board" or invokes /hermes-triage. Classifies each triage task, writes and Codex-reviews an OpenSpec change proposal for proposal-shaped ideas, and promotes every task to todo.
---

# Hermes Triage

Clear the `triage` column of a Hermes kanban board. Proposal-shaped ideas become
validated, Codex-reviewed OpenSpec change proposals landed on the board's base
branch. Everything else gets a real spec and moves on.

Never run Claude headlessly as part of this skill. No `claude -p`.

## Resolve the board

1. Determine the board. Use `--board <slug>` from the invocation if given,
   otherwise the active board:

   ```bash
   hermes kanban boards show
   ```

2. Resolve that board to a repository. **One board is one repository** — never
   infer the target repo from a task's text. Try in order:

   ```bash
   hermes project show <board-slug>          # use the `primary:` folder
   hermes kanban boards show                 # fall back to the board's default workdir
   ```

3. If neither yields an existing directory containing a `.git` entry, **stop the
   whole run** and tell the user the board resolves to no repo. Do not guess.

4. Record two values for the rest of the run:
   - `<repo>` — the resolved repository root
   - `<base>` — that repo's current branch, from `git -C <repo> branch --show-current`

5. Confirm the repo is usable:

   ```bash
   git -C <repo> status --porcelain
   ```

   Uncommitted changes are tolerated for now — they only block the merge step.
   Note them so the report at the end is honest.

## Classify

List the column:

```bash
hermes kanban --board <slug> list --status triage --json
```

Read each task's title and body. Sort into exactly two buckets:

- **Proposal idea** — a change to how the system works that needs design before
  implementation. Phrasings like "we should", "what if", "it'd be better if",
  a named feature with no stated mechanism, or anything whose scope is unclear
  until someone thinks it through.
- **Other** — bug reports with a concrete symptom, direct questions, mechanical
  chores, and junk or duplicates.

Announce the classification for the whole column before doing any work, so the
user can object before commits happen.

Process tasks one at a time, in the order returned. An abort on one task never
stops the run — record it and move to the next.

## Non-proposal lane

No worktree, no Codex, no proposal directory. Two steps:

1. Write a real title and body. The body must state the observed problem, the
   expected behavior, and how someone would verify a fix. If the task is junk or
   a duplicate, say so plainly and name the task it duplicates.

2. Promote:

   ```bash
   hermes kanban --board <slug> specify <task-id> \
     --title "<refined title>" \
     --body "<refined body>" \
     --assignee claude-triage \
     --author claude-triage
   ```

Verify with `hermes kanban --board <slug> show <task-id>` that the status is
`todo` and the body is what you wrote.

## Report

At the end of the run, print one line per task: id, bucket, outcome, and for
aborts the reason. Then state plainly which tasks remain in `triage`.
```

- [ ] **Step 3: Verify the skill loads**

Start a fresh Claude Code session in any directory and confirm `hermes-triage` appears in the available skills list with the description above. A malformed frontmatter block makes the skill silently absent.

- [ ] **Step 4: Exercise the non-proposal lane against the fixture**

```bash
hermes kanban --board triage-test create "app crashes when the config file is missing" \
  --body "no stack trace, just exits 1" --triage
hermes kanban --board triage-test list --status triage --json
```

Then invoke `/hermes-triage --board triage-test` and let it run. Expected: the task classified as **other**, promoted to `todo` with a body naming the symptom, expected behavior, and a verification step. Confirm:

```bash
hermes kanban --board triage-test show <task-id>
```

Expected: `status: todo`, `assignee: claude-triage`, and no `.worktrees` directory created in the fixture repo.

- [ ] **Step 5: Commit the skill**

`~/.claude/skills/` is not a git repository. Instead, verify the file is in place and record it in the plan's progress notes:

```bash
ls -la ~/.claude/skills/hermes-triage/SKILL.md && head -4 ~/.claude/skills/hermes-triage/SKILL.md
```

---

### Task 6: Skill — proposal lane through the validate gate

**Files:**
- Modify: `~/.claude/skills/hermes-triage/SKILL.md` (insert a `## Proposal lane` section between `## Non-proposal lane` and `## Report`)

**Interfaces:**
- Consumes: `<repo>`, `<base>`, `<slug>`, and the per-task loop from Task 5.
- Produces: a committed, `openspec validate --strict`-clean proposal on branch `wt/<task-id>`, and the shell variable conventions (`<task-id>`, `<change-slug>`, `<sha>`) that Task 7 continues from.

- [ ] **Step 1: Add the proposal lane section**

Insert into `~/.claude/skills/hermes-triage/SKILL.md`, before `## Report`:

```markdown
## Proposal lane

For each task classified as a proposal idea.

### 1. Isolate

```bash
git -C <repo> worktree add <repo>/.worktrees/<task-id> -b wt/<task-id> <base>
```

`<task-id>` is the raw Hermes task id — no slug suffix. If the branch or path
already exists, that is a leftover from an earlier aborted run: report it and
skip this task rather than reusing or force-deleting it.

### 2. Write the proposal

Pick `<change-slug>` as a short kebab-case verb phrase — match the style of the
existing directories under `<repo>/openspec/changes/`.

Read `<repo>/openspec/project.md` and `<repo>/openspec/config.yaml` for stack,
conventions, and domain vocabulary before writing a word. Read two existing
proposals under `<repo>/openspec/changes/` to match structure and depth.

Write into `<repo>/.worktrees/<task-id>/openspec/changes/<change-slug>/`:
- `proposal.md` — the problem, why it matters, the proposed approach, what is
  explicitly out of scope
- `tasks.md` — implementation steps a different engineer could follow
- `specs/` — spec deltas, following the layout of neighboring changes

The proposal is the deliverable. Do not write implementation code.

### 3. Validate — hard gate

```bash
cd <repo>/.worktrees/<task-id> && openspec validate <change-slug> --strict --json
```

Fix every reported issue and re-run until it is clean. If a failure is not
fixable — the validator wants a structure the idea cannot support — abort this
task: leave the worktree and branch in place, comment the validator output, and
move to the next task.

### 4. Commit, scoped

```bash
cd <repo>/.worktrees/<task-id>
git add openspec/changes/<change-slug>
git commit -m "docs: propose <change-slug>"
```

Stage only that directory. A scoped commit is what makes the Codex review in the
next step see the proposal and nothing else.

Record the resulting SHA — `git rev-parse HEAD` — as `<sha>`.
```

- [ ] **Step 2: Seed a proposal-shaped task in the fixture**

```bash
hermes kanban --board triage-test create "we should cache the config parse" \
  --body "re-reads the file on every access, feels wasteful" --triage
```

- [ ] **Step 3: Run the skill and stop after the validate gate**

Invoke `/hermes-triage --board triage-test`. Expected once step 4 of the proposal lane completes:

```bash
ls /tmp/hermes-triage-fixture/repo/.worktrees/
cd /tmp/hermes-triage-fixture/repo/.worktrees/<task-id> && git log --oneline -1 && git show --stat HEAD
openspec validate <change-slug> --strict
```

Expected: worktree present on `wt/<task-id>`; one commit touching only
`openspec/changes/<change-slug>/`; validator clean.

- [ ] **Step 4: Verify the abort path on a validator failure**

Hand-break a proposal to confirm the gate is real. Break the **spec delta**, not `proposal.md`:
`openspec validate <name> --strict` calls only `validateChangeDeltaSpecs`
(`dist/commands/validate.js:143-149`) and never opens `proposal.md`, so corrupting the narrative
passes and proves nothing.

```bash
cd /tmp/hermes-triage-fixture/repo/.worktrees/<task-id>
echo "garbage" > openspec/changes/<change-slug>/specs/<capability>/spec.md
openspec validate <change-slug> --strict
```

Expected: non-zero exit with a specific complaint about the delta structure. Restore with
`git checkout -- openspec/changes/<change-slug>/specs/`.

- [ ] **Step 5: Verify the file is in place**

```bash
grep -c "Proposal lane" ~/.claude/skills/hermes-triage/SKILL.md
```

Expected: `1`.

---

### Task 7: Skill — Codex loop, merge, promotion, abort paths

**Files:**
- Modify: `~/.claude/skills/hermes-triage/SKILL.md` (extend `## Proposal lane` with steps 5-7; add `## Abort paths` before `## Report`)

**Interfaces:**
- Consumes: `<repo>`, `<base>`, `<task-id>`, `<change-slug>`, `<sha>` from Task 6.
- Produces: the finished skill.

- [ ] **Step 1: Add the Codex loop, merge, and promotion steps**

Append to the `## Proposal lane` section of `~/.claude/skills/hermes-triage/SKILL.md`, after its step 4:

```markdown
### 5. Codex review loop — max 5 rounds

```bash
cd <repo>/.worktrees/<task-id>
codex exec review --commit <sha>
```

For each round:
- Read every finding. Fix the ones that are right.
- For a finding you believe is wrong, say why in your notes — do not silently
  ignore it. If it survives to the abort, it goes in the comment.
- Amend rather than stacking commits: `git commit --amend --no-edit` after
  staging fixes, then re-read `git rev-parse HEAD` into `<sha>`.
- Re-run the review against the new `<sha>`.

Stop when a round reports zero findings. **Hard cap: 5 rounds.** If round 5
still has findings, abort this task — leave the worktree and branch in place and
comment the open findings.

Count the rounds. The count goes in the promotion body.

### 6. Land it

```bash
git -C <repo> status --porcelain
```

If that prints anything, **abort the merge**: leave the worktree and branch in
place and comment that `<repo>` had uncommitted work. Merging into a dirty tree
risks the human's in-flight changes, and forcing past git's refusal is not an
option.

Otherwise:

```bash
git -C <repo> pull --rebase origin <base>
git -C <repo> merge --no-ff wt/<task-id> -m "docs: land proposal <change-slug>"
git -C <repo> push origin <base>
git -C <repo> worktree remove <repo>/.worktrees/<task-id>
git -C <repo> branch -d wt/<task-id>
```

If the push is rejected, stop after the merge: the commit stays local on
`<base>`, the worktree and branch are still in place because removal comes after the push, and
the comment says the push failed.

Record the landed SHA: `git -C <repo> rev-parse HEAD`.

### 7. Promote

```bash
hermes kanban --board <slug> specify <task-id> \
  --title "<refined title>" \
  --body "<body, see below>" \
  --assignee claude-triage \
  --author claude-triage
```

The body must let whoever implements this start without re-reading the original
idea. Include, in this order:
- The refined problem statement and scope
- `openspec/changes/<change-slug>/` — the path to the landed proposal
- The `<base>` SHA the proposal landed on
- Codex round count, and one line on what the review actually changed
- Anything you deliberately left out of scope

Then attach the full review trail:

```bash
hermes kanban --board <slug> comment <task-id> \
  --author claude-triage \
  "Codex review trail: <round-by-round summary>"
```

Verify with `hermes kanban --board <slug> show <task-id>`: status `todo`, body
carries the path and SHA.
```

- [ ] **Step 2: Add the abort paths section**

Insert into `~/.claude/skills/hermes-triage/SKILL.md`, before `## Report`:

```markdown
## Abort paths

Every abort does the same three things: leave the task in `triage`, record why,
continue to the next task. A single bad idea never halts the run.

```bash
hermes kanban --board <slug> comment <task-id> --author claude-triage "<reason>"
```

| Condition | What stays behind |
|---|---|
| Board resolves to no repo | Nothing. The whole run stops before listing tasks. |
| `wt/<task-id>` or its worktree path already exists | Nothing touched; that task is skipped. |
| `openspec validate --strict` unfixable | Worktree and branch intact; validator output in the comment. |
| 5 Codex rounds still dirty | Worktree and branch intact; open findings in the comment. |
| `<repo>` dirty at merge time | Worktree and branch intact; merging is the human's call. |
| `git push origin <base>` rejected | Merge commit local on `<base>`; worktree and branch still intact. |

Never force-push, never `git checkout -f`, never `worktree remove --force`, and
never delete a branch that still holds unmerged work.
```

- [ ] **Step 3: Run the full proposal lane against the fixture**

The fixture repo has no `origin`, so add one so the push step is exercised rather than skipped:

```bash
git init -q --bare /tmp/hermes-triage-fixture/origin.git
git -C /tmp/hermes-triage-fixture/repo remote add origin /tmp/hermes-triage-fixture/origin.git
git -C /tmp/hermes-triage-fixture/repo push -u origin dev
hermes kanban --board triage-test create "we should batch the writes" \
  --body "one syscall per record right now" --triage
```

Invoke `/hermes-triage --board triage-test`. Expected on completion:

```bash
git -C /tmp/hermes-triage-fixture/repo log --oneline -3
ls /tmp/hermes-triage-fixture/repo/openspec/changes/
ls /tmp/hermes-triage-fixture/repo/.worktrees/ 2>&1
git -C /tmp/hermes-triage-fixture/repo branch --list 'wt/*'
hermes kanban --board triage-test show <task-id>
```

Expected: merge commit on `dev`; the change directory present; `.worktrees/`
empty or absent; no `wt/*` branch left; task `todo` with the proposal path,
the SHA, and the Codex round count in its body.

- [ ] **Step 4: Verify the dirty-repo abort**

```bash
echo dirt > /tmp/hermes-triage-fixture/repo/dirty.txt
hermes kanban --board triage-test create "we should add a retry policy" --triage
```

Invoke `/hermes-triage --board triage-test`. Expected: the task stays in
`triage` with a comment naming the uncommitted work; `wt/<task-id>` and its
worktree still exist. Confirm:

```bash
hermes kanban --board triage-test show <task-id>
git -C /tmp/hermes-triage-fixture/repo branch --list 'wt/*'
rm /tmp/hermes-triage-fixture/repo/dirty.txt
```

- [ ] **Step 5: Tear down the fixture**

```bash
git -C /tmp/hermes-triage-fixture/repo worktree list
git -C /tmp/hermes-triage-fixture/repo worktree remove --force /tmp/hermes-triage-fixture/repo/.worktrees/* 2>/dev/null
hermes kanban boards rm triage-test
rm -rf /tmp/hermes-triage-fixture
```

Confirm the real board is still active: `hermes kanban boards show` should print `pmzbot`.

- [ ] **Step 6: Verify the finished skill**

```bash
grep -c "^## " ~/.claude/skills/hermes-triage/SKILL.md
grep -n "claude -p" ~/.claude/skills/hermes-triage/SKILL.md
```

Expected: 6 top-level sections (`Resolve the board`, `Classify`, `Non-proposal lane`, `Proposal lane`, `Abort paths`, `Report`); no `claude -p` hit.

---

### Task 8: Land the Hermes patch

**Files:**
- Modify: nothing new. Merges `feat/kanban-manual-specify`.

**Interfaces:**
- Consumes: Tasks 1 and 2.
- Produces: `hermes kanban specify --title/--body` available to the installed `hermes` binary rather than only in the worktree.

- [ ] **Step 1: Run the kanban suite one more time**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify && python -m pytest tests/hermes_cli/ -k kanban -q
```

Expected: all pass. A failure here is a regression from Tasks 1-2, not flake — fix before landing.

- [ ] **Step 2: Show the human the diff**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify && git log --oneline main..HEAD && git diff main...HEAD
```

Stop and wait for approval. The human asked to see this patch before it lands.

- [ ] **Step 3: On approval, land it**

Ask the human which they want — merge into local `main` at `~/.hermes/hermes-agent`, or open a PR to the fork. Do not choose for them; the fork has an upstream this patch may want to reach.

- [ ] **Step 4: Verify the installed CLI has manual mode**

```bash
hermes kanban specify --help | grep -E "^\s+--(title|body|assignee)"
```

Expected: all three flags. If absent, the install did not pick up the branch — report that rather than editing `~/.hermes/hermes-agent` directly.

---

### Task 9: Per-board auto-triage opt-out

Added mid-execution. Task 5 discovered that the gateway's dispatcher auto-decomposes triage
tasks on **every** board within ~85 seconds, racing `/hermes-triage` and winning. Nothing today
can exempt a board: `_auto_decompose_tick` sweeps `list_boards()` unconditionally, gated only by
the global `kanban.auto_decompose`, and `list_triage_ids` applies no assignee filter. Without
this task the skill cannot own a triage column.

**Files:**
- Modify: `hermes_cli/kanban_db.py:660-687` (`read_board_metadata` defaults) and
  `hermes_cli/kanban_db.py:691-717` (`write_board_metadata` kwargs)
- Modify: `gateway/kanban_watchers.py:1337-1380` (`_auto_decompose_tick` per-board loop)
- Modify: `hermes_cli/kanban.py` (`boards` subparser near `:322-328`, dispatch near `:1198-1223`,
  new handler near `:1378`)
- Test: `tests/hermes_cli/test_kanban_boards.py` (append)

**Interfaces:**
- Consumes: `read_board_metadata(slug) -> dict`, `write_board_metadata(board, *, name=None,
  description=None, icon=None, color=None, archived=None, default_workdir=None) -> None`,
  `list_boards(include_archived=False) -> list[dict]`.
- Produces: board metadata key `auto_triage` (bool, default `True`); CLI
  `hermes kanban boards set-auto-triage <slug> <on|off>`. Task 5's fix round and Task 7 both
  read this flag.

**Design constraints:**
- Default `True` — existing boards keep behaving exactly as they do now. This is opt-out, not
  opt-in. A board.json written before this task has no `auto_triage` key; `read_board_metadata`
  starts from a hardcoded default dict and then does `meta.update(raw)`, so the key materializes
  as `True` on next read with no migration. Do not add a schema version.
- Follow the `archived` template exactly: `"auto_triage": True` in the read-side default dict,
  `auto_triage: Optional[bool] = None` kwarg on the writer, and
  `if auto_triage is not None: meta["auto_triage"] = bool(auto_triage)` so an unmentioned key is
  preserved.
- The skip belongs in the dispatcher's per-board loop, not in `list_triage_ids`. `list_triage_ids`
  is also called by the manual `hermes kanban decompose` path, and a human running that command
  explicitly must still work on an opted-out board.

- [ ] **Step 1: Write the failing tests**

Append to `tests/hermes_cli/test_kanban_boards.py`. Read the top of that file first and reuse
its existing fixture rather than declaring a new one:

```python
def test_auto_triage_defaults_true_for_a_new_board(kanban_home):
    kb.create_board("optout-default", name="Optout Default")
    meta = kb.read_board_metadata("optout-default")
    assert meta["auto_triage"] is True


def test_auto_triage_materializes_true_for_a_legacy_board_json(kanban_home):
    kb.create_board("legacy", name="Legacy")
    path = kb.board_metadata_path("legacy")
    raw = jsonlib.loads(path.read_text())
    del raw["auto_triage"]
    path.write_text(jsonlib.dumps(raw))

    meta = kb.read_board_metadata("legacy")
    assert meta["auto_triage"] is True


def test_write_board_metadata_sets_and_preserves_auto_triage(kanban_home):
    kb.create_board("optout", name="Optout", description="keep me")

    kb.write_board_metadata("optout", auto_triage=False)
    assert kb.read_board_metadata("optout")["auto_triage"] is False
    assert kb.read_board_metadata("optout")["description"] == "keep me"

    kb.write_board_metadata("optout", description="changed")
    assert kb.read_board_metadata("optout")["auto_triage"] is False
    assert kb.read_board_metadata("optout")["description"] == "changed"

    kb.write_board_metadata("optout", auto_triage=True)
    assert kb.read_board_metadata("optout")["auto_triage"] is True
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify && python -m pytest tests/hermes_cli/test_kanban_boards.py -k auto_triage -v
```

Expected: 3 failures — `KeyError: 'auto_triage'`, and
`TypeError: write_board_metadata() got an unexpected keyword argument 'auto_triage'`.

- [ ] **Step 3: Add the metadata key**

In `hermes_cli/kanban_db.py`, add to `read_board_metadata`'s default dict, after `"archived": False,`:

```python
        "auto_triage": True,
```

In `write_board_metadata`, add the kwarg alongside `archived`:

```python
    auto_triage: Optional[bool] = None,
```

and in the merge body, next to the `archived` branch:

```python
    if auto_triage is not None:
        meta["auto_triage"] = bool(auto_triage)
```

Update the writer's docstring to name the new key.

- [ ] **Step 4: Run the metadata tests to verify they pass**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify && python -m pytest tests/hermes_cli/test_kanban_boards.py -v
```

Expected: all pass, including the pre-existing board tests.

- [ ] **Step 5: Write the failing dispatcher test**

The dispatcher skip needs its own test. Find the existing test module covering
`_auto_decompose_tick` — search with
`grep -rln "auto_decompose" tests/` — and append there, matching that module's existing
mocking style. If no test module covers it, create
`tests/gateway/test_kanban_auto_decompose_optout.py` and mirror the closest existing gateway
test's fixtures.

The test must assert the behavioral contract, not the implementation: with two boards, one
`auto_triage=True` and one `auto_triage=False`, each holding a triage task, a single
`_auto_decompose_tick` call invokes `decompose_task` for the enabled board's task and never for
the disabled board's task. Patch `kanban_decompose.decompose_task` with a mock and assert on the
task ids it received.

- [ ] **Step 6: Run it to verify it fails**

Expected: the mock is called for both task ids — the opted-out board is not yet skipped.

- [ ] **Step 7: Implement the dispatcher skip**

In `gateway/kanban_watchers.py`, inside `_auto_decompose_tick`'s loop over
`_kb.list_boards(include_archived=False)`, skip a board whose metadata opts out — before it calls
`_decomp.list_triage_ids()` for that board, so an opted-out board costs no query:

```python
                if not _kb.read_board_metadata(_slug).get("auto_triage", True):
                    continue
```

Use whatever the loop's actual board-slug variable is named — read the loop before editing. The
`.get(..., True)` default matters: a board.json that predates Task 9 must behave as before.

- [ ] **Step 8: Run the dispatcher test to verify it passes**

Run the module you added the test to. Expected: pass.

- [ ] **Step 9: Add the CLI setter**

In `hermes_cli/kanban.py`, add a subparser modeled on `boards set-default-workdir`
(argparse near `:322-328`, handler near `:1378`):

```python
    p_bat = boards_sub.add_parser(
        "set-auto-triage",
        help="Turn the dispatcher's automatic triage specify/decompose on or "
             "off for one board. Off leaves that board's triage column to an "
             "external owner.",
    )
    p_bat.add_argument("slug")
    p_bat.add_argument("state", choices=["on", "off"])
```

Use the actual subparser variable name from the surrounding `boards` block — read it first.

Handler, next to `_cmd_boards_set_default_workdir`:

```python
def _cmd_boards_set_auto_triage(args: argparse.Namespace) -> int:
    """Toggle the dispatcher's auto-triage sweep for one board."""
    normed = kb.normalize_board_slug(args.slug)
    enabled = args.state == "on"
    kb.write_board_metadata(normed, auto_triage=enabled)
    state = "on" if enabled else "off"
    print(f"Board {normed}: auto-triage {state}")
    return 0
```

Use the same slug-normalizing helper `_cmd_boards_set_default_workdir` uses — read that handler
and copy its approach rather than assuming `normalize_board_slug` exists under that name. Wire
the new subcommand into `_dispatch_boards` alongside the others.

Also surface the flag in `boards show` output so the state is discoverable — add a line next to
where that handler prints the default workdir.

- [ ] **Step 10: Write and run the CLI test**

Append to `tests/hermes_cli/test_kanban_boards.py`, using that file's existing CLI-invocation
helper if it has one (check for a `_run_cli`-style function; `tests/hermes_cli/test_kanban_specify.py:218`
has one to copy the shape from):

```python
def test_cli_set_auto_triage_off_then_on(kanban_home, capsys):
    kb.create_board("cliopt", name="Cli Opt")

    rc = _run_cli("boards", "set-auto-triage", "cliopt", "off")
    assert rc == 0
    assert kb.read_board_metadata("cliopt")["auto_triage"] is False

    rc = _run_cli("boards", "set-auto-triage", "cliopt", "on")
    assert rc == 0
    assert kb.read_board_metadata("cliopt")["auto_triage"] is True


def test_cli_set_auto_triage_rejects_bad_state(kanban_home):
    kb.create_board("cliopt2", name="Cli Opt 2")
    with pytest.raises(SystemExit):
        _run_cli("boards", "set-auto-triage", "cliopt2", "maybe")
```

Run: `python -m pytest tests/hermes_cli/test_kanban_boards.py -v`. Expected: all pass.

- [ ] **Step 11: Verify against the live pmzbot board**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify
python -c "
import sys; sys.path.insert(0, '.')
from hermes_cli import kanban_db as kb
print(kb.read_board_metadata('pmzbot').get('auto_triage'))
"
```

Expected: `True` — the real board, whose `board.json` predates this task, reads as opted in. Do
not change it here; Task 7 turns it off for the boards the skill owns.

- [ ] **Step 12: Commit**

```bash
cd /opt/dev/danielcor/hermes-kanban-specify
git add hermes_cli/kanban_db.py hermes_cli/kanban.py gateway/kanban_watchers.py tests/
git commit -m "feat(kanban): per-board auto-triage opt-out

The gateway dispatcher auto-decomposed triage tasks on every board, so an
external owner of a triage column always lost the race. Boards now carry
an auto_triage flag, default on, and the dispatcher skips a board that
turns it off. The manual decompose command still works there.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```
