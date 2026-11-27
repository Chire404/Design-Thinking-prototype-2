"""Business logic service layer for VeloPass."""


class AppError(Exception):
    """Structured domain exception mapped cleanly to JSON API responses."""

    def __init__(self, code: str, message: str, http_status: int = 400, details: dict | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.details = details or {}
