"""
Provenance Guard — backend for classifying whether submitted creative text
reads as human-written or AI-generated, scoring confidence, surfacing a
transparency label, handling appeals, rate limiting, and audit logging.

Two independent detection signals:
  1. Groq LLM (llama-3.3-70b-versatile) — holistic semantic/stylistic judgment.
  2. Stylometric heuristics (pure Python) — structural statistics of the text.

Design note on the asymmetry: on a writing platform, falsely labeling a
human's work as AI is worse than missing some AI. So the bar to declare
"Likely AI" is deliberately HIGH (>= 0.70) and the "Uncertain" band is wide.
"""

import os
import re
import json
import math
import sqlite3
import uuid
from datetime import datetime, timezone

from flask import Flask, request, jsonify, g
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dotenv import load_dotenv

load_dotenv()

# Groq is optional at import time so the app still boots without a key;
# the LLM signal degrades gracefully to "unavailable" if it can't run.
try:
    from groq import Groq
    _GROQ_KEY = os.getenv("GROQ_API_KEY")
    _groq_client = Groq(api_key=_GROQ_KEY) if _GROQ_KEY else None
except Exception:
    _groq_client = None

DB_PATH = os.getenv("PROVENANCE_DB", "provenance.db")

app = Flask(__name__)

# ---- Rate limiting -----------------------------------------------------------
# Reasoning lives in the README. Short version: a real writer submits a handful
# of pieces per session, not 10/minute, so 10/min leaves generous headroom for
# legitimate use while a flood script trips it immediately. 100/day caps
# sustained abuse from a single IP without blocking a heavy-but-human day.
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

# =============================================================================
# Storage (SQLite — structured, persistent, supports status updates cleanly)
# =============================================================================

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    db = sqlite3.connect(DB_PATH)
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS submissions (
            content_id   TEXT PRIMARY KEY,
            creator_id   TEXT,
            text         TEXT,
            attribution  TEXT,
            confidence   REAL,
            llm_score    REAL,
            stylo_score  REAL,
            status       TEXT,
            created_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            content_id  TEXT,
            event_type  TEXT,
            payload     TEXT,
            timestamp   TEXT
        );
        """
    )
    db.commit()
    db.close()


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def write_audit(content_id, event_type, payload):
    """Append one structured entry to the audit log."""
    db = get_db()
    db.execute(
        "INSERT INTO audit_log (content_id, event_type, payload, timestamp) VALUES (?, ?, ?, ?)",
        (content_id, event_type, json.dumps(payload), now_iso()),
    )
    db.commit()


# =============================================================================
# Signal 1 — Groq LLM
# Captures holistic semantic/stylistic coherence: phrasing, idea flow, the
# "feel" of the prose. Blind spot: can be fooled by lightly edited AI text and
# by very short samples; non-deterministic.
# =============================================================================

LLM_PROMPT = (
    "You are an expert at detecting AI-generated text. Assess whether the "
    "following text reads as human-written or AI-generated. Respond with ONLY "
    "a JSON object, no markdown, no preamble, in exactly this shape:\n"
    '{"ai_probability": <float 0.0-1.0>, "reasoning": "<one short sentence>"}\n'
    "where ai_probability is your estimate that the text is AI-generated "
    "(0.0 = certainly human, 1.0 = certainly AI).\n\nTEXT:\n"
)


def llm_signal(text):
    """Returns {'score': float|None, 'reasoning': str, 'available': bool}."""
    if _groq_client is None:
        return {"score": None, "reasoning": "LLM signal unavailable (no GROQ_API_KEY)", "available": False}
    try:
        resp = _groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": LLM_PROMPT + text}],
            temperature=0,
            max_tokens=150,
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r"^```(?:json)?|```$", "", raw, flags=re.MULTILINE).strip()
        data = json.loads(raw)
        score = max(0.0, min(1.0, float(data["ai_probability"])))
        return {"score": score, "reasoning": str(data.get("reasoning", "")), "available": True}
    except Exception as e:
        return {"score": None, "reasoning": f"LLM signal error: {e}", "available": False}


# =============================================================================
# Signal 2 — Stylometric heuristics (pure Python)
# Captures structural statistics. Human writing is "bursty" — variable sentence
# length, irregular punctuation. AI text trends uniform. Blind spot: short text
# (too few sentences for stable stats) and formal human writing that happens to
# be uniform (academic prose). That's why this is the LOWER-weighted signal.
# =============================================================================

def stylometric_signal(text):
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s.strip()]
    words = re.findall(r"\b\w+\b", text.lower())
    n_words = len(words)
    n_sents = max(1, len(sentences))

    sent_lengths = [len(re.findall(r"\b\w+\b", s)) for s in sentences] or [n_words]
    mean_len = sum(sent_lengths) / len(sent_lengths)
    if len(sent_lengths) > 1 and mean_len > 0:
        var = sum((x - mean_len) ** 2 for x in sent_lengths) / len(sent_lengths)
        cv = math.sqrt(var) / mean_len           # burstiness; low CV -> AI-like
    else:
        cv = 0.0

    ttr = len(set(words)) / n_words if n_words else 0.0   # vocabulary diversity
    punct = len(re.findall(r"[,;:\-—()\"'!?]", text))
    punct_density = punct / n_words if n_words else 0.0

    # Map each metric to an AI-likeness contribution in [0,1].
    # Low burstiness -> AI-like. CV ~0.6+ is human-typical.
    burst_ai = max(0.0, min(1.0, (0.6 - cv) / 0.6))
    # Low diversity -> mildly AI-like (clamped; short text inflates TTR).
    div_ai = max(0.0, min(1.0, (0.7 - ttr) / 0.4))
    # Sparse punctuation -> mildly AI-like.
    punct_ai = max(0.0, min(1.0, (0.08 - punct_density) / 0.08))

    stylo_score = 0.55 * burst_ai + 0.25 * div_ai + 0.20 * punct_ai
    return {
        "score": round(stylo_score, 4),
        "metrics": {
            "sentence_count": len(sentences),
            "mean_sentence_length": round(mean_len, 2),
            "sentence_length_cv": round(cv, 4),
            "type_token_ratio": round(ttr, 4),
            "punctuation_density": round(punct_density, 4),
        },
    }


# =============================================================================
# Confidence scoring + label
# `confidence` is the combined AI-probability in [0,1]:
#   ~0.0 = confidently human, ~0.5 = maximally uncertain, ~1.0 = confidently AI.
# =============================================================================

T_AI = 0.70        # high bar to declare AI (false-positive asymmetry)
T_HUMAN = 0.35     # below this -> human

LLM_WEIGHT = 0.65  # LLM trusted more; stylometrics noisier on short text
STYLO_WEIGHT = 0.35


def combine(llm_score, stylo_score):
    if llm_score is None:                      # graceful degradation
        return round(stylo_score, 4), "stylometric_only"
    combined = LLM_WEIGHT * llm_score + STYLO_WEIGHT * stylo_score
    return round(combined, 4), "ensemble"


def classify(confidence):
    if confidence >= T_AI:
        return "likely_ai"
    if confidence <= T_HUMAN:
        return "likely_human"
    return "uncertain"


def transparency_label(attribution, confidence):
    pct = round(confidence * 100)
    if attribution == "likely_ai":
        return (
            f"🤖 Likely AI-generated. Our analysis suggests this text was probably "
            f"created with AI assistance (confidence {pct}%). This is an automated "
            f"estimate, not a certainty — the creator can appeal if this is wrong."
        )
    if attribution == "likely_human":
        return (
            f"✍️ Likely human-written. Our analysis found no strong signs of AI "
            f"generation (AI-likelihood {pct}%). This is an automated estimate and "
            f"can be appealed."
        )
    return (
        f"❓ Uncertain. Our analysis couldn't confidently tell whether this text is "
        f"human-written or AI-generated (AI-likelihood {pct}%). We're showing this "
        f"openly rather than guessing. The creator can request a review."
    )


# =============================================================================
# Endpoints
# =============================================================================

@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute;100 per day")
def submit():
    body = request.get_json(silent=True) or {}
    text = body.get("text")
    creator_id = body.get("creator_id")
    if not text or not creator_id:
        return jsonify({"error": "Both 'text' and 'creator_id' are required."}), 400

    content_id = str(uuid.uuid4())

    llm = llm_signal(text)
    stylo = stylometric_signal(text)
    confidence, mode = combine(llm["score"], stylo["score"])
    attribution = classify(confidence)
    label = transparency_label(attribution, confidence)

    db = get_db()
    db.execute(
        """INSERT INTO submissions
           (content_id, creator_id, text, attribution, confidence,
            llm_score, stylo_score, status, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (content_id, creator_id, text, attribution, confidence,
         llm["score"], stylo["score"], "classified", now_iso()),
    )
    db.commit()

    write_audit(content_id, "classification", {
        "content_id": content_id,
        "creator_id": creator_id,
        "attribution": attribution,
        "confidence": confidence,
        "llm_score": llm["score"],
        "stylo_score": stylo["score"],
        "scoring_mode": mode,
        "signals_used": ["groq_llm", "stylometric"],
        "status": "classified",
    })

    return jsonify({
        "content_id": content_id,
        "attribution": attribution,
        "confidence": confidence,
        "label": label,
        "signals": {
            "llm": {"score": llm["score"], "reasoning": llm["reasoning"], "available": llm["available"]},
            "stylometric": {"score": stylo["score"], "metrics": stylo["metrics"]},
        },
        "scoring_mode": mode,
        "status": "classified",
    }), 200


@app.route("/appeal", methods=["POST"])
def appeal():
    body = request.get_json(silent=True) or {}
    content_id = body.get("content_id")
    reasoning = body.get("creator_reasoning")
    if not content_id or not reasoning:
        return jsonify({"error": "Both 'content_id' and 'creator_reasoning' are required."}), 400

    db = get_db()
    row = db.execute("SELECT * FROM submissions WHERE content_id = ?", (content_id,)).fetchone()
    if row is None:
        return jsonify({"error": f"No submission found for content_id {content_id}."}), 404

    db.execute("UPDATE submissions SET status = ? WHERE content_id = ?", ("under_review", content_id))
    db.commit()

    write_audit(content_id, "appeal", {
        "content_id": content_id,
        "creator_id": row["creator_id"],
        "original_attribution": row["attribution"],
        "original_confidence": row["confidence"],
        "appeal_reasoning": reasoning,
        "status": "under_review",
    })

    return jsonify({
        "content_id": content_id,
        "status": "under_review",
        "message": "Your appeal has been received and the content is now under review.",
        "original_decision": {
            "attribution": row["attribution"],
            "confidence": row["confidence"],
        },
    }), 200


@app.route("/log", methods=["GET"])
def log():
    """Returns recent audit entries. In production this would require auth."""
    db = get_db()
    rows = db.execute(
        "SELECT content_id, event_type, payload, timestamp FROM audit_log ORDER BY id DESC LIMIT 50"
    ).fetchall()
    entries = [{
        "content_id": r["content_id"],
        "event_type": r["event_type"],
        "timestamp": r["timestamp"],
        **json.loads(r["payload"]),
    } for r in rows]
    return jsonify({"entries": entries}), 200


@app.route("/review_queue", methods=["GET"])
def review_queue():
    """What a human reviewer sees: everything currently under review."""
    db = get_db()
    rows = db.execute(
        "SELECT content_id, creator_id, attribution, confidence, status, created_at "
        "FROM submissions WHERE status = 'under_review' ORDER BY created_at DESC"
    ).fetchall()
    return jsonify({"under_review": [dict(r) for r in rows]}), 200


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "llm_available": _groq_client is not None}), 200


if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=5000, debug=True)
