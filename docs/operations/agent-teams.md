# Agent teams

Operator ruling, September 20, 2026.
This is the working arrangement for multi-agent sessions in this repository.

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
It rules only when the builder and reviewer disagree or the plan is defective.

The builder writes the specification, then the implementation plan, then the code and documentation.
It commits on its isolated worktree branch.
The reviewer reviews each artifact in turn.
Design review includes the deletion seat required by [AGENTS.md](../../AGENTS.md#when-editing): identify unnecessary machinery and what deleting it would lose.
Task reviews give separate spec-compliance and quality verdicts.
The reviewer holds `kubectl`, `talosctl` and `omnictl` access for read-only verification.
Access does not authorize changes to a cluster.

## Before the operator leaves

Pre-warm SSH, both kubectl contexts, omnictl and talosctl access for every seat.
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
6. Fix the findings.
7. Send the next report path and commit range.
8. Repeat until the reviewer gives a clean verdict.
9. Tell the chief of staff the result and final report path.

Steps 4 and 5 belong to the reviewer; the builder performs the other steps.
The chief of staff does not relay reports, diffs or findings between them.
Unresolved disagreement or a defective plan goes to the chief of staff as an SDD file path.

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
