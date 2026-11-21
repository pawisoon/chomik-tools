import pytest
import requests

from chomik_core import ChomikClient
from chomik_core.errors import ProtocolError

AUTH_OK = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><AuthResponse xmlns="http://chomikuj.pl/">'
    "<AuthResult><status>Ok</status><token>T</token><hamsterId>5</hamsterId></AuthResult></AuthResponse></s:Body></s:Envelope>"
)


class Sess:
    def __init__(self, failures):
        self.failures, self.calls, self.headers = failures, 0, {}

    def post(self, *a, **k):
        self.calls += 1
        if self.calls <= self.failures:
            raise requests.ConnectionError("Connection reset by peer")
        r = requests.Response()
        r.status_code, r._content = 200, AUTH_OK.encode()
        return r


def test_soap_retries_transient_network_errors():
    waits = []
    c = ChomikClient("u", "p", session=Sess(2), sleep=waits.append)
    c.login()
    assert c.token == "T" and waits == [10, 30]


def test_soap_gives_up_after_retries():
    c = ChomikClient("u", "p", session=Sess(99), sleep=lambda s: None)
    with pytest.raises(ProtocolError, match="request failed"):
        c.login()


FAULT_500 = (
    '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
    "<faultstring>The server was unable to process the request due to an internal error.</faultstring>"
    "</s:Fault></s:Body></s:Envelope>"
)


class Flaky:
    """box.chomikuj.pl answering HTTP 500 to some calls (seen live 2026-11-21)."""

    def __init__(self, bodies):
        self.bodies, self.calls, self.headers = list(bodies), 0, {}

    def post(self, *a, **k):
        self.calls += 1
        status, text = self.bodies.pop(0) if len(self.bodies) > 1 else self.bodies[0]
        r = requests.Response()
        r.status_code, r._content = status, text.encode()
        return r


def test_soap_retries_server_500():
    waits = []
    s = Flaky([(500, FAULT_500), (500, "<html>error</html>"), (200, AUTH_OK)])
    c = ChomikClient("u", "p", session=s, sleep=waits.append)
    c.login()
    assert c.token == "T" and s.calls == 3 and waits == [2, 5]


def test_soap_500_gives_up_and_400_does_not_retry():
    c = ChomikClient("u", "p", session=Flaky([(500, FAULT_500)]), sleep=lambda s: None)
    with pytest.raises(ProtocolError, match="HTTP 500"):
        c.login()
    s = Flaky([(400, FAULT_500)])
    with pytest.raises(ProtocolError, match="HTTP 400"):
        ChomikClient("u", "p", session=s, sleep=lambda x: None).login()
    assert s.calls == 1
