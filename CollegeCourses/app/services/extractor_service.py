import csv
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.services.crawler_service import CrawledPage
from app.utils.text_utils import clean_text, extract_college_names, extract_context

logger = logging.getLogger(__name__)


@dataclass
class CollegeMatch:
    name: str
    url: Optional[str] = None
    city: Optional[str] = None
    state: Optional[str] = None
    confidence: float = 0.5
    source_pages: List[str] = field(default_factory=list)
    mention_count: int = 0
    context_snippets: List[str] = field(default_factory=list)


class ExtractorService:
    """Extract college/university mentions from crawled pages using regex + reference DB."""

    def __init__(self):
        self._reference_db: Optional[Dict[str, dict]] = None

    def _load_reference_db(self) -> Dict[str, dict]:
        """Load the colleges reference CSV into a lookup dict keyed by lowercase name."""
        if self._reference_db is not None:
            return self._reference_db

        self._reference_db = {}
        csv_path = Path(__file__).parent.parent / "data" / "colleges_reference.csv"

        if not csv_path.exists():
            logger.warning(f"Reference CSV not found at {csv_path}")
            return self._reference_db

        try:
            with open(csv_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    name = row.get("INSTNM", "").strip()
                    if name:
                        self._reference_db[name.lower()] = {
                            "name": name,
                            "url": row.get("INSTURL", ""),
                            "city": row.get("CITY", ""),
                            "state": row.get("STABBR", ""),
                            "unitid": row.get("UNITID", ""),
                        }
        except Exception as e:
            logger.error(f"Failed to load reference CSV: {e}")

        logger.info(f"Loaded {len(self._reference_db)} colleges from reference DB")
        return self._reference_db

    def extract_from_pages(
        self, pages: List[CrawledPage], source_college_name: Optional[str] = None
    ) -> Dict[str, CollegeMatch]:
        """
        Extract all college mentions from crawled pages.
        Returns a dict of unique colleges keyed by lowercase name.
        """
        ref_db = self._load_reference_db()
        colleges: Dict[str, CollegeMatch] = {}

        for page in pages:
            text = clean_text(page.text)
            candidates = extract_college_names(text)

            for candidate in candidates:
                # Skip the source college itself
                if source_college_name and candidate.lower() == source_college_name.lower():
                    continue

                # Determine confidence based on reference DB match
                confidence, db_data = self._match_against_db(candidate, ref_db)

                # Use canonical DB name as key for dedup when matched
                key = db_data["name"].lower() if db_data else candidate.lower()

                # Also skip source college by canonical name
                if source_college_name and key == source_college_name.lower():
                    continue

                if key in colleges:
                    # Update existing
                    match = colleges[key]
                    match.mention_count += 1
                    if page.url not in match.source_pages:
                        match.source_pages.append(page.url)
                    # Update confidence to highest seen
                    match.confidence = max(match.confidence, confidence)
                else:
                    # Create new match
                    match = CollegeMatch(
                        name=db_data["name"] if db_data else candidate,
                        url=self._format_url(db_data.get("url", "")) if db_data else None,
                        city=db_data.get("city") if db_data else None,
                        state=db_data.get("state") if db_data else None,
                        confidence=confidence,
                        source_pages=[page.url],
                        mention_count=1,
                    )
                    colleges[key] = match

                # Extract context snippet
                snippet = extract_context(text, candidate)
                if snippet and len(match.context_snippets) < 3:
                    match.context_snippets.append(snippet)

        return colleges

    def _match_against_db(
        self, candidate: str, ref_db: Dict[str, dict]
    ) -> Tuple[float, Optional[dict]]:
        """Match a candidate name against the reference database."""
        key = candidate.lower()

        # Exact match
        if key in ref_db:
            return 0.95, ref_db[key]

        # Substring match: check if candidate is contained in any DB name or vice versa
        for db_key, db_data in ref_db.items():
            if key in db_key or db_key in key:
                return 0.75, db_data

        # Regex-only match (no DB validation)
        return 0.5, None

    def search_colleges(self, query: str, limit: int = 20) -> List[dict]:
        """Search the reference database by name (for the /colleges/search endpoint)."""
        ref_db = self._load_reference_db()
        query_lower = query.lower()
        results = []

        for key, data in ref_db.items():
            if query_lower in key:
                results.append(data)
                if len(results) >= limit:
                    break

        return results

    def _format_url(self, url: str) -> Optional[str]:
        """Ensure URL has a scheme."""
        if not url:
            return None
        if not url.startswith("http"):
            return f"https://{url}"
        return url
