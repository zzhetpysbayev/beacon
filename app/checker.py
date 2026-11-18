"""Performs a single HTTP check and reads TLS certificate expiry."""
import socket
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx


@dataclass
class CheckResult:
    ok: bool
    status_code: int | None
    response_ms: float | None
    error: str | None


async def perform_check(monitor: dict) -> CheckResult:
    started = time.perf_counter()
    try:
        async with httpx.AsyncClient(
            verify=bool(monitor["verify_ssl"]),
            follow_redirects=True,
            timeout=monitor["timeout_s"],
        ) as client:
            resp = await client.request(monitor["method"], monitor["url"])
        elapsed = (time.perf_counter() - started) * 1000

        if resp.status_code != monitor["expected_status"]:
            return CheckResult(
                False, resp.status_code, elapsed,
                f"HTTP {resp.status_code}, expected {monitor['expected_status']}",
            )
        if monitor["keyword"] and monitor["keyword"] not in resp.text:
            return CheckResult(
                False, resp.status_code, elapsed,
                f"Keyword not found: {monitor['keyword']}",
            )
        return CheckResult(True, resp.status_code, elapsed, None)
    except Exception as exc:  # timeouts, DNS, connection and TLS errors
        return CheckResult(False, None, None, f"{type(exc).__name__}: {exc}")


def fetch_cert_expiry(host: str, port: int = 443, timeout: float = 10) -> datetime | None:
    """Return the certificate's notAfter as an aware datetime, or None on failure."""
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
        return datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), tz=timezone.utc)
    except Exception:
        return None
