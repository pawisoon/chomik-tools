"""Settings from the environment. Credentials never come in as tool arguments."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(name) or default


@dataclass
class Config:
    download_dir: str = field(default_factory=lambda: _env("CHOMIK_DOWNLOAD_DIR", "/data/downloads"))
    upload_dir: str = field(default_factory=lambda: _env("CHOMIK_UPLOAD_DIR", "/data/uploads"))
    state_dir: str = field(default_factory=lambda: _env("CHOMIK_STATE_DIR", "/data/state"))
    passwords_file: str = field(default_factory=lambda: _env("CHOMIK_PASSWORDS_FILE", "/data/chomik-passwords.json"))
    env_file: str = field(default_factory=lambda: _env("CHOMIK_ENV_FILE", ".env"))
    settings_file: str = field(default_factory=lambda: _env("CHOMIK_SETTINGS_FILE", "/data/settings.json"))

    def accounts(self) -> dict[str, str]:
        """Configured account letters -> usernames (from CHOMIK_<X>_USER)."""
        out = {}
        for key, value in os.environ.items():
            m = re.fullmatch(r"CHOMIK_([A-Z])_USER", key)
            if m and value:
                out[m.group(1)] = value
        return dict(sorted(out.items()))

    def letter(self, account: str) -> str:
        """Accept a letter ("a") or a configured username; return the letter."""
        accounts = self.accounts()
        if account.upper() in accounts:
            return account.upper()
        for letter, user in accounts.items():
            if user.lower() == account.lower():
                return letter
        known = ", ".join(f"{k} ({v})" for k, v in accounts.items()) or "none"
        raise ValueError(f"unknown account {account!r}; configured: {known}")
