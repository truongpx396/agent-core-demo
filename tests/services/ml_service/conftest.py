"""Shared fakes for the ML service's hermetic tests: a real `tokenizers.Tokenizer` built from a tiny vocabulary (no download) and a fake
ONNX session. Nothing here loads a model or opens a socket. It also holds every response any test in this directory gets from the app to
the committed OpenAPI document (`every_response_conforms_to_the_openapi_document`)."""
import functools
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from tokenizers import Tokenizer, models, pre_tokenizers

from tests.openapi_conformance import load, validator

pytest.importorskip("prometheus_client", reason="ml_service.metrics needs prometheus-client (requirements-dev.txt / the CI test job)")

# FastAPI serves these itself and never lists them in the document it serves.
FRAMEWORK_ROUTES = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
DOCUMENT = load("services/ml-service/openapi.json")


@functools.cache  # built once per (route, status): a per-request rebuild slowed the supervisor tests' timing into flaking
def _validator(path: str, method: str, status: int) -> Draft202012Validator:
    return validator(DOCUMENT, DOCUMENT["paths"][path][method]["responses"][str(status)]["content"]["application/json"]["schema"])


def conforms(method: str, url: Any, response: httpx.Response) -> None:
    """The response is one the committed document describes: the route and the status are listed, the headers it names are sent, and the
    body matches its schema."""
    path = urlsplit(str(url)).path
    if path in FRAMEWORK_ROUTES:
        return
    operation = DOCUMENT["paths"].get(path, {}).get(method.lower())
    if operation is None:  # a route the document does not list may only be answered as one the app does not have (test_classify_is_gone...)
        assert response.status_code in (404, 405), f"{method} {path} is not in services/ml-service/openapi.json and answered {response.status_code}"
        return
    declared = operation["responses"].get(str(response.status_code))
    assert declared is not None, f"{method} {path} answered {response.status_code}, which the document does not list: {sorted(operation['responses'])}"
    for header in declared.get("headers", {}):
        assert header.lower() in response.headers, f"{method} {path} {response.status_code} is documented with {header}, and did not send it"
    content = declared.get("content")
    if not content:
        return
    media = response.headers["content-type"].split(";")[0]
    assert media in content, f"{method} {path} {response.status_code} sent {media}; the document lists {sorted(content)}"
    if media == "application/json":
        _validator(path, method.lower(), response.status_code).validate(response.json())


@pytest.fixture(autouse=True)
def every_response_conforms_to_the_openapi_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every response any test in this directory gets from the app is held to the document, success and error paths alike: the document
    cannot describe something the service does not send, and the service cannot send what the document does not describe."""
    original = TestClient.request

    def request(self: TestClient, method: str, url: Any, *args: Any, **kwargs: Any) -> httpx.Response:
        response = original(self, method, url, *args, **kwargs)
        conforms(method, url, response)
        return response

    monkeypatch.setattr(TestClient, "request", request)

VOCAB = {"[PAD]": 0, "[UNK]": 1, "hello": 2, "world": 3, "foo": 4, "bar": 5, "baz": 6}


def make_tokenizer(vocab: dict[str, int] | None = None) -> Tokenizer:
    tok = Tokenizer(models.WordLevel(vocab or VOCAB, unk_token="[UNK]"))
    tok.pre_tokenizer = pre_tokenizers.Whitespace()
    return tok


class FakeSession:
    """Looks like an onnxruntime session. Its hidden state for a token with id `i` is `[i, 2i, 3i, 4i]`, so every pooling mode has a
    value that can be worked out by hand; `logits_fn`, if given, replaces the output (for the guard)."""

    def __init__(self, inputs=("input_ids", "attention_mask"), outputs=("last_hidden_state",), logits_fn=None):
        self.input_names = list(inputs)
        self.output_names = list(outputs)
        self.logits_fn = logits_fn
        self.calls: list[dict[str, np.ndarray]] = []

    def get_inputs(self):
        return [SimpleNamespace(name=n) for n in self.input_names]

    def get_outputs(self):
        return [SimpleNamespace(name=n) for n in self.output_names]

    def run(self, _output_names, feed):
        self.calls.append(feed)
        ids = feed["input_ids"].astype(np.float32)
        if self.logits_fn is not None:
            return [self.logits_fn(feed["input_ids"])]
        hidden = np.stack([ids, 2 * ids, 3 * ids, 4 * ids], axis=-1)
        return [hidden for _ in self.output_names]
