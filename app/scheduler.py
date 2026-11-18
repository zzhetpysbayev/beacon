"""Background loop: runs due checks, updates status, sends alerts, prunes history."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from . import db
from .checker import fetch_cert_expiry, perform_check
from .config import settings
from .notify import esc, send

log = logging.getLogger("monitor.scheduler")

SSL_RECHECK = timedelta(hours=6)
_running: set[int] = set()
_tasks: set[asyncio.Task] = set()


async def _notify(monitor_id: int, text: str) -> None:
    await send(text, db.verified_chat_ids_for_monitor(monitor_id))


async def _check_ssl(m: dict, now: datetime, updates: dict) -> None:
    parts = urlsplit(m["url"])
    if parts.scheme != "https" or not m["verify_ssl"]:
        return
    last = db.parse_ts(m["ssl_checked_at"])
    if last and now - last < SSL_RECHECK:
        return

    expires = await asyncio.to_thread(fetch_cert_expiry, parts.hostname, parts.port or 443)
    updates["ssl_checked_at"] = db.ts(now)
    updates["ssl_expires_at"] = db.ts(expires)
    if expires:
        days_left = (expires - now).days
        if days_left <= settings.ssl_warn_days:
            # Repeats at each SSL recheck (every 6 hours) until renewed.
            await _notify(
                m["id"],
                f"Warning: SSL certificate <b>{esc(m['name'])}</b> expires in "
                f"{days_left} day(s) ({expires.date().isoformat()})",
            )


async def run_monitor(m: dict) -> None:
    now = db.utcnow()
    result = await perform_check(m)
    db.insert_check(m["id"], db.ts(now), result.ok, result.status_code,
                    result.response_ms, result.error)

    old_status = m["status"]
    failures = 0 if result.ok else m["consecutive_failures"] + 1
    if result.ok:
        new_status = "up"
    elif failures >= settings.fail_threshold:
        new_status = "down"
    else:
        new_status = old_status  # not enough failures yet to change state

    updates = {
        "last_checked_at": db.ts(now),
        "last_status_code": result.status_code,
        "last_response_ms": result.response_ms,
        "last_error": result.error,
        "consecutive_failures": failures,
        "status": new_status,
    }
    if new_status != old_status:
        updates["last_status_change_at"] = db.ts(now)

    try:
        await _check_ssl(m, now, updates)
    except Exception:
        log.exception("SSL check failed for monitor %s", m["id"])

    last_alert = db.parse_ts(m["last_alert_at"])
    renotify_due = last_alert is None or now - last_alert >= timedelta(minutes=settings.renotify_minutes)

    if new_status == "down" and (old_status != "down" or renotify_due):
        updates["last_alert_at"] = db.ts(now)
        await _notify(
            m["id"],
            f"Down: <b>{esc(m['name'])}</b>\n{esc(m['url'])}\n"
            f"Reason: {esc(result.error)}",
        )
    elif new_status == "up" and old_status == "down":
        updates["last_alert_at"] = None
        await _notify(m["id"], f"Back up: <b>{esc(m['name'])}</b>\n{esc(m['url'])}")

    db.update_state(m["id"], updates)


async def _safe_run(m: dict) -> None:
    try:
        await run_monitor(m)
    except Exception:
        log.exception("Check failed for monitor %s", m["id"])
    finally:
        _running.discard(m["id"])


def _due(monitors: list[dict], now: datetime):
    for m in monitors:
        last = db.parse_ts(m["last_checked_at"])
        if last is None or now - last >= timedelta(seconds=m["interval_s"]):
            yield m


async def scheduler_loop(stop: asyncio.Event) -> None:
    last_prune = datetime.min.replace(tzinfo=timezone.utc)
    while not stop.is_set():
        now = db.utcnow()
        try:
            for m in _due(db.enabled_monitors(), now):
                if m["id"] in _running:
                    continue
                _running.add(m["id"])
                task = asyncio.create_task(_safe_run(m))
                _tasks.add(task)
                task.add_done_callback(_tasks.discard)
            if now - last_prune > timedelta(hours=1):
                db.prune_checks(settings.history_days)
                last_prune = now
        except Exception:
            log.exception("Scheduler tick failed")

        try:
            await asyncio.wait_for(stop.wait(), timeout=1)
        except asyncio.TimeoutError:
            pass

    for task in list(_tasks):
        task.cancel()
    await asyncio.gather(*list(_tasks), return_exceptions=True)
