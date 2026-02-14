import logging
from flask import Blueprint, request, jsonify, render_template

from crawler.site_crawler import validate_url, crawl_site
from crawler.utils import normalize_url
from crawler.college_lookup import lookup_college_url
from extractor.llm_extractor import extract_all_courses, verify_courses, extract_college_info
from extractor.models import CourseInfo
from api.markdown_writer import save_all_courses, save_course

logger = logging.getLogger(__name__)
bp = Blueprint("api", __name__)


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/api/lookup-college", methods=["POST"])
def api_lookup_college():
    data = request.get_json()
    college_name = data.get("college_name", "").strip()
    if not college_name or len(college_name) < 3:
        return jsonify({"error": "College name must be at least 3 characters"}), 400

    result = lookup_college_url(college_name)
    return jsonify(result)


@bp.route("/api/validate-url", methods=["POST"])
def api_validate_url():
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    result = validate_url(url)
    return jsonify(result)


@bp.route("/api/crawl-courses", methods=["POST"])
def api_crawl_courses():
    data = request.get_json()
    url = data.get("url", "").strip()
    mode = data.get("mode", "get_all")  # "get_all" or "verify"
    user_courses_raw = data.get("courses", "").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400

    if mode == "verify" and not user_courses_raw:
        return jsonify({"error": "Course list is required for verify mode"}), 400

    url = normalize_url(url)

    # Parse user courses (one per line or comma-separated)
    user_courses = []
    if user_courses_raw:
        for line in user_courses_raw.replace(",", "\n").splitlines():
            line = line.strip()
            if line:
                user_courses.append(line)

    # Crawl the website
    logger.info(f"Starting crawl of {url}")
    crawl_result = crawl_site(url, user_courses)

    # Determine if Playwright fallback is needed
    fallback_reason = None
    if crawl_result.get("js_detected"):
        fallback_reason = "JS-rendered site detected"
    elif crawl_result.get("cloudflare_detected"):
        fallback_reason = "Cloudflare/bot challenge detected"
    elif not crawl_result["pages_content"]:
        fallback_reason = "no content extracted"
    elif crawl_result["pages_crawled"] > 1 and crawl_result.get("main_page_length", 0) < 500:
        fallback_reason = f"main page suspiciously short ({crawl_result['main_page_length']} chars)"

    if fallback_reason:
        try:
            from crawler.playwright_crawler import crawl_with_playwright
            logger.info(f"Trying Playwright fallback ({fallback_reason})")
            pw_result = crawl_with_playwright(url)
            if pw_result["pages_content"]:
                crawl_result = pw_result
                crawl_result["used_playwright"] = True
            elif not crawl_result["pages_content"]:
                return jsonify({
                    "error": "Could not extract content from this website. The site may be blocking automated access.",
                    "pages_crawled": crawl_result["pages_crawled"],
                    "errors": crawl_result["errors"],
                }), 422
        except Exception as e:
            logger.warning(f"Playwright fallback failed: {e}")
            if not crawl_result["pages_content"]:
                return jsonify({
                    "error": "Could not extract content from this website. The site may be blocking automated access.",
                    "pages_crawled": crawl_result["pages_crawled"],
                    "errors": crawl_result["errors"],
                }), 422

    # Extract courses using LLM
    logger.info(f"Extracting courses from {len(crawl_result['pages_content'])} pages")

    if mode == "college_info":
        college_info, llm_warnings = extract_college_info(crawl_result["pages_content"])
        response = {
            "college_name": college_info.get("name") or crawl_result["college_name"],
            "college_url": url,
            "pages_crawled": crawl_result["pages_crawled"],
            "mode": mode,
            "college_info": college_info,
            "errors": crawl_result["errors"][:5],
        }
    else:
        if mode == "verify":
            courses, llm_warnings = verify_courses(crawl_result["pages_content"], user_courses)
        else:
            courses, llm_warnings = extract_all_courses(crawl_result["pages_content"])

        response = {
            "college_name": crawl_result["college_name"],
            "college_url": url,
            "pages_crawled": crawl_result["pages_crawled"],
            "mode": mode,
            "courses": [c.to_dict() for c in courses],
            "errors": crawl_result["errors"][:5],
        }

    if llm_warnings:
        response["llm_warnings"] = llm_warnings

    return jsonify(response)


@bp.route("/api/batch-college-info", methods=["POST"])
def api_batch_college_info():
    data = request.get_json()
    colleges = data.get("colleges", [])
    if not colleges:
        return jsonify({"error": "No college names provided"}), 400

    results = []
    for entry in colleges:
        entry = entry.strip()
        if not entry:
            continue

        # Parse optional courses: "College Name | Course1, Course2"
        course_list = []
        if "|" in entry:
            name, courses_part = entry.split("|", 1)
            name = name.strip()
            course_list = [c.strip() for c in courses_part.split(",") if c.strip()]
        else:
            name = entry

        # Look up the college URL
        lookup = lookup_college_url(name)
        if not lookup.get("found"):
            results.append({
                "name": name,
                "found": False,
                "error": lookup.get("error", "Could not find website URL"),
            })
            continue

        url = lookup["url"]
        try:
            crawl_result = crawl_site(url, [])
            if not crawl_result["pages_content"]:
                results.append({
                    "name": name,
                    "found": True,
                    "url": url,
                    "contact": None,
                    "email": None,
                    "address": None,
                    "error": "Could not extract content from website",
                })
                continue

            info, _ = extract_college_info(crawl_result["pages_content"])

            result_entry = {
                "name": name,
                "found": True,
                "url": url,
                "contact": info.get("contact"),
                "email": info.get("email"),
                "address": info.get("address"),
            }

            # Verify courses if specified
            if course_list:
                courses, _ = verify_courses(crawl_result["pages_content"], course_list)
                courses_status = []
                for course in courses:
                    cd = course.to_dict()
                    exists = cd.get("match_status") in ("matched", "similar")
                    courses_status.append({"name": cd["name"], "exists": exists})
                result_entry["courses_status"] = courses_status

            results.append(result_entry)
        except Exception as e:
            logger.error(f"Error processing {name}: {e}")
            results.append({
                "name": name,
                "found": True,
                "url": url,
                "contact": None,
                "email": None,
                "address": None,
                "error": str(e),
            })

    return jsonify({"results": results})


@bp.route("/api/save", methods=["POST"])
def api_save():
    data = request.get_json()
    college_name = data.get("college_name", "Unknown College")
    courses_data = data.get("courses", [])

    if not courses_data:
        return jsonify({"error": "No courses to save"}), 400

    courses = [CourseInfo.from_dict(c) for c in courses_data]

    # Save single course or all
    save_index = data.get("save_index")  # None = save all
    if save_index is not None:
        if 0 <= save_index < len(courses):
            path = save_course(courses[save_index], college_name)
            return jsonify({"saved": [path], "count": 1})
        return jsonify({"error": "Invalid course index"}), 400

    paths = save_all_courses(courses, college_name)
    return jsonify({"saved": paths, "count": len(paths)})
