# Deep Report

Multi-agent research report generator powered by Claude.

Deep Report orchestrates a swarm of Claude agents to research a topic in parallel, then synthesizes their findings into a comprehensive report.

## Requirements

- Python 3.10+
- [Claude Code](https://claude.ai/download) (Claude CLI must be installed and authenticated)

## Installation

```bash
# From PyPI (once published)
pip install deep-report

# From GitHub
pip install git+https://github.com/lampadare/deep-report

# With pipx (isolated environment, recommended)
pipx install deep-report
```

## Usage

### Quick Start

```bash
# Quick report with sensible defaults (10 agents, sonnet model)
deep-report "quantum computing" --quick

# Interactive configuration
deep-report "AI safety" --configure

# Full control via flags
deep-report "machine learning" --agents 20 --model opus --expertise expert
```

### Claude Code Skill

Install the skill for `/deep-report` command in Claude Code:

```bash
deep-report --install-skill
```

Then in Claude Code:
```
/deep-report "quantum computing" --quick
```

### CLI Options

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

### Examples

```bash
# Expert-level report with opus model
deep-report "quantum error correction" --model opus --agents 20 --expertise expert

# Report with seed references
deep-report "AI safety" --refs "/path/to/papers" --download-papers

# Report with seed URLs
deep-report "climate change" --refs "https://arxiv.org/abs/2301.00001,https://example.com/paper.pdf"

# Resume interrupted report
deep-report --resume ./quantum-computing_20260208_1430
```

## Output

Reports are created in the current directory:

```
./<topic>_<timestamp>/
├── report.md           # Main report
├── refs.md             # Compiled references
├── report_audio.md     # Audio version (if --audio)
├── SUMMARY.md          # Metrics and file list
├── full/agents/        # Raw research outputs
├── summaries/agents/   # Summarized research
├── papers/             # Downloaded PDFs (if --download-papers)
└── state/
    ├── plan.md
    ├── scope.md
    ├── progress.log
    └── orchestrator_state.json
```

## How It Works

1. **Setup Phase**: Creates report directory, processes seed references
2. **Planning Phase**: Generates research plan with parallel threads
3. **Research Phase**: Spawns N parallel Claude agents to research assigned topics
4. **Synthesis Phase**: Merges findings, resolves conflicts, assembles final report
5. **Cleanup Phase**: Compiles references, generates audio version if requested

## Development

```bash
git clone https://github.com/lampadare/deep-report
cd deep-report
pip install -e ".[dev]"
```

## License

MIT
