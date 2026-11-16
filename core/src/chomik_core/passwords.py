"""Profile and folder passwords for protected chomiki, kept out of code and tool arguments.

Sources (later wins):
  1. JSON file, default `chomik-passwords.json` (gitignored):
       {
         "profiles": {"SomeChomik": "profile password"},
         "folders":  {"SomeChomik": {"217": {"name": "Folder name", "password": "folder password"}}}
       }
  2. Env vars for your own accounts: CHOMIK_<X>_PROFILE_PASSWORD applies to
     the CHOMIK_<X>_USER chomik (X = A, B, ...).

Account names are matched case-insensitively. Folder ids are the numeric ids
shown by list_folders / the site (folder name is needed by the site's form).
"""
from __future__ import annotations

import json
import os
import re


class Passwords:
    def __init__(self, profiles: dict[str, str] | None = None, folders: dict[str, dict[str, dict]] | None = None):
        self._profiles = {k.lower(): v for k, v in (profiles or {}).items() if v}
        self._folders = {k.lower(): v for k, v in (folders or {}).items()}

    @classmethod
    def load(cls, path: str | None = "chomik-passwords.json", env: dict[str, str] | None = None) -> "Passwords":
        profiles: dict[str, str] = {}
        folders: dict[str, dict] = {}
        if path and os.path.isfile(path):
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            profiles.update(data.get("profiles") or {})
            folders.update(data.get("folders") or {})
        env = os.environ if env is None else env
        for key, value in env.items():
            m = re.fullmatch(r"CHOMIK_([A-Z0-9]+)_PROFILE_PASSWORD", key)
            if m and value:
                user = env.get(f"CHOMIK_{m.group(1)}_USER")
                if user:
                    profiles[user] = value
        return cls(profiles, folders)

    def profile(self, account: str) -> str | None:
        return self._profiles.get(account.lower())

    def folders_of(self, account: str) -> dict[tuple[str, str], str]:
        out = {}
        for fid, entry in (self._folders.get(account.lower()) or {}).items():
            if isinstance(entry, dict) and entry.get("password"):
                out[(str(fid), entry.get("name", ""))] = entry["password"]
        return out

    def __repr__(self) -> str:  # never print secrets
        return f"Passwords(profiles={sorted(self._profiles)}, folders={ {k: len(v) for k, v in self._folders.items()} })"
