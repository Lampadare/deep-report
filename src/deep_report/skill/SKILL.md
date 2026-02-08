---
name: deep-report
description: Launch the deep-report CLI to generate research reports. This skill runs a Python orchestrator that spawns parallel Claude agents. Invoke immediately when user says "deep report", "research report on X", or uses /deep-report. Do NOT read files or research first - pass the request directly to the CLI.
model: opus
context: fork
user-invocable: true
allowed-tools: Read, Bash(deep-report *), Write, Glob, Grep, WebFetch, AskUserQuestion
argument-hint: "[topic] --quick | --configure | --agents N --model opus --audio"
---

# Deep Report

Generate comprehensive research reports using parallel Claude agents.

## Your Task

1. **Check installation**: Run `deep-report --version`. If not found, tell user to run `pip install git+https://github.com/lampadare/deep-report`
2. **Parse request**: Extract topic and flags from user's message
3. **Explain**: Tell user the terminal will be busy, suggest monitoring with `tail -f ./<folder>/state/progress.log`
4. **Execute**: Run `deep-report "topic" [flags]`
5. **Summarize**: Report location, key sections, how to view with `cat ./folder/report.md`

## CLI Reference

!`deep-report --help`

## Quick Examples

- Quick report: `deep-report "quantum computing" --quick`
- With opus: `deep-report "AI safety" --model opus --agents 20`
- With seeds: `deep-report "topic" --refs "/path/to/folder"`
- Resume: `deep-report --resume ./report-folder/`
