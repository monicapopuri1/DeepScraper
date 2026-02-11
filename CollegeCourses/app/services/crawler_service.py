import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from app.utils.url_utils import (
    is_same_domain,
    normalize_url,
    resolve_url,
    should_skip_url,
)

logger = logging.getLogger(__name__)

USER_AGENT = "CollegeCrawler/1.0 (+https://github.com/college-crawler)"


@dataclass
class CrawledPage:
    url: str
    text: str
    title: str = ""
    depth: int = 0
    links: List[str] = field(default_factory=list)


@dataclass
class CrawlResult:
    pages: List[CrawledPage] = field(default_factory=list)
    pages_crawled: int = 0
    max_depth_reached: int = 0
    errors: List[Dict] = field(default_factory=list)


class CrawlerService:
    """Async BFS web crawler with same-domain filtering and rate limiting."""

    def __init__(
        self,
        max_depth: int = 3,
        max_pages: int = 100,
        timeout_seconds: int = 120,
        concurrency: int = 10,
        delay: float = 0.25,
    ):
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.timeout_seconds = timeout_seconds
        self.concurrency = concurrency
        self.delay = delay

    async def crawl(self, start_url: str) -> CrawlResult:
        """BFS crawl starting from start_url, staying on the same domain."""
        result = CrawlResult()
        visited: Set[str] = set()
        semaphore = asyncio.Semaphore(self.concurrency)
        start_time = time.time()

        # Check robots.txt
        robots_allowed = await self._check_robots(start_url)
        if not robots_allowed:
            result.errors.append({
                "url": start_url,
                "error": "Blocked by robots.txt",
                "stage": "crawl",
            })
            return result

        queue: asyncio.Queue = asyncio.Queue()
        normalized_start = normalize_url(start_url)
        queue.put_nowait((normalized_start, 0))
        visited.add(normalized_start)

        async with httpx.AsyncClient(
            timeout=10.0,
            follow_redirects=True,
            headers={"User-Agent": USER_AGENT},
            limits=httpx.Limits(max_connections=self.concurrency),
        ) as client:
            while not queue.empty() and result.pages_crawled < self.max_pages:
                # Check total timeout
                elapsed = time.time() - start_time
                if elapsed >= self.timeout_seconds:
                    logger.warning("Crawl timeout reached")
                    break

                # Gather a batch of URLs from the queue
                batch: List[Tuple[str, int]] = []
                while not queue.empty() and len(batch) < self.concurrency:
                    batch.append(queue.get_nowait())

                # Build tasks list
                remaining = self.max_pages - result.pages_crawled
                tasks = []
                for url, depth in batch[:remaining]:
                    tasks.append(self._fetch_page(client, url, depth, semaphore))

                pages = await asyncio.gather(*tasks, return_exceptions=True)

                for page_or_error in pages:
                    if isinstance(page_or_error, Exception):
                        result.errors.append({
                            "url": "unknown",
                            "error": str(page_or_error),
                            "stage": "crawl",
                        })
                        continue
                    if page_or_error is None:
                        continue

                    page = page_or_error
                    result.pages.append(page)
                    result.pages_crawled += 1
                    result.max_depth_reached = max(result.max_depth_reached, page.depth)

                    # Enqueue discovered links
                    if page.depth < self.max_depth:
                        for link in page.links:
                            if should_skip_url(link):
                                continue
                            if not is_same_domain(link, start_url):
                                continue
                            normalized = normalize_url(link)
                            if normalized not in visited:
                                visited.add(normalized)
                                queue.put_nowait((normalized, page.depth + 1))

        return result

    async def _fetch_page(
        self,
        client: httpx.AsyncClient,
        url: str,
        depth: int,
        semaphore: asyncio.Semaphore,
    ) -> Optional[CrawledPage]:
        """Fetch and parse a single page."""
        async with semaphore:
            try:
                # Rate limiting
                await asyncio.sleep(self.delay)

                response = await client.get(url)
                content_type = response.headers.get("content-type", "")
                if "text/html" not in content_type:
                    return None

                response.raise_for_status()
                html = response.text

                soup = BeautifulSoup(html, "lxml")

                # Remove script/style elements
                for tag in soup(["script", "style", "noscript"]):
                    tag.decompose()

                text = soup.get_text(separator=" ", strip=True)
                title = soup.title.string.strip() if soup.title and soup.title.string else ""

                # Extract links
                links = []
                for a_tag in soup.find_all("a", href=True):
                    resolved = resolve_url(url, a_tag["href"])
                    if resolved:
                        links.append(resolved)

                return CrawledPage(
                    url=url,
                    text=text,
                    title=title,
                    depth=depth,
                    links=links,
                )

            except httpx.HTTPStatusError as e:
                logger.debug(f"HTTP error for {url}: {e.response.status_code}")
                return None
            except Exception as e:
                logger.debug(f"Failed to fetch {url}: {e}")
                return None

    async def _check_robots(self, url: str) -> bool:
        """Check if we're allowed to crawl the given URL per robots.txt."""
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"

            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(robots_url)
                if resp.status_code != 200:
                    return True  # No robots.txt = allowed

                rp = RobotFileParser()
                rp.parse(resp.text.splitlines())
                return rp.can_fetch(USER_AGENT, url)
        except Exception:
            return True  # If we can't read robots.txt, assume allowed
