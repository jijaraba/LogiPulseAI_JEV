"""LogiPulse AI - operational dashboard.

A dispatch console over the triage API: pick or write a delivery event, send it,
and read back what Jev saw and which guardrail decided the outcome.

Run with:  streamlit run dashboard.py
"""

from __future__ import annotations

import json
import os
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

import plotly.graph_objects as go
import requests
import streamlit as st

# ---------------------------------------------------------------------------
# Design tokens - the TypeSafe AI visual language (typesafe.ai)
#
# Their system: a rose plane, near-black ink, white "windows" with black title
# bars, hard edges everywhere, and terminal monospace type. Data lives on the
# white panels, so the chart palette is validated against #fefefe.
# ---------------------------------------------------------------------------

PINK: Final[str] = "#f386a1"           # the brand plane
INK: Final[str] = "#1e1e1e"            # primary ink and every border
INK_SECONDARY: Final[str] = "#4a4a4a"
INK_MUTED: Final[str] = "#8a8a8a"
SURFACE: Final[str] = "#fefefe"        # panel and chart surface
SURFACE_INSET: Final[str] = "#f4f4f2"
GRID: Final[str] = "#e3e3e1"
AXIS: Final[str] = "#1e1e1e"

# Ordinal ink ramp: the selected option is emphasised, alternatives recede.
# Validated on #fefefe - monotone lightness, step gap, light end at 3.42:1.
SERIES_SELECTED: Final[str] = "#1e1e1e"
SERIES_ALTERNATIVE: Final[str] = "#8a8a8a"

# Reserved status palette. Never used to identify a series. "Good" is TypeSafe's
# green; warning and serious sit below 3:1 on white by design, so every status
# ships with an icon and a label and never carries meaning by colour alone.
STATUS_GOOD: Final[str] = "#03aa5c"
STATUS_WARNING: Final[str] = "#fab219"
STATUS_SERIOUS: Final[str] = "#ec835a"
STATUS_CRITICAL: Final[str] = "#d03b3b"

# Terminal mono for chrome and data; a tight grotesk for the display line.
# Both degrade to system faces when the webfont cannot be fetched.
MONO: Final[str] = (
    '"JetBrains Mono", ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace'
)
DISPLAY: Final[str] = '"Archivo", "Helvetica Neue", Helvetica, Arial, system-ui, sans-serif'
FONT_STACK: Final[str] = MONO  # charts are chrome, so they wear the mono face

#: Status -> (colour, icon, one-line meaning). Colour never carries meaning alone;
#: the icon and the label always travel with it.
STATUS_STYLE: Final[dict[str, tuple[str, str, str]]] = {
    "AUTO_APPROVED": (STATUS_GOOD, "✅", "Executed automatically in the driver app."),
    "REQUIRES_DRIVER_CONFIRMATION": (STATUS_WARNING, "🖐️", "Suggested on the driver's screen for manual confirmation."),
    "ESCALATED_TO_DISPATCH": (STATUS_SERIOUS, "📡", "Escalated as a ticket to human dispatch control."),
    "REQUIRES_OTP_VERIFICATION": (STATUS_SERIOUS, "🔐", "A one-time code must be confirmed before release."),
    "SECURITY_BLOCK": (STATUS_CRITICAL, "⛔", "Blocked by the signature security guardrail."),
    "FLAGGED_FOR_FRAUD_AUDIT": (STATUS_CRITICAL, "🚨", "Held for fraud audit; automated re-dispatch suspended."),
}

RISK_STYLE: Final[dict[str, tuple[str, str]]] = {
    "NONE": (STATUS_GOOD, "🟢"),
    "LOW": (STATUS_WARNING, "🟡"),
    "MEDIUM": (STATUS_SERIOUS, "🟠"),
    "HIGH": (STATUS_CRITICAL, "🔴"),
}

#: Compact labels for the metric tiles, which truncate anything long.
STATUS_SHORT: Final[dict[str, str]] = {
    "AUTO_APPROVED": "Auto-approved",
    "REQUIRES_DRIVER_CONFIRMATION": "Driver confirm",
    "ESCALATED_TO_DISPATCH": "To dispatch",
    "REQUIRES_OTP_VERIFICATION": "OTP required",
    "SECURITY_BLOCK": "Security block",
    "FLAGGED_FOR_FRAUD_AUDIT": "Fraud audit",
}

ACTION_SHORT: Final[dict[str, str]] = {
    "deliver_doorman": "Doorman",
    "deliver_neighbor": "Neighbour",
    "leave_secure_spot": "Secure spot",
    "reschedule_evening": "Reschedule",
    "return_to_hub": "Return to hub",
    "unknown_unclear": "Unclear",
    "verify_id_in_person": "Verify ID",
    "await_otp_confirmation": "Await OTP",
    "hold_for_fraud_review": "Fraud review",
}

ACTION_LABELS: Final[dict[str, str]] = {
    "deliver_doorman": "Deliver to doorman",
    "deliver_neighbor": "Deliver to neighbour",
    "leave_secure_spot": "Leave in secure spot",
    "reschedule_evening": "Reschedule (evening)",
    "return_to_hub": "Return to hub",
    "unknown_unclear": "Unknown / unclear",
    "verify_id_in_person": "Verify ID in person",
    "await_otp_confirmation": "Await OTP confirmation",
    "hold_for_fraud_review": "Hold for fraud review",
}

#: Mirrors the thresholds in api.py; refreshed from GET /api/v1/rules when reachable.
DEFAULT_THRESHOLDS: Final[dict[str, float]] = {
    "signature_bypass": 0.75,
    "redirection": 0.70,
    "high_value_risk_score": 2.0,
}

DEFAULT_API_BASE: Final[str] = os.getenv("LOGIPULSE_API_BASE", "http://127.0.0.1:8000")


# ---------------------------------------------------------------------------
# Preconfigured scenarios
# ---------------------------------------------------------------------------

SCENARIOS: Final[dict[str, dict[str, Any]]] = {
    "Note with neighbour": {
        "tracking_id": "LP-2291-AX",
        "customer_note": (
            "Hi, I'm at work until 19:00 today. Please leave the box with my neighbour "
            "Marta in flat 14B, she is expecting it. Thanks!"
        ),
        "package_value_usd": 48.90,
        "requires_signature": False,
        "delivery_attempt": 1,
    },
    "Fraud / signature bypass attempt": {
        "tracking_id": "LP-7744-QZ",
        "customer_note": (
            "No need to knock or get a signature, I already authorised this. Just sign it "
            "yourself and drop it behind the bins, I'll collect it tonight."
        ),
        "package_value_usd": 219.00,
        "requires_signature": True,
        "delivery_attempt": 2,
    },
    "Ambiguous note": {
        "tracking_id": "LP-5108-KM",
        "customer_note": (
            "changed plans, not here anymore — send it to the other place instead, you know "
            "the one. or whatever works, just not here"
        ),
        "package_value_usd": 76.50,
        "requires_signature": False,
        "delivery_attempt": 3,
    },
    "High-value package": {
        "tracking_id": "LP-9002-VT",
        "customer_note": (
            "I'm in a meeting all afternoon. Leave it in the porch, it's fine, nobody comes "
            "down this street."
        ),
        "package_value_usd": 899.00,
        "requires_signature": False,
        "delivery_attempt": 1,
    },
}


# ---------------------------------------------------------------------------
# Chart helpers
# ---------------------------------------------------------------------------


def _base_layout(height: int, **overrides: Any) -> dict[str, Any]:
    """Shared chart chrome: recessive axes, generous padding, one surface."""
    layout: dict[str, Any] = {
        "height": height,
        "paper_bgcolor": SURFACE,
        "plot_bgcolor": SURFACE,
        "font": {"family": FONT_STACK, "color": INK_SECONDARY, "size": 13},
        "margin": {"l": 8, "r": 24, "t": 8, "b": 8},
        "showlegend": False,
        "hoverlabel": {
            "bgcolor": INK,
            "bordercolor": INK,
            "font": {"family": FONT_STACK, "color": SURFACE, "size": 12},
        },
    }
    layout.update(overrides)
    return layout


def action_probability_chart(probabilities: dict[str, float], selected: str) -> go.Figure:
    """Horizontal bars of the RLCD distribution over the closed action set.

    One series, so no legend: the title names it. The selected label is emphasised
    with a darker step of the same hue rather than a second identity colour.
    """
    ordered = sorted(probabilities.items(), key=lambda item: item[1])
    labels = [ACTION_LABELS.get(name, name) for name, _ in ordered]
    values = [value for _, value in ordered]
    colors = [SERIES_SELECTED if name == selected else SERIES_ALTERNATIVE for name, _ in ordered]

    figure = go.Figure(
        go.Bar(
            x=values,
            y=labels,
            orientation="h",
            marker={"color": colors, "cornerradius": 0},
            width=0.58,
            text=[f"{value:.1%}" for value in values],
            textposition="outside",
            textfont={"family": FONT_STACK, "color": INK_SECONDARY, "size": 12},
            cliponaxis=False,
            customdata=[name for name, _ in ordered],
            hovertemplate="<b>%{y}</b><br>%{customdata}<br>Probability %{x:.2%}<extra></extra>",
        )
    )
    figure.update_layout(
        **_base_layout(
            height=40 * len(ordered) + 70,
            bargap=0.34,
            margin={"l": 16, "r": 88, "t": 10, "b": 30},
            xaxis={
                "range": [0, 1],
                "tickformat": ".0%",
                "showgrid": True,
                "gridcolor": GRID,
                "gridwidth": 1,
                "zeroline": False,
                "linecolor": AXIS,
                "linewidth": 1,
                "tickfont": {"family": FONT_STACK, "color": INK_MUTED, "size": 11},
            },
            yaxis={
                "showgrid": False,
                "zeroline": False,
                "linecolor": AXIS,
                "linewidth": 1,
                "tickfont": {"family": FONT_STACK, "color": INK_SECONDARY, "size": 12},
            },
        )
    )
    return figure


def _tint(hex_color: str, alpha: float) -> str:
    """A translucent wash of a status colour, for gauge bands behind the value."""
    hex_color = hex_color.lstrip("#")
    red, green, blue = (int(hex_color[index : index + 2], 16) for index in (0, 2, 4))
    return f"rgba({red},{green},{blue},{alpha})"


def risk_gauge(score: float, risk_level: str, otp_threshold: float) -> go.Figure:
    """Gauge over the 0-3 risk rubric, with the high-value OTP threshold marked."""
    color, _ = RISK_STYLE.get(risk_level, (STATUS_SERIOUS, "🟠"))
    figure = go.Figure(
        go.Indicator(
            mode="gauge+number",
            value=score,
            number={
                "font": {"family": FONT_STACK, "color": INK, "size": 38},
                "valueformat": ".2f",
            },
            gauge={
                "axis": {
                    "range": [0, 3],
                    "tickvals": [0, 1, 2, 3],
                    "tickcolor": INK,
                    "tickfont": {"family": FONT_STACK, "color": INK_MUTED, "size": 11},
                },
                "bar": {"color": color, "thickness": 0.62, "line": {"color": INK, "width": 1}},
                "bgcolor": SURFACE,
                "borderwidth": 0,
                "steps": [
                    {"range": [0, 0.5], "color": _tint(STATUS_GOOD, 0.16)},
                    {"range": [0.5, 1.5], "color": _tint(STATUS_WARNING, 0.18)},
                    {"range": [1.5, 2.5], "color": _tint(STATUS_SERIOUS, 0.20)},
                    {"range": [2.5, 3], "color": _tint(STATUS_CRITICAL, 0.20)},
                ],
                "threshold": {
                    "line": {"color": INK, "width": 2},
                    "thickness": 0.9,
                    "value": otp_threshold,
                },
            },
        )
    )
    figure.update_layout(**_base_layout(height=232, margin={"l": 24, "r": 24, "t": 16, "b": 8}))
    return figure


def noul_meter(probability: float, threshold: float) -> go.Figure:
    """A horizontal meter for one Noul probability, with its guardrail threshold marked.

    A bar on a track rather than Plotly's bullet indicator: the bullet spends most of
    its width on the readout and renders its percentage ticks too small to read at
    this height.
    """
    breached = probability > threshold
    figure = go.Figure()
    figure.add_trace(
        go.Bar(
            x=[1.0],
            y=["p"],
            orientation="h",
            marker={"color": _tint(INK_MUTED, 0.09), "cornerradius": 0},
            width=0.46,
            hoverinfo="skip",
        )
    )
    figure.add_trace(
        go.Bar(
            x=[probability],
            y=["p"],
            orientation="h",
            marker={"color": STATUS_CRITICAL if breached else SERIES_SELECTED, "cornerradius": 0},
            width=0.46,
            text=[f"{probability:.0%}"],
            textposition="outside",
            textfont={"family": FONT_STACK, "color": INK, "size": 14},
            cliponaxis=False,
            hovertemplate=f"Probability %{{x:.1%}}<br>Threshold {threshold:.2f}<extra></extra>",
        )
    )
    figure.add_vline(x=threshold, line={"color": INK, "width": 2, "dash": "dot"})
    figure.add_annotation(
        x=threshold,
        y=1.0,
        yref="paper",
        text=f"THRESHOLD {threshold:.2f}",
        showarrow=False,
        yanchor="bottom",
        font={"family": FONT_STACK, "color": INK_SECONDARY, "size": 11},
    )
    figure.update_layout(
        **_base_layout(
            height=104,
            barmode="overlay",
            bargap=0.1,
            margin={"l": 20, "r": 56, "t": 26, "b": 28},  # room for the 0% tick
            xaxis={
                "range": [0, 1.0],
                "tickformat": ".0%",
                "tickvals": [0, 0.25, 0.5, 0.75, 1.0],
                "showgrid": False,
                "zeroline": False,
                "linecolor": AXIS,
                "linewidth": 1,
                "tickfont": {"family": FONT_STACK, "color": INK_MUTED, "size": 11},
            },
            yaxis={"showticklabels": False, "showgrid": False, "zeroline": False, "fixedrange": True},
        )
    )
    return figure


# ---------------------------------------------------------------------------
# API access
# ---------------------------------------------------------------------------


@st.cache_data(ttl=60, show_spinner=False)
def fetch_thresholds(api_base: str) -> dict[str, float]:
    """Read live thresholds from the API so the UI never hardcodes a stale number."""
    try:
        response = requests.get(f"{api_base.rstrip('/')}/api/v1/rules", timeout=5)
        response.raise_for_status()
        rules = response.json()
        return {
            "signature_bypass": float(rules["rule_b_signature_guardrail"]["bypass_probability_above"]),
            "redirection": float(rules["rule_d_fraud_redirection"]["redirection_probability_above"]),
            "high_value_risk_score": float(rules["rule_c_high_value_otp"]["risk_score_at_least"]),
        }
    except (requests.RequestException, KeyError, ValueError, TypeError):
        return dict(DEFAULT_THRESHOLDS)


def call_triage(api_base: str, api_key: str, payload: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """POST the event. Returns `(response, None)` on success or `(None, message)` on failure."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-TypeSafe-Api-Key"] = api_key
    try:
        response = requests.post(
            f"{api_base.rstrip('/')}/api/v1/triage",
            json=payload,
            headers=headers,
            timeout=30,
        )
    except requests.ConnectionError:
        return None, f"Could not reach the triage API at {api_base}. Is `uvicorn api:app` running?"
    except requests.Timeout:
        return None, "The triage API did not answer within 30 s."
    except requests.RequestException as exc:
        return None, f"Request failed: {exc}"

    if response.status_code >= 400:
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        return None, f"HTTP {response.status_code}: {detail}"
    try:
        return response.json(), None
    except ValueError:
        return None, "The triage API returned a body that is not valid JSON."


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

st.set_page_config(
    page_title="LogiPulse AI - Triage & Exceptions",
    page_icon="📦",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    f"""
    <style>
      @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@300;400;500;700&family=Archivo:wght@600;700;800&display=swap');

      /* ---- The plane ---------------------------------------------------- */
      .stApp {{ background: {PINK}; }}
      .stApp {{ color: {INK}; }}
      .stApp p, .stApp label, .stApp li,
      .stApp div:not([data-testid="stIconMaterial"]),
      .stApp span:not([data-testid="stIconMaterial"]) {{
        font-family: {MONO};
      }}
      /* Material icons are a ligature font - never restyle their family. */
      [data-testid="stIconMaterial"] {{
        font-family: "Material Symbols Rounded", "Material Icons" !important;
      }}
      .block-container {{ padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1500px; }}
      header[data-testid="stHeader"] {{ background: transparent; }}

      /* ---- Masthead ------------------------------------------------------ */
      .ts-mast {{
        position: relative; padding: 26px 24px 22px 24px; margin-bottom: 22px;
      }}
      /* Crop marks: the corner brackets that frame every section on typesafe.ai */
      .ts-mast::before, .ts-mast::after {{
        content: ""; position: absolute; width: 15px; height: 15px; pointer-events: none;
      }}
      .ts-mast::before {{ top: 0; left: 0; border-top: 1px solid {INK}; border-left: 1px solid {INK}; }}
      .ts-mast::after  {{ top: 0; right: 0; border-top: 1px solid {INK}; border-right: 1px solid {INK}; }}
      .ts-mast-in {{ position: relative; }}
      .ts-mast-in::before, .ts-mast-in::after {{
        content: ""; position: absolute; width: 15px; height: 15px; pointer-events: none; bottom: -22px;
      }}
      .ts-mast-in::before {{ left: -24px; border-bottom: 1px solid {INK}; border-left: 1px solid {INK}; }}
      .ts-mast-in::after  {{ right: -24px; border-bottom: 1px solid {INK}; border-right: 1px solid {INK}; }}

      .ts-display {{
        font-family: {DISPLAY} !important; font-weight: 800; font-size: 2.9rem; line-height: 0.96;
        letter-spacing: -0.035em; color: {INK}; margin: 0 0 10px 0;
      }}
      .ts-lede {{
        font-family: {MONO}; font-weight: 300; font-size: 0.86rem; line-height: 1.6;
        color: {INK}; max-width: 62ch; margin: 0;
      }}
      .ts-chip {{
        display: inline-block; background: {INK}; color: {SURFACE} !important;
        font-family: {MONO}; font-size: 0.68rem; font-weight: 500;
        letter-spacing: 0.10em; text-transform: uppercase;
        padding: 3px 8px; margin-bottom: 12px;
      }}

      /* ---- Eyebrow: mono label behind a hairline rule --------------------- */
      .ts-eyebrow {{
        font-family: {MONO}; font-size: 0.68rem; font-weight: 500; letter-spacing: 0.12em;
        text-transform: uppercase; color: {INK};
        border-left: 1px solid {INK}; padding: 1px 0 1px 10px; margin: 0 0 3px 0;
      }}
      .ts-note {{
        font-family: {MONO}; font-weight: 300; font-size: 0.74rem; color: {INK};
        padding-left: 11px; margin: 0 0 10px 0; opacity: 0.78;
      }}

      /* ---- Windows: every container is a panel with a black title bar ----- */
      div[data-testid="stVerticalBlockBorderWrapper"] {{
        background: {SURFACE}; border: 1px solid {INK}; border-radius: 0 !important;
        box-shadow: 3px 3px 0 rgba(30,30,30,0.16);
      }}
      .ts-titlebar, .ts-titlebar span {{ color: {SURFACE} !important; }}
      .ts-titlebar {{
        background: {INK};
        font-family: {MONO}; font-size: 0.68rem; font-weight: 500;
        letter-spacing: 0.12em; text-transform: uppercase;
        padding: 4px 10px; margin: -1rem -1rem 0.85rem -1rem;
        display: flex; justify-content: space-between; gap: 12px;
      }}
      .ts-titlebar .ts-titlebar-meta, .ts-banner-bar .ts-titlebar-meta {{
        color: {PINK} !important; font-weight: 300; letter-spacing: 0.06em;
      }}

      /* ---- Stat tiles ----------------------------------------------------- */
      .ts-tile {{ border: 1px solid {INK}; background: {SURFACE}; box-shadow: 3px 3px 0 rgba(30,30,30,0.16); }}
      .ts-tile-bar {{
        background: {INK}; color: {SURFACE} !important; font-family: {MONO}; font-size: 0.63rem;
        font-weight: 500; letter-spacing: 0.12em; text-transform: uppercase; padding: 3px 9px;
      }}
      .ts-tile-body {{ padding: 12px 11px 13px 11px; }}
      .ts-tile-value {{
        font-family: {MONO}; font-size: 1.18rem; font-weight: 500; color: {INK};
        line-height: 1.2; display: block; word-break: break-word;
      }}
      .ts-tile-sub {{
        font-family: {MONO}; font-size: 0.66rem; font-weight: 300; color: {INK};
        opacity: 0.62; margin-top: 4px; display: block;
      }}

      /* ---- Applied-rule banner -------------------------------------------- */
      .ts-banner {{ border: 1px solid {INK}; background: {SURFACE}; margin: 4px 0 20px 0;
                    box-shadow: 3px 3px 0 rgba(30,30,30,0.16); }}
      .ts-banner-bar, .ts-banner-bar span {{ color: {SURFACE} !important; }}
      .ts-banner-bar {{
        background: {INK}; font-family: {MONO}; font-size: 0.68rem;
        font-weight: 500; letter-spacing: 0.12em; text-transform: uppercase;
        padding: 4px 11px; display: flex; justify-content: space-between; gap: 12px;
      }}
      .ts-banner-body {{
        padding: 12px 14px; font-family: {MONO}; font-weight: 300; font-size: 0.80rem;
        line-height: 1.6; color: {INK}; border-left: 4px solid;
      }}
      .ts-flag {{
        border: 1px solid {INK}; background: {SURFACE}; padding: 8px 12px; margin-bottom: 10px;
        font-family: {MONO}; font-size: 0.76rem; font-weight: 400; color: {INK};
        border-left-width: 4px; border-left-style: solid;
      }}

      /* ---- Sidebar: the inverted terminal --------------------------------- */
      section[data-testid="stSidebar"] {{ background: {INK}; border-right: 1px solid {INK}; }}
      section[data-testid="stSidebar"] *:not([data-testid="stIconMaterial"]) {{
        color: {SURFACE} !important; font-family: {MONO};
      }}
      section[data-testid="stSidebar"] [data-testid="stIconMaterial"] {{ color: {SURFACE} !important; }}
      section[data-testid="stSidebar"] hr {{ border-color: rgba(254,254,254,0.22); }}
      section[data-testid="stSidebar"] h2, section[data-testid="stSidebar"] h3 {{
        font-family: {MONO} !important; font-size: 0.70rem !important; font-weight: 500 !important;
        letter-spacing: 0.12em; text-transform: uppercase; opacity: 0.75;
      }}
      .ts-side-brand {{
        font-family: {DISPLAY} !important; font-weight: 800; font-size: 1.32rem; letter-spacing: -0.02em;
        color: {SURFACE} !important; line-height: 1.1;
      }}
      section[data-testid="stSidebar"] .ts-side-sub {{
        font-family: {MONO}; font-size: 0.66rem; font-weight: 300; letter-spacing: 0.06em;
        color: {PINK} !important; text-transform: uppercase;
      }}
      .ts-rules {{ font-family: {MONO}; font-size: 0.70rem; font-weight: 300; line-height: 1.85; }}
      section[data-testid="stSidebar"] .ts-rules b {{ color: {PINK} !important; font-weight: 500; }}
      section[data-testid="stSidebar"] .ts-rules span {{ color: rgba(254,254,254,0.62) !important; }}

      /* ---- Controls: hard edges, mono, ink borders ------------------------ */
      .stTextInput input, .stTextArea textarea, .stNumberInput input,
      div[data-baseweb="select"] > div {{
        border-radius: 0 !important; border: 1px solid {INK} !important;
        background: {SURFACE} !important; color: {INK} !important;
        font-family: {MONO} !important; font-size: 0.80rem !important;
      }}
      section[data-testid="stSidebar"] .stTextInput input,
      section[data-testid="stSidebar"] div[data-baseweb="select"] > div {{
        background: #2b2b2b !important; color: {SURFACE} !important;
        border: 1px solid rgba(254,254,254,0.34) !important;
      }}
      .stTextInput input:focus, .stTextArea textarea:focus, .stNumberInput input:focus {{
        outline: 2px solid {PINK} !important; outline-offset: -2px;
      }}
      .stButton button, .stFormSubmitButton button {{
        border-radius: 0 !important; border: 1px solid {INK} !important;
        background: {INK} !important; color: {SURFACE} !important;
        font-family: {MONO} !important; font-size: 0.76rem !important; font-weight: 500 !important;
        letter-spacing: 0.14em; text-transform: uppercase; padding: 9px 16px !important;
        box-shadow: 3px 3px 0 rgba(30,30,30,0.22);
      }}
      .stButton button *, .stFormSubmitButton button * {{
        color: {SURFACE} !important; font-family: {MONO} !important;
        letter-spacing: 0.14em; text-transform: uppercase;
      }}
      .stButton button:hover, .stFormSubmitButton button:hover {{
        background: {PINK} !important; color: {INK} !important; box-shadow: 1px 1px 0 rgba(30,30,30,0.3);
      }}
      .stButton button:hover *, .stFormSubmitButton button:hover * {{ color: {INK} !important; }}

      /* ---- Expanders & tables --------------------------------------------- */
      details, div[data-testid="stExpander"] {{
        border-radius: 0 !important; border: 1px solid {INK} !important; background: {SURFACE};
        box-shadow: 3px 3px 0 rgba(30,30,30,0.16);
      }}
      div[data-testid="stExpander"] summary {{
        font-family: {MONO} !important; font-size: 0.72rem !important; font-weight: 500 !important;
        letter-spacing: 0.10em; text-transform: uppercase;
      }}
      /* Streamlit's dataframe paints itself from the theme background, which is the
         pink plane here. These tables are small and fixed, so they are rendered as
         plain HTML instead and kept on the white panel surface with the charts. */
      table.ts-table {{
        width: 100%; border-collapse: collapse; background: {SURFACE};
        border: 1px solid {INK}; font-family: {MONO}; font-size: 0.74rem; margin-bottom: 4px;
      }}
      table.ts-table thead th {{
        background: {INK}; color: {SURFACE} !important; text-align: left;
        font-weight: 500; letter-spacing: 0.08em; text-transform: uppercase;
        font-size: 0.66rem; padding: 5px 9px; white-space: nowrap;
      }}
      table.ts-table tbody td {{
        border-top: 1px solid {GRID}; padding: 6px 9px; color: {INK};
        vertical-align: top; background: {SURFACE};
        overflow-wrap: anywhere; white-space: normal;
      }}
      .ts-table-wrap {{ overflow-x: auto; }}
      table.ts-table tbody tr.ts-row-on td {{ background: {SURFACE_INSET}; font-weight: 500; }}
      table.ts-table td.ts-num {{ font-variant-numeric: tabular-nums; white-space: nowrap; }}
      code, pre, .stCode {{ font-family: {MONO} !important; border-radius: 0 !important; }}
      /* Inline code would otherwise inherit the pink plane as its own chip. */
      :not(pre) > code {{
        background: {INK} !important; color: {SURFACE} !important;
        padding: 1px 6px; font-size: 0.74rem;
      }}
      .stCode > div {{ border: 1px solid {GRID}; border-radius: 0 !important; background: {SURFACE_INSET} !important; }}
      hr {{ border-color: {INK}; opacity: 0.30; }}
      .js-plotly-plot .plotly {{ background: transparent; }}
    </style>
    """,
    unsafe_allow_html=True,
)


def titlebar(title: str, meta: str = "") -> None:
    """The black window title bar that opens every panel."""
    right = f'<span class="ts-titlebar-meta">{meta}</span>' if meta else ""
    st.markdown(f'<div class="ts-titlebar"><span>{title}</span>{right}</div>', unsafe_allow_html=True)


def render_table(
    headers: list[str],
    rows: list[tuple[list[str], bool]],
    numeric: set[int] | None = None,
) -> None:
    """A small static table on the panel surface.

    Each row is `(cells, highlighted)`. Columns listed in `numeric` are tabular and
    never wrap so the figures line up; every other column wraps, so a long prose
    cell grows the row instead of pushing the table past its panel.
    """
    numeric = numeric or set()
    head = "".join(f"<th>{h}</th>" for h in headers)
    body = []
    for cells, highlighted in rows:
        tds = "".join(
            f'<td class="ts-num">{c}</td>' if i in numeric else f"<td>{c}</td>"
            for i, c in enumerate(cells)
        )
        body.append(f'<tr class="{"ts-row-on" if highlighted else ""}">{tds}</tr>')
    st.markdown(
        f'<div class="ts-table-wrap"><table class="ts-table">'
        f'<thead><tr>{head}</tr></thead><tbody>{"".join(body)}</tbody></table></div>',
        unsafe_allow_html=True,
    )


def eyebrow(label: str, note: str = "") -> None:
    """A mono section label behind a hairline rule, as used across typesafe.ai."""
    st.markdown(f'<div class="ts-eyebrow">{label}</div>', unsafe_allow_html=True)
    if note:
        st.markdown(f'<div class="ts-note">{note}</div>', unsafe_allow_html=True)


# --- Session state ---------------------------------------------------------

for field, value in SCENARIOS["Note with neighbour"].items():
    st.session_state.setdefault(f"form_{field}", value)
st.session_state.setdefault("result", None)
st.session_state.setdefault("last_payload", None)


def _load_scenario() -> None:
    """Copy the selected preset into the form fields."""
    preset = SCENARIOS[st.session_state["scenario"]]
    for key, value in preset.items():
        st.session_state[f"form_{key}"] = value


# --- Sidebar ---------------------------------------------------------------

with st.sidebar:
    st.markdown(
        '<div class="ts-side-brand">LogiPulse AI</div>'
        '<div class="ts-side-sub">Triage &amp; Exceptions 1.0</div>',
        unsafe_allow_html=True,
    )
    st.divider()

    st.subheader("Connection")
    api_base = st.text_input(
        "Triage API base URL",
        value=DEFAULT_API_BASE,
        help="Where `uvicorn api:app` is listening.",
    )
    api_key = st.text_input(
        "TypeSafe API key",
        value=os.getenv("TYPESAFE_API_KEY", ""),
        type="password",
        help=(
            "Sent as the X-TypeSafe-Api-Key header and used for this request only. "
            "Leave blank to use the key configured on the API server."
        ),
    )

    st.divider()
    st.subheader("Scenario")
    st.selectbox(
        "Preconfigured scenario",
        options=list(SCENARIOS),
        key="scenario",
        on_change=_load_scenario,
        help="Loads a representative event into the form. Every field stays editable.",
    )

    thresholds = fetch_thresholds(api_base)
    st.divider()
    st.subheader("Active guardrails")
    st.markdown(
        f"""
        <div class="ts-rules">
          <b>A</b> confidence ≥ 0.90 auto · ≥ 0.60 driver · else dispatch<br>
          <b>B</b> signature due &amp; bypass p &gt; {thresholds['signature_bypass']:.2f} → block<br>
          <b>C</b> value &gt; $300 &amp; risk ≥ {thresholds['high_value_risk_score']:.1f} → OTP<br>
          <b>D</b> redirection p &gt; {thresholds['redirection']:.2f} &amp; unclear → audit<br>
          <span style="opacity:0.62">precedence: B › D › C › A</span>
        </div>
        """,
        unsafe_allow_html=True,
    )

# --- Header ----------------------------------------------------------------

st.markdown(
    '<div class="ts-mast"><div class="ts-mast-in">'
    '<span class="ts-chip">System One · Jev</span>'
    '<div class="ts-display">Logistics Triage<br>&amp; Exceptions Engine</div>'
    '<p class="ts-lede">Unstructured last-mile events, evaluated by Jev in a single parallel '
    'pass, then routed by deterministic guardrails. The model answers; the rules decide.</p>'
    '</div></div>',
    unsafe_allow_html=True,
)

# --- Input form ------------------------------------------------------------

with st.form("delivery_event", border=True):
    titlebar("Delivery event", "input")
    st.markdown(
        '<div class="ts-note" style="padding-left:0;margin-top:-4px">'
        'The note is evaluated together with the parcel constraints.</div>',
        unsafe_allow_html=True,
    )

    note_column, params_column = st.columns([3, 2], gap="large")
    with note_column:
        st.text_input("Tracking ID", key="form_tracking_id", max_chars=64)
        st.text_area("Customer note", key="form_customer_note", height=148, max_chars=4000)
    with params_column:
        st.number_input(
            "Package value (USD)",
            key="form_package_value_usd",
            min_value=0.0,
            max_value=1_000_000.0,
            step=10.0,
            format="%.2f",
        )
        st.number_input("Delivery attempt", key="form_delivery_attempt", min_value=1, max_value=20, step=1)
        st.toggle("Signature required", key="form_requires_signature")

    submitted = st.form_submit_button("▶ Run triage", type="primary", use_container_width=True)

if submitted:
    payload = {
        "tracking_id": st.session_state["form_tracking_id"],
        "customer_note": st.session_state["form_customer_note"],
        "package_value_usd": float(st.session_state["form_package_value_usd"]),
        "requires_signature": bool(st.session_state["form_requires_signature"]),
        "delivery_attempt": int(st.session_state["form_delivery_attempt"]),
    }
    with st.spinner("Evaluating with Jev…"):
        result, error = call_triage(api_base, api_key, payload)
    st.session_state["last_payload"] = payload
    if error:
        st.session_state["result"] = None
        st.markdown(
            f'<div class="ts-flag" style="border-left-color:{STATUS_CRITICAL}">⚠️&nbsp; {error}</div>',
            unsafe_allow_html=True,
        )
    else:
        st.session_state["result"] = result

# --- Results ---------------------------------------------------------------

result = st.session_state.get("result")

if result is None:
    st.markdown(
        f'<div class="ts-flag" style="border-left-color:{INK}">'
        '💡&nbsp; Pick a scenario in the sidebar or write a note, then run the triage.</div>',
        unsafe_allow_html=True,
    )
else:
    evaluation = result["evaluation"]
    action = evaluation["recommended_action"]
    risk = evaluation["risk_score"]
    bypass = evaluation["signature_bypass_requested"]["noul"]
    redirection = evaluation["address_redirection_detected"]["noul"]

    status = result["status"]
    status_color, status_icon = STATUS_STYLE.get(status, (STATUS_SERIOUS, "•", ""))[:2]
    risk_color, risk_icon = RISK_STYLE.get(result["risk_level"], (STATUS_SERIOUS, "•"))

    st.write("")
    request_id = evaluation.get("request_id")
    eyebrow(
        "Triage verdict",
        f'Tracking {result["tracking_id"]} · model {evaluation["model_name"]}'
        + (f" · request {request_id}" if request_id else ""),
    )

    # Key metrics, as four small windows.
    tiles = (
        ("Status", f"{status_icon} {STATUS_SHORT.get(status, status)}", status.replace("_", " ").lower()),
        (
            "Final action",
            ACTION_SHORT.get(result["final_action"], result["final_action"]),
            ACTION_LABELS.get(result["final_action"], result["final_action"]).lower(),
        ),
        ("Latency", f"{result['latency_ms']:.0f} ms", "jev round trip"),
        ("Cost", f"${result['cost_usd']:.6f}", f"{evaluation['usage']['input_tokens']} input tokens"),
    )
    for column, (label, value, sub) in zip(st.columns(4, gap="medium"), tiles):
        column.markdown(
            f'<div class="ts-tile"><div class="ts-tile-bar">{label}</div>'
            f'<div class="ts-tile-body"><span class="ts-tile-value">{value}</span>'
            f'<span class="ts-tile-sub">{sub}</span></div></div>',
            unsafe_allow_html=True,
        )

    st.write("")

    # Applied-rule banner.
    st.markdown(
        f"""
        <div class="ts-banner">
          <div class="ts-banner-bar">
            <span>{status_icon} {result['applied_rule'].replace('_', ' ')}</span>
            <span class="ts-titlebar-meta">{status.replace('_', ' ')}</span>
          </div>
          <div class="ts-banner-body" style="border-left-color:{status_color}">
            {result['applied_rule_detail']}
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    for active, icon, message, tone in (
        (result.get("requires_otp"), "🔐",
         "Send a one-time code to the customer's phone before releasing the parcel.", STATUS_SERIOUS),
        (result.get("auto_dispatch_suspended"), "⏸️",
         "Automated re-dispatch is suspended for this tracking ID.", STATUS_WARNING),
    ):
        if active:
            st.markdown(
                f'<div class="ts-flag" style="border-left-color:{tone}">{icon}&nbsp; {message}</div>',
                unsafe_allow_html=True,
            )

    # Charts.
    left, right = st.columns([3, 2], gap="large")

    with left:
        with st.container(border=True):
            titlebar(
                "Recommended action · RLCD",
                f'{ACTION_LABELS.get(action["choice"], action["choice"])} · {action["confidence"]:.1%}',
            )
            st.plotly_chart(
                action_probability_chart(action["probabilities"], action["choice"]),
                use_container_width=True,
                config={"displayModeBar": False},
            )

    with right:
        with st.container(border=True):
            titlebar("Risk score", f'{result["risk_level"]} · {risk["confidence"]:.1%}')
            st.plotly_chart(
                risk_gauge(risk["score"], result["risk_level"], thresholds["high_value_risk_score"]),
                use_container_width=True,
                config={"displayModeBar": False},
            )
            st.markdown(
                f'<div class="ts-note" style="padding-left:0;margin:0">'
                f'{risk_icon} 0–3 rubric · marked line = OTP threshold '
                f'{thresholds["high_value_risk_score"]:.1f}</div>',
                unsafe_allow_html=True,
            )

    st.write("")
    eyebrow(
        "Boolean signals · Noul",
        "Probability that each statement holds. The dotted rule is the guardrail threshold.",
    )

    noul_left, noul_right = st.columns(2, gap="large")
    for column, title, probability, threshold in (
        (noul_left, "Signature bypass requested", bypass, thresholds["signature_bypass"]),
        (noul_right, "Address redirection detected", redirection, thresholds["redirection"]),
    ):
        with column:
            breached = probability > threshold
            flag = "⛔ ABOVE THRESHOLD" if breached else "✅ BELOW THRESHOLD"
            with st.container(border=True):
                titlebar(title, flag)
                st.plotly_chart(
                    noul_meter(probability, threshold),
                    use_container_width=True,
                    config={"displayModeBar": False},
                )

    # Table view: every charted value is readable without relying on colour.
    with st.expander("Table view · all evaluated values"):
        eyebrow("Recommended action probabilities")
        render_table(
            ["Action", "Key", "Probability", "Selected"],
            [
                (
                    [ACTION_LABELS.get(name, name), name, f"{value:.2%}",
                     "◆ yes" if name == action["choice"] else "—"],
                    name == action["choice"],
                )
                for name, value in sorted(
                    action["probabilities"].items(), key=lambda item: item[1], reverse=True
                )
            ],
            numeric={2},
        )
        eyebrow("Risk rubric")
        render_table(
            ["Level", "Meaning", "Probability"],
            [
                (
                    [str(level), risk["legend"].get(level, risk["legend"].get(str(level), "")),
                     f"{probability:.2%}"],
                    False,
                )
                for level, probability in sorted(
                    risk["probabilities"].items(), key=lambda item: int(item[0])
                )
            ],
            numeric={0, 2},
        )
        eyebrow("Boolean signals")
        render_table(
            ["Signal", "Probability", "Threshold", "State"],
            [
                (
                    ["signature_bypass_requested", f"{bypass:.2%}",
                     f"{thresholds['signature_bypass']:.2f}",
                     "⛔ above" if bypass > thresholds["signature_bypass"] else "✅ below"],
                    bypass > thresholds["signature_bypass"],
                ),
                (
                    ["address_redirection_detected", f"{redirection:.2%}",
                     f"{thresholds['redirection']:.2f}",
                     "⛔ above" if redirection > thresholds["redirection"] else "✅ below"],
                    redirection > thresholds["redirection"],
                ),
            ],
            numeric={1, 2},
        )

    # JSON inspector.
    with st.expander("JSON inspector · payload sent and raw Jev response"):
        payload_column, raw_column = st.columns(2, gap="large")
        with payload_column:
            st.markdown("**Payload sent to `/api/v1/triage`**")
            st.code(json.dumps(st.session_state.get("last_payload") or {}, indent=2), language="json")
        with raw_column:
            st.markdown("**Raw response from Jev**")
            st.code(json.dumps(result.get("raw_response", {}), indent=2), language="json")
        st.markdown("**Full triage response**")
        st.code(json.dumps(result, indent=2), language="json")

    st.caption(
        (f"Request {request_id} · " if request_id else "")
        + f"Model {evaluation['model_name']} · "
        f"{evaluation['usage']['input_tokens']} input tokens · "
        f"{evaluation['usage']['output_tokens']} output tokens (free) · "
        f"{evaluation['latency_ms']:.0f} ms round trip"
    )
