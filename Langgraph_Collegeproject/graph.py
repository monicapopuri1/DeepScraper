import asyncio
import json
import logging
import os
import re
import time
from typing import TypedDict

import httpx
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END

from scraper import fetch_page_text, fetch_page_and_links

load_dotenv()

OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3")   # llama3 8B — much better than llama3.2 2B
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434")

# ---------------------------------------------------------------------------
# Logging setup — one logger for the whole pipeline
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
    force=True,
)
log = logging.getLogger("college")

# Write pipeline logs to a dedicated clean file (no Flask ANSI noise)
_pipeline_handler = logging.FileHandler("pipeline.log", mode="a", encoding="utf-8")
_pipeline_handler.setFormatter(logging.Formatter(
    "%(asctime)s  %(levelname)-7s  %(message)s", datefmt="%H:%M:%S"
))
log.addHandler(_pipeline_handler)
log.propagate = True  # also still goes to stdout/server.log


def _ask_llm(prompt: str) -> str:
    """Call Ollama's local LLM via its REST API."""
    log.info("  [LLM] sending prompt (%d chars) to %s", len(prompt), OLLAMA_MODEL)
    t0 = time.time()
    response = httpx.post(
        f"{OLLAMA_URL}/api/generate",
        json={
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": 0},  # deterministic — same prompt always gives same answer
        },
        timeout=180,
    )
    response.raise_for_status()
    result = response.json()["response"].strip()
    log.info("  [LLM] response in %.1fs → %s", time.time() - t0, result[:120])
    return result

def _extract_json_array(text: str):
    """Extract the first well-formed JSON array from text that may contain prose."""
    # Find the first '[' and walk forward counting brackets to find its matching ']'
    start = text.find("[")
    if start == -1:
        return None
    depth = 0
    for i, ch in enumerate(text[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def resolve_course_synonyms(course: str) -> dict:
    """
    Two-step resolution:
      1. Ask the LLM to list ALL distinct Indian degree meanings for the abbreviation.
      2. If more than one distinct degree is returned, flag as ambiguous (decided
         programmatically — not left to the model's own judgement).
      3. For each unique meaning, ask the LLM for its Indian synonyms.

    Returns:
        {
          "full_name": str,
          "synonyms": [str, ...],
          "ambiguous": bool,
          "interpretations": [{"name": str, "short": str}, ...]
        }
    """
    # ── Step 1: enumerate all possible Indian degree meanings ────────────────
    meanings_prompt = (
        f"You are a database of Indian university degrees (UGC/AICTE/NMC/INC/BCI regulated).\n\n"
        f"List EVERY distinct degree or course in India that uses the abbreviation or name: \"{course}\"\n\n"
        f"Rules:\n"
        f"- Only list courses that actually exist and are taught in Indian colleges/universities.\n"
        f"- Do NOT include US/UK/Australian courses.\n"
        f"- Spelling variants of the SAME degree count as ONE entry.\n"
        f"- Output a JSON array of objects. Each object: {{\"name\": \"<full degree name>\", \"short\": \"<common abbrev>\"}}\n"
        f"- If only one degree uses this abbreviation, still output an array with one item.\n"
        f"- Output ONLY the JSON array. No prose, no markdown.\n\n"
        f"Abbreviation: \"{course}\""
    )

    meanings = []
    try:
        raw = _ask_llm(meanings_prompt)
        raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
        parsed = _extract_json_array(raw)
        if parsed is not None:
            meanings = parsed
            log.info("  [resolve] '%s' → %d meaning(s): %s", course, len(meanings),
                     [m.get("name", "") for m in meanings])
    except Exception as e:
        log.error("  [resolve] meanings step failed for '%s': %s", course, e)

    # ── Step 2: decide ambiguity programmatically ────────────────────────────
    if len(meanings) > 1:
        return {
            "full_name": "",
            "synonyms": [],
            "ambiguous": True,
            "interpretations": [
                {"name": m.get("name", ""), "short": m.get("short", course)}
                for m in meanings
            ],
        }

    # ── Step 3: single meaning — get all Indian synonyms ────────────────────
    full_name = meanings[0].get("name", course) if meanings else course

    syns_prompt = (
        f"You are an expert on Indian higher education.\n\n"
        f"List ALL synonyms, abbreviations, and alternate spellings used in India for:\n"
        f"\"{full_name}\"\n\n"
        f"Rules:\n"
        f"- Only include variants actually used in Indian universities/colleges.\n"
        f"- Include: with/without dots (B.Sc vs BSc), with/without 'in'/'of', bracket variants, etc.\n"
        f"- Always include the original input \"{course}\" in the list.\n"
        f"- Output a JSON array of strings only. No prose, no markdown.\n\n"
        f"Example output: [\"B.Sc Nursing\", \"BSc Nursing\", \"B.Sc. in Nursing\", \"BSN\"]"
    )

    synonyms = [course]
    try:
        raw = _ask_llm(syns_prompt)
        raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
        parsed = _extract_json_array(raw)
        if parsed is not None:
            synonyms = list(dict.fromkeys([course] + [s for s in parsed if isinstance(s, str)]))
            log.info("  [resolve] synonyms for '%s': %s", full_name, synonyms[:6])
    except Exception as e:
        log.error("  [resolve] synonyms step failed for '%s': %s", course, e)

    return {
        "full_name": full_name,
        "synonyms": synonyms,
        "ambiguous": False,
        "interpretations": [],
    }


RESULTS_FILE = "results.json"
PROGRESS_FILE = "progress.json"


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class CollegeState(TypedDict):
    entries: list[dict]           # [{url, courses, course_synonyms, correction_hint}] full input
    current_index: int            # which entry we're on
    results: list[dict]           # accumulated results
    current_url: str
    current_courses: list[str]
    course_synonyms: dict         # {course_name: [synonym, ...]} resolved before crawl
    correction_hint: str          # user's feedback when retrying a wrong result
    learned_patterns: list        # past errors learned for this domain
    html_content: str             # combined text from homepage + subpages
    subpage_links: list[str]      # course-related links found on homepage
    contact_links: list[str]      # contact-page links found on homepage
    course_found: bool
    playwright_evidence: str
    playwright_source_url: str
    contact: str
    email: str
    address: str
    error: str


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _load_json(path: str, default):
    if os.path.exists(path):
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            pass
    return default


def _save_json(path: str, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


# ---------------------------------------------------------------------------
# Nodes
# ---------------------------------------------------------------------------

def fetch_page(state: CollegeState) -> dict:
    idx = state["current_index"]
    entry = state["entries"][idx]
    url = entry["url"]
    courses = entry.get("courses", [])

    course_synonyms    = entry.get("course_synonyms", {})
    correction_hint    = entry.get("correction_hint", "")
    learned_patterns   = entry.get("learned_patterns", [])

    log.info("━" * 60)
    log.info("[%d/%d] NODE: fetch_page → %s", idx + 1, len(state["entries"]), url)
    log.info("  courses to check: %s", courses)
    if course_synonyms:
        log.info("  synonyms: %s", {k: v[:3] for k, v in course_synonyms.items()})
    if correction_hint:
        log.info("  correction_hint: %s", correction_hint[:120])
    if learned_patterns:
        log.info("  learned_patterns: %d pattern(s) for this domain", len(learned_patterns))

    # Build expanded search terms for link discovery (original names + synonyms)
    all_search_terms = list(courses)
    for syns in course_synonyms.values():
        all_search_terms.extend(syns)

    t0 = time.time()
    text, course_links, contact_links, error = fetch_page_and_links(url, courses=all_search_terms)
    log.info("  fetch completed in %.1fs", time.time() - t0)

    if error:
        log.warning("  fetch FAILED: %s", error)
        return {
            "current_url": url,
            "current_courses": courses,
            "course_synonyms": course_synonyms,
            "correction_hint": correction_hint,
            "learned_patterns": learned_patterns,
            "html_content": "",
            "subpage_links": [],
            "contact_links": [],
            "error": error,
        }

    log.info("  homepage text: %d chars", len(text))
    log.info("  course sub-pages found: %s", course_links)
    log.info("  contact pages found:    %s", contact_links)

    return {
        "current_url": url,
        "current_courses": courses,
        "course_synonyms": course_synonyms,
        "correction_hint": correction_hint,
        "learned_patterns": learned_patterns,
        "html_content": text,
        "subpage_links": course_links,
        "contact_links": contact_links,
        "error": "",
    }


def _expand_paginated_links(links: list[str], extra_pages: int = 2) -> list[str]:
    """
    If a link ends with /N (a page number), also generate /N+1 ... /N+extra_pages.
    This handles paginated programme listings like /our-programmes/UGP/1 → also try /2, /3.
    """
    expanded = []
    for link in links:
        expanded.append(link)
        m = re.search(r"^(.*/)(\d+)$", link)
        if m:
            base, page = m.group(1), int(m.group(2))
            for p in range(page + 1, page + extra_pages + 1):
                expanded.append(f"{base}{p}")
    return expanded


def crawl_subpages(state: CollegeState) -> dict:
    """
    Fetch course sub-pages and contact pages, append their text to html_content.
    Paginated programme listings (ending in /1) are expanded to also fetch /2 and /3.
    Contact pages are appended last so extract_contact sees them prominently.
    """
    log.info("NODE: crawl_subpages")
    combined = state["html_content"]

    subpage_links = _expand_paginated_links(state.get("subpage_links", []))

    for link in subpage_links:
        log.info("  fetching programme page: %s", link)
        t0 = time.time()
        text, err = fetch_page_text(link, max_chars=3000)
        if err:
            log.warning("    ✗ failed (%s)", err)
        elif text:
            log.info("    ✓ got %d chars (%.1fs)", len(text), time.time() - t0)
            combined += f"\n\n--- Programme page: {link} ---\n{text}"

    for link in state.get("contact_links", []):
        log.info("  fetching contact page:   %s", link)
        t0 = time.time()
        text, err = fetch_page_text(link, max_chars=3000)
        if err:
            log.warning("    ✗ failed (%s)", err)
        elif text:
            log.info("    ✓ got %d chars (%.1fs)", len(text), time.time() - t0)
            combined += f"\n\n--- Contact page: {link} ---\n{text}"

    log.info("  total html_content: %d chars passed to LLM", len(combined))
    return {"html_content": combined}


def check_courses(state: CollegeState) -> dict:
    log.info("NODE: check_courses")
    if state.get("error"):
        log.warning("  skipping — error in state: %s", state["error"])
        return {"course_found": False}

    courses             = state["current_courses"]
    course_synonyms_map = state.get("course_synonyms", {})
    correction_hint     = state.get("correction_hint", "")
    learned_patterns    = state.get("learned_patterns", [])
    html                = state["html_content"]

    if not courses:
        log.info("  no courses to check")
        return {"course_found": False}

    # Build a description of each course that includes all its known synonyms
    course_lines = []
    for course in courses:
        syns = course_synonyms_map.get(course, [])
        unique_syns = [s for s in syns if s.lower() != course.lower()][:8]
        if unique_syns:
            course_lines.append(f"  - {course}  (also known as: {', '.join(unique_syns)})")
        else:
            course_lines.append(f"  - {course}")
    courses_block = "\n".join(course_lines)

    # Build optional correction / learning context block
    extra_context = ""
    if correction_hint:
        extra_context += (
            f"\nIMPORTANT — USER CORRECTION (previous attempt was wrong):\n"
            f"{correction_hint}\n"
            f"Take this correction into account and re-evaluate carefully.\n"
        )
    if learned_patterns:
        pattern_lines = "\n".join(
            f"  - [{p['issue_type']}] {p['llm_analysis'] or p['user_feedback']}"
            for p in learned_patterns
        )
        extra_context += (
            f"\nLEARNED PATTERNS FOR THIS SITE (from past corrections):\n"
            f"{pattern_lines}\n"
        )

    prompt = (
        f"You are checking whether a college OFFERS specific courses for enrollment.\n"
        f"Use ONLY the text provided below. Do NOT use any prior knowledge.\n"
        f"{extra_context}\n"
        f"RULES FOR SAYING YES:\n"
        f"A course is offered only if the text shows the course name OR any of its listed synonyms\n"
        f"AND at least one of these supporting details for that course:\n"
        f"  - fees or fee structure\n"
        f"  - duration or number of years/semesters\n"
        f"  - eligibility or admission criteria\n"
        f"  - curriculum, syllabus, or subjects taught\n"
        f"  - a dedicated department or school that runs it\n"
        f"  - skills or career outcomes it leads to\n\n"
        f"RULES FOR SAYING NO:\n"
        f"  - The course name appears only in passing (e.g. a faculty bio, a research mention,\n"
        f"    a comparison to another institution, or a generic list with no supporting detail)\n"
        f"  - You are not sure — when in doubt, say Not Sure\n\n"
        f"Courses to verify (check for the course name OR any of its synonyms):\n{courses_block}\n\n"
        f"Website text:\n{html}\n\n"
        f"Start your answer with 'Yes' if any course is confirmed offered, else 'No'.\n"
        f"Then briefly state which course(s) and what evidence you found.\n"
        f"Example: Yes — B.Sc Psychology: dedicated department page with 3-year duration and eligibility criteria listed."
    )
    try:
        answer = _ask_llm(prompt)
        found = answer.lower().startswith("yes")
        log.info("  LLM answer: %s", answer[:300])   # full answer so you can audit the reasoning
        log.info("  course_found = %s", found)
        return {"course_found": found}
    except Exception as e:
        log.error("  LLM call failed: %s", e)
        return {"course_found": False, "error": f"LLM error in check_courses: {str(e)}"}


def _parse_contact_json(raw: str) -> dict:
    """Extract JSON from LLM output that may have surrounding prose or code fences."""
    # Strip code fences
    raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
    # Find first { ... } block
    match = re.search(r"\{[^{}]*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return {}


def _regex_extract_contact(text: str) -> dict:
    """
    Fallback: pull phone/email/address directly from text using regex.
    Used when the LLM returns prose instead of JSON.
    """
    phone_match = re.search(
        r"(?:Tel|Phone|Ph|Mobile|Contact)[:\s]*([+\d][\d\s\-/.()]{6,})", text, re.IGNORECASE
    )
    if not phone_match:
        phone_match = re.search(r"(\+?[\d][\d\s\-/.()]{8,})", text)

    email_match = re.search(r"[\w.+-]+@[\w-]+\.[a-zA-Z]{2,}", text)

    address_match = re.search(
        r"(?:Address|Addr|Location)[:\s]+([^\n]{10,120})", text, re.IGNORECASE
    )

    return {
        "phone": phone_match.group(1).strip() if phone_match else "",
        "email": email_match.group(0).strip() if email_match else "",
        "address": address_match.group(1).strip() if address_match else "",
    }


def _contact_section(html: str, max_chars: int = 6000) -> str:
    """
    Return contact-page sections from html_content, starting from the earliest
    of any recognised contact marker.  Recognises both HTTP-scraped markers
    ('--- Contact page:') and Playwright markers ('--- Contact info from:').
    Falls back to the full text when neither marker is present.
    """
    markers = ["--- Contact page:", "--- Contact info from:"]
    positions = [html.find(m) for m in markers if html.find(m) != -1]
    if positions:
        section = html[min(positions):]
    else:
        section = html
    return section[:max_chars]


def extract_contact(state: CollegeState) -> dict:
    log.info("NODE: extract_contact")
    if state.get("error"):
        log.warning("  skipping — error in state: %s", state["error"])
        return {"contact": "", "email": "", "address": ""}

    correction_hint  = state.get("correction_hint", "")
    learned_patterns = state.get("learned_patterns", [])

    contact_text = _contact_section(state["html_content"])
    log.info("  contact section: %d chars", len(contact_text))

    extra_context = ""
    if correction_hint:
        extra_context += (
            f"USER CORRECTION: {correction_hint}\n"
            f"Pay special attention to this when extracting contact details.\n\n"
        )
    if learned_patterns:
        pattern_lines = "\n".join(
            f"  - {p['llm_analysis'] or p['user_feedback']}" for p in learned_patterns
        )
        extra_context += f"LEARNED PATTERNS FOR THIS SITE:\n{pattern_lines}\n\n"

    prompt = (
        f"Extract the contact phone number, email address, and physical address from the text below.\n"
        f"{extra_context}"
        f"OUTPUT RULES — you MUST follow these exactly:\n"
        f"1. Output ONLY a single JSON object. Nothing else.\n"
        f"2. No prose, no markdown, no bullet points, no explanation before or after.\n"
        f"3. Start your response with {{ and end with }}\n"
        f"4. Use empty string \"\" for any field not found.\n\n"
        f"Required format (copy exactly, fill in values):\n"
        f"{{\"phone\": \"...\", \"email\": \"...\", \"address\": \"...\"}}\n\n"
        f"Text to extract from:\n{contact_text}"
    )
    try:
        raw = _ask_llm(prompt)
        data = _parse_contact_json(raw)

        # Fallback: if LLM ignored the JSON instruction, parse regex from the original text
        if not any(data.values()):
            log.warning("  JSON parse yielded nothing — trying regex fallback on contact section")
            data = _regex_extract_contact(contact_text)

        log.info("  extracted → phone=%r  email=%r  address=%r",
                 data.get("phone", ""), data.get("email", ""), data.get("address", ""))
        return {
            "contact": data.get("phone", ""),
            "email": data.get("email", ""),
            "address": data.get("address", ""),
        }
    except Exception as e:
        log.error("  LLM call failed: %s", e)
        return {"contact": "", "email": "", "address": "", "error": f"LLM error in extract_contact: {str(e)}"}


def log_failure(state: CollegeState) -> dict:
    log.error("NODE: log_failure → %s", state.get("error", "unknown error"))
    return {}


def save_result(state: CollegeState) -> dict:
    log.info("NODE: save_result")
    results = list(state.get("results", []))
    idx = state["current_index"]
    entry = state["entries"][idx]
    # Allow retry to overwrite an existing result at its original position
    record_index = entry.get("original_index", idx)

    record = {
        "index": record_index,
        "url": state.get("current_url", ""),
        "courses_requested": state.get("current_courses", []),
        "course_found": state.get("course_found", False),
        "playwright_evidence": state.get("playwright_evidence", ""),
        "playwright_source_url": state.get("playwright_source_url", ""),
        "contact": state.get("contact", ""),
        "email": state.get("email", ""),
        "address": state.get("address", ""),
        "status": "failed" if state.get("error") else "done",
        "error": state.get("error", ""),
    }

    # Replace if already exists (resume scenario or retry scenario)
    results = [r for r in results if r.get("index") != record_index]
    results.append(record)
    results.sort(key=lambda r: r["index"])

    log.info("  saved record: url=%s  course_found=%s  status=%s",
             record["url"], record["course_found"], record["status"])
    _save_json(RESULTS_FILE, results)
    _save_json(PROGRESS_FILE, {"current_index": idx + 1, "total": len(state["entries"])})

    return {
        "results": results,
        "current_index": idx + 1,
        # Reset per-entry fields
        "current_url": "",
        "current_courses": [],
        "course_synonyms": {},
        "correction_hint": "",
        "learned_patterns": [],
        "html_content": "",
        "subpage_links": [],
        "contact_links": [],
        "course_found": False,
        "playwright_evidence": "",
        "playwright_source_url": "",
        "contact": "",
        "email": "",
        "address": "",
        "error": "",
    }


def _playwright_contact_text(db, max_pages: int = 5) -> str:
    """
    Search the Playwright SQLite DB for pages that contain phone numbers or
    contact keywords and return their combined text.  This gives extract_contact
    real JS-rendered content instead of the raw HTML template.
    """
    contact_pattern = re.compile(
        r"\+91|Tel|Phone|Ph\b|Mobile|Fax|Contact Us|Address|Reach Us|Helpdesk",
        re.IGNORECASE,
    )
    rows = db.conn.execute(
        "SELECT url, text_content FROM pages ORDER BY depth ASC"
    ).fetchall()

    combined = ""
    seen = 0
    for url, text in rows:
        if contact_pattern.search(text):
            combined += f"\n\n--- Contact info from: {url} ---\n{text[:3000]}"
            seen += 1
            if seen >= max_pages:
                break

    log.info("  [PW] contact text pulled from %d DB pages (%d chars)", seen, len(combined))
    return combined


def playwright_fallback(state: CollegeState) -> dict:
    """
    Deep-crawl fallback using Playwright when the fast HTTP pass didn't find the course.
    Tries sitemap-only mode first (15-40 s), then BFS with a capped page limit.
    Uses Claude (Anthropic) for LLM verification instead of local Ollama.
    """
    log.info("NODE: playwright_fallback")
    if state.get("error"):
        log.warning("  skipping — error in state: %s", state["error"])
        return {"playwright_evidence": "", "playwright_source_url": ""}

    url = state["current_url"]
    courses = state["current_courses"]
    if not courses:
        return {"playwright_evidence": "", "playwright_source_url": ""}

    try:
        from playwright_crawler import (
            PlaywrightCrawler, CrawlerDB, CourseSearcher,
            derive_db_path, derive_domain, generate_variants,
        )
    except ImportError as e:
        log.error("  playwright_crawler not importable: %s", e)
        return {"playwright_evidence": "", "playwright_source_url": ""}

    db_path = derive_db_path(url)
    allowed_domains = (derive_domain(url),)

    course_synonyms_map = state.get("course_synonyms", {})

    async def _run():
        db = CrawlerDB(db_path)
        try:
            found_result = None
            for course in courses:
                # Prefer LLM-resolved synonyms; fall back to generate_variants
                resolved_syns = course_synonyms_map.get(course, [])
                if resolved_syns:
                    # Merge resolved synonyms with punctuation variants of each synonym
                    variant_set = []
                    seen_v = set()
                    for s in resolved_syns:
                        for v in generate_variants(s):
                            if v not in seen_v:
                                seen_v.add(v)
                                variant_set.append(v)
                    variants = variant_set
                else:
                    variants = generate_variants(course)
                log.info("  [PW] search variants for '%s': %s", course, variants[:6])
                searcher = CourseSearcher(db, variants)
                crawler = PlaywrightCrawler(
                    db,
                    allowed_domains=allowed_domains,
                    max_pages=40,
                    max_depth=3,
                    searcher=searcher,
                )
                log.info("  [PW] crawling for course: %s", course)
                early_result = await crawler.crawl_sitemap_only(url)
                result = early_result if early_result is not None else searcher.search_course(course)
                if result.get("found") and found_result is None:
                    found_result = result

            # Always pull contact text from the DB regardless of course result
            contact_text = _playwright_contact_text(db)

            if found_result:
                return True, found_result.get("evidence", ""), found_result.get("source_url", ""), contact_text
            return False, "", "", contact_text
        finally:
            db.close()

    try:
        found, evidence, source_url, pw_contact_text = asyncio.run(_run())
        log.info("  [PW] found=%s  source=%s  evidence=%s",
                 found, source_url, (evidence or "")[:120])

        # Append Playwright contact pages to html_content so extract_contact
        # gets JS-rendered text with real phone numbers instead of JS templates
        updated_html = state.get("html_content", "") + pw_contact_text

        return {
            "course_found": found,
            "playwright_evidence": evidence,
            "playwright_source_url": source_url,
            "html_content": updated_html,
        }
    except Exception as e:
        log.error("  playwright_fallback error: %s", e)
        return {"playwright_evidence": "", "playwright_source_url": ""}


# ---------------------------------------------------------------------------
# Conditional edges
# ---------------------------------------------------------------------------

def route_after_fetch(state: CollegeState) -> str:
    if state.get("error"):
        return "log_failure"
    return "crawl_subpages"


def route_after_check_courses(state: CollegeState) -> str:
    """Skip Playwright if course already found by the fast HTTP pass."""
    if state.get("course_found") or state.get("error"):
        return "extract_contact"
    return "playwright_fallback"


def route_after_save(state: CollegeState) -> str:
    if state["current_index"] < len(state["entries"]):
        return "fetch_page"
    return END


# ---------------------------------------------------------------------------
# Build graph
# ---------------------------------------------------------------------------

def build_graph():
    g = StateGraph(CollegeState)

    g.add_node("fetch_page", fetch_page)
    g.add_node("crawl_subpages", crawl_subpages)
    g.add_node("check_courses", check_courses)
    g.add_node("playwright_fallback", playwright_fallback)
    g.add_node("extract_contact", extract_contact)
    g.add_node("log_failure", log_failure)
    g.add_node("save_result", save_result)

    g.set_entry_point("fetch_page")

    g.add_conditional_edges("fetch_page", route_after_fetch, {
        "crawl_subpages": "crawl_subpages",
        "log_failure": "log_failure",
    })
    g.add_edge("crawl_subpages", "check_courses")
    g.add_conditional_edges("check_courses", route_after_check_courses, {
        "extract_contact": "extract_contact",
        "playwright_fallback": "playwright_fallback",
    })
    g.add_edge("playwright_fallback", "extract_contact")
    g.add_edge("extract_contact", "save_result")
    g.add_edge("log_failure", "save_result")

    g.add_conditional_edges("save_result", route_after_save, {
        "fetch_page": "fetch_page",
        END: END,
    })

    return g.compile()


# ---------------------------------------------------------------------------
# Public runner
# ---------------------------------------------------------------------------

def run_graph(entries: list[dict], status_holder: dict):
    """
    Run the LangGraph workflow.
    status_holder is a shared dict updated for /api/status.
    """
    # Resume from checkpoint
    progress = _load_json(PROGRESS_FILE, {})
    start_index = progress.get("current_index", 0)

    # Skip already-done entries
    remaining = entries[start_index:]
    if not remaining:
        status_holder["running"] = False
        return

    existing_results = _load_json(RESULTS_FILE, [])

    initial_state: CollegeState = {
        "entries": entries,
        "current_index": start_index,
        "results": existing_results,
        "current_url": "",
        "current_courses": [],
        "course_synonyms": {},
        "correction_hint": "",
        "learned_patterns": [],
        "html_content": "",
        "subpage_links": [],
        "contact_links": [],
        "course_found": False,
        "playwright_evidence": "",
        "playwright_source_url": "",
        "contact": "",
        "email": "",
        "address": "",
        "error": "",
    }

    graph = build_graph()

    for event in graph.stream(initial_state):
        # event is {node_name: state_update}
        # In langgraph 1.x some internal events have None as the value
        for node_name, state_update in event.items():
            if not isinstance(state_update, dict):
                continue
            if state_update.get("current_url"):
                status_holder["current_url"] = state_update["current_url"]
            if "current_index" in state_update:
                status_holder["current_index"] = state_update["current_index"]
            status_holder["total"] = len(entries)

    status_holder["running"] = False
