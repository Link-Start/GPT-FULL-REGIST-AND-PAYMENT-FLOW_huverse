from __future__ import annotations


class ProtocolError(RuntimeError):
    """Base error for protocol-first ChatGPT flows."""


class ProtocolResponseError(ProtocolError):
    def __init__(self, method: str, url: str, status_code: int, body: str | None = None) -> None:
        detail = body[:500] if body else ""
        super().__init__(f"{method} {url} failed with HTTP {status_code}: {detail}")
        self.method = method
        self.url = url
        self.status_code = status_code
        self.body = body


class MissingFieldError(ProtocolError):
    def __init__(self, field: str, source: str) -> None:
        super().__init__(f"missing field {field!r} from {source}")
        self.field = field
        self.source = source


class UnsupportedProtocolStep(ProtocolError):
    """Raised when a step requires data that cannot be produced by this project."""
