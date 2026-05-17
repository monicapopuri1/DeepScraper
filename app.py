import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from flask import Flask, jsonify, render_template, request

from configs import load_config
from graph import (
    run_graph, resolve_course_synonyms,
    RESULTS_FILE, PROGRESS_FILE,
    _load_json, _save_json, _ask_llm,
)
from cache import CacheDB

app = Flask(__name__)

_cache_db = CacheDB()
_domain_config = load_config()

# Shared status between Flask and the graph worker thread
_status = {
    "running": False,
    "current_url": "",
    "current_index": 0,
    "total": 0,
}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template("index.html", config=_domain_config)


@app.route("/api/resolve_courses", methods=["POST"])
def api_resolve_courses():
    """
    For each entry, resolve every course name into Indian synonyms using the LLM.
    Returns resolutions and flags any course that is ambiguous so the UI can ask
    the user to clarify before the crawl starts.
    """
    data = request.get_json(force=True)
    entries = data.get("entries", [])

    resolved_entries = []
    needs_any_clarification = False

    for entry in entries:
        attrs_raw = entry.get("search_terms", "")
        if isinstance(attrs_raw, str):
            attr_list = [c.strip() for c in attrs_raw.split(",") if c.strip()]
        else:
            attr_list = list(attrs_raw)

        resolutions = []
        for term in attr_list:
            res = resolve_course_synonyms(term)
            resolutions.append({"original": term, **res})
            if res.get("ambiguous"):
                needs_any_clarification = True

        resolved_entries.append({
            "url": entry.get("url", ""),
            "attrs_raw": attrs_raw,
            "resolutions": resolutions,
            "needs_clarification": any(r.get("ambiguous") for r in resolutions),
        })

    return jsonify({
        "entries": resolved_entries,
        "needs_clarification": needs_any_clarification,
    })


@app.route("/api/start", methods=["POST"])
def api_start():
    global _status

    with _lock:
        if _status["running"]:
            return jsonify({"error": "Already running"}), 400

    data = request.get_json(force=True)
    entries = data.get("entries", [])
    if not entries:
        return jsonify({"error": "No entries provided"}), 400

    # Normalize search_terms field to list
    for entry in entries:
        terms = entry.get("search_terms", "")
        if isinstance(terms, str):
            entry["search_terms"] = [c.strip() for c in terms.split(",") if c.strip()]

    # Cache lookup — skip entries we already have confirmed results for
    existing_results = _load_json(RESULTS_FILE, [])
    cached_records = list(existing_results)
    uncached_entries = []
    cached_count = 0

    for i, entry in enumerate(entries):
        url = entry.get("url", "")
        search_terms = entry.get("search_terms", [])
        cached = _cache_db.lookup(url, search_terms)
        if cached:
            cached["index"] = i
            cached_records = [r for r in cached_records if r.get("index") != i]
            cached_records.append(cached)
            cached_count += 1
        else:
            entry["original_index"] = i   # preserve position in final result list
            uncached_entries.append(entry)

    if cached_count:
        cached_records.sort(key=lambda r: r.get("index", 0))
        _save_json(RESULTS_FILE, cached_records)

    if not uncached_entries:
        with _lock:
            _status.update({"running": False, "current_url": "", "current_index": 0, "total": 0})
        return jsonify({"ok": True, "total": 0, "from_cache": cached_count})

    max_workers = int(os.getenv("MAX_WORKERS", "3"))
    total = len(uncached_entries)

    with _lock:
        _status["running"] = True
        _status["current_url"] = ""
        _status["current_index"] = 0
        _status["total"] = total
        _status.pop("error", None)

    def process_one(entry):
        """Run the full pipeline for a single college entry."""
        local_status = {}
        run_graph([entry], local_status)
        with _lock:
            _status["current_index"] += 1
            _status["current_url"] = entry.get("url", "")

    def worker():
        try:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(process_one, entry): entry for entry in uncached_entries}
                for future in as_completed(futures):
                    try:
                        future.result()
                    except Exception as e:
                        entry = futures[future]
                        with _lock:
                            _status["error"] = f"{entry.get('url')}: {e}"
        finally:
            with _lock:
                _status["running"] = False

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    return jsonify({"ok": True, "total": total, "from_cache": cached_count, "workers": max_workers})


@app.route("/api/results")
def api_results():
    results = _load_json(RESULTS_FILE, [])
    return jsonify(results)


@app.route("/api/status")
def api_status():
    with _lock:
        return jsonify(dict(_status))


@app.route("/api/reset", methods=["POST"])
def api_reset():
    with _lock:
        if _status["running"]:
            return jsonify({"error": "Cannot reset while running"}), 400

    for path in (RESULTS_FILE, PROGRESS_FILE):
        if os.path.exists(path):
            os.remove(path)

    with _lock:
        _status.update({"running": False, "current_url": "", "current_index": 0, "total": 0})
        _status.pop("error", None)

    return jsonify({"ok": True})


@app.route("/api/feedback", methods=["POST"])
def api_feedback():
    """
    Record user feedback for a completed result.
    feedback: "right" | "partially_right" | "wrong"
    For right/partially_right → save to verified cache so it's never re-crawled.
    For wrong → log error pattern for the domain so future retries are smarter.
    """
    data = request.get_json(force=True)
    index = data.get("index")
    feedback = data.get("feedback", "")
    notes = data.get("notes", "")

    if index is None or feedback not in ("right", "partially_right", "wrong"):
        return jsonify({"error": "Invalid feedback data"}), 400

    results = _load_json(RESULTS_FILE, [])
    result = next((r for r in results if r.get("index") == index), None)
    if not result:
        return jsonify({"error": "Result not found"}), 404

    url = result.get("url", "")

    if feedback in ("right", "partially_right"):
        _cache_db.save_result(result, feedback, notes)

    elif feedback == "wrong":
        llm_analysis = ""
        if notes:
            try:
                entity = _domain_config.get("entity_name", "entity")
                attribute = _domain_config.get("attribute_name", "attribute")
                analysis_prompt = (
                    f"A {entity} {attribute} verification system gave an incorrect result.\n"
                    f"URL checked: {url}\n"
                    f"{attribute}s verified: {result.get('attributes_requested', [])}\n"
                    f"System result: '{attribute} Found' if {result.get('match_found')} else '{attribute} Not Found'\n"
                    f"User correction: {notes}\n\n"
                    f"In 1-2 sentences, what likely went wrong and what should the system "
                    f"look for next time to get the correct answer? Be specific and concise."
                )
                llm_analysis = _ask_llm(analysis_prompt)
            except Exception:
                llm_analysis = ""
        _cache_db.save_pattern(url, "wrong_result", notes, llm_analysis)

    # Persist feedback state in results file so UI reflects it after polling
    for r in results:
        if r.get("index") == index:
            r["feedback"] = feedback
            r["feedback_notes"] = notes
            break
    _save_json(RESULTS_FILE, results)

    return jsonify({"ok": True})


@app.route("/api/retry", methods=["POST"])
def api_retry():
    """
    Re-run the pipeline for a single result with user correction context.
    Clears progress file so run_graph starts fresh for the single entry.
    """
    with _lock:
        if _status["running"]:
            return jsonify({"error": "Cannot retry while running"}), 400

    data = request.get_json(force=True)
    index = data.get("index")
    notes = data.get("notes", "")

    if index is None:
        return jsonify({"error": "Missing index"}), 400

    results = _load_json(RESULTS_FILE, [])
    result = next((r for r in results if r.get("index") == index), None)
    if not result:
        return jsonify({"error": "Result not found"}), 404

    url = result.get("url", "")
    search_terms = result.get("attributes_requested", [])

    # Check the verified cache first — no need to crawl if we already know the answer
    cached = _cache_db.lookup(url, search_terms)
    if cached:
        cached["index"] = index
        results = [r for r in results if r.get("index") != index]
        results.append(cached)
        results.sort(key=lambda r: r.get("index", 0))
        _save_json(RESULTS_FILE, results)
        return jsonify({"ok": True, "from_cache": True})

    learned = _cache_db.get_patterns(url)

    entry = {
        "url": url,
        "search_terms": search_terms,
        "term_synonyms": {},            # will be re-resolved if needed
        "correction_hint": notes,
        "learned_patterns": learned,
        "original_index": index,        # overwrite the original row
    }

    # Clear progress file so run_graph starts from index 0 for this single entry
    if os.path.exists(PROGRESS_FILE):
        os.remove(PROGRESS_FILE)

    with _lock:
        _status["running"] = True
        _status["current_url"] = ""
        _status["current_index"] = 0
        _status["total"] = 1
        _status.pop("error", None)

    def worker():
        try:
            run_graph([entry], _status)
        except Exception as e:
            with _lock:
                _status["running"] = False
                _status["error"] = str(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5001, use_reloader=False)
