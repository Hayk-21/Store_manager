"""Production entrypoint.

The port is read here in Python rather than interpolated into a shell command, so
binding cannot depend on how Railway's start command is expanded.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(
        "app.main:app",
        # "::" is dual-stack. Railway's private network (web.railway.internal, which
        # the bot service talks to) is IPv6-only, so binding 0.0.0.0 would make the
        # web service unreachable from the bot while still working publicly.
        host="::",
        port=port,
        # One worker: the pool is small because Neon's connection budget is tight.
        workers=1,
        # Railway terminates TLS in front of us; without these the app thinks every
        # request is plain http and refuses to set Secure cookies.
        proxy_headers=True,
        forwarded_allow_ips="*",
        access_log=False,
    )


if __name__ == "__main__":
    main()
