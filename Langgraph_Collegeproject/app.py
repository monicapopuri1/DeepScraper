import json
import os
import threading

from flask import Flask, jsonify, render_template, request

from graph import (
    run_graph, resolve_course_synonyms,
    RESULTS_FILE, PROGRESS_FILE,
    _load_json, _save_json, _ask_llm,
)
from cache import CacheDB

app = Flask(__name__)

_cache_db = CacheDB()

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
    return render_template("index.html")


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
        courses_raw = entry.get("courses", "")
        if isinstance(courses_raw, str):
            course_list = [c.strip() for c in courses_raw.split(",") if c.strip()]
        else:
            course_list = list(courses_raw)

        resolutions = []
        for course in course_list:
            res = resolve_course_synonyms(course)
            resolutions.append({"original": course, **res})
            if res.get("ambiguous"):
                needs_any_clarification = True

        resolved_entries.append({
            "url": entry.get("url", ""),
            "courses_raw": courses_raw,
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

    # Normalize courses field to list
    for entry in entries:
        courses = entry.get("courses", "")
        if isinstance(courses, str):
            entry["courses"] = [c.strip() for c in courses.split(",") if c.strip()]

    # Cache lookup — skip entries we already have confirmed results for
    existing_results = _load_json(RESULTS_FILE, [])
    cached_records = list(existing_results)
    uncached_entries = []
    cached_count = 0

    for i, entry in enumerate(entries):
        url = entry.get("url", "")
        courses = entry.get("courses", [])
        cached = _cache_db.lookup(url, courses)
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

    with _lock:
        _status["running"] = True
        _status["current_url"] = ""
        _status["current_index"] = 0
        _status["total"] = len(uncached_entries)
        _status.pop("error", None)

    def worker():
        try:
            run_graph(uncached_entries, _status)
        except Exception as e:
            with _lock:
                _status["running"] = False
                _status["error"] = str(e)

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    return jsonify({"ok": True, "total": len(uncached_entries), "from_cache": cached_count})


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
                analysis_prompt = (
                    f"A college course verification system gave an incorrect result.\n"
                    f"URL checked: {url}\n"
                    f"Courses verified: {result.get('courses_requested', [])}\n"
                    f"System result: {'Course Found' if result.get('course_found') else 'Course Not Found'}\n"
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
    courses = result.get("courses_requested", [])

    # Check the verified cache first — no need to crawl if we already know the answer
    cached = _cache_db.lookup(url, courses)
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
        "courses": courses,
        "course_synonyms": {},          # will be re-resolved if needed
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
