# Security scanning (README, "Security scanning"): the static and dependency scanners (Semgrep, Trivy, Checkov), SonarQube, the dynamic and AI pentest
# tools (ZAP, Strix) and DefectDojo, which collects their findings. Included by the Makefile, so every target is called exactly as before and `make help`
# lists them with everything else. The red-team checks of the model itself (`promptfoo-redteam`, `garak`) are evals: make/evals.mk.

# --- Static, dependency and image scanners ---------------------------------------------------------------------------------------------------------

semgrep:  ## Static analysis (SAST) over app/scripts/docker/Dockerfile via Docker (no local install) — same rulesets and hard gate as CI's `semgrep` job; see that job's own comment for why these `p/*` configs need no `semgrep login`
	docker run --rm -v $(PWD):/src -w /src semgrep/semgrep:1.177.0 semgrep scan --error --metrics=off \
		--config p/security-audit --config p/secrets --config p/python --config p/dockerfile \
		app/ scripts/ docker/ Dockerfile

trivy:  ## Scan dependencies/Dockerfile+compose/secrets for known vulns (aquasec/trivy via Docker — no local trivy install needed; same policy as CI's `trivy` job)
	# --file-patterns points trivy's pip analyzer at requirements-lock.txt,
	# not requirements.txt (trivy's own default match) — verified directly
	# this matters: requirements.txt's version RANGES (`langgraph>=0.2.20,<0.3`)
	# don't resolve to one installed version to check against the CVE
	# database, so scanning it alone silently finds nothing. Scanning the
	# exact-pinned lock file — what the Dockerfile/CI actually install,
	# per requirements-lock.txt's own header — surfaced 5 real HIGH-severity
	# fixable CVEs on first run here, including an RCE in langgraph-checkpoint
	# (CVE-2025-64439); see this repo's own disclosed pin-compatibility
	# constraints (garak/requirements-garak.txt) before bumping it.
	# infra/terraform excluded — Checkov (`make checkov`) owns that
	# directory's IaC scanning; see CI's `trivy` job for why (Trivy's own
	# DigitalOcean firewall policies flag this repo's necessary public
	# 80/443 ingress + broad egress as CRITICAL with no way to distinguish
	# it from an actually-dangerous open port).
	docker run --rm -v $(PWD):/repo aquasec/trivy:0.74.0 fs \
		--scanners vuln,secret,misconfig --severity HIGH,CRITICAL --ignore-unfixed \
		--file-patterns 'pip:requirements-lock\.txt$$' \
		--skip-dirs .venv,**/node_modules,.git,.mypy_cache,.ruff_cache,.pytest_cache,infra/terraform /repo

trivy-image:  ## Build the app image (see Dockerfile) and scan it for OS/library vulnerabilities
	docker build -t graph-agent-core:trivy .
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:0.74.0 image \
		--severity HIGH,CRITICAL --ignore-unfixed graph-agent-core:trivy

checkov:  ## Scan infra/terraform for IaC misconfigurations — same config/gate as CI's `checkov` job. pip install, not Docker: verified directly bridgecrewio/checkov's Docker Hub image no longer pulls ("repository does not exist"), unlike every other Docker-based scanner target in this file
	pip install -q checkov==3.3.17
	checkov --config-file .checkov.yaml

# --- SonarQube: code quality gate (opt-in `quality` compose profile) -------------------------------------------------------------------------------

sonar-up:  ## Start a PERSISTENT self-hosted SonarQube (Community Edition) at http://localhost:9002 — opt-in `quality` profile (see docker-compose.yml's own comment on why 9002, not 9000/9001, and why this is separate from CI's ephemeral `sonarqube` job). First login: admin/admin, then change the password before running `make sonar-scan` for real (a throwaway local instance can skip that, same as CI's own ephemeral one).
	$(COMPOSE) --profile quality up -d sonarqube-db sonarqube

sonar-down:  ## Stop the persistent SonarQube server (keeps its volumes — history/trends survive)
	$(COMPOSE) --profile quality stop sonarqube sonarqube-db

sonar-scan:  ## Run a scan against the persistent `make sonar-up` server (needs SONAR_TOKEN — generate one under My Account -> Security in the UI first). The very first baseline scan of this repo found ~90 pre-existing issues (2 BLOCKER, 10 CRITICAL) that the default "clean new code" quality gate does NOT block on — see the dashboard to triage them; that gate only fails a scan when NEW code introduces a new issue.
	@test -n "$$SONAR_TOKEN" || (echo "SONAR_TOKEN is required — generate one in the SonarQube UI (My Account -> Security)" && exit 1)
	# Joins docker-compose.yml's own network and talks to the `sonarqube`
	# service by its in-network name/port (9000, not the 9002 host
	# publish) — NOT `--network host` (what CI's ephemeral job uses, see
	# that job's own comment): host networking is Linux-only-reliable and
	# this target needs to work the same on a Mac dev machine too.
	docker run --rm --network $(COMPOSE_NETWORK) \
		-e SONAR_HOST_URL=http://sonarqube:9000 -e SONAR_TOKEN=$$SONAR_TOKEN \
		-v $(PWD):/usr/src -w /usr/src \
		sonarsource/sonar-scanner-cli@sha256:23ca0f137965d9dff2198074043fd48d386280bc5d0ccac8c8349cea4cf096a9

# --- Dynamic and AI pentest: only against a system you own or have written permission to test ------------------------------------------------------

zap-baseline:  ## OWASP ZAP baseline DAST scan (spider + PASSIVE rules only, no active attacks — safe against anything you merely have read access to) against a running target, default the local API (`make serve`/`make up-app`). Override with ZAP_TARGET=<url>. Docker-based (zaproxy/zap-stable, no local ZAP install needed); reports land under zap_reports/ (gitignored) as HTML + the XML format `make defectdojo-import` (DefectDojo's own ZAP parser — verified directly it wants XML, not the JSON zap-baseline.py can also emit) expects.
	mkdir -p zap_reports
	# --add-host: this is a standalone `docker run`, not joined to
	# docker-compose.yml's own network, so it needs the same
	# host.docker.internal mapping that file's own `api`/`agent-worker`
	# services set up for themselves (extra_hosts) to reach a host-process
	# `make serve` OR the containerized `make up-app` (both publish :8000
	# to the host either way) — native Linux Docker doesn't wire this up
	# for a bare `docker run` the way Docker Desktop does automatically.
	docker run --rm --add-host=host.docker.internal:host-gateway \
		-v $(PWD)/zap_reports:/zap/wrk:rw -t zaproxy/zap-stable zap-baseline.py \
		-t $${ZAP_TARGET:-http://host.docker.internal:8000} \
		-x baseline-report.xml -r baseline-report.html -I

zap-api-scan:  ## OWASP ZAP ACTIVE scan driven by the app's own OpenAPI spec (needs `make serve`/`make up-app` running) — the one that actually attacks each documented endpoint's input handling, same "dynamic pass against the live app" tier as `make strix-app`, just OWASP's own standard scanner instead of an autonomous agent. Only ever run against a target you own or have explicit permission to test — same rule `make strix-app` states for itself.
	mkdir -p zap_reports
	docker run --rm --add-host=host.docker.internal:host-gateway \
		-v $(PWD)/zap_reports:/zap/wrk:rw -t zaproxy/zap-stable zap-api-scan.py \
		-t $${ZAP_TARGET:-http://host.docker.internal:8000}/openapi.json -f openapi \
		-x api-scan.xml -r api-scan.html -I

zap-view:  ## Open the most recently modified ZAP HTML report (from `make zap-baseline`/`make zap-api-scan`)
	open "$$(ls -t zap_reports/*.html | head -1)" 2>/dev/null || xdg-open "$$(ls -t zap_reports/*.html | head -1)"

# Strix reads neither `.env` nor anything `make` could hand it: a variable reaches it only when it is exported in the shell
# (or stored in ~/.strix/cli-config.json), so a STRIX_LLM written in `.env` alone gives "STRIX_LLM is not set". Load JUST the
# names it documents (the model, its key and base URL, and the two optional web-search keys), not the whole file: the file also
# carries OPENAI_API_BASE (the local LiteLLM proxy), which Strix reads as an alias of LLM_API_BASE and would send the scan's
# cloud-model calls to, and every other secret in it, which a pentest agent has no use for. The aliases are unset for the
# same reason, in case the shell exported them. A non-empty value in `.env` wins over one already exported; an empty
# `NAME=` line is ignored, and the last of two lines wins. The file is READ, never evaluated: a value is taken literally
# (one pair of matching quotes is stripped; no `$VAR`, no inline `# comment`), so a key with a space or a `;` in it is data
# and not a command the shell would run.
STRIX_ENV = for n in STRIX_LLM LLM_API_KEY LLM_API_BASE EXA_API_KEY PERPLEXITY_API_KEY; do v=$$(sed -n -E "s/^$$n=(.+)$$/\1/p" .env 2>/dev/null | tail -n 1); case "$$v" in \"*\") v=$${v\#\"}; v=$${v%\"};; \'*\') v=$${v\#\'}; v=$${v%\'};; esac; [ -n "$$v" ] && export "$$n=$$v"; done; env -u OPENAI_API_BASE -u LITELLM_BASE_URL -u OLLAMA_API_BASE

# make strix ARGS="--instruction 'This is my own repo; I am the owner and authorize a defensive security review of its source.' --max-budget 1"
strix:  ## Autonomous AI pentest of this repo's SOURCE (static) — needs Docker, `pipx install strix-agent` once (pipx isolates it from this repo's own .venv, the pip-conflict concern `make garak` solves with a second venv instead), and a CLOUD LLM key (STRIX_LLM/LLM_API_KEY, read from .env or your shell — NOT the local Ollama the rest of this app runs on). Only ever point it at a target you own or have written authorization to test. Extra Strix flags go in ARGS: make strix ARGS="--instruction 'This is my own repo; ...'" (or --instruction-file, --max-budget 1)
	$(STRIX_ENV) strix --target . --scan-mode quick $(ARGS)

strix-app:  ## Same tool, but black-box against the RUNNING app + its OpenAPI spec (needs `make up` + `make serve`/`make up-app`, and `make ingest` for real data to probe) — dynamic testing, not just a source read
	$(STRIX_ENV) strix --target http://localhost:8000/openapi.json --target http://localhost:8000 --scan-mode quick $(ARGS)

strix-view:  ## Open the local dashboard (findings, repro steps, agent graph) for the most recent `make strix`/`make strix-app` run
	strix view

# --- DefectDojo: where the findings of the scans above are collected and deduplicated --------------------------------------------------------------

# Pinned DefectDojo release — see `defectdojo-up`'s own comment for why this
# is a plain git clone into ~/.cache (NOT vendored into this repo, same
# "external tool, own lifecycle" treatment `make strix` already gives
# pipx-installed strix-agent) rather than a docker-compose.yml service like
# sonarqube/sonarqube-db above: DefectDojo is a 6-container app with its own
# release cadence, not a two-container official image this repo can just
# declare inline. DJANGO_VERSION/NGINX_VERSION below are exported to match —
# verified directly both `defectdojo/defectdojo-django:3.3.100` and
# `defectdojo/defectdojo-nginx:3.3.100` exist on Docker Hub, not assumed
# from the compose file's own `:latest` default.
DEFECTDOJO_VERSION := 3.3.100
DEFECTDOJO_DIR := $(HOME)/.cache/graph-agent-core-defectdojo

defectdojo-up:  ## Start a PERSISTENT self-hosted DefectDojo (http://localhost:8080) — where `make defectdojo-import` sends ZAP/Trivy/Semgrep/Checkov findings to get deduplicated and tracked across runs, the same "opt-in, persistent, cross-run history" role `make sonar-up` plays for SonarQube above. First run clones the pinned release ($(DEFECTDOJO_VERSION)) into $(DEFECTDOJO_DIR) (NOT into this repo — see this file's own header comment) and pulls DefectDojo's prebuilt images (no `docker compose build` needed — verified directly the checked-in docker-compose.yml's own `image:` lines are enough); every run after that just restarts the same instance. Prints the initializer's one-time-generated admin password (user: admin) — SAVE IT, it isn't shown again (short of digging through `docker compose logs` in $(DEFECTDOJO_DIR) yourself).
	@mkdir -p $(HOME)/.cache
	@test -d "$(DEFECTDOJO_DIR)" || git clone --branch $(DEFECTDOJO_VERSION) --depth 1 https://github.com/DefectDojo/django-DefectDojo.git "$(DEFECTDOJO_DIR)"
	cd "$(DEFECTDOJO_DIR)" && DJANGO_VERSION=$(DEFECTDOJO_VERSION) NGINX_VERSION=$(DEFECTDOJO_VERSION) docker compose up -d
	@echo "Waiting for the initializer (first run: DB migration + seed data, up to ~3 min)..."
	@for i in $$(seq 1 60); do \
		cd "$(DEFECTDOJO_DIR)" && docker compose logs initializer 2>/dev/null | grep -q "Admin password:" && break; \
		sleep 5; \
	done
	@cd "$(DEFECTDOJO_DIR)" && docker compose logs initializer 2>/dev/null | grep "Admin password:" || echo "(already initialized on a previous run — admin password was only ever printed once; see DefectDojo's own docs to reset it if lost)"
	@echo "DefectDojo: http://localhost:8080 (user: admin) — generate an API v2 Key under My Account for \`make defectdojo-import\`"

defectdojo-down:  ## Stop the persistent DefectDojo server (keeps its volumes — findings/history survive)
	cd "$(DEFECTDOJO_DIR)" && docker compose stop

defectdojo-import:  ## Import one scan report into the running `make defectdojo-up` instance (scripts/defectdojo_import.py) — needs DEFECTDOJO_API_KEY (My Account -> API v2 Key in the UI) plus REPORT=<path> SCAN_TYPE="<DefectDojo scan_type string>" [ENGAGEMENT=<name>]. E.g.: `DEFECTDOJO_API_KEY=... make defectdojo-import REPORT=zap_reports/baseline-report.xml SCAN_TYPE="ZAP Scan"` — auto-creates the Product/Engagement on first import, no manual UI setup needed.
	@test -n "$$DEFECTDOJO_API_KEY" || (echo "DEFECTDOJO_API_KEY is required — generate one in the DefectDojo UI (My Account -> API v2 Key)" && exit 1)
	@test -n "$$REPORT" || (echo "REPORT=<path to scan report file> is required" && exit 1)
	@test -n "$$SCAN_TYPE" || (echo 'SCAN_TYPE="<DefectDojo scan_type string>" is required, e.g. SCAN_TYPE="ZAP Scan"' && exit 1)
	python -m scripts.defectdojo_import --file "$$REPORT" --scan-type "$$SCAN_TYPE" $${ENGAGEMENT:+--engagement-name "$$ENGAGEMENT"}
