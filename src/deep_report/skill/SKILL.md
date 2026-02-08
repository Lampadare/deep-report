---
name: deep-report
description: Create comprehensive research reports using a swarm of research agents. Use when asked to "create a report", "research topic", "analyze document/PDF/repo", "write analysis", "decision report", "deep dive on X", or "investigate".
model: opus
context: fork
user-invocable: true
allowed-tools: Read, Bash, Write, Glob, Grep, WebFetch, AskUserQuestion
argument-hint: "[topic] --quick | --configure | --agents N --model opus --audio"
---

# Deep Report

Generate comprehensive research reports using parallel Claude agents.

## Installation Check

Before running, verify deep-report is installed:

```bash
deep-report --version || pip install deep-report
```

If pip install fails, install from GitHub:
```bash
pip install git+https://github.com/yourusername/deep-report
```

## What This Skill Does

1. **Onboarding**: Explains how deep-report works
2. **Configuration**: Translates natural language to CLI flags
3. **Execution**: Runs the CLI (terminal will show Rich TUI progress)
4. **Summary**: Reports results when complete

## Direct CLI Usage

Power users can run directly in terminal:

```bash
deep-report "quantum computing" --quick
deep-report "AI safety" --configure
deep-report --help
```

## When Running via Skill

When invoked via `/deep-report`, you are a **conversational launcher** for the CLI.

### Execution Flow

1. **Parse user's request** - extract topic, flags, seed refs
2. **Explain what will happen** - the terminal will be busy
3. **Execute via Bash** - `deep-report "topic" [flags]`
4. **Wait for completion** - the command blocks
5. **Summarize results** - show report location

### Example

```
User: /deep-report "quantum computing" --quick

You: I'll generate a deep research report on quantum computing using 10 research
agents with Sonnet.

The orchestrator will:
1. Create report directory in current folder
2. Generate research plan
3. Run 10 parallel research agents
4. Synthesize findings into final report

Starting now. This terminal will be busy until completion.
To monitor in another terminal: tail -f ./quantum-computing_*/state/progress.log

[Execute: deep-report "quantum computing" --quick]
```

### After Completion

Report the results:
- Report location
- Word count (if available)
- Key sections
- How to view: `cat ./report-dir/report.md`

## CLI Options

| Option | Description | Default |
|--------|-------------|---------|
| `--quick` | Use defaults, no questions | - |
| `--configure` | Interactive interview | - |
| `--interactive` | Pause for approval at key points | off |
| `--model {sonnet,opus}` | Research agent model | sonnet |
| `--agents N` | Number of agents (max 30) | 10 |
| `--expertise {beginner,intermediate,expert}` | Target audience | intermediate |
| `--report-type {state-of-the-art,tutorial,comparison,survey}` | Report style | state-of-the-art |
| `--download-papers` | Download cited open-access PDFs | off |
| `--audio` | Generate podcast-friendly version | off |
| `--refs PATH` | Seed folder or comma-separated URLs | - |
| `--resume PATH` | Resume interrupted report | - |
| `--output PATH` | Custom output directory | cwd |

## Common Patterns

### Quick report with defaults
```bash
deep-report "Climate change mitigation" --quick
```

### Expert report with opus and 20 agents
```bash
deep-report "Quantum computing" --model opus --agents 20 --expertise expert
```

### Report with seed URLs
```bash
deep-report "AI safety" --refs "https://arxiv.org/paper1,https://example.com/paper2" --download-papers
```

### Report with local seed folder
```bash
deep-report "Neurotech" --refs "/path/to/references/folder" --model opus
```

### Resume interrupted report
```bash
deep-report --resume ./topic_20260207_1430
```

## Output Structure

```
./<topic>_<timestamp>/
├── report.md           # Main report
├── refs.md             # Compiled references
├── report_audio.md     # Audio version (if --audio)
├── SUMMARY.md          # Metrics and file list
├── full/agents/        # Raw research outputs
├── summaries/agents/   # Summarized research
├── papers/             # Downloaded PDFs
└── state/
    ├── manifest.json
    ├── plan.md
    ├── scope.md
    ├── progress.log    # For monitoring
    └── orchestrator_state.json
```

## Troubleshooting

**Not installed:** `pip install deep-report` or `pip install git+https://github.com/yourusername/deep-report`

**Claude CLI not found:** Install Claude Code from https://claude.ai/download

**API rate limits:** Wait and resume with `--resume ./report-dir/`

**Interrupted:** Resume with `--resume` flag pointing to report directory
