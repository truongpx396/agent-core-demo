# Back-office operator commands: run once and exit (the long-running loops are in host.mk). Included by the Makefile.

# --- Sweeps and digests ---

ops-digest:  ## One-shot metrics digest from the ops bot to the team channel (cron it)
	python -m scripts.ops_digest

followup-sweep:  ## One-shot sweep of due sales follow-ups, drafting nudges for a human to review (cron it)
	python -m scripts.followup_sweep

tool-call-dedup-sweep:  ## Delete stale tool_call_dedup rows past their retention window (cron it)
	python -m scripts.tool_call_dedup_sweep

usage-events-sweep:  ## Delete usage events older than USAGE_EVENT_RETENTION_DAYS (min 35). The caps read this table: cron it only once the retention policy is decided
	python -m scripts.usage_events_sweep

usage-events-carry-over:  ## One-time, idempotent: copy older usage_ledger history into usage_events so the caps keep the month so far (run before deploying that change and once after; ARGS=--dry-run, ARGS="--tenant acme")
	python -m scripts.usage_events_carry_over $(ARGS)

billing-usage-sweep:  ## Delete billing usage events older than BILLING_USAGE_EVENT_RETENTION_DAYS (min 35), never one whose export is unfinished (cron it once retention is decided)
	python -m credit_billing.cli.usage_sweep

billing-inbox-sweep:  ## Delete applied/ignored billing webhook inbox rows older than BILLING_INBOX_RETENTION_DAYS, never open or quarantined ones (cron it once retention is decided)
	python -m credit_billing.cli.inbox_sweep

# --- Spend, keys, credit wallet, billing schema ---

budget-policy:  ## Per-tenant / per-person spend-limit overrides: make budget-policy ARGS="show --tenant acme --principal alice"
	python -m scripts.budget_policy $(ARGS)

litellm-key:  ## Mint or inspect the app's budget-capped LiteLLM key, set the default per-person budget: LITELLM_MASTER_KEY=... make litellm-key ARGS="create --max-budget 600" | ARGS="person-budget set --max-budget 20"
	python -m scripts.litellm_key $(ARGS)

credits:  ## Credit wallet CLI: make credits ARGS="grant --tenant acme --amount 500 --by alice --reason 'pilot top-up'"; also adjust (signed) and show. A change needs --by and --reason
	python -m credit_billing.cli.credits $(ARGS)

billing-migrate:  ## Apply the billing schema scripts to an existing database, in order; safe to re-run (ARGS=--lock-timeout-seconds=30 waits longer)
	python -m credit_billing.cli.migrate $(ARGS)

billing-migrate-status:  ## Show which billing schema scripts are applied, pending or changed, writing nothing; ARGS=--check exits 1 if any is not current (a deploy gate)
	python -m credit_billing.cli.migrate --status $(ARGS)

# --- Reconcile checks ---

credit-reconcile:  ## Wallet check: was every usage in the last CREDIT_RECONCILE_LOOKBACK_DAYS charged? Exit 0 ok / 1 drift / 2 unfinished / 3 database unreachable; ARGS="--days 7 --json"
	python -m credit_billing.cli.reconcile $(ARGS)

usage-reconcile:  ## Meter check: do usage events match the LLM gateway's spend log over the last USAGE_RECONCILE_LOOKBACK_DAYS? Exit 0 agree / 1 drift / 2 unfinished. Needs LITELLM_MASTER_KEY; ARGS="--days 7 --json"
	python -m scripts.usage_reconcile $(ARGS)

# --- People and tokens ---

telegram-person:  ## Add, disable or re-enable a Telegram person at the authorization server: make telegram-person ACTION=add|disable|enable TELEGRAM_ID=<id>. Needs TELEGRAM_PERSON_SECRET and the gateway stack's Casdoor
	@test -n "$(TELEGRAM_ID)" || { echo "usage: make telegram-person ACTION=add|disable|enable TELEGRAM_ID=<telegram user id>"; exit 2; }
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.telegram_person $(or $(ACTION),add) --telegram-id "$(TELEGRAM_ID)"

authlimit-revoke:  ## Void a person's tokens on every authlimit replica within seconds: make authlimit-revoke TENANT=ecorp SUB=<person id>
	@test -n "$(TENANT)" -a -n "$(SUB)" || { echo "usage: make authlimit-revoke TENANT=<tenant> SUB=<subject>"; exit 2; }
	$(GATEWAY_COMPOSE) exec -T --index 1 authlimit /authlimit revoke --tenant "$(TENANT)" --sub "$(SUB)"
