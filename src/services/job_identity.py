from __future__ import annotations

from urllib.parse import unquote, urlsplit, urlunsplit


def normalize_job_url(url: str | None) -> str | None:
    if not url:
        return None

    raw = url.strip()
    if not raw:
        return None

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    netloc = parts.netloc.lower()
    path = unquote(parts.path or "").strip() or "/"

    if path != "/":
        path = path.rstrip("/")
    while "//" in path:
        path = path.replace("//", "/")

    return urlunsplit((scheme, netloc, path, "", ""))
