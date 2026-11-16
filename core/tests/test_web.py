import json
from urllib.parse import parse_qs

import pytest

from chomik_core import AuthError
from chomik_core.passwords import Passwords
from chomik_core.web import CaptchaRequired, ChomikWeb, WebCopyError, find_token, parse_form

PAGE = '<form><input name="__RequestVerificationToken" type="hidden" value="TOK1"></form> topbarCredits jxp'
COPY_FORM = (
    '<form id="copyFileForm" action="/action/content/copy/CopyFile">'
    '<input name="chosenFile.FileId" type="hidden" value="99">'
    '<input name="ChomikName" type="hidden" value="dst">'
    '<input name="LastChoosenFolderId" type="hidden" value="0">'
    '<input name="chosenFile.Cfbskr" type="hidden" value="-123">'
    '<input name="chosenFile.FolderSelection" type="radio" value="0" checked>'
    '<input name="chosenFile.FolderSelection" type="radio" value="2">'
    '<input name="CfSubmitBtn" type="submit" value="OK"></form>'
)
TREE = '<input name="SelectedFolderId" value="0"><input name="ChomikName" value="dst"><input name="TreeTicks" value="777">'


class R:
    def __init__(self, body, status=200):
        self.text = body if isinstance(body, str) else json.dumps(body)
        self.status_code = status

    def json(self):
        return json.loads(self.text)


class FakeWebSession:
    def __init__(self, routes):
        self.headers = {}
        self.routes = routes  # path -> list of bodies
        self.posts = []

    def get(self, url, timeout=None):
        return R(PAGE)

    def post(self, url, data=None, timeout=None, headers=None):
        path = url.replace("https://chomikuj.pl", "")
        self.posts.append((path, data))
        q = self.routes[path]
        return R(q.pop(0) if len(q) > 1 else q[0])


def web(routes):
    return ChomikWeb("dst", "pw", session=FakeWebSession(routes))


OK_LOGIN = {"IsSuccess": True, "Type": "Redirect"}
COPIED = {"IsSuccess": True, "Type": "Growl", "Data": {"Status": "OK"}, "Content": "Plik został zachomikowany"}
TOO_FAST = {"IsSuccess": True, "Type": "Window", "Data": None,
            "Content": "<p>Nie możesz tak szybko chomikować plików. Odczekaj kilka sekund i spróbuj ponownie.</p>"}


def test_parse_form_and_token():
    action, f = parse_form(COPY_FORM, "copyFileForm")
    assert action == "/action/content/copy/CopyFile"
    assert f["chosenFile.FolderSelection"] == "0" and "CfSubmitBtn" not in f
    assert find_token(PAGE) == "TOK1"


def test_copy_into_folder_sends_tree_fields():
    w = web({
        "/action/Login/TopBarLogin": [OK_LOGIN],
        "/action/Content/Copy/CopyFileWindow": [{"IsSuccess": True, "Type": "Window", "Content": COPY_FORM, "ContainsCaptcha": False}],
        "/action/content/selecttree/LoadTree": [TREE],
        "/action/content/copy/CopyFile": [COPIED],
    })
    assert "zachomikowany" in w.copy_file("99", "6")
    path, data = w.s.posts[-1]
    assert path == "/action/content/copy/CopyFile"
    assert data["chosenFile.FolderSelection"] == "2" and data["SelectedFolderId"] == "6"
    assert data["TreeTicks"] == "777" and data["chosenFile.Cfbskr"] == "-123"
    assert data["__RequestVerificationToken"] == "TOK1"


def test_generic_error_is_failure_even_with_issuccess():
    w = web({
        "/action/Login/TopBarLogin": [OK_LOGIN],
        "/action/Content/Copy/CopyFileWindow": [{"IsSuccess": True, "Content": COPY_FORM, "ContainsCaptcha": False}],
        "/action/content/copy/CopyFile": [{"IsSuccess": True, "Content": "Podczas obsługi akcji wystąpił błąd."}],
    })
    with pytest.raises(WebCopyError):
        w.copy_file("99", None)


def test_captcha_flag_raises():
    w = web({"/action/Login/TopBarLogin": [{"IsSuccess": False, "ContainsCaptcha": True}]})
    with pytest.raises(CaptchaRequired):
        w.login()


def test_contains_captcha_false_is_not_captcha():
    w = web({
        "/action/Login/TopBarLogin": [OK_LOGIN],
        "/action/Content/Copy/CopyFileWindow": [{"IsSuccess": True, "Content": "<p>nothing</p>", "ContainsCaptcha": False}],
    })
    with pytest.raises(WebCopyError, match="no copy form"):
        w.copy_file("1", None)


def test_unlock_account_and_folder_fields():
    w = web({
        "/action/Login/TopBarLogin": [OK_LOGIN],
        "/action/UserAccess/LoginToProtectedWindow": [{"IsSuccess": True, "Type": "Redirect"}],
        "/action/Files/LoginToFolder": [{"IsSuccess": True, "Data": ""}],
    })
    pw = Passwords({"Src": "p1"}, {"src": {"217": {"name": "Locked", "password": "p2"}}})
    w.apply_passwords(pw, "SRC")
    acc = next(d for p, d in w.s.posts if p.endswith("LoginToProtectedWindow"))
    fol = next(d for p, d in w.s.posts if p.endswith("LoginToFolder"))
    assert acc["TargetChomikName"] == "SRC" and acc["Password"] == "p1"
    assert (fol["FolderId"], fol["FolderName"], fol["Password"]) == ("217", "Locked", "p2")


def test_unlock_wrong_password():
    w = web({
        "/action/Login/TopBarLogin": [OK_LOGIN],
        "/action/Files/LoginToFolder": [{"IsSuccess": True, "Content": "Nieprawidłowe hasło"}],
    })
    with pytest.raises(AuthError):
        w.unlock_folder("src", "1", "x", "bad")


def test_passwords_load(tmp_path):
    f = tmp_path / "p.json"
    f.write_text(json.dumps({"profiles": {"Other": "x"}, "folders": {"Other": {"5": {"name": "F", "password": "y"}}}}))
    p = Passwords.load(str(f), env={"CHOMIK_A_USER": "Mine", "CHOMIK_A_PROFILE_PASSWORD": "z", "CHOMIK_B_PROFILE_PASSWORD": ""})
    assert p.profile("other") == "x" and p.profile("MINE") == "z"
    assert p.folders_of("OTHER") == {("5", "F"): "y"}
    assert "x" not in repr(p) and "z" not in repr(p)


ROWS = (
    '<div class="filerow fileItemContainer"><div class="fileinfo tab"><ul><li><span>0 KB</span></li></ul></div>'
    '<div class="fileActionsButtons clear visibleButtons fileIdContainer" rel="111"></div>'
    '<a class="expanderHeader downloadAction downloadContext" href="/u/F/How+to+Install(1),111.txt">x</a>'
    '<p class="additionalInfo">z chomika <span class="bold"><a href="/SomeUser">SomeUser</a></span></p></div>'
    '<div class="filerow alt fileItemContainer"><div class="fileinfo tab"><ul><li><span>10,51 GB</span></li></ul></div>'
    '<div class="fileActionsButtons clear visibleButtons fileIdContainer" rel="222"></div>'
    '<a class="expanderHeader downloadAction downloadContext" href="/u/F/Obs*c5*82uga+v2,222.avi(video)">y</a></div>'
)


def test_parse_file_rows_alternating_and_encoded():
    from chomik_core.web import parse_file_rows

    rows = parse_file_rows(ROWS)
    assert [(r.id, r.name, r.size_text, r.source) for r in rows] == [
        ("111", "How to Install(1).txt", "0 KB", "SomeUser"),
        ("222", "Obsługa v2.avi", "10,51 GB", None),
    ]


def test_network_errors_retry_reads_but_not_the_copy():
    import requests

    class Flaky(FakeWebSession):
        def __init__(self, routes, fail):
            super().__init__(routes)
            self.fail = fail  # path -> number of timeouts before answering

        def post(self, url, data=None, timeout=None, headers=None):
            path = url.replace("https://chomikuj.pl", "")
            if self.fail.get(path):
                self.fail[path] -= 1
                self.posts.append((path, "TIMEOUT"))
                raise requests.ReadTimeout("read timed out")
            return super().post(url, data, timeout, headers)

    routes = {
        "/action/Login/TopBarLogin": [OK_LOGIN],
        "/action/Content/Copy/CopyFileWindow": [{"IsSuccess": True, "Content": COPY_FORM, "ContainsCaptcha": False}],
        "/action/content/selecttree/LoadTree": [TREE],
        "/action/content/copy/CopyFile": [COPIED],
    }
    waits = []
    s = Flaky(routes, {"/action/Content/Copy/CopyFileWindow": 2, "/action/content/copy/CopyFile": 1})
    w = ChomikWeb("dst", "pw", session=s, sleep=waits.append)
    with pytest.raises(WebCopyError, match="network error"):
        w.copy_file("99", "6")  # window retried twice and worked; the copy timed out once and is NOT re-sent
    assert waits == [10, 30]
    assert [p for p, d in s.posts if p.endswith("/CopyFile")] == ["/action/content/copy/CopyFile"]


def copy_routes(*copy_replies):
    return {
        "/action/Login/TopBarLogin": [OK_LOGIN],
        "/action/Content/Copy/CopyFileWindow": [{"IsSuccess": True, "Content": COPY_FORM, "ContainsCaptcha": False}],
        "/action/content/selecttree/LoadTree": [TREE],
        "/action/content/copy/CopyFile": list(copy_replies),
    }


def test_too_fast_is_waited_out_not_counted_as_copied():
    waits = []
    w = ChomikWeb("dst", "pw", session=FakeWebSession(copy_routes(TOO_FAST, TOO_FAST, COPIED)), sleep=waits.append)
    assert "zachomikowany" in w.copy_file("99", "6")
    assert waits == [10, 30]
    assert len([p for p, _ in w.s.posts if p.endswith("/CopyFile")]) == 3


def test_too_fast_forever_raises_rate_limited():
    from chomik_core.web import CopyRateLimited

    w = ChomikWeb("dst", "pw", session=FakeWebSession(copy_routes(TOO_FAST)), sleep=lambda s: None)
    with pytest.raises(CopyRateLimited):
        w.copy_file("99", "6")


def test_issuccess_without_status_ok_is_not_success():
    w = web(copy_routes({"IsSuccess": True, "Type": "Window", "Data": None, "Content": "<p>Coś innego</p>"}))
    with pytest.raises(WebCopyError, match="rejected"):
        w.copy_file("99", "6")


FOLDER_FORM = (
    '<form id="copyFolderForm" action="/action/content/copy/CopyFolder">'
    '<input name="chosenFolder.ChomikName" type="hidden" value="src">'
    '<input name="chosenFolder.FolderId" type="hidden" value="233">'
    '<input name="chosenFolder.Cobskr" type="hidden" value="-5">'
    '<input name="chosenFolder.Name" type="text" value="Ebooki Test">'
    '<input name="SelectedFolderId" type="hidden" value="0"></form>'
)


def test_copy_folder_posts_selected_parent():
    w = web({
        "/action/Login/TopBarLogin": [OK_LOGIN],
        "/action/Content/Copy/CopyFolderWindow": [{"IsSuccess": True, "Content": FOLDER_FORM, "ContainsCaptcha": False}],
        "/action/content/selecttree/LoadTree": [TREE],
        "/action/content/copy/CopyFolder": [{"IsSuccess": True, "Type": "Growl", "Data": {"Status": "OK"},
                                             "Content": "Folder został zachomikowany"}],
    })
    assert "Folder" in w.copy_folder("src", "233", "2204")
    win = next(d for p, d in w.s.posts if p.endswith("CopyFolderWindow"))
    assert win == {"chosenFolder.ChomikName": "src", "chosenFolder.FolderId": "233", "__RequestVerificationToken": "TOK1"}
    path, data = w.s.posts[-1]
    assert path == "/action/content/copy/CopyFolder"
    assert data["SelectedFolderId"] == "2204" and data["TreeTicks"] == "777" and data["chosenFolder.Cobskr"] == "-5"
