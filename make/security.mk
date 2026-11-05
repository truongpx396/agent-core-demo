# Security scanners, and DefectDojo, which collects their findings. Included by the Makefile.

# --- Scanners ---

semgrep:  ## SAST over app/ scripts/ services/ml-service/ deploy/images/ Dockerfile via Docker; same rulesets and hard gate as CI's `semgrep` job (the p/* configs need no `semgrep login`)
	docker run --rm -v $(PWD):/src -w /src semgrep/semgrep:1.177.0 semgrep scan --error --metrics=off \
		--config p/security-audit --config p/secrets --config p/python --config p/dockerfile \
		app/ scripts/ services/ml-service/ deploy/images/ Dockerfile

trivy:  ## Scan dependencies, Dockerfile/compose and secrets for known vulnerabilities via Docker; same policy as CI's `trivy` job
	# Scans requirements-lock.txt, not requirements.txt: version ranges resolve to no installed version, so that finds nothing
	# (the lock file found 5 fixable HIGH CVEs on first run, incl. an RCE in langgraph-checkpoint, CVE-2025-64439).
	# Check garak/requirements-garak.txt before bumping pins. deploy/terraform is Checkov's: Trivy flags the necessary public
	# 80/443 ingress there as CRITICAL (see CI's `trivy` job).
	docker run --rm -v $(PWD):/repo aquasec/trivy:0.74.0 fs \
		--scanners vuln,secret,misconfig --severity HIGH,CRITICAL --ignore-unfixed \
		--file-patterns 'pip:requirements-lock\.txt$$' \
		--skip-dirs .venv,**/node_modules,.git,.mypy_cache,.ruff_cache,.pytest_cache,deploy/terraform /repo

trivy-image:  ## Build the app image and scan it for OS/library vulnerabilities
	docker build -t graph-agent-core:trivy .
	docker run --rm -v /var/run/docker.sock:/var/run/docker.sock aquasec/trivy:0.74.0 image \
		--severity HIGH,CRITICAL --ignore-unfixed graph-agent-core:trivy

checkov:  ## IaC misconfiguration scan of deploy/terraform; same config and gate as CI's `checkov` job. pip, not Docker: the bridgecrewio/checkov image no longer pulls
	pip install -q checkov==3.3.17
	checkov --config-file .checkov.yaml

# --- SonarQube ---

sonar-up:  ## Start a persistent SonarQube (Community) at http://localhost:9002 in the opt-in `quality` profile. First login admin/admin; change the password before a real `make sonar-scan`
	$(COMPOSE) --profile quality up -d sonarqube-db sonarqube

sonar-down:  ## Stop the persistent SonarQube (volumes and history kept)
	$(COMPOSE) --profile quality stop sonarqube sonarqube-db

sonar-scan:  ## Scan against the `make sonar-up` server (needs SONAR_TOKEN: My Account -> Security). The gate blocks only NEW issues; the baseline scan found ~90 older ones (2 BLOCKER, 10 CRITICAL) to triage
	@test -n "$$SONAR_TOKEN" || (echo "SONAR_TOKEN is required — generate one in the SonarQube UI (My Account -> Security)" && exit 1)
	# Joins the compose network and uses the in-network sonarqube:9000, not --network host (CI's choice): host networking
	# is unreliable on macOS.
	docker run --rm --network $(COMPOSE_NETWORK) \
		-e SONAR_HOST_URL=http://sonarqube:9000 -e SONAR_TOKEN=$$SONAR_TOKEN \
		-v $(PWD):/usr/src -w /usr/src \
		sonarsource/sonar-scanner-cli@sha256:23ca0f137965d9dff2198074043fd48d386280bc5d0ccac8c8349cea4cf096a9

# --- Dynamic and AI pentest (only systems you own or may test) ---

zap-baseline:  ## OWASP ZAP baseline scan (spider + passive rules only) of a running target, default the local API; ZAP_TARGET=<url> overrides. Reports in zap_reports/ (HTML + the XML `make defectdojo-import` expects)
	mkdir -p zap_reports
	# --add-host: a bare `docker run` is not on the compose network, and native Linux Docker does not map
	# host.docker.internal (Docker Desktop does), which the `api` service gets through extra_hosts.
	docker run --rm --add-host=host.docker.internal:host-gateway \
		-v $(PWD)/zap_reports:/zap/wrk:rw -t zaproxy/zap-stable zap-baseline.py \
		-t $${ZAP_TARGET:-http://host.docker.internal:8000} \
		-x baseline-report.xml -r baseline-report.html -I

zap-api-scan:  ## OWASP ZAP ACTIVE scan driven by the app's OpenAPI spec (needs `make serve` or `make up-app`), the standard-scanner counterpart of `make strix-app`. Only against a target you own or may test
	mkdir -p zap_reports
	docker run --rm --add-host=host.docker.internal:host-gateway \
		-v $(PWD)/zap_reports:/zap/wrk:rw -t zaproxy/zap-stable zap-api-scan.py \
		-t $${ZAP_TARGET:-http://host.docker.internal:8000}/openapi.json -f openapi \
		-x api-scan.xml -r api-scan.html -I

zap-view:  ## Open the newest ZAP HTML report (from `make zap-baseline` / `make zap-api-scan`)
	open "$$(ls -t zap_reports/*.html | head -1)" 2>/dev/null || xdg-open "$$(ls -t zap_reports/*.html | head -1)"

# Strix reads neither `.env` nor anything `make` can hand it, only the shell's exports. Load just its five documented names from `.env`,
# not the whole file (it also holds OPENAI_API_BASE, which Strix would take as LLM_API_BASE, and secrets it has no use for; those aliases
# are unset too). A non-empty `.env` value beats an exported one; values are read literally (one pair of quotes stripped), never evaluated.
STRIX_ENV = for n in STRIX_LLM LLM_API_KEY LLM_API_BASE EXA_API_KEY PERPLEXITY_API_KEY; do v=$$(sed -n -E "s/^$$n=(.+)$$/\1/p" .env 2>/dev/null | tail -n 1); case "$$v" in \"*\") v=$${v\#\"}; v=$${v%\"};; \'*\') v=$${v\#\'}; v=$${v%\'};; esac; [ -n "$$v" ] && export "$$n=$$v"; done; env -u OPENAI_API_BASE -u LITELLM_BASE_URL -u OLLAMA_API_BASE

strix:  ## Autonomous AI pentest of this repo's SOURCE. Needs Docker, `pipx install strix-agent` and a CLOUD LLM key (STRIX_LLM / LLM_API_KEY in .env or the shell). Only against a target you own or may test; extra flags via ARGS="--instruction '...'"
	$(STRIX_ENV) strix --target . --scan-mode quick $(ARGS)

strix-app:  ## The same tool, black-box against the RUNNING app and its OpenAPI spec (needs `make up`, `make serve` or `make up-app`, and `make ingest` for data to probe)
	$(STRIX_ENV) strix --target http://localhost:8000/openapi.json --target http://localhost:8000 --scan-mode quick $(ARGS)

strix-view:  ## Open the local dashboard (findings, repro steps) for the last `make strix` / `make strix-app` run
	strix view

# --- DefectDojo ---

# Pinned DefectDojo release, cloned into ~/.cache rather than run from docker-compose.yml: a 6-container app with its own release
# cadence. DJANGO_VERSION/NGINX_VERSION are set to match; both 3.3.100 images exist on Docker Hub (the compose file defaults to `:latest`).
DEFECTDOJO_VERSION := 3.3.100
DEFECTDOJO_DIR := $(HOME)/.cache/graph-agent-core-defectdojo

defectdojo-up:  ## Start a persistent DefectDojo (http://localhost:8080) for `make defectdojo-import`. The first run clones the pinned release into ~/.cache/graph-agent-core-defectdojo and prints the one-time admin password (user: admin): save it
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

defectdojo-down:  ## Stop the persistent DefectDojo (volumes and findings kept)
	cd "$(DEFECTDOJO_DIR)" && docker compose stop

defectdojo-import:  ## Import a scan report into `make defectdojo-up`: DEFECTDOJO_API_KEY=... make defectdojo-import REPORT=<path> SCAN_TYPE="<scan_type>" [ENGAGEMENT=<name>] (key: My Account -> API v2 Key; e.g. SCAN_TYPE="ZAP Scan")
	@test -n "$$DEFECTDOJO_API_KEY" || (echo "DEFECTDOJO_API_KEY is required — generate one in the DefectDojo UI (My Account -> API v2 Key)" && exit 1)
	@test -n "$$REPORT" || (echo "REPORT=<path to scan report file> is required" && exit 1)
	@test -n "$$SCAN_TYPE" || (echo 'SCAN_TYPE="<DefectDojo scan_type string>" is required, e.g. SCAN_TYPE="ZAP Scan"' && exit 1)
	python -m scripts.tools.defectdojo_import --file "$$REPORT" --scan-type "$$SCAN_TYPE" $${ENGAGEMENT:+--engagement-name "$$ENGAGEMENT"}
