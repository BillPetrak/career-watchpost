# ruff: noqa: E501
"""Focused source and DOM-behavior checks for the Today workspace."""

from __future__ import annotations

import subprocess
import textwrap
from importlib import resources
from pathlib import Path

_STATIC = Path(str(resources.files("careeros.api").joinpath("static")))


def _asset(name: str) -> str:
    return _STATIC.joinpath(name).read_text(encoding="utf-8")


def _today_slice(js: str) -> str:
    start = js.index("// Today workspace (#today)")
    end = js.index("// Job Detail view (#job/<id>)", start)
    return js[start:end]


_NODE_HARNESS = r"""
const fs = require("fs");

class FakeElement {
  constructor(tag, owner) {
    this.tagName = tag.toUpperCase(); this.ownerDocument = owner; this.parentNode = null;
    this.children = []; this.listeners = {}; this.attributes = {}; this._text = "";
    this.className = ""; this.hidden = false; this.disabled = false; this.checked = false;
  }
  get id() { return this.attributes.id || ""; }
  set id(value) { this.attributes.id = String(value); this.ownerDocument.ids[String(value)] = this; }
  get href() { return this.attributes.href || ""; }
  set href(value) { this.attributes.href = String(value); }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  set textContent(value) { this._text = String(value); this.children.forEach((child) => { child.parentNode = null; }); this.children = []; }
  get childNodes() { return this.children; }
  get nextSibling() { if (!this.parentNode) return null; const index = this.parentNode.children.indexOf(this); return this.parentNode.children[index + 1] || null; }
  get isConnected() { return this === this.ownerDocument.body || Boolean(this.parentNode && this.parentNode.isConnected); }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  insertBefore(child, reference) { child.parentNode = this; const index = reference ? this.children.indexOf(reference) : -1; if (index < 0) this.children.push(child); else this.children.splice(index, 0, child); return child; }
  remove() { if (!this.parentNode) return; const index = this.parentNode.children.indexOf(this); if (index >= 0) this.parentNode.children.splice(index, 1); this.parentNode = null; }
  setAttribute(name, value) { if (name === "id") this.id = value; else this.attributes[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name) { (this.listeners[name] || []).forEach((callback) => callback({ button: 0, preventDefault() {}, target: this })); }
  focus() { this.ownerDocument.activeElement = this; }
  matches(selector) {
    let value = selector.trim();
    if (value.endsWith(":not([hidden])")) { if (this.hidden) return false; value = value.slice(0, -14); }
    const tag = /^([a-z][a-z0-9-]*)/i.exec(value); if (tag && this.tagName !== tag[1].toUpperCase()) return false;
    const id = /#([a-z0-9_-]+)/i.exec(value); if (id && this.id !== id[1]) return false;
    for (const match of value.matchAll(/\.([a-z0-9_-]+)/ig)) if (!this.className.split(/\s+/).includes(match[1])) return false;
    for (const match of value.matchAll(/\[([^\]=~^$*]+)(?:([\^]?=)["']?([^\]"']*)["']?)?\]/g)) {
      const actual = this.getAttribute(match[1]); if (actual === null) return false;
      if (match[2] === "=" && actual !== match[3]) return false;
      if (match[2] === "^=" && !actual.startsWith(match[3])) return false;
    }
    return true;
  }
  querySelectorAll(selector) {
    const selectors = selector.split(",").map((part) => part.trim()); const nodes = [];
    const visit = (node) => node.children.forEach((child) => { nodes.push(child); visit(child); }); visit(this);
    const matchesChain = (node, candidate) => {
      const parts = candidate.split(/\s+/); if (!node.matches(parts.pop())) return false;
      let ancestor = node.parentNode;
      while (parts.length) {
        const expected = parts.pop(); while (ancestor && ancestor !== this.parentNode && !ancestor.matches(expected)) ancestor = ancestor.parentNode;
        if (!ancestor || ancestor === this.parentNode) return false; ancestor = ancestor.parentNode;
      }
      return true;
    };
    return nodes.filter((node) => selectors.some((candidate) => matchesChain(node, candidate)));
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

class FakeDocument {
  constructor() { this.ids = {}; this.activeElement = null; this.listeners = {}; this.body = new FakeElement("body", this); }
  createElement(tag) { return new FakeElement(tag, this); }
  createTextNode(text) { const node = new FakeElement("#text", this); node.textContent = text; return node; }
  getElementById(id) { const node = this.ids[id] || null; return node && node.isConnected ? node : null; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  querySelectorAll(selector) { return this.body.querySelectorAll(selector); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

const document = new FakeDocument();
const window = { document, location: { hash: "#today", origin: "http://careeros.test" }, addEventListener() {}, confirm() { return true; } };
global.document = document; global.window = window; global.URL = URL;
function append(parent, tag, id, className) { const node = document.createElement(tag); if (id) node.id = id; if (className) node.className = className; parent.appendChild(node); return node; }
const mainNode = append(document.body, "main", "main", "");
const todayView = append(mainNode, "section", "view-today", "view"); todayView.setAttribute("data-view", "today");
const toggle = append(todayView, "input", "today-use-learned", ""); toggle.checked = true;
const status = append(todayView, "div", "today-status", "status");
const content = append(todayView, "div", "today-content", "view-content");
const homeView = append(mainNode, "section", "view-home", "view"); homeView.setAttribute("data-view", "home"); homeView.hidden = true;
append(homeView, "div", "home-status", "status"); append(homeView, "div", "home-content", "view-content");

function response(payload, statusCode = 200) { return { ok: statusCode >= 200 && statusCode < 300, status: statusCode, json: async () => payload }; }
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
function assert(condition, message) { if (!condition) throw new Error(message); }
function tick() { return new Promise((resolve) => setTimeout(resolve, 0)); }
function job(id, title, company = "Example Org") { return { id, title, company, location: "Remote", remote_status: "remote", employment_type: "full_time", source: "fixture", published_at: "2026-09-01T00:00:00+00:00" }; }
function bucket(items = []) { return { total: items.length, items }; }
function reminder(id, title) { return { reminder_id: id, kind: "follow_up", due_at: "2026-09-18T09:00:00+00:00", job: job(id, title), active_application_status: null }; }
function application(id, title, nextAction, classification = "attention", overrides = {}) { return Object.assign({ application_id: id, job_id: id, current_status: "interview", updated_at: "2026-09-01T00:00:00+00:00", job: job(id, title), classification, next_action: nextAction, earliest_deadline_at: null, preparation: null, package: null }, overrides); }
function reviewItem(id, title, score) { const representative = job(id, title); return { group: { group_id: "g" + id, representative, members: [representative] }, match_score: score, score: score, review_decision: null, application_status: null, explanation: { summary: "Safe explanation for " + title, matches: [], gaps: [], unknowns: [], other_factors: [] } }; }
function snapshot(overrides = {}) { return Object.assign({ as_of: "2026-09-18T08:00:00+00:00", timezone: "UTC", today_ends_at: "2026-09-19T00:00:00+00:00", limit: 10, reminders: { overdue: bucket(), due_today: bucket() }, applications: { overdue: bucket(), due_today: bucket(), work: bucket() }, shortlist: { unstarted: bucket() }, review: { next: { candidate_total: 0, total: 0, limit: 3, minimum_score: 1, personalized: false, use_learned_preferences: true, items: [] } } }, overrides); }
function section(title) { return content.querySelectorAll("section").find((node) => { const heading = node.querySelector("h2"); return heading && heading.textContent === title; }); }

let source = fs.readFileSync(process.argv[2], "utf8");
source = source.replace(
  '  window.addEventListener("hashchange", navigate);\n})();',
  `  window.__todayTest = { renderContent: renderTodayContent, renderView: renderTodayView, refresh: requestTodayRefresh, complete: completeTodayReminder, toggleChanged: todayLearnedPreferenceChanged, navigate: navigate, content: function () { return content; } };\n  window.addEventListener("hashchange", navigate);\n})();`
);
eval(source);
const H = window.__todayTest;
"""


def _run_node_today(tmp_path: Path, scenario: str) -> None:
    script = tmp_path / "today-harness.js"
    script.write_text(
        _NODE_HARNESS + "\n" + textwrap.dedent(scenario), encoding="utf-8"
    )
    result = subprocess.run(
        ["node", str(script), str(_STATIC / "app.js")],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_today_dom_order_cards_empty_partial_and_safe_text(tmp_path: Path) -> None:
    _run_node_today(
        tmp_path,
        r"""
        const hostileTitle = "<img src=x onerror=alert(1)>";
        const applications = [
          application(11, "Deadline overdue", "review_preparation_deadline", "overdue", { earliest_deadline_at: "2026-09-17T09:00:00+00:00", preparation: { category: "incomplete", blocking_count: 2 } }),
          application(12, "Deadline today", "review_preparation_deadline", "due_today", { earliest_deadline_at: "2026-09-18T12:00:00+00:00", preparation: { category: "incomplete", blocking_count: 1 } }),
          application(13, "Complete prep", "complete_preparation", "attention", { preparation: { category: "unknown", blocking_count: 1 } }),
          application(14, "Confirm prep", "confirm_preparation_ready", "attention", { preparation: { category: "requirements_satisfied", blocking_count: 0 } }),
          application(15, "Stale draft", "review_rebase_package", "attention", { package: { has_draft: true, draft_approved_for_current_revision: false, draft_preparation_revision_stale: true } }),
          application(16, "Ordinary draft", "continue_package", "attention", { package: { has_draft: true, draft_approved_for_current_revision: false, draft_preparation_revision_stale: false } }),
          application(17, "Offer status", "manage_application", "attention", { current_status: "offer" }),
          application(18, "Interview status", "manage_application", "attention", { current_status: "interview" }),
        ];
        const shortlistRepresentative = job(21, "Shortlist title");
        const data = snapshot({
          reminders: { overdue: bucket([reminder(1, hostileTitle), reminder(2, "Second reminder")]), due_today: bucket([reminder(3, "Due reminder")]) },
          applications: { overdue: bucket([applications[0]]), due_today: bucket([applications[1]]), work: bucket(applications.slice(2)) },
          shortlist: { unstarted: bucket([{ group: { group_id: "short", representative: shortlistRepresentative, members: [shortlistRepresentative] }, conflict: false }]) },
          review: { next: { candidate_total: 4, total: 2, limit: 3, minimum_score: 1, personalized: true, use_learned_preferences: true, items: [reviewItem(31, "Lower score first", 2), reviewItem(32, "Higher score second", 99)] } },
        });
        H.renderContent(content, data);
        assert(content.querySelectorAll("h2").map((node) => node.textContent).join("|") === "Overdue reminders|Overdue application work|Reminders due later today|Application work due later today|Application attention|Shortlisted, not started|Review next", "section order changed");
        assert(section("Overdue reminders").querySelectorAll(".job-title").map((node) => node.textContent).join("|") === hostileTitle + "|Second reminder", "reminder server order changed");
        assert(section("Application attention").querySelectorAll(".job-title").map((node) => node.textContent).join("|") === "Complete prep|Confirm prep|Stale draft|Ordinary draft|Offer status|Interview status", "application server order changed");
        assert(section("Review next").querySelectorAll(".job-title").map((node) => node.textContent).join("|") === "Lower score first|Higher score second", "Review-next server order changed");
        const actionLabels = content.querySelectorAll(".today-application-card").map((card) => card.querySelector(".job-actions a").textContent);
        assert(actionLabels.join("|") === "Review preparation deadline|Review preparation deadline|Complete preparation|Confirm preparation ready|Review and rebase package|Continue package|Manage application|Manage application", "server next_action labels changed");
        assert(content.textContent.includes("Draft needs rebase") && content.textContent.includes("Draft in progress"), "package compact state missing");
        assert(content.textContent.includes("Match score:2") && content.textContent.includes("Safe explanation for Lower score first"), "review score/explanation missing");
        assert(content.querySelectorAll("img").length === 0 && content.querySelectorAll("script").length === 0, "hostile text became markup");
        assert(content.querySelectorAll(".today-review-card button").length === 0, "Review mutation control leaked into Today");

        H.renderContent(content, snapshot());
        assert(content.textContent.includes("Nothing needs attention today."), "all-empty state missing");
        assert(content.querySelectorAll("h2").length === 7, "empty sections lost frozen hierarchy");
        assert(content.textContent.includes("Review next is not personalized yet."), "zero-personalization state missing");

        H.renderContent(content, snapshot({ applications: { overdue: bucket(), due_today: bucket(), work: bucket([application(40, "Application only", "manage_application")]) } }));
        assert(content.textContent.includes("Application only") && !content.textContent.includes("Due reminder"), "application-only state failed");
        H.renderContent(content, snapshot({ reminders: { overdue: bucket([reminder(41, "Reminder only")]), due_today: bucket() } }));
        assert(content.textContent.includes("Reminder only") && !content.textContent.includes("Application only"), "reminder-only state failed");
        H.renderContent(content, snapshot({ review: { next: { candidate_total: 2, total: 0, limit: 3, minimum_score: 1, personalized: false, use_learned_preferences: false, items: [] } } }));
        assert(content.textContent.includes("No eligible opportunities currently meet the minimum match score."), "threshold-empty state missing");
        assert(content.textContent.includes("Learned preferences are off for this snapshot."), "learned-disabled state missing");
        """,
    )


def test_today_toggle_latest_response_wins(tmp_path: Path) -> None:
    _run_node_today(
        tmp_path,
        r"""
        async function main() {
          const requests = [];
          global.fetch = (url) => { const item = { url, pending: deferred() }; requests.push(item); return item.pending.promise; };
          const older = H.renderView();
          assert(requests[0].url === "/today?limit=10&use_learned_preferences=true", "default toggle query missing");
          toggle.checked = false; H.toggleChanged(); await tick();
          assert(requests.length === 2 && requests[1].url === "/today?limit=10&use_learned_preferences=false", "toggle query missing");
          requests[1].pending.resolve(response(snapshot({ reminders: { overdue: bucket([reminder(2, "Latest result")]), due_today: bucket() }, review: { next: { candidate_total: 0, total: 0, limit: 3, minimum_score: 1, personalized: false, use_learned_preferences: false, items: [] } } })));
          await tick(); await tick();
          assert(content.textContent.includes("Latest result"), "latest toggle result did not render");
          requests[0].pending.resolve(response(snapshot({ reminders: { overdue: bucket([reminder(1, "Stale result")]), due_today: bucket() } })));
          await older; await tick();
          assert(content.textContent.includes("Latest result") && !content.textContent.includes("Stale result"), "stale toggle response overwrote latest result");
        }
        main().catch((error) => { console.error(error); process.exitCode = 1; });
        """,
    )


def test_today_pending_get_cannot_repaint_after_navigation(tmp_path: Path) -> None:
    _run_node_today(
        tmp_path,
        r"""
        async function main() {
          const todayPending = deferred(); const reportPending = deferred();
          global.fetch = (url) => url.startsWith("/today?") ? todayPending.promise : reportPending.promise;
          const load = H.renderView();
          window.location.hash = "#home"; H.navigate();
          assert(homeView.hidden === false && todayView.hidden === true, "navigation did not leave Today");
          todayPending.resolve(response(snapshot({ reminders: { overdue: bucket([reminder(1, "Late Today result")]), due_today: bucket() } })));
          await load; await tick();
          assert(!content.textContent.includes("Late Today result"), "pending Today GET repainted after navigation");
        }
        main().catch((error) => { console.error(error); process.exitCode = 1; });
        """,
    )


def test_today_reminder_write_invalidates_stale_get_and_coalesces(
    tmp_path: Path,
) -> None:
    _run_node_today(
        tmp_path,
        r"""
        async function main() {
          const oldSnapshot = snapshot({ reminders: { overdue: bucket([reminder(1, "Completed reminder")]), due_today: bucket() } });
          H.renderContent(content, oldSnapshot);
          const card = content.querySelector(".today-reminder-card"); const item = oldSnapshot.reminders.overdue.items[0];
          const getRequests = []; const putPending = deferred(); let putCount = 0;
          global.fetch = (url, options = {}) => {
            if (options.method === "PUT") { putCount++; return putPending.promise; }
            const pending = deferred(); getRequests.push(pending); return pending.promise;
          };
          H.refresh(false); await tick();
          H.complete(card, item); H.complete(card, item);
          assert(putCount === 1, "duplicate Reminder write was not blocked");
          putPending.resolve(response({})); await tick();
          getRequests[0].resolve(response(oldSnapshot)); await tick(); await tick();
          assert(getRequests.length === 2, "dirty refresh was not coalesced into one latest GET");
          getRequests[1].resolve(response(snapshot())); await tick(); await tick();
          assert(!content.textContent.includes("Completed reminder"), "stale GET resurrected completed Reminder");
          assert(document.activeElement === toggle, "focus did not recover to a surviving Today control");
        }
        main().catch((error) => { console.error(error); process.exitCode = 1; });
        """,
    )


def test_today_pending_reminder_put_keeps_focus_intent_through_intervening_render(
    tmp_path: Path,
) -> None:
    _run_node_today(
        tmp_path,
        r"""
        async function main() {
          const pendingSnapshot = snapshot({
            reminders: { overdue: bucket([reminder(51, "Pending completion")]), due_today: bucket() },
            applications: { overdue: bucket(), due_today: bucket(), work: bucket([application(61, "Surviving action", "manage_application")]) },
          });
          const completedSnapshot = snapshot({
            applications: { overdue: bucket(), due_today: bucket(), work: bucket([application(61, "Surviving action", "manage_application")]) },
          });
          H.renderContent(content, pendingSnapshot);
          const card = content.querySelector('[data-reminder-id="51"]');
          const originalButton = card.querySelector("button");
          originalButton.focus();
          const item = pendingSnapshot.reminders.overdue.items[0];
          const putPending = deferred(); const getRequests = []; let putCount = 0;
          global.fetch = (url, options = {}) => {
            if (options.method === "PUT") { putCount++; return putPending.promise; }
            const pending = deferred(); getRequests.push(pending); return pending.promise;
          };

          H.complete(card, item); H.complete(card, item);
          assert(putCount === 1, "duplicate pending Reminder write was not blocked");
          H.refresh(false); await tick();
          assert(getRequests.length === 1, "intervening Today refresh did not start");
          getRequests[0].resolve(response(pendingSnapshot)); await tick(); await tick();
          assert(content.textContent.includes("Pending completion"), "intervening pre-write snapshot lost the pending Reminder");

          putPending.resolve(response({})); await tick(); await tick();
          assert(getRequests.length === 2, "successful Reminder write did not request its authoritative GET");
          getRequests[1].resolve(response(completedSnapshot)); await tick(); await tick();

          const survivor = content.querySelector(".today-application-card a");
          assert(putCount === 1, "Today refresh replayed the Reminder write");
          assert(!content.textContent.includes("Pending completion"), "stale data resurrected the completed Reminder");
          assert(document.activeElement && document.activeElement.isConnected, "focus remained on a detached completed Reminder control");
          assert(document.activeElement === survivor || document.activeElement === toggle || document.activeElement === status, "focus did not recover to a sensible surviving Today control");
          assert(document.activeElement !== originalButton, "removed Reminder control retained focus");
        }
        main().catch((error) => { console.error(error); process.exitCode = 1; });
        """,
    )


def test_today_overlapping_different_reminder_completions_keep_focus_connected(
    tmp_path: Path,
) -> None:
    _run_node_today(
        tmp_path,
        r"""
        async function main() {
          const bothSnapshot = snapshot({
            reminders: { overdue: bucket([reminder(71, "Reminder A"), reminder(72, "Reminder B")]), due_today: bucket() },
            applications: { overdue: bucket(), due_today: bucket(), work: bucket([application(81, "Surviving action", "manage_application")]) },
          });
          const onlyBSnapshot = snapshot({
            reminders: { overdue: bucket([reminder(72, "Reminder B")]), due_today: bucket() },
            applications: { overdue: bucket(), due_today: bucket(), work: bucket([application(81, "Surviving action", "manage_application")]) },
          });
          const completedSnapshot = snapshot({
            applications: { overdue: bucket(), due_today: bucket(), work: bucket([application(81, "Surviving action", "manage_application")]) },
          });
          H.renderContent(content, bothSnapshot);
          const cardA = content.querySelector('[data-reminder-id="71"]');
          const cardB = content.querySelector('[data-reminder-id="72"]');
          const buttonA = cardA.querySelector("button"); const buttonB = cardB.querySelector("button");
          const puts = {}; const putCounts = { 71: 0, 72: 0 }; const getRequests = [];
          global.fetch = (url, options = {}) => {
            if (options.method === "PUT") {
              const id = Number(url.split("/")[2]);
              putCounts[id]++; puts[id] = deferred(); return puts[id].promise;
            }
            const pending = deferred(); getRequests.push(pending); return pending.promise;
          };

          buttonA.focus(); H.complete(cardA, bothSnapshot.reminders.overdue.items[0]);
          buttonB.focus(); H.complete(cardB, bothSnapshot.reminders.overdue.items[1]);
          assert(putCounts[71] === 1 && putCounts[72] === 1, "different Reminders did not get independent writes");

          H.refresh(false); await tick();
          getRequests[0].resolve(response(bothSnapshot)); await tick(); await tick();
          assert(content.textContent.includes("Reminder A") && content.textContent.includes("Reminder B"), "intervening snapshot did not preserve both pending Reminders");

          puts[71].resolve(response({})); await tick(); await tick();
          assert(getRequests.length === 2, "Reminder A success did not start an authoritative GET");
          getRequests[1].resolve(response(onlyBSnapshot)); await tick(); await tick();
          assert(!content.textContent.includes("Reminder A") && content.textContent.includes("Reminder B"), "Reminder A reconciliation was not authoritative");
          const reconciledButtonB = content.querySelector('[data-reminder-id="72"] button');

          puts[72].resolve(response({})); await tick(); await tick();
          assert(getRequests.length === 3, "Reminder B success did not start its authoritative GET");
          getRequests[2].resolve(response(completedSnapshot)); await tick(); await tick();

          const survivor = content.querySelector(".today-application-card a");
          assert(putCounts[71] === 1 && putCounts[72] === 1, "a Reminder write was duplicated by reconciliation");
          assert(!content.textContent.includes("Reminder A") && !content.textContent.includes("Reminder B"), "a completed Reminder survived authoritative reconciliation");
          assert(document.activeElement && document.activeElement.isConnected, "overlapping completions left focus detached");
          assert(document.activeElement === survivor || document.activeElement === toggle || document.activeElement === status, "overlapping completions did not recover to a sensible Today control");
          assert(document.activeElement !== buttonA && document.activeElement !== buttonB && document.activeElement !== reconciledButtonB, "a removed Reminder control retained focus");
        }
        main().catch((error) => { console.error(error); process.exitCode = 1; });
        """,
    )


def test_today_is_first_nav_and_home_remains_default() -> None:
    html = _asset("index.html")
    nav = html[html.index('<nav class="nav"') : html.index("</nav>")]
    assert nav.index('id="nav-today"') < nav.index('id="nav-home"')
    js = _asset("app.js")
    assert 'var TODAY = "today";' in js
    assert "if (view === TODAY)" in js
    assert "view = HOME;" in js
    assert "view || visibleView() || HOME" in js


def test_today_uses_one_authoritative_get_and_exact_sections() -> None:
    js = _asset("app.js")
    today = _today_slice(js)
    assert '"/today?limit=10&use_learned_preferences="' in today
    ordered_render_calls = (
        'buildTodaySection("Overdue reminders", overdue, buildTodayReminderCard)',
        'buildTodaySection("Overdue application work", applicationOverdue, buildTodayApplicationCard)',
        'buildTodaySection("Reminders due later today", due, buildTodayReminderCard)',
        'buildTodaySection("Application work due later today", applicationDue, buildTodayApplicationCard)',
        'buildTodaySection("Application attention", applicationWork, buildTodayApplicationCard)',
        'buildTodaySection("Shortlisted, not started", unstarted, buildTodayShortlistCard)',
        "content.appendChild(buildTodayReviewSection(reviewNext))",
    )
    positions = [today.index(call) for call in ordered_render_calls]
    assert positions == sorted(positions)
    assert ".sort(" not in today
    assert "groupBy" not in today and "join(" not in today
    assert "getUTC" not in today and "Date.parse" not in today
    assert "classified by the server using UTC" in today
    assert "items.forEach" in today


def test_today_application_cards_render_only_server_projection_and_next_action() -> (
    None
):
    today = _today_slice(_asset("app.js"))
    for action, label in {
        "review_preparation_deadline": "Review preparation deadline",
        "complete_preparation": "Complete preparation",
        "confirm_preparation_ready": "Confirm preparation ready",
        "review_rebase_package": "Review and rebase package",
        "continue_package": "Continue package",
        "manage_application": "Manage application",
    }.items():
        assert f'{action}: "{label}"' in today
    assert "TODAY_APPLICATION_ACTIONS[item.next_action]" in today
    assert "item.earliest_deadline_at" in today
    assert "item.preparation.category" in today
    assert "item.preparation.blocking_count" in today
    assert "item.package.draft_preparation_revision_stale" in today
    assert "item.package.draft_approved_for_current_revision" in today
    assert 'href: "#applications"' in today
    for forbidden in (
        "completeness",
        "requirement.label",
        "requirement.detail",
        "material",
        "application.note",
        "review.note",
    ):
        assert forbidden not in today


def test_today_review_next_is_a_server_ordered_navigation_only_preview() -> None:
    today = _today_slice(_asset("app.js"))
    assert "Array.isArray(reviewNext.items) ? reviewNext.items : []" in today
    assert "items.forEach" in today
    assert "buildJobCard(job, item.match_score, null, group)" in today
    assert "appendCardExplanation(card, item.explanation)" in today
    assert 'href: "#review", textContent: "Open Review"' in today
    assert 'href: "#matching", textContent: "Matching Preferences"' in today
    assert "reviewNext.candidate_total" in today
    assert "reviewNext.total" in today
    assert "reviewNext.personalized === true" in today
    assert "reviewNext.use_learned_preferences === false" in today
    assert "No eligible opportunities to review." in today
    assert "No eligible opportunities currently meet the minimum match score." in today
    assert "attachQueueDecisionActions" not in today
    assert 'putJson("/review-decisions' not in today
    assert 'deleteJson("/review-decisions' not in today


def test_today_learned_preference_toggle_is_get_only_and_stale_guarded() -> None:
    html = _asset("index.html")
    today_html = html[
        html.index('id="view-today"') : html.index(
            "</section>", html.index('id="view-today"')
        )
    ]
    assert 'id="today-use-learned"' in today_html
    assert "Use learned preferences" in today_html
    today = _today_slice(_asset("app.js"))
    assert "todayUsesLearnedPreferences()" in today
    assert "String(useLearnedPreferences)" in today
    assert "useLearnedPreferences !== todayUsesLearnedPreferences()" in today
    assert "dataGeneration !== todayDataGeneration" in today
    assert "function todayLearnedPreferenceChanged()" in today
    assert "todayDataGeneration++" in today
    assert "requestTodayRefresh(false)" in today
    assert "/preferences" not in today


def test_today_safe_dom_and_public_fields_only() -> None:
    today = _today_slice(_asset("app.js"))
    assert "textContent" in today
    assert "innerHTML" not in today and "insertAdjacentHTML" not in today
    assert "reminder.message" not in today and "reminder.note" not in today
    assert "createElement" not in today or "el(" in today
    for external_field in (
        "job.title",
        "job.company",
        "item.explanation",
    ):
        assert external_field in today
    assert "eval(" not in today


def test_today_empty_states_and_focus_recovery() -> None:
    today = _today_slice(_asset("app.js"))
    assert "Nothing needs attention today." in today
    assert "Nothing here today." in today
    assert "today-boundary-note" in today
    assert "todayFocusTarget" in today
    assert "restoreTodayFocus" in today
    assert "data-reminder-id" in today
    assert ".today-application-card a" in today
    assert ".today-review-card a" in today
    assert 'document.getElementById("today-use-learned")' in today
    assert "status.focus()" in today


def test_today_independent_generation_and_concurrency_guards() -> None:
    today = _today_slice(_asset("app.js"))
    for name in (
        "todayViewGeneration",
        "todayRequestSeq",
        "todayDataGeneration",
        "todayRefreshActive",
        "todayRefreshOwner",
        "todayRefreshDirty",
        "todayRefreshSaved",
        "todayOperationSeq",
        "todayReminderInFlight",
    ):
        assert name in today or name in _asset("app.js")
    assert "todayRefreshDirty = true" in today
    assert "while (todayRefreshDirty" in today
    assert "if (todayRefreshOwner === owner)" in today
    assert "dataGeneration !== todayDataGeneration" in today
    assert "todayReminderInFlight[reminder.reminder_id]" in today
    assert "todayDataGeneration++" in today
    assert "requestTodayRefresh(true)" in today


def test_today_navigation_away_invalidates_pending_work() -> None:
    js = _asset("app.js")
    navigation = js[js.index("async function navigate()") : js.index("function el(")]
    assert "if (leaving === TODAY && name !== TODAY)" in navigation
    assert "todayRequestSeq++" in navigation
    assert "todayViewGeneration++" in navigation
    assert "todayRefreshDirty = false" in navigation
    assert "todayRefreshSaved = false" in navigation
    assert "todayFocusTarget = null" in navigation
    today = _today_slice(js)
    assert "visibleView() !== TODAY" in today


def test_today_put_retry_and_get_only_retry_contract() -> None:
    today = _today_slice(_asset("app.js"))
    assert 'putJson("/reminders/" + reminder.reminder_id + "/complete", {})' in today
    assert '"The reminder could not be completed."' in today
    assert "completeTodayReminder(card, reminder)" in today
    assert "error.status === 404" in today
    assert '"Reminder saved, but Today could not be refreshed.' in today
    # A refresh retry calls GET-only renderTodayView and does not replay PUT.
    assert "renderTodayView({ refresh: true })" in today


def test_today_job_detail_origin_preserves_existing_origins() -> None:
    js = _asset("app.js")
    assert "jobDetailOrigin = TODAY" in js
    assert 'href: "#today"' in js
    assert 'textContent: "Back to Today"' in js
    assert "jobDetailOrigin === REVIEWED" in js
    assert "jobDetailOrigin === APPLICATIONS" in js
    assert ' : "Back to jobs"' in js


def test_today_accessibility_contract() -> None:
    html = _asset("index.html")
    assert 'id="today-status" class="status" role="status" aria-live="polite"' in html
    assert 'id="today-content" class="view-content"' in html
    assert '<label class="inline-label" for="today-use-learned">' in html
    assert (
        'id="today-use-learned" name="use_learned_preferences" type="checkbox" checked'
        in html
    )
    js = _asset("app.js")
    assert 'setLoading(status, "Loading today' in js
    assert 'status.parentNode.removeAttribute("aria-busy")' in js
    assert 'label: "Complete reminder"' in js


def test_home_does_not_acquire_today_or_personalization_controls() -> None:
    html = _asset("index.html")
    home_start = html.index('id="view-home"')
    home_end = html.index("</section>", home_start)
    home = html[home_start:home_end]
    for forbidden in (
        "Overdue application work",
        "Application attention",
        "Review next",
        "Use learned preferences",
        "today-use-learned",
    ):
        assert forbidden not in home
    js = _asset("app.js")
    home_js = js[js.index("function renderHome") : js.index("// Jobs view")]
    assert 'fetchJson("/report")' in home_js
    assert 'fetchJson("/today' not in home_js
