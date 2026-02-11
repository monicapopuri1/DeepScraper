from enum import Enum

from pydantic import BaseModel, Field

from app.config import settings


class InputType(str, Enum):
    NAME = "name"
    URL = "url"
    AUTO = "auto"


class CrawlConfig(BaseModel):
    max_depth: int = Field(
        default=settings.default_max_depth, ge=1, le=5, description="Maximum crawl depth"
    )
    max_pages: int = Field(
        default=settings.default_max_pages, ge=1, le=500, description="Maximum pages to crawl"
    )
    timeout_seconds: int = Field(
        default=settings.default_timeout_seconds,
        ge=10,
        le=300,
        description="Total crawl timeout in seconds",
    )
    include_enrichment: bool = Field(
        default=True, description="Include College Scorecard enrichment data"
    )


class CrawlRequest(BaseModel):
    input: str = Field(..., min_length=1, description="College name or URL")
    input_type: InputType = Field(default=InputType.AUTO, description="Input type detection mode")
    crawl_config: CrawlConfig = Field(default_factory=CrawlConfig)
