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
