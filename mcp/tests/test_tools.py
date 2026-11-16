import asyncio
import json
import os

import pytest

from chomik_core import Agreement, Folder, FolderListing, RemoteFile
from chomik_mcp.config import Config
from chomik_mcp.server import Tools, _inside, build_server


class FakeClient:
    def __init__(self, username):
        self.username = username

    def login(self):
        pass

    def find_folder(self, parts):
        return Folder("7", parts[-1], tuple(parts))

    def list_folders(self, fid, path):
        return [Folder("8", "Sub", (*path, "Sub"))]

    def list_files(self, path, missing_ok=False):
        return FolderListing(path, self.username, [RemoteFile("1", "a.pdf", 10, Agreement("own"))])


@pytest.fixture
def tools(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("CHOMIK_"):
            monkeypatch.delenv(k)
    monkeypatch.setenv("CHOMIK_A_USER", "Mine")
    monkeypatch.setenv("CHOMIK_B_USER", "Backup")
    pw = tmp_path / "pw.json"
    pw.write_text(json.dumps({"profiles": {"Mine": "secret"}, "folders": {"Mine": {"5": {"name": "X", "password": "s2"}}}}))
    cfg = Config(download_dir=str(tmp_path / "dl"), upload_dir=str(tmp_path / "up"), state_dir=str(tmp_path / "st"),
                 passwords_file=str(pw))
    return Tools(cfg, client_factory=lambda letter: FakeClient({"A": "Mine", "B": "Backup"}[letter]))


def test_accounts_report_passwords_without_values(tools):
    out = tools.accounts()
    assert [a["account"] for a in out] == ["A", "B"]
    assert out[0]["profile_password_configured"] and out[0]["folder_passwords_configured"] == 1
    assert out[0]["delete_protected"] is True and out[1]["delete_protected"] is False
    assert "secret" not in json.dumps(out)


def test_accounts_by_letter_or_username(tools):
    assert tools.config.letter("b") == "B" and tools.config.letter("backup") == "B"
    with pytest.raises(ValueError, match="unknown account"):
        tools.config.letter("Z")


def test_list_files_from_account_path(tools):
    out = tools.list_files("A:/Books")
    assert out["owner"] == "Mine" and out["path"] == "/Books" and out["files"][0]["name"] == "a.pdf"


def test_list_folders(tools):
    assert tools.list_folders("A", "Books") == [{"name": "Sub", "path": "/Books/Sub"}]


def test_paths_cannot_escape_their_folders(tools, tmp_path):
    with pytest.raises(ValueError, match="escapes"):
        _inside(str(tmp_path / "up"), "../../etc/passwd")
    os.makedirs(tmp_path / "up")
    with pytest.raises(ValueError):
        tools.upload("../pw.json", "B")


def test_clone_refuses_same_account(tools):
    with pytest.raises(ValueError, match="same account"):
        tools.clone_account("A", "mine")


def test_no_delete_tools():
    names = {t.name for t in asyncio.run(build_server(Tools(Config())).list_tools())}
    assert names == {"accounts", "list_folders", "list_files", "download", "upload", "clone_account",
                     "clone_public", "job_status", "list_jobs"}
    assert not any(w in n for n in names for w in ("delete", "remove", "move", "rename"))


def test_http_requires_bearer_token():
    from chomik_mcp.__main__ import BearerAuth

    sent = []

    async def app(scope, receive, send):
        sent.append("app")

    async def send(msg):
        sent.append(msg.get("status"))

    auth = BearerAuth(app, "x" * 20)
    asyncio.run(auth({"type": "http", "path": "/mcp", "headers": []}, None, send))
    asyncio.run(auth({"type": "http", "path": "/mcp", "headers": [(b"authorization", b"Bearer " + b"x" * 20)]}, None, send))
    assert sent == [401, None, "app"]
