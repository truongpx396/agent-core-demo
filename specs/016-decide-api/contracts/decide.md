# Contract: `POST /v1/decide`

The decision interface of the ML service, in the request and answer shape of the TypeSafe **System One / Jev** interface. Supersedes the wire
of `specs/015-label-sets-and-tiered-routing/contracts/label-sets.md` (`POST /classify`, `task: "label"`, which no longer exists); the label-set
FILE format, the cutoffs, the indexes and the failover in that document are unchanged.

**Where the shape comes from.** Opper's compatibility page for System One (`docs.opper.ai/v3-api-reference/compatibility/systemone`) and TypeSafe's
Jev API page (`jevtypesafeai.com/jev/api`), read on 2026-10-23. **No real response has been recorded**: the one call made (Opper, key
supplied by the owner) answered `402 evaluation needs a card on file`, so every hosted field below is the documentation's, not an observation.

## Request

```json
{
  "state": "I was charged twice this month, please reverse one.",
  "questions": {
    "route-topics": {
      "type": "choice",
      "instructions": "What kind of work does the user's message ask of an assistant?",
      "criteria": {"lookup": "finding a fact written down somewhere", "action": "asking the assistant to do something in a system", "...": "..."}
    }
  }
}
```

| Field | Rule | Error |
|---|---|---|
| body | a JSON object; no field other than `model`, `state`, `questions` | 400 `invalid_input` |
| `assets` | not supported (images) | 422 `unsupported_field` |
| `model` | optional; `"local"` is the only value | 400 `unknown_model` |
| `state` | a non-empty string of at most `ML_CLASSIFY_MAX_CHARS` (4000) characters | 400 `invalid_input`; an object or array 422 `unsupported_state`; too long 413 `input_too_long` |
| `questions` | an object of 1..8 questions; names match `[A-Za-z0-9][A-Za-z0-9._-]{0,127}` | 400 `invalid_input`, `too_many_questions` |
| `type` | `choice` is answered; `score` and `noul` are known and not implemented; anything else is unknown | 422 `unsupported_question_type`; 400 `invalid_input` |
| `instructions` | a non-empty string. **Required for the shape; not used for matching** | 400 `invalid_input` |
| `criteria` | an object of 1..255 option names to string descriptions. **Descriptions are not used for matching** | 400 `invalid_input` |
| question name | **is the name of a label set** (`docker/ml-service/labelsets/<name>.json`) | 400 `unknown_decision` |
| `criteria` keys | **must equal the set's labels exactly** (a question and its set that drifted apart is an error, not a quietly wrong answer) | 400 `criteria_mismatch`, naming what is missing and what is extra |

Nothing about the caller is in the request and the service accepts none (no tenant, principal, history). No authentication: the service is internal.

## Answer, 200

```json
{
  "model": "bge-small-en-v1.5",
  "answers": {
    "route-topics": {
      "type": "choice",
      "choice": "lookup",
      "x_evidence": {"nearest": "lookup", "similarity": 0.81, "runner_up": "analysis", "margin": 0.07, "truncated": false},
      "x_labelset_version": "5b42dff1816d"
    }
  },
  "usage": {"input_tokens": 12, "output_tokens": 0},
  "x_served_by": "primary"
}
```

- `choice` is one of the question's options, or **`null`** when the text is near none of them (the set's cutoff for the serving model).
- `usage.input_tokens` is what the serving model was actually fed, once however many questions there are; `output_tokens` is `0`.
- Several questions are answered by **one** model with **one** embedding pass; `model` and `x_served_by` therefore describe all of them.
- Fields this service adds are prefixed `x_`. A reader of the documented fields never meets them.

## Unavailable, 503

`{"error": {"code": "model_unavailable", "message": "..."}}` with `Retry-After: 1`: no model could answer now (still indexing, both failed or slow,
a named set refused, no model has a cutoff for it). The body is the interface's own error shape (`{"error": {"code", "message"}}`). The app treats
exactly this code as "no answer" and any other non-200 as a failure.

## What differs from the hosted interface (all deliberate)

| | Hosted (per its documentation) | This service | Why |
|---|---|---|---|
| `choice` | always one of the options | **`null` when near none** | the cutoff is the point of a topic matcher; a hosted caller gets "none of these" by adding an option for it |
| `confidence`, `probabilities` | present on every `choice` | **absent** | a cosine similarity is not a probability; the evidence is in `x_evidence` under its own name |
| `state` | string, object or array | string | nothing here needs more |
| `instructions`, option descriptions | read by the model | required, ignored | the matcher learns from example sentences, which the interface has no place for |
| question types | `choice`, `score`, `noul` (and `box`) | `choice` | `noul` needs a fitted calibration before its number may be called a probability |
| `model` | required, a catalog id | optional, `"local"` | |
| authentication | bearer key | none | internal service |
| overload | 529 | 503 `model_unavailable` | the code the rest of this service already uses |

## Trying the hosted interface once (needs a card on the Opper account)

```bash
set -a; . ./.env; set +a      # exports OPPER_AI_KEY; never paste a key into a command line or a chat
curl -s https://api.opper.ai/v3/compat/v1/systemone -H "Authorization: Bearer $OPPER_AI_KEY" -H 'content-type: application/json' -d '{
  "model": "typesafe/jev-1.13.0", "state": "I was charged twice this month, please reverse one.",
  "questions": {"route": {"type": "choice", "instructions": "What is this about?", "criteria": {"billing": "money, charges, refunds", "bug": "something is broken", "account": "login or profile"}}}}'
```

Without a card it answers `402`. The free-tier models Opper lists are chat models, not decision ones (`GET /v3/models?type=evaluation` lists only paid ones).

## Swapping to the hosted interface (not done in this spec)

The app's client is `app/core/classification.py::choose`. To point it at the hosted interface:

1. Change the URL (`https://api.opper.ai/v3/compat/v1/systemone`, or the vendor's own), add `Authorization: Bearer <key>` and `"model": "typesafe/jev-1.13.0"`
   to the request. `Choice` already carries the instructions and the option descriptions the hosted model needs.
2. Decide the privacy question first: the request then carries the user's message (the first 2,000 characters) to a third party, for every routed turn. The
   constitution's rule on who may see user text applies; this was not decided here.
3. The hosted interface never abstains. `Labelled.confidence` is parsed when present; whether a low confidence means "none" (and at what number) is a policy to
   set on a measurement, or the question gets an "other" option.
4. Spend: the hosted call is billed per input token by the host and is invisible to the agent's meter and caps until it is added.
5. Record one real response, add it to `tests/core/test_classification.py` next to the documentation-derived one, and judge it on `evals/datasets/route-topics`
   with the harness before it serves a turn.
