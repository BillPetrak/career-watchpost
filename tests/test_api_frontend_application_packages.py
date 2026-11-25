# ruff: noqa: E501
"""Focused frontend contracts for job-specific Application Packages."""

from __future__ import annotations

import sqlite3
import subprocess
import textwrap
from pathlib import Path

from careeros.api.main import create_app
from careeros.database.schema import initialize_schema
from tests.support import TestClient

STATIC = Path(__file__).parents[1] / "src/careeros/api/static"


def asset(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def seed(path: Path) -> int:
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    initialize_schema(connection)
    timestamp = "2026-09-01T10:00:00+00:00"
    connection.execute(
        "INSERT INTO jobs(source,source_job_id,title,company,description,collected_at,updated_at) VALUES(?,?,?,?,?,?,?)",
        (
            "frontend-fixture",
            "fictional-package-job",
            "Fictional Package Engineer",
            "Example Org",
            "Synthetic frontend fixture only",
            timestamp,
            timestamp,
        ),
    )
    job_id = int(connection.execute("SELECT id FROM jobs").fetchone()[0])
    connection.execute(
        "INSERT INTO applications(job_id,current_status,created_at,updated_at) VALUES(?,?,?,?)",
        (job_id, "applied", timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO application_preparations(application_id,revision,requirements_reviewed_revision,requirements_reviewed_at,ready_confirmed_revision,ready_confirmed_at,created_at,updated_at) VALUES(1,0,0,?,0,?,?,?)",
        (timestamp, timestamp, timestamp, timestamp),
    )
    connection.execute(
        "INSERT INTO application_requirement_items(id,application_id,kind,label,required,fulfillment_state,sort_order,origin,capture_method,confirmed,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "question-1",
            1,
            "question",
            "Why this fictional role?",
            1,
            "satisfied",
            0,
            "manual",
            "manual",
            1,
            timestamp,
            timestamp,
        ),
    )
    connection.commit()
    connection.close()
    return job_id


def test_source_contract_order_scope_privacy_and_accessibility() -> None:
    source = asset("app.js")
    css = asset("style.css")
    keys = 'var keys = ["job", "group", "application", "preparation", "package", "review", "reminders"];'
    assert keys in source
    order = [
        'fillDetailSection(content, job, "application", results.application);',
        'fillDetailSection(content, job, "preparation", results.preparation);',
        'fillDetailSection(content, job, "package", results.package);',
        'fillDetailSection(content, job, "review", results.review);',
        'fillDetailSection(content, job, "reminders", results.reminders);',
    ]
    positions = [source.index(value) for value in order]
    assert positions == sorted(positions)
    for endpoint in (
        '"/package?offset=" + packageHistoryOffset + "&limit=" + packageHistoryLimit',
        '"/package/draft"',
        '"/material-selections/"',
        '"/answers/"',
        '"/confirmation"',
        '"/rebase"',
        '"/approval"',
        '"/finalization"',
        '"/materials?offset="',
        '"/materials/" + materialId',
    ):
        assert endpoint in source
    for state in (
        "packageSectionRequestSeq",
        "packageOperationSeq",
        "packageOperationActive",
        "packageDraftIntent",
        "packageHistoryRequestSeq",
        "packageChooserRequestSeq",
        "packageChooserDetailRequestSeq",
        "packageFinalizationReconcile",
    ):
        assert state in source
    package_slice = source[
        source.index(
            "// Application Package is an independently loaded"
        ) : source.index(
            "// Canonical review decisions",
            source.index("// Application Package is an independently loaded"),
        )
    ]
    for persistence in (
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "document.cookie",
        "caches.open",
        "console.",
    ):
        assert persistence not in package_slice
    assert "clearPackagePrivateState();" in source
    assert 'body.setAttribute("aria-live", "polite")' in package_slice
    assert 'setAttribute("aria-busy", "true")' in package_slice
    assert ".focus()" in package_slice
    assert "window.confirm(message)" in package_slice
    assert "No automatic retry was made" in package_slice
    assert "reconcilePackageFinalization" in package_slice
    assert package_slice.count('/finalization", "PUT"') == 1
    assert "Category is display metadata only" in package_slice
    assert 'textContent: "Rebase draft explicitly"' in package_slice
    assert (
        "var preparation = await currentPreparation(job, operation);" in package_slice
    )
    assert "{ rebase: true }, true" in package_slice
    assert "Job reconfirmation required" in package_slice
    assert "requirements review and Ready confirmation" in package_slice
    for approval_state in (
        "Current for this package revision",
        "Stale after a package change",
        "Not approved",
    ):
        assert approval_state in package_slice
    assert "payload: currentApproval ? {} : { approved: true }" in package_slice
    assert "payload: answer.confirmed ? {} : { confirmed: true }" in package_slice
    assert (
        'textContent: answer.confirmed ? "Clear answer confirmation" : "Confirm answer"'
        in package_slice
    )
    assert 'textContent: "Delete answer"' in package_slice
    assert "completeness.reasons.forEach" in package_slice
    assert "renderMaterialsList(" not in package_slice
    assert "renderMaterialDetail(" not in package_slice
    assert "buildPipelinePackageLifecycle(item.package)" in source
    pipeline = source[
        source.index("function buildApplicationPipelineCard") : source.index(
            "function renderApplicationsView"
        )
    ]
    assert "fetchJson(" not in pipeline
    assert "answer" not in pipeline.lower()
    assert "byte_size" not in pipeline
    assert ".application-package-section" in css
    assert ".package-answer-form" in css
    assert "@media (max-width: 600px)" in css
    assert ".package-lifecycle-actions" in css


def test_real_api_and_static_assets_support_frontend_package_contract(
    tmp_path: Path,
) -> None:
    database = tmp_path / "frontend-package.sqlite3"
    materials_root = tmp_path / "materials"
    job_id = seed(database)
    app = create_app(database, materials_root)
    with TestClient(app) as client:
        script = client.get("/ui/app.js")
        assert script.status_code == 200
        assert "Application package" in script.text
        empty = client.get(f"/applications/{job_id}/package?offset=0&limit=20")
        assert empty.status_code == 200
        assert empty.json()["package_id"] is None
        opened = client.put(
            f"/applications/{job_id}/package/draft",
            json={},
            headers={
                "X-CareerOS-Write": "1",
                "X-CareerOS-Preparation-Revision": "0",
            },
        )
        assert opened.status_code == 201
        detail = opened.json()
        assert detail["state"] == "draft"
        assert detail["requirements"][0]["requirement_id"] == "question-1"
        assert "description" not in detail["job_snapshot"]
        pipeline = client.get("/application-pipeline").json()["items"][0]
        assert pipeline["package"] == {
            "has_draft": True,
            "draft_approved_for_current_revision": False,
            "finalized_count": 0,
            "latest_finalized_at": None,
        }


NODE_HARNESS = r"""
"use strict";
const fs = require("fs");

class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName).toUpperCase(); this.ownerDocument = ownerDocument;
    this.parentNode = null; this.children = []; this.attributes = {}; this.listeners = {};
    this.hidden = false; this.disabled = false; this.required = false; this.value = "";
    this.className = ""; this._id = ""; this._text = ""; this.tabIndex = 0;
  }
  set id(value) { this._id = String(value); if (this._id) this.ownerDocument.ids[this._id] = this; }
  get id() { return this._id; }
  set textContent(value) { this._text = String(value); this.children.forEach((child) => { child.parentNode = null; }); this.children = []; }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  get childNodes() { return this.children; }
  get isConnected() { return this === this.ownerDocument.body || Boolean(this.parentNode && this.parentNode.isConnected); }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  prepend(child) { child.parentNode = this; this.children.unshift(child); return child; }
  insertBefore(child, reference) { child.parentNode = this; const index = reference ? this.children.indexOf(reference) : -1; if (index < 0) this.children.push(child); else this.children.splice(index, 0, child); return child; }
  remove() { if (!this.parentNode) return; const index = this.parentNode.children.indexOf(this); if (index >= 0) this.parentNode.children.splice(index, 1); this.parentNode = null; }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name) { (this.listeners[name] || []).forEach((callback) => callback({ preventDefault() {}, target: this })); }
  focus() { this.ownerDocument.activeElement = this; }
  matches(selector) {
    if (selector[0] === "#") return this.id === selector.slice(1);
    if (selector[0] === ".") return this.className.split(/\s+/).includes(selector.slice(1));
    return this.tagName.toLowerCase() === selector.toLowerCase();
  }
  querySelectorAll(selector) {
    const selectors = selector.split(",").map((part) => part.trim()); const found = [];
    const visit = (node) => node.children.forEach((child) => { if (selectors.some((item) => child.matches(item))) found.push(child); visit(child); });
    visit(this); return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

class FakeDocument {
  constructor() { this.ids = {}; this.activeElement = null; this.body = new FakeElement("body", this); }
  createElement(tag) { return new FakeElement(tag, this); }
  getElementById(id) { const node = this.ids[id] || null; return node && node.isConnected ? node : null; }
  addEventListener() {}
  querySelectorAll(selector) { if (selector === ".view[data-view]") return this.body.querySelectorAll(".view"); if (selector === ".nav a[href^='#']") return []; return this.body.querySelectorAll(selector); }
  querySelector(selector) { if (selector === ".view[data-view]:not([hidden])") return this.querySelectorAll(".view[data-view]").find((node) => !node.hidden) || null; return this.querySelectorAll(selector)[0] || null; }
}

const document = new FakeDocument();
const window = { document, location: { hash: "#job/1", origin: "http://careeros.test" }, addEventListener() {}, confirm() { return true; } };
global.document = document; global.window = window; global.URL = URL;
function append(parent, tag, id, className) { const node = document.createElement(tag); if (id) node.id = id; if (className) node.className = className; parent.appendChild(node); return node; }
const mainNode = append(document.body, "main", "main", "");
const jobView = append(mainNode, "section", "view-job", "view"); jobView.setAttribute("data-view", "job");
append(jobView, "div", "job-detail-status", "status");
const detailContent = append(jobView, "div", "job-detail-content", "");
const preparationSection = append(detailContent, "section", "", "chunk detail-preparation-section");
const preparationBody = append(preparationSection, "div", "", "section-body");
const packageSection = append(detailContent, "section", "", "chunk detail-package-section application-package-section");
const packageBody = append(packageSection, "div", "", "section-body");

function response(status, payload) { return { ok: status >= 200 && status < 300, status, json: async () => payload }; }
function emptyResponse(status) { return { ok: true, status, json: async () => { throw new Error("204 attempted JSON parsing"); } }; }
function deferred() { let resolve, reject; const promise = new Promise((yes, no) => { resolve = yes; reject = no; }); return { promise, resolve, reject }; }
function assert(condition, message) { if (!condition) throw new Error(message); }
function tick() { return new Promise((resolve) => setTimeout(resolve, 0)); }
function summary(draft, history = [], total = history.length) { return { application_id: 1, job_id: 1, package_id: draft || total ? "a".repeat(32) : null, draft, history: { items: history, total, offset: 0, limit: 20 } }; }
function detail(revision = 0, overrides = {}) {
  return Object.assign({
    package_id: "a".repeat(32), application_id: 1, job_id: 1, version_id: "b".repeat(32), version_number: 1,
    state: "draft", revision, preparation_revision: 7, forked_from: null, job_reconfirmation_required_at: null,
    created_at: "2026-09-01T00:00:00+00:00", updated_at: "2026-09-01T00:00:00+00:00",
    job_snapshot: { job_id: 1, title: "Fictional role", company: "Example Org" },
    approval: { status: "not_approved", approved_revision: null, approved_at: null },
    requirements: [{ requirement_id: "question-1", kind: "question", label: "Private question", detail: null, required: true, fulfillment_state: "satisfied", confirmed: true }],
    material_selections: [], answers: [], preparation_stale: false, job_stale: false, job_reconfirmation_required: false,
    completeness: { category: "incomplete", blocking_count: 1, reasons: [{ code: "answer_missing", blocking: true, message: "Answer is required." }] },
  }, overrides);
}
function draftSummary(revision = 0) { return { version_id: "b".repeat(32), version_number: 1, revision, preparation_revision: 7, preparation_stale: false, job_stale: false, job_reconfirmation_required: false, approval_status: "not_approved" }; }
function preparation(revision = 7, category = "needs_requirements") { return { application_id: 1, job_id: 1, revision, requirements_reviewed_revision: null, requirements_reviewed_at: null, ready_confirmed_revision: null, ready_confirmed_at: null, readiness: { category, reasons: [] }, items: [] }; }

let source = fs.readFileSync(process.argv[2], "utf8");
source = source.replace(
  '  window.addEventListener("hashchange", navigate);\n})();',
  `  window.__packageTest = {
    render: renderApplicationPackage, open: openPackageDraft, mutate: mutatePackageDraft,
    finalize: finalizePackageVersion, remove: deletePackageVersion,
    chooser: openPackageMaterialChooser, pipeline: buildPipelinePackageLifecycle,
    httpError: readHttpError, write: writeJson,
    reconcileStatus: reconcilePackageFinalization,
    summaryPage: loadPackageSummaryPage,
    invalidate: function () { clearPackagePrivateState(); detailRequestSeq++; },
    state: function () { return { operation: packageOperationActive, intent: packageDraftIntent, chooser: packageChooserState, reconcile: packageFinalizationReconcile, reconcileBusy: packageReconciliationBusyOwner }; },
    body: function () { return packageBody; }
  };
  window.addEventListener("hashchange", navigate);
})();`
);
eval(source);
const H = window.__packageTest;
"""


def run_node_package(tmp_path: Path, scenario: str) -> None:
    script = tmp_path / "package-harness.js"
    script.write_text(NODE_HARNESS + "\n" + textwrap.dedent(scenario), encoding="utf-8")
    result = subprocess.run(
        ["node", str(script), str(STATIC / "app.js")],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_node_draft_answers_conflict_stale_navigation_and_pipeline(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          let server = detail();
          let serverSummary = summary(null);
          const requests = [];
          global.fetch = async (url, options = {}) => {
            requests.push({ url, options });
            if (url === "/applications/1/preparation") return response(200, { revision: 7, items: [], readiness: {} });
            if (url === "/applications/1/package/draft") { serverSummary = summary(draftSummary()); return response(201, server); }
            if (url.startsWith("/applications/1/package?")) return response(200, serverSummary);
            if (url === "/application-package-versions/" + "b".repeat(32) && !options.method) return response(200, server);
            throw new Error("unexpected request " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: serverSummary }, () => {});
          assert(packageBody.textContent.includes("No open package draft"), "no-package state must render");
          document.getElementById("package-open-draft").dispatch("click");
          await tick(); await tick(); await tick();
          const opened = requests.find((item) => item.url.endsWith("/package/draft"));
          assert(opened.options.method === "PUT", "draft opening must PUT");
          assert(opened.options.headers["X-CareerOS-Write"] === "1", "draft opening must carry write intent");
          assert(opened.options.headers["X-CareerOS-Preparation-Revision"] === "7", "draft opening must use live Preparation revision");
          assert(opened.options.body === "{}", "draft opening body must be exact");
          assert(document.getElementById("package-draft-heading"), "draft detail must render");

          const answer = document.getElementById("package-answer-" + "b".repeat(32) + "-question-1");
          answer.value = "PRIVATE_UNSAVED_ANSWER";
          server = detail(1, { answers: [{ requirement_id: "question-1", text: "PRIVATE_SERVER_ANSWER", confirmed: false }], completeness: { category: "incomplete", blocking_count: 1, reasons: [{ code: "answer_unconfirmed", blocking: true, message: "Confirm the answer." }] } });
          serverSummary = summary(draftSummary(1));
          global.fetch = async (url, options = {}) => {
            requests.push({ url, options });
            if (options.method === "PUT" && url.includes("/answers/question-1")) return response(409, { detail: { code: "package_revision_conflict", current_revision: 1 } });
            if (url.startsWith("/applications/1/package?")) return response(200, serverSummary);
            if (url === "/application-package-versions/" + "b".repeat(32)) return response(200, server);
            throw new Error("unexpected request " + url);
          };
          answer.focus();
          answer.parentNode.dispatch("submit");
          await tick(); await tick(); await tick();
          const refreshed = document.getElementById("package-answer-" + "b".repeat(32) + "-question-1");
          assert(refreshed.value === "PRIVATE_SERVER_ANSWER", "conflict refresh must show authoritative answer");
          assert(H.state().intent && H.state().intent.text === "PRIVATE_UNSAVED_ANSWER", "only unsaved answer intent must be retained");
          assert(document.activeElement && document.activeElement.isConnected, "conflict reconciliation must not leave focus on detached answer control");
          assert(document.activeElement.matches(".retry-button"), "conflict reconciliation must focus its actionable Retry control immediately");
          const restore = packageBody.querySelector(".package-draft-intent").querySelector("button");
          restore.dispatch("click");
          assert(refreshed.value === "PRIVATE_UNSAVED_ANSWER", "explicit restore must return unsaved answer to editor");
          assert(H.state().intent === null, "restored intent must leave retained state");

          const lifecycle = H.pipeline({ has_draft: true, draft_approved_for_current_revision: false, finalized_count: 2, latest_finalized_at: "2026-09-02T00:00:00+00:00" });
          assert(lifecycle.textContent.includes("Finalized packages:2"), "pipeline must render compact finalized count");
          const readsBefore = requests.length;
          H.pipeline({ has_draft: false, draft_approved_for_current_revision: false, finalized_count: 0, latest_finalized_at: null });
          assert(requests.length === readsBefore, "pipeline rendering must issue no detail reads");

          const delayed = deferred();
          global.fetch = (url, options = {}) => options.method === "PUT" ? delayed.promise : Promise.resolve(response(200, serverSummary));
          refreshed.value = "NAVIGATION_PRIVATE_SENTINEL";
          refreshed.parentNode.dispatch("submit");
          await tick();
          H.invalidate(); window.location.hash = "#job/2";
          const newerFocus = append(detailContent, "button", "newer-view-focus", ""); newerFocus.focus();
          assert(packageSection.textContent === "", "navigation must clear private package DOM immediately");
          assert(H.state().intent === null && H.state().chooser === null, "navigation must clear private in-memory state");
          delayed.resolve(response(409, { detail: { code: "package_revision_conflict", current_revision: 2 } }));
          await tick(); await tick();
          assert(packageSection.textContent === "", "late answer response must not repopulate old job DOM");
          assert(document.activeElement === newerFocus, "stale conflict continuation must not steal focus from a newer view");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_http_helpers_preserve_safe_package_conflicts_and_accept_204(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const packageUrl = "/application-package-versions/" + "b".repeat(32);
          const privateDetail = await H.httpError(packageUrl, response(500, { detail: "PRIVATE_BACKEND_DETAIL /secret/path" }));
          assert(!privateDetail.message.includes("PRIVATE_BACKEND_DETAIL") && !privateDetail.message.includes("/secret/path"), "arbitrary package string details must be sanitized");
          assert(privateDetail.code === undefined, "arbitrary package string details must not become public codes");
          for (const code of ["package_revision_required", "invalid_package_revision", "preparation_revision_required", "invalid_preparation_revision"]) {
            const known = await H.httpError(packageUrl, response(422, { detail: code }));
            assert(known.code === code, "known package/Preparation code must be preserved: " + code);
            assert(known.message.includes(code), "known public code may be shown safely: " + code);
          }
          const conflict = await H.httpError(packageUrl, response(409, { detail: { code: "package_revision_conflict", current_revision: 12, private_path: "/must/not/expose" } }));
          assert(conflict.code === "package_revision_conflict", "structured package conflict code must be preserved");
          assert(conflict.currentRevision === 12, "structured current_revision must be preserved");
          assert(!Object.prototype.hasOwnProperty.call(conflict, "private_path") && !conflict.message.includes("must/not/expose"), "arbitrary backend internals must not be exposed");
          const nonPackage = await H.httpError("/review-decisions/1", response(422, { detail: "existing non-package detail" }));
          assert(nonPackage.message.includes("existing non-package detail"), "non-package string detail behavior must remain compatible");
          global.fetch = async () => { throw new TypeError("PRIVATE_NETWORK_DETAIL"); };
          try {
            await H.write(packageUrl + "/approval", { method: "PUT", headers: { Accept: "application/json" }, body: '{}' });
            throw new Error("network write should fail");
          } catch (error) {
            assert(error.message === "Could not reach the Career WatchPost API. Check that it is running, then retry.", "network errors must remain generic");
          }
          global.fetch = async () => emptyResponse(204);
          const result = await H.write(packageUrl, { method: "DELETE", headers: { Accept: "application/json" }, body: '{"confirm":true}' });
          assert(result === null, "204 No Content must succeed without JSON parsing");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_open_draft_conflict_refreshes_both_authorities_without_replay(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          let preparationGets = 0, packageGets = 0, draftPuts = 0;
          const history = [{ version_id: "c".repeat(32), version_number: 9, revision: 2, preparation_revision: 8, finalized_at: "2026-09-02T00:00:00+00:00" }];
          global.fetch = async (url, options = {}) => {
            if (url === "/applications/1/preparation") {
              preparationGets += 1;
              return response(200, preparationGets === 1 ? preparation(7) : preparation(8, "user_confirmed_ready"));
            }
            if (options.method === "PUT" && url === "/applications/1/package/draft") {
              draftPuts += 1;
              assert(options.headers["X-CareerOS-Preparation-Revision"] === "7", "draft open must use the initial live Preparation revision");
              return response(409, { detail: { code: "preparation_revision_conflict", current_revision: 8 } });
            }
            if (url.startsWith("/applications/1/package?")) {
              packageGets += 1;
              return response(200, summary(null, history, 1));
            }
            throw new Error("unexpected request " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: summary(null) }, () => {});
          document.getElementById("package-open-draft").dispatch("click");
          await tick(); await tick(); await tick(); await tick();
          assert(draftPuts === 1, "Preparation conflict must not automatically replay draft opening");
          assert(preparationGets === 2, "Preparation authority must be read once initially and once after conflict");
          assert(packageGets === 1, "package authority must refresh after the conflict");
          assert(preparationBody.textContent.toLowerCase().includes("user confirmed ready"), "refreshed Preparation authority must be visible");
          assert(packageBody.textContent.includes("Version 9"), "refreshed package authority must be visible");
          assert(packageBody.textContent.includes("use a new explicit Open action"), "conflict must require a fresh explicit open action");
          const conflictRetry = packageBody.querySelector(".error-box").querySelector(".retry-button");
          conflictRetry.dispatch("click");
          assert(draftPuts === 1, "conflict Retry must not issue a draft-open write");
          assert(document.activeElement === document.getElementById("package-open-draft"), "conflict Retry must focus the fresh Open action");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_stale_open_draft_conflict_cannot_render_refreshed_authority(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          const delayedPreparation = deferred();
          let preparationGets = 0, packageGets = 0, draftPuts = 0;
          global.fetch = async (url, options = {}) => {
            if (url === "/applications/1/preparation") {
              preparationGets += 1;
              if (preparationGets === 1) return response(200, preparation(7));
              return delayedPreparation.promise;
            }
            if (options.method === "PUT" && url === "/applications/1/package/draft") {
              draftPuts += 1;
              return response(409, { detail: { code: "preparation_revision_conflict", current_revision: 8 } });
            }
            if (url.startsWith("/applications/1/package?")) { packageGets += 1; return response(200, summary(null)); }
            throw new Error("unexpected request " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: summary(null) }, () => {});
          document.getElementById("package-open-draft").dispatch("click");
          await tick(); await tick();
          H.invalidate(); window.location.hash = "#job/2";
          const newerFocus = append(detailContent, "button", "newer-conflict-focus", ""); newerFocus.focus();
          delayedPreparation.resolve(response(200, preparation(8, "user_confirmed_ready")));
          await tick(); await tick(); await tick();
          assert(draftPuts === 1, "stale conflict must never replay the draft-open write");
          assert(preparationGets === 2, "the already-started authoritative Preparation refresh may settle");
          assert(packageGets === 0, "a stale operation must not start a package refresh for the newer view");
          assert(preparationBody.textContent === "" && packageSection.textContent === "", "stale authority must not render into the newer view");
          assert(document.activeElement === newerFocus, "stale conflict must not steal focus from the newer view");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_answer_add_edit_confirm_unconfirm_and_delete_requests(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          let revision = 0;
          let answer = null;
          const writes = [];
          function authoritative() {
            return detail(revision, {
              answers: answer ? [answer] : [],
              approval: { status: revision > 0 ? "stale" : "not_approved", approved_revision: revision > 0 ? 0 : null },
              completeness: { category: answer && answer.confirmed ? "complete" : "incomplete", blocking_count: answer && answer.confirmed ? 0 : 1, reasons: [{ code: answer ? "answer_unconfirmed" : "answer_missing", blocking: true, message: "Server reason." }] },
            });
          }
          global.fetch = async (url, options = {}) => {
            if (options.method) {
              writes.push({ url, options });
              assert(options.headers["X-CareerOS-Package-Revision"] === String(revision), "answer mutation must carry current package revision");
              assert(options.headers["X-CareerOS-Preparation-Revision"] === "7", "answer mutation must carry independent Preparation revision");
              if (url.endsWith("/answers/question-1") && options.method === "PUT") {
                answer = { requirement_id: "question-1", text: JSON.parse(options.body).text, confirmed: false };
              } else if (url.endsWith("/confirmation") && options.method === "PUT") {
                assert(options.body === '{"confirmed":true}', "confirmation body must be exact"); answer.confirmed = true;
              } else if (url.endsWith("/confirmation") && options.method === "DELETE") {
                assert(options.body === "{}", "unconfirmation body must be empty JSON"); answer.confirmed = false;
              } else if (url.endsWith("/answers/question-1") && options.method === "DELETE") {
                assert(options.body === "{}", "answer deletion body must be empty JSON"); answer = null;
              } else throw new Error("unexpected write " + options.method + " " + url);
              revision += 1;
              return response(200, authoritative());
            }
            if (url.startsWith("/applications/1/package?")) return response(200, summary(draftSummary(revision)));
            if (url === "/application-package-versions/" + "b".repeat(32)) return response(200, authoritative());
            throw new Error("unexpected read " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: summary(draftSummary()) }, () => {});
          await tick(); await tick();
          let input = document.getElementById("package-answer-" + "b".repeat(32) + "-question-1");
          input.value = "FIRST_PRIVATE_ANSWER"; input.parentNode.dispatch("submit");
          await tick(); await tick(); await tick();
          assert(JSON.parse(writes[0].options.body).text === "FIRST_PRIVATE_ANSWER", "answer add body must be exact");
          assert(packageBody.textContent.includes("Stale after a package change"), "semantic answer mutation must show approval as stale from server state");
          input = document.getElementById("package-answer-" + "b".repeat(32) + "-question-1");
          input.value = "EDITED_PRIVATE_ANSWER"; input.parentNode.dispatch("submit");
          await tick(); await tick(); await tick();
          assert(JSON.parse(writes[1].options.body).text === "EDITED_PRIVATE_ANSWER", "answer edit must use the same authoritative PUT contract");
          packageBody.querySelector(".package-answer-actions").querySelectorAll("button")[0].dispatch("click");
          await tick(); await tick(); await tick();
          packageBody.querySelector(".package-answer-actions").querySelectorAll("button")[0].dispatch("click");
          await tick(); await tick(); await tick();
          packageBody.querySelector(".package-answer-actions").querySelectorAll("button")[1].dispatch("click");
          await tick(); await tick(); await tick();
          assert(writes.map((item) => item.options.method).join(",") === "PUT,PUT,PUT,DELETE,DELETE", "answer lifecycle methods must be exact");
          assert(packageBody.textContent.includes("Add private answer"), "authoritative deletion refresh must restore the empty answer form");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_delete_conflict_keeps_authoritative_history(tmp_path: Path) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          const frozen = detail(6, { state: "finalized", finalized_at: "2026-09-01T00:00:00+00:00", completeness: { category: "complete", blocking_count: 0, reasons: [{ code: "package_complete", blocking: false, message: "Package is complete." }] } });
          const history = [{ version_id: "b".repeat(32), version_number: 1, revision: 6, preparation_revision: 7, finalized_at: "2026-09-01T00:00:00+00:00" }];
          let deletes = 0;
          global.fetch = async (url, options = {}) => {
            if (options.method === "DELETE") { deletes += 1; return response(409, { detail: "package_version_referenced" }); }
            if (url.startsWith("/applications/1/package?")) return response(200, summary(null, history, 1));
            if (url === "/application-package-versions/" + "b".repeat(32)) return response(200, frozen);
            throw new Error("unexpected request " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: summary(null, history, 1) }, () => {});
          packageBody.querySelector(".package-history-select").dispatch("click"); await tick(); await tick();
          packageBody.querySelector(".package-destructive-action").dispatch("click"); await tick(); await tick(); await tick();
          assert(deletes === 1, "reference conflict must not retry deletion");
          assert(packageBody.textContent.includes("Version 1"), "deletion conflict must retain authoritative history");
          assert(packageBody.textContent.includes("package_version_referenced"), "privacy-safe downstream reference code must be shown");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_incomplete_after_material_mutation_keeps_finalize_disabled(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          const materialVersion = "d".repeat(32);
          const requirements = [{ requirement_id: "material-1", kind: "material", label: "Exact document", required: true, fulfillment_state: "satisfied", confirmed: true }];
          const selection = [{ requirement_id: "material-1", material_version_id: materialVersion, material_name: "Fictional Resume", material_category: "resume", version_number: 4, display_filename: "fictional.pdf", byte_size: 42, current_availability: "available" }];
          let server = detail(2, {
            requirements,
            material_selections: selection,
            approval: { status: "current", approved_revision: 2 },
            completeness: { category: "complete", blocking_count: 0, reasons: [{ code: "package_complete", blocking: false, message: "Package is complete." }] },
          });
          let serverSummary = summary(draftSummary(2));
          global.fetch = async (url, options = {}) => {
            if (options.method === "DELETE" && url.endsWith("/material-selections/material-1")) {
              server = detail(3, {
                requirements,
                material_selections: [],
                approval: { status: "not_approved", approved_revision: null },
                completeness: { category: "incomplete", blocking_count: 1, reasons: [{ code: "material_selection_missing", blocking: true, message: "Select a Material." }] },
              });
              serverSummary = summary(draftSummary(3));
              return response(200, server);
            }
            if (url.startsWith("/applications/1/package?")) return response(200, serverSummary);
            if (url === "/application-package-versions/" + "b".repeat(32)) return response(200, server);
            throw new Error("unexpected request " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: serverSummary }, () => {});
          await tick(); await tick();
          let finalize = packageBody.querySelector(".package-lifecycle-actions").querySelectorAll("button")[1];
          assert(!finalize.disabled, "the initially complete and approved package must be finalizable");
          packageBody.querySelector(".package-material-actions").querySelectorAll("button")[1].dispatch("click");
          await tick(); await tick(); await tick(); await tick();
          assert(H.state().operation === null, "the Material operation cleanup must have completed");
          assert(packageBody.textContent.includes("Status: Incomplete · Blocking: 1"), "the authoritative incomplete state must render");
          assert(packageBody.textContent.includes("Not approved"), "the authoritative approval state must render");
          finalize = packageBody.querySelector(".package-lifecycle-actions").querySelectorAll("button")[1];
          assert(finalize.disabled, "Finalize must remain disabled after Material operation cleanup");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_stale_approval_after_revision_mutation_keeps_finalize_disabled(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          const answer = { requirement_id: "question-1", text: "Initial answer", confirmed: true };
          let server = detail(4, {
            answers: [answer],
            approval: { status: "current", approved_revision: 4 },
            completeness: { category: "complete", blocking_count: 0, reasons: [{ code: "package_complete", blocking: false, message: "Package is complete." }] },
          });
          let serverSummary = summary(draftSummary(4));
          global.fetch = async (url, options = {}) => {
            if (options.method === "PUT" && url.endsWith("/answers/question-1")) {
              server = detail(5, {
                answers: [{ requirement_id: "question-1", text: JSON.parse(options.body).text, confirmed: false }],
                approval: { status: "stale", approved_revision: 4 },
                completeness: { category: "complete", blocking_count: 0, reasons: [{ code: "package_complete", blocking: false, message: "Package is complete." }] },
              });
              serverSummary = summary(draftSummary(5));
              return response(200, server);
            }
            if (url.startsWith("/applications/1/package?")) return response(200, serverSummary);
            if (url === "/application-package-versions/" + "b".repeat(32)) return response(200, server);
            throw new Error("unexpected request " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: serverSummary }, () => {});
          await tick(); await tick();
          let finalize = packageBody.querySelector(".package-lifecycle-actions").querySelectorAll("button")[1];
          assert(!finalize.disabled, "the initially current approval must permit Finalize");
          const input = document.getElementById("package-answer-" + "b".repeat(32) + "-question-1");
          input.value = "Revision-changing answer";
          input.parentNode.dispatch("submit");
          await tick(); await tick(); await tick(); await tick();
          assert(H.state().operation === null, "the revision-changing operation cleanup must have completed");
          assert(packageBody.textContent.includes("Stale after a package change"), "the authoritative stale approval must render");
          assert(packageBody.textContent.includes("Status: Complete · Blocking: 0"), "approval staleness must be isolated from completeness in this fixture");
          finalize = packageBody.querySelector(".package-lifecycle-actions").querySelectorAll("button")[1];
          assert(finalize.disabled, "Finalize must remain disabled after stale-approval cleanup");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_rebase_stale_state_preserves_finalize_and_adjacent_control_semantics(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          const materialVersion = "d".repeat(32);
          const requirements = [
            { requirement_id: "question-1", kind: "question", label: "Private question", detail: null, required: true, fulfillment_state: "satisfied", confirmed: true },
            { requirement_id: "material-1", kind: "material", label: "Exact document", detail: null, required: true, fulfillment_state: "satisfied", confirmed: true },
          ];
          const materialSelections = [{ requirement_id: "material-1", material_version_id: materialVersion, material_name: "Fictional Resume", material_category: "resume", version_number: 4, display_filename: "fictional.pdf", byte_size: 42, current_availability: "available" }];
          const answers = [{ requirement_id: "question-1", text: "Private answer", confirmed: true }];
          let server = detail(5, {
            requirements,
            material_selections: materialSelections,
            answers,
            preparation_stale: true,
            approval: { status: "current", approved_revision: 5 },
            completeness: { category: "stale", blocking_count: 1, reasons: [{ code: "package_preparation_stale", blocking: true, message: "Rebase required." }] },
          });
          let serverSummary = summary(Object.assign(draftSummary(5), { preparation_stale: true, approval_status: "current" }));
          global.fetch = async (url, options = {}) => {
            if (url === "/applications/1/preparation") return response(200, preparation(8, "stale"));
            if (options.method === "PUT" && url.endsWith("/rebase")) {
              server = detail(6, {
                requirements,
                material_selections: materialSelections,
                answers,
                preparation_revision: 8,
                job_reconfirmation_required: true,
                approval: { status: "not_approved", approved_revision: null },
                completeness: { category: "stale", blocking_count: 4, reasons: [{ code: "package_job_reconfirmation_required", blocking: true, message: "Reconfirmation required." }] },
              });
              serverSummary = summary(Object.assign(draftSummary(6), { preparation_revision: 8, job_reconfirmation_required: true }));
              return response(200, server);
            }
            if (url.startsWith("/applications/1/package?")) return response(200, serverSummary);
            if (url === "/application-package-versions/" + "b".repeat(32)) return response(200, server);
            throw new Error("unexpected request " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: serverSummary }, () => {});
          await tick(); await tick();
          const rebase = packageBody.querySelector(".package-authority-state").querySelector("button");
          assert(rebase && !rebase.disabled, "authoritative stale state must leave explicit Rebase available");
          assert(!packageBody.querySelector(".package-answer-form").querySelector("textarea").disabled, "answer editing must remain available");
          assert(!packageBody.querySelector(".package-answer-form").querySelector("button").disabled, "answer saving must remain available");
          packageBody.querySelector(".package-material-actions").querySelectorAll("button").forEach((button) => assert(!button.disabled, "Material controls must remain available"));
          const initialLifecycle = packageBody.querySelector(".package-lifecycle-actions").querySelectorAll("button");
          assert(!initialLifecycle[0].disabled && initialLifecycle[1].disabled && !initialLifecycle[2].disabled, "approval and delete must remain available while semantic state disables only Finalize");
          rebase.dispatch("click");
          await tick(); await tick(); await tick(); await tick(); await tick();
          assert(H.state().operation === null, "the rebase operation cleanup must have completed");
          assert(packageBody.textContent.includes("Status: Stale · Blocking: 4"), "the authoritative stale rebase result must render");
          assert(packageBody.textContent.includes("Job reconfirmation required"), "the authoritative reconfirmation requirement must render");
          assert(packageBody.textContent.includes("Not approved"), "the authoritative post-rebase approval state must render");
          const lifecycle = packageBody.querySelector(".package-lifecycle-actions").querySelectorAll("button");
          assert(lifecycle[1].disabled, "Finalize must remain disabled after rebase cleanup");
          assert(!lifecycle[0].disabled && !lifecycle[2].disabled, "approval and delete must remain available after rebase cleanup");
          assert(!packageBody.querySelector(".package-answer-form").querySelector("textarea").disabled, "answer editing must be restored after rebase cleanup");
          packageBody.querySelector(".package-material-actions").querySelectorAll("button").forEach((button) => assert(!button.disabled, "Material controls must be restored after rebase cleanup"));
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_finalizable_semantics_survive_transient_operation_cleanup(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          const ready = detail(4, {
            answers: [{ requirement_id: "question-1", text: "Approved answer", confirmed: true }],
            approval: { status: "current", approved_revision: 4 },
            completeness: { category: "complete", blocking_count: 0, reasons: [{ code: "package_complete", blocking: false, message: "Package is complete." }] },
          });
          const write = deferred();
          global.fetch = async (url, options = {}) => {
            if (options.method === "PUT" && url.endsWith("/answers/question-1")) return write.promise;
            if (url === "/application-package-versions/" + "b".repeat(32)) return response(200, ready);
            throw new Error("unexpected request " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: summary(draftSummary(4)) }, () => {});
          await tick(); await tick();
          const finalize = packageBody.querySelector(".package-lifecycle-actions").querySelectorAll("button")[1];
          assert(!finalize.disabled, "an authoritative complete, non-stale, current-approved draft must enable Finalize");
          const input = document.getElementById("package-answer-" + "b".repeat(32) + "-question-1");
          input.value = "Attempted edit";
          input.parentNode.dispatch("submit");
          await tick();
          assert(H.state().operation !== null, "the package operation must remain active while its write is pending");
          assert(finalize.disabled, "an active package operation must interlock Finalize");
          write.resolve(response(500, { detail: "synthetic_package_write_failure" }));
          await tick(); await tick(); await tick();
          assert(H.state().operation === null, "the failed operation cleanup must complete");
          assert(!finalize.disabled, "cleanup must restore Finalize to the authoritative semantic state");
          assert(!input.disabled, "cleanup must also restore an otherwise available answer editor");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_exact_material_finalization_reconciliation_and_204_delete(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          const materialId = "c".repeat(32), materialVersion = "d".repeat(32);
          let server = detail(2, {
            requirements: [{ requirement_id: "material-1", kind: "material", label: "Exact document", required: true, fulfillment_state: "satisfied", confirmed: true }],
            completeness: { category: "incomplete", blocking_count: 1, reasons: [{ code: "material_selection_missing", blocking: true, message: "Select a Material." }] },
          });
          let serverSummary = summary(draftSummary(2));
          const requests = [];
          global.fetch = async (url, options = {}) => {
            requests.push({ url, options });
            if (url === "/materials?offset=0&limit=20&include_retired=false") return response(200, { items: [{ material_id: materialId, name: "Fictional Resume", category: "resume", status: "active", version_count: 1 }], total: 1, offset: 0, limit: 20 });
            if (url === "/materials/" + materialId) return response(200, { material_id: materialId, name: "Fictional Resume", category: "resume", status: "active", revision: 0, version_count: 1, versions: [{ version_id: materialVersion, version_number: 4, display_filename: "fictional.pdf", declared_media_type: "application/pdf", byte_size: 42, created_at: "2026-09-01T00:00:00+00:00", is_current: true }] });
            if (options.method === "PUT" && url.includes("/material-selections/material-1")) {
              server = detail(3, { requirements: server.requirements, material_selections: [{ requirement_id: "material-1", material_version_id: materialVersion, material_name: "Fictional Resume", material_category: "resume", version_number: 4, display_filename: "fictional.pdf", byte_size: 42, current_availability: "available" }], completeness: { category: "complete", blocking_count: 0, reasons: [{ code: "package_complete", blocking: false, message: "Package is complete." }] }, approval: { status: "current", approved_revision: 3 } });
              serverSummary = summary(draftSummary(3)); return response(200, server);
            }
            if (url.startsWith("/applications/1/package?")) return response(200, serverSummary);
            if (url === "/application-package-versions/" + "b".repeat(32)) return response(200, server);
            throw new Error("unexpected request " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: serverSummary }, () => {});
          await tick(); await tick();
          packageBody.querySelector(".package-material-actions").querySelector("button").dispatch("click");
          await tick();
          packageBody.querySelector(".material-select").dispatch("click");
          await tick();
          const use = packageBody.querySelector(".package-chooser-detail").querySelector("button");
          use.dispatch("click"); await tick(); await tick(); await tick();
          const selection = requests.find((item) => item.url.includes("/material-selections/material-1") && item.options.method === "PUT");
          assert(JSON.parse(selection.options.body).material_version_id === materialVersion, "chooser must submit the exact immutable version id");
          assert(selection.options.headers["X-CareerOS-Package-Revision"] === "2", "selection must carry package CAS");
          assert(selection.options.headers["X-CareerOS-Preparation-Revision"] === "7", "selection must carry independent Preparation CAS");

          let finalizePuts = 0;
          const finalized = detail(3, { state: "finalized", requirements: server.requirements, material_selections: server.material_selections, answers: [], approval: { status: "current", approved_revision: 3 }, completeness: { category: "complete", blocking_count: 0, reasons: [{ code: "package_complete", blocking: false, message: "Package is complete." }] }, finalized_at: "2026-09-02T00:00:00+00:00" });
          const history = [{ version_id: "b".repeat(32), version_number: 1, revision: 3, preparation_revision: 7, approved_at: "2026-09-02T00:00:00+00:00", finalized_at: "2026-09-02T00:00:00+00:00" }];
          global.fetch = async (url, options = {}) => {
            requests.push({ url, options });
            if (options.method === "PUT" && url.endsWith("/finalization")) { finalizePuts += 1; throw new TypeError("ambiguous network result"); }
            if (url === "/application-package-versions/" + "b".repeat(32)) return response(200, finalized);
            if (url.startsWith("/applications/1/package?")) return response(200, summary(null, history, 1));
            if (options.method === "DELETE" && url === "/application-package-versions/" + "b".repeat(32)) return emptyResponse(204);
            throw new Error("unexpected request " + url);
          };
          packageBody.querySelector(".package-lifecycle-actions").querySelectorAll("button")[1].dispatch("click");
          await tick(); await tick(); await tick(); await tick();
          assert(finalizePuts === 1, "ambiguous finalization must never automatically PUT again");
          assert(packageBody.textContent.includes("Finalized version 1"), "authoritative GET reconciliation must present finalized success");
          const deleteButton = packageBody.querySelector(".package-selected-history").querySelector(".package-destructive-action");
          serverSummary = summary(null);
          deleteButton.dispatch("click"); await tick(); await tick(); await tick();
          const deletion = requests.find((item) => item.options.method === "DELETE" && item.url === "/application-package-versions/" + "b".repeat(32));
          assert(deletion.options.headers["X-CareerOS-Package-Revision"] === "3", "deletion must carry package CAS");
          assert(!Object.prototype.hasOwnProperty.call(deletion.options.headers, "X-CareerOS-Preparation-Revision"), "deletion must not merge Preparation authority");
          assert(deletion.options.body === '{"confirm":true}', "deletion confirmation body must be exact");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_chooser_stale_responses_cannot_overwrite_newer_state(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          const packageData = detail(2, {
            requirements: [{ requirement_id: "material-1", kind: "material", label: "Exact document", required: true, fulfillment_state: "open", confirmed: true }],
          });
          const requirement = packageData.requirements[0];
          const materialA = "c".repeat(32), materialB = "d".repeat(32);
          const versionA = "e".repeat(32), versionB = "f".repeat(32);
          function listed(materialId, name) { return { material_id: materialId, name, category: "resume", status: "active", version_count: 1 }; }
          function listing(items, total, offset) { return { items, total, offset, limit: 20 }; }
          function material(materialId, versionId, name) { return { material_id: materialId, name, category: "resume", status: "active", revision: 0, version_count: 1, versions: [{ version_id: versionId, version_number: 1, display_filename: name + ".pdf", declared_media_type: "application/pdf", byte_size: 42, created_at: "2026-09-01T00:00:00+00:00", is_current: true }] }; }
          packageBody.textContent = "";
          const row = append(packageBody, "div", "", "package-requirement-item");
          const region = append(row, "div", "", "package-material-chooser");

          const olderPage = deferred(), newerPage = deferred();
          global.fetch = (url) => {
            if (url.includes("offset=0")) return Promise.resolve(response(200, listing([listed(materialA, "Initial page")], 60, 0)));
            if (url.includes("offset=20")) return olderPage.promise;
            if (url.includes("offset=40")) return newerPage.promise;
            throw new Error("unexpected request " + url);
          };
          H.chooser(row, packageBody, job, packageData, requirement);
          await tick(); await tick();
          const next = region.querySelector(".materials-pagination").querySelectorAll("button")[1];
          next.dispatch("click");
          next.dispatch("click");
          newerPage.resolve(response(200, listing([listed(materialA, "Newest page A"), listed(materialB, "Newest page B")], 60, 40)));
          await tick(); await tick();
          assert(region.textContent.includes("Newest page B"), "the newest rapid-page response must render");
          olderPage.resolve(response(200, listing([listed(materialA, "STALE PAGE MUST NOT RENDER")], 60, 20)));
          await tick(); await tick();
          assert(region.textContent.includes("Newest page B") && !region.textContent.includes("STALE PAGE MUST NOT RENDER"), "an older page response must not overwrite the newer page");

          const olderDetail = deferred();
          global.fetch = (url) => {
            if (url === "/materials/" + materialA) return olderDetail.promise;
            if (url === "/materials/" + materialB) return Promise.resolve(response(200, material(materialB, versionB, "Newest detail")));
            throw new Error("unexpected request " + url);
          };
          region.querySelectorAll(".material-select")[0].dispatch("click");
          region.querySelectorAll(".material-select")[1].dispatch("click");
          await tick(); await tick();
          assert(region.textContent.includes("Newest detail versions"), "the newer Material selection must render its exact versions");
          olderDetail.resolve(response(200, material(materialA, versionA, "STALE DETAIL MUST NOT RENDER")));
          await tick(); await tick();
          assert(region.textContent.includes("Newest detail versions") && !region.textContent.includes("STALE DETAIL MUST NOT RENDER"), "an older Material detail must not overwrite the newer selection");

          const closedDetail = deferred();
          global.fetch = (url) => url === "/materials/" + materialA ? closedDetail.promise : Promise.reject(new Error("unexpected request " + url));
          region.querySelectorAll(".material-select")[0].dispatch("click");
          region.querySelector(".package-secondary-action").dispatch("click");
          assert(H.state().chooser === null && region.textContent === "", "closing the chooser must invalidate its state immediately");
          closedDetail.resolve(response(200, material(materialA, versionA, "CLOSED DETAIL MUST NOT RENDER")));
          await tick(); await tick();
          assert(region.textContent === "", "a response for a closed chooser must not repopulate it");

          const replacedList = deferred();
          let chooserListReads = 0;
          global.fetch = (url) => {
            if (!url.startsWith("/materials?")) throw new Error("unexpected request " + url);
            chooserListReads += 1;
            if (chooserListReads === 1) return replacedList.promise;
            return Promise.resolve(response(200, listing([listed(materialB, "Replacement chooser")], 1, 0)));
          };
          H.chooser(row, packageBody, job, packageData, requirement);
          H.chooser(row, packageBody, job, packageData, requirement);
          await tick(); await tick();
          assert(region.textContent.includes("Replacement chooser"), "the replacement chooser must own the region");
          replacedList.resolve(response(200, listing([listed(materialA, "REPLACED LIST MUST NOT RENDER")], 1, 0)));
          await tick(); await tick();
          assert(region.textContent.includes("Replacement chooser") && !region.textContent.includes("REPLACED LIST MUST NOT RENDER"), "a replaced chooser response must not overwrite its successor");

          const operationDetail = deferred();
          global.fetch = (url) => {
            if (url === "/materials/" + materialB) return operationDetail.promise;
            if (url.startsWith("/applications/1/package?")) return Promise.resolve(response(200, summary(draftSummary(2))));
            if (url === "/application-package-versions/" + "b".repeat(32)) return Promise.resolve(response(200, packageData));
            throw new Error("unexpected request " + url);
          };
          region.querySelector(".material-select").dispatch("click");
          const refresh = H.summaryPage(packageBody, job, 0, null);
          await refresh; await tick();
          assert(H.state().chooser === null && packageBody.textContent.includes("Draft version 1"), "an authoritative package refresh must replace chooser state");
          operationDetail.resolve(response(200, material(materialB, versionB, "OPERATION-STALE DETAIL MUST NOT RENDER")));
          await tick(); await tick();
          assert(!packageBody.textContent.includes("OPERATION-STALE DETAIL MUST NOT RENDER"), "an operation-invalidated chooser response must not alter refreshed package state");

          const navigationList = deferred();
          global.fetch = (url) => url.startsWith("/materials?") ? navigationList.promise : Promise.reject(new Error("unexpected request " + url));
          const navigationRow = packageBody.querySelector(".package-requirement");
          H.chooser(navigationRow, packageBody, job, packageData, requirement);
          H.invalidate(); window.location.hash = "#job/2";
          navigationList.resolve(response(200, listing([listed(materialA, "NAVIGATION-STALE LIST MUST NOT RENDER")], 1, 0)));
          await tick(); await tick();
          assert(H.state().chooser === null && packageSection.textContent === "", "navigation invalidation must keep late chooser data out of the new view");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_unresolved_finalization_requires_new_explicit_action(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          const ready = detail(4, { approval: { status: "current", approved_revision: 4 }, completeness: { category: "complete", blocking_count: 0, reasons: [{ code: "package_complete", blocking: false, message: "Package is complete." }] } });
          let putCount = 0;
          global.fetch = async (url, options = {}) => {
            if (options.method === "PUT" && url.endsWith("/finalization")) { putCount += 1; throw new TypeError("connection reset"); }
            if (url === "/application-package-versions/" + "b".repeat(32)) return response(200, ready);
            if (url.startsWith("/applications/1/package?")) return response(200, summary(draftSummary(4)));
            throw new Error("unexpected request " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: summary(draftSummary(4)) }, () => {});
          await tick(); await tick();
          packageBody.querySelector(".package-lifecycle-actions").querySelectorAll("button")[1].dispatch("click");
          await tick(); await tick(); await tick(); await tick();
          assert(putCount === 1, "unresolved finalization must not be replayed");
          assert(packageBody.textContent.includes("target remains a draft"), "draft reconciliation must explain unresolved outcome");
          assert(packageBody.textContent.includes("Finalize package"), "a fresh explicit Finalize action must remain available");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_failed_finalization_reconciliation_interlocks_put_and_retries_get_only(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          const ready = detail(4, { approval: { status: "current", approved_revision: 4 }, completeness: { category: "complete", blocking_count: 0, reasons: [{ code: "package_complete", blocking: false, message: "Package is complete." }] } });
          const finalized = detail(4, { state: "finalized", approval: ready.approval, completeness: ready.completeness, finalized_at: "2026-09-02T00:00:00+00:00" });
          const history = [{ version_id: "b".repeat(32), version_number: 1, revision: 4, preparation_revision: 7, finalized_at: "2026-09-02T00:00:00+00:00" }];
          let putCount = 0, reconciliationGets = 0, finalizationStarted = false;
          global.fetch = async (url, options = {}) => {
            if (options.method === "PUT" && url.endsWith("/finalization")) { putCount += 1; finalizationStarted = true; throw new TypeError("ambiguous finalization"); }
            if (url === "/application-package-versions/" + "b".repeat(32)) {
              if (!finalizationStarted) return response(200, ready);
              if (H.state().reconcile !== null) reconciliationGets += 1;
              if (reconciliationGets <= 2) throw new TypeError("status unavailable");
              return response(200, finalized);
            }
            if (url.startsWith("/applications/1/package?")) return response(200, summary(null, history, 1));
            throw new Error("unexpected request " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: summary(draftSummary(4)) }, () => {});
          await tick(); await tick();
          const finalize = packageBody.querySelector(".package-lifecycle-actions").querySelectorAll("button")[1];
          finalize.dispatch("click");
          await tick(); await tick(); await tick();
          assert(putCount === 1, "the ambiguous finalization must issue exactly one PUT");
          assert(reconciliationGets === 1, "the first authoritative reconciliation GET must be attempted");
          assert(H.state().reconcile !== null, "failed status GET must retain unresolved finalization state");
          assert(finalize.disabled, "Finalize must remain visibly unavailable while unresolved");
          finalize.dispatch("click");
          H.finalize(packageBody, job, ready);
          await tick();
          assert(putCount === 1, "clicking or directly attempting Finalize while unresolved must not issue a second PUT");
          const retry = packageBody.querySelector(".package-reconciliation-retry");
          assert(retry && !retry.disabled, "the unresolved outcome must expose one enabled reconciliation Retry");
          retry.dispatch("click");
          await tick(); await tick(); await tick();
          assert(reconciliationGets === 2, "Retry must perform another authoritative GET");
          assert(H.state().reconcile !== null, "a repeated failed status GET must retain unresolved finalization state");
          assert(finalize.disabled, "Finalize must remain disabled after a repeated failed status GET");
          assert(putCount === 1, "Retry must never repeat the finalization PUT");
          const repeatedRetry = packageBody.querySelector(".package-reconciliation-retry");
          assert(repeatedRetry && !repeatedRetry.disabled, "a repeated failed status GET must keep GET-only Retry available");
          repeatedRetry.dispatch("click");
          await tick(); await tick(); await tick(); await tick();
          assert(reconciliationGets === 3, "a later Retry must perform only another authoritative GET");
          assert(putCount === 1, "repeated Retry must never repeat the finalization PUT");
          assert(H.state().reconcile === null, "authoritative finalized status must resolve the interlock");
          assert(packageBody.textContent.includes("Finalized version 1"), "Retry GET must render authoritative finalized success");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_node_stale_reconciliation_attempt_cannot_clear_newer_busy_owner(
    tmp_path: Path,
) -> None:
    run_node_package(
        tmp_path,
        r"""
        async function main() {
          const job = { id: 1 };
          const ready = detail(4, { approval: { status: "current", approved_revision: 4 }, completeness: { category: "complete", blocking_count: 0, reasons: [{ code: "package_complete", blocking: false, message: "Package is complete." }] } });
          const first = deferred(), second = deferred();
          let putCount = 0, reconciliationGets = 0, finalizationStarted = false;
          global.fetch = async (url, options = {}) => {
            if (options.method === "PUT" && url.endsWith("/finalization")) { putCount += 1; finalizationStarted = true; throw new TypeError("ambiguous finalization"); }
            if (url === "/application-package-versions/" + "b".repeat(32)) {
              if (!finalizationStarted) return response(200, ready);
              reconciliationGets += 1;
              return reconciliationGets === 1 ? first.promise : second.promise;
            }
            throw new Error("unexpected request " + url);
          };
          H.render(packageBody, job, { status: "fulfilled", value: summary(draftSummary(4)) }, () => {});
          await tick(); await tick();
          packageBody.querySelector(".package-lifecycle-actions").querySelectorAll("button")[1].dispatch("click");
          await tick(); await tick();
          const target = H.state().reconcile;
          assert(target && reconciliationGets === 1, "first reconciliation attempt must be pending");
          const newerAttempt = H.reconcileStatus(packageBody, job, target);
          await tick();
          assert(reconciliationGets === 2 && packageSection.getAttribute("aria-busy") === "true", "newer reconciliation must own busy state");
          first.resolve(response(200, ready));
          await tick(); await tick();
          assert(putCount === 1, "stale status completion must not issue a write");
          assert(H.state().reconcile === target, "stale status completion must not clear newer unresolved state");
          assert(H.state().reconcileBusy !== null && packageSection.getAttribute("aria-busy") === "true", "stale cleanup must not clear newer reconciliation busy ownership");
          second.reject(new TypeError("newer status unavailable"));
          await newerAttempt; await tick();
          assert(H.state().reconcile === target, "failed newer status check must remain unresolved");
          assert(H.state().reconcileBusy === null && !packageSection.hasAttribute("aria-busy"), "the owning newer attempt alone may clear its busy state");
          assert(packageBody.querySelector(".package-reconciliation-retry"), "newer failed attempt must retain GET-only Retry");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )
