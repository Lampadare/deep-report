"""Cluster-stage validation in the synthesize phase.

Covers `_cluster_threads`' defenses against an LLM that hallucinates thread IDs
or returns malformed output, plus the dedup applied to `completed_threads` when
deciding single- vs multi-pass synthesis.

All tests monkeypatch `spawn_agent` so no real Claude CLI is invoked.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from deep_report.orchestrator.phases import synthesize as synth_mod
from deep_report.orchestrator.state import State
from deep_report.orchestrator.utils import AgentResult


def _make_state(tmp_path: Path, completed: list[str]) -> State:
    state = State()
    state.report_dir = str(tmp_path)
    state.topic = "Test topic"
    state.brief = ""
    state.report_type = "deep-dive"
    state.expertise_level = "intermediate"
    state.completed_threads = list(completed)
    return state


def _write_summaries(summaries_dir: Path, thread_ids: list[str]) -> None:
    summaries_dir.mkdir(parents=True, exist_ok=True)
    for tid in thread_ids:
        (summaries_dir / f"{tid}_summary.md").write_text(f"summary for {tid}\n")


def _stub_spawn(monkeypatch, output: str, success: bool = True) -> None:
    def fake_spawn_agent(*_args, **_kwargs):
        return AgentResult(success=success, output=output)

    monkeypatch.setattr(synth_mod, "spawn_agent", fake_spawn_agent)


def test_cluster_drops_unknown_thread_ids(tmp_path, monkeypatch):
    """LLM returns a thread_id not in completed_threads; it must be filtered."""
    summaries_dir = tmp_path / "summaries" / "agents"
    _write_summaries(summaries_dir, ["t1", "t2"])

    state = _make_state(tmp_path, completed=["t1", "t2"])

    llm_output = json.dumps({
        "clusters": [
            {
                "id": 1,
                "title": "Section A",
                "theme": "theme A",
                "thread_ids": ["t1", "t99"],  # t99 is hallucinated
            },
            {
                "id": 2,
                "title": "Section B",
                "theme": "theme B",
                "thread_ids": ["t2"],
            },
        ]
    })
    _stub_spawn(monkeypatch, llm_output)

    clusters = synth_mod._cluster_threads(state, summaries_dir)

    assert len(clusters) == 2
    all_tids = [tid for c in clusters for tid in c["thread_ids"]]
    assert "t99" not in all_tids
    assert "t1" in all_tids
    assert "t2" in all_tids


def test_cluster_drops_orphan_summaries(tmp_path, monkeypatch):
    """Summary files for threads not in completed_threads must be ignored.

    The orphan thread id must not appear in the prompt sent to the LLM, nor
    in the final clusters.
    """
    summaries_dir = tmp_path / "summaries" / "agents"
    # t_orphan has a summary file but is NOT in completed_threads
    _write_summaries(summaries_dir, ["t1", "t2", "t_orphan"])

    state = _make_state(tmp_path, completed=["t1", "t2"])

    captured_prompts: list[str] = []

    def fake_spawn_agent(prompt, *_args, **_kwargs):
        captured_prompts.append(prompt)
        # Echo back only the threads we actually saw in the prompt; if the
        # phase forwards the orphan, validation should still strip it.
        return AgentResult(
            success=True,
            output=json.dumps({
                "clusters": [
                    {
                        "id": 1,
                        "title": "Section",
                        "theme": "theme",
                        "thread_ids": ["t1", "t2", "t_orphan"],
                    }
                ]
            }),
        )

    monkeypatch.setattr(synth_mod, "spawn_agent", fake_spawn_agent)

    clusters = synth_mod._cluster_threads(state, summaries_dir)

    # The orphan must not be fed into the LLM prompt.
    assert captured_prompts, "spawn_agent should have been called"
    assert "t_orphan" not in captured_prompts[0]

    # And must not survive validation even if the LLM echoes it.
    all_tids = [tid for c in clusters for tid in c["thread_ids"]]
    assert "t_orphan" not in all_tids
    assert set(all_tids) == {"t1", "t2"}


def test_cluster_handles_malformed_json(tmp_path, monkeypatch):
    """Non-JSON LLM output must not crash; fallback even-split kicks in."""
    summaries_dir = tmp_path / "summaries" / "agents"
    _write_summaries(summaries_dir, ["t1", "t2", "t3", "t4"])

    state = _make_state(tmp_path, completed=["t1", "t2", "t3", "t4"])

    _stub_spawn(monkeypatch, output="not json at all <<garbage>>")

    clusters = synth_mod._cluster_threads(state, summaries_dir)

    # Fallback should produce clusters covering only completed threads.
    assert clusters, "fallback must produce at least one cluster"
    all_tids = [tid for c in clusters for tid in c["thread_ids"]]
    assert set(all_tids) == {"t1", "t2", "t3", "t4"}
    # Every cluster has the required shape.
    for c in clusters:
        assert "id" in c
        assert "title" in c
        assert "theme" in c
        assert "thread_ids" in c


def test_followup_count_deduplicates(tmp_path, monkeypatch):
    """`run_synthesize` must dedupe completed_threads when picking strategy.

    11 raw entries with only 5 unique ids must take the single-pass branch
    (threshold is <=10 unique), proving the dedup happens.
    """
    state = _make_state(
        tmp_path,
        completed=["t1", "t1", "t2", "t2", "t3", "t3", "t4", "t4", "t5", "t5", "t1"],
    )
    # Avoid touching disk-bound checkpoint/registry side-effects.
    state._state_file = ""

    calls = {"single": 0, "multi": 0}

    def fake_single(*_args, **_kwargs):
        calls["single"] += 1
        return True

    def fake_multi(*_args, **_kwargs):
        calls["multi"] += 1
        return True

    monkeypatch.setattr(synth_mod, "_single_pass_synthesis", fake_single)
    monkeypatch.setattr(synth_mod, "_multi_pass_synthesis", fake_multi)
    # Stub out post-synthesis side-effects so the function returns cleanly.
    monkeypatch.setattr(synth_mod, "_compile_references", lambda *a, **k: None)

    ok = synth_mod.run_synthesize(state)

    assert ok is True
    assert calls["single"] == 1, "5 unique threads should take single-pass"
    assert calls["multi"] == 0, "dedup'd count of 5 must not trip multi-pass"
    assert state.synthesis_strategy == "single"
