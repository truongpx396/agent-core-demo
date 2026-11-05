# scripts

Run as `python -m scripts.<group>.<name>`; the Makefile has a target for most.

| Group | What | Examples |
|---|---|---|
| `jobs/` | Scheduled one-shots, safe to run from cron or a compose service | `followup_sweep`, `ops_digest`, `usage_events_sweep`, `usage_reconcile` |
| `checks/` | Run against a live stack or model and report pass/fail | `authlimit_smoke`, `mcp_gateway_smoke`, `gateway_check`, `toolcall_probe`, `eval` |
| `tools/` | Operator and developer CLIs, run when needed | `budget_policy`, `litellm_key`, `reembed`, `seed`, `index_skills`, `gen_auth_vectors` |
| `model_eval/` | The model-evaluation harness (`evals/README.md`) | `make model-eval` |
| `ai_review/` | The AI code reviewer CI runs | `.github/workflows/ai-review.yml` |
