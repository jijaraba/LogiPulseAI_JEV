"""TypeSafe AI (Jev) integration for LogiPulse.

One `system_one` call evaluates the delivery event against four questions in a
single parallel pass - a speculative fan-out. Nothing here makes a business
decision; it only turns an unstructured note into typed primitives. The
deterministic rules live in `api.py`.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Final

# Load a local .env before any configuration below is read, so a key placed there
# is picked up without exporting it by hand. Real environment variables always win:
# `override=False` means an exported value is never clobbered by the file, and the
# dependency is optional so the app still runs where it is not installed.
try:
    from dotenv import load_dotenv

    load_dotenv(override=False)
except ImportError:  # pragma: no cover - python-dotenv is optional
    pass

from typesafe_sdk import constants as ts_constants
from typesafe_sdk import (
    Choice,
    Noul,
    Score,
    SystemOneResponse,
    TypeSafeAPIConnectionError,
    TypeSafeAPIError,
    TypeSafeAPIResponseValidationError,
    TypeSafeAPITimeoutError,
    TypeSafeAuthenticationError,
    TypeSafeClient,
    TypeSafeError,
    TypeSafePermissionDeniedError,
    TypeSafeRateLimitError,
)

from schemas import (
    ChoiceEvaluation,
    DeliveryEventInput,
    EvaluationResult,
    NoulEvaluation,
    ScoreEvaluation,
    TokenUsage,
)

# ---------------------------------------------------------------------------
# Pricing and model configuration
# ---------------------------------------------------------------------------

#: USD per 1M billable input tokens.
COST_PER_1M_INPUT_TOKENS: Final[float] = 0.042
#: USD per 1M output tokens. Output tokens are currently free of charge.
COST_PER_1M_OUTPUT_TOKENS: Final[float] = 0.0

DEFAULT_MODEL: Final[str] = os.getenv("TYPESAFE_DEFAULT_MODEL", "jev-latest")
#: Where calls actually go. Mirrors the SDK's own resolution of TYPESAFE_BASE_URL.
BASE_URL: Final[str] = os.getenv("TYPESAFE_BASE_URL") or ts_constants.DEFAULT_BASE_URL
#: Per-call HTTP timeout. Jev answers in 70-500 ms; anything slower is a network fault.
REQUEST_TIMEOUT_SECONDS: Final[float] = float(os.getenv("LOGIPULSE_JEV_TIMEOUT", "10.0"))

#: Print a readable request/response block to the console for each evaluation.
#: The SDK's own `TYPESAFE_LOG_LEVEL=debug` dumps the raw wire on one long line; this
#: is the same exchange laid out for a human watching the server.
TRACE: Final[bool] = os.getenv("LOGIPULSE_TRACE_JEV", "").strip().lower() in {"1", "true", "yes", "on"}

logger = logging.getLogger("logipulse.jev")

#: Question names, kept as constants so the evaluator and the rules cannot drift apart.
Q_RECOMMENDED_ACTION: Final[str] = "recommended_action"
Q_RISK_SCORE: Final[str] = "risk_score"
Q_SIGNATURE_BYPASS: Final[str] = "signature_bypass_requested"
Q_ADDRESS_REDIRECTION: Final[str] = "address_redirection_detected"


class JevEvaluationError(RuntimeError):
    """A Jev call failed. Carries the HTTP status the API layer should surface."""

    def __init__(
        self,
        detail: str,
        *,
        status_code: int = 502,
        kind: str = "upstream_error",
        request_id: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code
        self.kind = kind
        self.request_id = request_id
        """TypeSafe's request id for the failed call, when the response carried one."""


# ---------------------------------------------------------------------------
# Question definitions (the speculative fan-out)
# ---------------------------------------------------------------------------


def build_questions() -> dict[str, Choice | Score | Noul]:
    """The four questions asked about every delivery event, in one parallel pass."""
    return {
        Q_RECOMMENDED_ACTION: Choice(
            instructions=(
                "Given the customer note and the parcel constraints, what should the driver "
                "do with this parcel right now?"
            ),
            criteria={
                "deliver_doorman": (
                    "Hand the parcel to a doorman, concierge, receptionist, or front-desk staff "
                    "of the building."
                ),
                "deliver_neighbor": (
                    "Leave the parcel with a neighbour, a nearby flat, or another named private "
                    "person at a different door."
                ),
                "leave_secure_spot": (
                    "Leave the parcel unattended in a specific safe place the customer named: "
                    "porch, back door, garage, shed, mailroom, or parcel box."
                ),
                "reschedule_evening": (
                    "Do not deliver now; reattempt later the same day or in another time window "
                    "the customer asked for."
                ),
                "return_to_hub": (
                    "Do not deliver; take the parcel back to the depot, for example because the "
                    "customer refuses it, is away for a long period, or the address is wrong."
                ),
                "unknown_unclear": (
                    "The note is missing, contradictory, unrelated, or too ambiguous to justify "
                    "any of the other options."
                ),
            },
        ),
        Q_RISK_SCORE: Score(
            instructions=(
                "How risky would it be to act on this note automatically, considering loss, "
                "theft, misdelivery, and the chance the note was not written by the real recipient?"
            ),
            criteria=[
                (
                    "No risk. The instruction is routine, internally consistent, and matches an "
                    "ordinary delivery; no value, identity, or address concern."
                ),
                (
                    "Low risk. Slightly unusual but plausible and specific, such as a named "
                    "neighbour or a well-described safe place at the same address."
                ),
                (
                    "Medium risk. The note is vague, urgent, pressures the driver, hands the parcel "
                    "to an unnamed third party, or is a repeated failed attempt."
                ),
                (
                    "High risk. The note shows signs of impersonation or social engineering: it "
                    "redirects the parcel elsewhere, waives verification, contradicts the shipment "
                    "record, or would release a valuable parcel to an unverified person."
                ),
            ],
        ),
        Q_SIGNATURE_BYPASS: Noul(
            instructions=(
                "The note asks for the parcel to be released without collecting a signature from "
                "the named recipient."
            ),
            criteria={
                "true": (
                    "The note asks to skip, waive, pre-authorise, forge, or self-sign the "
                    "signature, or to leave the parcel unattended even though a signature is due."
                ),
                "false": (
                    "The note says nothing about the signature, or accepts that a signature will "
                    "be collected in person."
                ),
            },
        ),
        Q_ADDRESS_REDIRECTION: Noul(
            instructions="The note asks for the parcel to go to a different address than the one on the label.",
            criteria={
                "true": (
                    "The note gives another street, town, workplace, pickup point, or locker, or "
                    "otherwise asks to move the parcel away from the labelled address."
                ),
                "false": (
                    "The note keeps the parcel at the labelled address, including handing it to a "
                    "neighbour or a safe place at that same address."
                ),
            },
        ),
    }


# ---------------------------------------------------------------------------
# Client management
# ---------------------------------------------------------------------------

_CLIENT_LOCK = threading.Lock()
_CLIENTS: dict[tuple[str, str], TypeSafeClient] = {}


def get_client(api_key: str | None = None, model: str | None = None) -> TypeSafeClient:
    """Return a pooled `TypeSafeClient`.

    Clients hold an HTTP connection pool, so one is reused per (key, model) pair
    instead of being rebuilt per request.
    """
    resolved_key = (api_key or os.getenv("TYPESAFE_API_KEY") or "").strip()
    if not resolved_key:
        raise JevEvaluationError(
            "No TypeSafe API key configured. Set TYPESAFE_API_KEY or pass one with the request.",
            status_code=401,
            kind="missing_api_key",
        )
    resolved_model = (model or DEFAULT_MODEL).strip()
    cache_key = (resolved_key, resolved_model)

    with _CLIENT_LOCK:
        client = _CLIENTS.get(cache_key)
        if client is None:
            try:
                client = TypeSafeClient(
                    api_key=resolved_key,
                    model=resolved_model,
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
            except TypeSafeError as exc:
                raise JevEvaluationError(
                    f"Could not initialise the TypeSafe client: {exc}",
                    status_code=401,
                    kind="client_init_failed",
                ) from exc
            _CLIENTS[cache_key] = client
        return client


def reset_clients() -> None:
    """Close and drop every pooled client. Used on shutdown and when keys rotate."""
    with _CLIENT_LOCK:
        for client in _CLIENTS.values():
            try:
                client.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                pass
        _CLIENTS.clear()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def build_state(event: DeliveryEventInput) -> dict[str, Any]:
    """Shape the event as structured state, so Jev sees the constraints and the note together."""
    return {
        "tracking_id": event.tracking_id,
        "customer_note": event.customer_note,
        "package_value_usd": round(event.package_value_usd, 2),
        "requires_signature": event.requires_signature,
        "delivery_attempt": event.delivery_attempt,
    }


def compute_cost_usd(input_tokens: int, output_tokens: int = 0) -> float:
    """Billed cost of one evaluation, at $0.042 / 1M input tokens and free output tokens."""
    return (input_tokens / 1_000_000.0) * COST_PER_1M_INPUT_TOKENS + (
        output_tokens / 1_000_000.0
    ) * COST_PER_1M_OUTPUT_TOKENS


def _coerce_legend(legend: dict[int, Any]) -> dict[int, str]:
    """Rubric entries may be objects or arrays; the dashboard renders them as text."""
    return {level: value if isinstance(value, str) else str(value) for level, value in legend.items()}


def _trace_exchange(
    state: dict[str, Any],
    questions: dict[str, Any],
    evaluation: EvaluationResult,
) -> None:
    """Print the request and its answers as a readable block."""
    line = "─" * 72
    out = [
        "",
        line,
        f"POST {BASE_URL}/v1/systemone   model={evaluation.model_name}",
        f"request_id={evaluation.request_id or '-'}   {evaluation.latency_ms:.0f} ms   "
        f"{evaluation.usage.input_tokens} in / {evaluation.usage.output_tokens} out   "
        f"${evaluation.cost_usd:.8f}",
        line,
        "STATE (lo que ve Jev)",
    ]
    for key, value in state.items():
        out.append(f"  {key:<20} {value!r}")

    out.append(f"QUESTIONS ({len(questions)} en un solo pase paralelo)")
    for name, q in questions.items():
        kind = getattr(q, "type", "?")
        if kind == "choice":
            detail = f"{len(q.criteria)} opciones: {', '.join(q.criteria)}"
        elif kind == "score":
            detail = f"rúbrica de {len(q.criteria)} niveles (0-{len(q.criteria) - 1})"
        else:
            detail = "true/false"
        out.append(f"  {name:<30} {kind:<7} {detail}")

    out.append("ANSWERS")
    action = evaluation.recommended_action
    ranked = sorted(action.probabilities.items(), key=lambda kv: kv[1], reverse=True)
    out.append(f"  recommended_action           choice  {action.choice} (confianza {action.confidence:.1%})")
    for label, prob in ranked:
        bar = "█" * max(0, round(prob * 24))
        out.append(f"      {label:<22} {prob:6.1%} {bar}")
    risk = evaluation.risk_score
    out.append(f"  risk_score                   score   {risk.score:.2f} / 3 (confianza {risk.confidence:.1%})")
    for level, prob in sorted(risk.probabilities.items()):
        bar = "█" * max(0, round(prob * 24))
        out.append(f"      nivel {level:<17} {prob:6.1%} {bar}")
    for name, noul in (
        ("signature_bypass_requested", evaluation.signature_bypass_requested.noul),
        ("address_redirection_detected", evaluation.address_redirection_detected.noul),
    ):
        bar = "█" * max(0, round(noul * 24))
        out.append(f"  {name:<30} noul    {noul:6.1%} {bar}")
    out.append(line)
    logger.info("\n".join(out))


def _optional_request_id(response: SystemOneResponse) -> str | None:
    """Return TypeSafe's request id, or `None` when the response carried no header.

    `SystemOneResponse.request_id` raises `TypeSafeError` rather than returning `None`
    when the header is absent, so reading it bare would turn a missing piece of
    optional metadata into a failed triage.
    """
    try:
        return response.request_id
    except TypeSafeError:
        return None


def _translate_error(exc: Exception) -> JevEvaluationError:
    """Map an SDK exception onto the HTTP status the API layer should return.

    Order matters: `TypeSafeAPITimeoutError` subclasses `TypeSafeAPIConnectionError`,
    and the specific 4xx errors subclass `TypeSafeAPIError`, so the narrowest type is
    always tested first.
    """
    # Only HTTP-level failures carry a response, and therefore a request id.
    request_id = getattr(exc, "request_id", None)
    if isinstance(exc, TypeSafeAuthenticationError):
        return JevEvaluationError(
            "TypeSafe rejected the API key.", status_code=401, kind="authentication_error", request_id=request_id
        )
    if isinstance(exc, TypeSafePermissionDeniedError):
        return JevEvaluationError(
            "The API key is not allowed to use this model.", status_code=403, kind="permission_denied", request_id=request_id
        )
    if isinstance(exc, TypeSafeRateLimitError):
        return JevEvaluationError(
            "Rate limit exceeded at TypeSafe; retry shortly.", status_code=429, kind="rate_limited", request_id=request_id
        )
    if isinstance(exc, TypeSafeAPITimeoutError):
        return JevEvaluationError(
            f"Jev did not answer within {REQUEST_TIMEOUT_SECONDS:.1f}s.",
            status_code=504,
            kind="upstream_timeout",
            request_id=request_id,
        )
    if isinstance(exc, TypeSafeAPIConnectionError):
        return JevEvaluationError(
            f"Could not reach the TypeSafe API: {exc}", status_code=503, kind="upstream_unreachable", request_id=request_id
        )
    if isinstance(exc, TypeSafeAPIResponseValidationError):
        return JevEvaluationError(
            f"Jev returned a response the SDK could not validate: {exc}",
            status_code=502,
            kind="invalid_upstream_response",
            request_id=request_id,
        )
    if isinstance(exc, TypeSafeAPIError):
        return JevEvaluationError(f"TypeSafe API error: {exc}", status_code=502, kind="api_error", request_id=request_id)
    if isinstance(exc, TypeSafeError):
        return JevEvaluationError(f"TypeSafe SDK error: {exc}", status_code=500, kind="sdk_error")
    return JevEvaluationError(f"Unexpected error during evaluation: {exc}", status_code=500, kind="internal_error")


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def evaluate_delivery_event(
    event: DeliveryEventInput,
    *,
    api_key: str | None = None,
    model: str | None = None,
) -> dict:
    """Evaluate one delivery event against all four Jev questions in a single pass.

    Args:
        event: The delivery event to triage.
        api_key: Optional key override; falls back to `TYPESAFE_API_KEY`.
        model: Optional model override; falls back to `TYPESAFE_DEFAULT_MODEL` or `jev-latest`.

    Returns:
        A dict with two keys: `evaluation`, an `EvaluationResult` carrying the typed
        primitives plus latency and cost, and `raw_response`, the verbatim JSON body.

    Raises:
        JevEvaluationError: The key is missing, or the call failed upstream.
    """
    client = get_client(api_key=api_key, model=model)
    state = build_state(event)
    questions = build_questions()

    started = time.perf_counter()
    try:
        response = client.system_one(state=state, questions=questions)
    except Exception as exc:  # noqa: BLE001 - re-raised as a typed domain error
        raise _translate_error(exc) from exc
    latency_ms = (time.perf_counter() - started) * 1000.0

    try:
        action_answer = response.choices[Q_RECOMMENDED_ACTION]
        risk_answer = response.scores[Q_RISK_SCORE]
        bypass_answer = response.nouls[Q_SIGNATURE_BYPASS]
        redirection_answer = response.nouls[Q_ADDRESS_REDIRECTION]
    except KeyError as exc:
        raise JevEvaluationError(
            f"Jev response is missing the answer for {exc}.",
            status_code=502,
            kind="incomplete_upstream_response",
        ) from exc

    input_tokens = response.usage.input_tokens or 0
    output_tokens = response.usage.output_tokens or 0

    evaluation = EvaluationResult(
        recommended_action=ChoiceEvaluation(
            choice=action_answer.choice,
            confidence=action_answer.confidence,
            probabilities=dict(action_answer.probabilities),
        ),
        risk_score=ScoreEvaluation(
            score=risk_answer.score,
            confidence=risk_answer.confidence,
            probabilities=dict(risk_answer.probabilities),
            legend=_coerce_legend(dict(risk_answer.legend)),
        ),
        signature_bypass_requested=NoulEvaluation(noul=bypass_answer.noul),
        address_redirection_detected=NoulEvaluation(noul=redirection_answer.noul),
        model_name=response.model,
        request_id=_optional_request_id(response),
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        latency_ms=round(latency_ms, 2),
        cost_usd=compute_cost_usd(input_tokens, output_tokens),
    )

    if TRACE:
        _trace_exchange(state, questions, evaluation)

    return {
        "evaluation": evaluation,
        "raw_response": response.model_dump(mode="json"),
        "request_payload": {"state": state, "model": model or DEFAULT_MODEL},
    }
