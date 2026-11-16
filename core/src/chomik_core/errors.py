"""Exception hierarchy. Everything raised by chomik_core derives from ChomikError."""


class ChomikError(Exception):
    """Base error for all chomik_core failures."""


class ProtocolError(ChomikError):
    """Server answered with something we could not parse or did not expect."""


class AuthError(ChomikError):
    """Login rejected or token missing."""


class ApiError(ChomikError):
    """SOAP call returned a non-Ok status. `code` holds the server's errorMessage."""

    def __init__(self, action: str, code: str | None):
        self.action = action
        self.code = code or "unknown"
        super().__init__(f"{action} failed: {self.code}")


class DownloadError(ChomikError):
    """Download URL withheld, HTML served instead of a file, or stream truncated."""


class ProtectedAccountError(ChomikError):
    """Destructive operation attempted on a protected account (CHOMIK_A_USER is always protected)."""


class UploadError(ChomikError):
    """Upload token rejected or upload server refused the file."""
