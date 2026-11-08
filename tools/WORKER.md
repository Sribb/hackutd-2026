# Headless worker (`tools/worker`)

Lets the orchestrator (Vinny's agent, per AGENTS.md section 2) start work on your laptop with no human in the loop. It reuses `tools/board`, `gh`, and your own Claude login: no open ports, no shared keys.

## Start it (about 5 minutes, once per laptop)

```bash
git fetch origin
git worktree add ../hackutd-worker origin/main   # its own copy: own board cursor, own branches
cd ../hackutd-worker
tools/worker --dry-run                           # shows your default lane and the permissions
tools/worker                                     # leave this terminal running; Ctrl-C stops it
```

Keep your normal interactive Claude session too. The worker uses its own `.board/` cursor in its worktree, so it never eats your unread posts.

## What it does

The orchestrator posts a line like:

```
TASK #42 @mounishmavuduru lane=backend/ Make /api/state use the live clock.
```

Your worker sees it (only lines that tag **you**, only from `WORKER_FROM`), posts `CLAIM #42`, and runs `claude -p` with:

- **Edits only inside the lane** (`Edit(backend/**)`, `Write(backend/**)`). Reading is allowed everywhere. With no `lane=`, it uses your folders from the AGENTS.md owner table.
- **A short command allowlist:** git (add, commit, fetch, checkout, normal push), `gh issue view/comment`, `gh pr create/view`, `tools/board post/read`, `tools/check`, pytest, and npm build/test.
- **Hard denials:** force-push, `gh pr merge`, `gh pr close`, `rm -rf`, `git reset --hard`, `git clean`, `tools/board wait/unread`.
- **A spending cap** of `WORKER_BUDGET` dollars per task (default 5; soft, so the current turn finishes).

The run ends by posting `DONE #42 | PR … | tools/check: …` or `BLOCKED #42: why | needs who`. **Merging stays with you.** Logs are in `.board/worker.log`, and each run's JSON (cost, result) is in `.board/worker-runs/`.

## Settings

| Env | Default |
|---|---|
| `WORKER_FROM` | `vinnycodes67 teameuphoriainv-dot mounishmavuduru` |
| `WORKER_LANE` | your AGENTS.md folders |
| `WORKER_BUDGET` | `5` (USD per task) |
| `WORKER_MODEL` | Claude's default |
| `WORKER_INTERVAL` | `30` (seconds between board polls) |

Every run spends **your** Claude usage. Stop the loop any time with Ctrl-C.
