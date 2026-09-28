-- Closes a real gap tool_call_dedup (13-tool-call-dedup.sql) narrowly
-- accepts as a race, for the three pure-INSERT mutating tools
-- (create_ticket/log_incident/add_followup — app/domains/*/store.py):
-- that table's own docstring documents "if the row read back has result
-- IS NULL, this caller runs fn() itself too" as an accepted double-run
-- window under the SAME tool_call_id. Without a constraint on the
-- TARGET table itself, that accepted race becomes a real duplicate ROW
-- here — a second ticket/incident/follow-up, not just a second dedup-table
-- claim. `ON CONFLICT (tool_call_id) DO NOTHING` on each of these tables
-- makes the actual side effect exactly-once too, at the one point that
-- matters (the row itself), independent of tool_call_dedup's own state.
--
-- Nullable, not NOT NULL: rows written by anything other than the agent's
-- own tool wrappers (a future admin/CLI insert path, a seed script) never
-- had a tool_call_id to begin with, and standard SQL UNIQUE semantics
-- already treat every NULL as distinct from every other NULL — any number
-- of NULL rows coexist without tripping the constraint.
--
-- Deliberately NOT a general "same ticket twice" guard — a business-key
-- uniqueness rule (e.g. "one open ticket per tenant+requester+subject")
-- is a product decision, not an engineering one, and is left as a named
-- follow-up (see GRAPH_PATTERNS.md). This closes the tool_call_id-keyed
-- REPLAY window specifically: the SAME tool_call_id landing twice, never
-- two tool_call_ids the agent genuinely decided to use.
\connect appdata

ALTER TABLE support_tickets ADD COLUMN tool_call_id TEXT UNIQUE;
ALTER TABLE ops_incidents ADD COLUMN tool_call_id TEXT UNIQUE;
ALTER TABLE crm_followups ADD COLUMN tool_call_id TEXT UNIQUE;
