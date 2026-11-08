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

## 4. When you finish: check, then post DONE

Done means merged to `main`, not just written.

1. Rebase on fresh `main` and run `tools/check`. It checks the Blender names, the scenario, the running API, the backend tests, and the web build against `docs/CONTRACT.md`, and it skips anything that doesn't exist yet. **Never post DONE on a FAIL.** Fix it. If the failure is in someone else's area, tag them with the FAIL lines instead.
2. Merge your PR, then post **one** DONE on the board in this shape:

   ```text
   [Vinny's Claude] DONE #12: web console loads field.glb and shows the asset ID on click
   PR: <link> | tools/check: PASS (3 pass, 0 fail, 3 skip)
   Changed: web/ only, no contract change
   Next: @mounishmavuduru the console now polls /api/state every 2 s, ready for real data
   ```

3. Tag only the people whose work touches yours. Use `` `@all` `` only for contract changes and milestones.

**When someone else's DONE touches what you use:** `git pull`, run `tools/check`, and keep working. Reply only if it fails, quoting the FAIL lines and tagging the poster. Don't post just to say thanks.

**Milestone check:** at each milestone in `README.md`, the team lead posts `` `@all` `` and every agent pulls, runs `tools/check`, and replies with its result line. That's how we know everything still works together.

## 5. Who owns what

Claim an unclaimed row by putting your login in it and matching the table in `README.md`.

| Area | Owner (GitHub login) | Folders |
|---|---|---|
| Backend: forecast, planner, API | `@mounishmavuduru` | `backend/` |
| 3D model quality in Blender | `@teameuphoriainv-dot` | `blender/` |
| Web console: 3D viewer, panels, time slider | Unclaimed | `web/` |
| Scenario, costs, Devpost, pitch | `@vinnycodes67` | `scenario/` |
| Ingest: blueprint file to blueprint.json with Claude | `@teameuphoriainv-dot` | `ingest/` |
| Library on Maincloud: presets and the asset library | `@teameuphoriainv-dot` | `library/` |

`docs/CONTRACT.md` is shared. If you change it, update it in the same commit and post on the board.
