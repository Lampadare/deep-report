---
name: deep-report
description: Generates multi-agent research reports by orchestrating the deep-report CLI. Use when the user asks for a "deep report", a "research report on X", or invokes /deep-report. The CLI is a long-running worker; this skill drives it via state/progress.jsonl and AskUserQuestion for any user interaction.
model: opus
argument-hint: "[topic] [--quick] [--agents N] [--model opus] [--audio]"
allowed-tools: Read Write Glob Grep AskUserQuestion Monitor Bash(deep-report *) Bash(mkdir *) Bash(tail *) Bash(grep *)
---

# Deep Report

Run the deep-report CLI in `--machine` mode and orchestrate it from the conversation.

## Protocol contract (machine mode)

The CLI runs as a silent worker. Communication is file-based:

- **`state/progress.jsonl`** — append-only NDJSON event log. Tail this to track progress. Event types you care about: `phase_start`, `phase_complete`, `approval_waiting`, `summary`, `report_ready`, `error`.
- **`state/pending_approval.json`** — gate request file. When an `approval_waiting` event fires, read this file's `metadata`, ask the user via `AskUserQuestion`, then release the gate with `deep-report --approve --report-dir <dir> --gate <id> --decision approve|reject|stop_early [--feedback "..."]`.
- **`SUMMARY.md`** — final metrics. Read after `report_ready`.
- **`REPORT_DIR=<path>`** — printed as an early stdout line in machine mode, so you know which dir to watch.

## Workflow

### 1. Check installation

Run `deep-report --version`. If missing, tell the user to install with `pipx install deep-report` (or `pip install git+https://github.com/lampadare/deep-report`).

### 2. Parse the request

Extract the topic from `$ARGUMENTS`. If the user already passed flags like `--quick`, `--agents 20`, or `--model opus`, skip step 3 and use those values directly.

### 3. Gather config via AskUserQuestion (two-call pattern)

**Call 1** — single binary choice:

| header | options |
|---|---|
| Mode | "Quick defaults (Recommended)" / "Customize" |

If the user picks **Quick**, skip to step 4 with defaults (sonnet, 10 agents, deep-dive, intermediate).

**Call 2 (only if Customize)** — bundle three questions in one `AskUserQuestion` call:

| header | options |
|---|---|
| Model | "Sonnet (Recommended)" / "Opus" |
| Agents | "10 (Recommended)" / "5" / "20" |
| Type | "Deep-dive (Recommended)" / "Tutorial" / "Comparison" / "Survey" |

### 4. Generate a short name

Build `--name` from the topic: lowercase, kebab-case, ≤30 chars, alphanumerics + hyphens only. Drop stopwords ("a", "the", "in", "of"), replace spaces with `-`, strip punctuation.

Examples:
- `"Quantum Computing in 2026"` → `quantum-computing-2026`
- `"A Survey of LLM Agents"` → `survey-llm-agents`
- `"AI Safety: Current Landscape"` → `ai-safety-landscape`

Required when topic > 100 chars; harmless otherwise.

### 5. Launch the CLI

Invoke the Bash tool with `run_in_background: true` and command:

```
deep-report "<TOPIC>" --machine --name "<NAME>" [--model opus] [--agents N] [--report-type X] [--audio]
```

Save the `task_id` returned by Bash. Do **not** call `TaskOutput block: true` — the buffered stdout is verbose and pulls non-essential text into context. State is in `progress.jsonl`.

### 6. Find the report directory

Read early stdout via `TaskOutput block: false`. Grep for `^REPORT_DIR=` (anchored — the line appears immediately after setup determines the path). If you don't see it on the first poll, call `TaskOutput block: false` again after a brief pause; retry up to ~10 times. If `progress.jsonl` events start arriving before you find REPORT_DIR=, derive the path from the Monitor stdout instead (the file lives at `<REPORT_DIR>/state/progress.jsonl`).

### 7. Watch the event stream

Use `Monitor` to tail the JSONL file filtered to key events:

```
tail -F <REPORT_DIR>/state/progress.jsonl | grep --line-buffered -E '"type":"(phase_start|phase_complete|approval_waiting|approval_timeout|intervention_needed|summary|report_ready|report_failed|error)"'
```

Each matched line is one notification. Translate each event into a single user-facing update:
- `phase_start` → "Phase N (<name>) starting..."
- `phase_complete` → "Phase N (<name>) complete"
- `approval_waiting` → see step 8
- `approval_timeout` → a gate timed out without a response; surface and offer to retry/abort
- `intervention_needed` → orchestrator wants attention (e.g. rate limit); surface and pause
- `error` → surface the message
- `report_ready` → see step 9 (success)
- `report_failed` → run aborted; read the `reason` field and surface; suggest `deep-report --resume`

### 8. Handle approval gates (only if `--interactive` was passed)

When `approval_waiting` fires:

1. Read `<REPORT_DIR>/state/pending_approval.json`. **Before acting, confirm `status == "pending"` and `gate_id` matches the event's `gate_id`** — if either doesn't match, the event is stale; wait for the next one.
2. The `metadata` field has decision-relevant data; never read the agent files themselves.
3. Use `AskUserQuestion` to ask the user what to do. Frame options based on `gate_type`:
   - `proceed_or_quit` → "Approve" / "Reject"
   - `proceed_stop_quit` → "Approve" / "Stop early (synthesize what's done)" / "Reject"
4. Release the gate:
   ```
   deep-report --approve --report-dir <REPORT_DIR> --gate <gate_id> --decision approve|reject|stop_early [--feedback "..."]
   ```
5. The CLI's poll loop sees the response within ~2s and continues.

### 9. Finish

On `report_ready`:
1. Read `<REPORT_DIR>/SUMMARY.md`.
2. Report the path and headline metrics to the user (word count, agents completed, references, cost).
3. Suggest: `cat <REPORT_DIR>/report.md` to preview.

## Errors and edge cases

- **`error: --machine requires a topic`** — pass the topic argument.
- **`error: --machine requires --name when topic is over 100 chars`** — generate a shorter name and retry.
- **CLI exits non-zero** — read the last events from `progress.jsonl` for the failure cause. Suggest `deep-report --resume <REPORT_DIR>` to retry from the last checkpoint.
- **Skill invoked without --interactive** — gates auto-approve. The user has no mid-run control; this is intentional for the "fire-and-forget" path.

## Quick examples

- Fastest path: `deep-report "quantum computing" --machine --name quantum-computing`
- Customized: `deep-report "AI safety landscape" --machine --name ai-safety --model opus --agents 20`
- With approval gates: add `--interactive` to the launch
- Resume: `deep-report --resume <REPORT_DIR>`
