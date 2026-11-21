# chomik-core

Shared Python client for the ChomikBox SOAP API of [chomikuj.pl](https://chomikuj.pl).
Merged from `chomik-web-downloader` and `chomik-web-uploader`, so new tools
(account cloner, MCP server) and the existing web apps can share one
implementation.

No CLI, no web UI. Just the library.

## Install

```bash
pip install "chomik-core @ git+https://github.com/pawisoon/chomik-core.git"
```

Local development:

```bash
uv venv && uv pip install -e '.[dev]' && .venv/bin/pytest
```

## Usage

```python
from chomik_core import ChomikClient, relay_file, walk_public

a = ChomikClient("account_a", "password")          # or ChomikClient.from_env("CHOMIK_A_")
b = ChomikClient("account_b", password_hash="...")  # md5 hex also accepted

# Folder tree of your own account
for folder in a.walk():
    listing = a.list_files_in(folder)
    print("/".join(folder.path), len(listing.files))

# Create folders, upload, download
dest = b.mkdir_p("Backup/Books")
b.upload_file("/data/book.pdf", dest)
a.download_to(listing.files[0], "/downloads/book.pdf", on_progress=lambda done, total: ...)

# Copy between accounts without touching disk
relay_file(a, listing.files[0], b, dest)

# Someone's public folder (subfolders from HTML, files via your token)
for rel_path, listing in walk_public(b, "https://chomikuj.pl/someuser/Folder"):
    ...

print(a.balance)  # transfer quota, refreshed by every Download call
```

## API

| | |
|---|---|
| `login()` | SOAP `Auth`; cached 300 s, called for you |
| `list_folders(id)`, `walk()`, `find_folder(path)`, `mkdir_p(path)` | SOAP `Folders` / `AddFolder` |
| `list_files(path)`, `list_files_in(folder)` | SOAP `Download` with a folder path; returns files + `Balance` |
| `resolve_download(file)` | SOAP `Download` with file id + agreement; retries 15 s / 60 s when the URL is withheld |
| `open_download(file, offset)` | streaming GET with the ChomikBox UA and the HTML-error-page guard |
| `download_to(file, dest)` | resumes from `dest.part` |
| `upload(stream, size, name, folder_id)` | SOAP `UploadToken` + raw multipart socket upload, from a file object or any byte iterator |
| `upload_file(path, dest)` | convenience wrapper; creates `dest` |
| `relay_file(src, file, dst, folder)` | download stream piped into upload |
| `walk_public(client, url)` | other users' public folders, recursive |
| `Searcher().search(query, file_type=, size_from_mb=, size_to_mb=, page=)` | site-wide search (website, no login); 25 `SearchHit`s per page, sizes rounded; at least 2 s between requests |
| `resolve_hit(hit)` | lists the hit's folder to get exact size + agreement, ready for `download_to` |
| `search.hit_from_url(url)` | a chomikuj.pl file link as a `SearchHit` |

Errors all derive from `ChomikError` (`AuthError`, `ApiError`, `DownloadError`,
`UploadError`, `ProtocolError`). The client logs to the `chomik_core` logger and
never prints.

One client is one account and is not thread-safe. Use one per worker.

## Server-side copy and passwords (web session)

`chomik_core.web.ChomikWeb` logs in to the website (needs the plain password,
not the hash) for things the SOAP API can't do:

```python
from chomik_core.web import ChomikWeb
from chomik_core.passwords import Passwords

w = ChomikWeb.from_env("CHOMIK_B_")
w.apply_passwords(Passwords.load(), "SomeChomik")  # profile + folder passwords
w.copy_file(file_id, dest_folder_id)               # server-side "Zachomikuj", no bytes through you
```

- **Profile passwords:** for your own chomik, `CHOMIK_<X>_PROFILE_PASSWORD` in
  `.env`; for others, `chomik-passwords.json`.
- **Folder passwords:** in `chomik-passwords.json`, by owner + folder id
  (template: `chomik-passwords.example.json`). That file is gitignored, and
  `repr(Passwords)` never shows the values.
- **Copy needs the source's permission.** Owners can turn off "chomikowanie" of
  files and folders. Then the site shows no Zachomikuj button and `CopyFile`
  returns a generic error, which becomes `WebCopyError`.
- **Captchas** raise `CaptchaRequired` and are never solved automatically.

Verified live (2026-11-15): web login, profile unlock, and a server-side copy
A→B of an 83 MB file in 0.5 s (exact size on B), after copying was allowed on A.
Folder-password unlock is still unverified live.

## Needs live verification

The unit tests run against recorded and synthetic responses only. These
behaviours are new compared to the source repos and need checking against the
real API:

- Upload `Content-Length` now counts bytes rather than characters. The old code
  under-declared it for non-ASCII filenames.
- Large folders: `Download` responses carry `messageSequence part/count`.
  Multi-part responses aren't handled yet.

Verified live (2026-11-13): login, `list_folders`/`walk`, and `list_files_in()`
with chomikuj path encoding (`space -> +`, non-ASCII -> `*xx`) on folders with
spaces and Polish letters. The login name's case doesn't matter in paths.

### Live tests

Put two accounts in `.env` (gitignored; template in `.env.example`):

```
CHOMIK_A_USER=...      # source account
CHOMIK_A_PASSWORD=...  # or CHOMIK_A_PASSWORD_HASH=<md5 hex>
CHOMIK_B_USER=...      # destination account
CHOMIK_B_PASSWORD=...
```

Then run the read-only checks. Each one runs per account, and an account left
blank is skipped:

```bash
./scripts/live-test.sh
```

Without `.env`, the script prompts for account A. Tools can load the same file
with `chomik_core.envfile.load_env_file()` and
`ChomikClient.from_env("CHOMIK_A_")`.

## Support

If these tools save you time, you can buy me a coffee:

<a href="https://suppi.pl/pawels"><img src="https://img.shields.io/badge/Suppi-Wesprzyj-ff5a5f?style=for-the-badge" alt="Suppi" height="40"></a>
<a href="https://buymeacoffee.com/pawisoon"><img src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black" alt="Buy Me a Coffee" height="40"></a>
