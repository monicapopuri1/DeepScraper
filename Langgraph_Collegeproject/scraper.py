import re
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
import urllib3
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

_COURSE_KEYWORDS = re.compile(
    r"course|programme|program|academic|department|faculty|"
    r"degree|curriculum|study|studies|admission|school|ug|pg|"
    r"undergrad|postgrad|master|bachelor|doctoral|phd|"
    r"nursing|medical|medicine|pharmacy|dental|health|paramedical|"
    r"physiotherapy|occupational|allied|science|arts|commerce|law|"
    r"engineering|technology|management|education|architecture",
    re.IGNORECASE,
)

_CONTACT_KEYWORDS = re.compile(
    r"contact|reach.?us|connect|get.?in.?touch|enquir|inquir|"
    r"about.?us|location|address|office|helpdesk|support|phone|email",
    re.IGNORECASE,
)


def _fetch_soup(url: str, timeout: int = 10):
    """Return (BeautifulSoup, response_url, error_string)."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, verify=False)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "lxml"), resp.url, None
    except requests.exceptions.Timeout:
        return None, url, f"Timeout after {timeout}s"
    except requests.exceptions.ConnectionError as e:
        return None, url, f"Connection error: {str(e)}"
    except requests.exceptions.HTTPError as e:
        return None, url, f"HTTP error: {e.response.status_code}"
    except Exception as e:
        return None, url, f"Unexpected error: {str(e)}"


def _extract_text(soup: BeautifulSoup, max_chars: int) -> str:
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    return " ".join(text.split())[:max_chars]


# File extensions that are never HTML pages — skip these as crawl targets
_NON_HTML_EXTENSIONS = re.compile(
    r"\.(jpg|jpeg|png|gif|webp|svg|ico|pdf|doc|docx|xls|xlsx|"
    r"ppt|pptx|zip|rar|mp4|mp3|avi|mov|css|js|xml|json)$",
    re.IGNORECASE,
)


def _extract_links(soup: BeautifulSoup, base_url: str, pattern: re.Pattern,
                   max_links: int, exclude: set[str] | None = None) -> list[str]:
    """Return up to max_links internal HTML links whose href or text matches pattern."""
    netloc = urlparse(base_url).netloc
    seen = exclude or set()
    candidates: list[tuple[int, str]] = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        abs_url = urljoin(base_url, href)
        parsed = urlparse(abs_url)

        if parsed.netloc != netloc:
            continue
        if abs_url in seen:
            continue
        # Skip non-HTML files (images, PDFs, documents, etc.)
        if _NON_HTML_EXTENSIONS.search(parsed.path):
            continue

        seen.add(abs_url)

        combined = href + " " + a.get_text(strip=True)
        matches = pattern.findall(combined)
        if matches:
            candidates.append((len(matches), abs_url))

    candidates.sort(reverse=True)
    return [u for _, u in candidates[:max_links]]


# Common contact-page paths found on Indian university websites.
# Tried as a fallback when the link scanner finds nothing.
_CONTACT_FALLBACK_PATHS = [
    "/contact-us",
    "/contact",
    "/contactus",
    "/contact.php",
    "/contact-us.php",
    "/about/contact",
    "/about-us/contact",
    "/about/contact-us",
    "/reach-us",
    "/enquiry",
    "/get-in-touch",
    "/campus/contact",
    "/university/contact",
]


def _probe_url(url: str, timeout: int = 4) -> bool:
    """Return True if the URL responds with a non-4xx/5xx status."""
    try:
        r = requests.head(url, headers=HEADERS, timeout=timeout, allow_redirects=True, verify=False)
        return r.status_code < 400
    except Exception:
        return False


def guess_contact_urls(base_url: str, already_found: list[str], max_guesses: int = 2) -> list[str]:
    """
    Probe all common contact-page paths in parallel (thread pool) and return
    up to max_guesses that actually respond. Parallel probing means worst-case
    wait is one timeout period, not timeout × number_of_paths.
    """
    parsed = urlparse(base_url)
    base = f"{parsed.scheme}://{parsed.netloc}"
    found_set = set(already_found)

    candidates = [base + p for p in _CONTACT_FALLBACK_PATHS if base + p not in found_set]
    if not candidates:
        return []

    found: list[str] = []
    with ThreadPoolExecutor(max_workers=len(candidates)) as pool:
        future_to_url = {pool.submit(_probe_url, url): url for url in candidates}
        for future in as_completed(future_to_url):
            url = future_to_url[future]
            try:
                if future.result():
                    found.append(url)
            except Exception:
                pass
            if len(found) >= max_guesses:
                break

    return found[:max_guesses]


def _build_course_pattern(courses: list[str]) -> re.Pattern | None:
    """
    Build a regex from the meaningful words inside the requested course names.
    e.g. ["B.Sc Psychology", "M.Sc Chemistry"] →  pattern matching
         "psychology|psycholog|psych|chemistry|chemical"
    This finds department/school pages specific to the requested courses,
    not just generic programme-listing pages.
    """
    # Short words and degree abbreviations that appear everywhere — skip them
    NOISE = {
        "in", "of", "and", "the", "for", "with", "to", "a", "an",
        "bsc", "msc", "btech", "mtech", "ba", "ma", "mba", "phd",
        "bca", "mca", "bed", "med", "llb", "llm", "bcom", "mcom",
        "bachelor", "master", "science", "arts", "commerce", "technology",
        "engineering", "degree", "diploma",
    }
    words: set[str] = set()
    for course in courses:
        for token in re.split(r"[\s,./()\-]+", course.lower()):
            clean = token.strip(".")
            if len(clean) > 3 and clean not in NOISE:
                words.add(re.escape(clean))
    if not words:
        return None
    return re.compile("|".join(sorted(words, key=len, reverse=True)), re.IGNORECASE)


def fetch_page_and_links(
    url: str,
    courses: list[str] | None = None,
    timeout: int = 10,
    max_chars: int = 6000,
    max_course_links: int = 3,
    max_contact_links: int = 2,
) -> tuple[str, list[str], list[str], str | None]:
    """
    Single HTTP request to `url`. Returns:
      (page_text, course_links, contact_links, error)
    course_links  – internal URLs relevant to the requested courses
                    (course-specific links first, then generic programme links)
    contact_links – internal URLs likely showing contact details
    Falls back to guessing common contact-page paths if scanner finds nothing.
    """
    soup, final_url, error = _fetch_soup(url, timeout)
    if error:
        return "", [], [], error

    text = _extract_text(soup, max_chars)
    seen: set[str] = {final_url, url}

    # 1. Course-specific links: pages whose URL or link text contains words
    #    from the actual course names the user is searching for.
    course_specific: list[str] = []
    course_pattern = _build_course_pattern(courses) if courses else None
    if course_pattern:
        course_specific = _extract_links(
            soup, final_url, course_pattern, max_course_links, seen.copy()
        )
        seen.update(course_specific)

    # 2. Generic programme/department links to fill any remaining slots
    remaining_slots = max_course_links - len(course_specific)
    generic_links: list[str] = []
    if remaining_slots > 0:
        generic_links = _extract_links(
            soup, final_url, _COURSE_KEYWORDS, remaining_slots, seen.copy()
        )
        seen.update(generic_links)

    course_links = course_specific + generic_links

    contact_links = _extract_links(soup, final_url, _CONTACT_KEYWORDS, max_contact_links, seen.copy())

    # Fallback: probe common paths when scanner found fewer than max_contact_links
    if len(contact_links) < max_contact_links:
        guessed = guess_contact_urls(final_url, contact_links, max_guesses=max_contact_links - len(contact_links))
        contact_links.extend(guessed)

    return text, course_links, contact_links, None


def fetch_page_text(url: str, timeout: int = 10, max_chars: int = 3000) -> tuple[str, str | None]:
    """Fetch a sub-page and return (cleaned_text, error). Used by crawl_subpages."""
    soup, _, error = _fetch_soup(url, timeout)
    if error:
        return "", error
    return _extract_text(soup, max_chars), None
