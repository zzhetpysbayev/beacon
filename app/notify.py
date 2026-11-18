"""Telegram notifications through the platform bot. Silently skipped when not configured."""
import html
import logging

import httpx

from .config import settings

log = logging.getLogger("monitor.notify")


def esc(value) -> str:
    return html.escape(str(value))


async def send(text: str, chat_ids: list[str]) -> None:
    if not settings.telegram_bot_token or not chat_ids:
        log.info("Telegram skipped (not configured or no verified contacts): %s", text[:80])
        return
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    async with httpx.AsyncClient(timeout=10) as client:
        for chat_id in chat_ids:
            try:
                resp = await client.post(
                    url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
                )
                resp.raise_for_status()
            except Exception:
                log.exception("Telegram send failed for chat %s", chat_id)
