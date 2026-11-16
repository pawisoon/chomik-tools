"""Fake HTTP session + fake upload server so tests never touch chomikuj.pl."""
from __future__ import annotations

import pathlib
import socket
import threading

import pytest

FIXTURES = pathlib.Path(__file__).parent / "fixtures"

ENV_HEAD = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
)
ENV_TAIL = "</s:Body></s:Envelope>"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def auth_ok(token="TOK", hamster="42") -> str:
    return (
        ENV_HEAD + '<AuthResponse xmlns="http://chomikuj.pl/"><AuthResult xmlns:a="http://chomikuj.pl" '
        'xmlns:i="http://www.w3.org/2001/XMLSchema-instance">'
        f"<a:status>Ok</a:status><a:errorMessage i:nil=\"true\"/><a:token>{token}</a:token>"
        f"<a:hamsterId>{hamster}</a:hamsterId></AuthResult></AuthResponse>" + ENV_TAIL
    )


def auth_fail() -> str:
    return (
        ENV_HEAD + '<AuthResponse xmlns="http://chomikuj.pl/"><AuthResult xmlns:a="http://chomikuj.pl">'
        "<a:status>InvalidPassword</a:status><a:errorMessage>InvalidPassword</a:errorMessage>"
        "<a:token>-1</a:token><a:hamsterId>-1</a:hamsterId></AuthResult></AuthResponse>" + ENV_TAIL
    )


def folders(items: list[tuple[str, str]]) -> str:
    infos = "".join(f"<FolderInfo><id>{i}</id><name>{n}</name></FolderInfo>" for i, n in items)
    return (
        ENV_HEAD + '<FoldersResponse xmlns="http://chomikuj.pl/"><FoldersResult xmlns:a="http://chomikuj.pl">'
        f'<a:status>Ok</a:status><a:folder><folders xmlns="http://chomikuj.pl">{infos}</folders></a:folder>'
        "</FoldersResult></FoldersResponse>" + ENV_TAIL
    )


def simple_ok(action: str, extra: str = "") -> str:
    return (
        ENV_HEAD + f'<{action}Response xmlns="http://chomikuj.pl/"><{action}Result xmlns:a="http://chomikuj.pl">'
        f"<a:status>Ok</a:status>{extra}</{action}Result></{action}Response>" + ENV_TAIL
    )


class FakeResponse:
    def __init__(self, text="", status_code=200, headers=None, content=b""):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}
        self._content = content
        self.closed = False

    def iter_content(self, chunk_size=1):
        for i in range(0, len(self._content), chunk_size):
            yield self._content[i : i + chunk_size]

    def close(self):
        self.closed = True


class FakeSession:
    """Routes SOAP POSTs by action to queued responses; GETs to a dict of URL -> FakeResponse."""

    def __init__(self):
        self.headers = {}
        self.soap: dict[str, list[str]] = {}
        self.gets: dict[str, FakeResponse] = {}
        self.calls: list[tuple[str, str]] = []

    def queue(self, action: str, *bodies: str):
        self.soap.setdefault(action, []).extend(bodies)

    def post(self, url, data=None, headers=None, timeout=None):
        action = headers["SOAPAction"].rsplit("/", 1)[-1]
        self.calls.append((action, data.decode("utf-8")))
        q = self.soap.get(action)
        if not q:
            raise AssertionError(f"unexpected SOAP call {action}")
        body = q.pop(0) if len(q) > 1 else q[0]
        return FakeResponse(text=body)

    def get(self, url, headers=None, stream=False, timeout=None):
        self.calls.append(("GET", url))
        return self.gets[url]


@pytest.fixture
def session():
    return FakeSession()


class UploadServer:
    """One-shot TCP server mimicking the chomikuj upload endpoint."""

    def __init__(self, reply=b'<resp res="1" fileid="1"/>'):
        self.reply = reply
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.head = b""
        self.body = b""
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.sock.accept()
        with conn:
            buf = b""
            while b"\r\n\r\n" not in buf:
                buf += conn.recv(65536)
            self.head, rest = buf.split(b"\r\n\r\n", 1)
            length = next(
                int(l.split(b":", 1)[1]) for l in self.head.split(b"\r\n") if l.lower().startswith(b"content-length")
            )
            body = rest
            while len(body) < length:
                data = conn.recv(65536)
                if not data:
                    break
                body += data
            self.body = body
            conn.sendall(self.reply)
        self.sock.close()

    def token_xml(self) -> str:
        return simple_ok(
            "UploadToken", f"<a:key>KEY</a:key><a:stamp>777</a:stamp><a:server>127.0.0.1:{self.port}</a:server>"
        )


@pytest.fixture
def upload_server():
    return UploadServer()
