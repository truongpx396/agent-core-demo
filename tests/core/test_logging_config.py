"""Tests for app/core/logging_config.py's structured logging setup."""
import io
import json
import logging

import pytest

from app.core.logging_config import (
    JSONFormatter,
    _RequestIdFilter,
    bind_request_id,
    request_id_var,
)


@pytest.fixture
def captured_logger():
    """A throwaway logger + handler pair writing JSON to an in-memory
    buffer — isolated from the real root logger (never calls
    configure_logging/basicConfig), so this can't interfere with pytest's
    own logging capture or leak a handler into other tests."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JSONFormatter())
    handler.addFilter(_RequestIdFilter())
    logger = logging.getLogger("test.logging_config")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    logger.propagate = False
    yield logger, stream
    logger.removeHandler(handler)


def _last_line(stream: io.StringIO) -> dict:
    lines = stream.getvalue().strip().splitlines()
    return json.loads(lines[-1])


class TestJSONFormatter:
    def test_basic_fields(self, captured_logger):
        logger, stream = captured_logger
        logger.info("something_happened")
        payload = _last_line(stream)
        assert payload["level"] == "INFO"
        assert payload["logger"] == "test.logging_config"
        assert payload["message"] == "something_happened"
        assert "timestamp" in payload

    def test_extra_fields_surface_as_real_json_keys(self, captured_logger):
        """The whole point (see this module's docstring): a plain
        `logging.basicConfig` formatter silently drops `extra=` fields —
        this one must not."""
        logger, stream = captured_logger
        logger.warning("tool_failed", extra={"tool": "search_docs", "run_id": "abc123"})
        payload = _last_line(stream)
        assert payload["tool"] == "search_docs"
        assert payload["run_id"] == "abc123"

    def test_non_json_native_extra_value_is_stringified_not_fatal(self, captured_logger):
        logger, stream = captured_logger
        logger.info("weird_value", extra={"code": ValueError("boom")})
        payload = _last_line(stream)
        assert "boom" in payload["code"]

    def test_exception_info_included(self, captured_logger):
        logger, stream = captured_logger
        try:
            raise RuntimeError("kaboom")
        except RuntimeError:
            logger.exception("it_broke")
        payload = _last_line(stream)
        assert "kaboom" in payload["exc_info"]


class TestBindRequestId:
    def test_injects_request_id_into_every_line_in_scope(self, captured_logger):
        logger, stream = captured_logger
        with bind_request_id("req-1"):
            logger.info("inside")
        payload = _last_line(stream)
        assert payload["request_id"] == "req-1"

    def test_no_request_id_field_outside_any_bound_scope(self, captured_logger):
        logger, stream = captured_logger
        logger.info("outside")
        payload = _last_line(stream)
        assert "request_id" not in payload

    def test_resets_after_the_block_exits(self, captured_logger):
        logger, stream = captured_logger
        with bind_request_id("req-1"):
            pass
        logger.info("after")
        payload = _last_line(stream)
        assert "request_id" not in payload

    def test_an_explicit_extra_request_id_is_not_overwritten(self, captured_logger):
        """An explicit extra={"request_id": ...} at a specific call site
        (e.g. app/turns/agent_worker.py's own failure-path log) wins over the
        ambient contextvar — same value in practice today, but explicit
        should still take precedence over implicit."""
        logger, stream = captured_logger
        with bind_request_id("ambient"):
            logger.warning("explicit", extra={"request_id": "explicit-value"})
        payload = _last_line(stream)
        assert payload["request_id"] == "explicit-value"

    def test_nested_binding_restores_the_outer_value_on_exit(self, captured_logger):
        logger, stream = captured_logger
        with bind_request_id("outer"):
            with bind_request_id("inner"):
                logger.info("nested")
                assert _last_line(stream)["request_id"] == "inner"
            logger.info("back_to_outer")
            assert _last_line(stream)["request_id"] == "outer"
        assert request_id_var.get() is None

    def test_reset_happens_even_if_the_block_raises(self, captured_logger):
        with pytest.raises(ValueError):
            with bind_request_id("req-1"):
                raise ValueError("boom")
        assert request_id_var.get() is None
