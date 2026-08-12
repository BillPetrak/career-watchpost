"use strict";

(function () {
  // CareerOS read-only dashboard.
  //
  // This file contains ONLY presentation and local navigation/loading state.
  // It calls exactly two documented read endpoints (GET /report, GET /jobs),
  // renders the values the API returns, and never re-implements ranking,
  // grouping, tracking, review-state, or reporting logic. API-provided strings
  // are always rendered with textContent/createElement -- never as HTML.

  var HOME = "home";
  var JOBS = "jobs";
  var initialized = false;

  // ---------------------------------------------------------------------------
  // Navigation (semantic hash links: #home, #jobs; unknown hashes -> home).
  // Non-view hashes such as the #main skip-link anchor never change the view or
  // reset loaded state, so activating the skip link while on Jobs keeps Jobs.
  // ---------------------------------------------------------------------------

  function parsedHash() {
    var raw = window.location.hash.replace(/^#\/?/, "").trim().toLowerCase();
    if (raw === JOBS) return JOBS;
    if (raw === HOME) return HOME;
    return null; // unknown / absent / non-view (e.g. #main)
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
    var view = parsedHash();
    if (view === null && !initialized) {
      // Initial load with an absent / unknown hash: default to Home and render.
      view = HOME;
    }
    initialized = true;
    var name = view || visibleView() || HOME;
    showView(name);
    // Only re-render when a real view route is set; otherwise (e.g. the #main
    // skip-link anchor) keep the currently visible, already-rendered view.
    if (view === JOBS) {
      await renderJobs();
    } else if (view === HOME) {
      await renderHome();
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
      throw new Error("Request to " + url + " failed (HTTP " + response.status + ").");
    }
    return response.json();
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
  function safeExternalLink(url) {
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
      textContent: "Open job",
    });
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
      var link = safeExternalLink(job.url);
      if (link) {
        card.appendChild(link);
      }
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
  // Wiring
  // ---------------------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
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