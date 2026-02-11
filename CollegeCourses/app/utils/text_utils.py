import re
from typing import List, Optional

# Patterns that match college/university names
COLLEGE_PATTERNS = [
    # "University of X" / "The University of X"
    re.compile(
        r"\b(?:The\s+)?University\s+of\s+(?:[A-Z][a-z]+(?:\s+(?:at|in|of)\s+)?){1,4}(?:[A-Z][a-z]+)",
        re.UNICODE,
    ),
    # "X University" / "X State University" (1-4 leading words)
    re.compile(
        r"\b(?:[A-Z][a-z]+(?:\s+(?:and|&)\s+)?){1,4}\s+(?:State\s+)?University\b",
        re.UNICODE,
    ),
    # "X College" / "X Community College" (1-4 leading words)
    re.compile(
        r"\b(?:[A-Z][a-z]+(?:\s+(?:and|&)\s+)?){1,4}\s+(?:Community\s+)?College\b",
        re.UNICODE,
    ),
    # "X Institute of Technology/Science" (1-3 leading words)
    re.compile(
        r"\b(?:[A-Z][a-z]+\s+){1,3}Institute\s+of\s+(?:Technology|Science|Art|Design)\b",
        re.UNICODE,
    ),
    # "X Academy" / "X Seminary" / "X Conservatory" (1-3 leading words)
    re.compile(
        r"\b(?:[A-Z][a-z]+\s+){1,3}(?:Academy|Seminary|Conservatory)\b",
        re.UNICODE,
    ),
]

# Well-known abbreviations mapping
KNOWN_ABBREVIATIONS = {
    "MIT": "Massachusetts Institute of Technology",
    "UCLA": "University of California, Los Angeles",
    "USC": "University of Southern California",
    "NYU": "New York University",
    "CMU": "Carnegie Mellon University",
    "UCSD": "University of California, San Diego",
    "UCSB": "University of California, Santa Barbara",
    "UCI": "University of California, Irvine",
    "UCR": "University of California, Riverside",
    "UCSF": "University of California, San Francisco",
    "UCD": "University of California, Davis",
    "UCSC": "University of California, Santa Cruz",
    "UCB": "University of California, Berkeley",
    "UNLV": "University of Nevada, Las Vegas",
    "UNC": "University of North Carolina at Chapel Hill",
    "UVA": "University of Virginia",
    "UGA": "University of Georgia",
    "LSU": "Louisiana State University",
    "OSU": "Ohio State University",
    "PSU": "Pennsylvania State University",
    "ASU": "Arizona State University",
    "FSU": "Florida State University",
    "MSU": "Michigan State University",
    "TAMU": "Texas A&M University",
    "RPI": "Rensselaer Polytechnic Institute",
    "WPI": "Worcester Polytechnic Institute",
    "RIT": "Rochester Institute of Technology",
    "GIT": "Georgia Institute of Technology",
    "VT": "Virginia Tech",
    "GT": "Georgia Tech",
    "UIUC": "University of Illinois Urbana-Champaign",
    "UMD": "University of Maryland",
    "UMASS": "University of Massachusetts",
    "UMICH": "University of Michigan",
    "UPENN": "University of Pennsylvania",
    "UCONN": "University of Connecticut",
}

# Abbreviation pattern: all-caps 2-5 letters that are known
ABBREVIATION_PATTERN = re.compile(r"\b([A-Z]{2,6})\b")


def extract_college_names(text: str) -> List[str]:
    """Extract potential college/university names from text using regex patterns."""
    candidates = set()

    # Pattern-based extraction
    for pattern in COLLEGE_PATTERNS:
        for match in pattern.finditer(text):
            name = match.group(0).strip()
            if len(name) > 5:  # Filter very short false positives
                candidates.add(name)

    # Abbreviation-based extraction
    for match in ABBREVIATION_PATTERN.finditer(text):
        abbr = match.group(1)
        if abbr in KNOWN_ABBREVIATIONS:
            candidates.add(KNOWN_ABBREVIATIONS[abbr])

    return list(candidates)


def clean_text(html_text: str) -> str:
    """Clean extracted text by collapsing whitespace and stripping."""
    text = re.sub(r"\s+", " ", html_text)
    return text.strip()


def extract_context(text: str, college_name: str, window: int = 150) -> Optional[str]:
    """Extract a context snippet around the first occurrence of a college name."""
    idx = text.lower().find(college_name.lower())
    if idx == -1:
        return None
    start = max(0, idx - window)
    end = min(len(text), idx + len(college_name) + window)
    snippet = text[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(text):
        snippet = snippet + "..."
    return snippet
