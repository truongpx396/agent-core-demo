# Tasks: spec 016

- [X] T201 `docker/ml-service/ml_service/decide.py`: `POST /v1/decide` (validation, answer, 503); `Labeller.decide`/`labels_of`/`_indexes_for` (one model for all questions, one embedding pass); `task: "label"` removed from `/classify`
- [X] T202 `app/core/classification.py`: `Choice`, `choose`, the answer validated; `Labelled.confidence`; the 503 `model_unavailable` as `NO_ANSWER`
- [X] T203 `routing_policy.ROUTE_INSTRUCTIONS` / `LABEL_DESCRIPTIONS`; `model_route` asks `ROUTE_QUESTION`; `ROUTER_LABELSET` to `ROUTER_LABELS`
- [X] T204 Tests: the service (wire, rejections table, boundaries, failover, one model for several questions, no text in a log, metrics), the client (local and documentation-derived answers, every wrong shape), the router, the settings, the container test; mutation check (45/45)
- [X] T205 Docs: this spec and `contracts/decide.md`; README, pattern 68 and ENGINEERING_PATTERNS 76; the 015 contracts marked superseded
- [ ] T206 **HUMAN** Add a card to the Opper account (or use TypeSafe's own key), run the call in `contracts/decide.md` once, record the real response and add it to `tests/core/test_classification.py`
- [ ] T207 **HUMAN** Decide the privacy and spend questions in the contract's swap checklist before any hosted implementation serves a turn
- [ ] T208 **HUMAN** Delete `/classify task="route"` and the head code, or keep it for an accepted head (open since spec 015)
