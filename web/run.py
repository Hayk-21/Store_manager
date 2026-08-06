"""Production entrypoint.

The port is read here in Python rather than interpolated into a shell command, so
binding cannot depend on how Railway's start command is expanded.

Binding defaults to ``0.0.0.0``. That is what Railway's healthcheck can reach; a
service bound only to ``::`` gets marked unhealthy and the deploy fails even
though the process is running perfectly well. If you want the bot to talk to this
service over Railway's private network -- which is IPv6-only -- set
``BIND_HOST=::`` and check the healthcheck still passes. The simpler and more
reliable arrangement is to leave this alone and point the bot at the public URL.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from app.logging_conf import setup_logging

log = logging.getLogger("storemanager.run")


def main() -> None:
    setup_logging()

    raw = (os.getenv("PORT") or "").strip()
    try:
        port = int(raw) if raw else 8080
    except ValueError:
        log.warning("PORT=%r is not a number; falling back to 8080", raw)
        port = 8080

    host = (os.getenv("BIND_HOST") or "").strip() or "0.0.0.0"  # noqa: S104

    log.info("binding %s:%d", host, port)
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        # One worker: the pool is small because Neon's connection budget is tight.
        workers=1,
        # Railway terminates TLS in front of us; without these the app thinks
        # every request is plain http and refuses to set Secure cookies.
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
    )


if __name__ == "__main__":
    main()
