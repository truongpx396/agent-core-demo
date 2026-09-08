---
name: deal-economics
description: Compute real multi-year deal math a plain calculator can't do — escalating annual pricing, volume-discount tiers, total contract value. Use when a lead is negotiating anything beyond a flat single-year price.
domains: [sales]
---

# Deal Economics

`calculator` only evaluates a single arithmetic expression — it can't hold
a loop or a multi-year schedule. For anything beyond one flat number, write
a short script and run it with `run_command_in_sandbox` instead.

## The pattern

```python
base = 50000          # year-1 price
escalation = 0.05      # annual increase, e.g. 5%
years = 3
discount = 0.10        # volume discount applied to the total, e.g. 10%

total = sum(base * ((1 + escalation) ** y) for y in range(years)) * (1 - discount)
print(f"{years}-year total after {discount:.0%} volume discount: {total:.2f}")
```

Adjust the four inputs to what the lead actually asked for — a flat
multi-year total with no escalation just sets `escalation = 0`; a
per-tier discount schedule (e.g. 5% at 2 years, 10% at 3+) is a small
`if`/`elif` on `years` instead of one fixed `discount` value. Print each
year's own number too if the lead wants to see the breakdown, not just
the total.

## Before you compute

1. Get the real inputs from the conversation — base price, term length,
   any escalation or discount the lead is asking about — never invent a
   number they didn't give you.
2. If the lead's own company site might have relevant context (team size,
   likely deal size band) that's already been crawled via
   `enrich_lead_from_website`, that text is already in their notes —
   pull specific figures from it with a script here instead of guessing.
3. Present the result plainly in your answer — the actual total, not just
   "I calculated it in the sandbox."

`run_command_in_sandbox` has no network access and no numpy/pandas
requirement for this — plain Python arithmetic is enough for deal math.
