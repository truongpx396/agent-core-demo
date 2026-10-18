# Contract: LiteLLM aliases (the "seams")

Both configs live in `deploy/litellm/`. Everything here is **configuration**; the app names only aliases (Constitution:
"LLM access"). Nothing in `app/` changes when an operator re-points an alias.

## 1. `chat-strong` — the escalation alias (FR-032)

Always present, so the proxy always starts and the alias always resolves; **inert** because the app switch
(`STRONG_CHAT_MODEL`) is empty by default.

Dev (`litellm-config.yaml`):

```yaml
  - model_name: chat-strong
    litellm_params:
      model: ollama_chat/qwen2.5:7b          # a bigger LOCAL model: no third party, no key
      api_base: http://host.docker.internal:11434
      num_ctx: 32000
      num_predict: 2048
      repeat_penalty: 1.3
```

Prod (`litellm-config.prod.yaml`) — compose passes `LLM_STRONG_*` defaulting to the `chat` values, so an unset variable cannot stop
the proxy and the alias is effectively `chat`:

```yaml
  - model_name: chat-strong
    litellm_params:
      model: os.environ/LLM_STRONG_CHAT_MODEL
      api_base: os.environ/LLM_STRONG_API_BASE
      api_key: os.environ/LLM_STRONG_API_KEY
    # model_info: { input_cost_per_token: …, output_cost_per_token: … }   # required if UNPRICED_MODEL_POLICY=block
```

**To attach any OpenAI-compatible provider** the operator edits this one entry (or the three env vars in prod) — no code,
no image rebuild, no app restart beyond the proxy's. Then sets `STRONG_CHAT_MODEL=chat-strong` and `ROUTER_ENABLED=true`.

**What a target must provide** (documented in `README.md`; the live test runs it against the dev stand-in):

1. OpenAI chat-completions, including `stream: true` with `stream_options.include_usage` (the app needs usage in the final chunk).
2. Native tool calls in the OpenAI shape (the agent binds tools; a model without them fails every tool turn).
3. A price LiteLLM can find, or `model_info` as above, when `UNPRICED_MODEL_POLICY=block` (else routing records `strong_unpriced`
   and stays local).
4. Context window ≥ the local model's `num_ctx` (history ceilings are sized against 32 000, `graph/state.py`).

**No gateway-level fallback is configured for `chat-strong`.** The degrade-to-local is done by the app so that the ledger prices
the model that served (`research.md` C3). The existing `chat → chat-backup` fallback is unchanged.

## 2. `embed` — pointing at ml-service (FR-023)

Dev / prod, an alternative to the Ollama (dev) or hosted (prod) entry — **not the default**:

```yaml
  - model_name: embed
    litellm_params:
      model: openai/bge-small-en-v1.5        # the registry name; the `openai/` prefix selects LiteLLM's OpenAI-compatible provider
      api_base: http://ml-service:80/v1
      api_key: unused                        # LiteLLM requires a value; ml-service ignores it
```

Procedure to adopt (documented, tested): (1) `make model-eval` says the candidate wins; (2) set the entry above;
(3) set `EMBED_QUERY_PREFIX` / `EMBED_DOCUMENT_PREFIX` to what the model needs (`GET /v1/models` → `needs_prefix`);
(4) restart the proxy; (5) the app now reports an identity **mismatch** for every existing collection and the answer cache
(this is the guard working); (6) run `make reembed COLLECTION_NAME=…` for each; (7) bake, then delete the old physical collections.

**To verify with a real proxy (manual; `quickstart.md` step 7):** that LiteLLM forwards `input` as a list, accepts
`encoding_format=base64` from the app's SDK, and returns usage; with `drop_params: true` a dropped parameter must not change the
vector. The hermetic SDK test covers the endpoint; only a real proxy covers the hop in between.
