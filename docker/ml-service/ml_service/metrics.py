"""Prometheus metrics for the ML service (T013). The service had none before; routing and fallback state is only visible from inside.

A private `CollectorRegistry` per `Metrics`, not the process-global default, so two apps in one test process do not collide on
duplicate names. Names are plain string literals so `tests/core/test_alert_rules.py` can check that every `ml_*` metric an alert
names is defined here (an alert on a metric nothing records is silent forever).
"""
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

from ml_service.registry import State

LATENCY_BUCKETS = (0.002, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0)
BATCH_BUCKETS = (1, 2, 4, 8, 16, 32, 64, 128, 256)


class Metrics:
    def __init__(self) -> None:
        self.registry = CollectorRegistry()
        r = self.registry
        self.model_state = Gauge("ml_model_state", "1 when the named model is ready, else 0.", ["model"], registry=r)
        self.classifier_serving = Gauge(
            "ml_classifier_serving", "1 for the classifier model currently answering (served_by primary|fallback), else 0.", ["served_by"], registry=r
        )
        self.embed_requests = Counter("ml_embed_requests_total", "Embedding requests accepted.", ["model"], registry=r)
        self.embed_items = Counter("ml_embed_items_total", "Texts embedded.", ["model"], registry=r)
        self.embed_errors = Counter("ml_embed_errors_total", "Embedding requests that failed.", ["model", "code"], registry=r)
        self.embed_latency = Histogram(
            "ml_embed_latency_seconds", "Wall time of an embedding request.", ["model"], buckets=LATENCY_BUCKETS, registry=r
        )
        self.embed_batch = Histogram("ml_embed_batch_size", "Texts per embedding request.", ["model"], buckets=BATCH_BUCKETS, registry=r)
        self.classify_requests = Counter("ml_classify_requests_total", "Classification requests, by who answered.", ["served_by"], registry=r)
        self.label_requests = Counter(
            "ml_label_requests_total", "Decisions asked of a label set (POST /v1/decide, one per question), by set and who answered (served_by none = a 503). The set is a name the service already knows.",
            ["labelset", "served_by"], registry=r,
        )

    def set_model_state(self, name: str, state: State) -> None:
        self.model_state.labels(model=name).set(1 if state is State.READY else 0)

    def render(self) -> tuple[bytes, str]:
        return generate_latest(self.registry), CONTENT_TYPE_LATEST
