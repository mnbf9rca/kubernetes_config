# Agent teams

Operator rulings, September 20, 2026, define this repository's multi-agent working arrangement.

## Three seats, one workspace

Herdr is a terminal multiplexer that recognises coding agents in its panes and exposes them through the `herdr` command line.
A workspace groups the panes; each seat is an agent in that workspace.
The builder uses an isolated Git worktree branch within the shared repository.

| Seat | Agent | Responsibility |
|---|---|---|
| Chief of staff | Claude, `chief-of-staff` | Operator conversation, work classification and delegation. |
| Builder | Codex, `implementer` | Specifications, implementation plans, code, documentation and commits. |
| Reviewer | Claude on the most capable model at high effort, `k8s-reviewer` on September 20 | Independent review and read-only verification. |

The chief of staff keeps its context clean.
It captures the operator's answers to brainstorming questions before the builder writes the specification.
It does not read inventories or diffs, or write specifications or plans.
It rules on disagreements, defective plans and the escalations defined below.
The builder writes the specification, then the implementation plan, then the code and documentation.
It commits on its isolated worktree branch.
The reviewer reviews each artifact in turn.
Design review includes the deletion seat required by [AGENTS.md](../../AGENTS.md#when-editing): identify unnecessary machinery and what deleting it would lose.
Task reviews give separate spec-compliance and quality verdicts.
The reviewer holds `kubectl`, `talosctl` and `omnictl` access for read-only verification.
Access does not authorize changes to a cluster.

## Before the operator leaves

Pre-warm SSH, both kubectl contexts, omnictl and talosctl access for every seat.
Run `scripts/reauth.sh` from the main checkout after every worktree is created; it warms all of those and allows each worktree's `.envrc` so `op` can run without prompting.
A seat whose shell is not interactive, which is how Codex runs commands (`zsh -lc`), gets no direnv hook: it must prefix every `make` or `op` command with `eval "$(direnv export zsh)"`, or each call prompts the operator through the 1Password desktop app.
Use [Omni access](omni-access.md) for setup and authentication.
The kubectl contexts are `cynexia-homelab` and `cynexia-vps`.
A browser sign-in prompt during unattended work blocks that seat until the operator returns.

## Direct review and file handovers

The plan tooling creates the subagent-driven-development (SDD) workspace at `.superpowers/sdd/<plan>/`.
This tree is gitignored across clones.
SDD holds the briefs, reports and verdict files used for handovers.
Every handover carries a file path there, never pasted artifact or command output.
A pasted report or diff stays in the recipient's context for the rest of its session; a path costs one line.
Use absolute handover paths.
The main checkout and a worktree can hold different files at the same relative path.
A short routing message can also carry the commit range and verdict.

1. Write the artifact and report in the locations required by the plan.
2. Commit the implementation or documentation on the worktree branch.
3. Send the reviewer the report path, brief path, changed file paths and commit range.
4. Write the review verdict to an SDD file.
5. Reply directly to the builder with the verdict path and verdict.
6. Fix the Critical and Important findings in one commit.
7. Send the next report path and commit range.
8. Repeat within the limits below until clean or escalated.
9. Tell the chief of staff the result and final report path.

Steps 4 and 5 belong to the reviewer; the builder performs the other steps.
The chief of staff does not relay reports, diffs or findings between them.
Unresolved disagreement or a defective plan goes to the chief of staff as an SDD file path.

## Loop governance

### Limits and batching

Task reviews allow at most three fix rounds.
A disputed finding escalates after two rounds even inside a task review.
Escalate any finding still open after the third fix round.
Spec and plan reviews allow two rounds.
Whole-branch reviews allow one fix wave and one scoped re-review.
Escalate anything left after those limits.
Only Critical and Important findings re-enter a loop.
Put Minors on the ledger's deferred list.
Fix deferred Minors in one batch at the end, or leave them deferred.
Do not re-raise a deferred Minor.
Re-review only the fix diff.
Fix all findings admitted to a round in one commit.
Send one re-review request for that commit.
Never make one commit per finding.
The reviewer sends one ranked report per request.
Dispatch small same-shape work, such as table rows or the four secret edits, as one batch.
Review that batch once.

### Ownership and waiting

After a review, the next move belongs to the builder.
Run every step your credentials and tools allow, including restore drills with the job key.
Ask the operator only for private-vault access, browser sign-in or publication approval.
Re-prompt once after fifteen minutes without a reply, then escalate continued silence to the chief of staff.
Operator ruling, September 20, 2026: never run long tasks synchronously.
Never block your turn on another seat or cluster Job for more than about one minute.
Start a background monitor or background wait, then continue other work or end your turn.
For Herdr agents, poll `herdr agent get <name>` in the background and report `done` or `blocked`.
For cluster Jobs, poll the Job's conditions in the background and report completion or failure.
Use foreground `herdr agent wait` only for waits known to take less than one minute.
Hand external waits longer than fifteen minutes to a background monitor in the chief of staff's session.

### Ledger and reports

The builder maintains `.superpowers/sdd/<plan>/progress.md`, the ledger.
The chief of staff reads it on demand.
The builder sends the chief of staff one line at task start, task completion and each escalation.
The completion line includes the verdict.
The builder sends no per-round updates to the chief of staff.
The chief of staff reports to the operator only at milestones: spec ready, plan ready, deployed or blocked.
A decision only the operator can make is also a reporting milestone.
Use absolute paths for every file named in messages to the operator, so links are clickable.

### Escalation

Escalate findings that contradict the spec or plan to the chief of staff.
Escalate irreversible actions, and cluster changes outside the task's plan, to the chief of staff.
Escalate a diff resource the branch never touched, or a guard failure outside the task's files.
Escalate disagreements that survive two rounds, or a silent seat after one re-prompt.
For an unresolved finding, write the finding verbatim and the builder's counter-position in one SDD file.
Send its absolute path to the chief of staff.
The chief of staff's ruling is final.
Record that ruling in the ledger.

## Herdr commands

Run these commands from a Herdr-managed pane, where `HERDR_ENV=1`.
Use `herdr --help`, `herdr agent` and `herdr pane` for the installed command syntax.
Replace the example absolute paths with the current plan directory.
Replace `<pane-id>` with the pane ID from `herdr agent list`.

| Command | Use |
|---|---|
| `herdr agent list` | Find live agent names, pane IDs and states. |
| `herdr agent prompt k8s-reviewer "Review /absolute/checkout/.superpowers/sdd/<plan>/report.md; range BASE..HEAD; brief /absolute/checkout/.superpowers/sdd/<plan>/brief.md"` | Builder sends the review handover directly. |
| `herdr agent prompt implementer "Review: /absolute/checkout/.superpowers/sdd/<plan>/review.md; verdict: CLEAN"` | Reviewer returns its verdict directly. |
| `herdr agent wait k8s-reviewer --timeout 60000` | Wait for a settled agent state. |
| `herdr agent read k8s-reviewer --source recent-unwrapped --lines 40` | Inspect progress or a question dialog. |
| `herdr pane run <pane-id> "<answer>"` | Send answer text and Enter to an inspected question dialog. |

Use `pane run` for a question dialog only when the operator has supplied or authorized the answer.
The pane command sends raw terminal input; it does not validate the question or the answer.
`herdr agent prompt` refuses an agent waiting at a question or approval dialog with `agent_blocked`.
A selection dialog may need `herdr agent send-keys <name> <key>` instead of text.
Use `agent prompt` for normal handovers.

Agent names are unique server-wide, not workspace-wide.
On September 20, `reviewer` was already taken, so this team's reviewer became `k8s-reviewer`.
Discover the name before sending work.
Do not assume a neighboring workspace's `reviewer` belongs to this team.

A standalone `herdr agent wait` can return the pre-prompt idle state when called within seconds of the prompt.
That response does not prove the new review finished.
Check for activity after submission and a verdict file for the requested commit range.
The installed CLI's `agent prompt --wait` observes activity before accepting a settled state.
