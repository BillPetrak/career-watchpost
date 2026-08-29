"use strict";

(function () {
  // CareerOS read-only dashboard.
  //
  // This file contains ONLY presentation and local navigation/loading state.
  // It calls the documented Home endpoint (GET /report), the Jobs listing
  // (GET /jobs), -- for the Review view -- the computation-only POST /report
  // after the user submits the candidate-profile form plus POST /review-queue
  // for authoritative post-decision queue refreshes plus the
  // per-card review-decision mutation PUT /review-decisions/{representative id}
  // on each Review Queue card (the same canonical endpoint the Job Detail
  // Review section uses; there is no client-side ranking or re-ranking and no
  // optimistic removal), -- for
  // the Job Detail view (#job/<id>) -- exactly five read GET endpoints
  // (GET /jobs/{id}, GET /jobs/{id}/group, GET /applications?job_id={id},
  // GET /review-decisions?job_id={id}, GET /reminders?job_id={id}), -- for
  // the Reminders overview view (#reminders) -- exactly three read GET
  // /reminders queries (overdue, due soon, and upcoming later, derived from a
  // single captured "now" through the endpoint's state/as_of/before/after
  // semantics), and -- for the Review section actions -- the two
  // review-decision mutations PUT /review-decisions/{id} and
  // DELETE /review-decisions/{id}, -- for the Application section actions --
  // the application-status mutation PUT /applications/{id}, and -- for the
  // Reminders section actions -- the five reminder mutations POST /reminders
  // (create), PATCH /reminders/{id} (inline Edit -> Save/Cancel), PUT
  // /reminders/{id}/complete, PUT /reminders/{id}/reopen, and
  // DELETE /reminders/{id} (Delete, always preceded by a native
  // window.confirm dialog). API-provided
  // strings are always rendered with textContent/createElement -- never as
  // HTML. The submitted profile is sent only to POST /report and is never
  // stored by this frontend.

  var HOME = "home";
  var TODAY = "today";
  var JOBS = "jobs";
  var REVIEW = "review";
  var REVIEWED = "reviewed";
  var APPLICATIONS = "applications";
  var REMINDERS = "reminders";
  var SAVED_SEARCHES = "saved-searches";
  var MATCHING = "matching";
  var JOB = "job";
  var initialized = false;

  // Matching Preferences is intentionally process-memory only. Values are
  // replaced with deep copies at every run boundary and never persisted.
  var EMPTY_PROFILE = { desiredTitles: [], requiredSkills: [], skills: [], preferredLocations: [], remotePreference: "", preferredEmploymentTypes: [], preferredSeniority: [], excludedTerms: [], hardExcludedTerms: [] };
  var matchingDraftProfile = cloneProfile(EMPTY_PROFILE);
  var matchingActiveProfile = cloneProfile(EMPTY_PROFILE);
  var matchingActiveProfileGeneration = 0;
  var matchingDraftGeneration = 0;
  var matchingPreviewGeneration = 0;
  var matchingPreviewRequestSeq = 0;
  var matchingPreviewSnapshot = null;
  var reviewSnapshot = null;
  var matchingPreviewControls = { grouped: true, minimumScore: "0", limit: "10" };
  var matchingPreviewControlsGeneration = 0;

  function cloneProfile(profile) {
    var copy = {};
    Object.keys(EMPTY_PROFILE).forEach(function (key) { copy[key] = Array.isArray(profile[key]) ? profile[key].slice() : String(profile[key] || ""); });
    return copy;
  }
  function freezeProfile(profile) {
    var copy = cloneProfile(profile);
    Object.keys(copy).forEach(function (key) {
      if (Array.isArray(copy[key])) Object.freeze(copy[key]);
    });
    return Object.freeze(copy);
  }
  function profilePayload(profile) {
    var body = {};
    var names = { desiredTitles: "desired_titles", requiredSkills: "required_skills", skills: "skills", preferredLocations: "preferred_locations", preferredEmploymentTypes: "preferred_employment_types", preferredSeniority: "preferred_seniority", excludedTerms: "excluded_terms", hardExcludedTerms: "hard_excluded_terms" };
    Object.keys(names).forEach(function (key) { if (profile[key].length) body[names[key]] = profile[key].slice(); });
    if (profile.remotePreference.length) body.remote_preference = profile.remotePreference.trim();
    return { profile: body };
  }

  // In-memory origin for the Job Detail Back link: remembers whether the
  // detail view was opened from Jobs or Review so the Back link prefers that
  // origin. Never persisted to storage; defaults to Jobs. Browser Back/Forward
  // keeps working naturally because every navigation is a real hash change.
  var jobDetailOrigin = JOBS;

  // A ranked explanation belongs to one response item from one in-memory
  // ranking run. It is deliberately never serialized into the URL or browser
  // storage. A new run replaces this value, and unranked navigation clears it.
  var rankingContextGeneration = 0;
  var rankedJobContext = null;
  var pendingRankedNavigation = null;

  // Saved Searches is an independent, read/select/run workspace. Its result
  // array and the definition returned with it are one immutable in-memory
  // snapshot; no browser storage or implicit reruns are used.
  var savedSearchState = {
    metadata: [], metadataTotal: 0, metadataPage: 0, metadataPageSize: 20, selected: null, metadataSeq: 0, selectionGeneration: 0,
    executionSeq: 0, executionGeneration: 0, rankingGeneration: 0, inFlight: false, snapshot: null,
    page: 0, pageSize: 20, profile: null,
  };

  // Stale-response guard for the Job Detail view: every full render of
  // #job/<id> increments this token, and every async operation that touches the
  // detail DOM (initial load, section retry, review-decision refresh) captures
  // it before awaiting so a response for an older page can never overwrite the
  // currently visible job.
  var detailRequestSeq = 0;

  // Stale-response guard for the Reminders overview view (#reminders): every
  // full render of the view increments this token, and every async operation
  // that touches the overview DOM (initial load, section retry) captures it
  // before awaiting so a response for an older render can never overwrite the
  // currently visible view. navigate() also bumps it the moment the user
  // leaves the view for another view.
  var remindersRequestSeq = 0;

  // Stale-response guard for the Review view (#review): every authoritative
  // POST /report run increments this token, navigate() bumps it when the user
  // leaves the view, and every async continuation (render, error path, or a
  // queue-card mutation's refresh) captures it before awaiting so an older
  // response can never overwrite newer results -- in particular an older
  // refresh can never restore a queue card that a later successful decision
  // removed.
  var reviewRequestSeq = 0;

  var reviewViewGeneration = 0;
  var reviewDataGeneration = 0;
  var reviewQueueRefreshActive = false;
  var reviewQueueRefreshOwner = null;
  var reviewQueueRefreshDirty = false;
  var reviewOperationSeq = 0;

  // Per-card in-flight guard values are operation identities, not booleans, so
  // old finally blocks cannot clean up a newer operation after re-entry.
  var reviewCardInFlight = {};

  // The exact fields of the most recent POST /report run. A successful
  // queue-card decision reuses these fields for POST /review-queue so the
  // authoritative server response alone removes the decided opportunity --
  // there is no client-side ranking, re-ranking, or optimistic removal.
  var lastReviewFields = null;

  // Serializes the Review decision actions: while a mutation (PUT/DELETE) and
  // its immediate refetch are in flight, no second mutation may start for the
  // same Review section. Buttons are disabled for the whole operation as well,
  // so this is defense-in-depth against programmatic/edge re-entry.
  var reviewOperationActive = false;

  // Reviewed is a durable, server-authoritative view separate from the
  // profile-driven Review Queue. Tokens are deliberately independent so a
  // late queue operation cannot affect this workspace (or vice versa).
  var reviewedViewGeneration = 0;
  var reviewedRequestSeq = 0;
  var reviewedDataGeneration = 0;
  var reviewedRefreshActive = false;
  var reviewedRefreshOwner = null;
  var reviewedRefreshDirty = false;
  var reviewedOperationSeq = 0;
  var reviewedCardInFlight = {};
  var reviewedState = { decision: "shortlist", grouped: true, offset: 0, limit: 20 };

  // Applications is deliberately independent from every other workspace.
  // Records are concrete-job owned and this view never invokes grouping,
  // ranking, review, reminder, or report services.
  var applicationsViewGeneration = 0;
  var applicationsRequestSeq = 0;
  var applicationsDataGeneration = 0;
  var applicationsRefreshActive = false;
  var applicationsRefreshOwner = null;
  var applicationsRefreshDirty = false;
  var applicationsOperationSeq = 0;
  var applicationsCardInFlight = {};
  var applicationsState = { view: "active", offset: 0, limit: 20 };
  var applicationsOriginState = null;
  var applicationsFocusTarget = null;

  // Serializes the Application status actions: while a mutation (PUT) and its
  // immediate refetch are in flight, no second mutation may start for the same
  // Application section. Buttons are disabled for the whole operation as well,
  // so this is defense-in-depth against programmatic/edge re-entry.
  var applicationOperationActive = false;
  // Preparation state is scoped to the current Job Detail generation. Drafts
  // are deliberately memory-only and are retained only after a revision
  // conflict so the user must explicitly reapply them.
  var preparationOperationSeq = 0;
  var preparationOperationActive = false;
  var preparationDraft = null;

  // Serializes the Reminder actions (create, complete, reopen): while a
  // mutation and its immediate refetch/reconciliation are in flight, no second
  // reminder mutation may start for the same Reminders section. Controls are
  // disabled for the whole operation as well, so this is defense-in-depth
  // against programmatic/edge re-entry and rapid-click duplicates.
  var reminderOperationActive = false;

  // Today is an independent, server-classified attention view.  These
  // generation/owner tokens keep overlapping refreshes and stale navigation
  // continuations from replacing a newer render.
  var todayViewGeneration = 0;
  var todayRequestSeq = 0;
  var todayDataGeneration = 0;
  var todayRefreshActive = false;
  var todayRefreshOwner = null;
  var todayRefreshDirty = false;
  var todayOperationSeq = 0;
  var todayReminderInFlight = {};
  var todayFocusTarget = null;

  // ---------------------------------------------------------------------------
  // Navigation (semantic hash links: #home, #jobs, #review; unknown hashes ->
  // home). Non-view hashes such as the #main skip-link anchor never change the
  // view or reset loaded state, so activating the skip link while on Review
  // keeps Review.
  // ---------------------------------------------------------------------------

  function parsedHash() {
    var raw = window.location.hash.replace(/^#\/?/, "").trim().toLowerCase();
    if (raw === TODAY) return TODAY;
    if (raw === JOBS) return JOBS;
    if (raw === REVIEW) return REVIEW;
    if (raw === REVIEWED) return REVIEWED;
    if (raw === APPLICATIONS) return APPLICATIONS;
    if (raw === REMINDERS) return REMINDERS;
    if (raw === SAVED_SEARCHES) return SAVED_SEARCHES;
    if (raw === MATCHING) return MATCHING;
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
    // Leaving the Job Detail view invalidates every in-flight job-detail
    // continuation (reminder save/refresh/reconcile, section retries, and the
    // review/application mutation refreshes): a late response captured for the
    // old job must never update the DOM once the user has navigated away. The
    // guard alone can leave a timing window, so the request token is bumped
    // here as well as on every Job Detail entry.
    if (leaving === JOB && name !== JOB) {
      detailRequestSeq++;
    }
    if (leaving === REMINDERS && name !== REMINDERS) {
      remindersRequestSeq++;
    }
    if (leaving === TODAY && name !== TODAY) {
      todayRequestSeq++;
      todayViewGeneration++;
      todayRefreshDirty = false;
    }
    if (leaving === REVIEW && name !== REVIEW) {
      // Invalidate any in-flight Review continuation (the form's POST /report
      // refresh or a queue-card mutation) the moment the user leaves the
      // view, so a late response can never update a hidden view or restore a
      // card after re-entry.
      reviewRequestSeq++;
      reviewViewGeneration++;
      reviewQueueRefreshDirty = false;
      reviewCardInFlight = {};
      var reviewSubmit = document.getElementById("review-submit");
      if (reviewSubmit) reviewSubmit.disabled = false;
      var reviewStatus = document.getElementById("review-status");
      if (reviewStatus) doneLoading(reviewStatus);
    }
    if (leaving === REVIEWED && name !== REVIEWED) {
      reviewedRequestSeq++;
      reviewedViewGeneration++;
      reviewedRefreshDirty = false;
    }
    if (leaving === APPLICATIONS && name !== APPLICATIONS) {
      applicationsRequestSeq++;
      applicationsViewGeneration++;
      applicationsRefreshDirty = false;
      applicationsFocusTarget = null;
    }
    if (leaving === SAVED_SEARCHES && name !== SAVED_SEARCHES) {
      savedSearchState.metadataSeq++;
      savedSearchState.selectionGeneration++;
      savedSearchState.executionSeq++;
      savedSearchState.inFlight = false;
    }
    if (leaving === MATCHING && name !== MATCHING) {
      matchingPreviewRequestSeq++;
      var matchingStatus = document.getElementById("matching-status");
      if (matchingStatus) doneLoading(matchingStatus);
    }
    if (name === REVIEWED && leaving !== REVIEWED) {
      reviewedViewGeneration++;
      reviewedRefreshDirty = false;
    }
    if (name === APPLICATIONS && leaving !== APPLICATIONS) {
      applicationsViewGeneration++;
      applicationsRefreshDirty = false;
    }
    if (name === REVIEW && leaving !== REVIEW) {
      reviewViewGeneration++;
      reviewQueueRefreshDirty = false;
    }
    var hasExactRankedTransition = name === JOB &&
      pendingRankedNavigation !== null &&
      pendingRankedNavigation.jobId === jobId &&
      pendingRankedNavigation.generation === rankingContextGeneration;
    if (name !== JOB && view !== JOB) {
      rankedJobContext = null;
      pendingRankedNavigation = null;
    } else if (name === JOB && !hasExactRankedTransition) {
      // Hash entry by direct navigation, reload, or browser history has no
      // captured ranked transition and must never reuse an old explanation.
      rankedJobContext = null;
    }
    if (name === JOB) {
      pendingRankedNavigation = null;
    }
    if (name === TODAY && leaving !== TODAY) {
      todayViewGeneration++;
      todayRefreshDirty = false;
    }
    showView(name);
    // Only re-render when a real view route is set; otherwise (e.g. the #main
    // skip-link anchor) keep the currently visible, already-rendered view.
    if (view === TODAY) {
      await renderTodayView();
    } else if (view === REVIEW) {
      if (leaving !== REVIEW) {
        var reviewProfileSummary = document.getElementById("review-profile-summary");
        renderProfileSummary(reviewProfileSummary, matchingActiveProfile);
        if (reviewSnapshot) renderStoredReviewSnapshot();
      }
    } else if (view === REVIEWED) {
      await renderReviewedView();
    } else if (view === APPLICATIONS) {
      await renderApplicationsView();
    } else if (view === SAVED_SEARCHES) {
      await renderSavedSearchesView();
    } else if (view === MATCHING) {
      renderMatchingWorkspace();
    } else if (view === REMINDERS) {
      await renderRemindersView();
    } else if (view === JOBS) {
      await renderJobs();
    } else if (view === HOME) {
      await renderHome();
    } else if (view === JOB) {
      if (leaving === JOBS || leaving === REVIEW || leaving === REVIEWED ||
        leaving === MATCHING) {
        jobDetailOrigin = leaving;
      } else if (leaving === APPLICATIONS) {
        jobDetailOrigin = leaving;
        applicationsOriginState = {
          view: applicationsState.view,
          offset: applicationsState.offset,
          limit: applicationsState.limit,
        };
      } else if (leaving === TODAY) {
        jobDetailOrigin = TODAY;
      } else if (leaving === SAVED_SEARCHES) {
        jobDetailOrigin = SAVED_SEARCHES;
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
          "X-CareerOS-Write": "1",
        },
        body: JSON.stringify(body),
      });
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

  // Generic same-origin JSON write helper used by the Job Detail Review
  // actions (PUT/DELETE /review-decisions/{id}) and the Application status
  // action (PUT /applications/{id}). Network failures and non-2xx responses
  // follow the same friendly-error treatment as the read/POST helpers; the
  // caller owns the refetch. Only those mutations are routed here -- the
  // Review view's POST stays on postReportJson.
  async function writeJson(url, options) {
    var response;
    options.headers = Object.assign({}, options.headers || {}, { "X-CareerOS-Write": "1" });
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

  // The Reminder create mutation (POST /reminders). Network failures and
  // non-2xx responses follow the same friendly-error treatment as writeJson;
  // the caller owns the reconciliation. POST /reminders is intentionally
  // non-idempotent, so a failed creation is never blindly replayed (see
  // handleCreateReminderFailure).
  function postJson(url, body) {
    return writeJson(url, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-CareerOS-Write": "1",
      },
      body: JSON.stringify(body),
    });
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

  // PATCH adapter for the Reminder edit mutation (PATCH /reminders/{id}).
  function patchJson(url, body) {
    return writeJson(url, {
      method: "PATCH",
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
    var errorCode = null;
    try {
      var payload = await response.json();
      if (
        isObject(payload) &&
        typeof payload.detail === "string" &&
        payload.detail.length > 0
      ) {
        message += " " + payload.detail;
      } else if (isObject(payload) && isObject(payload.detail) && typeof payload.detail.code === "string") {
        message += " " + payload.detail.code;
        errorCode = payload.detail.code;
      }
    } catch (_notJson) {
      // Non-JSON error body: keep the generic HTTP message.
    }
    var error = new Error(message);
    if (errorCode) error.code = errorCode;
    return error;
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
  // Matching Preferences workspace and its explicit, server-authoritative preview.
  function profileFieldSpec() {
    return [
      ["desiredTitles", "Desired job titles", "matching-desired-titles", "Comma- and/or newline-separated entries."],
      ["requiredSkills", "Required skills", "matching-required-skills", "Matching improves rank; missing skills can reduce rank when description evidence is available. Required does not remove a job."],
      ["skills", "Preferred skills", "matching-skills", "Comma- and/or newline-separated entries."],
      ["preferredLocations", "Preferred locations", "matching-locations", "Comma- and/or newline-separated entries."],
      ["preferredEmploymentTypes", "Preferred employment types", "matching-employment-types", "Comma- and/or newline-separated entries."],
      ["preferredSeniority", "Preferred seniority", "matching-seniority", "Comma- and/or newline-separated entries."],
      ["excludedTerms", "Terms to avoid", "matching-excluded-terms", "Soft penalties may reduce a match."],
      ["hardExcludedTerms", "Terms that block a match", "matching-hard-excluded-terms", "Matching terms force displayed match_score to zero; they do not delete the listing. A threshold-zero surface may still include it."],
    ];
  }
  function readMatchingDraft() {
    var profile = cloneProfile(matchingDraftProfile);
    profileFieldSpec().forEach(function (spec) { var node = document.getElementById(spec[2]); if (node) profile[spec[0]] = parseListValue(node.value); });
    var remote = document.getElementById("matching-remote"); profile.remotePreference = remote ? remote.value.trim() : "";
    return profile;
  }
  function matchingDraftInputChanged(event) {
    var node = event.target;
    var spec = profileFieldSpec().find(function (entry) { return entry[2] === node.id; });
    if (spec) {
      matchingDraftProfile[spec[0]] = parseListValue(node.value);
      matchingDraftGeneration++;
    } else if (node.id === "matching-remote") {
      matchingDraftProfile.remotePreference = node.value.trim();
      matchingDraftGeneration++;
    } else if (
      node.id === "matching-grouped" ||
      node.id === "matching-min-score" ||
      node.id === "matching-limit"
    ) {
      matchingPreviewControls = {
        grouped: document.getElementById("matching-grouped").checked,
        minimumScore: document.getElementById("matching-min-score").value,
        limit: document.getElementById("matching-limit").value,
      };
      matchingPreviewControlsGeneration++;
    }
    renderMatchingStaleNotice();
  }

  function matchingSnapshotIsStale(snapshot) {
    return snapshot.draftGeneration !== matchingDraftGeneration ||
      snapshot.profileGeneration !== matchingActiveProfileGeneration ||
      snapshot.controlsGeneration !== matchingPreviewControlsGeneration;
  }

  function renderMatchingStaleNotice() {
    if (!matchingPreviewSnapshot || !matchingSnapshotIsStale(matchingPreviewSnapshot)) {
      return;
    }
    var section = document.querySelector(".matching-preview");
    if (section && !section.querySelector(".stale-note")) {
      section.insertBefore(
        el("p", {
          className: "stale-note",
          textContent: "Previous preferences — rerun to update",
        }),
        section.children[1] || null
      );
    }
  }
  function renderProfileSummary(target, profile) {
    if (!target) return; target.textContent = "";
    var count = 0; Object.keys(profile).forEach(function (key) { count += Array.isArray(profile[key]) ? profile[key].length : (profile[key] ? 1 : 0); });
    target.appendChild(el("p", { className: "profile-summary-text", textContent: count ? count + " preference entr" + (count === 1 ? "y" : "ies") + " active for this session." : "No matching preferences are active for this session." }));
    profileFieldSpec().forEach(function (spec) { if (profile[spec[0]].length) target.appendChild(el("p", { textContent: spec[1] + ": " + profile[spec[0]].join(", ") })); });
    if (profile.remotePreference) target.appendChild(el("p", { textContent: "Remote preference: " + profile.remotePreference }));
  }
  function renderMatchingWorkspace() {
    var content = document.getElementById("matching-content");
    if (!content) return;
    content.textContent = "";
    var form = el("form", { className: "profile-form matching-form" });
    form.id = "matching-form";
    var fieldset = el("fieldset", { className: "profile-fields" });
    fieldset.appendChild(el("legend", { textContent: "Draft preferences" }));
    profileFieldSpec().forEach(function (spec) {
      var wrap = el("div", { className: "profile-field" });
      var label = el("label", { textContent: spec[1] });
      label.htmlFor = spec[2];
      var area = document.createElement("textarea");
      area.id = spec[2];
      area.name = spec[0];
      area.rows = 2;
      area.value = matchingDraftProfile[spec[0]].join(", ");
      wrap.appendChild(label);
      wrap.appendChild(area);
      wrap.appendChild(el("p", { className: "field-hint", textContent: spec[3] }));
      fieldset.appendChild(wrap);
    });
    var remoteWrap = el("div", { className: "profile-field" });
    var remoteLabel = el("label", { textContent: "Remote preference" });
    remoteLabel.htmlFor = "matching-remote";
    var remote = document.createElement("input");
    remote.id = "matching-remote";
    remote.name = "remote_preference";
    remote.maxLength = MAX_REMOTE_LENGTH;
    remote.value = matchingDraftProfile.remotePreference;
    remote.placeholder = "e.g. remote, hybrid, onsite";
    remoteWrap.appendChild(remoteLabel);
    remoteWrap.appendChild(remote);
    remoteWrap.appendChild(el("p", {
      className: "field-hint",
      textContent: "Free text accepted by the canonical API; whitespace is trimmed only.",
    }));
    fieldset.appendChild(remoteWrap);
    form.appendChild(fieldset);

    var runControls = el("div", { className: "profile-controls" });
    var groupedLabel = el("label", { className: "inline-label" });
    var grouped = document.createElement("input");
    grouped.type = "checkbox";
    grouped.id = "matching-grouped";
    grouped.checked = matchingPreviewControls.grouped;
    groupedLabel.appendChild(grouped);
    groupedLabel.appendChild(document.createTextNode(" Collapse duplicates"));
    runControls.appendChild(groupedLabel);
    var minLabel = el("label", { textContent: "Minimum match score" });
    minLabel.htmlFor = "matching-min-score";
    var min = document.createElement("input");
    min.type = "number";
    min.min = 0;
    min.max = 100;
    min.value = matchingPreviewControls.minimumScore;
    min.id = "matching-min-score";
    minLabel.appendChild(min);
    runControls.appendChild(minLabel);
    var limitLabel = el("label", { textContent: "Displayed results" });
    limitLabel.htmlFor = "matching-limit";
    var limit = document.createElement("input");
    limit.type = "number";
    limit.min = 1;
    limit.max = 100;
    limit.value = matchingPreviewControls.limit;
    limit.id = "matching-limit";
    limitLabel.appendChild(limit);
    runControls.appendChild(limitLabel);
    form.appendChild(runControls);

    var actions = el("div", { className: "profile-actions" });
    var preview = el("button", {
      className: "primary-button",
      textContent: "Preview matches",
      type: "submit",
    });
    preview.id = "matching-preview";
    actions.appendChild(preview);
    var adopt = el("button", {
      textContent: "Use these preferences for this session",
      type: "button",
    });
    adopt.id = "matching-adopt";
    adopt.addEventListener("click", adoptMatchingProfile);
    actions.appendChild(adopt);
    var reset = el("button", {
      textContent: "Reset draft to current preferences",
      type: "button",
    });
    reset.id = "matching-reset";
    reset.addEventListener("click", function () {
      matchingDraftProfile = cloneProfile(matchingActiveProfile);
      matchingDraftGeneration++;
      renderMatchingWorkspace();
    });
    actions.appendChild(reset);
    form.appendChild(actions);
    form.addEventListener("input", matchingDraftInputChanged);
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      previewMatching();
    });
    content.appendChild(form);

    var summary = el("div", { className: "profile-summary" });
    summary.id = "matching-active-summary";
    renderProfileSummary(summary, matchingActiveProfile);
    content.appendChild(summary);
    if (matchingPreviewSnapshot) renderMatchingPreview(matchingPreviewSnapshot, content);
  }
  function validateMatchingProfile(profile) {
    var specs = profileFieldSpec(); for (var i = 0; i < specs.length; i++) { var values = profile[specs[i][0]]; if (values.length > MAX_LIST_ENTRIES) return specs[i][1] + " has more than " + MAX_LIST_ENTRIES + " entries."; for (var j = 0; j < values.length; j++) if (values[j].length > MAX_ENTRY_LENGTH) return specs[i][1] + " entries must be " + MAX_ENTRY_LENGTH + " characters or fewer."; }
    if (profile.remotePreference.length > MAX_REMOTE_LENGTH) return "Remote preference must be " + MAX_REMOTE_LENGTH + " characters or fewer."; return null;
  }
  function announceMatchingValidation(status, message) {
    if (!status) return;
    doneLoading(status);
    clearErrorBoxes(status.parentNode);
    status.textContent = message;
    status.focus();
  }
  function adoptMatchingProfile() {
    var profile = readMatchingDraft();
    var error = validateMatchingProfile(profile);
    var status = document.getElementById("matching-status");
    if (error) {
      announceMatchingValidation(status, error);
      return;
    }
    matchingPreviewRequestSeq++;
    matchingActiveProfile = cloneProfile(profile);
    matchingActiveProfileGeneration++;
    matchingDraftProfile = cloneProfile(profile);
    matchingDraftGeneration++;
    renderMatchingWorkspace();
    if (status) {
      doneLoading(status);
      status.textContent = "Preferences adopted for this session. Generate Review or run a Saved Search explicitly when ready.";
    }
  }

  function validateMatchingPreviewControls(controls) {
    var minimum = Number(controls.minimumScore);
    var limit = Number(controls.limit);
    if (!Number.isInteger(minimum) || minimum < 0 || minimum > 100) {
      return "Minimum match score must be a whole number from 0 to 100.";
    }
    if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
      return "Displayed results must be a whole number from 1 to 100.";
    }
    return null;
  }

  async function previewMatching() {
    var profile = freezeProfile(matchingDraftProfile);
    var draftGeneration = matchingDraftGeneration;
    var profileGeneration = matchingActiveProfileGeneration;
    var controlsGeneration = matchingPreviewControlsGeneration;
    var controls = Object.freeze({
      grouped: matchingPreviewControls.grouped,
      minimumScore: matchingPreviewControls.minimumScore,
      limit: matchingPreviewControls.limit,
    });
    var validation = validateMatchingProfile(profile) ||
      validateMatchingPreviewControls(controls);
    var status = document.getElementById("matching-status");
    if (validation) {
      announceMatchingValidation(status, validation);
      return;
    }
    var seq = ++matchingPreviewRequestSeq;
    var runGeneration = ++matchingPreviewGeneration;
    setLoading(status, "Previewing matches…");
    try {
      var data = await postReportJson(
        "/ranked-jobs?grouped=" + (controls.grouped ? "true" : "false") +
          "&minimum_score=" + encodeURIComponent(controls.minimumScore) +
          "&offset=0&limit=" + encodeURIComponent(controls.limit),
        Object.assign({}, profilePayload(profile), { untracked: true })
      );
      if (seq !== matchingPreviewRequestSeq || visibleView() !== MATCHING) return;
      if (
        draftGeneration !== matchingDraftGeneration ||
        profileGeneration !== matchingActiveProfileGeneration ||
        controlsGeneration !== matchingPreviewControlsGeneration
      ) {
        if (status) {
          doneLoading(status);
          status.textContent = "Preferences changed while Preview was running. Rerun to update.";
        }
        renderMatchingStaleNotice();
        return;
      }
      if (
        !isObject(data) ||
        !Array.isArray(data.items) ||
        !isFiniteNumber(data.total) ||
        data.offset !== 0 ||
        data.limit !== Number(controls.limit) ||
        data.total < data.items.length
      ) {
        throw new Error("The /ranked-jobs response did not match the expected shape.");
      }
      matchingPreviewSnapshot = Object.freeze({
        profile: profile,
        profileGeneration: profileGeneration,
        draftGeneration: draftGeneration,
        controlsGeneration: controlsGeneration,
        controls: controls,
        runGeneration: runGeneration,
        items: Object.freeze(data.items.slice()),
        total: data.total,
      });
      renderMatchingWorkspace();
      doneLoading(status);
    } catch (errorResponse) {
      if (seq !== matchingPreviewRequestSeq || visibleView() !== MATCHING) return;
      renderError(status, errorResponse.message, previewMatching);
    }
  }

  function renderMatchingPreview(snapshot, target) {
    var section = el("section", { className: "chunk matching-preview" });
    section.appendChild(el("h2", { textContent: "Preview results" }));
    if (matchingSnapshotIsStale(snapshot)) {
      section.appendChild(el("p", {
        className: "stale-note",
        textContent: "Previous preferences — rerun to update",
      }));
    }
    section.appendChild(el("p", {
      className: "result-summary",
      textContent: String(snapshot.total) + " matches. " +
        (snapshot.controls.grouped ? "Grouped" : "Individual") +
        ", minimum " + snapshot.controls.minimumScore +
        ", showing up to " + snapshot.controls.limit + ".",
    }));
    var list = el("ul", { className: "job-list" });
    var presentationGeneration = ++rankingContextGeneration;
    snapshot.items.forEach(function (item) {
      var job = item.group && item.group.representative
        ? item.group.representative
        : (item.job || item);
      var card = buildJobCard(job, item.match_score, item.score, item.group);
      if (!card) return;
      attachCardExplanationAndContext(card, item.explanation, {
        generation: presentationGeneration,
        runGeneration: snapshot.runGeneration,
        profileGeneration: snapshot.profileGeneration,
        surface: "matching-preview",
        targetKind: snapshot.controls.grouped ? "opportunity" : "job",
        targetIdentity: item.group && item.group.group_id
          ? "group:" + item.group.group_id
          : "job:" + job.id,
        memberIds: item.group && item.group.members
          ? item.group.members.map(function (member) { return member.id; })
          : [job.id],
        runConfig: snapshot.controls,
      });
      list.appendChild(card);
    });
    section.appendChild(list);
    target.appendChild(section);
  }

  // Saved Searches workspace (metadata + explicit, bounded snapshot execution)
  // ---------------------------------------------------------------------------

  function savedSearchLabel(item) {
    return item.mode === "rank" ? "Rank" : "Search";
  }

  function savedSearchFields(definition) {
    var fields = el("dl", { className: "job-fields" });
    addField(fields, "Mode", savedSearchLabel(definition));
    addField(fields, "Query", definition.query);
    addField(fields, "Grouping", definition.grouped === true ? "Grouped" : "Individual");
    addField(fields, "Result limit", String(definition.result_limit));
    addField(fields, "Source", definition.source);
    addField(fields, "Location", definition.location);
    addField(fields, "Remote", definition.remote === true ? "Remote only" : definition.remote === false ? "Not remote" : "No preference");
    addField(fields, "Tracking", definition.untracked ? "Untracked only" : "All applications");
    if (Array.isArray(definition.include_statuses) && definition.include_statuses.length) addField(fields, "Statuses", definition.include_statuses.join(", "));
    if (Array.isArray(definition.exclude_statuses) && definition.exclude_statuses.length) addField(fields, "Excluded statuses", definition.exclude_statuses.join(", "));
    if (definition.mode === "rank") addField(fields, "Minimum score", definition.min_score === null ? "None" : definition.min_score);
    return fields;
  }

  function savedSearchDefinition(definition) {
    var section = el("section", { className: "saved-search-definition chunk" });
    section.appendChild(el("h2", { textContent: displayText(definition.name) }));
    section.appendChild(savedSearchFields(definition));
    return section;
  }

  function savedSearchJob(item, grouped) { var target = grouped ? (item.group || item) : (item.job || item); return target && target.representative ? target.representative : target; }

  function buildUnrankedJobCard(job, group) {
    if (!isObject(job)) return null;
    var card = el("li", { className: "job-card" });
    card.appendChild(el("h3", { className: "job-title", textContent: displayText(job.title) }));
    appendJobCardActions(card, job);
    var fields = el("dl", { className: "job-fields" });
    addField(fields, "Company", job.company); addField(fields, "Location", job.location);
    addField(fields, "Remote", job.remote_status); addField(fields, "Employment type", job.employment_type);
    addField(fields, "Source", job.source); addField(fields, "Published", displayDate(job.published_at));
    card.appendChild(fields);
    if (isObject(group) && Array.isArray(group.members) && group.members.length > 1) card.appendChild(el("p", { className: "group-note", textContent: group.members.length + " listings combined." }));
    return card;
  }

  function renderSavedResult(item, snapshot, generation) {
    var definition = snapshot.saved_search;
    var grouped = definition.grouped === true;
    var job = savedSearchJob(item, grouped);
    if (!isObject(job)) return null;
    var rank = definition.mode === "rank";
    var group = grouped ? (item.group || item) : null;
    var card = rank ? buildJobCard(job, item.match_score, item.score, group) : buildUnrankedJobCard(job, group);
    if (!card) return null;
    if (rank) {
      attachCardExplanationAndContext(card, item.explanation, {
        generation: generation,
        runGeneration: snapshot.runGeneration,
        profileGeneration: snapshot.profileGeneration,
        runConfig: snapshot.runConfig,
        runLabel: definition.name,
        surface: "saved-search", targetKind: grouped ? "opportunity" : "job",
        targetIdentity: grouped && item.group && item.group.group_id ? "group:" + item.group.group_id : "job:" + job.id,
        memberIds: grouped && item.group && Array.isArray(item.group.members) ? item.group.members.map(function (member) { return member.id; }) : [job.id],
        savedSearchId: definition.id, savedSearchUpdatedAt: definition.updated_at,
      });
    }
    return card;
  }

  function renderSavedSearchSnapshot(snapshot, target) {
    var content = target || document.getElementById("saved-searches-content");
    if (!content || !snapshot) return;
    var definition = snapshot.saved_search;
    var result = el("section", { className: "saved-search-results chunk" });
    result.appendChild(el("h2", { textContent: "Definition used for this run" }));
    if (definition.mode === "rank" &&
      snapshot.profileGeneration !== matchingActiveProfileGeneration) {
      result.appendChild(el("p", {
        className: "stale-note",
        textContent: "Previous preferences — rerun to update",
      }));
    }
    var executedFields = savedSearchFields(definition);
    addField(executedFields, "Name", definition.name);
    result.appendChild(executedFields);
    result.appendChild(el("h2", { textContent: definition.mode === "rank" ? "Ranked results" : "Search results" }));
    result.appendChild(el("p", { className: "result-summary", textContent: snapshot.total + " result" + (snapshot.total === 1 ? "" : "s") + ". This is one saved-search execution." }));
    var items = Array.isArray(snapshot.items) ? snapshot.items : [];
    if (!items.length) { result.appendChild(el("p", { className: "empty-note", textContent: "No results returned." })); content.appendChild(result); return; }
    var page = el("div", { className: "saved-search-page-controls" });
    var list = el("ul", { className: "job-list" });
    function draw() {
      list.textContent = "";
      var start = savedSearchState.page * savedSearchState.pageSize;
      var presentationGeneration = ++rankingContextGeneration;
      items.slice(start, start + savedSearchState.pageSize).forEach(function (item) {
        var card = renderSavedResult(item, snapshot, presentationGeneration);
        if (card) list.appendChild(card);
      });
      page.textContent = "";
      var pages = Math.max(1, Math.ceil(items.length / savedSearchState.pageSize));
      page.appendChild(el("span", { textContent: "Page " + (savedSearchState.page + 1) + " of " + pages }));
      if (savedSearchState.page > 0) { var previous = el("button", { textContent: "Previous", type: "button" }); previous.addEventListener("click", function () { savedSearchState.page--; draw(); }); page.appendChild(previous); }
      if (savedSearchState.page + 1 < pages) { var next = el("button", { textContent: "Next", type: "button" }); next.addEventListener("click", function () { savedSearchState.page++; draw(); }); page.appendChild(next); }
    }
    result.appendChild(page); result.appendChild(list); content.appendChild(result); draw();
  }

  function selectSavedSearch(item) {
    savedSearchState.selected = item; savedSearchState.selectionGeneration++; savedSearchState.executionSeq++; savedSearchState.executionGeneration++; savedSearchState.inFlight = false; savedSearchState.snapshot = null; savedSearchState.page = 0; savedSearchState.profile = null; rankingContextGeneration++; rankedJobContext = null; pendingRankedNavigation = null;
    renderSavedSearchWorkspace();
  }

  function renderSavedSearchWorkspace() {
    var content = document.getElementById("saved-searches-content"); if (!content) return; content.textContent = "";
    var layout = el("div", { className: "saved-search-layout" });
    var listPanel = el("aside", { className: "saved-search-list", "aria-label": "Saved Search definitions" });
    listPanel.appendChild(el("h2", { textContent: "Saved Searches" }));
    if (!savedSearchState.metadata.length) listPanel.appendChild(el("p", { className: "empty-note", textContent: "No Saved Searches yet. Definitions are currently authored through the existing CLI/API capabilities." }));
    savedSearchState.metadata.forEach(function (item) { var button = el("button", { className: "saved-search-option", type: "button" }); button.appendChild(el("strong", { textContent: displayText(item.name) })); button.appendChild(el("span", { className: "mode-badge", textContent: savedSearchLabel(item) })); button.appendChild(el("span", { textContent: displayText(item.query) })); if (savedSearchState.selected && item.id === savedSearchState.selected.id) button.classList.add("selected"); button.addEventListener("click", function () { selectSavedSearch(item); }); listPanel.appendChild(button); });
    if (savedSearchState.metadataTotal > savedSearchState.metadataPageSize) {
      var metadataPager = el("div", { className: "saved-search-page-controls" });
      metadataPager.appendChild(el("span", { textContent: "Page " + (savedSearchState.metadataPage + 1) + " of " + Math.ceil(savedSearchState.metadataTotal / savedSearchState.metadataPageSize) }));
      if (savedSearchState.metadataPage > 0) { var previousMetadata = el("button", { textContent: "Previous", type: "button" }); previousMetadata.addEventListener("click", function () { savedSearchState.metadataPage--; loadSavedSearchMetadata(); }); metadataPager.appendChild(previousMetadata); }
      if ((savedSearchState.metadataPage + 1) * savedSearchState.metadataPageSize < savedSearchState.metadataTotal) { var nextMetadata = el("button", { textContent: "Next", type: "button" }); nextMetadata.addEventListener("click", function () { savedSearchState.metadataPage++; loadSavedSearchMetadata(); }); metadataPager.appendChild(nextMetadata); }
      listPanel.appendChild(metadataPager);
    }
    layout.appendChild(listPanel);
    var detail = el("div", { className: "saved-search-detail" });
    if (!savedSearchState.selected) detail.appendChild(el("p", { className: "empty-note", textContent: "Select a Saved Search to view its configuration." }));
    else {
      detail.appendChild(savedSearchDefinition(savedSearchState.selected));
      var run = el("button", { className: "primary-button", textContent: "Run saved search", type: "button" }); run.id = "saved-search-run"; run.disabled = savedSearchState.inFlight;
      run.addEventListener("click", function () { runSavedSearch(savedSearchState.selected); }); detail.appendChild(run);
      if (savedSearchState.selected.mode === "rank") { var profileSummary = el("div", { className: "profile-summary" }); renderProfileSummary(profileSummary, matchingActiveProfile); detail.appendChild(profileSummary); var edit = el("p"); edit.appendChild(el("a", { href: "#matching", textContent: "Edit matching preferences" })); detail.appendChild(edit); }
      if (savedSearchState.snapshot) { renderSavedSearchSnapshot(savedSearchState.snapshot, detail); }
    }
    layout.appendChild(detail); content.appendChild(layout);
  }

  async function loadSavedSearchMetadata() {
    var seq = ++savedSearchState.metadataSeq, status = document.getElementById("saved-searches-status"); setLoading(status, "Loading Saved Searches…");
    try { var data = await fetchJson("/saved-searches?offset=" + (savedSearchState.metadataPage * savedSearchState.metadataPageSize) + "&limit=" + savedSearchState.metadataPageSize); if (seq !== savedSearchState.metadataSeq || visibleView() !== SAVED_SEARCHES) return; if (!isObject(data) || !Array.isArray(data.items)) throw new Error("The Saved Search response did not match the expected shape."); savedSearchState.metadata = data.items; savedSearchState.metadataTotal = typeof data.total === "number" ? data.total : data.items.length; if (savedSearchState.selected) { var current = data.items.find(function (item) { return item.id === savedSearchState.selected.id; }); if (current) savedSearchState.selected = current; else if (savedSearchState.metadataPage === 0) savedSearchState.selected = null; } renderSavedSearchWorkspace(); doneLoading(status); } catch (error) { if (seq !== savedSearchState.metadataSeq || visibleView() !== SAVED_SEARCHES) return; renderError(status, error.message, loadSavedSearchMetadata); }
  }

  async function runSavedSearch(definition, retryRun) {
    if (!definition) return;
    if (!savedSearchState.selected || savedSearchState.selected.id !== definition.id || savedSearchState.selected.updated_at !== definition.updated_at) {
      var changedStatus = document.getElementById("saved-searches-status");
      renderError(changedStatus, "This Saved Search changed. Refresh its metadata before running it.", loadSavedSearchMetadata);
      return;
    }
    var seq = ++savedSearchState.executionSeq;
    var selection = savedSearchState.selectionGeneration;
    var runGeneration = ++savedSearchState.executionGeneration;
    var status = document.getElementById("saved-searches-status");
    var captured = Object.freeze({
      profile: definition.mode === "rank"
        ? freezeProfile(retryRun && retryRun.profile
          ? retryRun.profile
          : matchingActiveProfile)
        : null,
      profileGeneration: definition.mode === "rank"
        ? (retryRun && Number.isInteger(retryRun.profileGeneration)
          ? retryRun.profileGeneration
          : matchingActiveProfileGeneration)
        : null,
    });
    if (captured.profile) {
      var validation = validateMatchingProfile(captured.profile);
      if (validation) {
        renderError(status, validation, function () {
          runSavedSearch(definition, captured);
        });
        return;
      }
    }
    savedSearchState.inFlight = true;
    setLoading(status, "Running saved search…");
    renderSavedSearchWorkspace();
    var body = captured.profile ? profilePayload(captured.profile) : {};
    try {
      var data = await postReportJson(
        "/saved-searches/" + encodeURIComponent(definition.name) +
          "/execute?snapshot=true",
        body
      );
      if (seq !== savedSearchState.executionSeq ||
        selection !== savedSearchState.selectionGeneration ||
        visibleView() !== SAVED_SEARCHES) return;
      if (captured.profile &&
        captured.profileGeneration !== matchingActiveProfileGeneration) {
        if (status) {
          doneLoading(status);
          status.textContent = "Matching preferences changed while the Saved Search was running. Run it again.";
        }
        return;
      }
      if (!isObject(data) || !isObject(data.saved_search) ||
        !Array.isArray(data.items)) {
        throw new Error("The Saved Search execution response did not match the expected shape.");
      }
      var used = data.saved_search;
      if (used.id !== definition.id || used.name !== definition.name ||
        used.updated_at !== definition.updated_at) {
        var changed = new Error("The Saved Search changed while it was running. Refresh its metadata before running it again.");
        changed.savedSearchChanged = true;
        throw changed;
      }
      if ((used.mode !== "search" && used.mode !== "rank") ||
        typeof used.updated_at !== "string" || data.mode !== used.mode ||
        data.grouped !== used.grouped || data.offset !== 0 ||
        data.limit !== used.result_limit || data.total !== data.items.length) {
        throw new Error("The Saved Search execution response did not match the expected shape.");
      }
      savedSearchState.snapshot = Object.freeze({
        saved_search: used,
        items: Object.freeze(data.items.slice()),
        total: data.total,
        offset: data.offset,
        limit: data.limit,
        profile: captured.profile,
        profileGeneration: captured.profileGeneration,
        runGeneration: runGeneration,
        runConfig: Object.freeze({
          query: used.query,
          grouped: used.grouped,
          minimumScore: used.min_score,
          resultLimit: used.result_limit,
        }),
      });
      savedSearchState.page = 0;
      renderSavedSearchWorkspace();
      doneLoading(status);
    } catch (error) {
      if (seq !== savedSearchState.executionSeq ||
        selection !== savedSearchState.selectionGeneration ||
        visibleView() !== SAVED_SEARCHES) return;
      if (error.status === 404) {
        savedSearchState.selected = null;
        savedSearchState.snapshot = null;
        savedSearchState.metadataPage = 0;
        await loadSavedSearchMetadata();
      } else if (error.savedSearchChanged === true) {
        renderError(status, error.message, loadSavedSearchMetadata);
      } else {
        renderError(status, error.message, function () {
          runSavedSearch(definition, captured);
        });
      }
    } finally {
      if (seq === savedSearchState.executionSeq) {
        savedSearchState.inFlight = false;
        renderSavedSearchWorkspace();
      }
    }
  }

  async function renderSavedSearchesView() { await loadSavedSearchMetadata(); }

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
      profile: cloneProfile(matchingActiveProfile),
      grouped: document.getElementById("review-grouped").checked,
      limit: document.getElementById("review-limit").value,
    };
  }

  function validateReviewFields(fields) {
    if (fields.profile) return validateMatchingProfile(fields.profile);
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
    if (fields.profile) return profilePayload(fields.profile);
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

  // The Review view's single authoritative request. Every run captures a
  // fresh reviewRequestSeq token so only the newest run may render results:
  // a superseded response (newer form submission, a queue-card refresh, or
  // navigation away) is discarded whole -- it can never restore a queue card
  // that a later successful decision removed.
  async function runReviewRequest(fields) {
    var captured = Object.freeze({
      profile: freezeProfile(fields.profile || matchingActiveProfile),
      profileGeneration: Number.isInteger(fields.profileGeneration)
        ? fields.profileGeneration
        : matchingActiveProfileGeneration,
      grouped: fields.grouped === true,
      limit: String(fields.limit),
    });
    var seq = ++reviewRequestSeq;
    reviewDataGeneration++;
    var runGeneration = reviewDataGeneration;
    rankedJobContext = null;
    pendingRankedNavigation = null;
    var status = document.getElementById("review-status");
    var content = document.getElementById("review-content");
    var submit = document.getElementById("review-submit");
    var url =
      "/report?grouped=" + (captured.grouped ? "true" : "false") +
      "&limit=" + encodeURIComponent(captured.limit);
    setLoading(status, "Generating review results…");
    if (submit) submit.disabled = true;
    try {
      var data = await postReportJson(url, profilePayload(captured.profile));
      if (seq !== reviewRequestSeq || visibleView() !== REVIEW) return;
      if (captured.profileGeneration !== matchingActiveProfileGeneration) {
        if (status) {
          doneLoading(status);
          status.textContent = "Matching preferences changed while Review was running. Generate Review again.";
        }
        return;
      }
      if (
        !isObject(data) ||
        !isObject(data.review_queue) ||
        !isObject(data.recommendations)
      ) {
        throw new Error("The /report response did not match the expected shape.");
      }
      reviewSnapshot = Object.freeze({
        profile: captured.profile,
        profileGeneration: captured.profileGeneration,
        grouped: captured.grouped,
        limit: captured.limit,
        runGeneration: runGeneration,
        data: Object.freeze(data),
      });
      lastReviewFields = captured;
      renderStoredReviewSnapshot();
      doneLoading(status);
    } catch (error) {
      if (seq !== reviewRequestSeq || visibleView() !== REVIEW) return;
      renderError(status, error.message, function () {
        runReviewRequest(captured);
      });
    } finally {
      // Only the current run may touch the submit button; a newer run owns
      // its disabled state until it settles.
      if (submit && seq === reviewRequestSeq) submit.disabled = false;
    }
  }

  function renderStoredReviewSnapshot() {
    if (!reviewSnapshot) return;
    var content = document.getElementById("review-content");
    if (!content) return;
    lastReviewFields = Object.freeze({
      profile: reviewSnapshot.profile,
      profileGeneration: reviewSnapshot.profileGeneration,
      grouped: reviewSnapshot.grouped,
      limit: reviewSnapshot.limit,
    });
    renderReviewContent(
      content,
      reviewSnapshot.data,
      ++rankingContextGeneration,
      reviewSnapshot
    );
  }

  function renderReviewContent(content, data, generation, runContext) {
    content.textContent = "";
    if (runContext.profileGeneration !== matchingActiveProfileGeneration) {
      content.appendChild(el("p", {
        className: "stale-note",
        textContent: "Previous preferences — rerun to update",
      }));
    }
    var summary = buildReviewSummary(data.review_queue, data.recommendations);
    var queue = buildReviewQueueSection(data.review_queue, generation, runContext);
    summary.setAttribute("data-review-summary", "true");
    queue.setAttribute("data-review-queue", "true");
    content.appendChild(summary);
    content.appendChild(queue);
    content.appendChild(buildRecommendationsSection(
      data.recommendations,
      generation,
      runContext
    ));
  }

  function buildReviewSummary(review, recommendations) {
    var section = el("section", { className: "chunk" });
    section.setAttribute(
      "data-recommendation-count",
      String(countValue(recommendations.candidate_count))
    );
    section.appendChild(el("h2", { textContent: "Summary" }));
    var message =
      countValue(review.candidate_count) + " opportunities to review. " +
      countValue(recommendations.candidate_count) + " recommendations. " +
      (review.grouped === true ? "Grouped." : "Individual jobs.");
    section.appendChild(el("p", { className: "result-summary", textContent: message }));
    return section;
  }

  function normalizeReviewQueueResponse(data, fields) {
    if (!isObject(data) || !Array.isArray(data.items) || !isFiniteNumber(data.total)) {
      throw new Error("The /review-queue response did not match the expected shape.");
    }
    return {
      items: data.items,
      candidate_count: data.total,
      grouped: fields.grouped,
      note: data.items.length === 0 ? "No opportunities to review." : "",
    };
  }

  function renderAuthoritativeQueue(data, fields, rankingGeneration) {
    var content = document.getElementById("review-content");
    if (!content) return;
    var queue = content.querySelector("[data-review-queue]");
    var summary = content.querySelector("[data-review-summary]");
    if (!queue || !summary) return;
    var review = normalizeReviewQueueResponse(data, fields);
    if (reviewSnapshot) {
      var updatedData = Object.assign({}, reviewSnapshot.data, {
        review_queue: review,
      });
      reviewSnapshot = Object.freeze({
        profile: reviewSnapshot.profile,
        profileGeneration: reviewSnapshot.profileGeneration,
        grouped: reviewSnapshot.grouped,
        limit: reviewSnapshot.limit,
        runGeneration: reviewSnapshot.runGeneration,
        data: Object.freeze(updatedData),
      });
    }
    var replacement = buildReviewQueueSection(
      review,
      rankingGeneration,
      reviewSnapshot
    );
    replacement.setAttribute("data-review-queue", "true");
    queue.replaceWith(replacement);
    var recommendationCount = summary.getAttribute("data-recommendation-count") || "0";
    var summaryText = summary.querySelector(".result-summary");
    if (summaryText) {
      summaryText.textContent =
        countValue(review.candidate_count) +
        " opportunities to review. " +
        recommendationCount +
        " recommendations. " +
        (review.grouped ? "Grouped." : "Individual jobs.");
    }
  }

  function buildReviewQueueSection(review, generation, runContext) {
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
      attachCardExplanationAndContext(card, item.explanation, {
        generation: generation,
        runGeneration: runContext ? runContext.runGeneration : null,
        profileGeneration: runContext ? runContext.profileGeneration : null,
        runConfig: runContext ? Object.freeze({
          grouped: runContext.grouped,
          limit: runContext.limit,
        }) : null,
        surface: "review-queue",
        targetKind: "opportunity",
        targetIdentity: group["group_id"]
          ? "group:" + group["group_id"]
          : "job:" + group.representative.id,
        memberIds: Array.isArray(group.members)
          ? group.members.map(function (member) { return member.id; })
          : [group.representative.id],
      });
      if (card) {
        // Queue-only controls: the representative job id is exactly what the
        // card's "View details" link targets, so deciding the card reviews
        // that same canonical opportunity (the whole duplicate group in
        // grouped mode). Recommendations never receive these controls.
        attachQueueDecisionActions(card, group.representative);
        list.appendChild(card);
      }
    });
    section.appendChild(list);
    return section;
  }

  function buildRecommendationsSection(recommendations, generation, runContext) {
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
      attachCardExplanationAndContext(card, entry.explanation, {
        generation: generation,
        runGeneration: runContext ? runContext.runGeneration : null,
        profileGeneration: runContext ? runContext.profileGeneration : null,
        runConfig: runContext ? Object.freeze({
          grouped: runContext.grouped,
          limit: runContext.limit,
        }) : null,
        surface: "recommendations",
        targetKind: grouped ? "opportunity" : "job",
        targetIdentity: grouped
          ? (group && group["group_id"]
            ? "group:" + group["group_id"]
            : "job:" + job.id)
          : "job:" + job.id,
        memberIds: grouped && group && Array.isArray(group.members)
          ? group.members.map(function (member) { return member.id; })
          : [job.id],
      });
      if (card) resultList.appendChild(card);
    });
    section.appendChild(resultList);
    return section;
  }

  function explanationFactors(explanation, key) {
    return isObject(explanation) && Array.isArray(explanation[key])
      ? explanation[key].filter(isObject)
      : [];
  }

  function factorText(factor) {
    var label = typeof factor.label === "string" ? factor.label : "Criterion evaluated";
    var value = typeof factor.value === "string" && factor.value.length > 0
      ? ": " + factor.value
      : "";
    return label + value;
  }

  function appendFactorList(parent, factors, className, limit) {
    var list = el("ul", { className: className });
    factors.slice(0, limit).forEach(function (factor) {
      list.appendChild(el("li", { textContent: factorText(factor) }));
    });
    parent.appendChild(list);
  }

  function appendCardExplanation(card, explanation) {
    if (!isObject(explanation)) return;
    var details = el("details", { className: "ranking-explanation-card" });
    details.appendChild(el("summary", { textContent: "Why this match?" }));
    details.appendChild(el("p", { className: "explanation-summary", textContent:
      typeof explanation.summary === "string" ? explanation.summary : "This result matched profile criteria." }));
    var matches = explanationFactors(explanation, "matches");
    if (matches.length > 0) {
      details.appendChild(el("h4", { textContent: "Matches" }));
      appendFactorList(details, matches, "explanation-factors explanation-matches", 3);
    }
    var material = explanationFactors(explanation, "gaps").concat(
      explanationFactors(explanation, "unknowns"),
      explanationFactors(explanation, "other_factors").filter(function (factor) {
        return factor.effect === "negative" || factor.effect === "blocking";
      })
    );
    if (material.length > 0) {
      details.appendChild(el("h4", { textContent: "Worth noting" }));
      appendFactorList(details, material, "explanation-factors explanation-gaps", 1);
    }
    card.appendChild(details);
  }

  function attachRankedLinkContext(card, job, explanation, rankingContext) {
    if (!isObject(explanation) || !isObject(rankingContext)) return;
    var link = card.querySelector('a[href^="#job/"]');
    if (!link) return;
    link.addEventListener("click", function (event) {
      if (event.button !== 0 || event.metaKey || event.ctrlKey ||
        event.shiftKey || event.altKey) return;
      var transitionGeneration = ++rankingContextGeneration;
      pendingRankedNavigation = { jobId: job.id, generation: transitionGeneration };
      rankedJobContext = {
        generation: transitionGeneration,
        runGeneration: rankingContext.runGeneration,
        profileGeneration: rankingContext.profileGeneration,
        runConfig: rankingContext.runConfig,
        runLabel: rankingContext.runLabel,
        surface: rankingContext.surface,
        targetKind: rankingContext.targetKind,
        targetIdentity: rankingContext.targetIdentity,
        jobId: job.id,
        explanation: explanation,
        memberIds: Array.isArray(rankingContext.memberIds) ? rankingContext.memberIds.slice() : [],
        savedSearchId: rankingContext.savedSearchId,
        savedSearchUpdatedAt: rankingContext.savedSearchUpdatedAt,
      };
    });
  }

  function attachCardExplanationAndContext(card, explanation, rankingContext) {
    if (!card) return;
    appendCardExplanation(card, explanation);
    var jobLink = card.querySelector('a[href^="#job/"]');
    if (!jobLink) return;
    var jobIdText = jobLink.getAttribute("href").slice("#job/".length);
    var jobId = parseInt(jobIdText, 10);
    if (isFiniteNumber(jobId)) {
      attachRankedLinkContext(card, { id: jobId }, explanation, rankingContext);
    }
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
  // Review Queue card actions (#review results)
  //
  // Each Review Queue card carries the three canonical decision controls
  // wired to the SAME endpoint and semantics as the Job Detail Review
  // section: PUT /review-decisions/{id} with the exact vocabulary values
  // (shortlist, dismiss, review_later). Grouped cards send the representative
  // job id, which the shared review service resolves to the whole canonical
  // opportunity.
  //
  // Protection is PER CARD: while one card's PUT is in flight only that
  // card's buttons are disabled and its job id is marked busy; every other
  // queue card stays fully usable. There is no optimistic removal and no
  // client-side ranking: a successful PUT is followed by re-running the exact
  // captured profile fields (lastReviewFields), so the authoritative
  // /review-queue response alone removes the decided opportunity and updates
  // the summary counts. A failed PUT never triggers that refresh: the card stays
  // visible, its buttons are re-enabled, and one recoverable inline error
  // offers a Retry that reissues the exact failed request.
  // ---------------------------------------------------------------------------

  function setQueueDecisionDisabled(card, disabled) {
    card.querySelectorAll(".decision-button").forEach(function (button) {
      button.disabled = disabled;
    });
    if (disabled) {
      card.setAttribute("aria-busy", "true");
    } else {
      card.removeAttribute("aria-busy");
    }
  }

  function attachQueueDecisionActions(card, job) {
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return;
    var actions = el("div", { className: "decision-actions" });
    actions.setAttribute("role", "group");
    actions.setAttribute("aria-label", "Review queue decision");
    REVIEW_DECISIONS.forEach(function (entry) {
      var button = el("button", {
        className: "decision-button",
        type: "button",
        textContent: entry.label,
      });
      button.addEventListener("click", function () {
        submitQueueDecision(card, job.id, entry.value);
      });
      actions.appendChild(button);
    });
    card.appendChild(actions);
  }

  function queueRefreshError(error, generation, fields) {
    if (generation !== reviewViewGeneration || visibleView() !== REVIEW) return;
    renderError(
      document.getElementById("review-status"),
      "Decision saved, but the Review Queue could not be refreshed. " + error.message,
      function () {
        requestReviewQueueRefresh(generation, fields);
      }
    );
  }

  async function runReviewQueueRefresh(generation, dataGeneration, fields) {
    if (
      generation !== reviewViewGeneration ||
      dataGeneration !== reviewDataGeneration ||
      visibleView() !== REVIEW
    ) return false;
    var rankingGeneration = ++rankingContextGeneration;
    rankedJobContext = null;
    pendingRankedNavigation = null;
    var url =
      "/review-queue?grouped=" +
      (fields.grouped ? "true" : "false") +
      "&limit=" +
      fields.limit;
    try {
      var data = await postReportJson(url, buildProfilePayload(fields));
      if (
        generation !== reviewViewGeneration ||
        dataGeneration !== reviewDataGeneration ||
        visibleView() !== REVIEW
      ) return false;
      renderAuthoritativeQueue(data, fields, rankingGeneration);
      doneLoading(document.getElementById("review-status"));
      return true;
    } catch (error) {
      queueRefreshError(error, generation, fields);
      return false;
    }
  }

  function requestReviewQueueRefresh(generation, fields) {
    if (!fields || generation !== reviewViewGeneration || visibleView() !== REVIEW) return;
    reviewQueueRefreshDirty = true;
    var dataGeneration = reviewDataGeneration;
    if (
      reviewQueueRefreshActive &&
      reviewQueueRefreshOwner &&
      reviewQueueRefreshOwner.generation === generation &&
      reviewQueueRefreshOwner.dataGeneration === dataGeneration
    ) return;
    var owner = {
      generation: generation,
      dataGeneration: dataGeneration,
    };
    reviewQueueRefreshActive = true;
    reviewQueueRefreshOwner = owner;
    (async function () {
      try {
        while (
          reviewQueueRefreshDirty &&
          reviewQueueRefreshOwner === owner &&
          owner.generation === reviewViewGeneration &&
          owner.dataGeneration === reviewDataGeneration &&
          visibleView() === REVIEW
        ) {
          reviewQueueRefreshDirty = false;
          await runReviewQueueRefresh(owner.generation, owner.dataGeneration, fields);
        }
      } finally {
        if (reviewQueueRefreshOwner === owner) {
          reviewQueueRefreshOwner = null;
          reviewQueueRefreshActive = false;
        }
      }
    })();
  }

  function findQueueCard(jobId) {
    var content = document.getElementById("review-content");
    if (!content) return null;
    var links = content.querySelectorAll('a[href="#job/' + jobId + '"]');
    for (var index = 0; index < links.length; index++) {
      var card = links[index].closest(".job-card");
      if (card && card.querySelector(".decision-button")) return card;
    }
    return null;
  }

  async function submitQueueDecision(card, jobId, decision) {
    // Per-card duplicate protection: a second activation for this card while
    // its PUT is in flight is ignored outright. Other queue cards are not
    // consulted, so they remain usable by design.
    if (reviewCardInFlight[jobId]) return;
    if (visibleView() !== REVIEW || !lastReviewFields) return;
    var generation = reviewViewGeneration;
    var fields = lastReviewFields;
    var operation = { id: ++reviewOperationSeq, generation: generation };
    reviewCardInFlight[jobId] = operation;
    if (card && card.isConnected) setQueueDecisionDisabled(card, true);
    try {
      await putJson("/review-decisions/" + jobId, { decision: decision });
      // The mutation has settled. Keep this now-stale card's decision controls
      // unavailable until the authoritative queue replaces it, but do not
      // continue announcing the card as busy while a separately retryable
      // queue refresh is pending.
      if (card && card.isConnected) card.removeAttribute("aria-busy");
      requestReviewQueueRefresh(generation, fields);
    } catch (error) {
      if (generation !== reviewViewGeneration || visibleView() !== REVIEW) return;
      if (card && card.isConnected) {
        setQueueDecisionDisabled(card, false);
        replaceSectionError(card, "The review decision could not be saved.", error, function () {
          submitQueueDecision(card, jobId, decision);
        });
      } else {
        renderError(document.getElementById("review-status"), "The review decision could not be saved. " + error.message, function () {
          submitQueueDecision(findQueueCard(jobId), jobId, decision);
        });
      }
    } finally {
      if (reviewCardInFlight[jobId] === operation) delete reviewCardInFlight[jobId];
    }
  }

  // ---------------------------------------------------------------------------
  // Reviewed opportunities workspace (#reviewed)
  //
  // This view is intentionally a narrow GET read model. It never calls the
  // report or review-queue computations: decisions and counts move only when
  // the server's reviewed-opportunities response is rendered.
  // ---------------------------------------------------------------------------

  function reviewedItemJob(item) {
    if (!isObject(item)) return null;
    if (isObject(item.group) && isObject(item.group.representative)) {
      return item.group.representative;
    }
    return isObject(item.job) ? item.job : null;
  }

  function reviewedItemOwnerId(item) {
    var owner = item && item.decision_job_id;
    return isFiniteNumber(owner) && owner >= 1
      ? owner
      : null;
  }

  function reviewedCounts(data) {
    var counts = isObject(data && data.counts) ? data.counts : {};
    return {
      shortlist: countValue(counts.shortlist),
      review_later: countValue(counts.review_later),
      dismiss: countValue(counts.dismiss),
    };
  }

  function reviewedUrl(state) {
    return "/reviewed-opportunities?decision=" + encodeURIComponent(state.decision) +
      "&grouped=" + (state.grouped ? "true" : "false") +
      "&offset=" + state.offset + "&limit=" + state.limit;
  }

  function reviewedTabLabel(decision) {
    return decision === "shortlist" ? "Shortlist" :
      decision === "review_later" ? "Review later" : "Dismissed";
  }

  function renderReviewedView(options) {
    options = options || {};
    var refreshing = options.refresh === true;
    reviewedDataGeneration++;
    var generation = reviewedViewGeneration;
    var seq = ++reviewedRequestSeq;
    var state = {
      decision: reviewedState.decision,
      grouped: reviewedState.grouped,
      offset: reviewedState.offset,
      limit: reviewedState.limit,
    };
    var status = document.getElementById("reviewed-status");
    var content = document.getElementById("reviewed-content");
    if (!status || !content) return Promise.resolve();
    setLoading(status, "Loading reviewed opportunities…");
    return fetchJson(reviewedUrl(state)).then(function (data) {
      if (seq !== reviewedRequestSeq || generation !== reviewedViewGeneration ||
          visibleView() !== REVIEWED) return;
      if (!isObject(data) || !Array.isArray(data.items) || !isFiniteNumber(data.total)) {
        throw new Error("The /reviewed-opportunities response did not match the expected shape.");
      }
      renderReviewedContent(content, data, state, generation);
      doneLoading(status);
    }).catch(function (error) {
      if (seq !== reviewedRequestSeq || generation !== reviewedViewGeneration ||
          visibleView() !== REVIEWED) return;
      if (!refreshing) clearText(content);
      renderError(status, refreshing
        ? "Decision saved, but Reviewed could not be refreshed. " + error.message
        : error.message, function () { renderReviewedView({ refresh: refreshing }); });
    });
  }

  function renderReviewedContent(content, data, state, generation) {
    clearText(content);
    var counts = reviewedCounts(data);
    var toolbar = el("div", { className: "reviewed-toolbar" });
    toolbar.setAttribute("role", "tablist");
    ["shortlist", "review_later", "dismiss"].forEach(function (decision) {
      var tab = el("button", { className: "reviewed-tab", type: "button",
        textContent: reviewedTabLabel(decision) + " (" + counts[decision === "dismiss" ? "dismiss" : decision] + ")" });
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-selected", String(state.decision === decision));
      tab.setAttribute("aria-controls", "reviewed-list");
      tab.disabled = state.decision === decision;
      tab.addEventListener("click", function () {
        reviewedState.decision = decision;
        reviewedState.offset = 0;
        renderReviewedView();
      });
      toolbar.appendChild(tab);
    });
    var label = el("label", { className: "inline-label" });
    var toggle = el("input", { type: "checkbox" });
    toggle.checked = state.grouped;
    toggle.addEventListener("change", function () {
      reviewedState.grouped = toggle.checked;
      reviewedState.offset = 0;
      renderReviewedView();
    });
    label.appendChild(toggle);
    label.appendChild(document.createTextNode(" Collapse duplicates"));
    toolbar.appendChild(label);
    content.appendChild(toolbar);
    var summary = el("p", { className: "result-summary", textContent:
      "Showing " + data.items.length + " of " + countValue(data.total) + " " +
      reviewedTabLabel(state.decision).toLowerCase() + " opportunities." });
    content.appendChild(summary);
    var list = el("ul", { className: "job-list", id: "reviewed-list" });
    data.items.forEach(function (item) {
      var job = reviewedItemJob(item);
      var ownerId = reviewedItemOwnerId(item);
      if (!job || ownerId === null) return;
      var group = isObject(item.group) ? item.group : null;
      var card = buildJobCard(job, null, null, group);
      if (!card) return;
      var details = card.querySelector('a[href^="#job/"]');
      if (details) details.href = "#job/" + ownerId;
      if (item.conflict === true) {
        card.appendChild(el("p", { className: "reviewed-conflict", textContent:
          "Conflicting decisions exist across these listings; the canonical decision owner is job " + ownerId + "." }));
      }
      attachReviewedDecisionActions(card, ownerId, item.review_decision);
      list.appendChild(card);
    });
    if (list.childNodes.length === 0) {
      content.appendChild(el("p", { className: "empty-note", textContent: "No reviewed opportunities in this view." }));
    } else {
      content.appendChild(list);
    }
    var pager = el("div", { className: "reviewed-pager" });
    var previous = el("button", { type: "button", textContent: "Previous" });
    previous.disabled = state.offset <= 0;
    previous.addEventListener("click", function () {
      reviewedState.offset = Math.max(0, state.offset - state.limit); renderReviewedView();
    });
    var next = el("button", { type: "button", textContent: "Next" });
    next.disabled = state.offset + state.limit >= countValue(data.total);
    next.addEventListener("click", function () {
      reviewedState.offset = state.offset + state.limit; renderReviewedView();
    });
    pager.appendChild(previous); pager.appendChild(next); content.appendChild(pager);
  }

  function setReviewedDecisionDisabled(card, disabled) {
    card.querySelectorAll(".reviewed-decision-button").forEach(function (button) {
      button.disabled = disabled;
    });
    if (disabled) card.setAttribute("aria-busy", "true");
    else card.removeAttribute("aria-busy");
  }

  function attachReviewedDecisionActions(card, jobId, currentDecision) {
    var actions = el("div", { className: "decision-actions" });
    actions.setAttribute("role", "group"); actions.setAttribute("aria-label", "Reviewed opportunity decision");
    REVIEW_DECISIONS.forEach(function (entry) {
      var button = el("button", { className: "decision-button reviewed-decision-button", type: "button", textContent: entry.label });
      button.setAttribute("aria-pressed", String(currentDecision === entry.value));
      button.addEventListener("click", function () { submitReviewedDecision(card, jobId, entry.value); });
      actions.appendChild(button);
    });
    var clear = el("button", { className: "decision-button reviewed-decision-button", type: "button", textContent: "Clear decision" });
    clear.addEventListener("click", function () { clearReviewedDecision(card, jobId); });
    actions.appendChild(clear); card.appendChild(actions);
  }

  function requestReviewedRefresh() {
    if (visibleView() !== REVIEWED) return;
    reviewedRefreshDirty = true;
    var generation = reviewedViewGeneration;
    if (reviewedRefreshActive && reviewedRefreshOwner && reviewedRefreshOwner.generation === generation) return;
    var owner = { generation: generation, dataGeneration: reviewedDataGeneration };
    reviewedRefreshActive = true; reviewedRefreshOwner = owner;
    (async function () {
      try {
        while (reviewedRefreshDirty && reviewedRefreshOwner === owner &&
          owner.generation === reviewedViewGeneration &&
          owner.dataGeneration === reviewedDataGeneration && visibleView() === REVIEWED) {
          reviewedRefreshDirty = false;
          await renderReviewedView({ refresh: true });
          owner.dataGeneration = reviewedDataGeneration;
        }
      } finally {
        if (reviewedRefreshOwner === owner) { reviewedRefreshOwner = null; reviewedRefreshActive = false; }
      }
    })();
  }

  function reviewedRefreshError(error, operation) {
    if (operation.generation !== reviewedViewGeneration || visibleView() !== REVIEWED) return;
    renderError(document.getElementById("reviewed-status"),
      "Decision saved, but Reviewed could not be refreshed. " + error.message,
      requestReviewedRefresh);
  }

  async function submitReviewedMutation(card, jobId, decision, clear) {
    if (reviewedCardInFlight[jobId] || visibleView() !== REVIEWED) return;
    var operation = { id: ++reviewedOperationSeq, generation: reviewedViewGeneration };
    reviewedCardInFlight[jobId] = operation; setReviewedDecisionDisabled(card, true);
    try {
      if (clear) await deleteJson("/review-decisions/" + jobId);
      else await putJson("/review-decisions/" + String(jobId), { decision: decision });
      if (card && card.isConnected) card.removeAttribute("aria-busy");
      requestReviewedRefresh();
    } catch (error) {
      if (operation.generation !== reviewedViewGeneration || visibleView() !== REVIEWED) return;
      setReviewedDecisionDisabled(card, false);
      replaceSectionError(card, "The review decision could not be saved.", error, function () {
        submitReviewedMutation(card, jobId, decision, clear);
      });
    } finally {
      if (reviewedCardInFlight[jobId] === operation) delete reviewedCardInFlight[jobId];
    }
  }

  function submitReviewedDecision(card, jobId, decision) { submitReviewedMutation(card, jobId, decision, false); }
  function clearReviewedDecision(card, jobId) { submitReviewedMutation(card, jobId, null, true); }

  // ---------------------------------------------------------------------------
  // Applications workspace (#applications)
  //
  // This is a flat, concrete-job-owned read model. The server supplies counts,
  // filtering, ordering, and pagination; the browser never groups or sorts
  // application records and never performs optimistic updates.
  // ---------------------------------------------------------------------------

  var APPLICATION_VIEWS = [
    { value: "active", label: "All active" },
    { value: "interested", label: "Interested" },
    { value: "applied", label: "Applied" },
    { value: "interview", label: "Interview" },
    { value: "offer", label: "Offer" },
    { value: "completed", label: "Completed" },
    { value: "all", label: "All" },
  ];

  function applicationsUrl(state) {
    return "/application-pipeline?view=" + encodeURIComponent(state.view) +
      "&offset=" + state.offset + "&limit=" + state.limit;
  }

  function applicationCounts(data) {
    var counts = isObject(data && data.counts) ? data.counts : {};
    return {
      interested: countValue(counts.interested), applied: countValue(counts.applied),
      interview: countValue(counts.interview), offer: countValue(counts.offer),
      rejected: countValue(counts.rejected), withdrawn: countValue(counts.withdrawn),
      active: countValue(counts.active), completed: countValue(counts.completed),
      all: countValue(counts.all),
    };
  }

  function applicationItemJob(item) {
    if (!isObject(item)) return null;
    return isObject(item.job) ? item.job : item;
  }

  function applicationItemStatus(item) {
    return isObject(item) && typeof item.current_status === "string" ? item.current_status : "";
  }

  function buildApplicationPipelineCard(item) {
    if (!isObject(item) || !isFiniteNumber(item.job_id) || item.job_id < 1) return null;
    var job = applicationItemJob(item);
    if (!job) return null;
    var card = el("li", { className: "job-card application-card" });
    card.setAttribute("data-job-id", String(item.job_id));
    var status = applicationItemStatus(item);
    card.appendChild(el("p", { className: "application-status", textContent: applicationStatusLabel(status) }));
    card.appendChild(el("h2", { className: "job-title", textContent: displayText(job.title) }));
    var fields = el("dl", { className: "job-fields" });
    addField(fields, "Company", job.company);
    addField(fields, "Location", job.location);
    addField(fields, "Source", job.source);
    addField(fields, "Last updated", displayDate(item.updated_at));
    addField(fields, "First tracked", displayDate(item.created_at));
    addField(fields, "Application owner", "job " + item.job_id);
    card.appendChild(fields);
    if (isObject(item.preparation)) {
      var prep = el("p", { className: "preparation-summary", textContent: "Preparation: " + prepLabel(item.preparation.category) + " · Blocking: " + countValue(item.preparation.blocking_count) });
      card.appendChild(prep);
    }
    var actions = el("div", { className: "job-actions" });
    var details = jobDetailsLink({ id: item.job_id });
    if (details) {
      details.textContent = "Open Job Detail";
      actions.appendChild(details);
    }
    var original = safeExternalLink(job.url, "Open original");
    if (original) actions.appendChild(original);
    card.appendChild(actions);
    var statusActions = el("div", { className: "status-actions" });
    statusActions.setAttribute("role", "group");
    statusActions.setAttribute("aria-label", "Application status for job " + item.job_id);
    APPLICATION_STATUSES.forEach(function (entry) {
      var button = el("button", { className: "status-button", type: "button", textContent: entry.label });
      button.setAttribute("aria-pressed", String(status === entry.value));
      if (status === entry.value) button.classList.add("current");
      button.addEventListener("click", function () {
        submitPipelineApplication(card, statusActions, item.job_id, entry.value);
      });
      statusActions.appendChild(button);
    });
    card.appendChild(statusActions);
    return card;
  }

  function renderApplicationsView(options) {
    options = options || {};
    var refreshing = options.refresh === true;
    if (applicationsOriginState && !refreshing) {
      applicationsState.view = applicationsOriginState.view;
      applicationsState.offset = applicationsOriginState.offset;
      applicationsState.limit = applicationsOriginState.limit;
      applicationsOriginState = null;
    }
    applicationsDataGeneration++;
    var generation = applicationsViewGeneration;
    var seq = ++applicationsRequestSeq;
    var state = { view: applicationsState.view, offset: applicationsState.offset, limit: applicationsState.limit };
    var status = document.getElementById("applications-status");
    var content = document.getElementById("applications-content");
    if (!status || !content) return Promise.resolve();
    setLoading(status, "Loading applications…");
    return fetchJson(applicationsUrl(state)).then(function (data) {
      if (seq !== applicationsRequestSeq || generation !== applicationsViewGeneration || visibleView() !== APPLICATIONS) return;
      if (!isObject(data) || !Array.isArray(data.items) || !isObject(data.counts) || !isFiniteNumber(data.total) ||
          data.view !== state.view || data.offset !== state.offset || data.limit !== state.limit) {
        throw new Error("The /application-pipeline response did not match the expected shape.");
      }
      // A mutation can remove the final item on a later page. Correct the
      // requested offset from the authoritative total and issue one bounded
      // read rather than stranding the user on an empty page.
      if (data.items.length === 0 && data.total > 0 && state.offset >= data.total) {
        applicationsState.offset = Math.max(0, Math.floor((data.total - 1) / state.limit) * state.limit);
        if (applicationsState.offset !== state.offset) {
          renderApplicationsView({ refresh: refreshing });
          return;
        }
      }
      renderApplicationsContent(content, data, state);
      doneLoading(status);
      if (refreshing) status.textContent = "Application status saved. Applications refreshed.";
      restoreApplicationsFocus(status, content);
    }).catch(function (error) {
      if (seq !== applicationsRequestSeq || generation !== applicationsViewGeneration || visibleView() !== APPLICATIONS) return;
      if (!refreshing) clearText(content);
      status.parentNode.removeAttribute("aria-busy");
      renderError(status, refreshing ? "Application status saved, but Applications could not be refreshed. " + error.message : error.message,
        function () { renderApplicationsView({ refresh: refreshing }); });
    });
  }

  function restoreApplicationsFocus(status, content) {
    var target = applicationsFocusTarget;
    applicationsFocusTarget = null;
    if (!target) return;
    var card = content.querySelector('[data-job-id="' + String(target.jobId) + '"]');
    if (card) {
      var button = card.querySelector('.status-button[aria-pressed="true"]');
      if (button) { button.focus(); return; }
      card.focus();
      return;
    }
    var firstButton = content.querySelector(".application-card .status-button");
    if (firstButton) { firstButton.focus(); return; }
    status.textContent = "Applications updated.";
    status.focus();
  }

  function renderApplicationsContent(content, data, state) {
    clearText(content);
    var counts = applicationCounts(data);
    var toolbar = el("div", { className: "applications-toolbar" });
    toolbar.setAttribute("role", "group");
    toolbar.setAttribute("aria-label", "Application filters");
    APPLICATION_VIEWS.forEach(function (entry) {
      var value = counts[entry.value];
      var tab = el("button", { className: "application-filter", type: "button", textContent: entry.label + " (" + value + ")" });
      tab.setAttribute("aria-pressed", String(state.view === entry.value));
      tab.disabled = state.view === entry.value;
      tab.addEventListener("click", function () { applicationsState.view = entry.value; applicationsState.offset = 0; renderApplicationsView(); });
      toolbar.appendChild(tab);
    });
    content.appendChild(toolbar);
    var total = countValue(data.total);
    if (data.items.length === 0) {
      content.appendChild(el("p", { className: "empty-note", textContent: total === 0 ? "No applications in this view." : "No applications on this page." }));
    } else {
      var start = state.offset + 1;
      var end = state.offset + data.items.length;
      content.appendChild(el("p", { className: "result-summary", textContent: "Showing " + start + "–" + end + " of " + total + " applications." }));
      var list = el("ul", { className: "job-list", id: "applications-list" });
      data.items.forEach(function (item) { var card = buildApplicationPipelineCard(item); if (card) list.appendChild(card); });
      content.appendChild(list);
    }
    var pager = el("div", { className: "applications-pager" });
    var previous = el("button", { type: "button", textContent: "Previous" });
    previous.disabled = state.offset <= 0;
    previous.addEventListener("click", function () { applicationsState.offset = Math.max(0, state.offset - state.limit); renderApplicationsView(); });
    var next = el("button", { type: "button", textContent: "Next" });
    next.disabled = state.offset + state.limit >= total;
    next.addEventListener("click", function () { applicationsState.offset = state.offset + state.limit; renderApplicationsView(); });
    pager.appendChild(previous); pager.appendChild(next); content.appendChild(pager);
  }

  function setPipelineActionsDisabled(group, disabled) {
    group.querySelectorAll("button").forEach(function (button) { button.disabled = disabled; });
    if (disabled) group.setAttribute("aria-busy", "true"); else group.removeAttribute("aria-busy");
  }

  function requestApplicationsRefresh() {
    if (visibleView() !== APPLICATIONS) return;
    applicationsRefreshDirty = true;
    var generation = applicationsViewGeneration;
    if (applicationsRefreshActive && applicationsRefreshOwner && applicationsRefreshOwner.generation === generation) return;
    var owner = { generation: generation, dataGeneration: applicationsDataGeneration };
    applicationsRefreshActive = true; applicationsRefreshOwner = owner;
    (async function () {
      try {
        while (applicationsRefreshDirty && applicationsRefreshOwner === owner && owner.generation === applicationsViewGeneration && visibleView() === APPLICATIONS) {
          applicationsRefreshDirty = false;
          await renderApplicationsView({ refresh: true });
          owner.dataGeneration = applicationsDataGeneration;
        }
      } finally {
        if (applicationsRefreshOwner === owner) { applicationsRefreshOwner = null; applicationsRefreshActive = false; }
      }
    })();
  }

  async function submitPipelineApplication(card, group, jobId, status) {
    if (applicationsCardInFlight[jobId] || visibleView() !== APPLICATIONS) return;
    var operation = { id: ++applicationsOperationSeq, generation: applicationsViewGeneration };
    applicationsCardInFlight[jobId] = operation;
    applicationsFocusTarget = { jobId: jobId, status: status };
    setPipelineActionsDisabled(group, true);
    try {
      await putJson("/applications/" + jobId, { status: status });
      requestApplicationsRefresh();
    } catch (error) {
      if (operation.generation !== applicationsViewGeneration || visibleView() !== APPLICATIONS) return;
      setPipelineActionsDisabled(group, false);
      replaceSectionError(card, "The application status could not be saved.", error, function () {
        submitPipelineApplication(card, group, jobId, status);
      });
    } finally {
      if (applicationsCardInFlight[jobId] === operation) delete applicationsCardInFlight[jobId];
    }
  }

  // ---------------------------------------------------------------------------
  // Reminders overview view (#reminders)
  //
  // Read-only presentation of the OPEN reminders, split into three sections by
  // the GET /reminders endpoint's own state/as_of/before/after semantics. All
  // open reminders are classified by comparing their due time to ONE "now"
  // captured when the view renders:
  //
  //   Overdue       state=overdue&as_of=now      open, due before now
  //   Due soon      state=open&after=now&        open, due within the next
  //                before=now+24h                 24 hours (both ends included)
  //   Upcoming      state=upcoming&as_of=        open, due after now+24h
  //                now+24h
  //
  // The three queries are mutually exclusive and together cover every open
  // reminder: a reminder due at exactly now lands in Due soon (after=now and
  // before=now+24h are both inclusive), and one due at exactly now+24h also
  // lands in Due soon (before=now+24h is inclusive). Completed reminders are
  // deliberately not shown. No classification, sorting, or grouping is
  // re-implemented here: the endpoint returns each section already ordered
  // (due_at ASC, then id ASC) and the view renders that server order verbatim.
  // Due times are displayed in the browser's local time from the
  // offset-bearing ISO value -- never a client-created timestamp -- so the
  // instant is exact and the timezone is the browser's, never a guess.
  //
  // The view is strictly read-only (three GETs via fetchJson) with no
  // mutation controls; every job row links to its canonical Job Detail view
  // (#job/<id>) where reminder actions live.
  // ---------------------------------------------------------------------------

  // How far ahead "due soon" reaches: 24 hours from the captured now.
  var REMINDERS_HORIZON_MS = 24 * 60 * 60 * 1000;

  // The three overview sections in display order. Each query is built from the
  // same captured now (and the horizon derived from it).
  var REMINDER_OVERVIEW_SECTIONS = [
    { key: "overdue", heading: "Overdue" },
    { key: "dueSoon", heading: "Due soon" },
    { key: "upcoming", heading: "Upcoming" },
  ];

  // Builds the three GET /reminders query strings from one captured now.
  function reminderOverviewUrls(nowIso, horizonIso) {
    var overdue = new URLSearchParams();
    overdue.set("state", "overdue");
    overdue.set("as_of", nowIso);
    var dueSoon = new URLSearchParams();
    dueSoon.set("state", "open");
    dueSoon.set("after", nowIso);
    dueSoon.set("before", horizonIso);
    var upcoming = new URLSearchParams();
    upcoming.set("state", "upcoming");
    upcoming.set("as_of", horizonIso);
    return {
      overdue: "/reminders?" + overdue,
      dueSoon: "/reminders?" + dueSoon,
      upcoming: "/reminders?" + upcoming,
    };
  }

  // Formats a Date as a UTC ISO-8601 timestamp with an explicit Z suffix
  // (YYYY-MM-DDTHH:MM:SSZ), which the API's timestamp parsing accepts. The
  // representation is built from the UTC accessors directly -- never the
  // Date's own ISO shortcut -- so the caller controls the exact output.
  function utcTimestamp(value) {
    var year = value.getUTCFullYear();
    var month = String(value.getUTCMonth() + 1).padStart(2, "0");
    var day = String(value.getUTCDate()).padStart(2, "0");
    var hour = String(value.getUTCHours()).padStart(2, "0");
    var minute = String(value.getUTCMinutes()).padStart(2, "0");
    var second = String(value.getUTCSeconds()).padStart(2, "0");
    return (
      year + "-" + month + "-" + day + "T" + hour + ":" + minute + ":" + second + "Z"
    );
  }

  // Renders an offset-bearing ISO timestamp (e.g. 2026-08-10T14:00:00+02:00 or
  // 2026-08-10T12:00:00Z) as a local-time YYYY-MM-DD HH:MM string. The offset
  // is parsed from the string so the instant is exact regardless of the
  // browser's timezone, then the local part is formatted with the browser's
  // local getters. Unusable values render as "—".
  function displayLocalDateTime(value) {
    var match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/.exec(
      typeof value === "string" ? value : ""
    );
    if (!match) return "—";
    var offsetMinutes = 0;
    if (match[7] !== "Z") {
      var sign = match[7].charAt(0) === "-" ? -1 : 1;
      var offsetHours = parseInt(match[7].slice(1, 3), 10);
      var offsetMins = parseInt(match[7].slice(4, 6), 10);
      offsetMinutes = sign * (offsetHours * 60 + offsetMins);
    }
    var instant =
      Date.UTC(
        parseInt(match[1], 10),
        parseInt(match[2], 10) - 1,
        parseInt(match[3], 10),
        parseInt(match[4], 10),
        parseInt(match[5], 10),
        match[6] ? parseInt(match[6], 10) : 0
      ) -
      offsetMinutes * 60 * 1000;
    var local = new Date(instant);
    var month = String(local.getMonth() + 1).padStart(2, "0");
    var day = String(local.getDate()).padStart(2, "0");
    var hour = String(local.getHours()).padStart(2, "0");
    var minute = String(local.getMinutes()).padStart(2, "0");
    return (
      local.getFullYear() + "-" + month + "-" + day + " " + hour + ":" + minute
    );
  }

  // Renders one open reminder row inside an overview section. Only public
  // fields are shown (kind, local due time, job title/company); the "View
  // details" hash link opens the canonical Job Detail view. No mutation
  // controls exist here.
  function buildReminderOverviewRow(reminder) {
    var item = el("li", { className: "reminder-item reminder-overview-item" });
    item.appendChild(
      el("span", {
        className: "reminder-kind",
        textContent: reminderKindLabel(reminder.kind),
      })
    );
    item.appendChild(
      el("span", {
        className: "reminder-due",
        textContent: displayLocalDateTime(reminder.due_at),
      })
    );
    var job = isObject(reminder.job) ? reminder.job : null;
    item.appendChild(
      el("span", {
        className: "reminder-job",
        textContent:
          job && typeof job.title === "string" && job.title.length > 0
            ? job.title
            : "—",
      })
    );
    if (job && typeof job.company === "string" && job.company.length > 0) {
      item.appendChild(
        el("span", { className: "reminder-company", textContent: job.company })
      );
    }
    if (job && isFiniteNumber(job.id) && job.id >= 1) {
      item.appendChild(
        el("a", {
          className: "job-link",
          href: "#job/" + job.id,
          textContent: "View details",
        })
      );
    }
    return item;
  }

  // Fills one overview section from its GET /reminders response. Empty
  // sections are omitted entirely; a failed/malformed section gets a
  // section-local error box whose Retry refetches only that section. Returns
  // "items" when at least one row was rendered, "empty" when the section
  // succeeded with no reminders, and "error" when it failed.
  function fillRemindersSection(key, state, container, body, nowIso, horizonIso) {
    body.textContent = "";
    if (state.status === "rejected") {
      body.appendChild(
        sectionErrorBox("The reminders could not be loaded.", state.reason, function () {
          retryRemindersSection(key, container, body, nowIso, horizonIso);
        })
      );
      return "error";
    }
    var data = state.value;
    if (!isObject(data) || !Array.isArray(data.items)) {
      body.appendChild(
        sectionErrorBox("The reminders could not be loaded.", null, function () {
          retryRemindersSection(key, container, body, nowIso, horizonIso);
        })
      );
      return "error";
    }
    if (data.items.length === 0) {
      container.remove();
      return "empty";
    }
    var list = el("ul", { className: "reminder-list" });
    data.items.forEach(function (reminder) {
      if (!isObject(reminder)) return;
      list.appendChild(buildReminderOverviewRow(reminder));
    });
    body.appendChild(list);
    return "items";
  }

  // Re-fetches one overview section after a Retry click. Captures the request
  // token before awaiting so a late response for a superseded render is
  // discarded.
  async function retryRemindersSection(key, container, body, nowIso, horizonIso) {
    var seq = remindersRequestSeq;
    var urls = reminderOverviewUrls(nowIso, horizonIso);
    var data;
    try {
      data = await fetchJson(urls[key]);
    } catch (error) {
      if (seq !== remindersRequestSeq) return;
      body.textContent = "";
      body.appendChild(
        sectionErrorBox("The reminders could not be loaded.", error, function () {
          retryRemindersSection(key, container, body, nowIso, horizonIso);
        })
      );
      return;
    }
    if (seq !== remindersRequestSeq) return;
    fillRemindersSection(
      key,
      { status: "fulfilled", value: data },
      container,
      body,
      nowIso,
      horizonIso
    );
  }

  // Renders the three overview sections into the view content in the exact
  // section order, rendering each section's server-returned order verbatim.
  function renderRemindersContent(content, results, nowIso, horizonIso) {
    content.textContent = "";
    var renderedAny = false;
    var succeededAny = false;
    var failedAny = false;
    REMINDER_OVERVIEW_SECTIONS.forEach(function (section) {
      var container = el("section", {
        className: "chunk",
        textContent: section.heading,
      });
      var body = el("div", { className: "section-body" });
      container.appendChild(body);
      var outcome = fillRemindersSection(
        section.key,
        results[section.key],
        container,
        body,
        nowIso,
        horizonIso
      );
      if (outcome === "items") {
        renderedAny = true;
        succeededAny = true;
        content.appendChild(container);
      } else if (outcome === "empty") {
        succeededAny = true;
      } else {
        failedAny = true;
        content.appendChild(container);
      }
    });
    if (!renderedAny && !failedAny && succeededAny) {
      content.appendChild(
        el("p", { className: "empty-note", textContent: "No reminders." })
      );
    }
  }

  async function renderRemindersView() {
    var seq = ++remindersRequestSeq;
    var status = document.getElementById("reminders-status");
    var content = document.getElementById("reminders-content");
    var now = new Date();
    var nowIso = utcTimestamp(now);
    var horizonIso = utcTimestamp(new Date(now.getTime() + REMINDERS_HORIZON_MS));
    var urls = reminderOverviewUrls(nowIso, horizonIso);
    setLoading(status, "Loading reminders…");
    clearText(content);
    var results = {};
    try {
      var settled = await Promise.allSettled(
        ["overdue", "dueSoon", "upcoming"].map(function (key) {
          return fetchJson(urls[key]);
        })
      );
      settled.forEach(function (item, index) {
        results[["overdue", "dueSoon", "upcoming"][index]] = item;
      });
    } catch (error) {
      if (seq !== remindersRequestSeq) return;
      clearText(content);
      renderError(status, error.message, renderRemindersView);
      return;
    }
    if (seq !== remindersRequestSeq) return;
    var allFailed = ["overdue", "dueSoon", "upcoming"].every(function (key) {
      var state = results[key];
      return (
        state.status === "rejected" ||
        !isObject(state.value) ||
        !Array.isArray(state.value.items)
      );
    });
    if (allFailed) {
      clearText(content);
      renderError(
        status,
        results.overdue.status === "rejected" ? results.overdue.reason.message : null,
        renderRemindersView
      );
      return;
    }
    renderRemindersContent(content, results, nowIso, horizonIso);
    doneLoading(status);
  }

  // ---------------------------------------------------------------------------
  // Job Detail view (#job/<id>)
  // ---------------------------------------------------------------------------
  // Today workspace (#today)
  //
  // GET /today is the sole authority for UTC classification, totals, ordering,
  // duplicate context, and application state. The browser only renders the
  // three already-classified collections and formats due_at for display.
  // ---------------------------------------------------------------------------

  function todayJob(item) {
    return isObject(item) && isObject(item.job) ? item.job : null;
  }

  function todayViewJobLink(job, label) {
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return null;
    return el("a", { className: "job-link", href: "#job/" + job.id, textContent: label || "View job" });
  }

  function buildTodayReminderCard(reminder) {
    var card = el("li", { className: "job-card today-reminder-card" });
    if (isFiniteNumber(reminder.reminder_id)) card.setAttribute("data-reminder-id", String(reminder.reminder_id));
    var job = todayJob(reminder);
    card.appendChild(el("h3", { className: "job-title", textContent: displayText(job && job.title) }));
    card.appendChild(el("p", { className: "reminder-kind", textContent: reminderKindLabel(reminder.kind) }));
    card.appendChild(el("p", { className: "reminder-due", textContent: "Due: " + displayLocalDateTime(reminder.due_at) }));
    if (job && job.company) card.appendChild(el("p", { className: "reminder-company", textContent: displayText(job.company) }));
    if (typeof reminder.active_application_status === "string" && reminder.active_application_status) {
      card.appendChild(el("p", { className: "application-status", textContent: "Application: " + applicationStatusLabel(reminder.active_application_status) }));
    }
    var actions = el("div", { className: "job-actions" });
    var complete = el("button", { className: "reminder-button", type: "button", textContent: "Complete", label: "Complete reminder" });
    complete.addEventListener("click", function () { completeTodayReminder(card, reminder); });
    actions.appendChild(complete);
    var link = todayViewJobLink(job, "View job");
    if (link) actions.appendChild(link);
    card.appendChild(actions);
    return card;
  }

  function buildTodayShortlistCard(item) {
    var card = el("li", { className: "job-card today-shortlist-card" });
    var group = isObject(item.group) ? item.group : null;
    var members = group && Array.isArray(group.members) ? group.members : [];
    var job = group && isObject(group.representative) ? group.representative :
      (members.length ? (isObject(members[0]) ? members[0] : null) : todayJob(item));
    card.appendChild(el("h3", { className: "job-title", textContent: displayText(job && job.title) }));
    if (job && job.company) card.appendChild(el("p", { className: "job-company", textContent: displayText(job.company) }));
    if (members.length > 1) card.appendChild(el("p", { className: "group-context", textContent: members.length + " source listings" }));
    if (item.conflict === true) card.appendChild(el("p", { className: "reviewed-conflict", textContent: "Conflicting decisions exist across these listings." }));
    var actions = el("div", { className: "job-actions" });
    var link = todayViewJobLink(job, "View job");
    if (link) actions.appendChild(link);
    actions.appendChild(el("a", { className: "job-link", href: "#reviewed", textContent: "Reviewed" }));
    card.appendChild(actions);
    return card;
  }

  function buildTodaySection(title, bucket, cardBuilder) {
    var section = el("section", { className: "chunk" });
    section.appendChild(el("h2", { textContent: title }));
    section.appendChild(el("p", { className: "result-summary", textContent: "" }));
    var list = el("ul", { className: "job-list" });
    var items = Array.isArray(bucket.items) ? bucket.items : [];
    items.forEach(function (item) {
      if (isObject(item)) {
        var card = cardBuilder(item);
        if (card) list.appendChild(card);
      }
    });
    var total = countValue(bucket.total);
    section.querySelector(".result-summary").textContent = String(total) + " needing attention.";
    if (total === 0) section.appendChild(el("p", { className: "empty-note", textContent: "Nothing here today." }));
    else section.appendChild(list);
    return section;
  }

  function renderTodayContent(content, data) {
    clearText(content);
    var links = el("p", { className: "today-workspace-links" });
    links.appendChild(el("span", { textContent: "Workspaces: " }));
    [[REMINDERS, "Reminders"], [APPLICATIONS, "Applications"], [REVIEWED, "Reviewed"]].forEach(function (entry, index) {
      if (index > 0) links.appendChild(document.createTextNode(" · "));
      links.appendChild(el("a", { href: "#" + entry[0], textContent: entry[1] }));
    });
    content.appendChild(links);
    var reminders = isObject(data.reminders) ? data.reminders : {};
    var shortlist = isObject(data.shortlist) ? data.shortlist : {};
    var overdue = isObject(reminders.overdue) ? reminders.overdue : { total: 0, items: [] };
    var due = isObject(reminders.due_today) ? reminders.due_today : { total: 0, items: [] };
    var unstarted = isObject(shortlist.unstarted) ? shortlist.unstarted : { total: 0, items: [] };
    var total = countValue(overdue.total) + countValue(due.total) + countValue(unstarted.total);
    if (total === 0) {
      content.appendChild(el("p", { className: "empty-note", textContent: "Nothing needs attention today." }));
      content.appendChild(el("p", { className: "today-boundary-note", textContent: "Reminder sections are classified by the server using UTC; due times are displayed in your browser's local time." }));
      return;
    }
    content.appendChild(buildTodaySection("Overdue reminders", overdue, buildTodayReminderCard));
    content.appendChild(buildTodaySection("Due later today", due, buildTodayReminderCard));
    content.appendChild(buildTodaySection("Shortlisted, not started", unstarted, buildTodayShortlistCard));
    content.appendChild(el("p", { className: "today-boundary-note", textContent: "Reminder sections are classified by the server using UTC; due times are displayed in your browser's local time." }));
  }

  async function renderTodayView(options) {
    options = options || {};
    var generation = todayViewGeneration;
    var seq = ++todayRequestSeq;
    todayDataGeneration++;
    var status = document.getElementById("today-status");
    var content = document.getElementById("today-content");
    if (!status || !content) return;
    setLoading(status, "Loading today…");
    if (!options.refresh) clearText(content);
    try {
      var data = await fetchJson("/today?limit=10");
      if (seq !== todayRequestSeq || generation !== todayViewGeneration || visibleView() !== TODAY) return;
      if (!isObject(data) || !isObject(data.reminders) || !isObject(data.shortlist)) throw new Error("The /today response did not match the expected shape.");
      renderTodayContent(content, data);
      doneLoading(status);
      if (options.saved) status.textContent = "Reminder saved. Today refreshed.";
      restoreTodayFocus(status, content);
    } catch (error) {
      if (seq !== todayRequestSeq || generation !== todayViewGeneration || visibleView() !== TODAY) return;
      if (options.saved) {
        status.parentNode.removeAttribute("aria-busy");
        renderError(status, "Reminder saved, but Today could not be refreshed. " + error.message, function () { renderTodayView({ refresh: true }); });
      } else {
        clearText(content);
        renderError(status, error.message, function () { renderTodayView(); });
      }
    }
  }

  function restoreTodayFocus(status, content) {
    var target = todayFocusTarget;
    todayFocusTarget = null;
    if (!target) return;
    var card = content.querySelector('[data-reminder-id="' + String(target.reminderId) + '"]');
    if (card) {
      var button = card.querySelector("button");
      if (button) { button.focus(); return; }
    }
    var next = content.querySelector(".today-reminder-card button, .today-shortlist-card a");
    if (next) { next.focus(); return; }
    status.textContent = "Today updated.";
    status.focus();
  }

  function requestTodayRefresh(saved) {
    if (visibleView() !== TODAY) return;
    todayRefreshDirty = true;
    var generation = todayViewGeneration;
    if (todayRefreshActive && todayRefreshOwner && todayRefreshOwner.generation === generation) return;
    var owner = { generation: generation, dataGeneration: todayDataGeneration };
    todayRefreshActive = true; todayRefreshOwner = owner;
    (async function () {
      try {
        while (todayRefreshDirty && todayRefreshOwner === owner && owner.generation === todayViewGeneration && visibleView() === TODAY) {
          todayRefreshDirty = false;
          await renderTodayView({ refresh: true, saved: saved === true });
          owner.dataGeneration = todayDataGeneration;
        }
      } finally {
        if (todayRefreshOwner === owner) { todayRefreshOwner = null; todayRefreshActive = false; }
      }
    })();
  }

  async function completeTodayReminder(card, reminder) {
    if (!isObject(reminder) || !isFiniteNumber(reminder.reminder_id) || todayReminderInFlight[reminder.reminder_id] || visibleView() !== TODAY) return;
    var operation = { id: ++todayOperationSeq, generation: todayViewGeneration };
    todayReminderInFlight[reminder.reminder_id] = operation;
    todayFocusTarget = { reminderId: reminder.reminder_id };
    card.setAttribute("aria-busy", "true");
    card.querySelectorAll("button").forEach(function (button) { button.disabled = true; });
    try {
      await putJson("/reminders/" + reminder.reminder_id + "/complete", {});
      if (operation.generation === todayViewGeneration && visibleView() === TODAY) requestTodayRefresh(true);
    } catch (error) {
      if (operation.generation !== todayViewGeneration || visibleView() !== TODAY) return;
      card.removeAttribute("aria-busy");
      card.querySelectorAll("button").forEach(function (button) { button.disabled = false; });
      if (error && error.status === 404) { requestTodayRefresh(false); return; }
      replaceSectionError(card, "The reminder could not be completed.", error, function () { completeTodayReminder(card, reminder); });
    } finally {
      if (todayReminderInFlight[reminder.reminder_id] === operation) delete todayReminderInFlight[reminder.reminder_id];
    }
  }

  // ---------------------------------------------------------------------------
  // Job Detail view (#job/<id>)
  //
  // Loads one stored job's detail through exactly five read GET endpoints
  // (GET /jobs/{id}, GET /jobs/{id}/group, GET /applications?job_id={id},
  // GET /review-decisions?job_id={id}, GET /reminders?job_id={id}), issued
  // concurrently via Promise.allSettled. The application and review reads use
  // the job-scoped list endpoints, so the normal absent state is an empty 200
  // list ("Not tracked." / "Not reviewed.") rather than a canonical 404; the
  // core job response is authoritative and renders the whole-page "Job not
  // found" state. A network/500/malformed core failure renders a whole-page
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
      application: "/applications?job_id=" + jobId,
      preparation: "/applications/" + jobId + "/preparation",
      review: "/review-decisions?job_id=" + jobId,
      reminders: "/reminders?job_id=" + jobId,
    };
  }

  async function loadJobDetailData(jobId) {
    var urls = jobDetailUrls(jobId);
    var keys = ["job", "group", "application", "preparation", "review", "reminders"];
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
    if (jobDetailOrigin === TODAY) {
      return el("a", { className: "job-back", href: "#today", textContent: "Back to Today" });
    }
    var label = jobDetailOrigin === REVIEW ? "Back to review" :
      jobDetailOrigin === REVIEWED ? "Back to reviewed" :
      jobDetailOrigin === APPLICATIONS ? "Back to applications" :
      jobDetailOrigin === MATCHING ? "Back to matching preferences" :
      jobDetailOrigin === SAVED_SEARCHES ? "Back to saved searches" : "Back to jobs";
    return el("a", {
      className: "job-back",
      href: "#" + jobDetailOrigin,
      textContent: label,
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
    applicationOperationActive = false;
    reminderOperationActive = false;
    preparationOperationActive = false;
    preparationDraft = null;
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
    content.appendChild(buildJobExplanationSection(job));
    fillDetailSection(content, job, "group", results.group);
    fillDetailSection(content, job, "application", results.application);
    fillDetailSection(content, job, "preparation", results.preparation);
    fillDetailSection(content, job, "review", results.review);
    fillDetailSection(content, job, "reminders", results.reminders);
  }

  function validJobExplanationContext(job) {
    if (rankedJobContext === null || !isObject(rankedJobContext.explanation)) return false;
    var explanation = rankedJobContext.explanation;
    var scope = explanation.scope;
    var listingCount = explanation.listing_count;
    var members = rankedJobContext.memberIds;
    var grouped = rankedJobContext.targetKind === "opportunity";
    var flat = rankedJobContext.targetKind === "job";
    if (rankedJobContext.surface === "saved-search") {
      if (!savedSearchState.snapshot || !isObject(savedSearchState.snapshot.saved_search) ||
        rankedJobContext.savedSearchId !== savedSearchState.snapshot.saved_search.id ||
        rankedJobContext.savedSearchUpdatedAt !== savedSearchState.snapshot.saved_search.updated_at ||
        rankedJobContext.runGeneration !== savedSearchState.snapshot.runGeneration) return false;
    }
    if (rankedJobContext.surface === "matching-preview") {
      if (!matchingPreviewSnapshot ||
        rankedJobContext.runGeneration !== matchingPreviewSnapshot.runGeneration) return false;
    }
    if (rankedJobContext.surface === "recommendations" ||
      rankedJobContext.surface === "review-queue") {
      if (!reviewSnapshot ||
        rankedJobContext.runGeneration !== reviewSnapshot.runGeneration) return false;
    }
    return (rankedJobContext.surface === "recommendations" ||
      rankedJobContext.surface === "review-queue" ||
      rankedJobContext.surface === "saved-search" ||
      rankedJobContext.surface === "matching-preview") &&
      rankedJobContext.generation === rankingContextGeneration &&
      rankedJobContext.jobId === job.id &&
      typeof rankedJobContext.targetIdentity === "string" && rankedJobContext.targetIdentity.length > 0 &&
      ((grouped && scope === "opportunity" && Array.isArray(members) &&
        members.indexOf(job.id) !== -1 && listingCount === members.length) ||
       (flat && scope === "job" && listingCount === 1));
  }

  function signedPoints(value) {
    return isFiniteNumber(value) && value > 0 ? "+" + value : String(countValue(value));
  }

  function buildJobExplanationSection(job) {
    var section = el("section", { className: "chunk ranking-explanation-detail" });
    section.appendChild(el("h2", { textContent: "Why this job?" }));
    if (!validJobExplanationContext(job)) {
      section.appendChild(el("p", { className: "empty-note", textContent:
        "No ranking context is available. Open this job from Matching Preview, Recommendations, Review Queue, or a ranked saved search to see why it matched." }));
      return section;
    }
    var explanation = rankedJobContext.explanation;
    section.appendChild(el("p", { className: "result-summary", textContent: explanation.summary }));
    var score = isObject(explanation.score) ? explanation.score : {};
    var fields = el("dl", { className: "job-fields explanation-score" });
    addField(fields, "Match score", String(countValue(score.match_score)));
    addField(fields, "Contribution total", signedPoints(score.contribution_total));
    addField(fields, "Score resolution", displayText(score.resolution));
    if (explanation.scope === "opportunity") {
      addField(fields, "Listings in result", String(countValue(explanation.listing_count)));
    }
    section.appendChild(fields);
    [["Matches", "matches"], ["Gaps", "gaps"], ["Unknowns", "unknowns"], ["Other factors", "other_factors"]].forEach(function (entry) {
      var factors = explanationFactors(explanation, entry[1]);
      if (factors.length === 0) return;
      section.appendChild(el("h3", { textContent: entry[0] }));
      var list = el("ul", { className: "explanation-factors explanation-detail-factors" });
      factors.forEach(function (factor) {
        var value = factorText(factor) + " (" + signedPoints(factor.points) + " points)";
        list.appendChild(el("li", { textContent: value }));
      });
      section.appendChild(list);
    });
    return section;
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
    if (sectionKey === "preparation") return "Preparation";
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
      renderApplication(body, job, settled, retry);
    } else if (sectionKey === "preparation") {
      renderPreparation(body, job, settled, retry);
    } else if (sectionKey === "review") {
      renderReview(body, job, settled, retry);
    } else {
      renderReminders(body, job, settled, retry);
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

  // Canonical application statuses (presentation labels only; the transmitted
  // values are the exact backend vocabulary: interested, applied, interview,
  // offer, rejected, withdrawn). All statuses are always available: the
  // backend permits any status -> any other status and this view must never
  // invent a linear state machine or disable a status based on the current
  // one. Repeating the current status is allowed and relies on backend
  // idempotency.
  var APPLICATION_STATUSES = [
    { value: "interested", label: "Interested" },
    { value: "applied", label: "Applied" },
    { value: "interview", label: "Interview" },
    { value: "offer", label: "Offer" },
    { value: "rejected", label: "Rejected" },
    { value: "withdrawn", label: "Withdrawn" },
  ];

  function applicationStatusLabel(value) {
    for (var index = 0; index < APPLICATION_STATUSES.length; index++) {
      if (APPLICATION_STATUSES[index].value === value) {
        return APPLICATION_STATUSES[index].label;
      }
    }
    return value;
  }

  function renderApplication(body, job, settled, retry) {
    if (settled.status === "rejected") {
      // Any rejection (network/500/503) is a genuine section error with Retry.
      // The canonical singular 404 is never used by this read path.
      body.appendChild(
        sectionErrorBox(
          "The application status could not be loaded.",
          settled.reason,
          retry
        )
      );
      return;
    }
    var data = settled.value;
    if (!isObject(data) || !Array.isArray(data.items)) {
      body.appendChild(
        sectionErrorBox(
          "The application response did not match the expected shape.",
          null,
          retry
        )
      );
      return;
    }
    // At most one current application per job (the applications.job_id UNIQUE
    // constraint guarantees it); more than one would expose a data-integrity
    // problem that must never be silently hidden by picking an arbitrary row.
    if (data.items.length > 1) {
      body.appendChild(
        sectionErrorBox(
          "The application response did not match the expected shape.",
          null,
          retry
        )
      );
      return;
    }
    if (data.items.length === 0) {
      // The empty 200 list is the normal absent state: the job is not
      // tracked yet. It is never an error, and the status actions must
      // still render so the first application state can be assigned.
      body.appendChild(
        el("p", { className: "empty-note", textContent: "Not tracked." })
      );
      body.appendChild(buildApplicationStatusActions(body, job, null));
      return;
    }
    var app = data.items[0];
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
    addField(fields, "Status", applicationStatusLabel(app.current_status));
    addField(fields, "First tracked", displayDate(app.created_at));
    addField(fields, "Last updated", displayDate(app.updated_at));
    body.appendChild(fields);
    body.appendChild(buildApplicationStatusActions(body, job, app));
  }

  // Renders the six status action buttons. `app` is null exactly when the
  // job-scoped list read returned an empty 200 list ("Not tracked."); the
  // server response stays authoritative -- no optimistic updates anywhere. The
  // currently active status (if any) is visually highlighted with .current and
  // aria-pressed="true".
  function buildApplicationStatusActions(body, job, app) {
    var group = el("div", { className: "status-actions" });
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "Application status");
    APPLICATION_STATUSES.forEach(function (entry) {
      var button = el("button", {
        className: "status-button",
        type: "button",
        textContent: entry.label,
      });
      if (app !== null && app.current_status === entry.value) {
        button.classList.add("current");
        button.setAttribute("aria-pressed", "true");
      }
      button.addEventListener("click", function () {
        applyApplicationStatus(group, body, job, entry.value);
      });
      group.appendChild(button);
    });
    return group;
  }

  function setApplicationActionsDisabled(group, disabled) {
    group.querySelectorAll("button").forEach(function (button) {
      button.disabled = disabled;
    });
  }

  // Focuses the status button matching `status` if one is present. After a
  // successful mutation the Application section is re-rendered (which replaces
  // the DOM), so keyboard focus is restored to the newly-current status button.
  function focusCurrentStatusButton(group, status) {
    group.querySelectorAll("button").forEach(function (button) {
      if (button.textContent === applicationStatusLabel(status)) {
        button.focus();
      }
    });
  }

  async function applyApplicationStatus(group, body, job, status) {
    if (applicationOperationActive) return;
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return;
    applicationOperationActive = true;
    var section = body.parentNode;
    var jobId = job.id;
    var seq = detailRequestSeq;
    setApplicationActionsDisabled(group, true);
    section.setAttribute("aria-busy", "true");
    try {
      await putJson("/applications/" + jobId, { status: status });
      await refreshApplication(body, job, seq);
    } catch (error) {
      if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
      setApplicationActionsDisabled(group, false);
      section.removeAttribute("aria-busy");
      replaceSectionError(
        body,
        "The application status could not be saved.",
        error,
        function () {
          applyApplicationStatus(group, body, job, status);
        }
      );
    } finally {
      applicationOperationActive = false;
    }
  }

  // After a successful mutation, refetch ONLY GET /applications?job_id={id}
  // and re-render just the Application section. An empty items list from that
  // GET is the canonical "Not tracked." absent state, never an error. The
  // response is authoritative and the whole Job Detail view is never reloaded.
  async function refreshApplication(body, job, seq) {
    var jobId = job.id;
    var settled = await fetchDetailSection("application", jobId);
    if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
    var section = body.parentNode;
    clearText(body);
    renderSectionBody(section, body, job, "application", settled);
    section.removeAttribute("aria-busy");
    if (
      settled.status === "fulfilled" &&
      isObject(settled.value) &&
      Array.isArray(settled.value.items) &&
      settled.value.items.length > 0 &&
      isObject(settled.value.items[0]) &&
      typeof settled.value.items[0].current_status === "string"
    ) {
      var group = section.querySelector(".status-actions");
      if (group) {
        focusCurrentStatusButton(group, settled.value.items[0].current_status);
      }
    }
  }

  // Preparation is an independently loaded, server-authoritative nested
  // workspace. It never creates an application and never writes optimistically.
  var PREPARATION_KINDS = ["material", "link", "question", "task", "deadline", "other"];
  var PREPARATION_FULFILLMENT = ["open", "satisfied", "not_applicable"];

  function prepHeaders(revision) {
    return { "Content-Type": "application/json", "Accept": "application/json", "X-CareerOS-Preparation-Revision": String(revision) };
  }

  function prepWrite(url, method, revision, payload) {
    return writeJson(url, { method: method, headers: prepHeaders(revision), body: payload === undefined ? undefined : JSON.stringify(payload) });
  }

  function preparationUnavailable(body) {
    body.appendChild(el("p", { className: "empty-note", textContent: "Preparation is unavailable until you explicitly track this job in Application. CareerOS will not create application state automatically." }));
  }

  function prepLabel(value) {
    if (value === "open") return "Applies";
    if (value === "not_applicable") return "Not applicable";
    return String(value || "").replace(/_/g, " ").replace(/(^| )\w/g, function (letter) { return letter.toUpperCase(); });
  }

  // Deadline requirements intentionally have no satisfied state in the UI.
  // Keep this guard at the presentation boundary too, in case an older or
  // malformed response contains one despite the API contract.
  function prepFulfillmentLabel(kind, value) {
    return kind === "deadline" && value === "satisfied" ? "Applies" : prepLabel(value);
  }

  function prepReasonText(reason) {
    if (!isObject(reason)) return "";
    return typeof reason.message === "string" ? reason.message : String(reason.code || "");
  }

  function prepField(label, control, id) {
    var wrap = el("div", { className: "preparation-field" });
    var lab = el("label", { textContent: label });
    lab.setAttribute("for", id);
    control.id = id;
    wrap.appendChild(lab); wrap.appendChild(control);
    return wrap;
  }

  function prepSelect(values, selected) {
    var select = el("select");
    values.forEach(function (value) { var option = el("option", { textContent: prepLabel(value) }); option.value = value; option.selected = value === selected; select.appendChild(option); });
    return select;
  }

  function prepRevision(data) { return isFiniteNumber(data && data.revision) ? data.revision : 0; }

  function renderPreparation(body, job, settled, retry) {
    body.setAttribute("aria-live", "polite");
    if (settled.status === "rejected") {
      if (settled.reason && settled.reason.status === 404) { preparationUnavailable(body); return; }
      body.appendChild(sectionErrorBox("The preparation could not be loaded.", settled.reason, retry)); return;
    }
    var data = settled.value;
    if (!isObject(data) || !Array.isArray(data.items) || !isObject(data.readiness)) {
      body.appendChild(sectionErrorBox("The preparation response did not match the expected shape.", null, retry)); return;
    }
    var readinessBox = el("div", { className: "preparation-readiness" });
    readinessBox.appendChild(el("p", { className: "readiness-category", textContent: "Readiness: " + prepLabel(data.readiness.category) }));
    var reasons = Array.isArray(data.readiness["reasons"]) ? data.readiness["reasons"] : [];
    var blockers = reasons.filter(function (reason) { return isObject(reason) && reason.code !== "job_information_unavailable"; });
    readinessBox.appendChild(el("p", { className: "readiness-blockers", textContent: "Blocking requirements: " + blockers.length }));
    var note = el("p", { className: "field-hint", textContent: "CareerOS records what you captured; it has NOT verified that the listing is complete." });
    readinessBox.appendChild(note);
    if (reasons.some(function (r) { return isObject(r) && r.code === "job_information_unavailable"; })) readinessBox.appendChild(el("p", { className: "warning-note", textContent: "The listing description is unavailable. You may independently review an authoritative external source before capturing requirements." }));
    body.appendChild(readinessBox);
    if (preparationDraft) {
      var conflict = el("div", { className: "error-box", role: "alert" });
      conflict.appendChild(el("p", { textContent: "Preparation changed elsewhere. Your unsent change is preserved in memory; review the refreshed requirements, then explicitly reapply it." }));
      var reapply = el("button", { type: "button", className: "retry-button", textContent: "Reapply unsent change" });
      reapply.addEventListener("click", function () { var draft = preparationDraft; preparationDraft = null; applyPreparationMutation(body, job, data, draft.url, draft.method, draft.payload, draft.focus); });
      conflict.appendChild(reapply); body.appendChild(conflict);
    }
    var form = buildPreparationForm(body, job, data);
    body.appendChild(form);
    var list = el("ol", { className: "preparation-list" });
    data.items.forEach(function (item, index) { if (isObject(item)) list.appendChild(buildRequirementRow(body, job, data, item, index)); });
    body.appendChild(list);
    var reviewSet = data.requirements_reviewed_revision === data.revision;
    var reviewButton = el("button", { type: "button", className: "preparation-action", textContent: reviewSet ? "Clear requirements review" : "Mark requirements reviewed" }); reviewButton.id = "preparation-review-action"; reviewButton.disabled = !reviewSet && data.items.some(function (item) { return !item.confirmed; }); reviewButton.title = reviewButton.disabled ? "Confirm every requirement before marking requirements reviewed." : "";
    reviewButton.addEventListener("click", function () { var url = "/applications/" + job.id + "/requirements-review"; applyPreparationMutation(body, job, data, url, reviewSet ? "DELETE" : "PUT", undefined, "preparation-review-action"); });
    body.appendChild(reviewButton);
    var readyAllowed = data.readiness.category === "requirements_satisfied" || data.readiness.category === "user_confirmed_ready";
    var readyButton = el("button", { type: "button", className: "preparation-action", textContent: data.ready_confirmed_revision === data.revision ? "Clear ready confirmation" : "Confirm ready" });
    readyButton.disabled = !readyAllowed && data.ready_confirmed_revision !== data.revision;
    readyButton.title = readyButton.disabled ? "Resolve all blocking requirements first." : "";
    readyButton.id = "preparation-ready-action";
    readyButton.addEventListener("click", function () { var url = "/applications/" + job.id + "/ready-confirmation"; applyPreparationMutation(body, job, data, url, data.ready_confirmed_revision === data.revision ? "DELETE" : "PUT", undefined, "preparation-ready-action"); });
    body.appendChild(readyButton);
  }

  function buildPreparationForm(body, job, data) {
    var form = el("form", { className: "preparation-form" }); form.setAttribute("aria-label", "Add preparation requirement");
    var fieldset = el("fieldset"); fieldset.appendChild(el("legend", { textContent: "Add requirement" })); form.appendChild(fieldset);
    var kind = prepSelect(PREPARATION_KINDS, "material"); fieldset.appendChild(prepField("Kind", kind, "preparation-kind"));
    var label = el("input", { type: "text" }); label.required = true; label.maxLength = 160; fieldset.appendChild(prepField("Requirement", label, "preparation-label"));
    var detail = el("textarea"); detail.maxLength = 1000; fieldset.appendChild(prepField("Detail (optional)", detail, "preparation-detail"));
    var required = el("input", { type: "checkbox" }); required.checked = true; var reqWrap = el("label", { className: "checkbox-label", textContent: "Required" }); reqWrap.prepend(required); fieldset.appendChild(reqWrap);
    var fulfillment = prepSelect(PREPARATION_FULFILLMENT, "open"); fieldset.appendChild(prepField("Fulfillment", fulfillment, "preparation-fulfillment"));
    var deadline = el("input", { type: "datetime-local" }); fieldset.appendChild(prepField("Deadline (optional)", deadline, "preparation-deadline"));
    function setFulfillmentOptions() { var selected = fulfillment.value; clearText(fulfillment); (kind.value === "deadline" ? ["open", "not_applicable"] : PREPARATION_FULFILLMENT).forEach(function (value) { var option = el("option", { textContent: prepLabel(value) }); option.value = value; option.selected = value === selected || (value === "open" && selected === "satisfied"); fulfillment.appendChild(option); }); }
    kind.addEventListener("change", setFulfillmentOptions); setFulfillmentOptions();
    var submit = el("button", { type: "submit", textContent: "Add requirement" }); form.appendChild(submit);
    form.addEventListener("submit", function (event) { event.preventDefault(); var payload = { kind: kind.value, label: label.value, detail: detail.value || null, required: required.checked, fulfillment_state: kind.value === "deadline" && fulfillment.value === "satisfied" ? "open" : fulfillment.value, deadline_at: deadline.value ? dueTimestamp(deadline.value) : null }; applyPreparationMutation(body, job, data, "/applications/" + job.id + "/requirements", "POST", payload, "preparation-label"); });
    return form;
  }

  // A requirement may hand its deadline to the existing canonical Reminder
  // form, but the user must still choose to submit that form. No requirement
  // label/detail is copied and this action never sends a request itself.
  function focusReminderFromRequirement(body, item) {
    var reminderForm = document.querySelector(".reminder-form");
    if (!reminderForm) return;
    var due = reminderForm.querySelector("#reminder-due");
    if (due && item.deadline_at) {
      due.value = localDatetimeValue(item.deadline_at);
      due.dispatchEvent(new Event("input", { bubbles: true }));
    }
    if (due) due.focus();
    var existing = body.querySelector(".preparation-reminder-status");
    if (!existing) {
      existing = el("p", {
        className: "field-hint preparation-reminder-status",
        role: "status",
      });
      body.appendChild(existing);
    }
    existing.textContent = item.deadline_at
      ? "Reminder form focused and due time prefilled. No reminder was created."
      : "Reminder form focused. Choose a due time; no reminder was created.";
  }

  function buildRequirementRow(body, job, data, item, index) {
    var row = el("li", { className: "preparation-item" }); row.setAttribute("data-requirement-id", String(item.id));
    row.appendChild(el("h3", { textContent: displayText(item.label) }));
    if (item.detail) row.appendChild(el("p", { textContent: displayText(item.detail) }));
    row.appendChild(el("p", { className: "requirement-meta", textContent: (item.required ? "Required" : "Optional") + " · " + prepFulfillmentLabel(item.kind, item.fulfillment_state) + (item.deadline_at ? " · Deadline: " + displayDate(item.deadline_at) : "") + " · Origin: " + displayText(item.origin || "manual") + " · Capture method: " + displayText(item.capture_method || "manual") }));
    row.appendChild(el("p", { className: item.confirmed ? "requirement-confirmed" : "warning-note", textContent: item.confirmed ? "Confirmed" : "Needs confirmation" }));
    var actions = el("div", { className: "preparation-actions" });
    var edit = el("button", { type: "button", textContent: "Edit" }); edit.id = "preparation-edit-" + item.id; edit.addEventListener("click", function () { editRequirement(row, body, job, data, item); }); actions.appendChild(edit);
    var confirm = el("button", { type: "button", textContent: item.confirmed ? "Clear item confirmation" : "Confirm requirement" }); confirm.id = "preparation-confirm-" + item.id; confirm.addEventListener("click", function () { applyPreparationMutation(body, job, data, "/application-requirements/" + item.id, "PATCH", { confirmed: !item.confirmed }, confirm.id); }); actions.appendChild(confirm);
    var reminder = el("button", { type: "button", textContent: "Create reminder…" });
    reminder.addEventListener("click", function () { focusReminderFromRequirement(body, item); });
    actions.appendChild(reminder);
    var del = el("button", { type: "button", textContent: "Delete" }); del.addEventListener("click", function () { if (window.confirm("Delete this requirement?")) applyPreparationMutation(body, job, data, "/application-requirements/" + item.id, "DELETE", undefined, "preparation-label"); }); actions.appendChild(del);
    [index - 1, index + 1].forEach(function (target, offset) { var button = el("button", { type: "button", textContent: offset === 0 ? "Up" : "Down" }); button.id = "preparation-" + (offset === 0 ? "up-" : "down-") + item.id; button.disabled = target < 0 || target >= data.items.length; button.addEventListener("click", function () { var ids = data.items.map(function (entry) { return entry.id; }); var swap = ids[index]; ids[index] = ids[target]; ids[target] = swap; applyPreparationMutation(body, job, data, "/applications/" + job.id + "/requirements/order", "PUT", { ids: ids }, "preparation-edit-" + item.id); }); actions.appendChild(button); });
    row.appendChild(actions); return row;
  }

  function editRequirement(row, body, job, data, item) {
    clearText(row); var form = el("form", { className: "preparation-edit-form" });
    var fieldset = el("fieldset"); fieldset.appendChild(el("legend", { textContent: "Edit requirement" })); form.appendChild(fieldset);
    var kind = prepSelect(PREPARATION_KINDS, item.kind); fieldset.appendChild(prepField("Kind", kind, "edit-kind-" + item.id));
    var label = el("input", { type: "text" }); label.value = item.label; label.required = true; fieldset.appendChild(prepField("Requirement", label, "edit-label-" + item.id));
    var detail = el("textarea"); detail.value = item.detail || ""; fieldset.appendChild(prepField("Detail", detail, "edit-detail-" + item.id));
    var required = el("input", { type: "checkbox" }); required.checked = Boolean(item.required); var req = el("label", { textContent: "Required" }); req.prepend(required); fieldset.appendChild(req);
    var fulfillment = prepSelect(item.kind === "deadline" ? ["open", "not_applicable"] : PREPARATION_FULFILLMENT, item.fulfillment_state); fieldset.appendChild(prepField("Fulfillment", fulfillment, "edit-state-" + item.id));
    var deadline = el("input", { type: "datetime-local" }); deadline.value = localDatetimeValue(item.deadline_at || ""); fieldset.appendChild(prepField("Deadline", deadline, "edit-deadline-" + item.id));
    var confirmed = el("input", { type: "checkbox" }); confirmed.checked = Boolean(item.confirmed); var confirmation = el("label", { textContent: "Confirmed" }); confirmation.prepend(confirmed); fieldset.appendChild(confirmation);
    kind.addEventListener("change", function () { var selected = fulfillment.value; clearText(fulfillment); (kind.value === "deadline" ? ["open", "not_applicable"] : PREPARATION_FULFILLMENT).forEach(function (value) { var option = el("option", { textContent: prepLabel(value) }); option.value = value; option.selected = value === selected || (value === "open" && selected === "satisfied"); fulfillment.appendChild(option); }); });
    var save = el("button", { type: "submit", textContent: "Save" }); var cancel = el("button", { type: "button", textContent: "Cancel" }); form.appendChild(save); form.appendChild(cancel); row.appendChild(form);
    cancel.addEventListener("click", function () { var replacement = buildRequirementRow(body, job, data, item, data.items.indexOf(item)); row.replaceWith(replacement); var focus = replacement.querySelector("#preparation-edit-" + item.id); if (focus) focus.focus(); });
    form.addEventListener("submit", function (event) { event.preventDefault(); applyPreparationMutation(body, job, data, "/application-requirements/" + item.id, "PATCH", { kind: kind.value, label: label.value, detail: detail.value || null, required: required.checked, fulfillment_state: kind.value === "deadline" && fulfillment.value === "satisfied" ? "open" : fulfillment.value, deadline_at: deadline.value ? dueTimestamp(deadline.value) : null, confirmed: confirmed.checked }, "preparation-edit-" + item.id); });
  }

  async function applyPreparationMutation(body, job, data, url, method, payload, focus) {
    if (preparationOperationActive) return; preparationOperationActive = true;
    var operation = { id: ++preparationOperationSeq, generation: detailRequestSeq }; body.parentNode.setAttribute("aria-busy", "true");
    var committed = false;
    try {
      await prepWrite(url, method, prepRevision(data), payload);
      committed = true;
      var latest = await fetchDetailSection("preparation", job.id);
      if (operation.generation !== detailRequestSeq || !isCurrentDetailJob(job.id)) return;
      preparationDraft = null;
      clearText(body);
      renderPreparation(body, job, latest, function () { retrySection(body.parentNode, body, job, "preparation"); });
      var target = focus && document.getElementById(focus);
      if (target) target.focus();
    } catch (error) {
      if (operation.generation !== detailRequestSeq || !isCurrentDetailJob(job.id)) return;
      var conflict = error && error.status === 409 &&
        (String(error.message || "").indexOf("preparation_revision_conflict") !== -1 ||
         error.code === "preparation_revision_conflict");
      if (conflict) {
        preparationDraft = { url: url, method: method, payload: payload, focus: focus };
        var refreshed;
        try {
          refreshed = await fetchDetailSection("preparation", job.id);
        } catch (refreshError) {
          replaceSectionError(body, "The authoritative preparation could not be refreshed.", refreshError, function () { retrySection(body.parentNode, body, job, "preparation"); });
          return;
        }
        if (operation.generation === detailRequestSeq && isCurrentDetailJob(job.id)) {
          clearText(body);
          renderPreparation(body, job, refreshed, function () { retrySection(body.parentNode, body, job, "preparation"); });
        }
      } else if (committed) {
        // The write already committed. Retry only the authoritative read; a
        // second POST could create a duplicate requirement.
        replaceSectionError(body, "The preparation was saved, but its current state could not be loaded.", error, function () { retrySection(body.parentNode, body, job, "preparation"); });
      } else {
        replaceSectionError(body, "The preparation change could not be saved.", error, function () { applyPreparationMutation(body, job, data, url, method, payload, focus); });
      }
    }
    finally { if (operation.generation === detailRequestSeq && isCurrentDetailJob(job.id)) body.parentNode.removeAttribute("aria-busy"); if (operation.generation === detailRequestSeq) preparationOperationActive = false; }
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
      // Any rejection (network/500/503) is a genuine section error with Retry.
      // The canonical singular 404 is never used by this read path.
      body.appendChild(
        sectionErrorBox("The review decision could not be loaded.", settled.reason, retry)
      );
      return;
    }
    var data = settled.value;
    if (!isObject(data) || !Array.isArray(data.items)) {
      body.appendChild(
        sectionErrorBox(
          "The review response did not match the expected shape.",
          null,
          retry
        )
      );
      return;
    }
    // At most one current decision per job (the review_decisions.job_id UNIQUE
    // constraint guarantees it); more than one would expose a data-integrity
    // problem that must never be silently hidden by picking an arbitrary row.
    if (data.items.length > 1) {
      body.appendChild(
        sectionErrorBox(
          "The review response did not match the expected shape.",
          null,
          retry
        )
      );
      return;
    }
    if (data.items.length === 0) {
      // The empty 200 list is the normal absent state: the job has not been
      // reviewed yet. It is never an error.
      renderReviewDecisionSection(body, job, null);
      return;
    }
    var review = data.items[0];
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
  // null exactly when the job-scoped list read returned an empty 200 list
  // ("Not reviewed."); the server response stays authoritative -- no optimistic
  // updates anywhere.
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

  // After a successful mutation, refetch ONLY GET /review-decisions?job_id={id}
  // and re-render just the Review section. An empty items list from that GET is
  // the canonical "Not reviewed." absent state, never an error. The response is
  // authoritative and the whole Job Detail view is never reloaded.
  async function refreshReviewDecision(body, job, seq) {
    var jobId = job.id;
    var settled = await fetchDetailSection("review", jobId);
    if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
    var section = body.parentNode;
    clearText(body);
    renderSectionBody(section, body, job, "review", settled);
    section.removeAttribute("aria-busy");
  }

  // The Reminders section always renders the always-visible create form first,
  // then (in exact server order, soonest first) either the empty state or the
  // reminder list with exactly one lifecycle action per item (Complete for an
  // open reminder, Reopen for a completed one). The response is authoritative
  // and the job never 404s this endpoint: an empty list is the normal "No
  // reminders." absent state.
  function renderReminders(body, job, settled, retry) {
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
    body.appendChild(buildReminderCreateForm(body, job));
    if (data.items.length === 0) {
      body.appendChild(
        el("p", { className: "empty-note", textContent: "No reminders." })
      );
      return;
    }
    var list = el("ul", { className: "reminder-list" });
    data.items.forEach(function (reminder) {
      if (!isObject(reminder)) return;
      list.appendChild(buildReminderRow(body, job, reminder));
    });
    body.appendChild(list);
  }

  // One reminder's display row: kind, due date, open/completed state, and the
  // action controls (Edit, Complete/Reopen, Delete). Used both by the initial
  // server-order render and by the edit Cancel path to restore the row in
  // place from the original server values -- never from re-derived DOM state.
  function buildReminderRow(body, job, reminder) {
    var item = el("li", { className: "reminder-item" });
    item.appendChild(
      el("span", {
        className: "reminder-kind",
        textContent: reminderKindLabel(reminder.kind),
      })
    );
    item.appendChild(
      el("span", {
        className: "reminder-due",
        textContent: displayDate(reminder.due_at),
      })
    );
    var state =
      typeof reminder.completed_at === "string" && reminder.completed_at.length > 0
        ? "Completed"
        : "Open";
    item.appendChild(el("span", { className: "health-count", textContent: state }));
    item.appendChild(buildReminderActions(body, job, reminder));
    return item;
  }

  // Canonical reminder kinds (presentation labels only; the transmitted values
  // are the exact backend vocabulary: follow_up, interview, offer, review,
  // custom). The create form offers exactly these five kinds and sends the
  // canonical value unchanged.
  var REMINDER_KINDS = [
    { value: "follow_up", label: "Follow up" },
    { value: "interview", label: "Interview" },
    { value: "offer", label: "Offer" },
    { value: "review", label: "Review" },
    { value: "custom", label: "Custom" },
  ];

  function reminderKindLabel(value) {
    for (var index = 0; index < REMINDER_KINDS.length; index++) {
      if (REMINDER_KINDS[index].value === value) {
        return REMINDER_KINDS[index].label;
      }
    }
    return value;
  }

  // The backend requires a full ISO-8601 due timestamp with an explicit UTC
  // offset (naive and date-only values are rejected; Z is accepted). A native
  // datetime-local input yields a naive local wall time, so the explicit
  // offset is derived from the browser's actual zone FOR THE ENTERED DATE AND
  // TIME (never today's offset, so DST changes are respected) and appended.
  // No timezone/date library is used, no named timezone is guessed, no offset
  // is silently assumed, and Date.toISOString() is never used as a shortcut
  // because it would change the intended wall-clock semantics. The server
  // remains authoritative and normalizes the value to UTC.
  function localUtcOffset(value) {
    var match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value);
    if (!match) return null;
    var year = parseInt(match[1], 10);
    var month = parseInt(match[2], 10);
    var day = parseInt(match[3], 10);
    var hour = parseInt(match[4], 10);
    var minute = parseInt(match[5], 10);
    var date = new Date(year, month - 1, day, hour, minute);
    if (
      date.getFullYear() !== year ||
      date.getMonth() !== month - 1 ||
      date.getDate() !== day ||
      date.getHours() !== hour ||
      date.getMinutes() !== minute
    ) {
      return null; // wall time does not exist in the local zone (e.g. DST gap)
    }
    // getTimezoneOffset() returns minutes behind UTC (e.g. -120 for UTC+02:00);
    // the sign is flipped so a positive value means ahead of UTC.
    var offsetMinutes = -date.getTimezoneOffset();
    var sign = offsetMinutes >= 0 ? "+" : "-";
    var absolute = Math.abs(offsetMinutes);
    var offsetHours = Math.floor(absolute / 60);
    var offsetMins = absolute % 60;
    return (
      sign +
      String(offsetHours).padStart(2, "0") +
      ":" +
      String(offsetMins).padStart(2, "0")
    );
  }

  // Serializes the datetime-local wall time into the canonical explicit-offset
  // value the server accepts (e.g. "2026-08-10T14:00+02:00"). No seconds are
  // invented: the backend parser accepts an HH:MM time component. Returns null
  // for an empty or unusable value.
  function dueTimestamp(value) {
    if (typeof value !== "string" || value.length === 0) return null;
    var offset = localUtcOffset(value);
    if (offset === null) return null;
    return value + offset;
  }

  // The minimal reverse conversion needed to PRE-POPULATE the per-reminder
  // datetime-local editor: parse the server's explicit-offset ISO-8601 due
  // timestamp (seconds optional) into the UTC instant, then format it in the
  // browser's local timezone as a datetime-local value (YYYY-MM-DDTHH:MM).
  // Minute granularity is INTENTIONAL: datetime-local cannot express seconds,
  // and this value only ever pre-populates the editor. A kind-only Save never
  // re-serializes it, so a CLI-created reminder with second precision keeps its
  // stored due time byte-for-byte; only a genuine user change to the due field
  // is re-serialized (to minute precision). The instant is always reconstructed
  // from the explicit offset -- never from today's offset -- and no wall-clock
  // shortcut is used. Returns "" for an empty or unusable value.
  function localDatetimeValue(value) {
    if (typeof value !== "string" || value.length === 0) return "";
    var match =
      /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?(?:\.\d+)?(Z|[+-]\d{2}:\d{2})$/.exec(
        value
      );
    if (!match) return "";
    var year = parseInt(match[1], 10);
    var month = parseInt(match[2], 10);
    var day = parseInt(match[3], 10);
    var hour = parseInt(match[4], 10);
    var minute = parseInt(match[5], 10);
    var second = match[6] ? parseInt(match[6], 10) : 0;
    var offset = match[7];
    var offsetMinutes = 0;
    if (offset !== "Z") {
      var offsetSign = offset.charAt(0) === "-" ? -1 : 1;
      offsetMinutes =
        offsetSign *
        (parseInt(offset.slice(1, 3), 10) * 60 + parseInt(offset.slice(4, 6), 10));
    }
    // The UTC instant for an explicit-offset timestamp is the wall components
    // interpreted in that offset, converted to UTC (never from the local zone).
    var instant =
      Date.UTC(year, month - 1, day, hour, minute, second) -
      offsetMinutes * 60 * 1000;
    var local = new Date(instant);
    var localYear = local.getFullYear();
    var localMonth = String(local.getMonth() + 1).padStart(2, "0");
    var localDay = String(local.getDate()).padStart(2, "0");
    var localHour = String(local.getHours()).padStart(2, "0");
    var localMinute = String(local.getMinutes()).padStart(2, "0");
    return (
      localYear +
      "-" +
      localMonth +
      "-" +
      localDay +
      "T" +
      localHour +
      ":" +
      localMinute
    );
  }

  // The browser's IANA timezone name (e.g. "Europe/Athens") via the standard
  // Intl API -- never guessed, never hard-coded, and not a named-timezone
  // library. Returns null when the runtime cannot resolve one (then the hint
  // falls back to the numeric-offset-only wording). The name identifies the
  // zone, while the numeric offset below stays the DST-correct value for the
  // specific date and time entered, so e.g. a Greek user in August sees the
  // zone "Europe/Athens" together with UTC+03:00 (its daylight-saving offset)
  // rather than the standard-time UTC+02:00.
  function browserTimezoneName() {
    try {
      var resolved = Intl.DateTimeFormat().resolvedOptions();
      if (resolved && typeof resolved.timeZone === "string" && resolved.timeZone.length > 0) {
        return resolved.timeZone;
      }
    } catch (error) {
      // ignore: fall back to the numeric-offset wording
    }
    return null;
  }

  function updateDueHint(due, hint) {
    var offset = localUtcOffset(due.value);
    if (offset === null) {
      hint.textContent =
        "Pick a due date and time to see the timezone it uses.";
      return;
    }
    var zone = browserTimezoneName();
    if (zone === null) {
      hint.textContent =
        "Due time uses your browser's local timezone (UTC" + offset + ").";
      return;
    }
    hint.textContent =
      "Due time uses your local timezone (" + zone + ", UTC" + offset + " for this date).";
  }

  // The always-visible compact create form at the top of the Reminders
  // section: a canonical kind select plus a native datetime-local due input.
  // No reminder message field exists: the API never accepts or exposes one.
  function buildReminderCreateForm(body, job) {
    var form = el("form", { className: "reminder-form" });
    form.setAttribute("aria-label", "Add reminder");
    var kindLabel = el("label", { className: "reminder-label", textContent: "Kind" });
    kindLabel.setAttribute("for", "reminder-kind");
    form.appendChild(kindLabel);
    var kind = el("select", { id: "reminder-kind", className: "reminder-input" });
    kind.setAttribute("name", "kind");
    REMINDER_KINDS.forEach(function (entry) {
      var option = el("option", { textContent: entry.label });
      option.value = entry.value;
      kind.appendChild(option);
    });
    form.appendChild(kind);
    var dueLabel = el("label", { className: "reminder-label", textContent: "Due" });
    dueLabel.setAttribute("for", "reminder-due");
    form.appendChild(dueLabel);
    var due = el("input", {
      id: "reminder-due",
      className: "reminder-input",
      type: "datetime-local",
    });
    due.setAttribute("name", "due_at");
    due.setAttribute("required", "");
    form.appendChild(due);
    var hint = el("p", { className: "field-hint", textContent: "" });
    form.appendChild(hint);
    var validation = el("p", { className: "reminder-validation" });
    validation.hidden = true;
    form.appendChild(validation);
    var submit = el("button", {
      type: "submit",
      className: "reminder-add-button",
      textContent: "Add reminder",
    });
    form.appendChild(submit);
    updateDueHint(due, hint);
    due.addEventListener("input", function () {
      updateDueHint(due, hint);
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      createReminder(form, body, job, kind, due, validation);
    });
    return form;
  }

  function setReminderControlsDisabled(body, disabled) {
    body
      .querySelectorAll(
        ".reminder-form input, .reminder-form select, .reminder-form button, .reminder-edit-form input, .reminder-edit-form select, .reminder-edit-form button, .reminder-button"
      )
      .forEach(function (control) {
        control.disabled = disabled;
      });
  }

  // POST /reminders is intentionally NON-idempotent, so a failed creation is
  // never blindly replayed. Client-side validation only improves UX: the
  // server remains authoritative and the section stays on its previously
  // rendered server state throughout.
  async function createReminder(form, body, job, kind, due, validation) {
    if (reminderOperationActive) return;
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return;
    var dueAt = due.value;
    if (dueAt.length === 0) {
      validation.hidden = false;
      validation.textContent = "Pick a due time.";
      due.focus();
      return;
    }
    var timestamp = dueTimestamp(dueAt);
    if (timestamp === null) {
      validation.hidden = false;
      validation.textContent =
        "That due time is not valid in your local timezone; pick another date and time.";
      due.focus();
      return;
    }
    clearText(validation);
    validation.hidden = true;
    reminderOperationActive = true;
    var section = body.parentNode;
    var jobId = job.id;
    var seq = detailRequestSeq;
    setReminderControlsDisabled(body, true);
    section.setAttribute("aria-busy", "true");
    try {
      await postJson("/reminders", {
        job_id: jobId,
        kind: kind.value,
        due_at: timestamp,
      });
      await refreshReminders(body, job, seq);
    } catch (error) {
      if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
      setReminderControlsDisabled(body, false);
      section.removeAttribute("aria-busy");
      handleCreateReminderFailure(body, form, job, seq, kind.value, dueAt, error);
    } finally {
      reminderOperationActive = false;
    }
  }

  // A failed creation is never re-sent. A deterministic 422 means the server
  // refused the payload (nothing was created): the form keeps its values and
  // the server detail is shown next to the form so the user can correct and
  // submit again normally. Any other failure (network drop, 500, 503) is
  // reconciled against the server first: the section refreshes via a safe GET
  // so the authoritative list reveals whether the reminder was actually
  // created before the user decides to submit a fresh one.
  function handleCreateReminderFailure(body, form, job, seq, kind, dueAt, error) {
    if (error && error.status === 422) {
      var validation = form.querySelector(".reminder-validation");
      if (validation) {
        validation.hidden = false;
        validation.textContent = error.message;
      }
      return;
    }
    replaceReminderSaveError(body, error, function () {
      reconcileReminders(body, job, seq, kind, dueAt);
    });
  }

  // The create-failure error panel uses an explicit "Check reminders" action
  // that reconciles against the server (a GET refresh) instead of re-sending
  // the non-idempotent POST. It replaces any existing panel so a section never
  // holds more than one error box.
  function replaceReminderSaveError(body, error, reconcileAction) {
    body.querySelectorAll(".error-box").forEach(function (box) {
      box.remove();
    });
    var box = el("div", { className: "error-box" });
    box.appendChild(
      el("p", {
        textContent:
          "The reminder could not be saved. Check whether it was created, then submit again if it is still missing.",
      })
    );
    if (error && error.message) {
      box.appendChild(
        el("p", { className: "error-detail", textContent: error.message })
      );
    }
    var check = el("button", {
      className: "retry-button",
      textContent: "Check reminders",
    });
    check.addEventListener("click", function () {
      box.remove();
      reconcileAction();
    });
    box.appendChild(check);
    body.appendChild(box);
  }

  // Reconciliation for a failed creation: refetch ONLY GET /reminders?job_id
  // and re-render the section so the server-authoritative list reveals whether
  // the reminder was actually created. Never re-POSTs. The previously entered
  // kind and due time are restored into the fresh form so the user can submit
  // again explicitly if the reminder is still absent.
  async function reconcileReminders(body, job, seq, kind, dueAt) {
    var settled = await fetchDetailSection("reminders", job.id);
    if (seq !== detailRequestSeq || !isCurrentDetailJob(job.id)) return;
    var section = body.parentNode;
    clearText(body);
    renderReminders(body, job, settled, function () {
      retrySection(section, body, job, "reminders");
    });
    var form = body.querySelector(".reminder-form");
    if (form) {
      var kindSelect = form.querySelector("#reminder-kind");
      var dueInput = form.querySelector("#reminder-due");
      if (kindSelect && typeof kind === "string") kindSelect.value = kind;
      if (dueInput && typeof dueAt === "string") dueInput.value = dueAt;
      var hint = form.querySelector(".field-hint");
      if (dueInput && hint) updateDueHint(dueInput, hint);
    }
    section.removeAttribute("aria-busy");
  }

  // Exactly one lifecycle action per reminder: an open reminder gets
  // "Complete" and a completed one gets "Reopen" -- never both -- plus an
  // Edit control for every reminder and a separate Delete control that demands
  // explicit confirmation. Editing is available on open AND completed
  // reminders: the backend PATCH updates only kind/due time and preserves the
  // completion state.
  function buildReminderActions(body, job, reminder) {
    var completed =
      typeof reminder.completed_at === "string" && reminder.completed_at.length > 0;
    var group = el("div", { className: "reminder-actions" });
    group.setAttribute("role", "group");
    group.setAttribute("aria-label", "Reminder action");
    var editButton = el("button", {
      className: "reminder-button reminder-edit-button",
      type: "button",
      textContent: "Edit",
    });
    editButton.setAttribute("aria-label", "Edit reminder");
    editButton.addEventListener("click", function () {
      startReminderEdit(group.parentNode, body, job, reminder);
    });
    group.appendChild(editButton);
    var button = el("button", {
      className: "reminder-button",
      type: "button",
      textContent: completed ? "Reopen" : "Complete",
    });
    button.addEventListener("click", function () {
      if (completed) {
        reopenReminder(group, body, job, reminder);
      } else {
        completeReminder(group, body, job, reminder);
      }
    });
    group.appendChild(button);
    var deleteButton = el("button", {
      className: "reminder-button delete-reminder",
      type: "button",
      textContent: "Delete",
    });
    deleteButton.addEventListener("click", function () {
      requestDeleteReminder(group, body, job, reminder);
    });
    group.appendChild(deleteButton);
    return group;
  }

  // Deleting a reminder is destructive, so the Delete click is gated by a
  // native window.confirm dialog before any request is issued. Confirmation
  // lives ONLY in this click wrapper: cancelling is a true no-op (no request,
  // no guard change, no control/aria-busy mutation), and an automated Retry
  // after a network/500/503 failure re-issues the exact DELETE directly
  // without asking again.
  function requestDeleteReminder(group, body, job, reminder) {
    if (reminderOperationActive) return;
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return;
    if (
      !isObject(reminder) ||
      !isFiniteNumber(reminder.reminder_id) ||
      reminder.reminder_id < 1
    ) {
      return;
    }
    if (!window.confirm("Delete this reminder? This cannot be undone.")) return;
    deleteReminder(group, body, job, reminder);
  }

  // PUT /reminders/{id}/complete is idempotent, so its Retry action may safely
  // re-issue the exact failed PUT; the server-authoritative response drives a
  // refetch of ONLY GET /reminders?job_id and a re-render of just this section.
  async function completeReminder(group, body, job, reminder) {
    if (reminderOperationActive) return;
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return;
    if (
      !isObject(reminder) ||
      !isFiniteNumber(reminder.reminder_id) ||
      reminder.reminder_id < 1
    ) {
      return;
    }
    reminderOperationActive = true;
    var section = body.parentNode;
    var jobId = job.id;
    var reminderId = reminder.reminder_id;
    var seq = detailRequestSeq;
    setReminderControlsDisabled(body, true);
    section.setAttribute("aria-busy", "true");
    try {
      await putJson("/reminders/" + reminderId + "/complete");
      await refreshReminders(body, job, seq);
    } catch (error) {
      if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
      setReminderControlsDisabled(body, false);
      section.removeAttribute("aria-busy");
      replaceSectionError(
        body,
        "The reminder could not be completed.",
        error,
        function () {
          completeReminder(group, body, job, reminder);
        }
      );
    } finally {
      reminderOperationActive = false;
    }
  }

  // PUT /reminders/{id}/reopen is idempotent, so its Retry action may safely
  // re-issue the exact failed PUT; the server-authoritative response drives a
  // refetch of ONLY GET /reminders?job_id and a re-render of just this section.
  async function reopenReminder(group, body, job, reminder) {
    if (reminderOperationActive) return;
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return;
    if (
      !isObject(reminder) ||
      !isFiniteNumber(reminder.reminder_id) ||
      reminder.reminder_id < 1
    ) {
      return;
    }
    reminderOperationActive = true;
    var section = body.parentNode;
    var jobId = job.id;
    var reminderId = reminder.reminder_id;
    var seq = detailRequestSeq;
    setReminderControlsDisabled(body, true);
    section.setAttribute("aria-busy", "true");
    try {
      await putJson("/reminders/" + reminderId + "/reopen");
      await refreshReminders(body, job, seq);
    } catch (error) {
      if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
      setReminderControlsDisabled(body, false);
      section.removeAttribute("aria-busy");
      replaceSectionError(
        body,
        "The reminder could not be reopened.",
        error,
        function () {
          reopenReminder(group, body, job, reminder);
        }
      );
    } finally {
      reminderOperationActive = false;
    }
  }

  // After a successful mutation, refetch ONLY GET /reminders?job_id={id} and
  // re-render just the Reminders section. The response is authoritative and
  // the whole Job Detail view is never reloaded. The reminders GET never 404s
  // for a job: an empty list is the normal "no reminders" absent state.
  async function refreshReminders(body, job, seq) {
    var jobId = job.id;
    var settled = await fetchDetailSection("reminders", jobId);
    if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
    var section = body.parentNode;
    clearText(body);
    renderReminders(body, job, settled, function () {
      retrySection(section, body, job, "reminders");
    });
    section.removeAttribute("aria-busy");
  }

  // DELETE /reminders/{id} is genuinely destructive: a 200 removes the row for
  // good (no undo), and the response drives a refetch of ONLY GET
  // /reminders?job_id and a re-render of just this section. A canonical 404
  // means the reminder is already gone (the desired "reminder absent" state
  // holds), so it is reconciled as success with the same reminder-only
  // refresh and no error panel. Any other failure (422/500/503/network) keeps
  // the existing list and shows exactly one section error panel whose Retry
  // re-issues the exact failed DELETE -- never re-confirming, because the
  // window.confirm gate lives only in requestDeleteReminder.
  async function deleteReminder(group, body, job, reminder) {
    if (reminderOperationActive) return;
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return;
    if (
      !isObject(reminder) ||
      !isFiniteNumber(reminder.reminder_id) ||
      reminder.reminder_id < 1
    ) {
      return;
    }
    reminderOperationActive = true;
    var section = body.parentNode;
    var jobId = job.id;
    var reminderId = reminder.reminder_id;
    var seq = detailRequestSeq;
    setReminderControlsDisabled(body, true);
    section.setAttribute("aria-busy", "true");
    try {
      await deleteJson("/reminders/" + reminderId);
      await refreshReminders(body, job, seq);
    } catch (error) {
      if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
      if (error && error.status === 404) {
        await refreshReminders(body, job, seq);
        return;
      }
      setReminderControlsDisabled(body, false);
      section.removeAttribute("aria-busy");
      replaceSectionError(
        body,
        "The reminder could not be deleted.",
        error,
        function () {
          deleteReminder(group, body, job, reminder);
        }
      );
    } finally {
      reminderOperationActive = false;
    }
  }

  // ---------------------------------------------------------------------------
  // Reminder edit (inline per-reminder Edit -> Save/Cancel)
  // ---------------------------------------------------------------------------

  // Entering edit mode replaces ONLY the clicked reminder's row with the inline
  // edit form (kind select + datetime-local due input + Save + Cancel). The
  // rest of the section -- the create form, the other rows, and the server
  // order -- stays untouched, and the kind select receives focus on entry.
  function startReminderEdit(item, body, job, reminder) {
    if (reminderOperationActive) return;
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return;
    if (
      !isObject(reminder) ||
      !isFiniteNumber(reminder.reminder_id) ||
      reminder.reminder_id < 1
    ) {
      return;
    }
    var form = buildReminderEditForm(body, job, reminder);
    clearText(item);
    item.appendChild(form);
    var kind = form.querySelector(".reminder-edit-kind");
    if (kind) kind.focus();
  }

  // The inline edit form. Only kind and due_at are editable -- no message
  // field exists (the API never accepts or exposes one). Existing values are
  // pre-populated: the current kind is pre-selected and the due instant is
  // shown in the browser's local timezone via localDatetimeValue. The ORIGINAL
  // values captured here (never values re-derived later from the DOM) are what
  // a Save compares against, so a kind-only edit never re-serializes the
  // untouched due time and stored seconds survive unchanged.
  function buildReminderEditForm(body, job, reminder) {
    var form = el("form", { className: "reminder-edit-form" });
    form.setAttribute("aria-label", "Edit reminder");
    var originalKind = reminder.kind;
    var originalDueField = localDatetimeValue(reminder.due_at);
    var kindLabel = el("label", { className: "reminder-label", textContent: "Kind" });
    kindLabel.setAttribute("for", "reminder-edit-kind-" + reminder.reminder_id);
    form.appendChild(kindLabel);
    var kind = el("select", {
      id: "reminder-edit-kind-" + reminder.reminder_id,
      className: "reminder-input reminder-edit-kind",
    });
    kind.setAttribute("name", "kind");
    REMINDER_KINDS.forEach(function (entry) {
      var option = el("option", { textContent: entry.label });
      option.value = entry.value;
      kind.appendChild(option);
    });
    kind.value = originalKind;
    form.appendChild(kind);
    var dueLabel = el("label", { className: "reminder-label", textContent: "Due" });
    dueLabel.setAttribute("for", "reminder-edit-due-" + reminder.reminder_id);
    form.appendChild(dueLabel);
    var due = el("input", {
      id: "reminder-edit-due-" + reminder.reminder_id,
      className: "reminder-input reminder-edit-due",
      type: "datetime-local",
    });
    due.setAttribute("name", "due_at");
    due.setAttribute("required", "");
    due.value = originalDueField;
    form.appendChild(due);
    var hint = el("p", { className: "field-hint" });
    form.appendChild(hint);
    var validation = el("p", { className: "reminder-validation" });
    validation.hidden = true;
    form.appendChild(validation);
    var save = el("button", {
      type: "submit",
      className: "reminder-add-button",
      textContent: "Save",
    });
    form.appendChild(save);
    var cancel = el("button", {
      type: "button",
      className: "reminder-button",
      textContent: "Cancel",
    });
    form.appendChild(cancel);
    updateDueHint(due, hint);
    due.addEventListener("input", function () {
      updateDueHint(due, hint);
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      saveReminderEdit(form, body, job, reminder, originalKind, originalDueField);
    });
    cancel.addEventListener("click", function () {
      cancelReminderEdit(form, body, job, reminder);
    });
    return form;
  }

  // Cancel is a true local no-op: ZERO network requests and ZERO server
  // mutation. The display row is restored in place from the original reminder
  // values (server-authoritative), preserving the server order, and focus
  // returns to that reminder's Edit button when practical.
  function cancelReminderEdit(form, body, job, reminder) {
    var item = form.parentNode;
    if (!item || !item.parentNode) return;
    var fresh = buildReminderRow(body, job, reminder);
    item.parentNode.replaceChild(fresh, item);
    var edit = fresh.querySelector(".reminder-edit-button");
    if (edit) edit.focus();
  }

  // Detects what actually changed and builds the PATCH body from ONLY the
  // changed fields. A kind-only edit sends exactly {"kind": ...} -- the
  // untouched due time is never re-serialized, so a CLI-created reminder with
  // second precision keeps its stored value byte-for-byte. A due-only edit
  // sends exactly {"due_at": ...} (serialized with the explicit offset for the
  // ENTERED date, so DST is respected). Both changed sends both fields.
  // Changing nothing is a local no-op: exit edit mode, restore the display
  // row, and make ZERO network requests -- the server state stays untouched.
  function saveReminderEdit(form, body, job, reminder, originalKind, originalDueField) {
    if (reminderOperationActive) return;
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return;
    if (
      !isObject(reminder) ||
      !isFiniteNumber(reminder.reminder_id) ||
      reminder.reminder_id < 1
    ) {
      return;
    }
    var kind = form.querySelector(".reminder-edit-kind");
    var due = form.querySelector(".reminder-edit-due");
    var validation = form.querySelector(".reminder-validation");
    if (!kind || !due || !validation) return;
    var kindChanged = kind.value !== originalKind;
    var dueField = due.value;
    var dueChanged = dueField !== originalDueField;
    if (!kindChanged && !dueChanged) {
      cancelReminderEdit(form, body, job, reminder);
      return;
    }
    var payload = {};
    if (kindChanged) payload.kind = kind.value;
    if (dueChanged) {
      var timestamp = dueTimestamp(dueField);
      if (timestamp === null) {
        validation.hidden = false;
        validation.textContent =
          "That due time is not valid in your local timezone; pick another date and time.";
        due.focus();
        return;
      }
      payload.due_at = timestamp;
    }
    clearText(validation);
    validation.hidden = true;
    submitReminderEdit(form, body, job, reminder, payload);
  }

  // The actual edit mutation (PATCH /reminders/{id}) under the shared
  // reminderOperationActive guard: controls are disabled and the section marked
  // aria-busy while the PATCH and its reminder-only refresh are in flight. A
  // successful PATCH re-fetches ONLY GET /reminders?job_id and re-renders just
  // the Reminders section (server-authoritative, no optimistic update). A
  // canonical 404 means the reminder disappeared -- the desired "reminder
  // absent" state holds -- so the same reminder-only refresh runs with no error
  // panel. A deterministic 422 (the server refused the payload) keeps edit mode
  // and the entered values, shows the server detail next to the form, and
  // re-enables the controls so the user can correct and Save again; the invalid
  // payload is never auto-retried. Every other failure (500/503/network)
  // preserves the form and entered values and shows exactly one error panel
  // whose Retry re-issues the EXACT failed PATCH payload (no re-confirmation).
  async function submitReminderEdit(form, body, job, reminder, payload) {
    if (reminderOperationActive) return;
    if (!isObject(job) || !isFiniteNumber(job.id) || job.id < 1) return;
    if (
      !isObject(reminder) ||
      !isFiniteNumber(reminder.reminder_id) ||
      reminder.reminder_id < 1
    ) {
      return;
    }
    reminderOperationActive = true;
    var section = body.parentNode;
    var jobId = job.id;
    var reminderId = reminder.reminder_id;
    var seq = detailRequestSeq;
    setReminderControlsDisabled(body, true);
    section.setAttribute("aria-busy", "true");
    try {
      await patchJson("/reminders/" + reminderId, payload);
      await refreshReminders(body, job, seq);
    } catch (error) {
      if (seq !== detailRequestSeq || !isCurrentDetailJob(jobId)) return;
      if (error && error.status === 404) {
        await refreshReminders(body, job, seq);
        return;
      }
      setReminderControlsDisabled(body, false);
      section.removeAttribute("aria-busy");
      if (error && error.status === 422) {
        var validation = form.querySelector(".reminder-validation");
        if (validation) {
          validation.hidden = false;
          validation.textContent = error.message;
        }
        return;
      }
      replaceReminderEditError(body, error, function () {
        submitReminderEdit(form, body, job, reminder, payload);
      });
    } finally {
      reminderOperationActive = false;
    }
  }

  // One-error-panel-maximum for edit failures: any existing section error box
  // is removed before a fresh one is appended, so a failed edit never
  // accumulates duplicate panels. The Retry action re-issues the exact failed
  // PATCH payload -- never a re-confirmation -- because the retry closure owns
  // the captured payload, not the form.
  function replaceReminderEditError(body, error, retryAction) {
    body.querySelectorAll(".error-box").forEach(function (box) {
      box.remove();
    });
    var box = el("div", { className: "error-box" });
    box.appendChild(el("p", { textContent: "The reminder could not be saved." }));
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
    body.appendChild(box);
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
