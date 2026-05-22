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

Or configure interactively:

```bash
deep-report "AI safety" --configure
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

Run the CLI as a silent file-coordinated worker — no questionary prompts, no Rich Live displays, no stdin reads. Used by the bundled Claude Code skill; usable by any agent driver.

```bash
deep-report "topic" --machine --name "short-name" [--interactive]
```

Communication contract:

- `state/progress.jsonl` — append-only NDJSON event log (tail with `tail -F`)
- `state/pending_approval.json` — gate request file
- `SUMMARY.md` — final metrics
- `REPORT_DIR=<path>` — printed as an early stdout line

With `--interactive` added, the CLI pauses at approval gates and waits for:

```bash
deep-report --approve --report-dir <dir> --gate <id> --decision approve|reject|stop_early [--feedback "..."]
```

## License

MIT
