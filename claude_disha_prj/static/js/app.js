// ==================== Batch College Info Lookup ====================
const batchTextarea = document.getElementById("batch-colleges");
const batchLookupBtn = document.getElementById("batch-lookup-btn");
const batchLoading = document.getElementById("batch-loading");
const batchResultsSection = document.getElementById("batch-results");
const batchResultsContainer = document.getElementById("batch-results-container");

batchLookupBtn.addEventListener("click", async () => {
    const text = batchTextarea.value.trim();
    if (!text) {
        alert("Please enter at least one college name.");
        return;
    }

    const colleges = text.split("\n").map(s => s.trim()).filter(s => s.length > 0);
    if (colleges.length === 0) {
        alert("Please enter at least one college name.");
        return;
    }

    batchLookupBtn.disabled = true;
    batchLookupBtn.textContent = "Looking up...";
    batchLoading.classList.remove("hidden");
    batchResultsSection.classList.add("hidden");

    try {
        const resp = await fetch("/api/batch-college-info", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ colleges }),
        });
        const data = await resp.json();

        if (!resp.ok) {
            throw new Error(data.error || "Batch lookup failed");
        }

        renderBatchResults(data.results);
    } catch (err) {
        batchResultsSection.classList.remove("hidden");
        batchResultsContainer.innerHTML =
            `<div class="error-box"><h3>Something went wrong</h3><p>${escapeHTML(err.message)}</p></div>`;
    }

    batchLoading.classList.add("hidden");
    batchLookupBtn.disabled = false;
    batchLookupBtn.textContent = "Find College Info";
});

function renderBatchResults(results) {
    batchResultsSection.classList.remove("hidden");
    batchResultsContainer.innerHTML = "";

    if (!results || results.length === 0) {
        batchResultsContainer.innerHTML = '<p class="no-results">No results returned.</p>';
        return;
    }

    results.forEach(r => {
        const card = document.createElement("div");
        card.className = "course-card batch-card";

        if (!r.found) {
            card.classList.add("not_found");
            card.innerHTML = `
                <div class="course-card-header">
                    <span class="course-name">${escapeHTML(r.name)}</span>
                    <span class="status-badge not_found">Not Found</span>
                </div>
                <div class="course-details">
                    <div class="course-field" style="color:#999;">${escapeHTML(r.error || "Could not find website URL")}</div>
                </div>
            `;
        } else {
            // Render course verification badges if present
            let coursesHTML = "";
            if (r.courses_status && r.courses_status.length > 0) {
                coursesHTML = '<div class="course-status-list">';
                r.courses_status.forEach(cs => {
                    const badgeClass = cs.exists ? "course-exists" : "course-not-exists";
                    const badgeLabel = cs.exists ? "Exists" : "Not Exists";
                    coursesHTML += `<span class="${badgeClass}">${escapeHTML(cs.name)}: ${badgeLabel}</span>`;
                });
                coursesHTML += '</div>';
            }

            const fields = [
                ["Website", r.url ? `<a href="${escapeHTML(r.url)}" target="_blank">${escapeHTML(r.url)}</a>` : null],
                ["Contact", r.contact],
                ["Email", r.email],
                ["Address", r.address],
            ];

            let detailsHTML = "";
            for (const [label, value] of fields) {
                if (value) {
                    if (label === "Website") {
                        detailsHTML += `<div class="course-field"><strong>${label}:</strong> ${value}</div>`;
                    } else {
                        detailsHTML += `<div class="course-field"><strong>${label}:</strong> ${escapeHTML(value)}</div>`;
                    }
                }
            }

            if (r.error) {
                detailsHTML += `<div class="course-field" style="color:#c53030;"><strong>Note:</strong> ${escapeHTML(r.error)}</div>`;
            }

            if (!detailsHTML && !coursesHTML) {
                detailsHTML = '<div class="course-field" style="color:#999;">No contact details found.</div>';
            }

            card.innerHTML = `
                <div class="course-card-header">
                    <span class="course-name">${escapeHTML(r.name)}</span>
                    <span class="status-badge matched">Found</span>
                </div>
                ${coursesHTML}
                <div class="course-details">${detailsHTML}</div>
            `;
        }

        batchResultsContainer.appendChild(card);
    });
}

// ==================== Existing Course Finder ====================
const urlInput = document.getElementById("college-url");
const validateBtn = document.getElementById("validate-btn");
const urlStatus = document.getElementById("url-status");
const submitBtn = document.getElementById("submit-btn");
const coursesGroup = document.getElementById("courses-group");
const coursesInput = document.getElementById("courses-input");
const loadingSection = document.getElementById("loading");
const resultsSection = document.getElementById("results");
const errorSection = document.getElementById("error-section");
const errorMessage = document.getElementById("error-message");
const resultsTitle = document.getElementById("results-title");
const pagesCrawled = document.getElementById("pages-crawled");
const coursesFound = document.getElementById("courses-found");
const coursesContainer = document.getElementById("courses-container");
const saveStatus = document.getElementById("save-status");
const collegeNameInput = document.getElementById("college-name");
const searchCollegeBtn = document.getElementById("search-college-btn");
const collegeSearchStatus = document.getElementById("college-search-status");
const collegeSuggestions = document.getElementById("college-suggestions");

let lastResult = null;
let crawlAbortController = null;

// College name search
async function searchCollege() {
    const name = collegeNameInput.value.trim();
    if (name.length < 3) {
        collegeSearchStatus.className = "status-msg error";
        collegeSearchStatus.textContent = "Please enter at least 3 characters.";
        collegeSearchStatus.classList.remove("hidden");
        return;
    }

    searchCollegeBtn.disabled = true;
    searchCollegeBtn.textContent = "Searching...";
    collegeSearchStatus.className = "status-msg info";
    collegeSearchStatus.textContent = "Searching for college website...";
    collegeSearchStatus.classList.remove("hidden");
    collegeSuggestions.classList.add("hidden");

    try {
        const resp = await fetch("/api/lookup-college", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ college_name: name }),
        });
        const data = await resp.json();

        if (!resp.ok) {
            throw new Error(data.error || "Search failed");
        }

        if (data.found) {
            urlInput.value = data.url;
            submitBtn.disabled = false;
            const confidence = data.source === "edu_domain"
                ? "High confidence (.edu domain)"
                : "Best guess (non-.edu domain)";
            collegeSearchStatus.className = "status-msg success";
            collegeSearchStatus.textContent = `Found: ${data.url} — ${confidence}`;
        } else {
            collegeSearchStatus.className = "status-msg error";
            collegeSearchStatus.textContent = data.error || "Could not find a matching website.";
        }

        if (data.all_results && data.all_results.length > 0) {
            renderSuggestions(data.all_results);
        }
    } catch (err) {
        collegeSearchStatus.className = "status-msg error";
        collegeSearchStatus.textContent = `Search failed: ${err.message}`;
    }

    searchCollegeBtn.disabled = false;
    searchCollegeBtn.textContent = "Search";
}

function renderSuggestions(results) {
    collegeSuggestions.innerHTML = '<div class="suggestions-title">Alternative results (click to use)</div>';
    results.forEach((r) => {
        const item = document.createElement("div");
        item.className = "suggestion-item";
        const badgeClass = r.is_edu ? "domain-badge edu" : "domain-badge";
        item.innerHTML =
            `<span class="suggestion-title">${escapeHTML(r.title)}</span>` +
            `<span class="${badgeClass}">${escapeHTML(r.domain)}</span>`;
        item.addEventListener("click", () => {
            urlInput.value = r.url;
            submitBtn.disabled = false;
            collegeSearchStatus.className = "status-msg success";
            collegeSearchStatus.textContent = `Selected: ${r.url}`;
        });
        collegeSuggestions.appendChild(item);
    });
    collegeSuggestions.classList.remove("hidden");
}

searchCollegeBtn.addEventListener("click", searchCollege);

collegeNameInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
        e.preventDefault();
        searchCollege();
    }
});

// Toggle courses textarea based on mode
document.querySelectorAll('input[name="mode"]').forEach((radio) => {
    radio.addEventListener("change", (e) => {
        coursesGroup.style.display = e.target.value === "verify" ? "block" : "none";
    });
});

// Enable submit when URL is entered
urlInput.addEventListener("input", () => {
    submitBtn.disabled = !urlInput.value.trim();
});

// Validate URL
validateBtn.addEventListener("click", async () => {
    const url = urlInput.value.trim();
    if (!url) return;

    validateBtn.disabled = true;
    validateBtn.textContent = "Checking...";
    urlStatus.className = "status-msg info";
    urlStatus.textContent = "Validating URL...";
    urlStatus.classList.remove("hidden");

    try {
        const resp = await fetch("/api/validate-url", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url }),
        });
        const data = await resp.json();

        if (data.valid) {
            urlStatus.className = "status-msg success";
            urlStatus.textContent = `Valid! ${data.title || "Site is reachable"}`;
            submitBtn.disabled = false;
        } else {
            urlStatus.className = "status-msg error";
            urlStatus.textContent = data.error || `Site returned status ${data.status_code}`;
        }
    } catch (err) {
        urlStatus.className = "status-msg error";
        urlStatus.textContent = "Network error. Please check the URL.";
    }

    validateBtn.disabled = false;
    validateBtn.textContent = "Validate";
});

// Submit: crawl and extract courses
submitBtn.addEventListener("click", async () => {
    const url = urlInput.value.trim();
    const mode = document.querySelector('input[name="mode"]:checked').value;
    const courses = coursesInput.value.trim();

    if (!url) return;
    if (mode === "verify" && !courses) {
        alert("Please enter courses to verify.");
        return;
    }

    // Cancel any in-flight request
    if (crawlAbortController) {
        crawlAbortController.abort();
    }
    crawlAbortController = new AbortController();

    // Show loading, disable controls
    loadingSection.classList.remove("hidden");
    resultsSection.classList.add("hidden");
    errorSection.classList.add("hidden");
    removeWarningBanner();
    submitBtn.disabled = true;
    validateBtn.disabled = true;
    submitBtn.textContent = "Crawling...";

    try {
        const resp = await fetch("/api/crawl-courses", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ url, mode, courses }),
            signal: crawlAbortController.signal,
        });
        const data = await resp.json();

        if (!resp.ok) {
            throw new Error(data.error || "Failed to extract courses");
        }

        lastResult = data;
        renderResults(data);

        // Show LLM warning banner if any chunks failed
        if (data.llm_warnings && data.llm_warnings.length > 0) {
            showWarningBanner(data.llm_warnings);
        }
    } catch (err) {
        if (err.name === "AbortError") return; // user cancelled, ignore
        errorSection.classList.remove("hidden");
        errorMessage.textContent = err.message;
    }

    loadingSection.classList.add("hidden");
    submitBtn.disabled = false;
    validateBtn.disabled = false;
    submitBtn.textContent = "Find Courses";
    crawlAbortController = null;
});

function renderResults(data) {
    resultsSection.classList.remove("hidden");
    resultsTitle.textContent = data.college_name || "Results";
    pagesCrawled.textContent = `Pages crawled: ${data.pages_crawled}`;
    coursesContainer.innerHTML = "";

    // Handle college_info mode separately
    if (data.mode === "college_info") {
        coursesFound.textContent = "";
        const info = data.college_info;
        if (!info || (!info.address && !info.email && !info.contact)) {
            coursesContainer.innerHTML =
                '<p class="no-results">No college contact information found. Try a different URL.</p>';
            return;
        }
        const card = document.createElement("div");
        card.className = "course-card";
        const fields = [
            ["Address", info.address],
            ["Email", info.email],
            ["Contact", info.contact],
        ];
        let detailsHTML = "";
        for (const [label, value] of fields) {
            if (value) {
                detailsHTML += `<div class="course-field"><strong>${label}:</strong> ${escapeHTML(value)}</div>`;
            }
        }
        if (!detailsHTML) {
            detailsHTML = '<div class="course-field" style="color:#999;">No contact details found.</div>';
        }
        card.innerHTML = `
            <div class="course-card-header"><span class="course-name">${escapeHTML(info.name || data.college_name || "College Information")}</span></div>
            <div class="course-details">${detailsHTML}</div>
        `;
        coursesContainer.appendChild(card);
        return;
    }

    coursesFound.textContent = `Courses found: ${data.courses.length}`;

    if (data.courses.length === 0) {
        coursesContainer.innerHTML =
            '<p class="no-results">No courses found. Try a different URL or check if the site blocks automated access.</p>';
        return;
    }

    // Sort: matched first, then similar, then additional, then not_found
    const order = { matched: 0, similar: 1, additional: 2, not_found: 3, "": 2 };
    const sorted = [...data.courses].sort(
        (a, b) => (order[a.match_status] ?? 2) - (order[b.match_status] ?? 2)
    );

    sorted.forEach((course, idx) => {
        const card = createCourseCard(course, idx);
        coursesContainer.appendChild(card);
    });
}

function createCourseCard(course, index) {
    const card = document.createElement("div");
    card.className = `course-card ${course.match_status || ""}`;

    const statusLabels = {
        matched: "Matched",
        similar: "Similar",
        not_found: "Not Found",
        additional: "Additional",
    };

    let headerHTML = `<span class="course-name">${escapeHTML(course.name)}</span>`;
    if (course.match_status) {
        headerHTML += `<span class="status-badge ${course.match_status}">${statusLabels[course.match_status] || ""}</span>`;
    }

    const fields = [
        ["Details", course.details],
        ["Skills Needed", course.skills_needed],
        ["Entrance Exam", course.entrance_exam],
        ["Duration", course.duration],
        ["Fees", course.fees],
        ["Next Start Date", course.next_start_date],
        ["Other Info", course.other_info],
    ];

    let detailsHTML = "";
    for (const [label, value] of fields) {
        if (value) {
            detailsHTML += `<div class="course-field"><strong>${label}:</strong> ${escapeHTML(value)}</div>`;
        }
    }

    if (!detailsHTML) {
        detailsHTML = '<div class="course-field" style="color:#999;">No additional details available.</div>';
    }

    card.innerHTML = `
        <div class="course-card-header">${headerHTML}</div>
        <div class="course-details">${detailsHTML}</div>
        <div class="course-card-footer">
            <button class="btn btn-save" onclick="saveCourse(${index})">Save</button>
        </div>
    `;

    return card;
}

// Save all courses — commented out (Save All button removed)
// saveAllBtn.addEventListener("click", async () => {
//     if (!lastResult) return;
//     await saveCourses(null);
// });

// Save single course
async function saveCourse(index) {
    await saveCourses(index);
}

async function saveCourses(saveIndex) {
    if (!lastResult) return;

    saveStatus.classList.remove("hidden");
    saveStatus.className = "status-msg info";
    saveStatus.textContent = "Saving...";

    try {
        const resp = await fetch("/api/save", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                college_name: lastResult.college_name,
                courses: lastResult.courses,
                save_index: saveIndex,
            }),
        });
        const data = await resp.json();

        if (!resp.ok) {
            throw new Error(data.error);
        }

        saveStatus.className = "status-msg success";
        saveStatus.textContent = `Saved ${data.count} file(s) to output/drafts/`;
    } catch (err) {
        saveStatus.className = "status-msg error";
        saveStatus.textContent = `Save failed: ${err.message}`;
    }
}

function escapeHTML(str) {
    if (!str) return "";
    const div = document.createElement("div");
    div.textContent = str;
    return div.innerHTML;
}

function showWarningBanner(warnings) {
    removeWarningBanner();
    const banner = document.createElement("div");
    banner.id = "llm-warning-banner";
    banner.style.cssText =
        "background:#fff3cd;border:1px solid #ffc107;color:#856404;padding:12px 16px;border-radius:6px;margin-bottom:16px;";
    banner.innerHTML =
        "<strong>Warning:</strong> Some content chunks could not be processed (rate limit or API error). " +
        "The results below may be incomplete.<br><small>" +
        warnings.map(escapeHTML).join("<br>") +
        "</small>";
    resultsSection.insertBefore(banner, resultsSection.firstChild);
}

function removeWarningBanner() {
    const existing = document.getElementById("llm-warning-banner");
    if (existing) existing.remove();
}
