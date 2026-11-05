# Evals of the model, the prompts and the retrieval, and the red team. Included by the Makefile.

# --- Model and retrieval evals ---

eval:  ## Golden-dataset evaluation against the real stack (needs `make up` + `make ingest`)
	python -m scripts.checks.eval

model-eval:  ## Evaluate ONE model against its recorded baseline: make model-eval KIND=embedding|reranker|screen|classifier CANDIDATE=<name> [VIA=gateway|service|inprocess] [DATASET=<name>] [OUTCOMES=<file>] (specs/014)
	@test -n "$(KIND)" -a -n "$(CANDIDATE)" || { echo "usage: make model-eval KIND=embedding CANDIDATE=<name> [VIA=...] [DATASET=...]"; exit 2; }
	python -m scripts.model_eval run --kind $(KIND) --candidate $(CANDIDATE) $(if $(VIA),--via $(VIA)) $(if $(DATASET),--dataset $(DATASET)) $(if $(OUTCOMES),--outcomes $(OUTCOMES))

model-eval-all:  ## The same candidate for every kind that declares it: make model-eval-all CANDIDATE=<name>
	@test -n "$(CANDIDATE)" || { echo "usage: make model-eval-all CANDIDATE=<name>"; exit 2; }
	python -m scripts.model_eval run --all-kinds --candidate $(CANDIDATE) $(if $(VIA),--via $(VIA))

model-eval-baseline:  ## RECORD an immutable baseline (FORCE=1 replaces, ADD_HARDWARE=1 adds this machine's latency): make model-eval-baseline KIND=... CANDIDATE=...
	@test -n "$(KIND)" -a -n "$(CANDIDATE)" || { echo "usage: make model-eval-baseline KIND=embedding CANDIDATE=<name>"; exit 2; }
	python -m scripts.model_eval baseline --kind $(KIND) --candidate $(CANDIDATE) $(if $(VIA),--via $(VIA)) $(if $(DATASET),--dataset $(DATASET)) $(if $(FORCE),--force) $(if $(ADD_HARDWARE),--add-hardware)

model-eval-data:  ## Download and verify the pinned public evaluation datasets (needs the network once)
	python -m scripts.model_eval fetch

model-eval-verify:  ## Check every committed dataset, baseline, licence and control; no model or network
	python -m scripts.model_eval verify

labelset-calibrate:  ## Choose a label set's "none of these" cutoff for ONE model on the dev split: make labelset-calibrate CANDIDATE=labelset-<model> [LABELSET=route-topics] [KEEP=0.95]; prints the `calibration` line (specs/015)
	@test -n "$(CANDIDATE)" || { echo "usage: make labelset-calibrate CANDIDATE=<a classifier candidate with source.model, e.g. labelset-bge-small-en-v1.5>"; exit 2; }
	python -m scripts.model_eval labelset-calibrate --candidate $(CANDIDATE) $(if $(LABELSET),--labelset $(LABELSET)) $(if $(KEEP),--keep $(KEEP))

router-label:  ## Score the route prompts' answers from the local and strong model with a judge (slow, needs the gateway): make router-label JUDGE=<alias> [LOCAL=chat] [STRONG=chat-strong] [DEADLINE=<s>] [LIMIT=<n>]; resumable
	@test -n "$(JUDGE)" || { echo "usage: make router-label JUDGE=<gateway alias that scores the answers>"; exit 2; }
	python -m scripts.model_eval route-label --judge $(JUDGE) $(if $(LOCAL),--local $(LOCAL)) $(if $(STRONG),--strong $(STRONG)) $(if $(STRONG_ID),--strong-id $(STRONG_ID)) $(if $(DEADLINE),--deadline $(DEADLINE)) $(if $(LIMIT),--limit $(LIMIT))

probe-tools:  ## Does the chat model emit usable tool calls? 25 labelled prompts plus 4 that must not loop, ~2 min on native Ollama (ARGS=--no-loops: quick half). Run after ANY change to a chat alias's model or repeat_penalty (GRAPH_PATTERNS.md 73)
	python -m scripts.checks.toolcall_probe $(ARGS)

# --- Prompt checks and LLM-judged quality ---

promptfoo:  ## Prompt regression checks for the domain system prompts against a real Ollama (needs `make up` or a native Ollama with CHAT_MODEL pulled)
	cd promptfoo && npm install --include=optional
	python -m promptfoo.dump_prompts
	cd promptfoo && for domain in support ops sales; do \
		npx promptfoo eval --config $$domain.yaml || exit 1; \
	done

deepeval:  ## LLM-judged RAG quality, conversation simulation and tool-call checks on the real graph (needs Docker, GOOGLE_API_KEY, GROQ_API_KEY). Read the reasons, not just pass/fail (GRAPH_PATTERNS.md 48); CI never fails on a score
	DEEPEVAL_TELEMETRY_OPT_OUT=1 pytest -m deepeval -q -s --reruns 2 --reruns-delay 10 --only-rerun "(ServerError|UNAVAILABLE|RESOURCE_EXHAUSTED|DEADLINE_EXCEEDED|RateLimitError|APITimeoutError|APIConnectionError|InternalServerError)" tests/deepeval

# --- Red team ---

promptfoo-redteam:  ## Adversarial variants of the support/sales/ops prompts (injection, policy violations, sandbox misuse) against the local target, graded by Gemini (needs GOOGLE_API_KEY; see promptfoo/redteam.yaml)
	cd promptfoo && npm install --include=optional
	python -m promptfoo.dump_prompts
	# Every promptfoo command runs from promptfoo/ (package.json lives there), so --config and --output are relative to it.
	# --max-concurrency 1 --delay 2100: serial calls >2s apart, under the free Gemini tier's 30 req/min (redteam.yaml, RATE LIMIT).
	# -o per stanza: `redteam run` writes to promptfoo/redteam.yaml whatever --config is (promptfoo's own default), which clobbered
	# the hand-authored support file once; so each scratch config is its own output. Explicit stanzas, not a loop: support's file has no domain prefix.
	# Leading `-`: promptfoo exits 100 as soon as any case fails, and a finding is the point; carry on to the next domain.
	-cp promptfoo/redteam.yaml promptfoo/.support-redteam-run-scratch.yaml
	-cd promptfoo && PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config .support-redteam-run-scratch.yaml --output .support-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100
	-cp promptfoo/sales-redteam.yaml promptfoo/.sales-redteam-run-scratch.yaml
	-cd promptfoo && PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config .sales-redteam-run-scratch.yaml --output .sales-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100
	-cp promptfoo/ops-redteam.yaml promptfoo/.ops-redteam-run-scratch.yaml
	-cd promptfoo && PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config .ops-redteam-run-scratch.yaml --output .ops-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100

garak:  ## Fast garak probes for known jailbreak/injection patterns (needs `make up` or a native Ollama); run in a SEPARATE Python env, not this .venv: garak upgrades langgraph-checkpoint past this app's pin
	python -m pip install -q -r garak/requirements-garak.txt
	python garak/run_ci_scan.py

garak-full:  ## The full, slow garak suite: a deliberate pre-release scan, not a per-PR gate; same separate-environment requirement as `make garak`
	python -m pip install -q -r garak/requirements-garak.txt
	OPENAICOMPATIBLE_API_KEY="sk-not-checked-by-ollama" python -m garak --config garak/config.yaml --target_type openai.OpenAICompatible --target_name "$${GARAK_MODEL:-qwen2.5:3b}"
