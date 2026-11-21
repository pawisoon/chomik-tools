import json
import os
import stat

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

import chomik_mcp.panel as panel_mod
from chomik_mcp.__main__ import BearerAuth
from chomik_mcp.panel import Panel
from chomik_mcp.settings import Settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("CHOMIK_") or k in ("MCP_TOKEN", "PANEL_PASSWORD"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("PANEL_PASSWORD", "panel-secret")
    settings = Settings(str(tmp_path / "settings.json"))
    changes = []
    p = Panel(settings, str(tmp_path / "pw.json"), on_change=lambda: changes.append(1))

    async def mcp(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"mcp"})

    app = Starlette(routes=p.routes())
    app.mount("/mcp", mcp)
    client = TestClient(BearerAuth(app, settings.token), base_url="http://nas:8765")
    return client, settings, tmp_path, changes


def login(client):
    r = client.post("/panel/login", data={"password": "panel-secret"}, follow_redirects=False)
    assert r.status_code == 303
    return r


def test_panel_needs_login(env):
    client, *_ = env
    r = client.get("/")
    assert "Hasło do panelu" in r.text and "Twoje konta" not in r.text
    r = client.post("/panel/login", data={"password": "nope"}, follow_redirects=False)
    assert r.headers["location"] == "/?err=1"
    assert client.post("/panel/accounts", data={"user:A": "x"}, follow_redirects=False).status_code == 303
    assert client.post("/panel/api/test?letter=A").status_code == 401


def test_save_accounts_goes_to_env_and_0600_file(env):
    client, settings, tmp_path, changes = env
    login(client)
    client.post("/panel/accounts", data={"user:A": "Mine", "password:A": "pw1", "profile:A": "prof-Xq7", "profile_set:A": "1"})
    assert os.environ["CHOMIK_A_USER"] == "Mine" and os.environ["CHOMIK_A_PASSWORD"] == "pw1"
    assert os.environ["CHOMIK_A_PROFILE_PASSWORD"] == "prof-Xq7" and changes
    mode = stat.S_IMODE(os.stat(tmp_path / "settings.json").st_mode)
    assert mode == 0o600
    page = client.get("/").text
    assert 'value="Mine"' in page and "pw1" not in page and "prof-Xq7" not in page
    # empty password field keeps the saved one
    client.post("/panel/accounts", data={"user:A": "Mine", "password:A": ""})
    assert settings.accounts["A"]["password"] == "pw1"


def test_add_and_remove_account(env):
    client, settings, *_ = env
    login(client)
    client.post("/panel/accounts/add", data={"user:A": "Mine", "password:A": "pw"})
    assert set(settings.accounts) == {"A", "B"} and settings.accounts["A"]["user"] == "Mine"
    client.post("/panel/accounts/remove", data={"letter": "B"})
    assert set(settings.accounts) == {"A"}


def test_folder_password_from_link(env, monkeypatch):
    client, settings, tmp_path, _ = env
    login(client)
    monkeypatch.setattr(panel_mod, "find_folder_form", lambda link: ("Other", "217", "Locked"))
    client.post("/panel/folders", data={"link": "https://chomikuj.pl/Other/Locked", "password": "fp"})
    client.post("/panel/profiles", data={"owner": "Other", "password": "op"})
    data = json.loads((tmp_path / "pw.json").read_text())
    assert data == {"profiles": {"Other": "op"}, "folders": {"Other": {"217": {"name": "Locked", "password": "fp"}}}}
    assert "fp" not in client.get("/").text


def test_token_shown_in_panel_and_required_on_mcp(env):
    client, settings, *_ = env
    token = settings.token()
    assert client.get("/mcp/").status_code == 401
    assert client.get("/mcp/", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    login(client)
    page = client.get("/").text
    assert token in page and page.count(token) == 2  # token field + hidden copy source, not the visible example
    client.post("/panel/token")
    assert settings.token() != token
    assert client.get("/mcp/", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_arr_settings_shown_in_panel(env, monkeypatch):
    client, settings, *_ = env
    login(client)
    page = client.get("/").text
    assert "http://nas:8765/arr/newznab" in page and "/arr/sabnzbd" in page
    assert settings.arr_api_key() in page and len(settings.arr_api_key()) == 32
    monkeypatch.setenv("ARR_ENABLED", "0")
    assert "/arr/newznab" not in client.get("/").text


def test_cross_site_post_rejected(env):
    client, *_ = env
    login(client)
    r = client.post("/panel/accounts", data={"user:A": "evil"}, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403


def test_find_folder_form_parses_locked_page(monkeypatch):
    class R:
        text = ('<form id="LoginToFolder" action="/action/Files/LoginToFolder"><input name="ChomikName" value="Other">'
                '<input name="FolderId" value="217"><input name="FolderName" value="Locked"><input name="Password"></form>')

    class S:
        def get(self, *a, **k):
            return R()

    assert panel_mod.find_folder_form("https://chomikuj.pl/Other/Locked", S()) == ("Other", "217", "Locked")
    assert panel_mod.find_folder_form("https://evil.example/x", S()) is None
