"""Team-channel notifications shared by every example domain (ops's
`post_to_team_channel` tool, support/sales's escalation/handoff side
effects) — one implementation instead of three copies.

Default sink is local/offline: appends to `var/team_channel.log` plus a
structured log line. If `SLACK_WEBHOOK_URL` is set, also best-effort POSTs
there — additive, never load-bearing (unlike e.g. TELEGRAM_BOT_TOKEN, which
fails loud on missing config since that channel has no degraded mode).
"""
import logging
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.core.config import SLACK_WEBHOOK_URL

logger = logging.getLogger(__name__)

_LOG_PATH = Path("var/team_channel.log")
_POST_TIMEOUT_SECONDS = 5


async def post_to_team_channel(channel: str, message: str) -> str:
    """Best-effort notify — never raises. `channel` is a free-text label
    (e.g. "support-escalations"), not a real Slack channel id — folded into
    the logged/written line rather than used as a routing parameter.

    The local file append is a few sync bytes, cheap enough to stay inline;
    only the Slack webhook POST is actually awaited.
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

    # "message" is a reserved stdlib LogRecord attribute (the configured
    # logging raises KeyError on an `extra` key collision) — "text" instead.
    logger.info("team_channel_message", extra={"channel": channel, "text": message})

    if SLACK_WEBHOOK_URL:
        try:
            async with httpx.AsyncClient(timeout=_POST_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    SLACK_WEBHOOK_URL, json={"text": f"*[{channel}]* {message}"}
                )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - additive sink, never blocks the caller
            logger.warning(
                "team_channel_slack_post_failed",
                extra={"channel": channel, "error_class": type(exc).__name__},
            )

    return f"Posted to {channel!r}."
