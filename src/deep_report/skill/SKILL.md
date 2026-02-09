---
name: deep-report
description: Launch the deep-report CLI to generate research reports. This skill runs a Python orchestrator that spawns parallel Claude agents. Invoke immediately when user says "deep report", "research report on X", or uses /deep-report. Do NOT read files or research first - pass the request directly to the CLI.
model: opus
context: fork
user-invocable: true
allowed-tools: Read, Bash(deep-report *), Write, Glob, Grep, WebFetch, AskUserQuestion, TaskOutput
argument-hint: "[topic] --quick | --configure | --agents N --model opus --audio"
---

# Deep Report

Generate comprehensive research reports using parallel Claude agents.

## Your Task

1. **Check installation**: Run `deep-report --version`. If not found, tell user to run `pip install git+https://github.com/lampadare/deep-report`

2. **Parse request**: Extract topic from user's message

3. **Ask for preferences**: Use AskUserQuestion to confirm settings before running:

   **Question 1 - Mode** (header: "Mode"):
   - "Quick (recommended)" - Uses defaults: 10 agents, sonnet, intermediate
   - "Configure" - Customize all options

   **If user picks "Configure", ask follow-ups:**

   **Question 2 - Model** (header: "Model"):
   - "Sonnet (recommended)" - Faster, cheaper
   - "Opus" - Higher quality, ~5x cost

   **Question 3 - Agents** (header: "Agents"):
   - "10 agents (recommended)"
   - "5 agents" - Faster
   - "20 agents" - More comprehensive

   **Question 4 - Type** (header: "Type"):
   - "State-of-the-art (recommended)"
   - "Tutorial"
   - "Comparison"
   - "Survey"

   Build flags from answers. If "Quick" → use `--quick`.

4. **Start background task**: Run the CLI with `run_in_background: true`:
   ```
   Bash: deep-report "topic" [flags] --cwd "$(pwd)"
   run_in_background: true
   ```
   This returns a `task_id` immediately.

5. **Get report directory**: Use `TaskOutput` with `block: false` to check early output. Look for "Creating report directory: <path>" to get the report folder path.

6. **Monitor progress**: Poll every 30-60 seconds:
   - Read `<report-dir>/state/progress.log` for human-readable status
   - Use `TaskOutput` with `block: false` to check if task is still running
   - Report progress to user (e.g., "Phase 2: Planning complete, starting research...")

7. **Wait for completion**: When `TaskOutput` shows the task finished:
   - Use `TaskOutput` with `block: true` to get final output
   - Check exit code for success/failure

8. **Summarize results**:
   - Read `<report-dir>/SUMMARY.md` for metrics
   - Tell user the report location and key stats
   - Suggest: `cat <report-dir>/report.md | head -200` to preview

## CLI Reference

!`deep-report --help`

## Quick Examples

- Quick report: `deep-report "quantum computing" --quick`
- With opus: `deep-report "AI safety" --model opus --agents 20`
- With seeds: `deep-report "topic" --refs "/path/to/folder"`
- Resume: `deep-report --resume ./report-folder/`
