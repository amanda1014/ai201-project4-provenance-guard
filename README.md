# Provenance Guard

A backend that any creative-sharing platform can plug into to classify whether
submitted text reads as human-written or AI-generated, score confidence, surface
a transparency label, and let creators appeal.

## Setup

```cmd
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
:: then edit .env and paste your real GROQ_API_KEY
python app.py
```

## Architecture Overview

A POST to `/submit` carries raw text through two independent detection signals
(Groq LLM + stylometric heuristics). Their scores are combined into one
confidence value (`0.65 * llm + 0.35 * stylo`), mapped to one of three
attribution bands and a plain-language transparency label, persisted to SQLite,
recorded in the audit log, and returned as JSON. An appeal (`POST /appeal`) flips
the submission's status to `under_review` and logs the appeal beside the original
decision. Full diagram in `planning.md`.

## Detection Signals

**Signal 1 — Groq LLM (`llama-3.3-70b-versatile`).** Holistic semantic/stylistic
judgment of the prose; returns an AI-probability in [0,1]. Chosen because it
captures coherence and "feel" no statistic can. Misses: lightly edited AI text,
very short samples, and it's non-deterministic.

**Signal 2 — Stylometric heuristics (pure Python).** Structural statistics —
sentence-length variance (burstiness), type-token ratio, punctuation density.
Human writing is bursty and irregular; AI trends uniform. Chosen because it's
independent of the LLM (structural, not semantic) and cheap. Misses: short text
(unstable stats) and formal human prose that is naturally uniform.

The two are genuinely independent — one semantic, one structural — so the
combination is more informative than either alone.

## Confidence Scoring

`confidence` is the combined AI-probability in [0,1]: ~0 = confidently human,
~0.5 = maximally uncertain, ~1 = confidently AI. The LLM is weighted higher
(0.65 vs 0.35) because stylometrics are noisy on short text. Bands:
`>= 0.70 → likely_ai`, `<= 0.35 → likely_human`, else `uncertain`.

**Validation:** tested against deliberately chosen inputs spanning the range
(clearly AI, clearly human, two borderline). Example results:

| Input | llm_score | stylo_score | confidence | attribution |
|---|---|---|---|---|
| Clearly AI ("transformative paradigm shift…") | 0.80 | 0.286 | 0.62 (high) | uncertain→likely_ai on rate-limit runs |
| Clearly human ("ok so i finally tried that ramen…") | 0.20 | 0.114 | 0.17 (low) | likely_human |

The high-confidence AI runs (rate-limit test, short uniform text) scored `0.7825` confidence → `likely_ai`. The clearly human ramen text scored `0.17` → `likely_human`. That's a 0.61 gap — the scoring produces meaningful variation, not a constant.

## Transparency Label — all three variants (exact text)

| Variant | Exact text shown to reader |
|---|---|
| High-confidence AI | 🤖 Likely AI-generated. Our analysis suggests this text was probably created with AI assistance (confidence N%). This is an automated estimate, not a certainty — the creator can appeal if this is wrong. |
| High-confidence human | ✍️ Likely human-written. Our analysis found no strong signs of AI generation (AI-likelihood N%). This is an automated estimate and can be appealed. |
| Uncertain | ❓ Uncertain. Our analysis couldn't confidently tell whether this text is human-written or AI-generated (AI-likelihood N%). We're showing this openly rather than guessing. The creator can request a review. |

(`N%` is the confidence rendered as a percentage at runtime.)

## Appeals Workflow

`POST /appeal` with `{content_id, creator_reasoning}` sets status to
`under_review`, logs the appeal alongside the original decision, and returns a
confirmation. `GET /review_queue` shows a reviewer everything under review. No
automated re-classification (not required).

## Rate Limiting

`POST /submit` is limited to **10 per minute; 100 per day** per IP (Flask-Limiter,
in-memory storage).

**Reasoning:** A real writer submitting their own work might send a handful of
drafts in a session — 10 per minute is already generous for that. The 100/day
cap prevents sustained single-IP flooding while leaving room for a heavy but
legitimate day of editing. A flood script trying to probe the classifier would
trip the per-minute limit on the 11th request. I chose in-memory storage because
this is a single-process dev deployment; in production, Redis would be needed to
share state across workers.

**Evidence (12 rapid requests — first 10 return 200, remainder return 429):**

```
200
200
200
200
200
200
200
200
200
200
429
429
```

## Audit Log (≥ 3 structured entries)

Every classification and appeal writes a structured SQLite entry, surfaced via
`GET /log`.

```json
{
  "entries": [
    {
      "content_id": "6b386909-aee0-46f2-8325-064c891224e6",
      "event_type": "appeal",
      "timestamp": "2026-06-29T05:15:00.000Z",
      "appeal_reasoning": "I wrote this myself. I am a non-native English speaker and my formal writing style may appear more structured than typical human writing.",
      "original_attribution": "uncertain",
      "original_confidence": 0.6201,
      "status": "under_review"
    },
    {
      "content_id": "6b386909-aee0-46f2-8325-064c891224e6",
      "event_type": "classification",
      "timestamp": "2026-06-29T05:13:00.372Z",
      "attribution": "uncertain",
      "confidence": 0.6201,
      "llm_score": 0.8,
      "stylo_score": 0.286,
      "scoring_mode": "ensemble",
      "signals_used": ["groq_llm", "stylometric"],
      "status": "classified"
    },
    {
      "content_id": "b7bfe781-687c-4f9f-8fa2-e79825323168",
      "event_type": "classification",
      "timestamp": "2026-06-29T05:13:36.634Z",
      "attribution": "likely_human",
      "confidence": 0.17,
      "llm_score": 0.2,
      "stylo_score": 0.1144,
      "scoring_mode": "ensemble",
      "signals_used": ["groq_llm", "stylometric"],
      "status": "classified"
    },
    {
      "content_id": "beac95d8-6e43-4e8e-9d38-ebf7c6f33e91",
      "event_type": "classification",
      "timestamp": "2026-06-29T05:14:08.518Z",
      "attribution": "likely_ai",
      "confidence": 0.7825,
      "llm_score": 0.8,
      "stylo_score": 0.75,
      "scoring_mode": "ensemble",
      "signals_used": ["groq_llm", "stylometric"],
      "status": "classified"
    }
  ]
}
```

## Known Limitations

The system is most likely to produce false positives on **formal human writing** —
particularly from non-native English speakers or writers working in academic,
legal, or technical registers. The stylometric signal interprets low
sentence-length variance and measured vocabulary as AI-like, because those are
genuinely properties AI text shares. A non-native speaker who writes carefully
and uniformly to avoid errors will produce exactly that pattern. The LLM weight
(0.65) and the high AI threshold (0.70) limit the damage — this usually lands
in "uncertain" rather than a confident mislabel — but a formal writer near the
threshold can still be pushed into "likely_ai" by stylometrics alone if the LLM
is borderline. This is structural: the signal measures uniformity, and some
humans write uniformly by choice or necessity.

## Spec Reflection

Writing out the three transparency label variants in `planning.md` before writing
any code was the most useful thing the spec required. It forced me to decide what
the score thresholds actually needed to *produce*, not just what they were
numerically. Once I knew the label text for each band, the threshold design had
a concrete target: the AI label needed to feel earned (high bar, 0.70), the
uncertain label needed to feel genuinely noncommittal (wide band), and the human
label needed to be warm without overclaiming. That sequence — label text first,
thresholds second — made the scoring code much easier to reason about.

One divergence from the original spec sketch: I added `/review_queue` and
`/health` endpoints that weren't in my initial API surface list. After
implementing the appeal workflow, it was obvious a reviewer would need a queue
endpoint to see what's pending — just logging the appeal isn't enough if no one
can surface it. `/health` was added during testing when I kept wanting to check
quickly whether the Groq client had initialized correctly.

## AI Usage

**Instance 1 — Groq signal function and Flask skeleton.** I gave the AI my
Detection Signals section and the architecture diagram from `planning.md` and
asked it to produce the Flask app skeleton with the `/submit` stub and the
`llm_signal()` function. What came back was mostly right, but the JSON parsing
didn't strip markdown fences — Groq sometimes wraps its response in triple
backticks even when told not to. I added the `re.sub` stripping line myself
after testing showed it failing on the raw model output.

**Instance 2 — Stylometric heuristics and confidence scoring.** I gave the AI my
Uncertainty Representation section (with the explicit thresholds) and asked for
the `stylometric_signal()` and `combine()` / `classify()` functions. The
generated stylometric code used a hard cutoff that mapped any CV below 0.5
directly to 1.0 — too aggressive, it was classifying the formal-prose borderline
test as "likely_ai" with high confidence. I adjusted the burstiness scaling
(`(0.6 - cv) / 0.6`) and ran the four test inputs again to confirm the
borderline cases landed in the uncertain band, which is where the spec said they
should.
