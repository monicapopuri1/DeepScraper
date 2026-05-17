# DeepScraper

[![MIT License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![LangGraph](https://img.shields.io/badge/built%20with-LangGraph-orange.svg)](https://github.com/langchain-ai/langgraph)

An AI-powered web scraper that verifies whether an entity (college, hospital, company) has a specific attribute (course, treatment, job role) — and extracts contact information — automatically.

Supports any domain via config files. Ships with configs for **Indian colleges**, **hospitals**, and **job listings**.

---

## The Problem

Students and parents in India face a frustrating challenge when searching for colleges:

- There are **thousands of colleges** across Karnataka and India, each with a different website
- Websites are **inconsistent** — some list courses on the homepage, others bury them 3 levels deep, some render content via JavaScript
- Manually visiting each website to check course availability is **time-consuming and error-prone**
- Contact information (phone, email, address) is scattered across different pages

**There was no automated way to answer: "Which of these 50 colleges offer B.Sc Nursing?"**

---

## What This Tool Does

Given a list of college websites and course names, the system:

1. Crawls each college website (homepage + relevant sub-pages)
2. Checks whether the requested courses are offered
3. Extracts phone number, email, and address
4. Displays results live in a web UI as each college is processed
5. Saves progress so it can resume if interrupted

---

## Design

### Why LangGraph?

The pipeline is built as a **LangGraph workflow** — a graph of named steps with typed shared state. This gives us:

- **Named nodes**: each action (fetch, crawl, check, extract, save) is a named step — easy to debug and trace
- **Conditional routing**: cleanly branches on error vs success, course-found vs not-found
- **Cyclic execution**: the graph loops back to `fetch_page` after each college — native in LangGraph
- **Streaming events**: emits events after each node so the UI gets live status updates

### Two-Pass Scraping Strategy

```
Pass 1 — Fast HTTP Scraper (requests + BeautifulSoup)
  ├── Fetches homepage (no JavaScript execution)
  ├── Discovers course and contact sub-pages via keyword matching
  ├── Crawls up to 3 course pages + 2 contact pages
  └── Sends combined text to Claude for course verification

  If course NOT found by Pass 1:

Pass 2 — Playwright Deep Crawler (Chromium headless)
  ├── Launches a real browser — executes JavaScript
  ├── Tries sitemap-first (fast: 15-40s) then BFS crawl
  ├── Searches all crawled pages for course name variants
  └── Verifies matches with Claude
```

**Why two passes?** Pass 1 handles 70% of colleges in ~10 seconds. Pass 2 handles JS-rendered sites and deep-nested course pages. Running Pass 1 first avoids the overhead of launching a browser for every college.

### Architecture Overview

```
Browser (UI — vanilla HTML/JS, polls every 2 seconds)
        │  HTTP REST
        ▼
Flask Web Server (app.py)
        │
        ├── ThreadPoolExecutor (3 parallel workers)
        │        │
        │   [Worker 1]   [Worker 2]   [Worker 3]
        │        │
        ▼
LangGraph Workflow (graph.py)
   fetch_page → crawl_subpages → check_courses
                                      │
                              course found? ──YES──→ extract_contact → save_result
                                      │
                                     NO
                                      │
                              playwright_fallback → extract_contact → save_result

        │
Claude API (Anthropic)          Playwright (Chromium)
  • check_courses                 • JS-rendered pages
  • extract_contact               • Sitemap crawl
  • course synonyms               • Deep BFS crawl

        │
    results.json + progress.json (disk)
```

### Why Claude API (Not a Local LLM)?

Two tasks require genuine natural language understanding:

**Course Verification** — A college page might say "M.Sc. Chemistry" when the user searched for "Master of Science Chemistry", or "Dept. of Chemical Sciences" which implies chemistry but doesn't state it. Simple string matching fails here. Claude understands semantic equivalence.

**Contact Extraction** — Phone numbers appear in dozens of formats: `+91-80-4012-9100`, `91 80 4012 9100 / 9600`, `Tel: 080-40129100`. Claude handles all variations without brittle regex.

**Why Claude over OpenAI or Gemini?** Claude follows structured output instructions (JSON, Yes/No format) more reliably, which matters when parsing LLM responses programmatically.

### Caching and Feedback Loop

- Results are cached in SQLite (`verified_results.db`) — re-running the same college+course skips the crawl entirely
- Users can mark results as **right / partially right / wrong** from the UI
- Wrong results trigger LLM analysis of what went wrong, stored as learned patterns
- On retry, learned patterns are injected into the prompt so the same mistake isn't repeated

---

## Where We Are Today

### What Works
- Full end-to-end pipeline: scrape → verify course → extract contact → display
- Two-pass scraping (HTTP + Playwright fallback for JS sites)
- 3 colleges processed in parallel (ThreadPoolExecutor)
- SSL certificate errors bypassed (`verify=False`) — handles colleges with expired certs
- Course synonym resolution — "B.Sc (N)" expands to "B.Sc Nursing", "BSc Nursing", etc.
- Feedback + retry system
- Resume on crash — `progress.json` checkpoints after every college

### Known Limitations and Accuracy

**Current accuracy: ~75-85%** with Claude Haiku.

| Failure Type | Frequency | Workaround |
|---|---|---|
| JS-rendered pages not caught by HTTP pass | ~10% | Playwright fallback handles most |
| Courses listed only in PDFs | ~5% | Not yet implemented |
| Wrong college URL provided | User input | Provide direct course-catalog URL |
| College blocks automated requests | ~5% | No fix |
| Abbreviated course names not in synonyms | ~5% | Use resolve_courses endpoint first |

---

## Improvements Made

| Version | Change | Why |
|---|---|---|
| v1 | Gemini API for grading (GradeBuddy) | Initial implementation |
| v2 | Switched to Claude API throughout | Better instruction-following, reliable JSON output |
| v3 | Added Playwright deep crawler | Handle JS-rendered college websites |
| v4 | Course synonym resolution | "B.Sc (N)" → searches for all Indian nursing degree variants |
| v5 | Feedback + learned patterns system | System improves from user corrections |
| v6 | Caching layer (SQLite) | Avoid re-crawling same college twice |
| v7 | **Parallel processing (3 workers)** | 3x speed — 10 colleges in ~3 min instead of ~20 min |
| v8 | **SSL certificate bypass** | RVCE and other colleges with broken SSL certs now work |
| v8 | **Switched LLM from Ollama llama3 → Claude Haiku** | 10x faster LLM calls, significantly fewer false results |

---

## Planned Improvements

- [ ] Always run Playwright (not just as fallback) — better JS coverage
- [ ] Upgrade to Claude Sonnet for higher accuracy
- [ ] PDF parsing — extract courses from college prospectus PDFs
- [ ] Low-confidence flagging — uncertain results shown as "needs review" instead of wrong guess
- [ ] College name → URL lookup — user provides college name, system finds the URL

---

## How to Run

### Prerequisites

- Python 3.11+
- An Anthropic API key with credits ([console.anthropic.com](https://console.anthropic.com))
- Playwright Chromium browser

### Installation

```bash
# Clone the repo
git clone https://github.com/monicapopuri1/DeepScraper.git
cd DeepScraper

# Create and activate virtual environment
python -m venv venv
source venv/bin/activate        # Mac/Linux
venv\Scripts\activate           # Windows

# Install dependencies
pip install -r requirements.txt

# Install Playwright browser
playwright install chromium
```

### Configuration

Create a `.env` file in the project root:

```
ANTHROPIC_API_KEY=sk-ant-api03-...your-key-here...
CLAUDE_MODEL=claude-haiku-4-5-20251001     # or claude-sonnet-4-6 for higher accuracy
MAX_WORKERS=3                               # number of parallel college workers
```

### Start the Server

```bash
python app.py
```

Open your browser at [http://127.0.0.1:5001](http://127.0.0.1:5001)

### Using the UI

1. Enter college website URLs and the courses to check (comma-separated)
2. Click **Resolve Courses** — the system expands abbreviations into Indian degree variants
3. Click **Start** — results appear live as each college is processed
4. Mark results as **Right / Wrong** to improve future accuracy via the feedback system
5. Click **Retry** on any wrong result after adding a correction note

### API Endpoints (for programmatic use)

| Method | Endpoint | Purpose |
|---|---|---|
| `POST` | `/api/resolve_courses` | Expand course abbreviations into synonyms |
| `POST` | `/api/start` | Start the pipeline with a list of entries |
| `GET` | `/api/status` | Live status — current college, progress |
| `GET` | `/api/results` | All results so far |
| `POST` | `/api/feedback` | Submit right/wrong feedback on a result |
| `POST` | `/api/retry` | Re-run pipeline for a single college |
| `POST` | `/api/reset` | Clear all results and start fresh |

### Example API Call

```bash
curl -X POST http://127.0.0.1:5001/api/start \
  -H "Content-Type: application/json" \
  -d '{
    "entries": [
      {"url": "https://christuniversity.in", "courses": "MBA, B.Sc Psychology"},
      {"url": "https://www.stjohns.in", "courses": "MBBS, B.Sc Nursing"}
    ]
  }'
```

### Adjusting Number of Parallel Workers

```bash
MAX_WORKERS=5 python app.py    # run 5 colleges simultaneously
```

Higher workers = faster, but risks getting blocked by college websites. 3 is recommended.

---

## Tech Stack

| Technology | Role |
|---|---|
| LangGraph | Pipeline orchestration with typed state and conditional routing |
| Claude API (Anthropic) | Course verification and contact extraction |
| Playwright (Chromium) | JS-rendered website crawling |
| requests + BeautifulSoup | Fast HTTP scraping for static pages |
| Flask | Web server and REST API |
| SQLite | Caching verified results and learned error patterns |
| ThreadPoolExecutor | Parallel college processing |
| Vanilla JS | Frontend UI with live polling |
