import re

CONFIG = {
    "domain": "india_colleges",
    "entity_name": "College",
    "attribute_name": "Course",
    "entity_placeholder": "https://christuniversity.in",
    "attribute_placeholder": "MBA, B.Sc Nursing, B.Tech",

    # Keywords for scraper link discovery
    "link_keywords": re.compile(
        r"course|programme|program|academic|department|faculty|"
        r"degree|curriculum|study|studies|admission|school|ug|pg|"
        r"undergrad|postgrad|master|bachelor|doctoral|phd|"
        r"nursing|medical|medicine|pharmacy|dental|health|paramedical|"
        r"physiotherapy|occupational|allied|science|arts|commerce|law|"
        r"engineering|technology|management|education|architecture",
        re.IGNORECASE,
    ),

    # Keywords for Playwright to prioritise relevant URLs
    "priority_url_keywords": re.compile(
        r"programme|course|engineering|mtech|pg|postgraduate|school|"
        r"institute|department|faculty|admission|nursing|medical|medicine|"
        r"pharmacy|dental|health|science|arts|commerce|law|management|"
        r"education|architecture|technology",
        re.IGNORECASE,
    ),

    # URL fragments to skip during crawl
    "skip_url_patterns": re.compile(
        r"/faculty/|/news/|/event/|/gallery/|/blog|/alumni/"
        r"|/doctor|/physician|/staff|/search|/find-a|/directory|/profile/",
        re.IGNORECASE,
    ),

    # LLM prompt for verifying whether an attribute exists on the page
    "verify_prompt": (
        "You are checking whether a college OFFERS specific courses for enrollment.\n"
        "Use ONLY the text provided below. Do NOT use any prior knowledge.\n"
        "{extra_context}"
        "RULES FOR SAYING YES:\n"
        "A course is offered only if the text shows the course name OR any of its listed synonyms\n"
        "AND at least one of these supporting details for that course:\n"
        "  - fees or fee structure\n"
        "  - duration or number of years/semesters\n"
        "  - eligibility or admission criteria\n"
        "  - curriculum, syllabus, or subjects taught\n"
        "  - a dedicated department or school that runs it\n"
        "  - skills or career outcomes it leads to\n\n"
        "RULES FOR SAYING NO:\n"
        "  - The name appears only in passing (faculty bio, research mention, comparison)\n"
        "  - You are not sure — when in doubt, say No\n\n"
        "Courses to verify (check for the name OR any of its synonyms):\n{attributes_block}\n\n"
        "Website text:\n{html}\n\n"
        "Start your answer with 'Yes' if any course is confirmed offered, else 'No'.\n"
        "Then briefly state which course(s) and what evidence you found.\n"
        "Example: Yes — B.Sc Psychology: dedicated department page with 3-year duration listed."
    ),

    # LLM prompt for Playwright deep-verification
    "playwright_verify_prompt": (
        "You are checking whether a university offers a specific course or programme.\n\n"
        "Target: \"{target}\"\n\n"
        "Also accept closely related name variants:\n{variants_bullet}\n\n"
        "The text below was extracted from the university website at: {url}\n\n"
        "Page text:\n{text}\n\n"
        "Answer with EXACTLY this format:\n"
        "VERDICT: FOUND | NOT_FOUND | POSSIBLY_FOUND\n"
        "EVIDENCE: <quote the exact text from the page, or \"none\" if not found>\n"
        "NOTES: <any clarifying notes>"
    ),

    # Whether to expand abbreviations into synonyms before searching
    "use_synonyms": True,
    "synonyms_context": "Indian university degrees (UGC/AICTE/NMC/INC/BCI regulated)",

    # Common contact page paths to probe when link scanner finds nothing
    "contact_paths": [
        "/contact-us", "/contact", "/contactus", "/contact.php",
        "/contact-us.php", "/about/contact", "/about-us/contact",
        "/about/contact-us", "/reach-us", "/enquiry",
        "/get-in-touch", "/campus/contact", "/university/contact",
    ],
}
