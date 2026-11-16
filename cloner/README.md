# chomik-cloner

Copies a whole chomikuj.pl account, one folder of it, or someone's public folder
into another account you own. Built on [chomik-core](../core).

It only copies. It never deletes, renames or moves anything, on either account.
The `CHOMIK_A_USER` account is also delete-protected in chomik-core itself.

## How it works

1. **Plan:** walk the source tree, list every folder's files, print totals (count,
   size, biggest top-level folders, transfer agreement types).
2. **Copy:** for each file, create the matching folder on the destination and
   stream download→upload through this machine, with no temp files.
3. **Skip:** files already in the destination folder (same name and size) are
   skipped, and every result goes to a SQLite state file, so you can stop and
   rerun at any time.

## Usage

```bash
uv venv && uv pip install -e '.[dev]'
```

Credentials are letters mapped to `CHOMIK_<X>_USER` / `_PASSWORD` /
`_PASSWORD_HASH` in an env file (template: `../.env.example`).

```bash
# See what a full clone means before doing it
.venv/bin/chomik-clone --env ../.env account --src A --dst B --dry-run

# One folder, into a subfolder on B
.venv/bin/chomik-clone --env ../.env account --src A --dst B --only "Books/PDF" --dest-root "Backup"

# Everything
.venv/bin/chomik-clone --env ../.env account --src A --dst B

# Someone's public folder (read with B's token)
.venv/bin/chomik-clone --env ../.env public https://chomikuj.pl/user/Folder --dst B --dest-root "Public/user"
```

Options: `--max-files N`, `--max-size BYTES`, `--state FILE` (default
`clone-state.sqlite`), `-v`.

Skipped folders: `@eaDir` (Synology NAS thumbnails and index files, about 1,500
files in A) is skipped by default, with everything below it. Use `--skip-dir NAME`
(repeatable) to set your own list, or `--no-skip` to copy everything.

## Verified live (2026-11-13)

- A→B copy of 2 files (167 MB) in nested folders: exact byte sizes on B.
- Rerun with a fresh state DB found both files on B and copied nothing.
- Throughput about 1.1–1.2 MB/s per file through a home connection. A full clone
  of a large account takes days; run it on the NAS.

## Server-side copy (default `--method server`)

The destination's web session uses the site's "Zachomikuj" feature, so no bytes go
through this machine. The source must allow copying in its settings, and protected
profiles and folders need passwords (`--passwords`, see chomik-core). A captcha
stops the run cleanly, and rerunning resumes. `--fallback-stream` streams files
the site refuses.

Live run (2026-11-15): 5 files, 18.85 GB, A→B in about 5 s.

Arrival checks: after copying a folder's files, the cloner lists that folder on
the website (reliable, unlike the SOAP listing) up to 3 times over about a minute.
A file counts as done only once it shows up. Files that never appear are sent
once more, and if they still don't appear they're reported as `MISSING` and
retried on the next run (`--fallback-stream` streams them instead). The skip-existing
check uses the same website listing, so reruns don't create duplicates.

Found live (2026-11-15): the site sometimes answers "Plik został zachomikowany"
without copying (an 11 GB file failed twice, then worked on the third send). The
checks above catch that. `--no-verify` turns them off.

Pace: the default `--pause` is 5 s. In the first full run at 0.5 s, many copies
were silently dropped, and the same files arrived when sent 10 s apart. Some files
(for example 3 PDFs in A's root) still never arrive even when spaced; the site
gives no reason. They're reported as `MISSING`.

Locked source folders: before copying a folder, the cloner checks the destination
can see it. Padlocked folders are marked `locked` without any copy attempts. Add
their passwords to `chomik-passwords.json` and rerun.

Caveat: a very slow copy that lands after the re-send could still leave a
`name(1).ext` duplicate.

Rate limit (found 2026-11-16): when copies come too fast, the site answers
"Nie możesz tak szybko chomikować plików" with `IsSuccess: true`. Earlier versions
counted that as copied, which caused many of the silent drops. chomik-core now
counts a copy as done only on `Data.Status == "OK"`, and waits out the rate limit
(10/30/60 s) before redoing the copy.

Folder copy (default, server mode): a source folder that the destination doesn't
have yet is copied with the site's "Zachomikuj folder", one request for all its
files. Files that don't arrive are then copied one by one as usual. Live findings:
subfolders aren't included (each level gets its own request), and copying into a
folder that already exists makes a `Name(1)` duplicate, so existing folders always
go file by file. Turned off by `--no-folder-copy`, `--max-size` or `--max-files`.

Hidden duplicates: the SOAP listing returns only one of several identical files in
a folder (A's root listed `X(2).pdf` but hid `X.pdf` and `X(1).pdf`). With a web
session, the plan adds the files that only the website lists.

Files the site refuses: some files never copy, by single-file or folder copy, and
the site gives no reason (seen with some files the source had itself copied from
another user). They stay `MISSING`.

## Not yet

- Parallel transfers, quota pre-check against the destination's limits, web UI, Docker.
- Public folders: the subfolder walk scrapes HTML (same rule as the downloader)
  and hasn't been tested live in the cloner yet.

## Support

If these tools save you time, you can buy me a coffee:

<a href="https://suppi.pl/pawels"><img src="https://img.shields.io/badge/Suppi-Wesprzyj-ff5a5f?style=for-the-badge" alt="Suppi" height="40"></a>
<a href="https://buymeacoffee.com/pawisoon"><img src="https://img.shields.io/badge/Buy%20Me%20a%20Coffee-FFDD00?style=for-the-badge&logo=buymeacoffee&logoColor=black" alt="Buy Me a Coffee" height="40"></a>
