-- Records the CONCRETE model a chat alias resolved to at call time
-- (app/model_resolver.py, GRAPH_PATTERNS.md pattern 38) — never a metric
-- label, never readable by a graph node; visible only in this ledger's
-- own forensics, alongside the alias already recorded per row.
\connect appdata

ALTER TABLE usage_ledger ADD COLUMN resolved_model TEXT;
