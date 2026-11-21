"""Sonarr/Radarr bridge: release matching, Newznab feed, SABnzbd API and the download worker. No network."""
import os
import pathlib
import xml.etree.ElementTree as ET
from datetime import datetime

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from chomik_core import Agreement, Balance, ChomikError, RemoteFile, SearchHit, SearchPage
from chomik_core.search import parse_search_page
from chomik_core.web import CaptchaRequired
from chomik_mcp.arr import ArrBridge, ArrConfig
from chomik_mcp.arr.bridge import NEWZNAB_NS, read_nzb_token
from chomik_mcp.arr.release import Wanted, match, parse_query
from chomik_mcp.config import Config
from chomik_mcp.server import Tools

KEY = "k" * 32
CORE_FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "core" / "tests" / "fixtures"
GB = 1024**3


def hit(name, folder="/u/Filmy", size=2 * GB, id_="1"):
    return SearchHit(id_, name, "", size, "u", folder, f"https://chomikuj.pl{folder}/x,{id_}.mkv",
                     datetime(2020, 1, 2, 3, 4))


# ------------------------------------------------------------------ release


def test_parse_query():
    assert parse_query("Big Buck Bunny 2008", movie=True) == Wanted("Big Buck Bunny", 2008)
    assert parse_query("The Office", season="2", ep="3") == Wanted("The Office", None, 2, 3)
    assert parse_query("The Office S02E03") == Wanted("The Office", None, 2, 3)
    assert parse_query("Blade Runner 2049", movie=True) == Wanted("Blade Runner", 2049)  # arr appends the year too


def test_match_movie_from_real_search_page():
    page = parse_search_page((CORE_FIXTURES / "search_video.html").read_text(encoding="utf-8"))
    wanted = parse_query("Big Buck Bunny 2008", movie=True)
    releases = [r for h in page.hits if (r := match(h, wanted))]
    by_id = {r.hit.id: r.title for r in releases}
    assert by_id["6295888111"] == "Big.Buck.Bunny.2008.1080p.WEBRip-Chomikuj"  # "Big Buck Bunny 1080p (2008).avi"
    assert by_id["49670430"] == "Big.Buck.Bunny.2008.DVDRip-Chomikuj"  # "... [2008] DVDRip.avi"
    assert by_id["788872744"] == "Big.Buck.Bunny.2008.BDRip-Chomikuj"  # year taken from the request
    assert len(releases) == len(page.hits)  # every hit on this page is the film


def test_match_rules():
    movie = Wanted("Big Buck Bunny", 2008)
    assert match(hit("Big Buck Bunny 2008 720p WEB-DL lektor PL.mkv"), movie).title == \
        "Big.Buck.Bunny.2008.720p.WEB-DL.Polish-Chomikuj"
    assert match(hit("Big Buck Bunny 1999.mkv"), movie) is None  # wrong year
    assert match(hit("Big Bunny.mkv"), movie) is None  # title word missing
    assert match(hit("Big Buck Bunny.rar"), movie) is None  # not a video
    assert match(hit("Big Buck Bunny S01E01.mkv"), movie) is None  # episode, not a movie
    assert match(hit("Big Buck Bunny.mkv", size=10 * 1024**2), movie, min_bytes=100 * 1024**2) is None
    # Title only in the folder name, file named by episode
    tv = Wanted("Ojciec Mateusz", None, 3, 4)
    r = match(hit("odc 4.avi", folder="/u/Seriale/Ojciec+Mateusz/Sezon+3"), tv)
    assert r is not None and r.title == "Ojciec.Mateusz.S03E04.DVDRip-Chomikuj"
    assert match(hit("Ojciec Mateusz 3x04 HDTV 720p.mkv"), tv).title == "Ojciec.Mateusz.S03E04.720p.HDTV-Chomikuj"
    assert match(hit("Ojciec Mateusz S03E05.mkv"), tv) is None
    season = Wanted("Ojciec Mateusz", None, 3, None)
    assert match(hit("Ojciec Mateusz S03E05.mkv"), season).episode == 5
    assert match(hit("Ojciec Mateusz.mkv"), season) is None  # no episode marker
    # Polish letters fold for matching and naming
    assert match(hit("Ślepnąc od świateł S01E01.mkv"), Wanted("Slepnac od swiatel", None, 1, 1)) is not None
    assert match(hit("Łódź S01E01.mkv"), Wanted("Łódź", None, 1, 1)).title.startswith("Lodz.S01E01")


def test_tv_rejects_other_shows_sharing_the_title():
    # Real hits for "Ranczo S01E01" (2026-11-21)
    tv = Wanted("Ranczo", None, 1, 1)
    assert match(hit("Ranczo Duttonów S01E01.avi", folder="/u/Ranczo+Dutton*c3*b3w"), tv) is None
    assert match(hit("S01E01_Dino_ranczo.mp4", folder="/u/Bajki/Dino+ranczo"), tv) is None
    assert match(hit("Ranczo S01E01 Spadek (001).mkv", size=704 * 1024**2), tv).title == \
        "Ranczo.S01E01.720p.WEBRip-Chomikuj"  # no tag: 704 MB episode guessed as 720p
    assert match(hit("001.Ranczo.s01e01.avi", folder="/u/Ranczo+serial/Sezon+1"), tv) is not None
    r = match(hit("Ranczo - odc. 1 – Spadek (Sezon 1).mp4", folder="/u/RANCZO+Sezon+1+*282006*29"), tv)
    assert r is not None and (r.season, r.episode) == (1, 1)
    assert match(hit("079 Ranczo sezon7  odc.1.mp4", folder="/u/Ranczo"), tv) is None  # season 7


def test_tv_queries_are_targeted():
    assert ArrBridge.queries(Wanted("Ranczo", None, 1, 2)) == ["Ranczo S01E02", "Ranczo odc 2"]
    assert ArrBridge.queries(Wanted("Ranczo", None, 3)) == ["Ranczo S03", "Ranczo sezon 3"]
    assert ArrBridge.queries(Wanted("Big Buck Bunny", 2008)) == ["Big Buck Bunny"]


# ------------------------------------------------------------------- bridge


class FakeSearcher:
    def __init__(self, hits=None, error=None):
        self.hits = hits or []
        self.error = error
        self.calls = []

    def search(self, query, **kw):
        self.calls.append((query, kw))
        if self.error:
            raise self.error
        return SearchPage(query, kw.get("page", 1), list(self.hits), has_next=False)


class FakeClient:
    def __init__(self, username="Mine", size=11, agreement="transfer", balance=Balance(10_000, 0, 0, 0)):
        self.username = username
        self.size, self.agreement, self.balance = size, agreement, balance
        self.downloads = []

    def login(self):
        pass

    def resolve_hit(self, h):
        return RemoteFile(h.id, h.name, self.size, Agreement(self.agreement, 1), h.folder_path, h.owner)

    def download_to(self, f, dest, on_progress=None):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as fh:
            fh.write(b"x" * f.size)
        if on_progress:
            on_progress(f.size, f.size)
        self.downloads.append(dest)
        return dest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("CHOMIK_", "ARR_")):
            monkeypatch.delenv(k)
    monkeypatch.setenv("CHOMIK_A_USER", "Mine")
    cfg = Config(download_dir=str(tmp_path / "dl"), upload_dir=str(tmp_path / "up"), state_dir=str(tmp_path / "st"),
                 passwords_file=str(tmp_path / "pw.json"))
    fake = FakeClient()
    searcher = FakeSearcher([
        hit("Big Buck Bunny 2008 1080p BluRay.mkv", id_="11"),
        hit("Big Buck Bunny 2008 DVDRip.avi", id_="12", size=700 * 1024**2),
        hit("Something Else 2008.mkv", id_="13"),
    ])
    tools = Tools(cfg, client_factory=lambda letter: fake, searcher=searcher)
    bridge = ArrBridge(tools, "secret", KEY, config=ArrConfig(min_size_mb=100), start_worker=False)
    client = TestClient(Starlette(routes=bridge.routes()), base_url="http://nas:8765")
    return bridge, client, fake, searcher, tmp_path


def items(xml_text):
    root = ET.fromstring(xml_text)
    return root.findall("./channel/item")


def test_caps_and_auth(setup):
    _, c, *_ = setup
    caps = ET.fromstring(c.get("/arr/newznab/api?t=caps").text)
    assert caps.find("./searching/movie-search").get("supportedParams") == "q"
    err = ET.fromstring(c.get("/arr/newznab/api?t=search&q=x&apikey=wrong").text)
    assert err.tag == "error" and err.get("code") == "100"
    assert c.get("/arr/sabnzbd/api?mode=queue&apikey=wrong&output=json").json()["status"] is False
    assert c.get("/arr/sabnzbd/api?mode=version").json()["version"]
    assert c.get("/arr/health").status_code == 401


def test_rss_without_query_returns_placeholder(setup):
    _, c, _, searcher, _ = setup
    got = items(c.get(f"/arr/newznab/api?t=movie&apikey={KEY}").text)
    assert len(got) == 1 and "Connection.Test" in got[0].findtext("title")
    assert searcher.calls == []  # no chomikuj request for RSS


def test_movie_search_feed(setup):
    _, c, _, searcher, _ = setup
    r = c.get(f"/arr/newznab/api?t=movie&q=Big+Buck+Bunny+2008&apikey={KEY}&cat=2000,2040")
    got = items(r.text)
    titles = [i.findtext("title") for i in got]
    assert titles == ["Big.Buck.Bunny.2008.1080p.BluRay-Chomikuj", "Big.Buck.Bunny.2008.DVDRip-Chomikuj"]
    assert searcher.calls[0] == ("Big Buck Bunny", {"file_type": "video", "size_from_mb": 100, "size_to_mb": 0,
                                                    "page": 1})
    cats = [a.get("value") for a in got[0].findall(f"{{{NEWZNAB_NS}}}attr") if a.get("name") == "category"]
    assert cats == ["2000", "2040"]
    assert got[0].find("enclosure").get("url").startswith("http://nas:8765/arr/newznab/nzb/")
    # second identical search comes from the cache
    c.get(f"/arr/newznab/api?t=movie&q=Big+Buck+Bunny+2008&apikey={KEY}")
    assert len(searcher.calls) == 1


def test_search_errors_become_newznab_errors(setup):
    bridge, c, _, searcher, _ = setup
    searcher.error = CaptchaRequired("captcha")
    err = ET.fromstring(c.get(f"/arr/newznab/api?t=search&q=Other&apikey={KEY}").text)
    assert err.get("code") == "900" and "captcha" in err.get("description")
    assert bridge.last_error


def grab(c, title="Big.Buck.Bunny.2008.1080p.BluRay-Chomikuj"):
    feed = items(c.get(f"/arr/newznab/api?t=movie&q=Big+Buck+Bunny+2008&apikey={KEY}").text)
    url = next(i.find("enclosure").get("url") for i in feed if i.findtext("title") == title)
    nzb = c.get(url.replace("http://nas:8765", ""))
    assert nzb.status_code == 200 and b"<nzb" in nzb.content
    root = ET.fromstring(nzb.content)
    assert root.find(".//{http://www.newzbin.com/DTD/2003/nzb}segment") is not None  # arr NZB validation
    r = c.post(f"/arr/sabnzbd/api?mode=addfile&cat=radarr&priority=-100&apikey={KEY}&output=json",
               files={"name": (f"{title}.nzb", nzb.content, "application/x-nzb")})
    return r.json(), nzb.content


def test_grab_download_and_history(setup):
    bridge, c, fake, _, tmp = setup
    out, _ = grab(c)
    assert out["status"] is True and out["nzo_ids"][0].startswith("SABnzbd_nzo_")
    q = c.get(f"/arr/sabnzbd/api?mode=queue&apikey={KEY}&output=json").json()["queue"]
    assert q["slots"][0]["filename"] == "Big.Buck.Bunny.2008.1080p.BluRay-Chomikuj" and q["slots"][0]["cat"] == "radarr"

    bridge._run(bridge.queue.next_queued())

    assert c.get(f"/arr/sabnzbd/api?mode=queue&apikey={KEY}").json()["queue"]["slots"] == []
    h = c.get(f"/arr/sabnzbd/api?mode=history&apikey={KEY}&cat=radarr").json()["history"]["slots"][0]
    assert h["status"] == "Completed" and h["bytes"] == 11
    folder = tmp / "dl" / "arr" / "radarr" / "Big.Buck.Bunny.2008.1080p.BluRay-Chomikuj"
    assert h["storage"] == str(folder)
    assert (folder / "Big.Buck.Bunny.2008.1080p.BluRay-Chomikuj.mkv").read_bytes() == b"x" * 11

    # the arr removes it from history after import, deleting files
    c.get(f"/arr/sabnzbd/api?mode=history&name=delete&value={h['nzo_id']}&del_files=1&apikey={KEY}")
    assert c.get(f"/arr/sabnzbd/api?mode=history&apikey={KEY}").json()["history"]["slots"] == []
    assert not folder.exists()


def test_same_file_grabbed_twice_is_one_job(setup):
    _, c, *_ = setup
    a, _ = grab(c)
    b, _ = grab(c)
    assert a["nzo_ids"] == b["nzo_ids"]


def test_forged_nzb_rejected(setup):
    _, c, *_ = setup
    _, nzb = grab(c)
    token = read_nzb_token(nzb)
    forged = nzb.replace(token.encode(), token[:-4].encode() + b"0000")
    r = c.post(f"/arr/sabnzbd/api?mode=addfile&cat=radarr&apikey={KEY}",
               files={"name": ("x.nzb", forged, "application/x-nzb")})
    assert r.json()["status"] is False


def test_failures_reported_to_arr(setup):
    bridge, c, fake, *_ = setup
    fake.balance = Balance(5, 0, 0, 0)  # 5 MB of transfer left
    fake.size = 50 * 1024**2
    grab(c)
    bridge._run(bridge.queue.next_queued())
    h = c.get(f"/arr/sabnzbd/api?mode=history&apikey={KEY}").json()["history"]["slots"][0]
    assert h["status"] == "Failed" and "not enough transfer" in h["fail_message"]
    assert fake.downloads == []


def test_monthly_limit(setup):
    bridge, c, fake, *_ = setup
    bridge.cfg.monthly_limit_gb = 1
    fake.size = 2 * GB
    grab(c)
    bridge._run(bridge.queue.next_queued())
    h = c.get(f"/arr/sabnzbd/api?mode=history&apikey={KEY}").json()["history"]["slots"][0]
    assert h["status"] == "Failed" and "monthly limit" in h["fail_message"]


def test_queue_survives_restart(setup):
    bridge, c, fake, searcher, tmp = setup
    out, _ = grab(c)
    bridge.queue.update(out["nzo_ids"][0], status="Downloading")
    tools = bridge.tools
    again = ArrBridge(tools, "secret", KEY, config=ArrConfig(), start_worker=False)
    item = again.queue.next_queued()
    assert item is not None and item.nzo_id == out["nzo_ids"][0]


def test_sab_config_for_arr_test_button(setup):
    bridge, c, *_ = setup
    cfg = c.get(f"/arr/sabnzbd/api?mode=get_config&apikey={KEY}&output=json").json()["config"]
    assert cfg["misc"]["complete_dir"] == bridge.complete_dir
    assert any(cat["name"] == "radarr" for cat in cfg["categories"])
    # Sonarr's RemotePathMappingCheck errors when the category folder doesn't exist yet
    assert os.path.isdir(os.path.join(bridge.complete_dir, "sonarr"))
