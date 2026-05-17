import re

CONFIG = {
    "domain": "hospitals",
    "entity_name": "Hospital",
    "attribute_name": "Treatment",
    "entity_placeholder": "https://manipalhospitals.com",
    "attribute_placeholder": "Cardiac Surgery, Oncology, Knee Replacement",

    "link_keywords": re.compile(
        r"treatment|specialty|speciality|department|service|therapy|"
        r"cardiology|orthopedic|oncology|neurology|surgery|diagnostic|"
        r"procedure|care|unit|centre|center|clinic|facility",
        re.IGNORECASE,
    ),

    "priority_url_keywords": re.compile(
        r"treatment|specialty|speciality|department|cardio|ortho|cancer|"
        r"neuro|surgery|therapy|diagnostic|procedure|clinic|facility",
        re.IGNORECASE,
    ),

    "skip_url_patterns": re.compile(
        r"/news/|/event/|/gallery/|/blog|/careers|/jobs|/media|/press|/alumni/",
        re.IGNORECASE,
    ),

    "verify_prompt": (
        "You are checking whether a hospital PROVIDES specific medical treatments or services.\n"
        "Use ONLY the text provided below. Do NOT use any prior knowledge.\n"
        "{extra_context}"
        "RULES FOR SAYING YES:\n"
        "A treatment is provided only if the text explicitly mentions it AND shows at least one of:\n"
        "  - a dedicated department or specialty unit for it\n"
        "  - specialist doctors or surgeons for that treatment\n"
        "  - procedures, technology, or equipment used\n"
        "  - patient information or appointment booking for it\n\n"
        "RULES FOR SAYING NO:\n"
        "  - The treatment appears only in passing\n"
        "  - You are not sure — when in doubt, say No\n\n"
        "Treatments to verify:\n{attributes_block}\n\n"
        "Website text:\n{html}\n\n"
        "Start your answer with 'Yes' if any treatment is confirmed provided, else 'No'.\n"
        "Then briefly state which treatment(s) and what evidence you found."
    ),

    "playwright_verify_prompt": (
        "You are checking whether a hospital provides a specific medical treatment.\n\n"
        "Target: \"{target}\"\n\n"
        "Also accept closely related name variants:\n{variants_bullet}\n\n"
        "The text below was extracted from the hospital website at: {url}\n\n"
        "Page text:\n{text}\n\n"
        "Answer with EXACTLY this format:\n"
        "VERDICT: FOUND | NOT_FOUND | POSSIBLY_FOUND\n"
        "EVIDENCE: <quote the exact text from the page, or \"none\" if not found>\n"
        "NOTES: <any clarifying notes>"
    ),

    "use_synonyms": False,
    "synonyms_context": "",

    "contact_paths": [
        "/contact", "/contact-us", "/reach-us", "/appointments",
        "/book-appointment", "/location", "/about/contact", "/find-us",
    ],
}
