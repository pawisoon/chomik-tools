"""chomikuj.pl web session: server-side "Zachomikuj" (copy a file into your chomik).

No bytes pass through this machine; the server copies the file between accounts.
Reverse-engineered from the site's JS (ch.UI.CopyFiles, 2026-11):

  POST /action/Login/TopBarLogin              Login, Password, ReturnUrl, __RequestVerificationToken
  POST /action/Content/Copy/CopyFileWindow    fileId                -> form #copyFileForm
  POST /action/Content/Copy/CopyFile          form fields + chosenFile.FolderSelection=2 + SelectedFolderId

  POST /action/UserAccess/LoginToProtectedWindow   profile (account) password
  POST /action/Files/LoginToFolder                 folder password

Copy only works when the source chomik allows it: owners can switch off
"chomikowanie" of files/folders, and then the site shows no Zachomikuj button
and CopyFile answers with a generic error (seen live on account A, 2026-11-13).

Folder copy (CopyFolder) exists too, but the site can put it behind a captcha
(CopyFolderCaptcha). This module never solves or bypasses captchas: it raises
CaptchaRequired and lets the caller fall back to per-file copy or streaming.
"""
from __future__ import annotations

import html as htmllib
import json
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser

import requests

from .errors import AuthError, ChomikError
from .passwords import Passwords

log = logging.getLogger("chomik_core")

BASE = "https://chomikuj.pl"
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"


class CaptchaRequired(ChomikError):
    """The site asked for a captcha. Do it by hand in a browser, or fall back to streaming."""


class WebCopyError(ChomikError):
    pass


class CopyRateLimited(WebCopyError):
    """The site kept answering "too fast" for a copy."""


class FolderLocked(ChomikError):
    """Folder has its own password and this session hasn't unlocked it."""


@dataclass(frozen=True)
class WebFile:
    id: str
    name: str  # full name with extension, as stored
    size_text: str  # rounded, e.g. "7 GB"; the site shows no exact size here
    source: str | None = None  # "z chomika X": account the file was copied from


_ROW_SPLIT = re.compile(r'class="filerow\b[^"]*\bfileItemContainer\b[^"]*"')  # rows alternate: "filerow alt fileItemContainer"
_ROW_ID = re.compile(r'fileIdContainer"\s+rel="(\d+)"')
_ROW_HREF = re.compile(r'class="expanderHeader[^"]*"\s+href="([^"]+)"')
_ROW_SIZE = re.compile(r'class="fileinfo[^"]*">.*?<span>([^<]+)</span>', re.S)
_ROW_SOURCE = re.compile(r'z chomika\s*<span class="bold"><a href="/([^"/]+)"')
_HREF_TAIL = re.compile(r",(\d+)(\.[^/?(]*)?(?:\([^)]*\))?$")
_NEXT_PAGE = re.compile(r'class="right"\s+rel="(\d+)"[^>]*>\s*nast')


def parse_file_rows(body: str) -> list[WebFile]:
    from .names import decode_segment

    out = []
    for chunk in _ROW_SPLIT.split(body)[1:]:
        m_id, m_href = _ROW_ID.search(chunk), _ROW_HREF.search(chunk)
        if not (m_id and m_href):
            continue
        seg = htmllib.unescape(m_href.group(1)).rstrip("/").rsplit("/", 1)[-1]
        tail = _HREF_TAIL.search(seg)
        base = seg[: tail.start()] if tail else seg
        ext = (tail.group(2) or "") if tail else ""
        size = _ROW_SIZE.search(chunk)
        src = _ROW_SOURCE.search(chunk)
        out.append(WebFile(m_id.group(1), decode_segment(base) + ext, size.group(1).strip() if size else "",
                           src.group(1) if src else None))
    return out


class _FormParser(HTMLParser):
    """Collect the first <form>'s inputs: name -> value (radios: the checked one, else first)."""

    def __init__(self, form_id: str | None = None):
        super().__init__()
        self.form_id = form_id
        self.in_form = False
        self.done = False
        self.action = None
        self.fields: dict[str, str] = {}
        self._radio_seen: set[str] = set()

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form" and not self.done and (self.form_id is None or a.get("id") == self.form_id):
            self.in_form, self.action = True, a.get("action")
        elif self.in_form and tag in ("input", "select", "textarea"):
            name = a.get("name")
            if not name or a.get("type") in ("submit", "reset", "button"):
                return
            if a.get("type") == "radio":
                if "checked" in a or name not in self._radio_seen:
                    self.fields[name] = a.get("value", "")
                self._radio_seen.add(name)
                return
            self.fields[name] = a.get("value", "")

    def handle_endtag(self, tag):
        if tag == "form" and self.in_form:
            self.in_form, self.done = False, True


def parse_form(page_html: str, form_id: str | None = None) -> tuple[str | None, dict[str, str]]:
    p = _FormParser(form_id)
    p.feed(page_html)
    return p.action, p.fields


_TOKEN = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"|value="([^"]+)"[^>]*name="__RequestVerificationToken"')


def find_token(page_html: str) -> str | None:
    m = _TOKEN.search(page_html)
    return (m.group(1) or m.group(2)) if m else None


def _json(r: requests.Response) -> dict:
    try:
        return r.json()
    except (ValueError, json.JSONDecodeError):
        raise WebCopyError(f"{r.request.path_url}: expected JSON, got HTTP {r.status_code} {r.text[:200]!r}")


def _text(fragment: str) -> str:
    return re.sub(r"\s+", " ", htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment or ""))).strip()


_ERROR_TEXT = re.compile(r"wyst[aą]pi[lł] b[lł][aą]d|spr[oó]bowa[cć] ponownie|nie uda[lł]o|b[lł][aą]d", re.I)
_RATE_LIMIT = re.compile(r"tak szybko", re.I)  # "Nie możesz tak szybko chomikować plików. Odczekaj kilka sekund..."
_CAPTCHA_MARKUP =re.compile(r'g-recaptcha|captchaContainer|data-sitekey', re.I)


def _captcha(d: dict) -> bool:
    """True only when the site really asks for one (flag or captcha widget in the HTML)."""
    if d.get("ContainsCaptcha") is True:
        return True
    return bool(_CAPTCHA_MARKUP.search(str(d.get("Content") or "") + str(d.get("Data") or "")))


class ChomikWeb:
    """Logged-in web session for one account. Use for server-side copies into this account."""

    def __init__(self, username: str, password: str, session: requests.Session | None = None, timeout: float = 30,
                 sleep=None):
        import time

        self._sleep = sleep or time.sleep
        self.username = username
        self._password = password
        self.timeout = timeout
        self.s = session or requests.Session()
        self.s.headers.update({"User-Agent": BROWSER_UA, "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.8"})
        self.token: str | None = None
        self.logged_in = False

    @classmethod
    def from_env(cls, prefix: str = "CHOMIK_B_", **kw) -> "ChomikWeb":
        import os

        user, pw = os.environ.get(prefix + "USER"), os.environ.get(prefix + "PASSWORD")
        if not user or not pw:
            raise ValueError(f"{prefix}USER and {prefix}PASSWORD needed (web login can't use the md5 hash)")
        return cls(user, pw, **kw)

    # Waits between attempts when the site drops or times out a request.
    NET_RETRY_DELAYS: tuple[float, ...] = (10, 30, 90)

    def _net(self, what: str, fn, retry: bool = True):
        """Run one HTTP call; network errors become WebCopyError (a ChomikError).

        retry=False for calls that change something (the copy itself): after a
        timeout the copy may have happened, and sending it again could leave a
        duplicate. The caller's arrival check sorts that out instead.
        """
        delays = self.NET_RETRY_DELAYS if retry else ()
        for attempt in range(len(delays) + 1):
            try:
                return fn()
            except requests.RequestException as e:
                if attempt == len(delays):
                    raise WebCopyError(f"{what}: network error: {e}") from e
                log.warning("%s: network error (%s); retry %d in %ss", what, e, attempt + 1, delays[attempt])
                self._sleep(delays[attempt])

    def _post(self, path: str, data: dict, *, retry: bool = True) -> requests.Response:
        data = {**data, "__RequestVerificationToken": self.token or ""}
        return self._net(path, lambda: self.s.post(BASE + path, data=data, timeout=self.timeout,
                                                   headers={"X-Requested-With": "XMLHttpRequest"}), retry)

    def _refresh_token(self, path: str = "/") -> str:
        r = self._net(path, lambda: self.s.get(BASE + path, timeout=self.timeout))
        tok = find_token(r.text)
        if not tok:
            raise WebCopyError(f"no anti-forgery token on {path}")
        self.token = tok
        return r.text

    def login(self) -> None:
        if self.logged_in:
            return
        self._refresh_token("/")
        r = self._post("/action/Login/TopBarLogin", {"Login": self.username, "Password": self._password, "ReturnUrl": ""})
        d = _json(r)
        if _captcha(d):
            raise CaptchaRequired("web login asks for a captcha; log in once by hand in a browser, then retry")
        if not d.get("IsSuccess"):
            msg = _text(d.get("Content") or d.get("Data") or "") or str({k: d.get(k) for k in ("Type", "Title")})
            raise AuthError(f"web login failed for {self.username!r}: {msg[:200]}")
        # Token is per session; the logged-in page carries the new one.
        page = self._refresh_token(f"/{self.username}")
        # topbarCredits only exists in the logged-in top bar (ch.UI.TopBar.loggedIn).
        if "topbarCredits" not in page and "Wyloguj" not in page:
            raise AuthError("web login reported success but the page doesn't look logged in")
        self.logged_in = True
        log.info("web session logged in as %s", self.username)

    # ------------------------------------------------------- password unlocks

    def unlock_account(self, account: str, password: str, *, adult: bool = False) -> None:
        """Enter a chomik's profile (access) password for this session ("Dostęp do tego konta wymaga podania hasła")."""
        self.login()
        fields = {
            "Password": password,
            "RemeberMe": "false",  # sic, the site's field name
            "IsAdult": "True" if adult else "False",
            "Sex": "",
            "AccountName": account,
            "AdultFilter": "True",
            "ChomikType": "Friend",
            "TargetChomikName": account,
        }
        r = self._post("/action/UserAccess/LoginToProtectedWindow", fields)
        if r.text.lstrip().startswith("{"):
            self._check_unlock(_json(r), f"profile password for {account!r}")
        # Usually a plain form post: the site redirects to the chomik page.
        # Unlocked = page served 200 without the password form (locked = 401 + form).
        elif r.status_code == 401 or "LoginToProtectedWindow" in r.text:
            raise AuthError(f"profile password for {account!r} rejected")
        self._refresh_token(f"/{account}")
        log.info("unlocked profile %s", account)

    def unlock_folder(self, account: str, folder_id: str, folder_name: str, password: str) -> None:
        """Enter a folder password (padlock folders) for this session."""
        self.login()
        fields = {
            "ChomikName": account,
            "FolderId": str(folder_id),
            "FolderName": folder_name,
            "Password": password,
            "Remember": "false",
        }
        d = _json(self._post("/action/Files/LoginToFolder", fields))
        self._check_unlock(d, f"folder password for {account}/{folder_name}")
        log.info("unlocked folder %s/%s", account, folder_name)

    def _check_unlock(self, d: dict, what: str) -> None:
        if _captcha(d):
            raise CaptchaRequired(f"{what}: the site asks for a captcha")
        msg = _text(d.get("Content") or "")
        if not d.get("IsSuccess") or _ERROR_TEXT.search(msg) or re.search(r"nieprawid[lł]owe has[lł]o|z[lł]e has[lł]o", msg, re.I):
            raise AuthError(f"{what} rejected: {msg[:200] or d.get('Type')}")

    def apply_passwords(self, passwords: "Passwords", account: str) -> None:
        """Unlock `account`'s profile and every configured folder of it, if passwords are known."""
        pw = passwords.profile(account)
        if pw:
            self.unlock_account(account, pw)
        for (folder_id, folder_name), fpw in passwords.folders_of(account).items():
            self.unlock_folder(account, folder_id, folder_name, fpw)

    # --------------------------------------------------------------- listing

    def list_files(self, owner: str, folder_id: str, *, max_pages: int = 2000) -> list[WebFile]:
        """All files in a folder as the website shows them (paged, 30 per page).

        More reliable than the SOAP Download listing, which was seen to omit
        files. Raises FolderLocked for padlocked folders not unlocked yet.
        """
        self.login()
        out: list[WebFile] = []
        page = 1
        while page and page <= max_pages:
            r = self._post("/action/files/fileslistonly", {
                "chomikName": owner, "folderId": str(folder_id),
                "fileListSortType": "Name", "fileListAscending": "True",
                "gallerySortType": "Name", "galleryAscending": "False",
                "pageNr": str(page), "isGallery": "False",
            })
            body = r.text
            if 'id="LoginToFolder"' in body:
                raise FolderLocked(f"folder {owner}/{folder_id} is password-protected")
            out.extend(parse_file_rows(body))
            nxt = _NEXT_PAGE.search(body)
            page = int(nxt.group(1)) if nxt and int(nxt.group(1)) > page else 0
        return out

    # ------------------------------------------------------------------ copy

    # Waits after the site's "too fast" answer before trying the same copy again.
    RATE_LIMIT_DELAYS: tuple[float, ...] = (10, 30, 60)

    def copy_file(self, file_id: str, dest_folder_id: str | None) -> str:
        """Server-side copy of any visible file into this account.

        `dest_folder_id=None` uses the site's default "zachomikowane" folder.
        Returns the site's confirmation text.
        """

        def prepare(fields):
            if "chosenFile.FileId" not in fields:
                return False
            if dest_folder_id is not None:
                # The browser loads the folder tree into the form before you pick a
                # folder; its TreeTicks must come along or the copy fails.
                fields.update(self._tree_fields(fields.get("LastChoosenFolderId", "0")))
                fields["chosenFile.FolderSelection"] = "2"
                fields["SelectedFolderId"] = str(dest_folder_id)
            return True

        return self._copy(f"file {file_id}", "/action/Content/Copy/CopyFileWindow", {"fileId": str(file_id)},
                          "copyFileForm", "/action/Content/Copy/CopyFile", prepare)

    def copy_folder(self, owner: str, folder_id: str, dest_parent_id: str) -> str:
        """Server-side copy of one folder's files ("Zachomikuj folder").

        Creates a folder with the source folder's name inside `dest_parent_id`
        and copies the files directly in it. Found live (2026-11-16):
        subfolders are NOT copied, and if a folder of that name already exists
        the site makes "Name(1)" instead of merging, so only use this for
        folders the destination doesn't have yet.
        """

        def prepare(fields):
            if "chosenFolder.FolderId" not in fields:
                return False
            fields.update(self._tree_fields(fields.get("SelectedFolderId", "0")))
            fields["SelectedFolderId"] = str(dest_parent_id)
            return True

        return self._copy(f"folder {owner}/{folder_id}", "/action/Content/Copy/CopyFolderWindow",
                          {"chosenFolder.ChomikName": owner, "chosenFolder.FolderId": str(folder_id)},
                          "copyFolderForm", "/action/content/copy/CopyFolder", prepare)

    def _copy(self, what: str, window: str, window_data: dict, form_id: str, default_action: str, prepare) -> str:
        """Open the copy window, fill the form, submit; wait and redo on "too fast".

        Success is only {"Data": {"Status": "OK"}}. The site answers IsSuccess
        true for errors and for its rate limit too ("Nie możesz tak szybko
        chomikować plików"), which used to be counted as copied.
        """
        self.login()
        for attempt in range(len(self.RATE_LIMIT_DELAYS) + 1):
            d = _json(self._post(window, window_data))
            content = d.get("Content") or d.get("Data") or ""
            if _captcha(d):
                raise CaptchaRequired(f"copy of {what} asks for a captcha")
            action, fields = parse_form(content if isinstance(content, str) else "", form_id)
            if not prepare(fields):
                raise WebCopyError(f"copy window for {what} has no copy form: {_text(str(content))[:200]!r}")
            fields.pop("__RequestVerificationToken", None)
            d = _json(self._post(action if action and action.startswith("/") else default_action, fields, retry=False))
            if _captcha(d):
                raise CaptchaRequired(f"copy of {what} asks for a captcha")
            data = d.get("Data")
            msg = _text(d.get("Content") or d.get("Message") or (data if isinstance(data, str) else "") or "")
            if d.get("IsSuccess") and isinstance(data, dict) and str(data.get("Status", "")).upper() == "OK":
                return msg
            if _RATE_LIMIT.search(msg):
                if attempt == len(self.RATE_LIMIT_DELAYS):
                    raise CopyRateLimited(f"copy of {what}: still rate-limited after retries: {msg[:200]}")
                delay = self.RATE_LIMIT_DELAYS[attempt]
                log.warning("copy of %s: site says too fast; retry %d in %ss", what, attempt + 1, delay)
                self._sleep(delay)
                continue
            raise WebCopyError(f"copy of {what} rejected: {msg[:300] or d}")
        raise AssertionError("unreachable")

    def _tree_fields(self, selected: str) -> dict[str, str]:
        r = self._post("/action/content/selecttree/LoadTree", {"chomikName": self.username, "selectedFolderId": selected})
        body = r.text
        try:
            j = r.json()
            body = j.get("Content") or j.get("Data") or j.get("Html") or body
        except ValueError:
            pass
        _, f = parse_form("<form>" + body + "</form>")
        return {k: v for k, v in f.items() if k in ("TreeTicks", "SelectedFolderId", "ChomikName")}
