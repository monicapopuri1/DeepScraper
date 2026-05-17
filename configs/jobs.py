import re

CONFIG = {
    "domain": "jobs",
    "entity_name": "Company",
    "attribute_name": "Job Role",
    "entity_placeholder": "https://careers.infosys.com",
    "attribute_placeholder": "Data Scientist, Python Developer, Product Manager",

    "link_keywords": re.compile(
        r"career|job|hiring|vacancy|recruitment|opening|position|role|"
        r"opportunity|apply|application|talent|people|team|join|work",
        re.IGNORECASE,
    ),

    "priority_url_keywords": re.compile(
        r"career|job|hiring|vacancy|recruitment|opening|position|apply",
        re.IGNORECASE,
    ),

    "skip_url_patterns": re.compile(
        r"/news/|/event/|/gallery/|/blog|/press|/media|/investor|/about/history",
        re.IGNORECASE,
    ),

    "verify_prompt": (
        "You are checking whether a company is currently HIRING for specific job roles.\n"
        "Use ONLY the text provided below. Do NOT use any prior knowledge.\n"
        "{extra_context}"
        "RULES FOR SAYING YES:\n"
        "A job role is available only if the text explicitly lists it as an open position AND shows:\n"
        "  - a job title matching the role\n"
        "  - job description, responsibilities, or requirements\n"
        "  - an apply link or application process\n\n"
        "RULES FOR SAYING NO:\n"
        "  - The role appears only as a general mention (e.g. 'we hire engineers')\n"
        "  - No specific open position is listed\n"
        "  - You are not sure — when in doubt, say No\n\n"
        "Job roles to verify:\n{attributes_block}\n\n"
        "Website text:\n{html}\n\n"
        "Start your answer with 'Yes' if any role is confirmed open, else 'No'.\n"
        "Then briefly state which role(s) and what evidence you found."
    ),

    "playwright_verify_prompt": (
        "You are checking whether a company is hiring for a specific job role.\n\n"
        "Target: \"{target}\"\n\n"
        "Also accept closely related name variants:\n{variants_bullet}\n\n"
        "The text below was extracted from the company website at: {url}\n\n"
        "Page text:\n{text}\n\n"
        "Answer with EXACTLY this format:\n"
        "VERDICT: FOUND | NOT_FOUND | POSSIBLY_FOUND\n"
        "EVIDENCE: <quote the exact text from the page, or \"none\" if not found>\n"
        "NOTES: <any clarifying notes>"
    ),

    "use_synonyms": False,
    "synonyms_context": "",

    "contact_paths": [
        "/contact", "/contact-us", "/careers/contact", "/about/contact",
        "/locations", "/offices",
    ],
}
