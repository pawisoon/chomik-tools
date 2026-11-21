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

    def resolve_hit(self, hit):
        self.resolved = hit
        return RemoteFile(hit.id, hit.name, 4, Agreement("transfer", 1))

    def download_to(self, f, dest, on_progress=None):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(b"data")
        if on_progress:
            on_progress(4, 4)
        return dest


class FakeSearcher:
    def __init__(self):
        self.calls = []

    def search(self, query, **kw):
        from datetime import datetime

        from chomik_core import SearchHit, SearchPage

        self.calls.append((query, kw))
        hit = SearchHit("6295888111", "Big Buck Bunny 1080p (2008).avi", "149,3 MB", 156_552_806, "m_cel",
                        "/m_cel/Filmy/Big+Buck+Bunny",
                        "https://chomikuj.pl/m_cel/Filmy/Big+Buck+Bunny/Big+Buck+Bunny+1080p+(2008),6295888111.avi(video)",
                        datetime(2013, 2, 10, 4, 19))
        return SearchPage(query, kw.get("page", 1), [hit], has_next=True)


def wait(job, timeout=5.0):
    import time

    end = time.time() + timeout
    while job.status == "running" and time.time() < end:
        time.sleep(0.01)
    return job


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
    return Tools(cfg, client_factory=lambda letter: FakeClient({"A": "Mine", "B": "Backup"}[letter]),
                 searcher=FakeSearcher())


def test_search_passes_filters_and_returns_urls(tools):
    out = tools.search("big buck bunny", file_type="video", min_size_mb=100, page=2)
    assert tools.searcher.calls == [("big buck bunny", {"file_type": "video", "size_from_mb": 100,
                                                         "size_to_mb": 0, "page": 2})]
    assert out["has_next"] and out["count"] == 1
    assert out["hits"][0]["url"].endswith(",6295888111.avi(video)") and out["hits"][0]["date"] == "2013-02-10T04:19:00"


def test_download_file_saves_under_owner_folder(tools, tmp_path):
    url = tools.search("x")["hits"][0]["url"]
    out = tools.download_file(url)
    job = wait(tools.jobs.get(out["job_id"]))
    assert job.status == "done", job.error
    dest = tmp_path / "dl" / "m_cel" / "Filmy" / "Big Buck Bunny" / "Big Buck Bunny 1080p (2008).avi"
    assert out["saving_to"] == str(dest) and dest.read_bytes() == b"data"
    assert job.progress["bytes"] == 4


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
    assert names == {"accounts", "list_folders", "list_files", "search", "download", "download_file", "upload",
                     "clone_account", "clone_public", "job_status", "list_jobs"}
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
