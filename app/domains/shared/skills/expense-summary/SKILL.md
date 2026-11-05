---
name: expense-summary
description: Summarize and total a list of expense line items into a reimbursement-ready report. Use when the user pastes expense line items, receipts, or costs and wants them summed, categorized, or formatted as an expense report.
---

# Expense Summary

Turn a list of raw expense line items into a short reimbursement report.

1. For each line item, use the `calculator` tool to compute any sums or
   subtotals you need (per-category totals, then the grand total) — never
   add numbers yourself in prose; every arithmetic result the report states
   must come from an actual `calculator` call.
2. Group line items by category if the user's input implies categories
   (e.g. "travel", "meals", "software"); otherwise list them as one group.
3. Format the final answer as:
   - One line per item: `- <description>: $<amount>`
   - A subtotal line per category (if categorized).
   - A single **Total: $<grand total>** line at the end, computed via
     `calculator` from the category subtotals (or all items, if
     uncategorized).

Numbers from the calculator are not retrieved-document citations — do not
add a `[n]` marker to them.
