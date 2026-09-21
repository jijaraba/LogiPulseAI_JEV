"""LogiPulse AI - FastAPI triage service.

Jev turns the note into typed primitives; this module decides what happens. Every
threshold in section 3 of the spec lives here as a constant, and the rules are
applied in a fixed precedence so the same event always yields the same verdict.

Run with:  uvicorn api:app --reload
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated, Any, Final

from fastapi import Depends, FastAPI, Header, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

import jev_evaluator
from jev_evaluator import JevEvaluationError, evaluate_delivery_event
from schemas import (
    UNATTENDED_ACTIONS,
    AppliedRule,
    DeliveryEventInput,
    EvaluationResult,
    FinalAction,
    RiskLevel,
    TriageResponse,
    TriageStatus,
)

logging.basicConfig(
    level=os.getenv("LOGIPULSE_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
)
logger = logging.getLogger("logipulse.api")


# ---------------------------------------------------------------------------
# Rule thresholds (section 3)
# ---------------------------------------------------------------------------

#: Rule A - RLCD confidence routing.
CONFIDENCE_AUTO_APPROVE: Final[float] = 0.90
CONFIDENCE_DRIVER_CONFIRM: Final[float] = 0.60
#: Rule B - signature security guardrail.
SIGNATURE_BYPASS_THRESHOLD: Final[float] = 0.75
#: Rule C - risk control by package value.
HIGH_VALUE_USD: Final[float] = 300.00
HIGH_VALUE_RISK_SCORE: Final[float] = 2.0
#: Rule D - ambiguous redirection fraud detection.
REDIRECTION_THRESHOLD: Final[float] = 0.70

#: Risk banding applied to the continuous 0-3 expected score.
RISK_BANDS: Final[tuple[tuple[float, RiskLevel], ...]] = (
    (0.5, RiskLevel.NONE),
    (1.5, RiskLevel.LOW),
    (2.5, RiskLevel.MEDIUM),
)

ALLOW_ORIGINS: Final[list[str]] = [
    origin.strip() for origin in os.getenv("LOGIPULSE_CORS_ORIGINS", "*").split(",") if origin.strip()
]


# ---------------------------------------------------------------------------
# Rule engine
# ---------------------------------------------------------------------------


def classify_risk(score: float) -> RiskLevel:
    """Band the expected risk score onto a human-facing level."""
    for upper_bound, level in RISK_BANDS:
        if score < upper_bound:
            return level
    return RiskLevel.HIGH


def _route_by_confidence(confidence: float) -> tuple[TriageStatus, str]:
    """Rule A: route on the confidence of the recommended action."""
    if confidence >= CONFIDENCE_AUTO_APPROVE:
        return (
            TriageStatus.AUTO_APPROVED,
            f"High confidence ({confidence:.2f} >= {CONFIDENCE_AUTO_APPROVE:.2f}): "
            "executed automatically in the driver app.",
        )
    if confidence >= CONFIDENCE_DRIVER_CONFIRM:
        return (
            TriageStatus.REQUIRES_DRIVER_CONFIRMATION,
            f"Moderate confidence ({CONFIDENCE_DRIVER_CONFIRM:.2f} <= {confidence:.2f} < "
            f"{CONFIDENCE_AUTO_APPROVE:.2f}): suggested on the driver's screen for manual confirmation.",
        )
    return (
        TriageStatus.ESCALATED_TO_DISPATCH,
        f"Low confidence ({confidence:.2f} < {CONFIDENCE_DRIVER_CONFIRM:.2f}): "
        "ticket escalated to human dispatch control.",
    )


def apply_business_rules(event: DeliveryEventInput, evaluation: EvaluationResult) -> dict[str, Any]:
    """Turn a Jev evaluation into a deterministic operational verdict.

    Precedence is security first, then fraud, then value, then confidence routing:
    B > D > C > A. A hard block must not be softened by a confident model, and a
    fraud flag must not be pre-empted by an OTP step that assumes the phone number
    on file still belongs to the real recipient.
    """
    action = evaluation.recommended_action
    confidence = action.confidence
    risk_level = classify_risk(evaluation.risk_score.score)

    try:
        recommended = FinalAction(action.choice)
    except ValueError:
        # The model answered outside the declared option set; treat as unusable.
        logger.warning("Unrecognised action %r from Jev; falling back to unknown_unclear.", action.choice)
        recommended = FinalAction.UNKNOWN_UNCLEAR

    # --- Rule B: required-signature security guardrail -------------------
    bypass_probability = evaluation.signature_bypass_requested.noul
    if event.requires_signature and bypass_probability > SIGNATURE_BYPASS_THRESHOLD:
        # Never release an unattended or third-party handover when a signature is due.
        final_action = (
            FinalAction.RETURN_TO_HUB
            if recommended in UNATTENDED_ACTIONS or recommended is FinalAction.UNKNOWN_UNCLEAR
            else FinalAction.VERIFY_ID_IN_PERSON
        )
        return {
            "status": TriageStatus.SECURITY_BLOCK,
            "final_action": final_action,
            "risk_level": risk_level,
            "confidence": confidence,
            "applied_rule": AppliedRule.RULE_B_SIGNATURE_GUARDRAIL,
            "applied_rule_detail": (
                f"Signature required and bypass requested (p={bypass_probability:.2f} > "
                f"{SIGNATURE_BYPASS_THRESHOLD:.2f}). Suggested '{action.choice}' blocked; "
                f"forced '{final_action.value}'. The parcel may not be left unattended or with a "
                "neighbour without a signature."
            ),
            "requires_otp": False,
            "auto_dispatch_suspended": True,
        }

    # --- Rule D: ambiguous redirection fraud audit -----------------------
    redirection_probability = evaluation.address_redirection_detected.noul
    if redirection_probability > REDIRECTION_THRESHOLD and recommended is FinalAction.UNKNOWN_UNCLEAR:
        return {
            "status": TriageStatus.FLAGGED_FOR_FRAUD_AUDIT,
            "final_action": FinalAction.HOLD_FOR_FRAUD_REVIEW,
            "risk_level": risk_level,
            "confidence": confidence,
            "applied_rule": AppliedRule.RULE_D_FRAUD_REDIRECTION,
            "applied_rule_detail": (
                f"Address redirection detected (p={redirection_probability:.2f} > "
                f"{REDIRECTION_THRESHOLD:.2f}) while the requested action is ambiguous "
                "('unknown_unclear'). Event flagged for fraud audit and automated re-dispatch suspended."
            ),
            "requires_otp": False,
            "auto_dispatch_suspended": True,
        }

    # --- Rule C: risk control by package value ---------------------------
    if event.package_value_usd > HIGH_VALUE_USD and evaluation.risk_score.score >= HIGH_VALUE_RISK_SCORE:
        return {
            "status": TriageStatus.REQUIRES_OTP_VERIFICATION,
            "final_action": FinalAction.AWAIT_OTP_CONFIRMATION,
            "risk_level": risk_level,
            "confidence": confidence,
            "applied_rule": AppliedRule.RULE_C_HIGH_VALUE_OTP,
            "applied_rule_detail": (
                f"High-value parcel (${event.package_value_usd:,.2f} > ${HIGH_VALUE_USD:,.2f}) at "
                f"risk score {evaluation.risk_score.score:.2f} >= {HIGH_VALUE_RISK_SCORE:.2f}. "
                f"A one-time code must be sent to the customer's phone before '{action.choice}' "
                "is authorised."
            ),
            "requires_otp": True,
            "auto_dispatch_suspended": False,
        }

    # --- Rule A: RLCD confidence routing (default path) ------------------
    status, detail = _route_by_confidence(confidence)
    return {
        "status": status,
        "final_action": recommended,
        "risk_level": risk_level,
        "confidence": confidence,
        "applied_rule": AppliedRule.RULE_A_CONFIDENCE_ROUTING,
        "applied_rule_detail": detail,
        "requires_otp": False,
        "auto_dispatch_suspended": status is TriageStatus.ESCALATED_TO_DISPATCH,
    }


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Warn early about a missing key, and release HTTP pools on shutdown."""
    if not os.getenv("TYPESAFE_API_KEY"):
        logger.warning(
            "TYPESAFE_API_KEY is not set. Requests must supply the key via the "
            "X-TypeSafe-Api-Key header."
        )
    yield
    jev_evaluator.reset_clients()


app = FastAPI(
    title="LogiPulse AI - Logistics Triage & Exceptions Engine",
    description=(
        "Real-time triage of last-mile delivery exceptions. Jev (TypeSafe AI System One) "
        "evaluates the event; deterministic guardrails decide the outcome."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOW_ORIGINS,
    allow_credentials="*" not in ALLOW_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)


async def get_api_key(
    x_typesafe_api_key: Annotated[str | None, Header(alias="X-TypeSafe-Api-Key")] = None,
) -> str | None:
    """Per-request key override.

    Intended for local and demo use, where the Streamlit sidebar holds the key. In a
    shared deployment, drop this dependency and rely on the server-side environment.
    """
    return x_typesafe_api_key.strip() if x_typesafe_api_key else None


@app.exception_handler(JevEvaluationError)
async def jev_error_handler(_: Request, exc: JevEvaluationError) -> JSONResponse:
    """Surface upstream failures with their mapped status instead of a bare 500."""
    logger.error(
        "Jev evaluation failed (%s): %s%s",
        exc.kind,
        exc.detail,
        f" [request_id={exc.request_id}]" if exc.request_id else "",
    )
    body: dict[str, Any] = {"detail": exc.detail, "error": exc.kind}
    if exc.request_id:
        body["request_id"] = exc.request_id
    return JSONResponse(status_code=exc.status_code, content=body)


@app.get("/health", tags=["ops"], summary="Liveness probe")
async def health() -> dict[str, Any]:
    """Report service liveness and whether a server-side key is configured."""
    return {
        "status": "ok",
        "service": "logipulse-ai",
        "version": app.version,
        "model": jev_evaluator.DEFAULT_MODEL,
        "server_api_key_configured": bool(os.getenv("TYPESAFE_API_KEY")),
    }


@app.get("/api/v1/rules", tags=["triage"], summary="Active thresholds")
async def rules() -> dict[str, Any]:
    """Expose the thresholds the engine enforces, so the UI never hardcodes them."""
    return {
        "precedence": ["RULE_B", "RULE_D", "RULE_C", "RULE_A"],
        "rule_a_confidence_routing": {
            "auto_approve_at": CONFIDENCE_AUTO_APPROVE,
            "driver_confirm_at": CONFIDENCE_DRIVER_CONFIRM,
        },
        "rule_b_signature_guardrail": {"bypass_probability_above": SIGNATURE_BYPASS_THRESHOLD},
        "rule_c_high_value_otp": {
            "package_value_usd_above": HIGH_VALUE_USD,
            "risk_score_at_least": HIGH_VALUE_RISK_SCORE,
        },
        "rule_d_fraud_redirection": {
            "redirection_probability_above": REDIRECTION_THRESHOLD,
            "requires_action": FinalAction.UNKNOWN_UNCLEAR.value,
        },
    }


@app.post(
    "/api/v1/triage",
    response_model=TriageResponse,
    tags=["triage"],
    summary="Triage a last-mile delivery event",
)
async def triage(
    event: DeliveryEventInput,
    api_key: Annotated[str | None, Depends(get_api_key)] = None,
) -> TriageResponse:
    """Evaluate a delivery event with Jev and apply the operational guardrails."""
    result = evaluate_delivery_event(event, api_key=api_key)
    evaluation: EvaluationResult = result["evaluation"]
    verdict = apply_business_rules(event, evaluation)

    logger.info(
        "triage %s -> %s / %s (rule=%s, confidence=%.2f, risk=%.2f, %.0fms, $%.8f, request_id=%s)",
        event.tracking_id,
        verdict["status"].value,
        verdict["final_action"].value,
        verdict["applied_rule"].value,
        verdict["confidence"],
        evaluation.risk_score.score,
        evaluation.latency_ms,
        evaluation.cost_usd,
        evaluation.request_id or "-",
    )

    return TriageResponse(
        tracking_id=event.tracking_id,
        latency_ms=evaluation.latency_ms,
        cost_usd=evaluation.cost_usd,
        evaluation=evaluation,
        raw_response=result["raw_response"],
        **verdict,
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(_: Request, exc: Exception) -> JSONResponse:
    """Last-resort handler so an unexpected fault never leaks a stack trace."""
    logger.exception("Unhandled error", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error while triaging the event.", "error": "internal_error"},
    )


if __name__ == "__main__":  # pragma: no cover - convenience entry point
    import uvicorn

    uvicorn.run(
        "api:app",
        host=os.getenv("LOGIPULSE_HOST", "127.0.0.1"),
        port=int(os.getenv("LOGIPULSE_PORT", "8000")),
        reload=True,
    )
