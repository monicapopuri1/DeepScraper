from typing import Optional
from urllib.parse import urlparse, urlunparse, urljoin, parse_qs, urlencode


TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source",
}


def normalize_url(url: str) -> str:
    """Normalize a URL by removing fragments, tracking params, and trailing slashes."""
    parsed = urlparse(url)
    # Remove fragment
    # Remove tracking query params
    if parsed.query:
        params = parse_qs(parsed.query, keep_blank_values=True)
        filtered = {k: v for k, v in params.items() if k.lower() not in TRACKING_PARAMS}
        query = urlencode(filtered, doseq=True)
    else:
        query = ""
    # Remove trailing slash from path (but keep root "/")
    path = parsed.path.rstrip("/") or "/"
    normalized = urlunparse((
        parsed.scheme.lower(),
        parsed.netloc.lower(),
        path,
        parsed.params,
        query,
        "",  # no fragment
    ))
    return normalized


def extract_domain(url: str) -> str:
    """Extract the registrable domain from a URL (e.g., 'www.stanford.edu' → 'stanford.edu')."""
    hostname = urlparse(url).netloc.lower()
    # Strip port
    hostname = hostname.split(":")[0]
    # Remove www prefix
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def is_same_domain(url: str, base_url: str) -> bool:
    """Check if url belongs to the same domain (including subdomains) as base_url."""
    base_domain = extract_domain(base_url)
    url_hostname = urlparse(url).netloc.lower().split(":")[0]
    return url_hostname == base_domain or url_hostname.endswith("." + base_domain)


def resolve_url(base: str, href: str) -> Optional[str]:
    """Resolve a relative URL against a base. Returns None for non-HTTP schemes."""
    try:
        absolute = urljoin(base, href)
        parsed = urlparse(absolute)
        if parsed.scheme in ("http", "https"):
            return absolute
        return None
    except Exception:
        return None


def is_valid_url(url: str) -> bool:
    """Check if a string looks like a valid HTTP(S) URL."""
    try:
        parsed = urlparse(url)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


SKIP_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".rar",
    ".jpg", ".jpeg", ".png", ".gif", ".svg", ".ico", ".webp",
    ".mp3", ".mp4", ".avi", ".mov", ".wmv",
    ".css", ".js", ".json", ".xml", ".rss",
}


def should_skip_url(url: str) -> bool:
    """Check if a URL points to a non-HTML resource based on extension."""
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in SKIP_EXTENSIONS)
