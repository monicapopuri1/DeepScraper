import logging
from typing import List, Optional, Tuple

from duckduckgo_search import DDGS

from app.utils.url_utils import is_valid_url

logger = logging.getLogger(__name__)

AGGREGATOR_DOMAINS = {
    "wikipedia.org", "en.wikipedia.org",
    "usnews.com", "niche.com", "unigo.com",
    "collegeboard.org", "petersons.com",
    "cappex.com", "collegesimply.com",
}


class SearchService:
    """Resolve a college name to its official website URL using DuckDuckGo search."""

    async def search_college_url(self, college_name: str) -> Tuple[Optional[str], float]:
        """
        Search for a college's official URL.
        Returns (url, confidence) tuple.
        """
        try:
            query = f"{college_name} official website"
            results = await self._search(query)
            if not results:
                return None, 0.0

            # Score and rank results
            best_url = None
            best_score = 0.0

            for result in results[:10]:
                href = result.get("href") or result.get("link", "")
                if not is_valid_url(href):
                    continue

                score = self._score_result(href, college_name, result)
                if score > best_score:
                    best_score = score
                    best_url = href

            if best_url:
                confidence = min(best_score, 1.0)
                logger.info(f"Resolved '{college_name}' → {best_url} (confidence: {confidence:.2f})")
                return best_url, confidence

            return None, 0.0

        except Exception as e:
            logger.error(f"Search failed for '{college_name}': {e}")
            return None, 0.0

    async def _search(self, query: str) -> List[dict]:
        """Perform a DuckDuckGo search (runs sync search in thread)."""
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=10))
            return results
        except Exception as e:
            logger.error(f"DuckDuckGo search error: {e}")
            return []

    def _score_result(self, url: str, college_name: str, result: dict) -> float:
        """Score a search result for relevance to the college."""
        score = 0.3  # base score for appearing in results

        url_lower = url.lower()

        # .edu domain is a strong signal
        if ".edu" in url_lower:
            score += 0.4

        # Penalize aggregator sites
        for domain in AGGREGATOR_DOMAINS:
            if domain in url_lower:
                score -= 0.3
                break

        # Check if college name words appear in URL
        name_words = college_name.lower().split()
        url_matches = sum(1 for w in name_words if w in url_lower and len(w) > 2)
        if name_words:
            score += 0.2 * (url_matches / len(name_words))

        # Check if it's a root page (not deep link)
        from urllib.parse import urlparse
        path = urlparse(url).path.strip("/")
        if not path:
            score += 0.1

        return score
