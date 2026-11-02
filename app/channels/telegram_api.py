"""The Telegram Bot API calls the channel makes (specs/021), in one place. Names and limits are from https://core.telegram.org/bots/api, read on
2026-11-02: `sendMessage` text is 1-4096 characters, `callback_data` 1-64 bytes, `answerCallbackQuery` text 0-200, `secret_token` 1-256 of
`A-Z a-z 0-9 _ -`, and `getUpdates` does not work while a webhook is set. Two things the page did NOT settle, so nothing here relies on them: what
`editMessageText` answers when the text is unchanged, and any per-chat rate limit on edits. A refused edit is therefore ignored, and the final text
is always sent or edited in last.

Everything a person sees is best-effort: a failed send, edit or answer is logged and counted by the caller's outcome, never raised, so one bad chat
cannot stop the loop that serves the others. Starting up is the opposite: `set_webhook`, `delete_webhook` and `get_updates` raise.
"""
import logging

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.telegram.org"
MESSAGE_CHAR_LIMIT = 4000  # under Telegram's 4096, with headroom
CALLBACK_ANSWER_LIMIT = 200
NO_KEYBOARD: dict[str, list] = {"inline_keyboard": []}


def split_text(text: str, limit: int = MESSAGE_CHAR_LIMIT) -> list[str]:
    """Splits rather than truncates: a long, grounded answer with citations is a normal shape here, not an edge case."""
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [""]


class BotApi:
    def __init__(self, http: httpx.AsyncClient, token: str) -> None:
        self._http = http
        self._token = token

    def _url(self, method: str) -> str:
        return f"{API_BASE}/bot{self._token}/{method}"

    async def _best_effort(self, method: str, payload: dict, **log: object) -> dict | None:
        """The `result` of a call that succeeded, or None. The token is in the URL, so an exception is logged by class and never by text."""
        try:
            reply = await self._http.post(self._url(method), json=payload)
            reply.raise_for_status()
            body = reply.json()
        except Exception as exc:  # noqa: BLE001 - one failed call must not kill the loop that serves the other chats
            logger.warning("telegram_call_failed", extra={"method": method, "error_class": type(exc).__name__, **log})
            return None
        result = body.get("result") if isinstance(body, dict) else None
        return result if isinstance(result, dict) else {}

    async def send_message(self, chat_id: int, text: str, *, reply_markup: dict | None = None) -> int | None:
        """One message of at most the limit; returns its id (None when it did not go, or Telegram did not say)."""
        payload: dict = {"chat_id": chat_id, "text": text[:MESSAGE_CHAR_LIMIT]}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        result = await self._best_effort("sendMessage", payload, chat_id=chat_id)
        message_id = (result or {}).get("message_id")
        return message_id if isinstance(message_id, int) else None

    async def send_text(self, chat_id: int, text: str) -> int | None:
        """A text of any length, in order; returns the id of its FIRST message."""
        first: int | None = None
        for index, chunk in enumerate(split_text(text)):
            if not chunk:
                continue  # sendMessage refuses an empty text, and an empty reply read as the bot ignoring the person
            message_id = await self.send_message(chat_id, chunk)
            if index == 0:
                first = message_id
        return first

    async def edit_message(self, chat_id: int, message_id: int, text: str, *, reply_markup: dict | None = None) -> bool:
        """Replace a message's text. The keyboard is always stated: pass `NO_KEYBOARD` to take it away, rather than rely on what an omitted one does."""
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text[:MESSAGE_CHAR_LIMIT], "reply_markup": reply_markup or NO_KEYBOARD}
        return await self._best_effort("editMessageText", payload, chat_id=chat_id) is not None

    async def answer_callback(self, callback_id: str, text: str = "", *, alert: bool = False) -> None:
        payload: dict = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text[:CALLBACK_ANSWER_LIMIT]
            payload["show_alert"] = alert
        await self._best_effort("answerCallbackQuery", payload)

    async def typing(self, chat_id: int) -> None:
        await self._best_effort("sendChatAction", {"chat_id": chat_id, "action": "typing"}, chat_id=chat_id)  # cosmetic

    async def get_updates(self, offset: int, timeout: int) -> list[dict]:
        reply = await self._http.post(
            self._url("getUpdates"), json={"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        )
        reply.raise_for_status()
        result = reply.json().get("result", [])
        return [u for u in result if isinstance(u, dict) and isinstance(u.get("update_id"), int)]

    async def set_webhook(self, url: str, secret: str) -> None:
        reply = await self._http.post(
            self._url("setWebhook"), json={"url": url, "secret_token": secret, "allowed_updates": ["message", "callback_query"]}
        )
        reply.raise_for_status()
        if not reply.json().get("ok"):
            raise RuntimeError("Telegram refused setWebhook")

    async def delete_webhook(self) -> None:
        """Poll mode starts by removing a webhook left by an earlier run: getUpdates refuses to work while one is set. Pending updates are kept."""
        reply = await self._http.post(self._url("deleteWebhook"), json={"drop_pending_updates": False})
        reply.raise_for_status()
