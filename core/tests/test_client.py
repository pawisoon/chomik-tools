import pytest
from conftest import FakeResponse, auth_fail, auth_ok, fixture, folders, simple_ok

from chomik_core import (
    Agreement,
    AuthError,
    ChomikClient,
    DownloadError,
    RemoteFile,
    UploadError,
    relay_file,
)
from chomik_core.client import build_upload_request, parse_download
from chomik_core.public import parse_subfolder_links
from chomik_core import soap


def make(session, **kw):
    return ChomikClient("me", "secret", session=session, sleep=kw.pop("sleep", lambda s: None), **kw)


def test_parse_recorded_download_response():
    result = soap.find(soap.parse(fixture("download_with_url.xml")), "DownloadResult")
    [listing] = parse_download(result)
    assert listing.owner == "SomeUser"
    assert listing.path == "/SomeUser/Audi Instrukcja Obsługi"
    [f] = listing.files
    assert (f.id, f.name, f.size) == ("8706373657", "2008-audi-a3-s3-71864.pdf", 83452416)
    assert f.agreement == Agreement("own", 0)
    assert f.url.endswith("e=SIGNED_TOKEN&pv=2")  # &amp; unescaped


def test_login_and_password_hash(session):
    session.queue("Auth", auth_ok())
    c = make(session)
    c.login()
    assert (c.token, c.hamster_id) == ("TOK", "42")
    assert "<passHash>5ebe2294ecd0e0f08eab7690d2a6ee69</passHash>" in session.calls[0][1]
    c.login()  # cached
    assert len(session.calls) == 1


def test_login_rejected(session):
    session.queue("Auth", auth_fail())
    with pytest.raises(AuthError):
        make(session).login()


def test_mkdir_p_creates_only_missing(session):
    session.queue("Auth", auth_ok())
    session.queue("Folders", folders([("10", "Books")]), folders([]), folders([("11", "Sci Fi")]))
    session.queue("AddFolder", simple_ok("AddFolder"))
    folder = make(session).mkdir_p("/Books/Sci Fi/")
    assert (folder.id, folder.path) == ("11", ("Books", "Sci Fi"))
    assert [a for a, _ in session.calls].count("AddFolder") == 1
    add_body = next(b for a, b in session.calls if a == "AddFolder")
    assert "<newFolderId>10</newFolderId><name>Sci Fi</name>" in add_body


def test_walk(session):
    session.queue("Auth", auth_ok())
    session.queue("Folders", folders([("1", "A"), ("2", "B")]), folders([("3", "A1")]), folders([]), folders([]))
    paths = [f.path for f in make(session).walk()]
    assert paths == [("A",), ("A", "A1"), ("B",)]


def test_list_files_reads_balance(session):
    session.queue("Auth", auth_ok())
    session.queue("Download", fixture("download_with_url.xml"))
    c = make(session)
    listing = c.list_files("/SomeUser/Audi+Instrukcja+Obs*c5*82ugi")
    assert len(listing.files) == 1
    assert c.balance.transfer_value == 49318 and c.balance.transfer_extra == 2458252
    assert "<id>/SomeUser/Audi+Instrukcja+Obs*c5*82ugi</id>" in session.calls[-1][1]


def test_resolve_download_retries_when_url_withheld(session):
    delays = []
    session.queue("Auth", auth_ok())
    session.queue("Download", fixture("download_empty.xml"), fixture("download_with_url.xml"))
    c = make(session, sleep=delays.append)
    f = RemoteFile("8706373657", "x.pdf", 10, Agreement("own"))
    c.resolve_download(f)
    assert delays == [15]
    assert f.url.startswith("http://s8300.chomikuj.pl/File.aspx")


def test_resolve_download_gives_up(session):
    session.queue("Auth", auth_ok())
    session.queue("Download", fixture("download_empty.xml"))
    with pytest.raises(DownloadError):
        make(session).resolve_download(RemoteFile("1", "x", 1, Agreement("own")))


def test_small_agreement_omits_cost(session):
    session.queue("Auth", auth_ok())
    session.queue("Download", fixture("download_with_url.xml"))
    make(session).resolve_download(RemoteFile("8706373657", "x", 1, Agreement("small", 5)))
    body = session.calls[-1][1]
    assert "<name>small</name></AgreementInfo>" in body and "<cost>" not in body


def test_html_guard(session):
    f = RemoteFile("1", "v.mp4", 100, Agreement("own"), url="http://dl/x")
    session.gets["http://dl/x"] = FakeResponse(headers={"content-type": "text/html"}, content=b"<html>nope")
    with pytest.raises(DownloadError, match="HTML"):
        with make(session).open_download(f):
            pass
    assert f.url is None


def test_download_to_resumes(session, tmp_path):
    dest = tmp_path / "out.bin"
    (tmp_path / "out.bin.part").write_bytes(b"abc")
    f = RemoteFile("1", "out.bin", 6, Agreement("own"), url="http://dl/x")
    session.gets["http://dl/x"] = FakeResponse(headers={"content-length": "3"}, content=b"def")
    make(session).download_to(f, str(dest))
    assert dest.read_bytes() == b"abcdef"


def test_upload_content_length_counts_bytes(session, upload_server):
    session.queue("Auth", auth_ok())
    session.queue("UploadToken", upload_server.token_xml())
    payload = b"x" * 200_000
    progress = []
    name = make(session).upload(payload_chunks(payload), len(payload), "zażółć:.pdf", "99", lambda d, t: progress.append(d))
    upload_server.thread.join(5)
    assert name == "zażółć .pdf"
    assert payload in upload_server.body
    assert upload_server.body.endswith(b"--!CHB777--\r\n\r\n")
    assert 'filename="zażółć .pdf"'.encode() in upload_server.body
    assert progress[0] == 0 and progress[-1] == len(payload)


def test_upload_rejects_short_source(session, upload_server):
    session.queue("Auth", auth_ok())
    session.queue("UploadToken", upload_server.token_xml())
    with pytest.raises(UploadError, match="ended at"):
        make(session).upload([b"abc"], 10, "a.txt", "1")


def test_relay_streams_between_accounts(session, upload_server):
    src_session = type(session)()
    src_session.gets["http://dl/x"] = FakeResponse(headers={"content-length": "5"}, content=b"hello")
    src = make(src_session)
    session.queue("Auth", auth_ok())
    session.queue("UploadToken", upload_server.token_xml())
    dst = make(session)
    f = RemoteFile("1", "hi.txt", 5, Agreement("own"), url="http://dl/x")
    assert relay_file(src, f, dst, "7") == "hi.txt"
    upload_server.thread.join(5)
    assert b"hello\r\n--!CHB777--" in upload_server.body


def test_build_upload_request_length():
    head, tail = build_upload_request("h", "80", "k", "1", "ą.txt", 10, "5", "6")
    http, body_head = head.split(b"\r\n\r\n", 1)
    declared = int(http.split(b"Content-Length: ")[1])
    assert declared == len(body_head) + 10 + len(tail)


def test_parse_subfolder_links():
    page = (
        '<div id="foldersList"><a href="/u/Books">B</a><a href="/u/Books">dup</a>'
        '<a href="/u/file,123.pdf">f</a><a href="https://chomikuj.pl/u/Music">M</a></div>'
    )
    assert parse_subfolder_links(page, "https://chomikuj.pl/u") == [
        "https://chomikuj.pl/u/Books",
        "https://chomikuj.pl/u/Music",
    ]


def payload_chunks(data, n=8192):
    for i in range(0, len(data), n):
        yield data[i : i + n]


def test_account_a_is_delete_protected(session, monkeypatch):
    from chomik_core import ProtectedAccountError

    monkeypatch.setenv("CHOMIK_A_USER", "Me")
    monkeypatch.delenv("CHOMIK_PROTECTED_USERS", raising=False)
    c = make(session)  # username "me", case-insensitive match
    assert c.protected
    with pytest.raises(ProtectedAccountError):
        c.assert_deletable("delete file")
    other = ChomikClient("someone_else", "x", session=session)
    assert not other.protected
    other.assert_deletable()
    monkeypatch.setenv("CHOMIK_PROTECTED_USERS", "x, someone_else")
    assert other.protected


def test_list_files_missing_ok(session):
    not_avail = simple_ok("Download").replace(
        "<a:status>Ok</a:status>", "<a:status>failed</a:status><a:errorMessage>failed : requested file(s) not available</a:errorMessage>"
    )
    session.queue("Auth", auth_ok())
    session.queue("Download", not_avail)
    c = make(session)
    assert c.list_files("/me/Empty", missing_ok=True).files == []
    from chomik_core import ApiError
    with pytest.raises(ApiError):
        c.list_files("/me/Empty")
