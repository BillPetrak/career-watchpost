"use strict";

(function () {
  // CareerOS read-only dashboard.
  //
  // This file contains ONLY presentation and local navigation/loading state.
  // It calls the documented Home endpoint (GET /report), the Jobs listing
  // (GET /jobs), -- for the Review view -- the computation-only
  // POST /report after the user submits the candidate-profile form, -- for
  // the Job Detail view (#job/<id>) -- exactly five canonical read GET
  // endpoints (GET /jobs/{id}, GET /jobs/{id}/group, GET /applications/{id},
  // GET /review-decisions/{id}, GET /reminders?job_id={id}), and -- for the
  // Review section actions -- the two review-decision mutations
  // PUT /review-decisions/{id} and DELETE /review-decisions/{id}. API-provided
  // strings are always rendered with textContent/createElement -- never as
  // HTML. The submitted profile is sent only to POST /report and is never
  // stored by this frontend.

  var HOME = "home";
  var JOBS = "jobs";
  var REVIEW = "review";
  var JOB = "job";
  var initialized = false;

  // In-memory origin for the Job Detail Back link: remembers whether the
  // detail view was opened from Jobs or Review so the Back link prefers that
  // origin. Never persisted to storage; defaults to Jobs. Browser Back/Forward
  // keeps working naturally because every navigation is a real hash change.
  var jobDetailOrigin = JOBS;

  // Stale-response guard for the Job Detail view: every full render of
  // #job/<id> increments this token, and every async operation that touches the
  // detail DOM (initial load, section retry, review-decision refresh) captures
  // it before awaiting so a response for an older page can never overwrite the
  // currently visible job.
  var detailRequestSeq = 0;

  // Serializes the Review decision actions: while a mutation (PUT/DELETE) and
  // its immediate refetch are in flight, no second mutation may start for the
  // same Review section. Buttons are disabled for the whole operation as well,
  // so this is defense-in-depth against programmatic/edge re-entry.
  var reviewOperationActive = false;

  // ---------------------------------------------------------------------------
  // Navigation (semantic hash links: #home, #jobs, #review; unknown hashes ->
  // home). Non-view hashes such as the #main skip-link anchor never change the
  // view or reset loaded state, so activating the skip link while on Review
  // keeps Review.
  // ---------------------------------------------------------------------------

  function parsedHash() {
    var raw = window.location.hash.replace(/^#\/?/, "").trim().toLowerCase();
    if (raw === JOBS) return JOBS;
    if (raw === REVIEW) return REVIEW;
    if (raw === HOME) return HOME;
    return null; // unknown / absent / non-view (e.g. #main)
  }

  // Parses the #job/<positive-integer-id> route. Returns the job id (>= 1) for
  // valid routes and null for absent / malformed ids (#job, #job/0, #job/-1,
  // #job/abc, #job/12/foo, ...), which are treated exactly like unknown hashes.
  function parsedJobId() {
    var raw = window.location.hash.replace(/^#\/?/, "").trim().toLowerCase();
    var match = /^job\/(\d+)$/.exec(raw);
    if (!match) return null;
    var id = parseInt(match[1], 10);
    return id >= 1 ? id : null;
  }

  function showView(name) {
    var sections = document.querySelectorAll(".view[data-view]");
    sections.forEach(function (section) {
      section.hidden = section.getAttribute("data-view") !== name;
    });
    document.querySelectorAll(".nav a[href^='#']").forEach(function (link) {
      if (link.getAttribute("href") === "#" + name) {
        link.setAttribute("aria-current", "page");
      } else {
        link.removeAttribute("aria-current");
      }
    });
  }

  function visibleView() {
    var visible = document.querySelector(".view[data-view]:not([hidden])");
    return visible ? visible.getAttribute("data-view") : null;
  }

  async function navigate() {
    var jobId = parsedJobId();
    var view = parsedHash();
    if (jobId !== null) {
      view = JOB;
    }
    if (view === null && !initialized) {
      // Initial load with an absent / unknown hash: default to Home and render.
      view = HOME;
    }
    initialized = true;
    var name = view || visibleView() || HOME;
    var leaving = visibleView();
    showView(name);
    // Only re-render when a real view route is set; otherwise (e.g. the #main
    // skip-link anchor) keep the currently visible, already-rendered view.
    if (view === REVIEW) {
      // The Review view renders its static form and never auto-submits; clear
      // any prior results so nothing stale is shown when the view is entered.
      resetReviewResults();
    } else if (view === JOBS) {
      if (leaving === REVIEW) resetReviewResults();
      await renderJobs();
    } else if (view === HOME) {
      if (leaving === REVIEW) resetReviewResults();
      await renderHome();
    } else if (view === JOB) {
      if (leaving === REVIEW) resetReviewResults();
      if (leaving === JOBS || leaving === REVIEW) {
        jobDetailOrigin = leaving;
      }
      await renderJobDetail(jobId);
    }
    document.getElementById("main").scrollTop = 0;
  }

  // ---------------------------------------------------------------------------
  // DOM helpers (safe: no innerHTML with API data)
  // ---------------------------------------------------------------------------

  function el(tagName, options) {
    var node = document.createElement(tagName);
    if (!options) return node;
    if (options.textContent !== undefined) {
      node.textContent = options.textContent;
    }
    if (options.className) node.className = options.className;
    if (options.id) node.id = options.id;
    if (options.href) node.href = options.href;
    if (options.target) node.target = options.target;
    if (options.rel) node.setAttribute("rel", options.rel);
    if (options.label) node.setAttribute("aria-label", options.label);
    if (options.type) node.type = options.type;
    return node;
  }

  function clearText(node) {
    node.textContent = "";
  }

  // ---------------------------------------------------------------------------
  // HTTP (same-origin fetch only; no external services, no secrets)
  // ---------------------------------------------------------------------------

  async function fetchJson(url) {
    var response = await fetch(url, { headers: { Accept: "application/json" } });
    if (!response.ok) {
      var error = await readHttpError(url, response);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  // The Review view's single computation-only request: POST the inline
  // candidate profile to GET /report's POST sibling. Same-origin only, and the
  // profile is never persisted or logged by this frontend.
  async function postReportJson(url, body) {
    var response;
    try {
      response = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "Accept": "application/json",
        },
        body: JSON.stringify(body),
      });
    } catch (_networkError) {
      throw new Error("Could not reach the CareerOS API. Check that it is running, then retry.");
    }
    if (!response.ok) {
      throw await readHttpError(url, response);
    }
    return response.json();
  }

  // Generic same-origin JSON write helper used by the Job Detail Review
  // actions. Network failures and non-2xx responses follow the same
  // friendly-error treatment as the read/POST helpers; the caller owns the
  // refetch. Only the review-decision PUT/DELETE are routed here -- the Review
  // view's POST stays on postReportJson.
  async function writeJson(url, options) {
    var response;
    try {
      response = await fetch(url, options);
    } catch (_networkError) {
      throw new Error("Could not reach the CareerOS API. Check that it is running, then retry.");
    }
    if (!response.ok) {
      var error = await readHttpError(url, response);
      error.status = response.status;
      throw error;
    }
    return response.json();
  }

  // Thin adapters that carry the exact mutation method for the Review section.
  function putJson(url, body) {
    return writeJson(url, {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
      },
      body: JSON.stringify(body),
    });
  }

  function deleteJson(url) {
    return writeJson(url, {
      method: "DELETE",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
      },
    });
  }

  async function readHttpError(url, response) {
    var message = "Request to " + url + " failed (HTTP " + response.status + ").";
    try {
      var payload = await response.json();
      if (
        isObject(payload) &&
        typeof payload.detail === "string" &&
        payload.detail.length > 0
      ) {
        message += " " + payload.detail;
      }
    } catch (_notJson) {
      // Non-JSON error body: keep the generic HTTP message.
    }
    return new Error(message);
  }

  function isObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }

  function isFiniteNumber(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function countValue(value) {
    return isFiniteNumber(value) ? value : 0;
  }

  function displayDate(value) {
    var text = typeof value === "string" ? value : "";
    var datePart = text.split("T")[0];
    return datePart || "—";
  }

  function displayText(value) {
    return typeof value === "string" && value.length > 0 ? value : "—";
  }

  // Safe external job link: only http(s) URLs become links; everything else is
  // rendered as plain text so untrusted values are never executable markup.
  function safeExternalLink(url, text) {
    if (typeof url !== "string" || url.length === 0) return null;
    var parsed = null;
    try {
      parsed = new URL(url, window.location.origin);
    } catch (_error) {
      return null;
    }
    if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
      return null;
    }
    return el("a", {
      className: "job-link",
      href: parsed.href,
      target: "_blank",
      rel: "noopener noreferrer",
      textContent: typeof text === "string" && text.length > 0 ? text : "Open job",
    });
  }

  // The two per-card actions: a same-origin "View details" hash link into the
  // Job Detail view and the existing safe "Open original" external link. Both
  // are only added when a usable value exists.
  function jobDetailsLink(job) {
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return null;
    return el("a", {
      className: "job-link",
      href: "#job/" + job.id,
      textContent: "View details",
    });
  }

  function appendJobCardActions(card, job) {
    if (!isObject(job)) return;
    var actions = el("div", { className: "job-actions" });
    var details = jobDetailsLink(job);
    if (details) actions.appendChild(details);
    var original = safeExternalLink(job.url, "Open original");
    if (original) actions.appendChild(original);
    if (actions.childNodes.length > 0) {
      card.appendChild(actions);
    }
  }

  // ---------------------------------------------------------------------------
  // Loading / error / empty states
  // ---------------------------------------------------------------------------

  function clearErrorBoxes(scope) {
    scope.querySelectorAll(".error-box").forEach(function (box) {
      box.remove();
    });
  }

  function setLoading(statusNode, message) {
    var scope = statusNode.parentNode;
    clearErrorBoxes(scope);
    clearText(statusNode);
    statusNode.textContent = message;
    scope.setAttribute("aria-busy", "true");
  }

  function doneLoading(statusNode) {
    var scope = statusNode.parentNode;
    clearText(statusNode);
    scope.removeAttribute("aria-busy");
  }

  function renderError(statusNode, message, retryAction) {
    clearText(statusNode);
    var box = el("div", { className: "error-box", textContent: "Could not load data." });
    if (message) {
      box.appendChild(el("p", { className: "error-detail", textContent: message }));
    }
    var retry = el("button", { className: "retry-button", textContent: "Retry" });
    retry.addEventListener("click", function () {
      box.remove();
      retryAction();
    });
    box.appendChild(retry);
    statusNode.parentNode.insertBefore(box, statusNode.nextSibling);
  }

  // ---------------------------------------------------------------------------
  // Home view (driven by GET /report only)
  // ---------------------------------------------------------------------------

  async function renderHome() {
    var status = document.getElementById("home-status");
    var content = document.getElementById("home-content");
    setLoading(status, "Loading CareerOS dashboard…");
    try {
      var data = await fetchJson("/report");
      if (!isObject(data) || !isObject(data.overview)) {
        throw new Error("The /report response did not match the expected shape.");
      }
      renderHomeContent(content, data);
      doneLoading(status);
    } catch (error) {
      clearText(content);
      renderError(status, error.message, renderHome);
    }
  }

  function renderHomeContent(content, data) {
    content.textContent = "";
    var overview = data.overview;

    var statCards = el("div", { className: "stat-grid" });
    var stats = [
      { label: "Stored jobs", value: overview.total_jobs },
      { label: "Distinct opportunities", value: overview.distinct_opportunities },
      { label: "Duplicate groups", value: overview.canonical_groups },
      { label: "To review", value: overview.unreviewed_groups },
      { label: "Tracked applications", value: overview.tracked_applications },
      { label: "Saved searches", value: overview.saved_searches },
    ];
    stats.forEach(function (stat) {
      statCards.appendChild(
        el("div", { className: "stat-card", textContent: stat.label }),
      );
      var value = el("div", { className: "stat-value", textContent: String(countValue(stat.value)) });
      statCards.appendChild(value);
    });
    content.appendChild(statCards);

    var pipeline = data.application_pipeline;
    if (isObject(pipeline) && isObject(pipeline.status_counts)) {
      buildPipelineChunk(content, pipeline.status_counts);
    }

    var actions = data.action_items;
    if (isObject(actions)) {
      buildActionItemsChunk(content, actions);
    }

    var reminders = data.reminders;
    if (isObject(reminders)) {
      buildRemindersChunk(content, reminders);
    }

    var health = data.collection_health;
    if (isObject(health) && Array.isArray(health.sources) && health.sources.length > 0) {
      buildHealthChunk(content, overview.due_sources, health.sources);
    }

    if (countValue(overview.total_jobs) === 0) {
      var empty = el("p", {
        className: "empty-note",
        textContent:
          "No data yet. Run a collection (e.g. `uv run careeros collect arbeitnow --limit 5`) to populate the dashboard.",
      });
      content.appendChild(empty);
    }
  }

  function buildPipelineChunk(content, statusCounts) {
    var section = el("section", { className: "chunk", textContent: "Application pipeline" });
    var list = el("ul", { className: "pipeline-list" });
    var hasAny = false;
    Object.keys(statusCounts).forEach(function (status) {
      var value = countValue(statusCounts[status]);
      if (value === 0) return;
      hasAny = true;
      var item = el("li", { className: "pipeline-item " + status });
      item.appendChild(el("span", { className: "pipeline-status", textContent: status }));
      item.appendChild(el("span", { className: "pipeline-count", textContent: String(value) }));
      list.appendChild(item);
    });
    if (hasAny) {
      section.appendChild(list);
      content.appendChild(section);
    }
  }

  function buildActionItemsChunk(content, actions) {
    var section = el("section", { className: "chunk", textContent: "Actions" });
    var list = el("ul", { className: "action-list" });
    var hasAny = false;
    ["interview", "offer", "interested", "awaiting_progress"].forEach(function (kind) {
      var items = actions[kind];
      if (!Array.isArray(items) || items.length === 0) return;
      items.forEach(function (item) {
        if (!isObject(item)) return;
        hasAny = true;
        var entry = el("li", { className: "action-item " + kind });
        entry.appendChild(el("span", { className: "action-kind", textContent: kind }));
        entry.appendChild(
          el("span", { className: "action-title", textContent: displayText(item.label) }),
        );
        list.appendChild(entry);
      });
    });
    if (hasAny) {
      section.appendChild(list);
      content.appendChild(section);
    }
  }

  function buildRemindersChunk(content, reminders) {
    var overdue = countValue(reminders.overdue_count);
    var openTotal = countValue(reminders.open_total);
    if (overdue === 0 && openTotal === 0) return;
    var section = el("section", { className: "chunk", textContent: "Reminders" });
    var summary = el("p", {
      className: "reminder-summary",
      textContent: overdue + " overdue, " + openTotal + " open.",
    });
    section.appendChild(summary);
    var upcoming = reminders.upcoming;
    if (Array.isArray(upcoming) && upcoming.length > 0) {
      var list = el("ul", { className: "reminder-list" });
      upcoming.forEach(function (reminder) {
        if (!isObject(reminder)) return;
        var item = el("li", { className: "reminder-item" });
        item.appendChild(
          el("span", { className: "reminder-kind", textContent: displayText(reminder.kind) }),
        );
        var job = isObject(reminder.job) ? reminder.job : null;
        item.appendChild(
          el("span", {
            className: "reminder-job",
            textContent: job ? displayText(job.title) : "—",
          }),
        );
        item.appendChild(
          el("span", { className: "reminder-due", textContent: displayDate(reminder.due_at) }),
        );
        list.appendChild(item);
      });
      section.appendChild(list);
    }
    content.appendChild(section);
  }

  function buildHealthChunk(content, dueSources, sources) {
    var section = el("section", { className: "chunk", textContent: "Source health" });
    var list = el("ul", { className: "health-list" });
    sources.forEach(function (source) {
      if (!isObject(source)) return;
      var statusText;
      if (source.last_succeeded === true) statusText = "ok";
      else if (source.last_succeeded === false) statusText = "failed";
      else statusText = "never";
      var note = "last " + statusText;
      if (source.due) note += ", due";
      var item = el("li", { className: "health-item" });
      item.appendChild(el("span", { className: "health-source", textContent: displayText(source.source) }));
      item.appendChild(el("span", { className: "health-count", textContent: source.fetched + " fetched" }));
      item.appendChild(el("span", { className: "health-count", textContent: source.inserted + " inserted" }));
      item.appendChild(el("span", { className: "health-ok", textContent: note }));
      list.appendChild(item);
    });
    section.appendChild(list);
    if (Array.isArray(dueSources) && dueSources.length > 0) {
      section.appendChild(
        el("p", {
          className: "due-note",
          textContent: "Due for collection: " + dueSources.join(", ") + ".",
        }),
      );
    }
    content.appendChild(section);
  }

  // ---------------------------------------------------------------------------
  // Jobs view (driven by GET /jobs only)
  // ---------------------------------------------------------------------------

  async function renderJobs() {
    var query = document.getElementById("jobs-query").value;
    var params = new URLSearchParams();
    if (query.trim()) params.set("query", query.trim());
    var qs = params.toString();
    await loadJobs(qs ? "/jobs?" + qs : "/jobs");
  }

  async function loadJobs(url) {
    var status = document.getElementById("jobs-status");
    var content = document.getElementById("jobs-content");
    setLoading(status, "Loading jobs…");
    try {
      var data = await fetchJson(url);
      if (!isObject(data) || !Array.isArray(data.items)) {
        throw new Error("The /jobs response did not match the expected shape.");
      }
      renderJobsContent(content, data);
      doneLoading(status);
    } catch (error) {
      clearText(content);
      renderError(status, error.message, renderJobs);
    }
  }

  function renderJobsContent(content, data) {
    content.textContent = "";
    var items = data.items;
    var total = countValue(data.total);

    if (items.length === 0) {
      var empty = el("p", {
        className: "empty-note",
        textContent: total === 0 ? "No jobs found." : "No jobs match this search.",
      });
      content.appendChild(empty);
      return;
    }

    var summary = el("p", {
      className: "result-summary",
      textContent: "Showing " + items.length + " of " + total + " jobs.",
    });
    content.appendChild(summary);

    var list = el("ul", { className: "job-list" });
    items.forEach(function (job) {
      if (!isObject(job)) return;
      var card = el("li", { className: "job-card" });
      card.appendChild(el("h2", { className: "job-title", textContent: displayText(job.title) }));
      appendJobCardActions(card, job);
      var fields = el("dl", { className: "job-fields" });
      addField(fields, "Company", job.company);
      addField(fields, "Location", job.location);
      addField(fields, "Remote", job.remote_status);
      addField(fields, "Employment type", job.employment_type);
      addField(fields, "Published", displayDate(job.published_at));
      addField(fields, "Source", job.source);
      card.appendChild(fields);
      list.appendChild(card);
    });
    content.appendChild(list);
  }

  function addField(fields, label, value) {
    var item = el("div", { className: "job-field" });
    item.appendChild(el("dt", { className: "field-label", textContent: label + ":" }));
    item.appendChild(el("dd", { className: "field-value", textContent: displayText(value) }));
    fields.appendChild(item);
  }

  // ---------------------------------------------------------------------------
  // Review view (driven by the computation-only POST /report only)
  //
  // Presentation of personalized review/recommendation results in the exact
  // server-returned order; no re-ranking, grouping, or scoring is ever
  // re-implemented here. The view opens with the profile form and never
  // auto-submits: results appear only after the user submits.
  // ---------------------------------------------------------------------------

  var MAX_LIST_ENTRIES = 100;
  var MAX_ENTRY_LENGTH = 100;
  var MAX_REMOTE_LENGTH = 50;

  function resetReviewResults() {
    var content = document.getElementById("review-content");
    var status = document.getElementById("review-status");
    var validation = document.getElementById("review-validation");
    if (content) clearText(content);
    if (status) clearText(status);
    if (validation) clearText(validation);
    var section = document.getElementById("view-review");
    if (section) section.removeAttribute("aria-busy");
    var submit = document.getElementById("review-submit");
    if (submit) submit.disabled = false;
  }

  function parseListValue(raw) {
    return String(raw)
      .split(/[,\n]+/)
      .map(function (entry) {
        return entry.trim();
      })
      .filter(function (entry) {
        return entry.length > 0;
      });
  }

  function readReviewFields() {
    return {
      desiredTitles: parseListValue(document.getElementById("review-titles").value),
      skills: parseListValue(document.getElementById("review-skills").value),
      preferredLocations: parseListValue(
        document.getElementById("review-locations").value
      ),
      remotePreference: document.getElementById("review-remote").value.trim(),
      grouped: document.getElementById("review-grouped").checked,
      limit: document.getElementById("review-limit").value,
    };
  }

  function validateReviewFields(fields) {
    var lists = [
      ["Desired titles", fields.desiredTitles],
      ["Skills", fields.skills],
      ["Preferred locations", fields.preferredLocations],
    ];
    for (var index = 0; index < lists.length; index++) {
      var label = lists[index][0];
      var entries = lists[index][1];
      if (entries.length > MAX_LIST_ENTRIES) {
        return label + " has more than " + MAX_LIST_ENTRIES + " entries.";
      }
      for (var entryIndex = 0; entryIndex < entries.length; entryIndex++) {
        if (entries[entryIndex].length > MAX_ENTRY_LENGTH) {
          return label + " entries must be " + MAX_ENTRY_LENGTH + " characters or fewer.";
        }
      }
    }
    if (fields.remotePreference.length > MAX_REMOTE_LENGTH) {
      return (
        "Remote preference must be " + MAX_REMOTE_LENGTH + " characters or fewer."
      );
    }
    return null;
  }

  function setReviewValidation(message) {
    var node = document.getElementById("review-validation");
    if (!node) return;
    clearText(node);
    if (message) node.textContent = message;
  }

  function reviewFormSubmitted(event) {
    event.preventDefault();
    submitReview();
  }

  function submitReview() {
    var fields = readReviewFields();
    var validationError = validateReviewFields(fields);
    setReviewValidation(validationError);
    if (validationError) return;
    runReviewRequest(fields);
  }

  // Only the approved profile fields are ever sent; empty fields are omitted.
  function buildProfilePayload(fields) {
    var profile = {};
    if (fields.desiredTitles.length > 0) {
      profile.desired_titles = fields.desiredTitles;
    }
    if (fields.skills.length > 0) {
      profile.skills = fields.skills;
    }
    if (fields.preferredLocations.length > 0) {
      profile.preferred_locations = fields.preferredLocations;
    }
    if (fields.remotePreference.length > 0) {
      profile.remote_preference = fields.remotePreference;
    }
    return { profile: profile };
  }

  async function runReviewRequest(fields) {
    var status = document.getElementById("review-status");
    var content = document.getElementById("review-content");
    var submit = document.getElementById("review-submit");
    var url =
      "/report?grouped=" + (fields.grouped ? "true" : "false") + "&limit=" + fields.limit;
    setLoading(status, "Generating review results…");
    if (submit) submit.disabled = true;
    try {
      var data = await postReportJson(url, buildProfilePayload(fields));
      if (
        !isObject(data) ||
        !isObject(data.review_queue) ||
        !isObject(data.recommendations)
      ) {
        throw new Error("The /report response did not match the expected shape.");
      }
      renderReviewContent(content, data);
      doneLoading(status);
    } catch (error) {
      clearText(content);
      renderError(status, error.message, function () {
        runReviewRequest(fields);
      });
    } finally {
      if (submit) submit.disabled = false;
    }
  }

  function renderReviewContent(content, data) {
    content.textContent = "";
    content.appendChild(buildReviewSummary(data.review_queue, data.recommendations));
    content.appendChild(buildReviewQueueSection(data.review_queue));
    content.appendChild(buildRecommendationsSection(data.recommendations));
  }

  function buildReviewSummary(review, recommendations) {
    var section = el("section", { className: "chunk" });
    section.appendChild(el("h2", { textContent: "Summary" }));
    var message =
      countValue(review.candidate_count) + " opportunities to review. " +
      countValue(recommendations.candidate_count) + " recommendations. " +
      (review.grouped === true ? "Grouped." : "Individual jobs.");
    section.appendChild(el("p", { className: "result-summary", textContent: message }));
    return section;
  }

  function buildReviewQueueSection(review) {
    var section = el("section", { className: "chunk" });
    section.appendChild(el("h2", { textContent: "Review queue" }));
    var items = Array.isArray(review.items) ? review.items : [];
    if (items.length === 0) {
      section.appendChild(
        el("p", {
          className: "empty-note",
          textContent:
            typeof review.note === "string" && review.note.length > 0
              ? review.note
              : "No opportunities to review.",
        })
      );
      return section;
    }
    var list = el("ul", { className: "job-list" });
    items.forEach(function (item) {
      if (!isObject(item)) return;
      var group = isObject(item.group) ? item.group : null;
      if (!group) return;
      var card = buildJobCard(group.representative, item.match_score, null, group);
      if (card) list.appendChild(card);
    });
    section.appendChild(list);
    return section;
  }

  function buildRecommendationsSection(recommendations) {
    var section = el("section", { className: "chunk" });
    section.appendChild(el("h2", { textContent: "Recommendations" }));
    var grouped = recommendations.grouped === true;
    var entries = grouped ? recommendations.ranked_groups : recommendations.ranked_jobs;
    var list = Array.isArray(entries) ? entries : [];
    if (list.length === 0) {
      section.appendChild(
        el("p", {
          className: "empty-note",
          textContent:
            typeof recommendations.note === "string" && recommendations.note.length > 0
              ? recommendations.note
              : "No recommendations.",
        })
      );
      return section;
    }
    var resultList = el("ul", { className: "job-list" });
    list.forEach(function (entry) {
      if (!isObject(entry)) return;
      var job = grouped ? entry.group && entry.group.representative : entry.job;
      var group = grouped ? entry.group : null;
      var card = buildJobCard(job, entry.match_score, entry.score, group);
      if (card) resultList.appendChild(card);
    });
    section.appendChild(resultList);
    return section;
  }

  function buildJobCard(job, matchScore, score, group) {
    if (!isObject(job)) return null;
    var card = el("li", { className: "job-card" });
    card.appendChild(el("h3", { className: "job-title", textContent: displayText(job.title) }));
    appendJobCardActions(card, job);
    var fields = el("dl", { className: "job-fields" });
    addField(fields, "Company", job.company);
    addField(fields, "Location", job.location);
    addField(fields, "Remote", job.remote_status);
    addField(fields, "Employment type", job.employment_type);
    addField(fields, "Source", job.source);
    addField(fields, "Published", displayDate(job.published_at));
    addField(fields, "Match score", String(countValue(matchScore)));
    if (score !== null && score !== undefined) {
      addField(fields, "Score", String(countValue(score)));
    }
    card.appendChild(fields);
    if (isObject(group) && Array.isArray(group.members) && group.members.length > 1) {
      card.appendChild(
        el("p", {
          className: "group-note",
          textContent: group.members.length + " listings combined.",
        })
      );
    }
    return card;
  }

  // ---------------------------------------------------------------------------
  // Job Detail view (#job/<id>)
  //
  // Loads one stored job's detail through exactly five canonical read GET
  // endpoints (GET /jobs/{id}, GET /jobs/{id}/group, GET /applications/{id},
  // GET /review-decisions/{id}, GET /reminders?job_id={id}), issued
  // concurrently via Promise.allSettled. The core job response is
  // authoritative: a canonical 404 renders the whole-page "Job not found"
  // state, while a network/500/malformed core failure renders a whole-page
  // error with a Retry that reruns the entire detail load. Ancillary sections
  // (opportunity, application, review, reminders) each fail independently with
  // a local error + Retry that refetches only that section. Opportunity
  // members come from the embedded group response -- never per-member
  // requests -- and a one-member singleton group is a normal "no duplicates"
  // state that omits the section entirely (never a 404-tolerated absence).
  // ---------------------------------------------------------------------------

  function jobDetailUrls(jobId) {
    var base = "/jobs/" + jobId;
    return {
      job: base,
      group: base + "/group",
      application: "/applications/" + jobId,
      review: "/review-decisions/" + jobId,
      reminders: "/reminders?job_id=" + jobId,
    };
  }

  async function loadJobDetailData(jobId) {
    var urls = jobDetailUrls(jobId);
    var keys = ["job", "group", "application", "review", "reminders"];
    var settled = await Promise.allSettled(
      keys.map(function (key) {
        return fetchJson(urls[key]);
      })
    );
    var results = {};
    keys.forEach(function (key, index) {
      results[key] = settled[index];
    });
    return results;
  }

  async function fetchDetailSection(sectionKey, jobId) {
    var urls = jobDetailUrls(jobId);
    try {
      var value = await fetchJson(urls[sectionKey]);
      return { status: "fulfilled", value: value };
    } catch (reason) {
      return { status: "rejected", reason: reason };
    }
  }

  function backLink() {
    return el("a", {
      className: "job-back",
      href: "#" + jobDetailOrigin,
      textContent: jobDetailOrigin === REVIEW ? "Back to review" : "Back to jobs",
    });
  }

  // True only when the current hash still targets the given job id. Every async
  // detail operation checks this (plus the request token) after awaiting so a
  // stale response can never overwrite the currently visible job.
  function isCurrentDetailJob(jobId) {
    return parsedJobId() === jobId;
  }

  function renderJobNotFound(content, jobId) {
    clearText(content);
    content.appendChild(backLink());
    content.appendChild(el("h1", { textContent: "Job not found" }));
    content.appendChild(
      el("p", {
        className: "empty-note",
        textContent: "No job found with ID " + jobId + ".",
      })
    );
  }

  function renderWholePageError(content, error, retryAction) {
    clearText(content);
    content.appendChild(backLink());
    var box = el("div", { className: "error-box" });
    box.appendChild(el("p", { textContent: "Could not load this job." }));
    if (error && error.message) {
      box.appendChild(
        el("p", { className: "error-detail", textContent: error.message })
      );
    }
    var retry = el("button", { className: "retry-button", textContent: "Retry" });
    retry.addEventListener("click", function () {
      box.remove();
      retryAction();
    });
    box.appendChild(retry);
    content.appendChild(box);
  }

  async function renderJobDetail(jobId) {
    var seq = ++detailRequestSeq;
    reviewOperationActive = false;
    var status = document.getElementById("job-detail-status");
    var content = document.getElementById("job-detail-content");
    setLoading(status, "Loading job details…");
    clearText(content);
    try {
      var results = await loadJobDetailData(jobId);
      if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
      var core = results.job;
      if (core.status === "rejected") {
        var reason = core.reason;
        if (reason && reason.status === 404) {
          renderJobNotFound(content, jobId);
        } else {
          renderWholePageError(content, reason, function () {
            renderJobDetail(jobId);
          });
        }
        doneLoading(status);
        return;
      }
      var job = core.value;
      if (!isObject(job)) {
        throw new Error("The /jobs/{id} response did not match the expected shape.");
      }
      renderJobDetailContent(content, job, results);
      doneLoading(status);
    } catch (error) {
      if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
      clearText(content);
      renderWholePageError(content, error, function () {
        renderJobDetail(jobId);
      });
    }
  }

  function renderJobDetailContent(content, job, results) {
    clearText(content);
    content.appendChild(backLink());
    content.appendChild(
      el("h1", { className: "job-title", textContent: displayText(job.title) })
    );
    content.appendChild(buildCoreJobSection(job));
    fillDetailSection(content, job, "group", results.group);
    fillDetailSection(content, job, "application", results.application);
    fillDetailSection(content, job, "review", results.review);
    fillDetailSection(content, job, "reminders", results.reminders);
  }

  function buildCoreJobSection(job) {
    var section = el("section", { className: "chunk" });
    section.appendChild(el("h2", { textContent: "Job" }));
    var fields = el("dl", { className: "job-fields" });
    addField(fields, "Title", job.title);
    addField(fields, "Company", job.company);
    addField(fields, "Location", job.location);
    addField(fields, "Remote", job.remote_status);
    addField(fields, "Employment type", job.employment_type);
    addField(fields, "Source", job.source);
    addField(fields, "Published", displayDate(job.published_at));
    section.appendChild(fields);
    var link = safeExternalLink(job.url, "Open original");
    if (link) section.appendChild(link);
    return section;
  }

  function sectionHeading(sectionKey) {
    if (sectionKey === "group") return "Opportunity";
    if (sectionKey === "application") return "Application";
    if (sectionKey === "review") return "Review";
    return "Reminders";
  }

  function fillDetailSection(content, job, sectionKey, settled) {
    var container = el("section", { className: "chunk" });
    container.appendChild(el("h2", { textContent: sectionHeading(sectionKey) }));
    var body = el("div", { className: "section-body" });
    container.appendChild(body);
    content.appendChild(container);
    renderSectionBody(container, body, job, sectionKey, settled);
  }

  function renderSectionBody(container, body, job, sectionKey, settled) {
    clearText(body);
    if (
      sectionKey === "group" &&
      settled.status === "fulfilled" &&
      (!isObject(settled.value) ||
        !Array.isArray(settled.value.members) ||
        settled.value.members.length <= 1)
    ) {
      // A one-member (singleton) group is the normal "no duplicates" state:
      // omit the Opportunity section entirely.
      container.hidden = true;
      return;
    }
    container.hidden = false;
    renderSectionByKey(body, job, sectionKey, settled, function () {
      retrySection(container, body, job, sectionKey);
    });
  }

  function renderSectionByKey(body, job, sectionKey, settled, retry) {
    if (sectionKey === "group") {
      renderOpportunity(body, job, settled, retry);
    } else if (sectionKey === "application") {
      renderApplication(body, settled, retry);
    } else if (sectionKey === "review") {
      renderReview(body, job, settled, retry);
    } else {
      renderReminders(body, settled, retry);
    }
  }

  async function retrySection(container, body, job, sectionKey) {
    var seq = detailRequestSeq;
    clearText(body);
    container.hidden = false;
    body.appendChild(el("p", { className: "status", textContent: "Loading…" }));
    var settled = await fetchDetailSection(sectionKey, job.id);
    if (seq !== detailRequestSeq || !isCurrentDetailJob(job.id)) return;
    renderSectionBody(container, body, job, sectionKey, settled);
  }

  function sectionErrorBox(message, error, retryAction) {
    var box = el("div", { className: "error-box" });
    box.appendChild(el("p", { textContent: message }));
    if (error && error.message) {
      box.appendChild(
        el("p", { className: "error-detail", textContent: error.message })
      );
    }
    var retry = el("button", { className: "retry-button", textContent: "Retry" });
    retry.addEventListener("click", function () {
      box.remove();
      retryAction();
    });
    box.appendChild(retry);
    return box;
  }

  // Replaces any existing error box in the section body with a fresh one, so a
  // section never accumulates duplicate error panels. Mutation flows re-render
  // state in place on failure and must replace, never append; the render-time
  // load paths already guarantee a single box because they clear the body first.
  function replaceSectionError(body, message, error, retryAction) {
    body.querySelectorAll(".error-box").forEach(function (box) {
      box.remove();
    });
    body.appendChild(sectionErrorBox(message, error, retryAction));
  }

  function renderOpportunity(body, job, settled, retry) {
    if (settled.status === "rejected") {
      // Any group failure (a 404 included) is a section error with Retry,
      // never a normal "no group" absence.
      body.appendChild(
        sectionErrorBox("The opportunity could not be loaded.", settled.reason, retry)
      );
      return;
    }
    var group = settled.value;
    if (!isObject(group) || !Array.isArray(group.members)) {
      body.appendChild(
        sectionErrorBox(
          "The opportunity response did not match the expected shape.",
          null,
          retry
        )
      );
      return;
    }
    body.appendChild(
      el("p", {
        className: "result-summary",
        textContent: group.members.length + " listings in this opportunity.",
      })
    );
    var list = el("ul", { className: "job-list" });
    group.members.forEach(function (member) {
      var card = buildMemberCard(member, job);
      if (card) list.appendChild(card);
    });
    body.appendChild(list);
  }

  function buildMemberCard(member, currentJob) {
    if (!isObject(member)) return null;
    var card = el("li", { className: "job-card" });
    card.appendChild(
      el("h3", { className: "job-title", textContent: displayText(member.title) })
    );
    appendJobCardActions(card, member);
    var fields = el("dl", { className: "job-fields" });
    addField(fields, "Company", member.company);
    addField(fields, "Location", member.location);
    addField(fields, "Remote", member.remote_status);
    addField(fields, "Employment type", member.employment_type);
    addField(fields, "Source", member.source);
    addField(fields, "Published", displayDate(member.published_at));
    card.appendChild(fields);
    if (
      isObject(currentJob) &&
      isFiniteNumber(currentJob.id) &&
      member.id === currentJob.id
    ) {
      card.appendChild(
        el("p", { className: "group-note current-job", textContent: "Current job" })
      );
    }
    return card;
  }

  function renderApplication(body, settled, retry) {
    if (settled.status === "rejected") {
      if (settled.reason && settled.reason.status === 404) {
        body.appendChild(
          el("p", { className: "empty-note", textContent: "Not tracked." })
        );
        return;
      }
      body.appendChild(
        sectionErrorBox(
          "The application status could not be loaded.",
          settled.reason,
          retry
        )
      );
      return;
    }
    var app = settled.value;
    if (!isObject(app) || typeof app.current_status !== "string") {
      body.appendChild(
        sectionErrorBox(
          "The application response did not match the expected shape.",
          null,
          retry
        )
      );
      return;
    }
    var fields = el("dl", { className: "job-fields" });
    addField(fields, "Status", app.current_status);
    addField(fields, "First tracked", displayDate(app.created_at));
    addField(fields, "Last updated", displayDate(app.updated_at));
    body.appendChild(fields);
  }

  // Canonical review decisions (presentation labels only; the transmitted
  // values are the exact backend vocabulary: shortlist, dismiss, review_later).
  var REVIEW_DECISIONS = [
    { value: "shortlist", label: "Shortlist" },
    { value: "dismiss", label: "Dismiss" },
    { value: "review_later", label: "Review later" },
  ];

  function renderReview(body, job, settled, retry) {
    if (settled.status === "rejected") {
      if (settled.reason && settled.reason.status === 404) {
        renderReviewDecisionSection(body, job, null);
        return;
      }
      body.appendChild(
        sectionErrorBox("The review decision could not be loaded.", settled.reason, retry)
      );
      return;
    }
    var review = settled.value;
    if (!isObject(review) || typeof review.decision !== "string") {
      body.appendChild(
        sectionErrorBox(
          "The review response did not match the expected shape.",
          null,
          retry
        )
      );
      return;
    }
    renderReviewDecisionSection(body, job, review);
  }

  // Renders the current decision state plus the action controls. `review` is
  // null exactly when the canonical GET returned 404 ("Not reviewed."); the
  // server response stays authoritative -- no optimistic updates anywhere.
  function renderReviewDecisionSection(body, job, review) {
    if (review === null) {
      body.appendChild(
        el("p", { className: "empty-note", textContent: "Not reviewed." })
      );
    } else {
      var fields = el("dl", { className: "job-fields" });
      addField(fields, "Decision", review.decision);
      addField(fields, "Decided", displayDate(review.created_at));
      addField(fields, "Last updated", displayDate(review.updated_at));
      body.appendChild(fields);
    }
    body.appendChild(buildReviewDecisionActions(body, job, review));
  }

  function buildReviewDecisionActions(body, job, review) {
    var group = el("div", { className: "decision-actions" });
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "Review decision");
    REVIEW_DECISIONS.forEach(function (entry) {
      var button = el("button", {
        className: "decision-button",
        type: "button",
        textContent: entry.label,
      });
      if (review !== null && review.decision === entry.value) {
        button.classList.add("current");
        button.setAttribute("aria-pressed", "true");
      }
      button.addEventListener("click", function () {
        applyReviewDecision(group, body, job, entry.value);
      });
      group.appendChild(button);
    });
    if (review !== null) {
      var clear = el("button", {
        className: "decision-button clear-decision",
        type: "button",
        textContent: "Clear decision",
      });
      clear.addEventListener("click", function () {
        clearReviewDecision(group, body, job);
      });
      group.appendChild(clear);
    }
    return group;
  }

  function setReviewActionsDisabled(group, disabled) {
    group.querySelectorAll("button").forEach(function (button) {
      button.disabled = disabled;
    });
  }

  async function applyReviewDecision(group, body, job, decision) {
    if (reviewOperationActive) return;
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return;
    reviewOperationActive = true;
    var section = body.parentNode;
    var jobId = job.id;
    var seq = detailRequestSeq;
    setReviewActionsDisabled(group, true);
    section.setAttribute("aria-busy", "true");
    try {
      await putJson("/review-decisions/" + jobId, { decision: decision });
      await refreshReviewDecision(body, job, seq);
    } catch (error) {
      if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
      setReviewActionsDisabled(group, false);
      section.removeAttribute("aria-busy");
      replaceSectionError(
        body,
        "The review decision could not be saved.",
        error,
        function () {
          applyReviewDecision(group, body, job, decision);
        }
      );
    } finally {
      reviewOperationActive = false;
    }
  }

  async function clearReviewDecision(group, body, job) {
    if (reviewOperationActive) return;
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return;
    reviewOperationActive = true;
    var section = body.parentNode;
    var jobId = job.id;
    var seq = detailRequestSeq;
    setReviewActionsDisabled(group, true);
    section.setAttribute("aria-busy", "true");
    try {
      await deleteJson("/review-decisions/" + jobId);
      await refreshReviewDecision(body, job, seq);
    } catch (error) {
      if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
      setReviewActionsDisabled(group, false);
      section.removeAttribute("aria-busy");
      replaceSectionError(
        body,
        "The review decision could not be cleared.",
        error,
        function () {
          clearReviewDecision(group, body, job);
        }
      );
    } finally {
      reviewOperationActive = false;
    }
  }

  // After a successful mutation, refetch ONLY GET /review-decisions/{id} and
  // re-render just the Review section. A 404 from that GET is the canonical
  // "Not reviewed." absent state, never an error. The response is authoritative
  // and the whole Job Detail view is never reloaded.
  async function refreshReviewDecision(body, job, seq) {
    var jobId = job.id;
    var settled = await fetchDetailSection("review", jobId);
    if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
    var section = body.parentNode;
    clearText(body);
    renderSectionBody(section, body, job, "review", settled);
    section.removeAttribute("aria-busy");
  }

  function renderReminders(body, settled, retry) {
    if (settled.status === "rejected") {
      body.appendChild(
        sectionErrorBox("The reminders could not be loaded.", settled.reason, retry)
      );
      return;
    }
    var data = settled.value;
    if (!isObject(data) || !Array.isArray(data.items)) {
      body.appendChild(
        sectionErrorBox(
          "The reminders response did not match the expected shape.",
          null,
          retry
        )
      );
      return;
    }
    if (data.items.length === 0) {
      body.appendChild(el("p", { className: "empty-note", textContent: "No reminders." }));
      return;
    }
    var list = el("ul", { className: "reminder-list" });
    data.items.forEach(function (reminder) {
      if (!isObject(reminder)) return;
      var item = el("li", { className: "reminder-item" });
      item.appendChild(
        el("span", { className: "reminder-kind", textContent: displayText(reminder.kind) })
      );
      item.appendChild(
        el("span", { className: "reminder-due", textContent: displayDate(reminder.due_at) })
      );
      var state =
        typeof reminder.completed_at === "string" && reminder.completed_at.length > 0
          ? "Completed"
          : "Open";
      item.appendChild(el("span", { className: "health-count", textContent: state }));
      list.appendChild(item);
    });
    body.appendChild(list);
  }

  // ---------------------------------------------------------------------------
  // Wiring
  // ---------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    var reviewForm = document.getElementById("review-form");
    if (reviewForm) {
      reviewForm.addEventListener("submit", reviewFormSubmitted);
    }
    var form = document.getElementById("jobs-form");
    if (form) {
      form.addEventListener("submit", function (event) {
        event.preventDefault();
        renderJobs();
      });
    }
    navigate();
  });

  window.addEventListener("hashchange", navigate);
})();