from typing import List, Optional

from pydantic import BaseModel, Field


class Location(BaseModel):
    city: Optional[str] = None
    state: Optional[str] = None


class RankingsData(BaseModel):
    admission_rate: Optional[float] = None
    sat_avg: Optional[int] = None
    undergraduate_enrollment: Optional[int] = None
    source: Optional[str] = None


class CollegeDetail(BaseModel):
    name: str
    url: Optional[str] = None
    location: Optional[Location] = None
    college_type: Optional[str] = None
    rankings_data: Optional[RankingsData] = None
    source_pages: List[str] = Field(default_factory=list)
    mention_count: int = 1
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    context_snippets: List[str] = Field(default_factory=list)


class CrawlSummary(BaseModel):
    resolved_url: str
    pages_crawled: int = 0
    max_depth_reached: int = 0
    duration_seconds: float = 0.0
    status: str = "completed"


class CrawlError(BaseModel):
    url: Optional[str] = None
    error: str
    stage: str = "crawl"


class CrawlResponse(BaseModel):
    summary: CrawlSummary
    colleges: List[CollegeDetail] = Field(default_factory=list)
    errors: List[CrawlError] = Field(default_factory=list)
