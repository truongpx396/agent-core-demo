# Contract: the recorded request and the idempotency conflict

What an idempotency key *stands for*, how it is recorded, how a reuse is judged, and what each caller in this repository compares.

## 1. The recorded request

Every keyed write stores, in `credit_transactions.request`, a JSON object whose values are strings or `null`.

| Operation | Function | Keys (all always stored) |
|---|---|---|
| grant; positive adjustment (source `adjustment`) | `grant_in`, `adjust_in` | `kind`="grant", `amount`, `source`, `provider`, `external_ref`, `expires_at`, `reason`, `actor` |
| debit; clawback; negative adjustment | `debit_in`, `clawback_in`, `adjust_in` | `kind` (`debit`/`clawback`/`adjust`), `amount`, `usage_event_id`, `cost_usd`, `credits_per_usd`, `markup`, `reason`, `actor` |
| expiry | `_expire_lot_in` | `kind`="expire", `lot_id` |

### Normalisation (so equal requests are equal strings)

| Value | Stored as |
|---|---|
| `amount` | `credits_amount(...)` (six places, half-up) formatted fixed-point: `"500.000000"`. The magnitude; the kind carries the sign. |
| `cost_usd`, `credits_per_usd`, `markup` | `Decimal` normalised and formatted fixed-point without exponent (`0.0100` and `0.01` are both `"0.01"`; `100` is `"100"`); `null` when there is no pricing |
| `expires_at` | the instant in UTC as ISO 8601 with microseconds (`2026-11-13T07:20:11.000000+00:00`), or `null` |
| `reason`, `source`, `provider`, `external_ref`, `usage_event_id`, `actor`, `lot_id` | the string exactly as given, or `null`. Not trimmed, not case-folded. |

The values compared are the values the *caller passed*, not what Postgres stored after rounding (the pricing columns round on storage, so comparing the stored columns would raise false conflicts).

## 2. Judging a reused key

```text
key unused                                  -> insert the transaction with its request       -> Applied("applied")
key used, stored request is NULL (legacy)   -> Applied("duplicate"), nothing compared         (today's behaviour)
key used, stored request present            -> for each field in (compare ∪ {"kind"}):
                                                 if the stored value is absent: skip that field
                                                 if stored != new: it differs
                                               none differ -> Applied("duplicate")
                                               any differ  -> raise IdempotencyConflict       (nothing written)
```

A field the stored request does not contain (a request recorded by an older version with fewer keys) is skipped, so adding a field later never makes old rows conflict.

## 3. The `compare=` argument

`grant_in`, `debit_in`, `clawback_in`, `adjust_in` accept `compare: Collection[str] | None = None`.

- `None` (default): every field of the operation (the strict comparison).
- A collection of names: only those fields, plus `kind`, which is always compared.
- Names must come from the operation's vocabulary in section 1; an unknown name raises `ValueError` listing the allowed ones, **before any statement runs**.
- `frozenset()` compares the kind alone: "this key names a fact, and any call under it is that fact".

## 4. `IdempotencyConflict`

```text
class IdempotencyConflict(ValueError):
    key: str
    tenant: str
    differences: dict[str, tuple[str | None, str | None]]    # field -> (first, new)
    str(e) ->  idempotency key 'K' was already used for a different request (amount: '500.000000' -> '600.000000'; reason: 'a' -> 'b'); nothing changed. A retry must send exactly the same request; a different request needs a new key.
```

- Subclass of `ValueError`: the operator command's existing handler keeps working.
- Long values are truncated for display (80 characters, ellipsis); the exception's `differences` carries them whole.
- Logged once, at warning, as `credit_idempotency_conflict` with `tenant`, `key`, `fields` (names only, no values).
- Exported from `credit_billing.credits`, `credit_billing.port`, and re-exported by `app/billing_integration/port.py`. A remote implementation of `WalletPort` maps it to a 409-style response.

## 5. What each caller compares, and why

| Caller | Key | `compare` | Why |
|---|---|---|---|
| Host charge via the port (`record_usage_in`) | usage event id | default (all) | everything is derived from the stored usage event, so a replay is identical; a difference is a bug or a changed price and must be loud. The host's `_charge` swallows and counts it like any wallet fault (`credit_debit_failed`, `error_class=IdempotencyConflict`). |
| Webhook grant (`webhooks._apply`) | `{provider}:{event_id}` | `GRANT_FIELDS - {"expires_at"}` | the expiry is `now + product days` at processing time; a concurrent replay computes a later instant. A conflict on the remaining fields means the catalogue changed under the event: quarantined at once as `idempotency_conflict`. |
| Webhook clawback | `clawback:{provider}:{payment_ref}` | `frozenset()` | the key names the payment. Two events describing one refund legitimately differ in `reason` and in the reclaimable amount; the second must remain a duplicate (`already_reversed`). |
| Operator command | `--key` | default (all) | a person reads the conflict; `--expires-at` makes an expiry retry-exact |
| Expiry sweep | `expire:{lot_id}` | default (`lot_id`) | nothing else varies |

## 6. Compatibility

- Databases that already have data keep every existing transaction; their keys behave as before.
- No function signature loses a parameter; `compare` is new and optional. The port's two operations are unchanged; the contract gains one documented exception.
- Strictness is new for any external caller of `grant_in`/`debit_in` that reused a key with different content and relied on being told "duplicate": it now fails, which is the point; it is a deliberate behaviour change and is named as such in the PR.
