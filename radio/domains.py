"""Radio provider hostname matching."""

from urllib.parse import urlsplit


def is_somafm_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and (
        host == "somafm.com" or host.endswith(".somafm.com")
    )
