# Provenance Guard — Planning

## Architecture

```
                          POST /submit  {text, creator_id}
                                  |
                                  v
                        +-------------------+
                        |  Flask /submit    |
                        +-------------------+
                                  |  raw text
                  +---------------+----------------+
                  v                                v
        +-------------------+            +----------------------+
        | Signal 1: Groq LLM|            | Signal 2: Stylometry |
        | -> ai_probability |            | -> stylo_score       |
        |    (0.0-1.0)      |            |    (0.0-1.0)          |
        +-------------------+            +----------------------+
                  |  llm_score                     |  stylo_score
                  +---------------+----------------+
                                  v
                        +-------------------+
                        | Confidence scoring|
                        | 0.65*llm+0.35*sty |
                        | -> confidence     |
                        +-------------------+
                                  |  combined score
                                  v
                        +-------------------+
                        | Classify + Label  |
                        | bands -> attribution & label text
                        +-------------------+
                                  |  attribution, confidence, label
                  +---------------+----------------+
                  v                                v
        +-------------------+            +----------------------+
        |   Audit log       |            |  JSON response       |
        |  (SQLite entry)   |            |  to caller           |
        +-------------------+            +----------------------+


   POST /appeal {content_id, creator_reasoning}
            |
            v
   +-------------------+    update status     +-------------------+
   | Flask /appeal     | -------------------> | submissions table |
   |                   |   "under_review"     +-------------------+
   +-------------------+
            |  appeal entry
            v
   +-------------------+
   |   Audit log       |  (logs appeal alongside original decision)
   +-------------------+
            |
            v
   confirmation response
```

**Submission flow:** A POST to `/submit` carries raw text through both detection
signals in parallel; their scores are combined into a single confidence value,
mapped to one of three attribution bands and a plain-language label, persisted to
the submissions table, recorded in the audit log, and returned as JSON.

**Appeal flow:** A POST to `/appeal` with a `content_id` flips that submission's
status to `under_review`, writes an appeal entry to the audit log next to the
original decision, and returns a confirmation. No automated re-classification.

## Detection Signals

**Signal 1 — Groq LLM (`llama-3.3-70b-versatile`).** Measures holistic
semantic/stylistic coherence — phrasing, idea flow, the overall "feel" of the
prose. Output: `ai_probability` in [0,1]. Blind spot: easily fooled by lightly
edited AI text, unstable on very short samples, non-deterministic.

**Signal 2 — Stylometric heuristics (pure Python).** Measures structural
statistics: sentence-length variance (burstiness), type-token ratio (vocabulary
diversity), punctuation density. Human writing is bursty/irregular; AI text
trends uniform. Output: `stylo_score` in [0,1]. Blind spot: short text (too few
sentences for stable stats) and formal human writing that is naturally uniform
(academic/financial prose) — which is why it is the lower-weighted signal.

These are independent: one semantic, one structural.

**Combination:** `confidence = 0.65 * llm_score + 0.35 * stylo_score`. The LLM
is weighted higher because stylometrics are noisy on short text. If the LLM
signal is unavailable, the system degrades to stylometrics only and flags the
mode.

## Uncertainty Representation

`confidence` is the combined **AI-probability** in [0,1]:
- ~0.0 = confidently human
- ~0.5 = maximally uncertain
- ~1.0 = confidently AI

Thresholds (note the deliberate asymmetry — see false-positive answer below):
- `confidence >= 0.70` → **likely_ai**
- `confidence <= 0.35` → **likely_human**
- otherwise → **uncertain**

The "uncertain" band is wide and the AI bar is high, so a 0.51 lands in
"uncertain" while 0.95 lands firmly in "likely AI" — meaningfully different
labels, not a binary flip at 0.5.

I want a score of 0.5 to mean "we genuinely cannot tell" — not a coin flip
leaning slightly one way, but an honest admission that the two signals don't
agree enough to commit. The wide uncertain band (0.36–0.69) exists because
the cost of a wrong confident call is high: a writer falsely labeled as an
AI cheater could lose credibility. I'd rather surface uncertainty and invite
an appeal than guess and cause real harm. The high AI threshold (0.70) directly
supports this — the system only commits to "likely AI" when both signals push
strongly in that direction together.

## Transparency Label Design

Three variants (exact text is generated in `transparency_label()`):

| Variant | Text |
|---|---|
| High-confidence AI | 🤖 Likely AI-generated. Our analysis suggests this text was probably created with AI assistance (confidence N%). This is an automated estimate, not a certainty — the creator can appeal if this is wrong. |
| High-confidence human | ✍️ Likely human-written. Our analysis found no strong signs of AI generation (AI-likelihood N%). This is an automated estimate and can be appealed. |
| Uncertain | ❓ Uncertain. Our analysis couldn't confidently tell whether this text is human-written or AI-generated (AI-likelihood N%). We're showing this openly rather than guessing. The creator can request a review. |

## Appeals Workflow

- **Who:** the creator of a classified submission.
- **What they provide:** `content_id` + `creator_reasoning` (free text).
- **What the system does:** sets status → `under_review`, logs the appeal next
  to the original decision in the audit log, returns a confirmation echoing the
  original attribution + confidence.
- **Reviewer view:** `GET /review_queue` lists everything currently under review
  (content_id, creator_id, original attribution, confidence, timestamp).

## Anticipated Edge Cases

**Edge case 1 — Formal human writing (academic prose, non-native English speakers).**
A writer whose first language isn't English, or someone writing a formal essay
or research summary, may produce naturally uniform sentence lengths and measured
vocabulary. The stylometric signal reads low burstiness and moderate TTR as
AI-like, pushing the stylo_score up. Because stylometrics is the lower-weighted
signal (0.35) and the AI threshold is high (0.70), this usually lands in
"uncertain" rather than "likely_ai" — but it's a real false-positive risk. This
is exactly the scenario the appeals workflow is designed for.

**Edge case 2 — Short repetitive poetry.**
A poem with short, similarly-structured lines (think haiku, or lyric poetry with
refrains) gives the stylometric signal too few sentences for stable variance
estimates, and simple vocabulary drives TTR down. The signal can aggressively
over-score these as AI-like. Compounded with an LLM that may also find minimal
variation in a deliberately sparse poem, the system may push an "uncertain" or
even "likely_ai" result on genuinely human-crafted work. Minimum-sentence guards
help but don't fully solve it.

## False-Positive Scenario (Milestone 1 requirement)

A false positive — calling a human's work AI-generated — is worse than a false
negative on a creative platform because it directly attacks the creator's
credibility and originality, which is the core thing they're trying to protect.
A missed AI piece is an attribution failure; a false positive is an accusation.

The system addresses this in three ways. First, the AI threshold is set at 0.70,
not 0.50 — both signals have to push hard toward "AI" before we commit. Second,
the wide uncertain band (0.36–0.69) gives borderline cases a soft landing:
the label says "we couldn't tell" rather than forcing a verdict. Third, every
label — including the AI one — hedges explicitly: "automated estimate, not a
certainty." Finally, the appeal endpoint exists precisely for this case: a
creator can submit their reasoning and the content moves to "under_review" for
human evaluation. No automated re-classification happens without a person in
the loop.

## API Surface

| Endpoint | Method | Accepts | Returns |
|---|---|---|---|
| `/submit` | POST | `{text, creator_id}` | content_id, attribution, confidence, label, signals |
| `/appeal` | POST | `{content_id, creator_reasoning}` | status `under_review` + confirmation |
| `/log` | GET | — | recent audit entries |
| `/review_queue` | GET | — | submissions under review |
| `/health` | GET | — | status + llm availability |

## AI Tool Plan

**M3 (submission endpoint + first signal):** I gave the AI the Detection Signals
section and the architecture diagram and asked it to generate the Flask app
skeleton plus the Groq signal function. Before wiring anything in, I tested the
`llm_signal()` function directly on a clearly AI-generated paragraph and a casual
human-written message to confirm it returned a float in [0,1] and not a string
or error. I then reviewed the prompt format to make sure the model was being
asked for a JSON-only response with no markdown fencing.

**M4 (second signal + confidence scoring):** I gave the AI the Detection Signals
and Uncertainty Representation sections and asked it to produce the stylometric
function and the `combine()` / `classify()` logic. I verified that the threshold
values in the generated code matched exactly what I had written in this document
— AI tools have a tendency to default to 0.5 cutoffs. I ran the four test inputs
from the spec and checked that clearly AI text scored noticeably higher than
clearly human text before moving on.

**M5 (production layer):** I gave the AI the Label Variants and Appeals Workflow
sections and asked for the `transparency_label()` function and the `/appeal`
endpoint. I verified all three label variants were reachable by submitting inputs
at different score levels, and confirmed that after a `/appeal` call, `GET /log`
showed the entry with `"status": "under_review"` and `appeal_reasoning` populated.
