"""
playwright_crawler.py — Playwright Deep Crawler for Course Lookup

Crawls any website with Playwright (Chromium headless), stores every
rendered page in SQLite, then searches all stored pages for a target course.

Usage:
    pip install playwright anthropic
    playwright install chromium

    # Default (SIU):
    python playwright_crawler.py

    # Custom URL and course:
    python playwright_crawler.py --url https://collegedunia.com/ --course "MBA"
    python playwright_crawler.py --url https://www.iitb.ac.in/ --course "M.Tech. Computer Science"

    # Full options:
    python playwright_crawler.py --url URL --course COURSE [--max-pages N] [--max-depth N] [--db FILE]
"""

import argparse
import asyncio
import sqlite3
import re
import os
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import anthropic
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_PAGES = 100
MAX_DEPTH = 4
RATE_LIMIT_SECONDS = 1.0

# URL fragments that signal course/programme relevance — get higher BFS priority
PRIORITY_KEYWORDS = re.compile(
    r"programme|course|engineering|mtech|m\.tech|pg|postgraduate|school|institute|department|faculty|admission|"
    r"nursing|medical|medicine|pharmacy|dental|health|paramedical|physiotherapy|occupational|allied|"
    r"science|arts|commerce|law|management|education|architecture|technology",
    re.IGNORECASE,
)

# URL fragments to skip — pages that are irrelevant to course listings
# (doctor/staff directories, news, events, search results, galleries, etc.)
SKIP_BULK = re.compile(
    r"/faculty/|/news/|/event/|/gallery/|/blog|/alumni/"
    r"|/doctor|/physician|/staff|/search|/find-a|/directory|/profile/",
    re.IGNORECASE,
)

# Extensions to skip
SKIP_EXTENSIONS = re.compile(
    r"\.(pdf|jpg|jpeg|png|gif|svg|ico|css|js|zip|doc|docx|xls|xlsx|ppt|pptx|mp4|mp3|avi|mov)$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Config helpers (derived from CLI args)
# ---------------------------------------------------------------------------

def derive_domain(url: str) -> str:
    """Extract the registrable domain from a URL, e.g. 'https://www.siu.edu.in/' -> 'siu.edu.in'."""
    netloc = urlparse(url).netloc.lower()
    netloc = netloc.lstrip("www.")
    return netloc


def derive_db_path(url: str) -> str:
    """Turn a URL into a safe SQLite filename, e.g. 'siu.edu.in' -> 'crawl_siu_edu_in.db'."""
    domain = derive_domain(url)
    safe = re.sub(r"[^\w]", "_", domain)
    return f"crawl_{safe}.db"


# Common abbreviations used in Indian university course names → full forms
_ABBREV_MAP = {
    "AI":   "Artificial Intelligence",
    "ML":   "Machine Learning",
    "AIML": "Artificial Intelligence and Machine Learning",
    "DS":   "Data Science",
    "CS":   "Computer Science",
    "IT":   "Information Technology",
    "IoT":  "Internet of Things",
    "CV":   "Computer Vision",
    "NLP":  "Natural Language Processing",
    "VR":   "Virtual Reality",
    "AR":   "Augmented Reality",
    "EV":   "Electric Vehicle",
    "VLSI": "Very Large Scale Integration",
}


def generate_variants(course_name: str) -> list[str]:
    """
    Generate search variants for the given course name.
    Handles punctuation/spacing alternates AND expands common abbreviations
    (e.g. "AI" → "Artificial Intelligence") so SQLite LIKE searches don't miss
    pages that spell out the full form.
    """
    def _variants_of(name: str) -> list[str]:
        vs = [name]
        # . vs no dot:  "M.Tech." <-> "M.Tech"
        if "." in name:
            vs.append(name.replace(".", ""))
        # & <-> and
        if " and " in name:
            vs.append(name.replace(" and ", " & "))
        if " & " in name:
            vs.append(name.replace(" & ", " and "))
        return vs

    candidates = _variants_of(course_name)

    # Expand abbreviations one at a time, then expand all together.
    # Start from the original name and iteratively replace each abbreviation.
    fully_expanded = course_name
    for abbrev, full_form in _ABBREV_MAP.items():
        pattern = re.compile(r"\b" + re.escape(abbrev) + r"\b", re.IGNORECASE)
        if pattern.search(course_name):
            # Single-abbrev expansion (e.g. only AI expanded)
            single = pattern.sub(full_form, course_name)
            candidates.extend(_variants_of(single))
            candidates.append(full_form)
            # Build fully-expanded version (all abbrevs replaced)
            fully_expanded = pattern.sub(full_form, fully_expanded)

    # Add the version where every abbreviation is expanded simultaneously
    if fully_expanded != course_name:
        candidates.extend(_variants_of(fully_expanded))

    # Remove duplicates while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for v in candidates:
        if v not in seen:
            seen.add(v)
            unique.append(v)
    return unique


# ---------------------------------------------------------------------------
# CrawlerDB — thin SQLite wrapper
# ---------------------------------------------------------------------------

class CrawlerDB:
    """SQLite wrapper storing crawled page text."""

    def __init__(self, db_path: str = "crawl.db"):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self._init_schema()

    def _init_schema(self):
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                url          TEXT PRIMARY KEY,
                text_content TEXT,
                depth        INTEGER,
                crawled_at   TEXT
            )
        """)
        self.conn.commit()

    def insert_page(self, url: str, text_content: str, depth: int):
        now = datetime.now(timezone.utc).isoformat()
        self.conn.execute(
            "INSERT OR REPLACE INTO pages (url, text_content, depth, crawled_at) VALUES (?, ?, ?, ?)",
            (url, text_content, depth, now),
        )
        self.conn.commit()

    def url_exists(self, url: str) -> bool:
        row = self.conn.execute("SELECT 1 FROM pages WHERE url = ?", (url,)).fetchone()
        return row is not None

    def count_pages(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]

    def search_variant(self, variant: str):
        """
        Return (url, text_content) rows where variant appears as a complete phrase,
        not embedded inside another word (e.g. 'MBA' must not match 'Marimba').
        SQLite LIKE is used as a fast pre-filter; Python regex with word-boundary
        lookarounds is then applied to remove false positives.
        """
        like_pattern = f"%{variant}%"
        candidates = self.conn.execute(
            "SELECT url, text_content FROM pages WHERE text_content LIKE ? COLLATE NOCASE",
            (like_pattern,),
        ).fetchall()
        # Post-filter: require variant to be surrounded by non-word characters
        pattern = re.compile(r'(?<!\w)' + re.escape(variant) + r'(?!\w)', re.IGNORECASE)
        return [(url, text) for url, text in candidates if pattern.search(text)]

    def close(self):
        self.conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_internal_url(url: str, allowed_domains: tuple[str, ...]) -> bool:
    """Returns True if the URL's domain matches any of the allowed domains."""
    parsed = urlparse(url)
    netloc = parsed.netloc.lower().lstrip("www.")
    return any(netloc == d or netloc.endswith("." + d) for d in allowed_domains)


def should_skip(url: str) -> bool:
    """Returns True for PDFs, images, fragments, mailto, javascript links."""
    if not url or url.startswith(("mailto:", "tel:", "javascript:", "#")):
        return True
    parsed = urlparse(url)
    if SKIP_EXTENSIONS.search(parsed.path):
        return True
    return False


def priority_score(url: str) -> int:
    """Lower score = higher BFS priority. Programme URLs get 0, others get 1."""
    return 0 if PRIORITY_KEYWORDS.search(url) else 1


def extract_text(html: str) -> str:
    """Strip HTML tags and collapse whitespace."""
    soup = BeautifulSoup(html, "lxml")
    # Remove script/style noise
    for tag in soup(["script", "style", "noscript", "header", "footer", "nav"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)
    # Collapse multiple spaces/newlines
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_links(html: str, base_url: str, allowed_domains: tuple[str, ...]) -> list[str]:
    """Extract all <a href> links resolved against base_url, filtered to allowed_domains."""
    soup = BeautifulSoup(html, "lxml")
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(base_url, href)
        # Strip fragment
        full = full.split("#")[0]
        if full and not should_skip(full) and is_internal_url(full, allowed_domains):
            links.append(full)
    return links


def parse_sitemap(xml_text: str, base_url: str) -> list[str]:
    """Parse sitemap XML and return all <loc> URLs."""
    soup = BeautifulSoup(xml_text, "xml")
    urls = [loc.text.strip() for loc in soup.find_all("loc")]
    # Also handle sitemap index — return nested sitemap URLs too
    return [u for u in urls if u.startswith("http")]


# ---------------------------------------------------------------------------
# PlaywrightCrawler
# ---------------------------------------------------------------------------

class PlaywrightCrawler:
    """BFS crawler using Playwright Chromium headless."""

    def __init__(
        self,
        db: CrawlerDB,
        allowed_domains: tuple[str, ...],
        priority_seeds: list[str] | None = None,
        max_pages: int = MAX_PAGES,
        max_depth: int = MAX_DEPTH,
        searcher=None,
        sitemap_only: bool = False,
        priority_url_keywords=None,
        skip_url_patterns=None,
    ):
        self.db = db
        self.allowed_domains = allowed_domains
        self.priority_seeds = priority_seeds or []
        self.max_pages = max_pages
        self.max_depth = max_depth
        self.searcher = searcher  # CourseSearcher instance for early-exit checks
        self.sitemap_only = sitemap_only
        self.visited: set[str] = set()
        self.priority_url_keywords = priority_url_keywords or PRIORITY_KEYWORDS
        self.skip_url_patterns = skip_url_patterns or SKIP_BULK

    # -------------------------------------------------------- sitemap-only crawl

    async def crawl_sitemap_only(self, start_url: str) -> dict | None:
        """
        Fast path: fetch sitemap, filter URLs by course/programme keywords,
        crawl only those pages — no BFS, no rate-limit sleep.
        Typical runtime: 15-40 seconds instead of 4+ minutes.
        """
        import time
        t0 = time.time()
        print(f"[FAST] Sitemap-only mode — skipping BFS crawl.")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                ignore_https_errors=True,
            )
            page = await context.new_page()

            # Step 1 — get sitemap URLs
            sitemap_urls = await self._probe_sitemaps(page, start_url)
            if not sitemap_urls:
                print("[FAST] No sitemap found — falling back to BFS crawl.")
                await browser.close()
                return await self.crawl(start_url)

            # Step 2 — filter: keep only programme-relevant URLs, drop bulk noise
            # Also allow subdomains of the root domain (e.g. set.jainuniversity.ac.in)
            root_domain = self.allowed_domains[0]

            # Build a course-specific word pattern from the searcher's variants
            # so URLs like /nursing/ are kept when searching for B.Sc Nursing,
            # /pharmacy/ for B.Pharm, etc. — works for any domain, not just medical.
            course_url_pattern = None
            if self.searcher:
                course_words = set()
                for v in self.searcher.variants:
                    for token in re.split(r"[\s,./()\-]+", v.lower()):
                        clean = token.strip(".")
                        if len(clean) > 3:
                            course_words.add(re.escape(clean))
                if course_words:
                    course_url_pattern = re.compile(
                        "|".join(sorted(course_words, key=len, reverse=True)),
                        re.IGNORECASE,
                    )

            def _relevant(u):
                if self.skip_url_patterns.search(u):
                    return False
                if root_domain not in urlparse(u).netloc:
                    return False
                return bool(
                    self.priority_url_keywords.search(u)
                    or (course_url_pattern and course_url_pattern.search(u))
                )

            filtered = [u for u in sitemap_urls if _relevant(u)]
            # Remove already-visited
            filtered = [u for u in filtered if not self.db.url_exists(u)]
            # Deduplicate
            filtered = list(dict.fromkeys(filtered))

            print(f"[FAST] {len(sitemap_urls)} sitemap URLs → {len(filtered)} after filtering")
            if not filtered:
                print("[FAST] No programme URLs found in sitemap. Try BFS mode.")
                await browser.close()
                return None

            # Step 3 — fetch only the filtered pages (up to max_pages)
            pages_crawled = 0
            early_result = None

            for url in filtered[:self.max_pages]:
                html, text = await self._fetch_page(page, url)
                if html is None:
                    continue

                pages_crawled += 1
                print(f"[FAST] Page {pages_crawled}/{min(len(filtered), self.max_pages)}: {url} — {len(text)} chars")
                self.db.insert_page(url, text, depth=1)

                # Early-exit check
                if self.searcher is not None:
                    result = self.searcher.check_page(url, text)
                    if result is not None and result.get("llm_verdict") in ("FOUND", "POSSIBLY_FOUND"):
                        print(f"[FAST] Match found after {pages_crawled} pages ({time.time()-t0:.1f}s).")
                        early_result = result
                        break

            await browser.close()

        elapsed = time.time() - t0
        total = self.db.count_pages()
        print(f"[FAST] Done. {pages_crawled} pages fetched in {elapsed:.1f}s — {total} total in DB.")
        return early_result

    # ------------------------------------------------------------------ crawl

    async def crawl(self, start_url: str) -> dict | None:
        """
        BFS crawl. Returns an early result dict if the course is found during
        crawling, otherwise returns None (caller should run post-crawl search).
        """
        print(f"[CRAWL] Starting BFS crawl of {start_url} ...")

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 800},
                ignore_https_errors=True,
            )
            page = await context.new_page()

            # Phase 1 — Sitemap probe
            sitemap_urls = await self._probe_sitemaps(page, start_url)
            if sitemap_urls:
                print(f"[CRAWL] Sitemap found: {len(sitemap_urls)} URLs pre-loaded")

            # Build initial BFS queue: (priority, depth, url)
            # Use a list sorted by priority so lower-priority items go last
            queue: deque = deque()

            def enqueue(url: str, depth: int):
                if url not in self.visited and not self.db.url_exists(url):
                    self.visited.add(url)
                    score = 0 if self.priority_url_keywords.search(url) else 1
                    queue.append((score, depth, url))

            # Seed queue — priority seeds first (highest priority)
            for seed in self.priority_seeds:
                enqueue(seed, 1)
            enqueue(start_url, 0)

            # Add sitemap URLs filtered by domain and keyword (depth=1)
            # Only add keyword-matching sitemap URLs — skip bulk non-relevant pages
            # (e.g. hundreds of /faculty/name-* pages that drown out course pages)
            programme_sitemap = [
                u for u in sitemap_urls
                if self.priority_url_keywords.search(u) and not self.skip_url_patterns.search(u)
            ]
            for u in programme_sitemap:
                enqueue(u, 1)

            pages_crawled = 0
            early_result = None

            while queue and pages_crawled < self.max_pages:
                # Sort queue so priority=0 items come first
                sorted_q = sorted(queue, key=lambda x: x[0])
                queue.clear()
                queue.extend(sorted_q)

                _, depth, url = queue.popleft()

                if depth > self.max_depth:
                    continue

                html, text = await self._fetch_page(page, url)
                if html is None:
                    continue

                pages_crawled += 1
                char_count = len(text)
                print(
                    f"[CRAWL] Page {pages_crawled}/{self.max_pages}: {url} "
                    f"(depth={depth}) — {char_count} chars"
                )

                self.db.insert_page(url, text, depth)

                # Early-exit check: scan this page immediately for a match
                if self.searcher is not None:
                    result = self.searcher.check_page(url, text)
                    if result is not None and result.get("llm_verdict") in ("FOUND", "POSSIBLY_FOUND"):
                        print(f"[CRAWL] Early exit — course found after {pages_crawled} pages.")
                        early_result = result
                        break

                # Discover links → enqueue
                if depth < self.max_depth:
                    for link in extract_links(html, url, self.allowed_domains):
                        enqueue(link, depth + 1)

                await asyncio.sleep(RATE_LIMIT_SECONDS)

            await browser.close()

        total = self.db.count_pages()
        db_size_mb = os.path.getsize(self.db.db_path) / (1024 * 1024)
        print(f"[CRAWL] Done. {total} pages stored in {self.db.db_path} ({db_size_mb:.1f} MB)")
        return early_result

    # ------------------------------------------------------- sitemap probing

    async def _probe_sitemaps(self, page, base_url: str) -> list[str]:
        """Try common sitemap paths, return all discovered URLs."""
        sitemap_candidates = [
            urljoin(base_url, "/sitemap.xml"),
            urljoin(base_url, "/sitemap_index.xml"),
            urljoin(base_url, "/sitemap"),
        ]
        all_urls: list[str] = []
        for sm_url in sitemap_candidates:
            try:
                response = await page.goto(sm_url, wait_until="domcontentloaded", timeout=15000)
                if response and response.status == 200:
                    content = await page.content()
                    urls = parse_sitemap(content, base_url)
                    if urls:
                        all_urls.extend(urls)
                        print(f"[CRAWL] Sitemap parsed: {sm_url} ({len(urls)} URLs)")
            except Exception:
                pass
        # Deduplicate
        return list(dict.fromkeys(all_urls))

    # ----------------------------------------------------------- page fetcher

    async def _fetch_page(self, page, url: str) -> tuple[str | None, str]:
        """Navigate to url, wait for networkidle, return (html, text)."""
        try:
            response = await page.goto(
                url,
                wait_until="networkidle",
                timeout=30000,
            )
            if response is None or response.status >= 400:
                return None, ""
            html = await page.content()
            text = extract_text(html)
            return html, text
        except PlaywrightTimeout:
            print(f"[CRAWL] Timeout: {url}")
            # Try with a shorter wait condition on timeout
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                html = await page.content()
                text = extract_text(html)
                return html, text
            except Exception:
                return None, ""
        except Exception as exc:
            print(f"[CRAWL] Error fetching {url}: {exc}")
            return None, ""


# ---------------------------------------------------------------------------
# Course Searcher + LLM Verification
# ---------------------------------------------------------------------------

class CourseSearcher:
    """Searches SQLite pages for course variants, verifies with Claude."""

    def __init__(self, db: CrawlerDB, variants: list[str]):
        self.db = db
        self.variants = variants
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else None

    @staticmethod
    def _whole_word_match(text: str, variant: str) -> bool:
        """
        Return True only if variant appears as a complete phrase in text —
        i.e. not embedded inside another word.
        e.g. 'MBA' matches ' MBA,' and '(MBA)' but NOT 'Marimba' or 'DMBA'.
        Uses negative lookbehind/lookahead so punctuation variants like
        'M.B.A.' also work correctly.
        """
        pattern = re.compile(r'(?<!\w)' + re.escape(variant) + r'(?!\w)', re.IGNORECASE)
        return bool(pattern.search(text))

    def check_page(self, url: str, text: str) -> dict | None:
        """
        Check a single page's text for any variant (in-memory, no DB query).
        Requires whole-word match — variant must not be part of another word.
        If a variant is found, immediately run LLM verification.
        Returns a result dict if matched, None if no match.
        """
        for variant in self.variants:
            if self._whole_word_match(text, variant):
                print(f"[EARLY] Match found on: {url} (variant: \"{variant}\")")
                return self._llm_verify(url, text, variant)
        return None

    def search_course(self, course_name: str) -> dict:
        """
        Search all stored pages for all name variants.
        Returns dict with found, confidence, evidence, source_url.
        """
        print(f'\n[SEARCH] Searching for: "{course_name}"')
        print(f"[SEARCH] Checking {len(self.variants)} name variants across {self.db.count_pages()} pages...")

        matches: list[dict] = []

        for variant in self.variants:
            rows = self.db.search_variant(variant)
            for url, text_content in rows:
                matches.append({
                    "url": url,
                    "variant": variant,
                    "text_snippet": self._extract_snippet(text_content, variant),
                    "text_content": text_content,
                })
                print(f"[SEARCH] Text match found on: {url} (variant: \"{variant}\")")

        if not matches:
            return {
                "found": False,
                "confidence": 0,
                "evidence": "No text matches found across any of the 8 name variants.",
                "source_url": None,
                "llm_verdict": "NOT_FOUND",
            }

        # Deduplicate by URL (keep first match per URL)
        seen_urls: set[str] = set()
        unique_matches = []
        for m in matches:
            if m["url"] not in seen_urls:
                seen_urls.add(m["url"])
                unique_matches.append(m)

        # LLM verification on top matches (up to 3 pages)
        best_result = None
        for match in unique_matches[:3]:
            result = self._llm_verify(match["url"], match["text_content"], match["variant"])
            if result["llm_verdict"] in ("FOUND", "POSSIBLY_FOUND"):
                best_result = result
                break

        if best_result is None:
            # Text match existed but LLM didn't confirm — use first match
            best_result = {
                "found": False,
                "confidence": 30,
                "evidence": unique_matches[0]["text_snippet"],
                "source_url": unique_matches[0]["url"],
                "llm_verdict": "POSSIBLY_FOUND",
            }

        return best_result

    def _extract_snippet(self, text: str, variant: str, window: int = 200) -> str:
        """Extract text around the matched variant."""
        idx = text.lower().find(variant.lower())
        if idx == -1:
            return text[:200]
        start = max(0, idx - window // 2)
        end = min(len(text), idx + len(variant) + window // 2)
        return "..." + text[start:end] + "..."

    def _llm_verify(self, url: str, text_content: str, matched_variant: str) -> dict:
        """Call Claude claude-sonnet-4-6 to verify course presence. Returns result dict."""
        if self.client is None:
            print("[LLM] ANTHROPIC_API_KEY not set — skipping LLM verification.")
            return {
                "found": True,
                "confidence": 50,
                "evidence": f'Text match found for variant: "{matched_variant}"',
                "source_url": url,
                "llm_verdict": "POSSIBLY_FOUND",
            }

        print(f"[LLM] Verifying with Claude claude-sonnet-4-6 on: {url}")

        # Truncate text to fit context (keep first 6000 chars — most relevant)
        truncated_text = text_content[:6000]
        if len(text_content) > 6000:
            truncated_text += "\n...[truncated]"

        variants_bullet = "\n".join(f"- {v}" for v in self.variants)
        prompt = f"""You are checking whether a university offers a specific course or programme.

Target: "{matched_variant}"

Also accept closely related name variants:
{variants_bullet}

The text below was extracted from the university's website at: {url}

Page text:
{truncated_text}

Answer with EXACTLY this format:
VERDICT: FOUND | NOT_FOUND | POSSIBLY_FOUND
EVIDENCE: <quote the exact text from the page that supports your verdict, or "none" if not found>
NOTES: <any clarifying notes, e.g. a similar but different course was found instead>
"""

        try:
            message = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=400,
                messages=[{"role": "user", "content": prompt}],
            )
            response_text = message.content[0].text.strip()

            # Parse response
            verdict = "NOT_FOUND"
            evidence = "No evidence extracted."
            notes = ""

            for line in response_text.splitlines():
                line = line.strip()
                if line.startswith("VERDICT:"):
                    raw = line.replace("VERDICT:", "").strip().upper()
                    if "FOUND" in raw and "NOT" not in raw:
                        verdict = "FOUND"
                    elif "POSSIBLY" in raw:
                        verdict = "POSSIBLY_FOUND"
                    else:
                        verdict = "NOT_FOUND"
                elif line.startswith("EVIDENCE:"):
                    evidence = line.replace("EVIDENCE:", "").strip()
                elif line.startswith("NOTES:"):
                    notes = line.replace("NOTES:", "").strip()

            confidence = {"FOUND": 80, "POSSIBLY_FOUND": 55, "NOT_FOUND": 0}.get(verdict, 0)

            if notes:
                evidence = f"{evidence} [Notes: {notes}]"

            return {
                "found": verdict in ("FOUND", "POSSIBLY_FOUND"),
                "confidence": confidence,
                "evidence": evidence,
                "source_url": url,
                "llm_verdict": verdict,
            }

        except Exception as exc:
            print(f"[LLM] Error calling Claude: {exc}")
            # LLM failed — do NOT claim the course is found; a text match alone is not enough
            return {
                "found": False,
                "confidence": 0,
                "evidence": f'Text match for "{matched_variant}" but LLM verification failed: {exc}',
                "source_url": url,
                "llm_verdict": "NOT_FOUND",
            }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

async def main():
    parser = argparse.ArgumentParser(
        description="Playwright deep crawler — check if a university offers a course."
    )
    parser.add_argument(
        "--url",
        default="https://www.siu.edu.in/",
        help="University website URL to crawl (default: https://www.siu.edu.in/)",
    )
    parser.add_argument(
        "--course",
        default="M.Tech. Electronics and Communication",
        help='Course name to search for (default: "M.Tech. Electronics and Communication")',
    )
    parser.add_argument(
        "--max-pages",
        type=int,
        default=MAX_PAGES,
        help=f"Max pages to crawl (default: {MAX_PAGES})",
    )
    parser.add_argument(
        "--max-depth",
        type=int,
        default=MAX_DEPTH,
        help=f"Max BFS depth (default: {MAX_DEPTH})",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="SQLite DB file path (default: auto-derived from URL)",
    )
    parser.add_argument(
        "--sitemap-only",
        action="store_true",
        default=False,
        help="Fast mode: skip BFS, fetch only sitemap-filtered programme URLs (15-40s vs 4+ min)",
    )
    args = parser.parse_args()

    start_url = args.url
    course_name = args.course
    db_path = args.db or derive_db_path(start_url)
    allowed_domains = (derive_domain(start_url),)
    variants = generate_variants(course_name)

    print(f"[CONFIG] URL:       {start_url}")
    print(f"[CONFIG] Course:    {course_name}")
    print(f"[CONFIG] Variants:  {variants}")
    print(f"[CONFIG] Domain:    {allowed_domains[0]}")
    print(f"[CONFIG] DB:        {db_path}")
    print(f"[CONFIG] Max pages: {args.max_pages}  Max depth: {args.max_depth}")
    print(f"[CONFIG] Mode:      {'sitemap-only (fast)' if args.sitemap_only else 'BFS crawl (thorough)'}")

    db = CrawlerDB(db_path)
    searcher = CourseSearcher(db, variants)
    crawler = PlaywrightCrawler(
        db,
        allowed_domains=allowed_domains,
        max_pages=args.max_pages,
        max_depth=args.max_depth,
        searcher=searcher,
        sitemap_only=args.sitemap_only,
    )

    # Crawl — returns early result if course found mid-crawl, else None
    if args.sitemap_only:
        early_result = await crawler.crawl_sitemap_only(start_url)
    else:
        early_result = await crawler.crawl(start_url)

    if early_result is not None:
        result = early_result
        print("[SEARCH] Skipping post-crawl search — course already found during crawl.")
    else:
        # Full post-crawl search across all stored pages
        result = searcher.search_course(course_name)

    # Print result table
    found_str = "YES" if result["found"] else "NO"
    confidence = result.get("confidence", 0)
    evidence = result.get("evidence", "N/A")
    source_url = result.get("source_url", "N/A")
    llm_verdict = result.get("llm_verdict", "N/A")

    print("\n" + "=" * 55)
    print("RESULT")
    print("=" * 55)
    print(f"University:           {start_url}")
    print(f"Course searched:      {course_name}")
    print(f"Course Found:         {found_str}")
    print(f"LLM Verdict:          {llm_verdict}")
    print(f"Confidence:           {confidence}%")
    print(f"Evidence:             {evidence[:200]}")
    print(f"Source URL:           {source_url}")
    print("=" * 55)
    print(f"\nInspect all crawled pages: sqlite3 {db_path} 'SELECT url FROM pages;'")

    db.close()


if __name__ == "__main__":
    asyncio.run(main())
