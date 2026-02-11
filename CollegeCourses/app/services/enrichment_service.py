import logging
from typing import Optional

from app.config import settings

logger = logging.getLogger(__name__)


class EnrichmentService:
    """
    College Scorecard API enrichment service.

    Currently stubbed — will activate automatically when
    COLLEGE_SCORECARD_API_KEY is set in .env.
    """

    def __init__(self):
        self.api_key = settings.college_scorecard_api_key
        self.enabled = bool(self.api_key)
        if self.enabled:
            logger.info("College Scorecard enrichment enabled")
        else:
            logger.info(
                "College Scorecard enrichment disabled (no API key). "
                "Set COLLEGE_SCORECARD_API_KEY in .env to enable."
            )

    async def enrich(self, college_name: str) -> Optional[dict]:
        """
        Enrich a college with Scorecard data.
        Returns None when API key is not configured.
        """
        if not self.enabled:
            return None

        # TODO: Implement when API key is available
        # API endpoint: https://api.data.gov/ed/collegescorecard/v1/schools
        # Params: school.name={name}&api_key={key}&fields=...
        logger.debug(f"Enrichment stub called for: {college_name}")
        return None

    @property
    def is_available(self) -> bool:
        return self.enabled
