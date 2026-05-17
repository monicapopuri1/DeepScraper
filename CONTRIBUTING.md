# Contributing to College Course Finder

Thank you for helping improve this tool for students and parents across India!

---

## Ways to Contribute

### 1. Report a Wrong Result
If the tool gives an incorrect answer for a college + course combination:
1. Open an [Issue](https://github.com/monicapopuri1/DeepScraper/issues)
2. Include the college URL, course name, what the tool returned, and what the correct answer is
3. A link to the page on the college website that confirms the correct answer is very helpful

### 2. Add a College URL
The tool works best when given the right URL. If you know the correct URL for a college's course listings page (not just the homepage), open an issue or PR with:
- College name
- State
- Homepage URL
- Direct course catalog URL (if different from homepage)

### 3. Fix a Bug or Add a Feature
1. Fork the repository
2. Create a branch: `git checkout -b fix/your-fix-name`
3. Make your changes
4. Test locally (see [How to Run](README.md#how-to-run))
5. Open a Pull Request with a clear description of what you changed and why

### 4. Improve Course Synonyms
Indian degrees have many abbreviations and alternate spellings. If the tool misses a course because of a naming variant, open an issue describing:
- The course name as typed
- The variant used on the college website
- The state/university board it belongs to

---

## Development Setup

```bash
git clone https://github.com/monicapopuri1/DeepScraper.git
cd DeepScraper
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install chromium
cp .env.example .env
# Add your ANTHROPIC_API_KEY to .env
python app.py
```

---

## Code Structure

| File | Purpose |
|---|---|
| `app.py` | Flask server, REST API routes, parallel worker management |
| `graph.py` | LangGraph workflow — all pipeline nodes and routing logic |
| `scraper.py` | Fast HTTP scraper using requests + BeautifulSoup |
| `playwright_crawler.py` | Deep JS-rendering crawler using Playwright + Claude |
| `cache.py` | SQLite cache for verified results and learned error patterns |
| `templates/index.html` | Frontend UI |

---

## Guidelines

- **Do not commit `.env`** — it contains your API key
- **Do not commit `*.db` files** — these are local crawl caches
- Keep pull requests focused — one fix or feature per PR
- If adding a new dependency, update `requirements.txt`
- Test with at least 3 colleges before submitting a PR

---

## Disclaimer

This tool scrapes publicly accessible college websites for informational purposes. Please respect each website's `robots.txt` and terms of service. Do not use this tool for bulk commercial data collection.
