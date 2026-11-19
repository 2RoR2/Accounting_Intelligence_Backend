# Planner playbook

How a Claude session acts as the project planner and orchestrator for Accounting Intelligence.
Written 2026-11-17 when planning moved from Fable to Opus. Read this, then `docs/MASTER_PLAN.md` section 10.

## 1. Roles

| Role | Who | Does | Never does |
|---|---|---|---|
| Owner | Jonathan | Sets priorities, approves merges, deploys, secrets, shared-database changes, paid usage | — |
| Planner / orchestrator | One long-lived Opus session | Keeps section 10 current, picks the next task, writes briefs, starts executor sessions, checks their evidence, prepares merges | Writes feature code itself; merges without Jonathan's OK |
| Executor | One Opus session per task | Implements one task on its own branch and worktree, tests, opens a PR | Touches another task's files; merges; deploys |
| Reviewer | Codex (or a fresh Claude session) | Reviews the PR diff, base/head and test evidence | Trusts the executor's summary as proof |

## 2. The planning loop

Run this loop every time an executor finishes or Jonathan asks "what next".

1. **Read state from GitHub, not memory.** Open PRs in both repos, `features` heads, CI if any. One command; do not re-scan the codebase.
2. **Verify the finished task.** Read its PR diff stat and the evidence it reports. Re-run the one check that matters most (usually the test command or a short benchmark) when the claim decides a merge. An executor's summary is context, not proof.
3. **Update section 10.** Mark the task done or blocked with the PR number and one line of evidence. Keep it short; the PR holds the detail.
4. **Pick the next tasks.** Take the first unblocked tasks in section 10.4 order. Run at most one backend and one frontend executor at the same time, never two writers on the same files.
5. **Write the brief** (template in section 4) and start the executor session.
6. **Tell Jonathan** in five lines or fewer: what finished, what is ready to merge (with the PR link), what started, what is blocked on him.

## 3. Rules the planner enforces

- `features` is the integration branch, `main` the release branch. Every task branches from current `features` and returns by PR. Nothing is pushed to `features` or `main` directly. Jonathan merges, or explicitly says "merge #N".
- Merge order: a backend PR before the frontend PR that calls it; bug fixes before features that build on the same code. After each merge, the remaining PRs are updated from `features` before they merge.
- Never use the shared Neon database for development or tests. Local Postgres only (`docker compose up -d db`). The hosted UAT environment gets its own database, created only with Jonathan's approval.
- Never print, copy or commit secrets. `.env` stays local and git-ignored. Briefs refer to variable names only.
- Keep teammates' work: their visual design (`src/styles/*.css`, `src/components/common/ui`) and existing functions stay; tasks change flows and wording on top of them. If a change needs a design decision, ask Jonathan rather than redesigning.
- Guard rails from the brief never move: record printed tax only, no tax computation or filing, no payment initiation, no live ERP posting, nothing approved or posted automatically.
- Accuracy claims use the hold-out set or real pilot documents, never the tuning set.
- When a usage limit or blocker stops an executor, record the exact task and step in section 10 so the next session resumes rather than restarts.
- **Few branches, few PRs (Jonathan, 2026-11-19).**
  1. One branch and one PR per task, per repository. Fixes found during a task (review findings, a lock, a re-read command, a missing choice) go on that task's branch. A new branch is only for a fix that is urgent and unrelated to the task.
  2. Executors never start new sessions or branches on their own. They report the need to the planner, and the planner decides.
  3. Before starting new work, the planner shrinks the open queue: it tells Jonathan which PRs are ready, in merge order, and folds small dependent PRs into one where that is safe (one already contains the other, or they touch the same files for the same task).
  4. After a PR merges, the planner lists its head branch for deletion. Branches are deleted only with Jonathan's OK, or automatically once he turns on "Automatically delete head branches" in the repository settings.
- **Codex status (Jonathan, 2026-11-19: "always tell me if codex isnt working or running out of usage").** When a Codex run (review or build) fails to start, errors, stalls, returns no result, or reports a usage or rate limit, tell Jonathan at once in one line, not just in the evening status: what Codex was doing (PR or task), what happened (error text, or the limit and its reset time if shown), and what the planner did instead (Opus took the review or task, or it waits). Say so again when Codex works again. A missing Codex review never counts as a clean one: a PR without a completed Codex review does not meet any merge rule unless Jonathan says to merge without it.

## 4. Brief template for an executor

```
Task <ID> from docs/MASTER_PLAN.md section 10 (read it and docs/PLANNER_PLAYBOOK.md section 3 first).
Repo/worktree: <repo>, new branch planv3/<id>-<short-name> from origin/features, own worktree.
Goal: <one sentence in accountant terms>.
Scope: <files/areas>. Out of scope: <what not to touch>.
Steps: <3-7 concrete steps>.
Tests: <exact commands>; no new failing test names vs features.
Done when: <observable acceptance criteria, including a browser check for UI tasks>.
Finish: push the branch, open a PR into features with summary, evidence and a not-verified list; do not merge.
One branch and one PR per repository for this task: review fixes and anything found along the way go on this branch. Never start another session or branch; report the need to the planner.
Never print .env values; local database only.
```

## 5. Gates before asking Jonathan to merge

| Change type | Minimum evidence |
|---|---|
| Backend | Full suite (no database) shows no new failing test names; database-backed tests for the touched routes pass on local Postgres |
| Frontend | `npm run typecheck`, `npm run lint`, `npm test` pass; screenshots or a Playwright run of the changed screens at desktop and phone width |
| Extraction / AI | `scripts/lite_benchmark.py` on the tuning set within 1 point of the logged number; hold-out only at milestone gates |
| Workflow (end to end) | The section 10.3 walkthrough steps the task touches pass on localhost |
| Any | Reviewer (Codex or a fresh session) found no blocking issue, or the issues are fixed |

## 6. Handoff note (end of every planner session)

Append to section 10.6: date, what changed, open PRs and their state, running sessions, blockers on Jonathan, the next task. Five to ten lines.
