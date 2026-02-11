import logging

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

from app.models.request import CrawlRequest
from app.models.response import CrawlResponse
from app.services.extractor_service import ExtractorService
from app.services.orchestrator import Orchestrator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)

app = FastAPI(
    title="College Mention Crawler API",
    description="Deep-crawl a college website and extract all colleges/universities mentioned across the site.",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = Orchestrator()
extractor_service = ExtractorService()


@app.get("/api/v1/health")
async def health_check():
    return {"status": "healthy", "version": "1.0.0"}


@app.post("/api/v1/crawl", response_model=CrawlResponse)
async def crawl_college(request: CrawlRequest):
    """
    Crawl a college website and extract all mentioned colleges/universities.

    Accepts either a college name (e.g. "Stanford University") or a URL
    (e.g. "https://www.stanford.edu"). When a name is provided, the service
    will automatically resolve it to the official website URL.
    """
    return await orchestrator.process(request)


@app.get("/api/v1/colleges/search")
async def search_colleges(
    name: str = Query(..., min_length=2, description="College name to search for"),
    limit: int = Query(default=20, ge=1, le=100, description="Max results"),
):
    """Search the reference database for colleges by name."""
    results = extractor_service.search_colleges(name, limit=limit)
    return {"query": name, "count": len(results), "results": results}
