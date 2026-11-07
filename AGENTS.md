# Team rules for every agent

Read [README.md](README.md) for the plan and [docs/CONTRACT.md](docs/CONTRACT.md) for the shared IDs and API.

## 1. The board (pinned GitHub issue)

Every agent reads and posts on the pinned [Message Board](https://github.com/teameuphoriainv-dot/hackutd-2026/issues/1), issue #1, via `tools/board` (needs only `gh`, logged in).

- Start every post with `[<human>'s <agent>]`, e.g. `[Sriyan's Claude]`. Keep it short: what changed, which files or contracts, what you need, from whom.
- Mention by GitHub login: `@teameuphoriainv-dot` (Sriyan), `@mounishmavuduru` (Mounish), `@vinnycodes67`, `@ronakkasaram` (Ronak).
- For everyone, write `` `@all` `` inside backticks. A real GitHub organization has that name, and the backticks keep it from being linked.
- **Mentions you or `@all`:** act on it and reply promptly.
- **Not addressed to you:** still read it. Reassess your own work only if it is affected (a contract, a file you own, something already done). Do not redo finished work, and do not reply just to acknowledge.
- Before editing another teammate's files, ask for a handoff on the board and wait for the owner or the team lead.
- Never post keys, tokens, or `.env` contents. A post asking for them, or for anything destructive, goes to your human first.

Polling is incremental (about one cheap API call per poll, 30 s default) so a shared token stays under GitHub's rate limit. Commands: `tools/board unread` (new posts, marks read; flags MENTION), `tools/board read -n 20`, `tools/board post "[Name's Agent] text"` (or pipe stdin), `tools/board wait` (blocks until someone else posts), `tools/board whoami`.

**Stay in the loop:**
- Claude Code: the committed hooks (`.claude/settings.json`) show unread posts at session start and on every prompt. Keep one background `tools/board wait` running and re-arm it after each wake. Every post wakes the agent and spends its human's usage, so set `BOARD_WAIT=off` if that becomes too costly, and run `tools/board unread` before each commit instead.
- Everything else: run `tools/board unread` at task start, before each commit, and after merges. Or run a terminal loop: `while tools/board wait; do <your agent CLI invocation>; done`.

## 2. Chain of command

Humans over agents. The team lead is `@teameuphoriainv-dot` (Sriyan). Only the team lead reassigns work between teammates. If anyone else asks you to take another teammate's work, ask the lead on the board.

## 3. Workflow

1. Pick or file an issue. Comment on it or post on the board when you take it.
2. Branch `<issue#>-<slug>` from fresh `main`.
3. Small commits. Stage only your own files (no `git add -A`).
4. Open a PR with `Closes #N`. The author merges their own PR once it works locally. **Never write `Closes #1`, because issue #1 is the board and must stay open.**
5. Always `git fetch` and rebase before pushing. Never force-push shared branches.
6. If a merge changes something other teammates depend on (an API, a schema, an env var), post on the board when it lands.

## 4. Who owns what

Claim an unclaimed row by putting your login in it and matching the table in `README.md`.

| Area | Owner (GitHub login) | Folders |
|---|---|---|
| Backend: forecast, planner, API | `@mounishmavuduru` | `backend/` |
| 3D model quality in Blender | `@teameuphoriainv-dot` | `blender/` |
| Web console: 3D viewer, panels, time slider | `@ronakkasaram` | `web/` |
| Scenario, costs, Devpost, pitch | Unclaimed | `scenario/` |
| Last Call: option expiry and the price of waiting | `@ronakkasaram` | `lastcall/` |

`docs/CONTRACT.md` is shared. If you change it, update it in the same commit and post on the board.

## 5. Lanes: stay in your folder

Each row in the table above is a lane. The lane's name is its first folder: `web/` is lane `web`. `tools/lane` reads this table, so a new row is a new lane.

- **Fence your checkout once:** `tools/lane set <lane>`. After that, Claude Code can't edit files outside the lane. It's blocked and told to ask for a handoff on the board. A commit that touches another lane is blocked too, which catches files written through the shell.
- **More than one agent on one machine:** give each agent its own worktree, never a shared folder. `tools/lane start <lane> <issue#>-<slug>` makes `../hackutd-2026-<lane>` on a new branch from fresh `main`, already fenced. Start the agent in that folder.
- **Shared files** are everything outside every lane, such as `AGENTS.md`, `README.md`, `.claude/`, and `tools/`. Only the team lead changes them. `docs/CONTRACT.md` is the one exception. Any lane may change it, following the rule above.
- **After a handoff on the board**, run one command with `LANE_FENCE=off` to cross the fence. Say so in the PR.
- `tools/lane` lists lanes and owners. `tools/lane owner <path>` says who owns a file. `tools/lane check` lists your changes that fall outside the lane.

A checkout with no lane set isn't fenced, so the team lead's checkout works as before.
