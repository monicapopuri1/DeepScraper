# College Info Checker — Architecture & HLD

## 1. What This System Does

Given a list of college website URLs and course names, the system:
1. Crawls each college website (homepage + relevant sub-pages)
2. Checks whether the requested courses are offered
3. Extracts phone, email, and address from the contact page
4. Displays results live in a web UI, saves progress to disk, and resumes if interrupted

---

## 2. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                        Browser (UI)                         │
│   index.html  — vanilla HTML/JS, polls every 2 seconds      │
└────────────────────────┬────────────────────────────────────┘
                         │  HTTP (REST)
                         ▼
┌─────────────────────────────────────────────────────────────┐
│                    Flask Web Server                          │
│   app.py  — 4 routes: GET /  POST /api/start                │
│                        GET /api/results  GET /api/status    │
│                        POST /api/reset                       │
│                                                             │
│   Background Thread  ──────────────────────────────────┐   │
│   (daemon=True)                                         │   │
└─────────────────────────────────────────────────────────┼───┘
                                                          │
                                                          ▼
┌─────────────────────────────────────────────────────────────┐
│              LangGraph Workflow  (graph.py)                  │
│                                                             │
│  CollegeState (shared typed dict, mutated node-by-node)     │
│                                                             │
│  fetch_page ──► crawl_subpages ──► check_courses            │
│       │                                    │                │
│  (error?)                           extract_contact         │
│       │                                    │                │
│  log_failure ──────────────────────► save_result            │
│                                            │                │
│                              (more URLs?) ─┤               │
│                                    ┌───────┘               │
│                              fetch_page  /  END             │
└─────────────────────────────────────────────────────────────┘
                         │
              ┌──────────┴──────────┐
              ▼                     ▼
┌─────────────────────┐  ┌──────────────────────────────────┐
│   scraper.py        │  │   Ollama  (local LLM server)     │
│                     │  │                                  │
│  requests + BS4     │  │  Model: llama3.2 (2 GB, free)    │
│  fetch homepage     │  │  Port: 11434                     │
│  discover links     │  │  API: POST /api/generate         │
│  fallback probing   │  │                                  │
│  (ThreadPoolExec)   │  │  Used by:                        │
└─────────────────────┘  │  • check_courses node            │
                         │  • extract_contact node          │
                         └──────────────────────────────────┘
                                        │
              ┌─────────────────────────┘
              ▼
┌─────────────────────────────────────────────────────────────┐
│                  Disk (JSON files)                           │
│   results.json   — all extracted records                    │
│   progress.json  — { current_index, total }                 │
└─────────────────────────────────────────────────────────────┘
```

---

## 3. Component Breakdown

### 3.1 Flask Web Server (`app.py`)

| Route | Method | Purpose |
|-------|--------|---------|
| `/` | GET | Serves the HTML UI |
| `/api/start` | POST | Accepts `{entries:[{url, courses}]}`, spawns background thread |
| `/api/results` | GET | Returns `results.json` contents for live polling |
| `/api/status` | GET | Returns `{running, current_url, current_index, total}` |
| `/api/reset` | POST | Deletes result/progress files, resets state |

**Threading model:**
- The Flask main thread serves HTTP requests
- A single daemon background thread runs the LangGraph workflow
- A `threading.Lock` protects the shared `_status` dict from race conditions
- The background thread writes results to disk; Flask reads from disk, so no shared memory for data

---

### 3.2 LangGraph Workflow (`graph.py`)

#### Why LangGraph?

LangGraph is used instead of a plain Python loop because it provides:

| Need | How LangGraph Delivers It |
|------|--------------------------|
| **Named steps** | Each action is a named node — easy to debug, log, and trace |
| **Typed shared state** | `CollegeState` TypedDict flows through all nodes automatically |
| **Conditional routing** | `add_conditional_edges` branches on error vs success cleanly |
| **Cyclic execution** | The graph loops back to `fetch_page` after each URL — native in LangGraph, needs manual plumbing in plain code |
| **Streaming events** | `graph.stream()` emits events after each node — used to update the live status bar |
| **Composability** | Nodes are plain Python functions — easy to add/remove steps |

#### State Object (`CollegeState`)

```
CollegeState
├── entries          [ {url, courses}, ... ]   ← full input list, never changes
├── current_index    int                        ← which URL we're on (loop counter)
├── results          [ {record}, ... ]          ← accumulated results
│
│  ── per-URL fields, reset after each save_result ──
├── current_url      str
├── current_courses  [str]
├── html_content     str   ← grows as subpages are fetched and appended
├── subpage_links    [str] ← programme/dept page URLs discovered on homepage
├── contact_links    [str] ← contact page URLs (scanned + fallback guessed)
├── course_found     bool
├── contact          str   ← phone
├── email            str
├── address          str
└── error            str   ← set on fetch failure; causes log_failure branch
```

LangGraph merges the dict returned by each node into the state automatically — only fields returned from a node are updated; everything else is preserved.

#### Nodes — What Each Does

```
┌──────────────┬──────────────────────────────────────────────────────────────┐
│ Node         │ Responsibility                                                │
├──────────────┼──────────────────────────────────────────────────────────────┤
│ fetch_page   │ One HTTP GET to the homepage via scraper.fetch_page_and_links │
│              │ → extracts page text (6000 chars max)                         │
│              │ → discovers course sub-page links (regex keyword matching)    │
│              │ → discovers contact page links (regex + parallel fallback)    │
│              │ Sets: html_content, subpage_links, contact_links, error       │
├──────────────┼──────────────────────────────────────────────────────────────┤
│ crawl_       │ Fetches each sub-page link (course pages first, then contact) │
│ subpages     │ Appends text to html_content with section headers             │
│              │ Result: html_content = homepage + programme pages + contact   │
├──────────────┼──────────────────────────────────────────────────────────────┤
│ check_       │ Sends html_content + course list to LLM (llama3.2)           │
│ courses      │ Prompt asks for Yes/No + which courses found                  │
│              │ Sets: course_found (bool)                                     │
├──────────────┼──────────────────────────────────────────────────────────────┤
│ extract_     │ Sends html_content to LLM (llama3.2)                         │
│ contact      │ Prompt asks for JSON {phone, email, address}                  │
│              │ Robust JSON parser handles LLM prose/code-fence wrapping      │
│              │ Sets: contact, email, address                                 │
├──────────────┼──────────────────────────────────────────────────────────────┤
│ log_failure  │ No-op — error is already in state from fetch_page             │
│              │ Exists so the graph has a named node to route to on failure   │
├──────────────┼──────────────────────────────────────────────────────────────┤
│ save_result  │ Writes record to results.json                                 │
│              │ Writes {current_index+1} to progress.json (resume checkpoint) │
│              │ Resets all per-URL fields in state                            │
│              │ Advances current_index → triggers loop-back or END            │
└──────────────┴──────────────────────────────────────────────────────────────┘
```

#### Conditional Edges (Routing Logic)

```
After fetch_page:
  error set?  ──YES──► log_failure ──► save_result
              ──NO───► crawl_subpages ──► check_courses

After save_result:
  current_index < len(entries)?  ──YES──► fetch_page   (next URL)
                                 ──NO───► END
```

#### Resume / Fault Tolerance

```python
# On every run_graph() call:
progress = load_json("progress.json")   # { current_index: N }
start_index = progress.get("current_index", 0)
# Skip already-completed entries by starting the state at start_index
```

`save_result` writes `progress.json` after every single URL completes.
If the server is killed mid-run, restarting and clicking Start again picks up from the last saved `current_index`.

---

### 3.3 Web Scraper (`scraper.py`)

Responsible for all HTTP I/O. No AI involved here — pure deterministic code.

#### Link Discovery Strategy

```
fetch_page_and_links(url)
    │
    ├─ 1 HTTP GET to homepage
    │
    ├─ Extract text (strip scripts/nav/footer, collapse whitespace, 6000 char cap)
    │
    ├─ Scan all <a href> tags:
    │     Course links  → href or link text matches _COURSE_KEYWORDS regex
    │     Contact links → href or link text matches _CONTACT_KEYWORDS regex
    │     Scored by number of keyword matches → top N returned
    │
    └─ Fallback for contact (if scanner found < 2 links):
          13 common paths tried IN PARALLEL (ThreadPoolExecutor)
          e.g. /contact-us, /contact, /contact.php, /about/contact ...
          HEAD request (4s timeout) per path — parallel = max 4s total wait
          Returns paths that respond with HTTP 2xx
```

**Why parallel probing?** Sequential probing of 13 paths × 4s timeout = up to 52s per college. Parallel probing = 4s maximum regardless of how many paths are tried.

---

### 3.4 AI / LLM Layer

#### Which model is used

| Model | Where it runs | Cost | Why chosen |
|-------|--------------|------|-----------|
| **llama3.2** (2B) | Locally via Ollama | Free, no API key | OpenAI & Anthropic API keys had no billing credits. Ollama was already installed with llama3.2 on this machine. |

#### Why an LLM at all?

Two tasks in this system genuinely require natural language understanding:

**Task 1 — Course Verification (`check_courses`)**

A college page might say:
- "M.Sc. Chemistry" when you searched for "Master of Science Chemistry"
- "Dept. of Chemical Sciences" — implies chemistry but doesn't say so
- "School of Sciences → Chemistry" — nested navigation text

A simple `string.contains()` would miss all these. The LLM understands semantic equivalence and abbreviations.

**Task 2 — Contact Extraction (`extract_contact`)**

Contact info on university pages appears in wildly different formats:
- Phone: `+91-80-4012-9100`, `91 80 4012 9100 / 9600`, `Tel: 080-40129100`
- Address: sometimes in footer, sometimes in a table, sometimes split across divs

The LLM is given the full page text and asked to return structured JSON. It handles all format variations without brittle regex patterns.

#### How the LLM is called

```
graph.py: _ask_llm(prompt)
    │
    └─► httpx.post("http://localhost:11434/api/generate", json={
            "model": "llama3.2",
            "prompt": "...",
            "stream": false
        })
        │
        └─► Ollama server (local process) runs inference
            Response: { "response": "Yes, Master of Science Chemistry" }
```

No LangChain LLM wrapper is used. The call goes directly via `httpx` to Ollama's REST API. This was necessary because:
- `langchain-openai` → OpenAI key exhausted
- `langchain-anthropic` → not installed (no internet at install time)
- `httpx` is already a dependency of `langgraph-sdk` so no extra install needed

#### LLM Prompt Design

**check_courses prompt:**
```
You are checking whether a college website offers specific courses.
Look for the course names or closely related terms (abbreviations,
department names, or partial matches like 'M.Sc Chemistry' for
'Master of Science Chemistry').

Courses to find: {courses}
Web page text: {html_content}

Does this college appear to offer any of the listed courses?
Start your answer with 'Yes' or 'No', then list which courses were found.
```

Key design decisions:
- Explicitly tells the LLM to match abbreviations and partial names
- Asks for Yes/No first (easy to parse with `answer.lower().startswith("yes")`)

**extract_contact prompt:**
```
Extract the contact phone number, email address, and physical address.
Reply with ONLY a raw JSON object — no explanation, no markdown.
Format: {"phone": "...", "email": "...", "address": "..."}
Use empty string for any field not found.
```

Key design decisions:
- Instructs "ONLY raw JSON" to avoid the LLM wrapping with ```json ... ```
- `_parse_contact_json()` still handles code fences defensively with regex

---

### 3.5 Persistence Layer

No database. Two JSON files on disk:

| File | Written by | Read by | Contents |
|------|-----------|---------|---------|
| `results.json` | `save_result` node | `/api/results` route | Array of result records |
| `progress.json` | `save_result` node | `run_graph()` on startup | `{current_index, total}` |

**Why no database?**
For a batch job processing O(100) URLs, file I/O is fast enough and eliminates a dependency. The files are small (< 50 KB for 100 colleges).

---

## 4. Data Flow — One College URL

```
User submits: { url: "https://christuniversity.in", courses: "MBA, M.Sc Chemistry" }

  ▼ Flask /api/start
  ▼ Background thread starts
  ▼ run_graph() called

  ╔═══════════════╗
  ║  fetch_page   ║  GET christuniversity.in
  ╚═══════════════╝    → html_content = "CHRIST University... School of Sciences..."
         │              → subpage_links = ["/departments/school-of-sciences/..."]
         │              → contact_links = ["/contact-us"]   (fallback guessed)
         ▼
  ╔════════════════════╗
  ║  crawl_subpages    ║  GET /departments/school-of-sciences/...
  ╚════════════════════╝  GET /contact-us
         │               html_content now = homepage + dept page + contact page
         ▼
  ╔════════════════╗
  ║  check_courses ║  llama3.2: "Do MBA, M.Sc Chemistry appear in this text?"
  ╚════════════════╝  → "Yes, MBA, M.Sc Chemistry"
         │            → course_found = True
         ▼
  ╔══════════════════╗
  ║  extract_contact ║  llama3.2: "Extract phone, email, address as JSON"
  ╚══════════════════╝  → {"phone": "+91 80 4012 9100", "email": "mail@christuniversity.in",
         │                  "address": "Hosur Road, Bengaluru - 560029"}
         ▼
  ╔═════════════╗
  ║ save_result ║  Writes to results.json + progress.json
  ╚═════════════╝  current_index → 1
         │
         ▼   more URLs? → loop to fetch_page
             no more?   → END

  ▼ Browser polls /api/results every 2s → shows row in table
```

---

## 5. Technology Choices Summary

| Technology | Role | Why |
|-----------|------|-----|
| **LangGraph** | Orchestration | Structured node/edge graph with typed state, cyclic execution, streaming events, clean error routing |
| **llama3.2** via Ollama | LLM inference | Free, local, no API key, already installed; good enough for Yes/No + JSON extraction |
| **requests + BeautifulSoup** | Web scraping | Simple, reliable for static HTML; university pages are mostly server-rendered |
| **httpx** | LLM API calls | Already a transitive dependency; supports sync calls with timeout |
| **ThreadPoolExecutor** | Parallel URL probing | Prevents sequential HEAD requests from blocking the graph for 60+ seconds |
| **Flask** | Web server | Lightweight, minimal setup; serves both the UI and REST API from one process |
| **JSON files** | Persistence | No database dependency; sufficient for O(100) records |
| **Vanilla JS** | Frontend | No build step, no framework overhead; simple polling loop |

---

## 6. Limitations and Known Gaps

| Limitation | Root Cause | Potential Fix |
|-----------|-----------|--------------|
| JavaScript-rendered pages | `requests` only gets static HTML; contact info loaded via JS is invisible | Use Playwright/Selenium headless browser |
| Homepage-only courses | If a college doesn't list courses on the homepage or any linked page | User provides direct course-catalog URL instead of homepage |
| LLM hallucination risk | llama3.2 (2B) may say "Yes" even if a course isn't there | Use a larger model (llama3:70b, GPT-4o) for higher accuracy |
| Sequential URL processing | One URL at a time through the graph | Parallelise with multiple graph instances or LangGraph's built-in `Send` API |
| No JS rendering | Sites like BITS Pilani load contact info via JavaScript | Playwright integration in `scraper.py` |
