-- Backs app/agent/tool_idempotency.py::idempotent — the exactly-once guard
-- every "mutating"/"outward" tool (TOOL_CAPABILITIES) now goes through
-- before performing its real side effect.
--
-- tool_call_id (the LLM-provider-assigned id on each AIMessage.tool_calls
-- entry) is the PRIMARY KEY: two invocations sharing one are, by
-- construction, the SAME logical invocation, never two different intended
-- actions colliding — a fresh LLM decision always gets a fresh id. That's
-- what makes an unconditional INSERT ... ON CONFLICT DO NOTHING a correct
-- exactly-once claim with no separate locking needed.
--
-- `result` is NULL while the call is still in flight (reserved but not yet
-- complete) and set once by idempotent() after fn() returns — a second
-- caller that raced the INSERT and lost sees NULL and proceeds to run fn()
-- itself rather than blocking (see that function's own docstring for why
-- this narrow window is an accepted tradeoff, not a bug: the thread lock
-- app/job_queue/queue.py::acquire_thread_lock already holds for a job's
-- whole run means two DIFFERENT jobs can never legitimately be executing
-- the SAME tool_call_id at the same real moment in this app today).
--
-- tenant/thread_id/tool_name are denormalized observability metadata only
-- (an operator asking "which conversation produced this dedup hit"), never
-- read by idempotent()'s own correctness logic.
\connect appdata

CREATE TABLE tool_call_dedup (
    tool_call_id TEXT PRIMARY KEY,
    tenant TEXT NOT NULL,
    thread_id TEXT,
    tool_name TEXT NOT NULL,
    result TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Lets an operator find "every dedup hit for this thread" without a full
-- table scan; not on the hot path (idempotent() only ever looks up by the
-- primary key).
CREATE INDEX tool_call_dedup_thread_id_idx ON tool_call_dedup (thread_id);
