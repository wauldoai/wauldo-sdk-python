"""Verification block of a task: verdict, claims, honesty signals, run
contract attestation, and the outcome of a fact-check race.

Re-exported by :mod:`wauldo.agents_types` and the package root.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class TaskClaim:
    """A single verified claim from the agent output."""

    text: str
    supported: bool
    confidence: float


@dataclass
class HonestySignals:
    """Checks of the final answer itself (``TaskVerification.honesty_signals``)."""

    #: The answer is empty or says nothing (e.g. "no answer").
    is_non_answer: bool = False
    #: The answer is a raw JSON dump with no readable prose.
    json_garbage: bool = False
    #: Which checks fired, so a client can say why the answer was flagged.
    matched_patterns: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HonestySignals":
        return cls(
            is_non_answer=bool(d.get("is_non_answer", False)),
            json_garbage=bool(d.get("json_garbage", False)),
            matched_patterns=list(d.get("matched_patterns") or []),
        )


@dataclass
class ContractAttestation:
    """Did the run honour its agent's contract (allowed tools, transitions,
    output schema, forbidden behaviours)? Independent of the fact-check
    ``verdict``: an answer can be ``SAFE`` and its run ``DEGRADED``.

    ``verdict`` is ``"SUCCESS"``, ``"DEGRADED"`` or ``"FAIL"``. Each
    violation is a dict whose ``kind`` names it (``"disallowed_tool"``,
    ``"unknown_state"``, ``"transition_violation"``,
    ``"output_schema_mismatch"``, ``"forbidden_pattern_matched"``,
    ``"misconfigured"``) with that kind's fields.
    """

    matches: bool
    verdict: str
    replay_token: str = ""
    violations: List[Dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ContractAttestation":
        return cls(
            matches=bool(d.get("matches", False)),
            verdict=d.get("verdict", ""),
            replay_token=d.get("replay_token", ""),
            violations=[v for v in d.get("violations") or [] if isinstance(v, dict)],
        )


@dataclass
class RaceCandidate:
    """One model's result in a fact-check race."""

    model: str
    score: Optional[float] = None
    timed_out: bool = False


@dataclass
class RaceResult:
    """Outcome of a fact-check race (``Task.race_result``): the winning
    model and every candidate."""

    winner: str
    winner_score: float
    candidates: List[RaceCandidate] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RaceResult":
        return cls(
            winner=d.get("winner", ""),
            winner_score=float(d.get("winner_score", 0.0)),
            candidates=[
                RaceCandidate(
                    model=c.get("model", ""),
                    score=c.get("score"),
                    timed_out=bool(c.get("timed_out", False)),
                )
                for c in d.get("candidates") or []
                if isinstance(c, dict)
            ],
        )


def _optional(cls: Any, raw: Any) -> Any:
    """``cls.from_dict(raw)`` for a dict, ``None`` otherwise."""
    return cls.from_dict(raw) if isinstance(raw, dict) else None


@dataclass
class TaskVerification:
    """Verification block attached to completed tasks.

    See the ``verdict`` enum :class:`~wauldo.agents_types.Verdict` for
    possible values. When ``verification_source == "prompt_only"`` the
    ``confidence`` and ``hallucination_rate`` fields reflect self-consistency
    only; rely on ``verdict`` + ``support_score`` + ``message`` as
    authoritative.

    .. note::
        ``support_score`` is the public name for the same numeric value
        the wire protocol calls ``trust_score`` (0-1 fraction of claims
        supported by the sources). Both attributes are populated and
        always equal — ``trust_score`` is kept for backward compatibility
        with code written before the 2026-04-17 reframe and the JSON
        wire field is unchanged.
    """

    verdict: str
    hallucination_rate: float
    confidence: float
    trust_score: float
    verification_source: str
    claims: List[TaskClaim] = field(default_factory=list)
    verification_retries: int = 0
    message: Optional[str] = None
    sources_cited: List[int] = field(default_factory=list)
    stripped_claims: List[str] = field(default_factory=list)
    #: Checks of the final answer itself; ``None`` when they were not run.
    honesty_signals: Optional[HonestySignals] = None
    #: Whether the run honoured the agent's contract; ``None`` for runs
    #: without one.
    contract_attestation: Optional[ContractAttestation] = None
    #: Why the run failed before reaching a verdict; ``None`` on success.
    failure_reason: Optional[str] = None
    #: Stage of the pipeline where the run failed; ``None`` on success.
    failure_stage: Optional[str] = None

    @property
    def support_score(self) -> float:
        """Fraction of claims supported by the sources (0-1).

        Alias for :attr:`trust_score`. The wire format keeps
        ``trust_score`` for backward compatibility; new code should
        prefer ``support_score`` to match the public marketing name.
        """
        return self.trust_score

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TaskVerification":
        return cls(
            verdict=d.get("verdict", ""),
            hallucination_rate=float(d.get("hallucination_rate", 0.0)),
            confidence=float(d.get("confidence", 0.0)),
            trust_score=float(d.get("trust_score", 0.0)),
            verification_source=d.get("verification_source", ""),
            claims=[
                TaskClaim(
                    text=c.get("text", ""),
                    supported=bool(c.get("supported", False)),
                    confidence=float(c.get("confidence", 0.0)),
                )
                for c in d.get("claims", [])
            ],
            verification_retries=int(d.get("verification_retries", 0)),
            message=d.get("message"),
            sources_cited=list(d.get("sources_cited", [])),
            stripped_claims=list(d.get("stripped_claims", [])),
            honesty_signals=_optional(HonestySignals, d.get("honesty_signals")),
            contract_attestation=_optional(ContractAttestation, d.get("contract_attestation")),
            failure_reason=d.get("failure_reason"),
            failure_stage=d.get("failure_stage"),
        )
