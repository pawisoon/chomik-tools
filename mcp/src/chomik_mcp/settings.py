"""Accounts and the MCP token entered in the web panel, stored in one JSON file.

The file lives in the data folder with 0600 permissions. Its accounts are
copied into the environment (CHOMIK_<X>_USER / _PASSWORD / _PROFILE_PASSWORD),
which is where the rest of the code reads them, and override variables set in
docker-compose.
"""
from __future__ import annotations

import json
import os
import secrets
import string

LETTERS = string.ascii_uppercase


class Settings:
    def __init__(self, path: str):
        self.path = path
        self.data: dict = {"accounts": {}, "mcp_token": "", "secret": ""}
        self.load()

    def load(self) -> None:
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        if not self.data.get("secret"):
            self.data["secret"] = secrets.token_hex(32)
            self.save()

    def save(self) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ------------------------------------------------------------ accounts

    @property
    def accounts(self) -> dict[str, dict]:
        return self.data.setdefault("accounts", {})

    def next_letter(self) -> str:
        used = set(self.accounts) | {k.split("_")[1] for k in os.environ if k.startswith("CHOMIK_") and k.endswith("_USER")}
        return next(c for c in LETTERS if c not in used)

    def set_account(self, letter: str, user: str, password: str | None, profile_password: str | None) -> None:
        acc = self.accounts.setdefault(letter, {})
        acc["user"] = user.strip()
        if password:
            acc["password"] = password
        if profile_password is not None:
            if profile_password:
                acc["profile_password"] = profile_password
            else:
                acc.pop("profile_password", None)

    def remove_account(self, letter: str) -> None:
        self.accounts.pop(letter, None)
        for suffix in ("USER", "PASSWORD", "PASSWORD_HASH", "PROFILE_PASSWORD"):
            os.environ.pop(f"CHOMIK_{letter}_{suffix}", None)

    def apply_to_env(self) -> None:
        for letter, acc in self.accounts.items():
            if acc.get("user"):
                os.environ[f"CHOMIK_{letter}_USER"] = acc["user"]
            if acc.get("password"):
                os.environ[f"CHOMIK_{letter}_PASSWORD"] = acc["password"]
                os.environ.pop(f"CHOMIK_{letter}_PASSWORD_HASH", None)
            if acc.get("profile_password"):
                os.environ[f"CHOMIK_{letter}_PROFILE_PASSWORD"] = acc["profile_password"]
            else:
                os.environ.pop(f"CHOMIK_{letter}_PROFILE_PASSWORD", None)

    # --------------------------------------------------------------- token

    def token(self) -> str:
        """MCP_TOKEN from the environment wins; else the panel's token (created on first use)."""
        env = os.environ.get("MCP_TOKEN", "")
        if len(env) >= 16:
            return env
        if not self.data.get("mcp_token"):
            self.new_token()
        return self.data["mcp_token"]

    def token_from_env(self) -> bool:
        return len(os.environ.get("MCP_TOKEN", "")) >= 16

    def new_token(self) -> str:
        self.data["mcp_token"] = secrets.token_urlsafe(32)
        self.save()
        return self.data["mcp_token"]


def load_passwords_file(path: str) -> dict:
    if os.path.exists(path):
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    else:
        data = {}
    data.setdefault("profiles", {})
    data.setdefault("folders", {})
    return data


def save_passwords_file(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
