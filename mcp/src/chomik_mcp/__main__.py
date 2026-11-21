"""chomik-mcp: run over stdio (local MCP clients) or HTTP (self-hosted, e.g. on a NAS).

  chomik-mcp                       # stdio
  chomik-mcp --http                # http://0.0.0.0:8765: settings panel at /, MCP at /mcp
"""
from __future__ import annotations

import argparse
import hmac
import logging
import os
import sys
from typing import Callable

from chomik_core.envfile import load_env_file

from .config import Config
from .server import Tools, build_server
from .settings import Settings


class BearerAuth:
    """Require `Authorization: Bearer <token>` on /mcp. The panel has its own login."""

    def __init__(self, app, token: str | Callable[[], str]):
        self.app = app
        self.token = token if callable(token) else (lambda: token)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/health":
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok\n"})
            return
        if scope["type"] == "http" and scope.get("path", "").startswith("/mcp"):
            headers = dict(scope.get("headers") or [])
            given = headers.get(b"authorization", b"")
            if not hmac.compare_digest(given, b"Bearer " + self.token().encode()):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"missing or wrong bearer token\n"})
                return
        await self.app(scope, receive, send)


def http_app(server, token: str | Callable[[], str], allowed_hosts: list[str], extra_routes=()):
    from mcp.server.transport_security import TransportSecuritySettings

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed_hosts),
        allowed_hosts=allowed_hosts,
        allowed_origins=[f"http://{h}" for h in allowed_hosts] + [f"https://{h}" for h in allowed_hosts],
    )
    app = server.streamable_http_app(transport_security=security, host="0.0.0.0")
    app.router.routes.extend(extra_routes)
    return BearerAuth(app, token)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chomik-mcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--http", action="store_true", help="serve MCP over HTTP (with the settings panel) instead of stdio")
    p.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8765")))
    p.add_argument("--env", default=os.environ.get("CHOMIK_ENV_FILE", ".env"), help="env file with CHOMIK_<X>_* logins")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    load_env_file(a.env)
    os.environ["CHOMIK_ENV_FILE"] = a.env
    config = Config()

    if not a.http:
        if os.path.exists(config.settings_file):
            Settings(config.settings_file).apply_to_env()
        build_server(Tools(config)).run("stdio")
        return 0

    from .panel import Panel

    settings = Settings(config.settings_file)
    settings.apply_to_env()
    tools = Tools(config)
    panel = Panel(settings, config.passwords_file, on_change=tools.reset_clients)
    if not os.environ.get("PANEL_PASSWORD"):
        logging.warning("PANEL_PASSWORD is not set: the settings panel stays locked")
    hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    import uvicorn

    routes = list(panel.routes())
    if os.environ.get("ARR_ENABLED", "1").lower() not in ("0", "false", "no"):
        from .arr import ArrBridge

        routes += ArrBridge(tools, settings.data["secret"], settings.arr_api_key).routes()
    app = http_app(build_server(tools), settings.token, hosts, routes)
    uvicorn.run(app, host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
