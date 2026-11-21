"""Site-wide search: parsing saved result pages (captured live 2026-11-21) and request handling."""
from datetime import datetime

import pytest

from chomik_core import ChomikClient, DownloadError, ProtocolError
from chomik_core.search import (SEARCH_URL, SearchHit, Searcher, SearchRateLimited, hit_from_url, parse_date,
                                parse_search_page, parse_size)
from chomik_core.web import CaptchaRequired

from conftest import auth_ok, fixture


def test_parse_size():
    assert parse_size("152,4 MB") == int(152.4 * 1024**2)
    assert parse_size("0,86 GB") == int(0.86 * 1024**3)
    assert parse_size("7 KB") == 7 * 1024
    assert parse_size("1 023 KB") == 1023 * 1024
    assert parse_size("") == 0 and parse_size("?") == 0


def test_parse_date():
    assert parse_date("10 lut 13 4:19") == datetime(2013, 2, 10, 4, 19)
    assert parse_date("3 paź 21 23:05") == datetime(2021, 10, 3, 23, 5)
    assert parse_date("nonsense") is None


def test_parse_results_page():
    page = parse_search_page(fixture("search_video.html"), "big buck bunny")
    assert len(page.hits) == 24  # 25 rows minus one partner ad
    assert page.has_next is True
    hit = next(h for h in page.hits if h.id == "6295888111")
    assert hit.name == "Big Buck Bunny 1080p (2008).avi"
    assert hit.owner == "m_cel"
    assert hit.folder_path == "/m_cel/Filmy/Big+Buck+Bunny"
    assert hit.url.startswith("https://chomikuj.pl/m_cel/Filmy/")
    assert all(h.size_bytes > 0 and h.size_text for h in page.hits)
    assert all(h.date is not None for h in page.hits)
    # Polish characters in folder names come back re-encoded the SOAP way.
    assert any("*c3*b3" in h.folder_path for h in page.hits)


def test_hit_from_url():
    h = hit_from_url("https://chomikuj.pl/m_cel/Filmy/Big+Buck+Bunny/Big+Buck+Bunny+1080p+(2008),6295888111.avi(video)")
    assert (h.id, h.name, h.owner) == ("6295888111", "Big Buck Bunny 1080p (2008).avi", "m_cel")
    assert h.folder_path == "/m_cel/Filmy/Big+Buck+Bunny"
    with pytest.raises(ValueError):
        hit_from_url("https://chomikuj.pl/m_cel/Filmy")


def test_parse_empty_page():
    page = parse_search_page(fixture("search_empty.html"), "qzxwvkjhqpzzt")
    assert page.hits == [] and page.has_next is False


def test_captcha_page_raises():
    with pytest.raises(CaptchaRequired):
        parse_search_page('<div class="g-recaptcha" data-sitekey="x"></div>')


def test_unexpected_page_raises():
    with pytest.raises(ProtocolError):
        parse_search_page("<html>maintenance</html>")


class R:
    def __init__(self, text, status_code=200):
        self.text, self.status_code = text, status_code


class FakeSearchSession:
    def __init__(self, *bodies):
        self.headers = {}
        self.bodies = list(bodies)
        self.posts = []

    def post(self, url, data=None, timeout=None, headers=None):
        self.posts.append((url, data))
        return R(self.bodies.pop(0))


def test_search_sends_form_and_spaces_requests():
    now = [100.0]
    slept = []

    def sleep(s):
        slept.append(s)
        now[0] += s

    s = FakeSearchSession(fixture("search_video.html"), fixture("search_empty.html"))
    searcher = Searcher(s, min_interval=2.0, sleep=sleep, clock=lambda: now[0])
    searcher.search("big buck bunny", file_type="video", size_from_mb=100, page=2)
    searcher.search("other")
    url, form = s.posts[0]
    assert url == SEARCH_URL
    assert form["FileName"] == "big buck bunny" and form["FileType"] == "video"
    assert form["SizeFrom"] == "100" and form["Page"] == "2"
    assert slept == [2.0]  # second call waited for the gap


def test_search_validates_input():
    searcher = Searcher(FakeSearchSession(), sleep=lambda s: None)
    with pytest.raises(ValueError):
        searcher.search("  ")
    with pytest.raises(ValueError):
        searcher.search("x", file_type="films")


def test_search_retries_when_rate_limited():
    too_fast = "<p>Nie możesz tak szybko wyszukiwać</p>"
    s = FakeSearchSession(too_fast, fixture("search_empty.html"))
    page = Searcher(s, sleep=lambda x: None).search("x")
    assert page.hits == [] and len(s.posts) == 2

    s = FakeSearchSession(too_fast, too_fast, too_fast)
    with pytest.raises(SearchRateLimited):
        Searcher(s, sleep=lambda x: None).search("x")


def _hit(id_, name="2008-audi-a3-s3-71864.pdf"):
    return SearchHit(id_, name, "80 MB", 0, "SomeUser", "/SomeUser/Audi+Instrukcja+Obs*c5*82ugi", "https://x")


def test_resolve_hit_by_id_then_name(session):
    session.queue("Auth", auth_ok())
    session.queue("Download", fixture("download_with_url.xml"))
    c = ChomikClient("u", "p", session=session, sleep=lambda s: None)
    f = c.resolve_hit(_hit("8706373657", name="renamed.pdf"))
    assert f.id == "8706373657" and f.size == 83452416 and f.agreement.name
    assert "<id>/SomeUser/Audi+Instrukcja+Obs*c5*82ugi</id>" in session.calls[-1][1]
    assert c.resolve_hit(_hit("999")).id == "8706373657"  # matched by name


def test_resolve_hit_missing(session):
    session.queue("Auth", auth_ok())
    session.queue("Download", fixture("download_with_url.xml"))
    c = ChomikClient("u", "p", session=session, sleep=lambda s: None)
    with pytest.raises(DownloadError, match="no longer listed"):
        c.resolve_hit(_hit("999", name="gone.pdf"))


@pytest.mark.live
def test_live_search():
    page = Searcher().search("big buck bunny", file_type="video")
    assert page.hits, "real search returned nothing"
    assert all(h.id.isdigit() and h.folder_path.startswith("/") for h in page.hits)
