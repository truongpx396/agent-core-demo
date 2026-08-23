-- Dedicated database for LangGraph's Postgres checkpointer (app/agent.py) —
-- separate from `appdata` because AsyncPostgresSaver.setup() owns and
-- migrates its own multi-table schema (checkpoints/checkpoint_blobs/
-- checkpoint_writes); that shouldn't share a lifecycle with hand-maintained
-- app tables (employees, usage_ledger). No DDL here: the schema is created
-- by AsyncPostgresSaver.setup() at process startup (app/agent.py's
-- _open_checkpointer), idempotently, on every process including each
-- independently-scaled agent_worker.py instance.
CREATE DATABASE checkpointer OWNER langfuse;
