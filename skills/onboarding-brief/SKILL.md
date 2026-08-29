---
name: onboarding-brief
description: Compose a new-hire onboarding brief for an Acme Corp employee. Use when the user asks for an onboarding brief, a welcome packet, a new-hire summary, or wants to prep for someone's first day.
---

# Onboarding Brief

Produce a short onboarding brief for a named new hire (or a named
department, if no specific person is given).

1. Call `query_employees` to look up the person (or the department's
   roster if no name was given). If the name doesn't match anyone, say so
   plainly instead of guessing who was meant.
2. Call `search_docs` with `topic=company` for any Acme Corp policies or
   background relevant to that person's department (e.g. team norms,
   onboarding policy notes already in the knowledge base).
3. Write the brief as:
   - **Role**: title, department, start context (hired date from
     query_employees).
   - **Team & background**: 1-2 sentences drawn from whatever `search_docs`
     returned, cited normally (`[n]`) per the usual citation rules.
   - **First-week checklist**: 3-5 concrete, generic onboarding items (e.g.
     "meet your manager", "set up your workstation") — these are
     boilerplate, not sourced from a document, so they are never cited.

If `query_employees` finds no match at all, stop after step 1 and report
that plainly rather than inventing a brief for someone who isn't in the
directory.
