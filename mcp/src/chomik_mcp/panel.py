"""Web panel: enter accounts, profile/folder passwords, and get the MCP token.

Served next to /mcp in HTTP mode. Login with PANEL_PASSWORD. Polish UI, like
chomik-web-downloader and chomik-web-uploader.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import time
from urllib.parse import parse_qs, quote, urlparse

import requests
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from chomik_core import ChomikClient
from chomik_core.web import ChomikWeb, parse_form

from .settings import Settings, load_passwords_file, save_passwords_file

COOKIE = "chomik_panel"
SESSION_SECONDS = 7 * 24 * 3600

T = {  # Polish UI strings (DeepL)
    "settings": "Ustawienia", "login": "Zaloguj się", "logout": "Wyloguj się", "panel_pw": "Hasło do panelu",
    "wrong_pw": "Nieprawidłowe hasło",
    "no_panel_pw": "Aby otworzyć ten panel, ustaw zmienną środowiskową PANEL_PASSWORD.",
    "accounts": "Twoje konta na chomikuj.pl", "account": "Konto", "user": "Login", "password": "Hasło",
    "profile_pw": "Hasło do profilu (opcjonalne)", "keep": "Zostaw puste, żeby zachować zapisane hasło",
    "save_accounts": "Zapisz konta", "add_account": "Dodaj konto", "remove": "Usuń", "test": "Sprawdź logowanie",
    "test_ok": "Logowanie działa", "test_fail": "Logowanie nie powiodło się", "saved": "Zapisano",
    "protected": "Pierwsze konto (A) jest zabezpieczone przed usunięciem.",
    "others": "Hasła do kont chomikuj innych użytkowników", "profiles": "Hasła do profilu",
    "username": "Nazwa użytkownika", "folders": "Hasła do folderów", "folder_link": "Link do folderu",
    "folder_hint": "Wklej link do folderu chronionego hasłem, na przykład https://chomikuj.pl/User/Folder",
    "add": "Dodaj", "empty": "Jeszcze nie ma wpisów", "connect": "Połącz swojego asystenta AI",
    "mcp_url": "Adres MCP", "token": "Token dostępu", "show": "Pokaż", "copy": "Skopiuj",
    "new_token": "Wygeneruj nowy token",
    "new_token_hint": "Po wygenerowaniu nowego tokenu zaktualizuj go w swoim asystencie AI.",
    "example": "Przykładowa konfiguracja",
    "privacy": "Twoje hasła są przechowywane wyłącznie na tym serwerze, w folderze danych. "
               "Nigdy nie są wysyłane do asystenta AI.",
    "status": "Stan serwera", "configured": "Skonfigurowane konta",
    "no_form": "Nie udało mi się znaleźć formularza do podania hasła pod tym linkiem. "
               "Czy ten folder naprawdę jest chroniony hasłem?",
    "copied": "Skopiowano",
}


def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


class Panel:
    def __init__(self, settings: Settings, passwords_file: str, on_change=None):
        self.settings = settings
        self.passwords_file = passwords_file
        self.on_change = on_change or (lambda: None)

    # ---------------------------------------------------------------- auth

    @property
    def password(self) -> str:
        return os.environ.get("PANEL_PASSWORD", "")

    def _sign(self, expiry: int) -> str:
        key = (self.settings.data["secret"] + self.password).encode()
        return f"{expiry}.{hmac.new(key, str(expiry).encode(), hashlib.sha256).hexdigest()}"

    def _authed(self, request: Request) -> bool:
        if not self.password:
            return False
        value = request.cookies.get(COOKIE, "")
        expiry, _, _ = value.partition(".")
        return expiry.isdigit() and int(expiry) > time.time() and hmac.compare_digest(value, self._sign(int(expiry)))

    @staticmethod
    def _same_origin(request: Request) -> bool:
        origin = request.headers.get("origin")
        return origin is None or urlparse(origin).netloc == request.headers.get("host")

    @staticmethod
    async def _form(request: Request) -> dict[str, str]:
        body = (await request.body()).decode("utf-8", "replace")
        return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}

    def _back(self, msg: str = "", err: str = "", anchor: str = "") -> RedirectResponse:
        q = f"?ok={quote(msg)}" if msg else (f"?err={quote(err)}" if err else "")
        return RedirectResponse("/" + q + (f"#{anchor}" if anchor else ""), status_code=303)

    def guard(self, fn):
        async def wrapped(request: Request) -> Response:
            if request.method == "POST" and not self._same_origin(request):
                return Response("bad origin", status_code=403)
            if not self._authed(request):
                if request.url.path.startswith("/panel/api/"):
                    return JSONResponse({"ok": False, "message": "login required"}, status_code=401)
                return RedirectResponse("/", status_code=303)
            return await fn(request)
        return wrapped

    # -------------------------------------------------------------- routes

    async def index(self, request: Request) -> Response:
        if not self.password:
            return HTMLResponse(_page("chomik-mcp", f'<div class="card narrow"><p>{T["no_panel_pw"]}</p></div>'))
        if not self._authed(request):
            err = f'<p class="err">{T["wrong_pw"]}</p>' if request.query_params.get("err") else ""
            body = f"""<form class="card narrow" method="post" action="/panel/login">
              <h2>{T['login']}</h2>{err}
              <label>{T['panel_pw']}<input type="password" name="password" autofocus required autocomplete="current-password"></label>
              <button class="btn primary" type="submit">{T['login']}</button></form>"""
            return HTMLResponse(_page("chomik-mcp", body))
        return HTMLResponse(_page("chomik-mcp", self._settings_page(request), logged_in=True))

    async def login(self, request: Request) -> Response:
        if not self._same_origin(request):
            return Response("bad origin", status_code=403)
        form = await self._form(request)
        if not self.password or not hmac.compare_digest(form.get("password", ""), self.password):
            time.sleep(1)
            return RedirectResponse("/?err=1", status_code=303)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(COOKIE, self._sign(int(time.time()) + SESSION_SECONDS), max_age=SESSION_SECONDS,
                        httponly=True, samesite="strict", secure=request.url.scheme == "https")
        return resp

    async def logout(self, request: Request) -> Response:
        resp = RedirectResponse("/", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    def _apply_accounts(self, form: dict[str, str]) -> None:
        for letter in sorted({k.split(":")[1] for k in form if k.startswith("user:")}):
            user = form.get(f"user:{letter}", "").strip()
            if not user:
                continue
            self.settings.set_account(letter, user, form.get(f"password:{letter}") or None,
                                      form.get(f"profile:{letter}") if form.get(f"profile_set:{letter}") else None)
        self.settings.save()
        self.settings.apply_to_env()
        self.on_change()

    async def save_accounts(self, request: Request) -> Response:
        self._apply_accounts(await self._form(request))
        return self._back(T["saved"], anchor="konta")

    async def add_account(self, request: Request) -> Response:
        self._apply_accounts(await self._form(request))
        letter = self.settings.next_letter()
        self.settings.accounts.setdefault(letter, {"user": ""})
        self.settings.save()
        return self._back(anchor=f"konto-{letter}")

    async def remove_account(self, request: Request) -> Response:
        form = await self._form(request)
        letter = form.get("letter", "")
        if letter in self.settings.accounts:
            self.settings.remove_account(letter)
            self.settings.save()
            self.on_change()
        return self._back(T["saved"], anchor="konta")

    async def test_account(self, request: Request) -> Response:
        letter = request.query_params.get("letter", "")
        acc = self.settings.accounts.get(letter, {})
        user = acc.get("user") or os.environ.get(f"CHOMIK_{letter}_USER", "")
        pw = acc.get("password") or os.environ.get(f"CHOMIK_{letter}_PASSWORD", "")
        if not user or not pw:
            return JSONResponse({"ok": False, "message": T["test_fail"]})
        try:
            ChomikClient(user, pw).login()
            ChomikWeb(user, pw).login()
            return JSONResponse({"ok": True, "message": T["test_ok"]})
        except Exception as e:  # noqa: BLE001 - shown to the panel user
            return JSONResponse({"ok": False, "message": f"{T['test_fail']}: {str(e)[:160]}"})

    async def add_profile(self, request: Request) -> Response:
        form = await self._form(request)
        owner, pw = form.get("owner", "").strip(), form.get("password", "")
        if owner and pw:
            data = load_passwords_file(self.passwords_file)
            data["profiles"][owner] = pw
            save_passwords_file(self.passwords_file, data)
        return self._back(T["saved"], anchor="inne")

    async def remove_profile(self, request: Request) -> Response:
        form = await self._form(request)
        data = load_passwords_file(self.passwords_file)
        data["profiles"].pop(form.get("owner", ""), None)
        save_passwords_file(self.passwords_file, data)
        return self._back(T["saved"], anchor="inne")

    async def add_folder(self, request: Request) -> Response:
        form = await self._form(request)
        link, pw = form.get("link", "").strip(), form.get("password", "")
        found = find_folder_form(link) if link and pw else None
        if not found:
            return self._back(err=T["no_form"], anchor="inne")
        owner, folder_id, name = found
        data = load_passwords_file(self.passwords_file)
        data["folders"].setdefault(owner, {})[folder_id] = {"name": name, "password": pw}
        save_passwords_file(self.passwords_file, data)
        return self._back(T["saved"], anchor="inne")

    async def remove_folder(self, request: Request) -> Response:
        form = await self._form(request)
        data = load_passwords_file(self.passwords_file)
        data["folders"].get(form.get("owner", ""), {}).pop(form.get("folder_id", ""), None)
        save_passwords_file(self.passwords_file, data)
        return self._back(T["saved"], anchor="inne")

    async def regenerate_token(self, request: Request) -> Response:
        self.settings.new_token()
        return self._back(T["saved"], anchor="mcp")

    def routes(self) -> list[Route]:
        g = self.guard
        return [
            Route("/", self.index),
            Route("/panel/login", self.login, methods=["POST"]),
            Route("/panel/logout", self.logout, methods=["POST"]),
            Route("/panel/accounts", g(self.save_accounts), methods=["POST"]),
            Route("/panel/accounts/add", g(self.add_account), methods=["POST"]),
            Route("/panel/accounts/remove", g(self.remove_account), methods=["POST"]),
            Route("/panel/api/test", g(self.test_account), methods=["POST"]),
            Route("/panel/profiles", g(self.add_profile), methods=["POST"]),
            Route("/panel/profiles/remove", g(self.remove_profile), methods=["POST"]),
            Route("/panel/folders", g(self.add_folder), methods=["POST"]),
            Route("/panel/folders/remove", g(self.remove_folder), methods=["POST"]),
            Route("/panel/token", g(self.regenerate_token), methods=["POST"]),
        ]

    # ---------------------------------------------------------------- page

    def _accounts(self) -> list[tuple[str, dict, str]]:
        """(letter, saved settings, source) for env and panel accounts."""
        out = {}
        for k, v in os.environ.items():
            if k.startswith("CHOMIK_") and k.endswith("_USER") and len(k) == len("CHOMIK_A_USER") and v:
                out[k[7]] = ({}, "env")
        for letter, acc in self.settings.accounts.items():
            out[letter] = (acc, "panel")
        return [(letter, *out[letter]) for letter in sorted(out)]

    def _settings_page(self, request: Request) -> str:
        msg = ""
        if request.query_params.get("ok"):
            msg = f'<div class="flash ok">{_e(request.query_params["ok"])}</div>'
        elif request.query_params.get("err"):
            msg = f'<div class="flash err">{_e(request.query_params["err"])}</div>'

        cards = []
        accounts = self._accounts() or [("A", {}, "panel")]  # first run: an empty form for account A
        for letter, acc, source in accounts:
            user = acc.get("user") or os.environ.get(f"CHOMIK_{letter}_USER", "")
            has_pw = bool(acc.get("password") or os.environ.get(f"CHOMIK_{letter}_PASSWORD"))
            has_profile = bool(acc.get("profile_password") or os.environ.get(f"CHOMIK_{letter}_PROFILE_PASSWORD"))
            pw_hint = T["keep"] if has_pw else ""
            remove = (f'<button class="btn ghost small" form="rm-{letter}" type="submit">{T["remove"]}</button>'
                      if source == "panel" and letter in self.settings.accounts else "")
            badge = '<span class="pill">🛡️</span>' if letter == "A" else ""
            cards.append(f"""
            <div class="acc" id="konto-{letter}">
              <div class="acc-head"><span class="letter">{letter}</span>{badge}
                <span class="grow"></span>
                <button class="btn ghost small" type="button" onclick="testLogin('{letter}', this)">{T['test']}</button>{remove}
              </div>
              <div class="grid3">
                <label>{T['user']}<input name="user:{letter}" value="{_e(user)}" autocomplete="off" required></label>
                <label>{T['password']}<input type="password" name="password:{letter}" placeholder="{_e(pw_hint)}" autocomplete="new-password"></label>
                <label>{T['profile_pw']}<input type="password" name="profile:{letter}" placeholder="{_e(T['keep'] if has_profile else '')}" autocomplete="new-password" oninput="this.form['profile_set:{letter}'].value=this.value?'1':''"></label>
              </div>
              <input type="hidden" name="profile_set:{letter}" value="">
              <div class="test" id="test-{letter}"></div>
            </div>""")
        rm_forms = "".join(f'<form id="rm-{letter}" method="post" action="/panel/accounts/remove"><input type="hidden" name="letter" value="{letter}"></form>'
                           for letter, _, source in accounts if source == "panel" and letter in self.settings.accounts)

        pw = load_passwords_file(self.passwords_file)
        profiles = "".join(f"""<li><span>{_e(owner)}</span><span class="dots">••••••</span>
            <form method="post" action="/panel/profiles/remove"><input type="hidden" name="owner" value="{_e(owner)}">
            <button class="btn ghost small">{T['remove']}</button></form></li>""" for owner in sorted(pw["profiles"]))
        folders = "".join(f"""<li><span>{_e(owner)} / {_e(entry.get('name'))}</span><span class="dots">••••••</span>
            <form method="post" action="/panel/folders/remove"><input type="hidden" name="owner" value="{_e(owner)}">
            <input type="hidden" name="folder_id" value="{_e(fid)}"><button class="btn ghost small">{T['remove']}</button></form></li>"""
                          for owner, entries in sorted(pw["folders"].items()) for fid, entry in sorted(entries.items()))
        empty = f'<li class="muted">{T["empty"]}</li>'

        host = request.headers.get("host", "localhost:8765")
        mcp_url = f"{request.url.scheme}://{host}/mcp"
        token = self.settings.token()
        cfg = lambda tok: json.dumps({"mcpServers": {"chomik": {"type": "http", "url": mcp_url,  # noqa: E731
                                                                "headers": {"Authorization": "Bearer " + tok}}}}, indent=2, ensure_ascii=False)
        config, config_masked = cfg(token), cfg("•" * 12)
        regen = ("" if self.settings.token_from_env() else
                 f"""<form method="post" action="/panel/token" onsubmit="return confirm('{T['new_token_hint']}')">
                 <button class="btn ghost small">{T['new_token']}</button></form>""")

        return f"""{msg}
        <section class="card" id="konta">
          <h2>{T['accounts']}</h2>
          <p class="muted">{T['protected']} {T['privacy']}</p>
          <form method="post" action="/panel/accounts" autocomplete="off">
            {''.join(cards) or f'<p class="muted">{T["empty"]}</p>'}
            <div class="row"><button class="btn primary" type="submit">{T['save_accounts']}</button>
            <button class="btn ghost" type="submit" formaction="/panel/accounts/add" formnovalidate>+ {T['add_account']}</button></div>
          </form>{rm_forms}
        </section>

        <section class="card" id="inne">
          <h2>{T['others']}</h2>
          <h3>{T['profiles']}</h3>
          <ul class="list">{profiles or empty}</ul>
          <form class="inline" method="post" action="/panel/profiles" autocomplete="off">
            <input name="owner" placeholder="{T['username']}" required>
            <input type="password" name="password" placeholder="{T['password']}" required autocomplete="new-password">
            <button class="btn ghost">{T['add']}</button>
          </form>
          <h3>{T['folders']}</h3>
          <ul class="list">{folders or empty}</ul>
          <form class="inline" method="post" action="/panel/folders" autocomplete="off">
            <input name="link" type="url" placeholder="{T['folder_link']}" required title="{T['folder_hint']}">
            <input type="password" name="password" placeholder="{T['password']}" required autocomplete="new-password">
            <button class="btn ghost">{T['add']}</button>
          </form>
          <p class="muted small">{T['folder_hint']}</p>
        </section>

        <section class="card" id="mcp">
          <h2>{T['connect']}</h2>
          <label>{T['mcp_url']}<div class="copyrow"><input readonly value="{_e(mcp_url)}" id="mcpurl"><button class="btn ghost small" type="button" onclick="copyField('mcpurl', this)">{T['copy']}</button></div></label>
          <label>{T['token']}<div class="copyrow"><input readonly type="password" value="{_e(token)}" id="tok">
            <button class="btn ghost small" type="button" onclick="const t=document.getElementById('tok');t.type=t.type==='password'?'text':'password'">{T['show']}</button>
            <button class="btn ghost small" type="button" onclick="copyField('tok', this)">{T['copy']}</button></div></label>
          <h3>{T['example']}</h3>
          <pre class="code" id="cfg">{_e(config_masked)}</pre>
          <textarea id="cfgfull" hidden>{_e(config)}</textarea>
          <div class="row"><button class="btn ghost small" type="button" onclick="copyText(document.getElementById('cfgfull').value, this)">{T['copy']}</button>{regen}</div>
        </section>"""


def find_folder_form(link: str, session: requests.Session | None = None) -> tuple[str, str, str] | None:
    """(owner, folder id, folder name) from the password form a locked folder page shows."""
    if urlparse(link).netloc.lower() not in ("chomikuj.pl", "www.chomikuj.pl"):
        return None
    try:
        r = (session or requests).get(link, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    except requests.RequestException:
        return None
    _, fields = parse_form(r.text, "LoginToFolder")
    if not fields.get("FolderId"):
        return None
    owner = fields.get("ChomikName") or urlparse(link).path.strip("/").split("/")[0]
    return owner, fields["FolderId"], fields.get("FolderName", "")


def _page(title: str, body: str, logged_in: bool = False) -> str:
    logout = (f'<form method="post" action="/panel/logout"><button class="btn ghost small">{T["logout"]}</button></form>'
              if logged_in else "")
    return f"""<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(title)} · {T['settings']}</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🐹</text></svg>">
<style>{CSS}</style></head>
<body><header><div class="wrap"><div class="logo"><span>🐹</span>chomik-mcp <small>{T['settings']}</small></div>{logout}</div></header>
<main class="wrap">{body}</main>
<footer class="wrap">
  <div class="support"><p>Podobają ci się te narzędzia?</p>
  <a class="big suppi" href="https://suppi.pl/pawels" target="_blank" rel="noopener">❤ Wsparcie na Suppi</a>
  <a class="big bmc" href="https://buymeacoffee.com/pawisoon" target="_blank" rel="noopener">☕ Buy Me a Coffee</a></div>
</footer>
<script>
function copyText(t, b) {{ navigator.clipboard.writeText(t).then(() => {{ const o = b.textContent; b.textContent = '{T['copied']}'; setTimeout(() => b.textContent = o, 1500); }}); }}
function copyField(id, b) {{ copyText(document.getElementById(id).value, b); }}
function testLogin(letter, b) {{
  const out = document.getElementById('test-' + letter); out.className = 'test'; out.textContent = '…'; b.disabled = true;
  fetch('/panel/api/test?letter=' + letter, {{ method: 'POST' }}).then(r => r.json()).then(d => {{
    out.className = 'test ' + (d.ok ? 'ok' : 'err'); out.textContent = (d.ok ? '✓ ' : '✗ ') + d.message;
  }}).catch(e => {{ out.className = 'test err'; out.textContent = e; }}).finally(() => b.disabled = false);
}}
</script></body></html>"""


CSS = """
:root{--bg:#fbf6ee;--card:#fffdf9;--ink:#1f1a14;--ink2:#5d5348;--line:#e6d9c6;--accent:#d9732b;--soft:#fbe3cf;--ok:#2f8f5b;--err:#c0392b}
@media (prefers-color-scheme:dark){:root{--bg:#17130f;--card:#221c16;--ink:#f4ece1;--ink2:#b7aa99;--line:#3a3027;--accent:#f0904a;--soft:#3a2616;--ok:#5cc58b;--err:#ff7b6b}}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,Segoe UI,sans-serif}
.wrap{max-width:900px;margin:0 auto;padding:0 16px}
header{border-bottom:1px solid var(--line);background:var(--card)}
header .wrap{display:flex;align-items:center;justify-content:space-between;height:60px}
.logo{display:flex;align-items:center;gap:8px;font-weight:800;font-size:18px}.logo span{font-size:24px}.logo small{font-weight:500;color:var(--ink2);font-size:14px}
main{padding:28px 16px 8px;display:grid;gap:20px}
main>*{min-width:0}
.card{background:var(--card);border:1px solid var(--line);border-radius:18px;padding:24px}
.card.narrow{max-width:420px;margin:40px auto;display:grid;gap:14px}
h2{font-size:20px;margin-bottom:6px}h3{font-size:15px;margin:20px 0 8px}
.muted{color:var(--ink2)}.small{font-size:13px;margin-top:8px}
label{display:grid;gap:6px;font-weight:600;font-size:13px;color:var(--ink2)}
input{width:100%;height:42px;padding:0 12px;border:1.5px solid var(--line);border-radius:10px;background:var(--bg);color:var(--ink);font:inherit}
input:focus{outline:none;border-color:var(--accent)}
.btn{display:inline-flex;align-items:center;justify-content:center;height:42px;padding:0 18px;border-radius:999px;border:1.5px solid transparent;font:600 14px system-ui,sans-serif;cursor:pointer;white-space:nowrap;text-decoration:none}
.btn.primary{background:var(--accent);color:#fff}.btn.ghost{background:transparent;border-color:var(--line);color:var(--ink)}
.btn.small{height:34px;padding:0 14px;font-size:13px}.btn:disabled{opacity:.6}
.row{display:flex;flex-wrap:wrap;gap:10px;margin-top:16px;align-items:center}.row form{display:contents}
.acc{border:1px solid var(--line);border-radius:14px;padding:16px;margin-top:14px}
.acc-head{display:flex;align-items:center;gap:8px;margin-bottom:12px}.grow{flex:1}
.letter{display:grid;place-items:center;width:32px;height:32px;border-radius:9px;background:var(--accent);color:#fff;font-weight:800}
.pill{font-size:16px}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}
.test{margin-top:10px;font-size:14px;font-weight:600}.test.ok{color:var(--ok)}.test.err{color:var(--err)}
.flash{padding:12px 16px;border-radius:12px;font-weight:600}.flash.ok{background:color-mix(in srgb,var(--ok) 15%,transparent);color:var(--ok)}.flash.err{background:color-mix(in srgb,var(--err) 15%,transparent);color:var(--err)}
.err{color:var(--err);font-weight:600}
.list{list-style:none;display:grid;gap:8px}.list li{display:flex;align-items:center;gap:12px;padding:10px 12px;border:1px solid var(--line);border-radius:10px}.list li>span:first-child{flex:1;overflow-wrap:anywhere}.list li.muted{border-style:dashed;justify-content:center;font-style:italic}.dots{color:var(--ink2)}
form.inline{display:grid;grid-template-columns:1.4fr 1fr auto;gap:10px;margin-top:10px}
.copyrow{display:flex;gap:8px}
.code{background:#1d1813;color:#f1e7da;border-radius:12px;padding:16px;font:13px/1.6 ui-monospace,Menlo,monospace;overflow-x:auto;white-space:pre}
footer{padding:28px 16px 48px}
.support{text-align:center;border-radius:18px;padding:24px;background:linear-gradient(135deg,#fff7e0,#ffe9ec);color:#2a1d10}
.support p{font-weight:700;margin-bottom:14px}
.big{display:inline-flex;align-items:center;gap:8px;margin:6px;padding:14px 26px;border-radius:14px;font-weight:800;font-size:17px;text-decoration:none}
.big.suppi{background:#ff5a5f;color:#fff}.big.bmc{background:#ffdd00;color:#111}
@media (max-width:700px){.grid3,form.inline{grid-template-columns:1fr}}
"""
