"""Unit tests for the typed Agents/Tasks models and their from_dict parsers.

These tests cover the wire format → dataclass conversion in
``wauldo.agents_types`` without hitting the network. End-to-end
integration against a live server is exercised in the E2E smoke test.
"""

from __future__ import annotations

from wauldo.agents_types import (
    Agent,
    AgentList,
    AgentRunResponse,
    StateTransition,
    Task,
    TaskStatus,
    TaskVerification,
    Verdict,
)


def test_verdict_enum_values_match_server():
    assert Verdict.SAFE == "SAFE"
    assert Verdict.UNCERTAIN == "UNCERTAIN"
    assert Verdict.PARTIAL == "PARTIAL"
    assert Verdict.BLOCK == "BLOCK"
    assert Verdict.CONFLICT == "CONFLICT"
    assert Verdict.UNVERIFIED == "UNVERIFIED"
    assert Verdict.INSUFFICIENT_CLAIMS == "INSUFFICIENT_CLAIMS"


def test_task_status_values_match_server():
    assert TaskStatus.QUEUED == "queued"
    assert TaskStatus.RUNNING == "running"
    assert TaskStatus.COMPLETED == "completed"
    assert TaskStatus.FAILED == "failed"
    assert TaskStatus.CANCELLED == "cancelled"


def test_agent_from_dict_minimal():
    a = Agent.from_dict({
        "id": "ag-1",
        "tenant_id": "t1",
        "name": "bot",
        "wauldo_toml": "[agent]\nname = \"bot\"",
        "created_at": 100,
        "updated_at": 200,
    })
    assert a.id == "ag-1"
    assert a.tenant_id == "t1"
    assert a.name == "bot"
    assert a.description == ""
    assert a.preset is None


def test_agent_from_dict_preserves_unknown_fields():
    a = Agent.from_dict({
        "id": "ag-1",
        "tenant_id": "t1",
        "name": "bot",
        "wauldo_toml": "x",
        "future_field": "server-added-in-v11",
    })
    assert a._extra == {"future_field": "server-added-in-v11"}


def test_agent_list_from_dict():
    alist = AgentList.from_dict({
        "agents": [
            {"id": "a1", "tenant_id": "t", "name": "x", "wauldo_toml": "y"},
            {"id": "a2", "tenant_id": "t", "name": "x2", "wauldo_toml": "y"},
        ],
        "pagination": {"total": 2, "limit": 20, "offset": 0},
    })
    assert len(alist.agents) == 2
    assert alist.pagination.total == 2
    assert alist.agents[0].id == "a1"


def test_agent_run_response_from_dict():
    r = AgentRunResponse.from_dict({
        "task_id": "t-1",
        "agent_id": "ag-1",
        "status": "queued",
        "created_at": 500,
    })
    assert r.task_id == "t-1"
    assert r.status == "queued"


def test_task_verification_from_dict_populated():
    v = TaskVerification.from_dict({
        "verdict": "SAFE",
        "hallucination_rate": 0.1,
        "confidence": 0.95,
        "trust_score": 0.8,
        "verification_source": "source_documents",
        "claims": [
            {"text": "claim-1", "supported": True, "confidence": 0.9},
        ],
        "verification_retries": 1,
        "message": None,
    })
    assert v.verdict == Verdict.SAFE
    assert v.trust_score == 0.8
    assert len(v.claims) == 1
    assert v.claims[0].supported is True
    assert v.claims[0].confidence == 0.9


def test_task_verification_reads_signals_attestation_and_failure():
    v = TaskVerification.from_dict({
        "verdict": "INSUFFICIENT_CLAIMS",
        "hallucination_rate": 0.0,
        "confidence": 0.0,
        "trust_score": 0.0,
        "verification_source": "source_documents",
        "honesty_signals": {"is_non_answer": True, "json_garbage": False,
                            "matched_patterns": ["empty"]},
        "contract_attestation": {
            "matches": False,
            "verdict": "DEGRADED",
            "replay_token": "rt",
            "violations": [{"kind": "disallowed_tool", "state": "PLAN", "tool": "web_search"}],
        },
        "failure_reason": "LLM provider timeout",
        "failure_stage": "fact_check",
    })
    assert v.verdict == Verdict.INSUFFICIENT_CLAIMS
    assert v.honesty_signals is not None and v.honesty_signals.is_non_answer is True
    assert v.honesty_signals.matched_patterns == ["empty"]
    assert v.contract_attestation is not None
    assert v.contract_attestation.verdict == "DEGRADED"
    assert v.contract_attestation.violations[0]["tool"] == "web_search"
    assert v.failure_reason == "LLM provider timeout"
    assert v.failure_stage == "fact_check"


def test_task_verification_without_the_new_fields_leaves_them_none():
    v = TaskVerification.from_dict({"verdict": "SAFE", "trust_score": 1.0})
    assert v.honesty_signals is None
    assert v.contract_attestation is None
    assert v.failure_reason is None and v.failure_stage is None


def test_task_from_dict_reads_share_and_race_result():
    t = Task.from_dict({
        "task_id": "t",
        "status": "completed",
        "share_id": "r_x",
        "share_url": "https://wauldo.com/r/r_x",
        "race_result": {
            "winner": "model-a",
            "winner_score": 0.9,
            "candidates": [
                {"model": "model-a", "score": 0.9, "timed_out": False},
                {"model": "model-b", "score": None, "timed_out": True},
            ],
        },
    })
    assert t.share_id == "r_x"
    assert t.share_url == "https://wauldo.com/r/r_x"
    assert t.race_result is not None and t.race_result.winner == "model-a"
    assert t.race_result.candidates[1].timed_out is True
    assert t.race_result.candidates[1].score is None
    # Typed fields no longer land in the untyped leftovers.
    assert "share_url" not in t._extra
    bare = Task.from_dict({"task_id": "t", "status": "running"})
    assert bare.share_url is None and bare.race_result is None


def test_task_verification_from_dict_unverified_carries_message():
    v = TaskVerification.from_dict({
        "verdict": "UNVERIFIED",
        "hallucination_rate": 0.0,
        "confidence": 1.0,
        "trust_score": 0.0,
        "verification_source": "prompt_only",
        "message": "No source documents uploaded — trust_score forced to 0.0.",
    })
    assert v.verdict == Verdict.UNVERIFIED
    assert v.trust_score == 0.0
    assert v.confidence == 1.0
    assert "No source documents" in (v.message or "")


def test_task_from_dict_is_done_property():
    for s in ("completed", "failed", "cancelled"):
        t = Task.from_dict({"task_id": "t", "status": s})
        assert t.is_done is True, f"{s} should be terminal"
    for s in ("queued", "running"):
        t = Task.from_dict({"task_id": "t", "status": s})
        assert t.is_done is False, f"{s} should not be terminal"


def test_task_from_dict_with_verification():
    t = Task.from_dict({
        "task_id": "t-1",
        "tenant_id": "tn",
        "status": "completed",
        "prompt": "hi",
        "result": "hello",
        "verification": {
            "verdict": "SAFE",
            "hallucination_rate": 0.0,
            "confidence": 0.9,
            "trust_score": 0.7,
            "verification_source": "source_documents",
        },
    })
    assert t.task_id == "t-1"
    assert t.result == "hello"
    assert t.verification is not None
    assert t.verification.verdict == Verdict.SAFE


def test_state_transition_from_dict():
    e = StateTransition.from_dict({
        "state_name": "Analysis",
        "to_state": "Tradeoffs",
        "condition": "Sequential execution",
        "raw_output": "…",
        "timestamp": 1776283000,
        "success": True,
        "retry_count": 0,
        "duration_ms": 10428,
        "prompt_tokens": 95,
        "completion_tokens": 1457,
        "repair_count": 0,
        "cache_hit": False,
        "validation_notes": [],
    })
    assert e.state_name == "Analysis"
    assert e.to_state == "Tradeoffs"
    assert e.duration_ms == 10428
    assert e.success is True
    assert e.validation_notes == []


def test_agent_run_response_from_dict_carries_the_task_when_present():
    from wauldo.agents_types import AgentRunResponse

    d = {
        "task_id": "tk1",
        "agent_id": "bot",
        "status": "completed",
        "created_at": 1,
        "task": {"task_id": "tk1", "status": "completed", "result": "42"},
    }
    out = AgentRunResponse.from_dict(d)
    assert out.task is not None
    assert out.task.result == "42"
    assert out.task.is_settled
    assert AgentRunResponse.from_dict({"task_id": "tk1"}).task is None


def test_task_is_settled_covers_paused_for_approval():
    from wauldo.agents_types import Task

    paused = Task.from_dict({"task_id": "t", "status": "paused_for_approval"})
    assert paused.is_settled and not paused.is_done
    running = Task.from_dict({"task_id": "t", "status": "running"})
    assert not running.is_settled
