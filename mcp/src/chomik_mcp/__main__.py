"""chomik-mcp: run over stdio (local MCP clients) or HTTP (self-hosted, e.g. on a NAS).

  chomik-mcp                       # stdio
  chomik-mcp --http                # http://0.0.0.0:8765/mcp, needs MCP_TOKEN
"""
from __future__ import annotations

import argparse
import hmac
import logging
import os
import sys

from chomik_core.envfile import load_env_file

from .server import build_server


class BearerAuth:
    """Reject HTTP requests without `Authorization: Bearer <MCP_TOKEN>`."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") != "/health":
            headers = dict(scope.get("headers") or [])
            given = headers.get(b"authorization", b"")
            if not hmac.compare_digest(given, b"Bearer " + self.token):
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": b"missing or wrong bearer token\n"})
                return
        if scope["type"] == "http" and scope.get("path") == "/health":
            await send({"type": "http.response.start", "status": 200, "headers": [(b"content-type", b"text/plain")]})
            await send({"type": "http.response.body", "body": b"ok\n"})
            return
        await self.app(scope, receive, send)


def http_app(server, token: str, allowed_hosts: list[str]):
    from mcp.server.transport_security import TransportSecuritySettings

    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed_hosts),
        allowed_hosts=allowed_hosts,
        allowed_origins=[f"http://{h}" for h in allowed_hosts] + [f"https://{h}" for h in allowed_hosts],
    )
    return BearerAuth(server.streamable_http_app(transport_security=security, host="0.0.0.0"), token)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chomik-mcp", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--http", action="store_true", help="serve MCP over HTTP instead of stdio")
    p.add_argument("--host", default=os.environ.get("MCP_HOST", "0.0.0.0"))
    p.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", "8765")))
    p.add_argument("--env", default=os.environ.get("CHOMIK_ENV_FILE", ".env"), help="env file with CHOMIK_<X>_* logins")
    a = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(message)s")
    load_env_file(a.env)
    os.environ["CHOMIK_ENV_FILE"] = a.env
    server = build_server()
    if not a.http:
        server.run("stdio")
        return 0
    token = os.environ.get("MCP_TOKEN", "")
    if len(token) < 16:
        print("MCP_TOKEN must be set (16+ characters) to serve over HTTP", file=sys.stderr)
        return 2
    hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
    import uvicorn

    uvicorn.run(http_app(server, token, hosts), host=a.host, port=a.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
