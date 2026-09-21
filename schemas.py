"""Pydantic models for the LogiPulse AI triage engine.

Everything crossing a boundary - the HTTP request, the Jev evaluation, the HTTP
response - is modelled here so the API surface stays typed end to end.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class DeliveryAction(str, Enum):
    """The closed set of options Jev selects between for `recommended_action`."""

    DELIVER_DOORMAN = "deliver_doorman"
    DELIVER_NEIGHBOR = "deliver_neighbor"
    LEAVE_SECURE_SPOT = "leave_secure_spot"
    RESCHEDULE_EVENING = "reschedule_evening"
    RETURN_TO_HUB = "return_to_hub"
    UNKNOWN_UNCLEAR = "unknown_unclear"


class FinalAction(str, Enum):
    """What the driver app is actually told to do.

    A superset of `DeliveryAction`: the guardrails in section 3 can override the
    model's recommendation with an action that is not in Jev's choice space.
    """

    DELIVER_DOORMAN = "deliver_doorman"
    DELIVER_NEIGHBOR = "deliver_neighbor"
    LEAVE_SECURE_SPOT = "leave_secure_spot"
    RESCHEDULE_EVENING = "reschedule_evening"
    RETURN_TO_HUB = "return_to_hub"
    UNKNOWN_UNCLEAR = "unknown_unclear"
    # Guardrail-only outcomes.
    VERIFY_ID_IN_PERSON = "verify_id_in_person"
    AWAIT_OTP_CONFIRMATION = "await_otp_confirmation"
    HOLD_FOR_FRAUD_REVIEW = "hold_for_fraud_review"


class TriageStatus(str, Enum):
    """Terminal disposition of a delivery event."""

    AUTO_APPROVED = "AUTO_APPROVED"
    REQUIRES_DRIVER_CONFIRMATION = "REQUIRES_DRIVER_CONFIRMATION"
    ESCALATED_TO_DISPATCH = "ESCALATED_TO_DISPATCH"
    SECURITY_BLOCK = "SECURITY_BLOCK"
    REQUIRES_OTP_VERIFICATION = "REQUIRES_OTP_VERIFICATION"
    FLAGGED_FOR_FRAUD_AUDIT = "FLAGGED_FOR_FRAUD_AUDIT"


class RiskLevel(str, Enum):
    """Human-facing banding of the continuous `risk_score`."""

    NONE = "NONE"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class AppliedRule(str, Enum):
    """Which deterministic rule from section 3 decided the outcome."""

    RULE_A_CONFIDENCE_ROUTING = "RULE_A_CONFIDENCE_ROUTING"
    RULE_B_SIGNATURE_GUARDRAIL = "RULE_B_SIGNATURE_GUARDRAIL"
    RULE_C_HIGH_VALUE_OTP = "RULE_C_HIGH_VALUE_OTP"
    RULE_D_FRAUD_REDIRECTION = "RULE_D_FRAUD_REDIRECTION"


#: Actions that release the parcel without the named recipient present. These are
#: the ones the signature guardrail (rule B) must never allow.
UNATTENDED_ACTIONS: frozenset[FinalAction] = frozenset(
    {
        FinalAction.DELIVER_DOORMAN,
        FinalAction.DELIVER_NEIGHBOR,
        FinalAction.LEAVE_SECURE_SPOT,
    }
)


# ---------------------------------------------------------------------------
# Request
# ---------------------------------------------------------------------------


class DeliveryEventInput(BaseModel):
    """An unstructured last-mile delivery event awaiting triage."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "tracking_id": "LP-2291-AX",
                    "customer_note": "Not home until 7pm, please leave it with my neighbour at 14B.",
                    "package_value_usd": 48.90,
                    "requires_signature": False,
                    "delivery_attempt": 1,
                }
            ]
        },
    )

    tracking_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        description="Carrier tracking identifier for the parcel.",
    )
    customer_note: str = Field(
        ...,
        min_length=1,
        max_length=4000,
        description="Free-text note left by the customer, or captured by the driver at the door.",
    )
    package_value_usd: float = Field(
        ...,
        ge=0.0,
        le=1_000_000.0,
        description="Declared value of the parcel in USD; drives the high-value guardrail.",
    )
    requires_signature: bool = Field(
        ...,
        description="Whether the shipment contract demands a signature from the named recipient.",
    )
    delivery_attempt: int = Field(
        ...,
        ge=1,
        le=20,
        description="Which delivery attempt this is, starting at 1.",
    )

    @field_validator("tracking_id", "customer_note")
    @classmethod
    def _strip_and_require_content(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be blank")
        return stripped


# ---------------------------------------------------------------------------
# Jev evaluation payloads
# ---------------------------------------------------------------------------


class ChoiceEvaluation(BaseModel):
    """Output of a Jev `Choice` primitive."""

    model_config = ConfigDict(frozen=True)

    choice: str = Field(..., description="Label with the highest probability.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence in the selected label.")
    probabilities: dict[str, float] = Field(
        default_factory=dict,
        description="Full RLCD distribution over the closed option set; sums to ~1.",
    )


class ScoreEvaluation(BaseModel):
    """Output of a Jev `Score` primitive."""

    model_config = ConfigDict(frozen=True)

    score: float = Field(..., description="Probability-weighted expected score across the rubric.")
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence in the score.")
    probabilities: dict[int, float] = Field(
        default_factory=dict,
        description="Probability of each rubric level, keyed by integer level.",
    )
    legend: dict[int, str] = Field(
        default_factory=dict,
        description="Rubric descriptions keyed by integer level, for interpreting the score.",
    )


class NoulEvaluation(BaseModel):
    """Output of a Jev `Noul` primitive: the probability that a statement holds."""

    model_config = ConfigDict(frozen=True)

    noul: float = Field(..., ge=0.0, le=1.0, description="Probability the statement is true.")

    @computed_field  # type: ignore[prop-decorator]
    @property
    def probability(self) -> float:
        """Alias for `noul`, kept because both spellings are used operationally."""
        return self.noul


class TokenUsage(BaseModel):
    """Billable token counts reported by the Jev API."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)


class EvaluationResult(BaseModel):
    """The complete, typed result of one speculative fan-out against Jev."""

    model_config = ConfigDict(frozen=True)

    recommended_action: ChoiceEvaluation
    risk_score: ScoreEvaluation
    signature_bypass_requested: NoulEvaluation
    address_redirection_detected: NoulEvaluation

    model_name: str = Field(..., description="Model that answered the questions, e.g. 'jev-latest'.")
    request_id: str | None = Field(
        default=None,
        description=(
            "TypeSafe's `x-typesafe-request-id` for this call. Quote it to support, and use it "
            "to line a triage up against the usage console."
        ),
    )
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: float = Field(..., ge=0.0, description="Wall-clock round trip to the Jev API, in ms.")
    cost_usd: float = Field(..., ge=0.0, description="Billed cost of this evaluation, in USD.")


# ---------------------------------------------------------------------------
# Response
# ---------------------------------------------------------------------------


class TriageResponse(BaseModel):
    """What the driver app and dispatch console consume."""

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {
                    "tracking_id": "LP-2291-AX",
                    "status": "AUTO_APPROVED",
                    "final_action": "deliver_neighbor",
                    "risk_level": "LOW",
                    "confidence": 0.94,
                    "applied_rule": "RULE_A_CONFIDENCE_ROUTING",
                    "applied_rule_detail": "Confidence 0.94 >= 0.90: executed automatically in the driver app.",
                    "latency_ms": 142.7,
                    "cost_usd": 0.0000131,
                }
            ]
        }
    )

    tracking_id: str
    status: TriageStatus
    final_action: FinalAction
    risk_level: RiskLevel
    confidence: float = Field(
        ...,
        ge=0.0,
        le=1.0,
        description=(
            "Routing confidence. Sourced from the `recommended_action` Choice, which is the "
            "decision being routed; Noul answers carry a probability rather than a confidence."
        ),
    )
    applied_rule: AppliedRule
    applied_rule_detail: str = Field(
        ...,
        description="Human-readable explanation of the rule that fired, for the dispatch banner.",
    )
    latency_ms: float = Field(..., ge=0.0)
    cost_usd: float = Field(..., ge=0.0)

    requires_otp: bool = Field(
        default=False,
        description="Whether an OTP must be sent to the customer before the parcel is released.",
    )
    auto_dispatch_suspended: bool = Field(
        default=False,
        description="Whether automated re-dispatch is suspended pending a human decision.",
    )
    evaluation: EvaluationResult = Field(..., description="The full Jev evaluation behind this decision.")
    raw_response: dict[str, Any] = Field(
        default_factory=dict,
        description="Verbatim JSON body returned by the Jev API, for auditing.",
    )
