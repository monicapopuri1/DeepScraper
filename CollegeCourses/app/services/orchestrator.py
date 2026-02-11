import logging
import time
from typing import List, Optional, Tuple

from app.config import settings
from app.models.request import CrawlConfig, CrawlRequest, InputType
from app.models.response import (
    CollegeDetail,
    CrawlError,
    CrawlResponse,
    CrawlSummary,
    Location,
    RankingsData,
)
from app.services.crawler_service import CrawlerService
from app.services.enrichment_service import EnrichmentService
from app.services.extractor_service import ExtractorService
from app.services.search_service import SearchService
from app.utils.url_utils import is_valid_url

logger = logging.getLogger(__name__)


class Orchestrator:
    """Coordinates the full crawl pipeline: resolve URL → crawl → extract → enrich."""

    def __init__(self):
        self.search_service = SearchService()
        self.extractor_service = ExtractorService()
        self.enrichment_service = EnrichmentService()

    async def process(self, request: CrawlRequest) -> CrawlResponse:
        start_time = time.time()
        errors: List[CrawlError] = []
        college_name_hint: Optional[str] = None

        # Step 1: Resolve input to URL
        url, input_type = await self._resolve_input(request)

        if not url:
            return CrawlResponse(
                summary=CrawlSummary(
                    resolved_url="",
                    duration_seconds=time.time() - start_time,
                    status="failed",
                ),
                errors=[CrawlError(error=f"Could not resolve input: {request.input}", stage="resolve")],
            )

        if input_type == InputType.NAME:
            college_name_hint = request.input

        logger.info(f"Starting crawl of {url}")

        # Step 2: Crawl
        config = request.crawl_config
        crawler = CrawlerService(
            max_depth=config.max_depth,
            max_pages=config.max_pages,
            timeout_seconds=config.timeout_seconds,
            concurrency=settings.crawler_concurrency,
            delay=settings.crawler_delay,
        )

        crawl_result = await crawler.crawl(url)

        for err in crawl_result.errors:
            errors.append(CrawlError(
                url=err.get("url"),
                error=err.get("error", "Unknown error"),
                stage=err.get("stage", "crawl"),
            ))

        if crawl_result.pages_crawled == 0:
            return CrawlResponse(
                summary=CrawlSummary(
                    resolved_url=url,
                    pages_crawled=0,
                    duration_seconds=time.time() - start_time,
                    status="failed",
                ),
                errors=errors or [CrawlError(error="No pages could be crawled", stage="crawl")],
            )

        # Step 3: Extract colleges
        college_matches = self.extractor_service.extract_from_pages(
            crawl_result.pages, source_college_name=college_name_hint
        )

        # Step 4: Enrich (if enabled and requested)
        colleges: List[CollegeDetail] = []
        for key, match in college_matches.items():
            rankings_data = None
            if config.include_enrichment and self.enrichment_service.is_available:
                enrichment = await self.enrichment_service.enrich(match.name)
                if enrichment:
                    rankings_data = RankingsData(**enrichment)

            detail = CollegeDetail(
                name=match.name,
                url=match.url,
                location=Location(city=match.city, state=match.state)
                if match.city or match.state
                else None,
                rankings_data=rankings_data,
                source_pages=match.source_pages,
                mention_count=match.mention_count,
                confidence=match.confidence,
                context_snippets=match.context_snippets,
            )
            colleges.append(detail)

        # Sort by mention count (desc), then confidence (desc)
        colleges.sort(key=lambda c: (c.mention_count, c.confidence), reverse=True)

        duration = time.time() - start_time
        timed_out = duration >= config.timeout_seconds

        return CrawlResponse(
            summary=CrawlSummary(
                resolved_url=url,
                pages_crawled=crawl_result.pages_crawled,
                max_depth_reached=crawl_result.max_depth_reached,
                duration_seconds=round(duration, 2),
                status="partial" if timed_out else "completed",
            ),
            colleges=colleges,
            errors=errors,
        )

    async def _resolve_input(self, request: CrawlRequest) -> Tuple[Optional[str], InputType]:
        """Resolve the input to a URL, auto-detecting type if needed."""
        input_val = request.input.strip()
        input_type = request.input_type

        if input_type == InputType.AUTO:
            input_type = InputType.URL if is_valid_url(input_val) else InputType.NAME

        if input_type == InputType.URL:
            url = input_val
            if not url.startswith("http"):
                url = f"https://{url}"
            return url, InputType.URL

        # Name → search for URL
        url, confidence = await self.search_service.search_college_url(input_val)
        if url and confidence > 0.3:
            return url, InputType.NAME

        return None, InputType.NAME
