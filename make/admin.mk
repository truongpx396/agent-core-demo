# Back-office administration: operator commands that run once and exit, on the business side of the system. They act on the databases and the authorization
# server, not on a running process of the app: the sweeps and digests meant for cron, spend limits and keys, the credit wallet, the billing schema migrations,
# the reconcile checks, and the people and tokens of the front door. (The long-running reconcile and export loops are workers: make/host.mk.) Included by the
# Makefile, so every target is called exactly as before and `make help` lists them with everything else.

# --- Sweeps and digests (one-shot, meant for cron) -------------------------------------------------------------------------------------------------

ops-digest:  ## Run the ops bot's one-shot metrics digest, posting to the team channel (see scripts/ops_digest.py; meant for real cron)
	python -m scripts.ops_digest

followup-sweep:  ## Run the sales concierge's one-shot due-follow-up sweep, drafting nudges for a human to review (see scripts/followup_sweep.py; meant for real cron)
	python -m scripts.followup_sweep

tool-call-dedup-sweep:  ## Delete stale tool_call_dedup rows past their retention window (see scripts/tool_call_dedup_sweep.py; meant for real cron)
	python -m scripts.tool_call_dedup_sweep

usage-events-sweep:  ## Delete usage events older than USAGE_EVENT_RETENTION_DAYS (floor 35) (see scripts/usage_events_sweep.py; the table the caps read, so wire to cron only once your retention policy is decided)
	python -m scripts.usage_events_sweep

usage-events-carry-over:  ## One-time, idempotent: copy usage_ledger history older than the first usage event into usage_events, because the dollar caps sum the events and would otherwise forget the month so far. Run it before deploying that change and once after. ARGS=--dry-run counts and writes nothing; ARGS="--tenant acme" does one tenant (see scripts/usage_events_carry_over.py)
	python -m scripts.usage_events_carry_over $(ARGS)

billing-usage-sweep:  ## Delete the billing package's usage events older than BILLING_USAGE_EVENT_RETENTION_DAYS (floor 35), never one whose export never finished (credit-billing-usage-sweep; wire to cron once your retention policy is decided)
	python -m credit_billing.cli.usage_sweep

billing-inbox-sweep:  ## Delete applied/ignored billing webhook inbox rows older than BILLING_INBOX_RETENTION_DAYS (see packages/billing/src/credit_billing/cli/inbox_sweep.py; never touches open or quarantined rows; wire to cron once your retention policy is decided)
	python -m credit_billing.cli.inbox_sweep

# --- Spend limits, keys, the credit wallet and the billing schema ----------------------------------------------------------------------------------

budget-policy:  ## Operator CLI for per-tenant / per-person spend-limit overrides, e.g. `make budget-policy ARGS="show --tenant acme --principal alice"` (see scripts/budget_policy.py)
	python -m scripts.budget_policy $(ARGS)

litellm-key:  ## Mint / inspect the app's scoped, budget-capped LiteLLM key, set the default per-person budget, e.g. `LITELLM_MASTER_KEY=... make litellm-key ARGS="create --max-budget 600"` or `ARGS="person-budget set --max-budget 20"` (see scripts/litellm_key.py)
	python -m scripts.litellm_key $(ARGS)

credits:  ## Operator CLI for the credit wallet, e.g. `make credits ARGS="grant --tenant acme --amount 500 --by alice --reason 'pilot top-up'"`, `adjust` (signed) and `show` (balance, lots, entries); --by and --reason are required for a change (see packages/billing/src/credit_billing/cli/credits.py)
	python -m credit_billing.cli.credits $(ARGS)

billing-migrate:  ## Apply the billing package's schema scripts to an existing database, in order, and record them (see packages/billing/src/credit_billing/cli/migrate.py; safe to re-run and to run twice at once; `ARGS=--lock-timeout-seconds=30` to wait longer)
	python -m credit_billing.cli.migrate $(ARGS)

billing-migrate-status:  ## Show which billing schema scripts are applied, pending or changed, and write nothing; `make billing-migrate-status ARGS=--check` exits 1 if any is not current (a deploy gate)
	python -m credit_billing.cli.migrate --status $(ARGS)

# --- Reconcile checks ------------------------------------------------------------------------------------------------------------------------------

credit-reconcile:  ## Wallet check: was every recorded usage worth credits actually charged? Over the last CREDIT_RECONCILE_LOOKBACK_DAYS; exit 0 charged / 1 drift / 2 could not finish / 3 database unreachable (the billing package's credit-billing-reconcile; ARGS="--days 7 --json")
	python -m credit_billing.cli.reconcile $(ARGS)

usage-reconcile:  ## Meter check: do the usage events agree with the LLM gateway's spend log over the last USAGE_RECONCILE_LOOKBACK_DAYS? exit 0 agree / 1 drift / 2 could not finish. Needs LITELLM_MASTER_KEY; ARGS="--days 7 --json" (see scripts/usage_reconcile.py)
	python -m scripts.usage_reconcile $(ARGS)

# --- People and tokens at the authorization server and the front door ------------------------------------------------------------------------------

telegram-person:  ## Add, disable or re-enable a Telegram person at the authorization server (specs/020): make telegram-person ACTION=add|disable|enable TELEGRAM_ID=<telegram user id>. Needs TELEGRAM_PERSON_SECRET in .env and the gateway stack's Casdoor
	@test -n "$(TELEGRAM_ID)" || { echo "usage: make telegram-person ACTION=add|disable|enable TELEGRAM_ID=<telegram user id>"; exit 2; }
	@. $(GATEWAY_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.telegram_person $(or $(ACTION),add) --telegram-id "$(TELEGRAM_ID)"

authlimit-revoke:  ## Void every token issued to a person, on every authlimit replica within seconds: make authlimit-revoke TENANT=ecorp SUB=<the person's id>   (`authlimit revoke --list` shows them)
	@test -n "$(TENANT)" -a -n "$(SUB)" || { echo "usage: make authlimit-revoke TENANT=<tenant> SUB=<subject>"; exit 2; }
	$(GATEWAY_COMPOSE) exec -T --index 1 authlimit /authlimit revoke --tenant "$(TENANT)" --sub "$(SUB)"
