"""
cache.py — Verified-results cache + learned-patterns store.

verified_results : rows confirmed correct by the user (right / partially_right).
                   Looked up before crawling so we never re-search a known answer.
learned_patterns : notes on what went wrong for a domain so future retries are smarter.
"""

import json
import sqlite3
from datetime import datetime, timezone
from urllib.parse import urlparse

CACHE_DB = "verified_results.db"


class CacheDB:
    def __init__(self, db_path: str = CACHE_DB):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self._init()

    def _init(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS verified_results (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                url                  TEXT    NOT NULL,
                courses_json         TEXT    NOT NULL,
                course_found         INTEGER NOT NULL DEFAULT 0,
                contact              TEXT    DEFAULT '',
                email                TEXT    DEFAULT '',
                address              TEXT    DEFAULT '',
                playwright_evidence  TEXT    DEFAULT '',
                playwright_source_url TEXT   DEFAULT '',
                feedback             TEXT    NOT NULL,
                user_notes           TEXT    DEFAULT '',
                verified_at          TEXT    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS learned_patterns (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                url_domain    TEXT NOT NULL,
                issue_type    TEXT NOT NULL,
                user_feedback TEXT NOT NULL,
                llm_analysis  TEXT DEFAULT '',
                created_at    TEXT NOT NULL
            );
        """)
        self.conn.commit()

    # ------------------------------------------------------------------
    # verified_results
    # ------------------------------------------------------------------

    def save_result(self, result: dict, feedback: str, user_notes: str = ""):
        """Persist a user-confirmed result so it can be served from cache later."""
        courses = sorted(result.get("courses_requested", []))
        self.conn.execute("""
            INSERT INTO verified_results
              (url, courses_json, course_found, contact, email, address,
               playwright_evidence, playwright_source_url, feedback, user_notes, verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            result.get("url", ""),
            json.dumps(courses),
            1 if result.get("course_found") else 0,
            result.get("contact", ""),
            result.get("email", ""),
            result.get("address", ""),
            result.get("playwright_evidence", ""),
            result.get("playwright_source_url", ""),
            feedback,
            user_notes,
            datetime.now(timezone.utc).isoformat(),
        ))
        self.conn.commit()

    def lookup(self, url: str, courses: list) -> dict | None:
        """Return a cached result for this url + course list, or None."""
        courses_json = json.dumps(sorted(courses))
        row = self.conn.execute("""
            SELECT url, courses_json, course_found, contact, email, address,
                   playwright_evidence, playwright_source_url, feedback, user_notes
            FROM verified_results
            WHERE url = ? AND courses_json = ?
            ORDER BY verified_at DESC LIMIT 1
        """, (url, courses_json)).fetchone()
        if row:
            return {
                "url": row[0],
                "courses_requested": json.loads(row[1]),
                "course_found": bool(row[2]),
                "contact": row[3],
                "email": row[4],
                "address": row[5],
                "playwright_evidence": row[6],
                "playwright_source_url": row[7],
                "feedback": row[8],
                "user_notes": row[9],
                "from_cache": True,
                "status": "done",
                "error": "",
            }
        return None

    # ------------------------------------------------------------------
    # learned_patterns
    # ------------------------------------------------------------------

    def save_pattern(self, url: str, issue_type: str, user_feedback: str,
                     llm_analysis: str = ""):
        """Record what went wrong for a domain so future runs can avoid the same mistake."""
        domain = urlparse(url).netloc
        self.conn.execute("""
            INSERT INTO learned_patterns (url_domain, issue_type, user_feedback, llm_analysis, created_at)
            VALUES (?, ?, ?, ?, ?)
        """, (domain, issue_type, user_feedback, llm_analysis,
              datetime.now(timezone.utc).isoformat()))
        self.conn.commit()

    def get_patterns(self, url: str) -> list[dict]:
        """Return the most recent learned patterns for the domain of this URL."""
        domain = urlparse(url).netloc
        rows = self.conn.execute("""
            SELECT issue_type, user_feedback, llm_analysis
            FROM learned_patterns
            WHERE url_domain = ?
            ORDER BY created_at DESC LIMIT 5
        """, (domain,)).fetchall()
        return [{"issue_type": r[0], "user_feedback": r[1], "llm_analysis": r[2]}
                for r in rows]
