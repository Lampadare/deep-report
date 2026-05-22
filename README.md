# Deep Report

Multi-agent research reports, powered by Claude.

## Install

```bash
pipx install deep-report
```

Requires Python 3.10+ and [Claude Code](https://claude.ai/download) installed and authenticated.

## Use

```bash
deep-report "quantum computing" --quick
```

Or, without `--quick`, the bare command runs an interactive interview:

```bash
deep-report "AI safety"
```

See all options:

```bash
deep-report --help
```

## Claude Code skill

```bash
deep-report --install-skill
```

Then in Claude Code: `/deep-report "topic" --quick`

## Machine mode (for skills and agents)

Use `--machine` only when driving the CLI from another program (skill, agent, CI). For interactive humans, use `--quick` or the default interview — they offer a richer Rich-based UI. Machine mode is a silent file-coordinated worker: no questionary prompts, no Rich Live displays, no stdin reads.

```bash
deep-report "topic" --machine --name "short-name" [--interactive]
```

Communication contract:

- `state/progress.jsonl` — append-only NDJSON event log (tail with `tail -F`)
- `state/pending_approval.json` — gate request file
- `SUMMARY.md` — final metrics
- `REPORT_DIR=<path>` — printed as an early stdout line (anchored grep `^REPORT_DIR=`)

With `--interactive` added, the CLI pauses at approval gates and waits for:

```bash
deep-report --approve --report-dir <dir> --gate <id> --decision approve|reject|stop_early [--feedback "..."]
```

**Caveat:** `--machine --interactive` does not currently route interventions (rate-limit retries, etc.) through the file-handshake protocol — they auto-skip in machine mode for now. Approval gates work fully.

## License

MIT
