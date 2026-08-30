"""Team-channel notifications, shared by every example domain that needs to
tell a human something happened: the ops bot's own `post_to_team_channel`
tool (app/domains/ops/tools.py), and the support/sales domains' internal
escalation/handoff side effects (`escalate_to_human`, `handoff_to_human`) —
one implementation, several call sites, rather than three copies of "write
a line somewhere a human will see it."

Default sink is local and fully offline, matching this app's posture
everywhere else: append to `var/team_channel.log` (created on first use)
and a structured log line. If `SLACK_WEBHOOK_URL` is set, this ALSO
best-effort POSTs there — additive, never load-bearing, the same
relationship this app already has with Langfuse/its observability stack
(GRAPH_PATTERNS.md's "additive, not load-bearing" note in the README):
a demo team-channel notifier degrading to a log line on a webhook failure
is fine, unlike e.g. TELEGRAM_BOT_TOKEN, which fails loud on missing
config because that channel has no reasonable degraded mode at all.
"""
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.core.config import SLACK_WEBHOOK_URL

logger = logging.getLogger(__name__)

_LOG_PATH = Path("var/team_channel.log")
_POST_TIMEOUT_SECONDS = 5


def post_to_team_channel(channel: str, message: str) -> str:
    """Best-effort notify — never raises. `channel` is a free-text label
    (e.g. "support-escalations", "sales-handoffs", "ops-digest"), not a
    real Slack channel id: this demo has no workspace to address, so it's
    folded into the logged/written line instead of a routing parameter a
    real integration would use.
    """
    timestamp = datetime.now(UTC).isoformat()
    line = f"[{timestamp}] [{channel}] {message}"

    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as exc:
        logger.warning(
            "team_channel_local_sink_failed",
            extra={"channel": channel, "error_class": type(exc).__name__},
        )

    # "message" is a reserved stdlib LogRecord attribute (structlog's
    # configured logging, app/core/logging_config.py, raises KeyError if an
    # `extra` key collides with it — verified empirically running this
    # against the real stack before catching it here) — "text" instead.
    logger.info("team_channel_message", extra={"channel": channel, "text": message})

    if SLACK_WEBHOOK_URL:
        try:
            httpx.post(
                SLACK_WEBHOOK_URL,
                json={"text": f"*[{channel}]* {message}"},
                timeout=_POST_TIMEOUT_SECONDS,
            ).raise_for_status()
        except Exception as exc:  # noqa: BLE001 - additive sink, never blocks the caller
            logger.warning(
                "team_channel_slack_post_failed",
                extra={"channel": channel, "error_class": type(exc).__name__},
            )

    return f"Posted to {channel!r}."
