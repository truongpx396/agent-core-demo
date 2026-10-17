# Contract: new application settings

Each goes in `app/core/config.py` `Settings` (module-level constant beside the others) **and** `.env.example`
(Constitution: Configuration). Defaults preserve today's behavior exactly (SC-005, SC-008).

| Setting | Type / default | Meaning | Spec |
|---|---|---|---|
| `EMBED_QUERY_PREFIX` | str `""` | text prepended to a query before embedding | FR-016 |
| `EMBED_DOCUMENT_PREFIX` | str `""` | text prepended to a document/passage/note before embedding | FR-016 |
| `EMBED_MODEL_REVISION` | str `""` | operator label included in the embedding identity; bump when re-pulling a model in place | FR-018 |
| `EMBED_IDENTITY_CACHE_SECONDS` | float `10` | how long an alias→collection identity check is cached | FR-019 |
| `STRONG_CHAT_MODEL` | str `""` | the escalation alias name (`chat-strong`); empty = no escalation exists | FR-032, FR-033 |
| `ROUTER_ENABLED` | bool `false` | master switch for asking the classifier | FR-033 |
| `ROUTER_THRESHOLD` | float `0.5` | escalate when the head's score ≥ this; set from the harness's curve | FR-033, FR-038 |
| `ROUTER_TIMEOUT_SECONDS` | float `0.5` | deadline for the `/classify` call; a miss = "no advice" | FR-031 |

**Validation (fail fast at import, Principle V).** `ROUTER_ENABLED=true` with an empty `STRONG_CHAT_MODEL` is a configuration error
(refuse to start with a message naming both), not a silent no-op. `ROUTER_THRESHOLD` outside `[0, 1]` is an error.
`STRONG_CHAT_MODEL == CHAT_MODEL` is an error (a router that can only pick the same model is a misconfiguration).

**Prod.** `deploy/env/prod.env.example` gains `LLM_STRONG_CHAT_MODEL`, `LLM_STRONG_API_BASE`, `LLM_STRONG_API_KEY` with the comment
that leaving them unset makes the alias equal `chat`; compose passes them with `${VAR:-${LLM_CHAT_MODEL}}` defaults.

**Not settings.** Per-tenant routing control (a deliberate gap); the classifier's primary/fallback names (they are the registry,
`docker/ml-service/models.toml`); the head threshold on the service side (it is advisory metadata).
