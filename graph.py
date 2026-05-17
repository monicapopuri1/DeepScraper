import asyncio
import json
import logging
import os
import re
import threading
import time
from typing import TypedDict

import anthropic
from dotenv import load_dotenv
from langgraph.graph import StateGraph, END

from configs import load_config
from scraper import fetch_page_text, fetch_page_and_links

load_dotenv()

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

_claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_domain_config = load_config()

# Lock for thread-safe writes to results.json when running in parallel
_results_file_lock = threading.Lock()

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
    """Call Claude API for LLM inference."""
    log.info("  [LLM] sending prompt (%d chars) to %s", len(prompt), CLAUDE_MODEL)
    t0 = time.time()
    message = _claude_client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1024,
        messages=[{"role": "user", "content": prompt}],
    )
    result = message.content[0].text.strip()
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


def resolve_term_synonyms(term: str) -> dict:
    """
    Expand a search term into synonyms and detect ambiguity.
    Only runs when config["use_synonyms"] is True.

    Returns:
        {
          "full_name": str,
          "synonyms": [str, ...],
          "ambiguous": bool,
          "interpretations": [{"name": str, "short": str}, ...]
        }
    """
    if not _domain_config.get("use_synonyms"):
        return {"full_name": term, "synonyms": [term], "ambiguous": False, "interpretations": []}

    context = _domain_config.get("synonyms_context", "")

    # Step 1: enumerate all possible meanings for this term
    meanings_prompt = (
        f"You are a database of {context}.\n\n"
        f"List EVERY distinct meaning or variant that uses the abbreviation or name: \"{term}\"\n\n"
        f"Rules:\n"
        f"- Only list entries that actually exist in this domain.\n"
        f"- Spelling variants of the SAME entry count as ONE item.\n"
        f"- Output a JSON array of objects: {{\"name\": \"<full name>\", \"short\": \"<abbreviation>\"}}\n"
        f"- Output ONLY the JSON array. No prose, no markdown.\n\n"
        f"Term: \"{term}\""
    )

    meanings = []
    try:
        raw = _ask_llm(meanings_prompt)
        raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
        parsed = _extract_json_array(raw)
        if parsed is not None:
            meanings = parsed
            log.info("  [resolve] '%s' → %d meaning(s): %s", term, len(meanings),
                     [m.get("name", "") for m in meanings])
    except Exception as e:
        log.error("  [resolve] meanings step failed for '%s': %s", term, e)

    # Step 2: flag as ambiguous if more than one distinct meaning found
    if len(meanings) > 1:
        return {
            "full_name": "",
            "synonyms": [],
            "ambiguous": True,
            "interpretations": [
                {"name": m.get("name", ""), "short": m.get("short", term)}
                for m in meanings
            ],
        }

    # Step 3: single meaning — get all synonyms/variants
    full_name = meanings[0].get("name", term) if meanings else term

    syns_prompt = (
        f"You are an expert on {context}.\n\n"
        f"List ALL synonyms, abbreviations, and alternate spellings for:\n"
        f"\"{full_name}\"\n\n"
        f"Rules:\n"
        f"- Only include variants actually used in this domain.\n"
        f"- Always include the original input \"{term}\" in the list.\n"
        f"- Output a JSON array of strings only. No prose, no markdown."
    )

    synonyms = [term]
    try:
        raw = _ask_llm(syns_prompt)
        raw = re.sub(r"```[a-z]*", "", raw).replace("```", "").strip()
        parsed = _extract_json_array(raw)
        if parsed is not None:
            synonyms = list(dict.fromkeys([term] + [s for s in parsed if isinstance(s, str)]))
            log.info("  [resolve] synonyms for '%s': %s", full_name, synonyms[:6])
    except Exception as e:
        log.error("  [resolve] synonyms step failed for '%s': %s", term, e)

    return {
        "full_name": full_name,
        "synonyms": synonyms,
        "ambiguous": False,
        "interpretations": [],
    }


# Keep old name as alias so existing callers don't break during transition
resolve_course_synonyms = resolve_term_synonyms


RESULTS_FILE = "results.json"
PROGRESS_FILE = "progress.json"


# ---------------------------------------------------------------------------
# State schema
# ---------------------------------------------------------------------------

class SearchState(TypedDict):
    entries: list[dict]           # [{url, search_terms, term_synonyms, correction_hint}]
    current_index: int            # which entry we're on
    results: list[dict]           # accumulated results
    current_url: str
    search_terms: list[str]       # what we're looking for (courses, treatments, job roles, …)
    term_synonyms: dict           # {term: [synonym, ...]} resolved before crawl
    correction_hint: str          # user's feedback when retrying a wrong result
    learned_patterns: list        # past errors learned for this domain
    html_content: str             # combined text from homepage + subpages
    subpage_links: list[str]      # attribute-related links found on homepage
    contact_links: list[str]      # contact-page links found on homepage
    match_found: bool             # whether the attribute was found on the site
    playwright_evidence: str
    playwright_source_url: str
    contact: str
    email: str
    address: str
    error: str


# Alias for backward compatibility
CollegeState = SearchState


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

def fetch_page(state: SearchState) -> dict:
    idx = state["current_index"]
    entry = state["entries"][idx]
    url = entry["url"]
    search_terms    = entry.get("search_terms", [])
    term_synonyms   = entry.get("term_synonyms", {})
    correction_hint = entry.get("correction_hint", "")
    learned_patterns = entry.get("learned_patterns", [])

    log.info("━" * 60)
    log.info("[%d/%d] NODE: fetch_page → %s", idx + 1, len(state["entries"]), url)
    log.info("  search_terms: %s", search_terms)
    if term_synonyms:
        log.info("  synonyms: %s", {k: v[:3] for k, v in term_synonyms.items()})
    if correction_hint:
        log.info("  correction_hint: %s", correction_hint[:120])
    if learned_patterns:
        log.info("  learned_patterns: %d pattern(s) for this domain", len(learned_patterns))

    # Expand with synonyms for better link discovery
    all_search_terms = list(search_terms)
    for syns in term_synonyms.values():
        all_search_terms.extend(syns)

    t0 = time.time()
    text, attribute_links, contact_links, error = fetch_page_and_links(
        url, search_terms=all_search_terms, config=_domain_config
    )
    log.info("  fetch completed in %.1fs", time.time() - t0)

    if error:
        log.warning("  fetch FAILED: %s", error)
        return {
            "current_url": url,
            "search_terms": search_terms,
            "term_synonyms": term_synonyms,
            "correction_hint": correction_hint,
            "learned_patterns": learned_patterns,
            "html_content": "",
            "subpage_links": [],
            "contact_links": [],
            "error": error,
        }

    log.info("  homepage text: %d chars", len(text))
    log.info("  attribute sub-pages found: %s", attribute_links)
    log.info("  contact pages found:       %s", contact_links)

    return {
        "current_url": url,
        "search_terms": search_terms,
        "term_synonyms": term_synonyms,
        "correction_hint": correction_hint,
        "learned_patterns": learned_patterns,
        "html_content": text,
        "subpage_links": attribute_links,
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


def check_match(state: SearchState) -> dict:
    log.info("NODE: check_match")
    if state.get("error"):
        log.warning("  skipping — error in state: %s", state["error"])
        return {"match_found": False}

    search_terms     = state["search_terms"]
    term_synonyms    = state.get("term_synonyms", {})
    correction_hint  = state.get("correction_hint", "")
    learned_patterns = state.get("learned_patterns", [])
    html             = state["html_content"]

    if not search_terms:
        log.info("  no search terms to check")
        return {"match_found": False}

    # Build attribute block including synonyms
    attribute_lines = []
    for term in search_terms:
        syns = term_synonyms.get(term, [])
        unique_syns = [s for s in syns if s.lower() != term.lower()][:8]
        if unique_syns:
            attribute_lines.append(f"  - {term}  (also known as: {', '.join(unique_syns)})")
        else:
            attribute_lines.append(f"  - {term}")
    attributes_block = "\n".join(attribute_lines)

    # Build optional correction / learning context
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

    prompt = _domain_config["verify_prompt"].format(
        extra_context=extra_context,
        attributes_block=attributes_block,
        html=html,
    )

    try:
        answer = _ask_llm(prompt)
        found = answer.lower().startswith("yes")
        log.info("  LLM answer: %s", answer[:300])
        log.info("  match_found = %s", found)
        return {"match_found": found}
    except Exception as e:
        log.error("  LLM call failed: %s", e)
        return {"match_found": False, "error": f"LLM error in check_match: {str(e)}"}


# Alias so existing references don't break
check_courses = check_match


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


def save_result(state: SearchState) -> dict:
    log.info("NODE: save_result")
    results = list(state.get("results", []))
    idx = state["current_index"]
    entry = state["entries"][idx]
    record_index = entry.get("original_index", idx)

    record = {
        "index": record_index,
        "url": state.get("current_url", ""),
        "attributes_requested": state.get("search_terms", []),
        "match_found": state.get("match_found", False),
        "playwright_evidence": state.get("playwright_evidence", ""),
        "playwright_source_url": state.get("playwright_source_url", ""),
        "contact": state.get("contact", ""),
        "email": state.get("email", ""),
        "address": state.get("address", ""),
        "status": "failed" if state.get("error") else "done",
        "error": state.get("error", ""),
    }

    log.info("  saved record: url=%s  match_found=%s  status=%s",
             record["url"], record["match_found"], record["status"])

    # Thread-safe write: read latest from disk, merge, write back
    with _results_file_lock:
        on_disk = _load_json(RESULTS_FILE, [])
        on_disk = [r for r in on_disk if r.get("index") != record_index]
        on_disk.append(record)
        on_disk.sort(key=lambda r: r["index"])
        _save_json(RESULTS_FILE, on_disk)

    results = on_disk

    return {
        "results": results,
        "current_index": idx + 1,
        # Reset per-entry fields
        "current_url": "",
        "search_terms": [],
        "term_synonyms": {},
        "correction_hint": "",
        "learned_patterns": [],
        "html_content": "",
        "subpage_links": [],
        "contact_links": [],
        "match_found": False,
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


def playwright_fallback(state: SearchState) -> dict:
    """
    Deep-crawl fallback using Playwright when the fast HTTP pass didn't find the attribute.
    Tries sitemap-only mode first (15-40 s), then BFS with a capped page limit.
    Uses Claude (Anthropic) for LLM verification.
    """
    log.info("NODE: playwright_fallback")
    if state.get("error"):
        log.warning("  skipping — error in state: %s", state["error"])
        return {"playwright_evidence": "", "playwright_source_url": ""}

    url = state["current_url"]
    search_terms = state["search_terms"]
    if not search_terms:
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

    term_synonyms_map = state.get("term_synonyms", {})

    async def _run():
        db = CrawlerDB(db_path)
        try:
            found_result = None
            for course in search_terms:
                # Prefer LLM-resolved synonyms; fall back to generate_variants
                resolved_syns = term_synonyms_map.get(course, [])
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
                    priority_url_keywords=_domain_config.get("priority_url_keywords"),
                    skip_url_patterns=_domain_config.get("skip_url_patterns"),
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
            "match_found": found,
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
    """Skip Playwright if attribute already found by the fast HTTP pass."""
    if state.get("match_found") or state.get("error"):
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
    g.add_node("check_match", check_match)
    g.add_node("playwright_fallback", playwright_fallback)
    g.add_node("extract_contact", extract_contact)
    g.add_node("log_failure", log_failure)
    g.add_node("save_result", save_result)

    g.set_entry_point("fetch_page")

    g.add_conditional_edges("fetch_page", route_after_fetch, {
        "crawl_subpages": "crawl_subpages",
        "log_failure": "log_failure",
    })
    g.add_edge("crawl_subpages", "check_match")
    g.add_conditional_edges("check_match", route_after_check_courses, {
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
        "search_terms": [],
        "term_synonyms": {},
        "correction_hint": "",
        "learned_patterns": [],
        "html_content": "",
        "subpage_links": [],
        "contact_links": [],
        "match_found": False,
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
