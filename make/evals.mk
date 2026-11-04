# Evaluations of the model, the prompts and the retrieval: the golden-set eval, the model-swap evals (evals/README.md), the prompt checks (promptfoo), the
# LLM-judged quality tests (deepeval) and the red team (promptfoo-redteam, garak). Run `make eval` and `make promptfoo` after a prompt, model-alias or
# retrieval change. Included by the Makefile, so every target is called exactly as before and `make help` lists them with everything else.

# --- Model and retrieval evals: the golden set, a model swap against its recorded baseline, the routing labels, the tool-call probe ----------------

eval:  ## Run the golden-dataset evaluation against the real stack (needs `make up` + `make ingest`)
	python -m scripts.eval

model-eval:  ## Evaluate ONE model against its recorded baseline: make model-eval KIND=embedding|reranker|screen|classifier CANDIDATE=<name> [VIA=gateway|service|inprocess] [DATASET=<name>] [OUTCOMES=<file>] (a label set: KIND=classifier CANDIDATE=labelset-bge-small-en-v1.5 DATASET=route-topics). A manual tier like `make eval` (needs models/services); specs/014, evals/README.md
	@test -n "$(KIND)" -a -n "$(CANDIDATE)" || { echo "usage: make model-eval KIND=embedding CANDIDATE=<name> [VIA=...] [DATASET=...]"; exit 2; }
	python -m scripts.model_eval run --kind $(KIND) --candidate $(CANDIDATE) $(if $(VIA),--via $(VIA)) $(if $(DATASET),--dataset $(DATASET)) $(if $(OUTCOMES),--outcomes $(OUTCOMES))

model-eval-all:  ## The same candidate for every kind that declares it: make model-eval-all CANDIDATE=<name> (a control such as baseline-as-candidate runs for all)
	@test -n "$(CANDIDATE)" || { echo "usage: make model-eval-all CANDIDATE=<name>"; exit 2; }
	python -m scripts.model_eval run --all-kinds --candidate $(CANDIDATE) $(if $(VIA),--via $(VIA))

model-eval-baseline:  ## RECORD a baseline (immutable; FORCE=1 to replace, ADD_HARDWARE=1 to add this machine's latency): make model-eval-baseline KIND=... CANDIDATE=...
	@test -n "$(KIND)" -a -n "$(CANDIDATE)" || { echo "usage: make model-eval-baseline KIND=embedding CANDIDATE=<name>"; exit 2; }
	python -m scripts.model_eval baseline --kind $(KIND) --candidate $(CANDIDATE) $(if $(VIA),--via $(VIA)) $(if $(DATASET),--dataset $(DATASET)) $(if $(FORCE),--force) $(if $(ADD_HARDWARE),--add-hardware)

model-eval-data:  ## Download and verify the pinned public evaluation datasets (needs the network once; offline afterwards)
	python -m scripts.model_eval fetch

model-eval-verify:  ## Check every committed evaluation dataset, baseline, licence and control, with no model and no network
	python -m scripts.model_eval verify

labelset-calibrate:  ## Choose a label set's "none of these" cutoff for ONE model on the dev split (needs the model cached): make labelset-calibrate CANDIDATE=labelset-bge-small-en-v1.5 [LABELSET=route-topics] [KEEP=0.95]. Prints the line to put under `calibration` in docker/ml-service/labelsets/<set>.json; specs/015
	@test -n "$(CANDIDATE)" || { echo "usage: make labelset-calibrate CANDIDATE=<a classifier candidate with source.model, e.g. labelset-bge-small-en-v1.5>"; exit 2; }
	python -m scripts.model_eval labelset-calibrate --candidate $(CANDIDATE) $(if $(LABELSET),--labelset $(LABELSET)) $(if $(KEEP),--keep $(KEEP))

router-label:  ## Score the route prompts' answers from the local and strong model with a judge (minutes per prompt; needs the gateway): make router-label JUDGE=<alias> [LOCAL=chat] [STRONG=chat-strong] [DEADLINE=<seconds>] [LIMIT=<n>]. Writes the ignored outcomes/ file; resumable
	@test -n "$(JUDGE)" || { echo "usage: make router-label JUDGE=<gateway alias that scores the answers>"; exit 2; }
	python -m scripts.model_eval route-label --judge $(JUDGE) $(if $(LOCAL),--local $(LOCAL)) $(if $(STRONG),--strong $(STRONG)) $(if $(STRONG_ID),--strong-id $(STRONG_ID)) $(if $(DEADLINE),--deadline $(DEADLINE)) $(if $(LIMIT),--limit $(LIMIT))

probe-tools:  ## Does the configured chat model emit tool calls the app can use? 25 labelled prompts against the app's real tools plus 4 open-ended ones that must not loop; about 2 minutes on a native Ollama (`ARGS=--no-loops` is the seconds-long tool half). Run it after ANY change to a chat alias's model or repeat_penalty: make probe-tools [ARGS="--alias chat-strong"] [ARGS="--model qwen2.5:7b --repeat-penalty 1.3 1.1"] (GRAPH_PATTERNS.md pattern 73)
	python -m scripts.toolcall_probe $(ARGS)

# --- Prompt checks and LLM-judged quality ----------------------------------------------------------------------------------------------------------

promptfoo:  ## Prompt-level regression checks for the domain system prompts against a real Ollama (needs `make up` or a native Ollama with CHAT_MODEL pulled)
	cd promptfoo && npm install --include=optional
	python -m promptfoo.dump_prompts
	cd promptfoo && for domain in support ops sales; do \
		npx promptfoo eval --config $$domain.yaml || exit 1; \
	done

deepeval:  ## LLM-judged RAG quality (tests/deepeval/test_rag_quality_deepeval.py) + a multi-turn conversation simulation (test_conversation_simulator_deepeval.py) + tool-call trajectory correctness (test_tool_correctness_deepeval.py) against the real graph — needs Docker + GOOGLE_API_KEY (RAG quality/tool correctness, tests/deepeval/conftest.py's deepeval_judge fixture) + GROQ_API_KEY (conversation simulation only — Gemini's response_schema can't grade its KnowledgeRetentionMetric, see deepeval_conversation_judge); read the printed reasons by hand, don't trust pass/fail alone (see those files' own disclosed judge-reliability findings, GRAPH_PATTERNS.md pattern 48). Same command CI's own `deepeval` job runs; every file's test cases are `flaky=True` there so a bad score can't fail the build.
	DEEPEVAL_TELEMETRY_OPT_OUT=1 pytest -m deepeval -q -s --reruns 2 --reruns-delay 10 --only-rerun "(ServerError|UNAVAILABLE|RESOURCE_EXHAUSTED|DEADLINE_EXCEEDED|RateLimitError|APITimeoutError|APIConnectionError|InternalServerError)" tests/deepeval

# --- Red team: adversarial prompts and raw-model jailbreak scans -----------------------------------------------------------------------------------

promptfoo-redteam:  ## Adversarial variants of the support/sales/ops prompts (prompt injection, policy violations, sandbox misuse) against the real local target, generated+graded by Gemini 3.1 Flash-Lite (needs GOOGLE_API_KEY, see .env.example) — see promptfoo/redteam.yaml's own comments for why redteam.provider is the one deliberate exception to this project's offline commitment
	cd promptfoo && npm install --include=optional
	python -m promptfoo.dump_prompts
	# `--max-concurrency 1 --delay 2100`: keeps every domain's redteam.provider
	# calls serial with >2s between them, safely under the free Gemini tier's
	# 30 req/min this was built against (see redteam.yaml's own RATE LIMIT
	# paragraph) — raise only alongside a paid tier/higher quota.
	# Every promptfoo command runs from inside promptfoo/, where package.json and node_modules live
	# (so the repo root stays Python-only); --config and --output are therefore relative to that directory.
	# `redteam run`'s generated+graded test suite is written to `-o`/
	# `--output`, which DEFAULTS to a HARDCODED `redteam.yaml` in the
	# --config file's own DIRECTORY — not to the --config file itself,
	# regardless of what it's named (confirmed directly in promptfoo's own
	# installed source, promptfoo/node_modules/promptfoo/dist/src/main.js:
	# `redteamPath = path.join(configDir, "redteam.yaml")`). Copying each
	# domain's config to its own scratch file first does NOT protect the
	# hand-authored sources on its own — every scratch file already lives in
	# THIS SAME `promptfoo/` directory, so all three would silently default
	# to overwriting `promptfoo/redteam.yaml` (support's own hand-authored
	# file) regardless of which domain actually ran. An explicit `-o`
	# pointed back at each stanza's own scratch file is what actually keeps
	# them isolated — verified the hard way: an early version of this target
	# without it clobbered the checked-in `redteam.yaml` from a run against
	# an unrelated scratch config. Three explicit stanzas, not a
	# `for domain in ...` loop like the `promptfoo` target above — support's
	# file is `redteam.yaml` (no domain prefix, predates the sales/ops
	# siblings), so the source filename isn't uniform across domains the way
	# `promptfoo/$$domain.yaml` already is.
	#
	# Leading `-` on each `redteam run` line: promptfoo's OWN exit code is
	# nonzero (a hardcoded 100 by default) the moment ANY test case fails —
	# confirmed directly in its installed source (`process.exitCode =
	# failedTestExitCode ?? 100`), verified the hard way when a real Gemini
	# run found a genuine finding in `sales-redteam` and `make` aborted
	# right there, never reaching `ops-redteam` at all. That's the opposite
	# of what a MANUAL, read-it-by-hand target wants — a finding here isn't
	# a bug in this target, it's the entire point of running it — so `-`
	# tells `make` to keep going to the next domain regardless.
	-cp promptfoo/redteam.yaml promptfoo/.support-redteam-run-scratch.yaml
	-cd promptfoo && PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config .support-redteam-run-scratch.yaml --output .support-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100
	-cp promptfoo/sales-redteam.yaml promptfoo/.sales-redteam-run-scratch.yaml
	-cd promptfoo && PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config .sales-redteam-run-scratch.yaml --output .sales-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100
	-cp promptfoo/ops-redteam.yaml promptfoo/.ops-redteam-run-scratch.yaml
	-cd promptfoo && PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config .ops-redteam-run-scratch.yaml --output .ops-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100

garak:  ## Fast, curated probe subset scanning the real model for known jailbreak/injection patterns — needs `make up`/a native Ollama AND a SEPARATE Python environment, never this repo's own .venv (installing garak here upgrades langgraph-checkpoint past what this app's own pin allows — see garak/requirements-garak.txt)
	python -m pip install -q -r garak/requirements-garak.txt
	python garak/run_ci_scan.py

garak-full:  ## The full, slow garak probe suite — deliberate, pre-release scanning, not a per-PR gate (same framing as `make eval`); same separate-environment requirement as `make garak`
	python -m pip install -q -r garak/requirements-garak.txt
	OPENAICOMPATIBLE_API_KEY="sk-not-checked-by-ollama" python -m garak --config garak/config.yaml --target_type openai.OpenAICompatible --target_name "$${GARAK_MODEL:-qwen2.5:3b}"
