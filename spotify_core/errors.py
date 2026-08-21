"""Error taxonomy for spotify_core.

Callers can catch SpotifyCoreError for anything raised by this package,
or narrow it down to RateLimitError / SpotifyAPIError / AuthError /
ConfigError as needed.
"""


class SpotifyCoreError(Exception):
    """Base class for all errors raised by spotify_core."""


class RateLimitError(SpotifyCoreError):
    """An endpoint category is rate-limited (long 429 lockout).

    ``category`` is the normalized endpoint category string and
    ``retry_until`` is a unix timestamp after which requests may resume.
    """

    def __init__(self, category, retry_until):
        self.category = category
        self.retry_until = retry_until
        super().__init__(f"{category} blocked until {retry_until}")


# Historical name kept so existing callers/patches keep working.
LongRateLimitBlock = RateLimitError


class SpotifyAPIError(SpotifyCoreError):
    """A Spotify API request failed after retries (or non-retryably)."""

    def __init__(self, status_code, message):
        self.status_code = status_code
        self.message = message
        super().__init__(message)


class AuthError(SpotifyCoreError):
    """OAuth token exchange/refresh failed."""


class ConfigError(SpotifyCoreError):
    """Configuration is missing or invalid."""


class NotFoundError(SpotifyCoreError):
    """A referenced entity (e.g. album) does not exist in state."""
