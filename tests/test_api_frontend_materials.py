# ruff: noqa: E501
"""Focused contract tests for the zero-build Materials workspace."""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from urllib.parse import quote

from careeros.api.main import create_app
from careeros.database.connection import connect
from careeros.database.schema import initialize_schema
from tests.support import TestClient

STATIC = Path(__file__).parents[1] / "src/careeros/api/static"


NODE_HARNESS = r"""
"use strict";
const fs = require("fs");

class FakeElement {
  constructor(tagName, ownerDocument) {
    this.tagName = String(tagName).toUpperCase();
    this.ownerDocument = ownerDocument;
    this.parentNode = null;
    this.children = [];
    this.attributes = {};
    this.listeners = {};
    this.hidden = false;
    this.disabled = false;
    this.required = false;
    this.value = "";
    this.files = [];
    this.className = "";
    this._id = "";
    this._text = "";
  }
  set id(value) { this._id = String(value); if (this._id) this.ownerDocument.ids[this._id] = this; }
  get id() { return this._id; }
  set textContent(value) {
    this._text = String(value);
    this.children.forEach((child) => { child.parentNode = null; });
    this.children = [];
  }
  get textContent() { return this._text + this.children.map((child) => child.textContent).join(""); }
  get childNodes() { return this.children; }
  get isConnected() { return this === this.ownerDocument.body || Boolean(this.parentNode && this.parentNode.isConnected); }
  appendChild(child) { child.parentNode = this; this.children.push(child); return child; }
  insertBefore(child, reference) {
    child.parentNode = this;
    const index = reference ? this.children.indexOf(reference) : -1;
    if (index < 0) this.children.push(child); else this.children.splice(index, 0, child);
    return child;
  }
  remove() {
    if (!this.parentNode) return;
    const index = this.parentNode.children.indexOf(this);
    if (index >= 0) this.parentNode.children.splice(index, 1);
    this.parentNode = null;
  }
  setAttribute(name, value) { this.attributes[name] = String(value); }
  getAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name) ? this.attributes[name] : null; }
  hasAttribute(name) { return Object.prototype.hasOwnProperty.call(this.attributes, name); }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) { (this.listeners[name] ||= []).push(callback); }
  dispatch(name) { (this.listeners[name] || []).forEach((callback) => callback({ preventDefault() {}, target: this })); }
  focus() { this.ownerDocument.activeElement = this; }
  setCustomValidity(message) { this.validationMessage = message; }
  reportValidity() { return !this.validationMessage; }
  reset() {
    this.querySelectorAll("input").forEach((input) => { input.value = ""; input.files = []; });
  }
  matches(selector) {
    if (selector[0] === "#") return this.id === selector.slice(1);
    if (selector[0] === ".") return this.className.split(/\s+/).includes(selector.slice(1));
    return this.tagName.toLowerCase() === selector.toLowerCase();
  }
  querySelectorAll(selector) {
    const selectors = selector.split(",").map((part) => part.trim());
    const found = [];
    const visit = (node) => node.children.forEach((child) => {
      if (selectors.some((item) => child.matches(item))) found.push(child);
      visit(child);
    });
    visit(this);
    return found;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
}

class FakeDocument {
  constructor() { this.ids = {}; this.activeElement = null; this.body = new FakeElement("body", this); }
  createElement(tagName) { return new FakeElement(tagName, this); }
  getElementById(id) {
    const node = this.ids[id] || null;
    return node && node.isConnected ? node : null;
  }
  addEventListener() {}
  querySelectorAll(selector) {
    if (selector === ".view[data-view]") return this.body.querySelectorAll(".view");
    if (selector === ".nav a[href^='#']") return [];
    return this.body.querySelectorAll(selector);
  }
  querySelector(selector) {
    if (selector === ".view[data-view]:not([hidden])") {
      return this.querySelectorAll(".view[data-view]").find((node) => !node.hidden) || null;
    }
    return this.querySelectorAll(selector)[0] || null;
  }
}

const document = new FakeDocument();
const window = {
  document,
  location: { hash: "#review", origin: "http://careeros.test" },
  addEventListener() {},
};
global.document = document;
global.window = window;
global.URL = URL;

function append(parent, tag, id, className) {
  const node = document.createElement(tag);
  if (id) node.id = id;
  if (className) node.className = className;
  parent.appendChild(node);
  return node;
}
const mainNode = append(document.body, "main", "main", "");
const reviewView = append(mainNode, "section", "view-review", "view");
reviewView.setAttribute("data-view", "review");
append(reviewView, "div", "review-profile-summary", "");
const materialsView = append(mainNode, "section", "view-materials", "view");
materialsView.setAttribute("data-view", "materials");
materialsView.hidden = true;
append(materialsView, "div", "materials-status", "status");
append(materialsView, "div", "materials-content", "materials-workspace");

function response(status, payload) {
  return { ok: status >= 200 && status < 300, status, json: async () => payload };
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function assert(condition, message) { if (!condition) throw new Error(message); }
function tick() { return new Promise((resolve) => setTimeout(resolve, 0)); }
function detail(id, name, status = "active", revision = 1, versionCount = 1) {
  return {
    material_id: id, name, category: "resume", status, revision,
    version_count: versionCount, updated_at: "2026-08-31T00:00:00+00:00",
    versions: [{
      version_id: "b".repeat(32), version_number: versionCount, is_current: true,
      display_filename: "resume.pdf", byte_size: 10,
      declared_media_type: "application/pdf", created_at: "2026-08-31T00:00:00+00:00",
    }],
  };
}
function listing(item, offset = 0, total = 1) {
  return { items: item ? [item] : [], total, offset, limit: 20 };
}

let source = fs.readFileSync(process.argv[2], "utf8");
source = source.replace(
  '  window.addEventListener("hashchange", navigate);\n})();',
  `  window.__materialsTest = {
    navigate: navigate,
    render: renderMaterialsView,
    update: updateMaterialMetadata,
    addVersion: addMaterialVersion,
    retirement: changeMaterialRetirement,
    importMaterial: importMaterial,
    requestReconcile: requestMaterialsReconciliation,
    generation: function () { return materialsViewGeneration; },
    state: function () { return materialsState; },
    setState: function (patch) { Object.keys(patch).forEach(function (key) { materialsState[key] = patch[key]; }); },
    operation: function () { return materialsOperationActive; },
    versionDraft: function () { return materialsVersionDraft; },
    reconcileDirty: function () { return materialsReconcileDirty; },
    createBlocked: function () { return materialsCreateReconcileRequired; }
  };
  window.addEventListener("hashchange", navigate);
})();`
);
eval(source);
const H = window.__materialsTest;
"""


def run_node_materials(tmp_path: Path, scenario: str) -> None:
    script = tmp_path / "materials-harness.js"
    script.write_text(NODE_HARNESS + "\n" + textwrap.dedent(scenario), encoding="utf-8")
    result = subprocess.run(
        ["node", str(script), str(STATIC / "app.js")],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def asset(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


def materials_js() -> str:
    source = asset("app.js")
    start = source.index("// Materials workspace (#materials)")
    end = source.index("// Home view (driven by GET /report only)", start)
    return source[start:end]


def test_materials_shell_and_semantic_navigation() -> None:
    html = asset("index.html")
    source = asset("app.js")
    for marker in (
        'href="#materials"',
        'id="view-materials"',
        'data-view="materials"',
        'id="materials-status"',
        'aria-live="polite"',
        'id="materials-content"',
    ):
        assert marker in html
    assert 'var MATERIALS = "materials";' in source
    assert "if (raw === MATERIALS) return MATERIALS;" in source
    assert "await renderMaterialsView();" in source


def test_raw_file_upload_and_frozen_headers() -> None:
    source = materials_js()
    assert "FormData" not in source
    assert "ArrayBuffer" not in source
    assert "FileReader" not in source
    assert '"Content-Type": "application/octet-stream"' in source
    assert '"X-CareerOS-Display-Filename": strictMaterialHeader(file.name)' in source
    assert (
        '"X-CareerOS-Declared-Media-Type": file.type || "application/octet-stream"'
        in source
    )
    assert 'headers["X-CareerOS-Material-Revision"] = String(revision);' in source
    assert 'headers["X-CareerOS-Material-Name"] = strictMaterialHeader' in source
    assert (
        'headers["X-CareerOS-Material-Category"] = createMetadata.category;' in source
    )
    assert 'return materialWrite(url, "POST", file, headers);' in source
    assert (
        "options.headers = Object.assign({}, options.headers || {}, "
        '{ "X-CareerOS-Write": "1" });' in asset("app.js")
    )


def test_canonical_categories_and_workflows_are_present() -> None:
    source = materials_js()
    for category in (
        "resume",
        "cover_letter_base",
        "portfolio",
        "supporting_document",
        "other",
    ):
        assert f'value: "{category}"' in source
    for label in (
        "Import material",
        "Rename or recategorize",
        "Add immutable version",
        "Retire material",
        "Restore material",
        "Exact version history",
        "Download exact version ",
        "Active only",
        "Active and retired",
    ):
        assert label in source
    for endpoint in (
        '"/materials?offset="',
        '"/materials/" + id',
        '"/materials/" + id + "/versions"',
        '"/materials/" + id + "/retirement"',
        '"/material-versions/" + versionId + "/content"',
    ):
        assert endpoint in source


def test_server_authority_conflicts_and_stale_guards() -> None:
    source = materials_js()
    assert "requestMaterialsReconciliation" in source
    assert "runMaterialsReconciliation" in source
    assert "settleMaterialsOperation" in source
    assert 'error.code === "material_revision_conflict"' in source
    assert "materialsEditDraft" in source
    assert "materialsVersionDraft" in source
    assert "retained only in memory" in source
    assert "saved again explicitly" in source
    assert "requires an explicit submission" in source
    assert "do not resubmit the import" in source
    assert "materialsViewGeneration" in source
    assert "materialsListRequestSeq" in source
    assert "materialsDetailRequestSeq" in source
    assert "materialsOperationActive !== operation" in source
    assert "materialsCurrent(generation)" in source

    import_start = source.index("async function importMaterial")
    import_end = source.index("async function updateMaterialMetadata", import_start)
    import_flow = source[import_start:import_end]
    assert import_flow.count('materialUpload("/materials"') == 1
    assert import_flow.count("importMaterial(form") == 1


def test_safe_rendering_privacy_and_attachment_only_contract() -> None:
    html = asset("index.html")
    source = materials_js()
    combined = html + source
    for forbidden in (
        ".innerHTML",
        "insertAdjacentHTML",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "document.cookie",
        "serviceWorker",
        "sha256",
        "storage_key",
        "store_id",
        "CAREEROS_MATERIALS_ROOT",
        "source_path",
    ):
        assert forbidden not in combined
    for forbidden_tag in ("<iframe", "<object", "<embed"):
        assert forbidden_tag not in html.lower()
    assert 'download.setAttribute("download", "");' in source
    assert "Downloading creates an unmanaged copy outside CareerOS" in source
    assert "no document is shown in the page" in source
    assert 'href: "/material-versions/" + versionId + "/content"' in source
    assert 'href: "#materials"' not in source


def test_accessibility_focus_and_narrow_layout_contract() -> None:
    source = materials_js()
    css = asset("style.css")
    assert 'el("fieldset"' in source
    assert 'el("legend"' in source
    assert "aria-pressed" in source
    assert 'setAttribute("aria-busy", "true")' in source
    assert "material-detail-heading" in source
    assert "material-edit-name" in source
    assert "material-version-file" in source
    assert "@media (max-width: 600px)" in css
    assert ".materials-layout" in css
    assert "grid-template-columns: 1fr" in css
    assert "overflow-wrap: anywhere" in css


def test_cross_generation_write_is_owned_and_authoritatively_reconciled(
    tmp_path: Path,
) -> None:
    run_node_materials(
        tmp_path,
        r"""
        async function main() {
          const id = "a".repeat(32);
          let server = detail(id, "Old authoritative name");
          const write = deferred();
          let writeCount = 0;
          let getCount = 0;
          global.fetch = (url, options = {}) => {
            if (options.method === "PATCH") { writeCount += 1; return write.promise; }
            if (url.startsWith("/materials?")) { getCount += 1; return Promise.resolve(response(200, listing(server))); }
            if (url === "/materials/" + id) { getCount += 1; return Promise.resolve(response(200, server)); }
            throw new Error("unexpected request " + url);
          };

          window.location.hash = "#materials";
          await H.navigate();
          H.setState({ selectedId: id });
          await H.render();
          const originGeneration = H.generation();
          const firstWrite = H.update(server, "Submitted name", "resume", originGeneration);
          assert(writeCount === 1, "the first write must start exactly once");

          window.location.hash = "#review";
          await H.navigate();
          window.location.hash = "#materials";
          await H.navigate();
          assert(H.operation() !== null, "navigation must retain the global write owner");
          assert(document.getElementById("view-materials").getAttribute("aria-busy") === "true", "re-entry must remain busy");
          assert(document.getElementById("materials-status").textContent.includes("Saving material metadata"), "loading must not erase the mutation announcement");
          assert(document.getElementById("material-edit-name").disabled, "new-generation write controls must remain disabled");
          assert(document.getElementById("material-detail-heading").textContent === "Old authoritative name", "the new generation first renders its authoritative read");

          await H.update(server, "Overlapping write", "resume", H.generation());
          assert(writeCount === 1, "a second write must not overlap the old-generation write");

          server = detail(id, "Authoritative committed name", "active", 2);
          write.resolve(response(200, detail(id, "STALE RESPONSE MUST NOT RENDER", "active", 2)));
          await firstWrite;
          assert(document.getElementById("material-detail-heading").textContent === "Authoritative committed name", "reconciliation must render the current server read, never the stale write response");
          assert(!document.getElementById("view-materials").hasAttribute("aria-busy"), "busy must clear after reconciliation");
          assert(H.operation() === null, "the write owner must release only after reconciliation");

          const before = getCount;
          const listRead = deferred();
          const detailRead = deferred();
          let delayedReads = 0;
          global.fetch = (url) => {
            delayedReads += 1;
            if (url.startsWith("/materials?")) return listRead.promise;
            if (url === "/materials/" + id) return detailRead.promise;
            throw new Error("unexpected request " + url);
          };
          const reconcile = H.requestReconcile({});
          H.requestReconcile({});
          H.requestReconcile({});
          assert(delayedReads === 2, "overlapping reconciliation requests must share the active refresh");
          listRead.resolve(response(200, listing(server)));
          detailRead.resolve(response(200, server));
          await tick();
          global.fetch = (url) => {
            delayedReads += 1;
            if (url.startsWith("/materials?")) return Promise.resolve(response(200, listing(server)));
            if (url === "/materials/" + id) return Promise.resolve(response(200, server));
            throw new Error("unexpected request " + url);
          };
          await reconcile;
          assert(delayedReads === 4, "many dirty marks must coalesce to one bounded follow-up refresh");
          assert(getCount >= before, "the scenario must execute authoritative reads");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_failed_reconciliation_keeps_write_owned_until_visible_retry_succeeds(
    tmp_path: Path,
) -> None:
    run_node_materials(
        tmp_path,
        r"""
        async function main() {
          const id = "a".repeat(32);
          let server = detail(id, "Original name");
          let failRefresh = false;
          let readCount = 0;
          global.fetch = (url, options = {}) => {
            if (options.method === "PATCH") {
              server = detail(id, "Committed name", "active", 2);
              failRefresh = true;
              return Promise.resolve(response(200, server));
            }
            if (url.startsWith("/materials?") || url === "/materials/" + id) {
              readCount += 1;
              if (failRefresh) return Promise.reject(new Error("temporary read failure"));
              return Promise.resolve(response(200, url.startsWith("/materials?") ? listing(server) : server));
            }
            throw new Error("unexpected request " + url);
          };

          window.location.hash = "#materials";
          await H.navigate();
          H.setState({ selectedId: id });
          await H.render();
          await H.update(server, "Committed name", "resume", H.generation());
          assert(H.operation() !== null, "a failed authoritative refresh must retain write ownership");
          assert(H.reconcileDirty(), "a failed authoritative refresh must retain a retry marker across navigation");
          assert(document.getElementById("view-materials").getAttribute("aria-busy") === "true", "failed reconciliation must stay busy");
          assert(document.getElementById("materials-status").textContent.includes("authoritative refresh failed"), "the live region must explain the failed reconciliation");
          const retries = document.getElementById("materials-content").querySelectorAll(".retry-button");
          assert(retries.length > 0, "failed reconciliation must expose a visible retry");

          const beforeRetry = readCount;
          failRefresh = false;
          retries[0].dispatch("click");
          for (let attempt = 0; attempt < 5 && H.operation() !== null; attempt += 1) await tick();
          assert(readCount >= beforeRetry + 2, "the retry must rerun the complete authoritative list/detail reconciliation");
          assert(H.operation() === null, "write ownership may release after the retry succeeds");
          assert(!document.getElementById("view-materials").hasAttribute("aria-busy"), "successful retry must clear busy state");
          assert(document.getElementById("material-detail-heading").textContent === "Committed name", "the retry must render authoritative state");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_navigation_during_reconciliation_starts_current_generation_follow_up(
    tmp_path: Path,
) -> None:
    run_node_materials(
        tmp_path,
        r"""
        async function main() {
          const id = "a".repeat(32);
          let server = detail(id, "Original name");
          const oldList = deferred();
          const oldDetail = deferred();
          let writeFinished = false;
          let oldReconcileReads = 0;
          let currentReads = 0;
          global.fetch = (url, options = {}) => {
            if (options.method === "PATCH") {
              server = detail(id, "Current authoritative name", "active", 2);
              writeFinished = true;
              return Promise.resolve(response(200, server));
            }
            if (writeFinished && oldReconcileReads < 2) {
              oldReconcileReads += 1;
              return url.startsWith("/materials?") ? oldList.promise : oldDetail.promise;
            }
            if (url.startsWith("/materials?")) {
              currentReads += 1;
              return Promise.resolve(response(200, listing(server)));
            }
            if (url === "/materials/" + id) {
              currentReads += 1;
              return Promise.resolve(response(200, server));
            }
            throw new Error("unexpected request " + url);
          };

          window.location.hash = "#materials";
          await H.navigate();
          H.setState({ selectedId: id });
          await H.render();
          const mutation = H.update(server, "Current authoritative name", "resume", H.generation());
          await tick();
          assert(oldReconcileReads === 2, "the old generation reconciliation must be pending");

          for (let cycle = 0; cycle < 2; cycle += 1) {
            window.location.hash = "#review";
            await H.navigate();
            window.location.hash = "#materials";
            await H.navigate();
          }
          const readsBeforeOldSettlement = currentReads;
          oldList.resolve(response(200, listing(detail(id, "STALE LIST"))));
          oldDetail.resolve(response(200, detail(id, "STALE DETAIL")));
          await mutation;

          assert(currentReads >= readsBeforeOldSettlement + 2, "stale reconciliation settlement must trigger a current-generation follow-up");
          assert(document.getElementById("material-detail-heading").textContent === "Current authoritative name", "old-generation reconciliation must never render stale detail");
          assert(H.operation() === null, "the write owner must release after current-generation authority is established");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_retired_conflict_keeps_version_file_through_restore_and_reapply(
    tmp_path: Path,
) -> None:
    run_node_materials(
        tmp_path,
        r"""
        async function main() {
          const id = "a".repeat(32);
          const original = { name: "original.pdf", type: "application/pdf" };
          const replacement = { name: "replacement.pdf", type: "application/pdf" };
          let server = detail(id, "Resume");
          let versionMode = "retired-conflict";
          global.fetch = (url, options = {}) => {
            if (url === "/materials/" + id + "/versions" && options.method === "POST") {
              if (versionMode === "retired-conflict") {
                server = detail(id, "Resume", "retired", 2);
                return Promise.resolve(response(409, { detail: { code: "material_retired" } }));
              }
              if (versionMode === "conflict") {
                return Promise.resolve(response(409, { detail: { code: "material_revision_conflict" } }));
              }
              server = detail(id, "Resume", "active", 4, 2);
              return Promise.resolve(response(201, server));
            }
            if (url === "/materials/" + id + "/retirement" && options.method === "DELETE") {
              server = detail(id, "Resume", "active", 3);
              return Promise.resolve(response(200, server));
            }
            if (url.startsWith("/materials?")) return Promise.resolve(response(200, listing(server)));
            if (url === "/materials/" + id) return Promise.resolve(response(200, server));
            throw new Error("unexpected request " + url + " " + (options.method || "GET"));
          };

          window.location.hash = "#materials";
          await H.navigate();
          H.setState({ selectedId: id });
          await H.render();
          await H.addVersion(server, original, H.generation());
          assert(H.versionDraft() && H.versionDraft().file === original, "retired conflict must retain the selected File object");
          assert(!document.getElementById("material-version-form"), "retired detail must not expose add-version submission");
          assert(document.getElementById("material-version-draft-discard"), "retired detail must visibly expose the retained memory-only draft");
          assert(document.getElementById("materials-detail-region").textContent.includes("restore it"), "retired UI must explain how the retained file becomes usable");

          await H.retirement(H.state().detail, H.generation());
          assert(H.versionDraft() && H.versionDraft().file === original, "restore must not discard the retained file");
          assert(document.getElementById("material-version-form"), "restored detail must expose explicit resubmission");

          versionMode = "success";
          await H.addVersion(H.state().detail, H.versionDraft().file, H.generation());
          assert(H.versionDraft() === null, "confirmed add-version success must clear the retained file");

          versionMode = "conflict";
          await H.addVersion(H.state().detail, original, H.generation());
          const fileInput = document.getElementById("material-version-file");
          fileInput.files = [replacement];
          fileInput.dispatch("change");
          assert(H.versionDraft().file === replacement, "choosing a replacement must explicitly replace the retained file");
          document.getElementById("material-version-draft-discard").dispatch("click");
          assert(H.versionDraft() === null, "the explicit discard control must clear the retained file");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_ambiguous_later_page_create_reconciles_first_page_before_resubmit(
    tmp_path: Path,
) -> None:
    run_node_materials(
        tmp_path,
        r"""
        async function main() {
          const id = "a".repeat(32);
          const created = detail(id, "Committed import");
          const post = deferred();
          const firstPage = deferred();
          const urls = [];
          let postCount = 0;
          let firstPageReads = 0;
          global.fetch = (url, options = {}) => {
            urls.push(url);
            if (url === "/materials" && options.method === "POST") { postCount += 1; return post.promise; }
            if (url.startsWith("/materials?offset=20")) return Promise.resolve(response(200, listing(detail("c".repeat(32), "Later page"), 20, 21)));
            if (url.startsWith("/materials?offset=0")) {
              firstPageReads += 1;
              if (firstPageReads === 1) return Promise.resolve(response(200, listing(null, 0, 0)));
              return firstPage.promise;
            }
            throw new Error("unexpected request " + url);
          };

          window.location.hash = "#materials";
          await H.navigate();
          H.setState({ offset: 20, selectedId: null });
          await H.render();
          const name = document.getElementById("material-import-name");
          const file = document.getElementById("material-import-file");
          const form = document.getElementById("material-import-form");
          assert(name.maxLength === 160, "the import name boundary must be 160 characters");
          name.value = "N".repeat(160);
          file.files = [{ name: "resume.pdf", type: "application/pdf" }];

          const attempt = H.importMaterial(form, H.generation());
          assert(postCount === 1, "create must be sent exactly once");
          post.reject(new Error("lost create response"));
          await tick();
          assert(urls.some((url) => url.startsWith("/materials?offset=0")), "ambiguous create must reconcile the authoritative first page");
          assert(document.getElementById("material-import-submit").disabled, "blind duplicate resubmission must remain disabled during reconciliation");
          assert(H.createBlocked(), "the create guard must remain active until first-page authority returns");
          assert(document.getElementById("materials-status").textContent.includes("authoritative first page"), "the live region must explain the reconciliation");

          firstPage.resolve(response(200, listing(created, 0, 1)));
          await attempt;
          assert(postCount === 1, "ambiguous create must never be retried automatically");
          assert(H.state().offset === 0, "pagination must remain on the authoritative first page");
          assert(!H.createBlocked(), "successful first-page reconciliation may release create submission");
          assert(!document.getElementById("material-import-submit").disabled, "create submission may re-enable only after reconciliation");
          assert(document.activeElement && document.activeElement.id === "material-import-name", "import failure focus must return to an existing input");
        }
        main().catch((error) => { console.error(error.stack || error); process.exit(1); });
        """,
    )


def test_material_name_inputs_use_frozen_160_character_limit() -> None:
    source = materials_js()
    assert source.count("name.maxLength = 160;") == 2
    assert "name.maxLength = 255;" not in source


def _encoded(value: str) -> str:
    return quote(value, safe="~-._")


def test_browser_contract_round_trip_uses_temporary_store(tmp_path: Path) -> None:
    database = tmp_path / "frontend-materials.sqlite3"
    root = tmp_path / "materials-root"
    with connect(database) as connection:
        initialize_schema(connection)

    create_headers = {
        "Content-Type": "application/octet-stream",
        "X-CareerOS-Write": "1",
        "X-CareerOS-Material-Name": _encoded("Résumé master"),
        "X-CareerOS-Material-Category": "resume",
        "X-CareerOS-Display-Filename": _encoded("résumé.pdf"),
        "X-CareerOS-Declared-Media-Type": "application/pdf",
    }
    with TestClient(create_app(database, root), auto_write=False) as client:
        created = client.post(
            "/materials", headers=create_headers, content=b"version one"
        )
        assert created.status_code == 201
        detail = created.json()
        material_id = detail["material_id"]
        first_version = detail["current_version"]["version_id"]

        listed = client.get("/materials?offset=0&limit=20&include_retired=false")
        assert listed.status_code == 200
        assert [item["material_id"] for item in listed.json()["items"]] == [material_id]

        edited = client.patch(
            f"/materials/{material_id}",
            headers={
                "X-CareerOS-Write": "1",
                "X-CareerOS-Material-Revision": "1",
            },
            json={"name": "Résumé primary", "category": "portfolio"},
        )
        assert edited.status_code == 200
        assert edited.json()["revision"] == 2

        added = client.post(
            f"/materials/{material_id}/versions",
            headers={
                "Content-Type": "application/octet-stream",
                "X-CareerOS-Write": "1",
                "X-CareerOS-Material-Revision": "2",
                "X-CareerOS-Display-Filename": _encoded("résumé-v2.pdf"),
                "X-CareerOS-Declared-Media-Type": "application/pdf",
            },
            content=b"version two",
        )
        assert added.status_code == 201
        assert added.json()["revision"] == 3
        assert [version["version_number"] for version in added.json()["versions"]] == [
            2,
            1,
        ]

        conflict = client.patch(
            f"/materials/{material_id}",
            headers={
                "X-CareerOS-Write": "1",
                "X-CareerOS-Material-Revision": "2",
            },
            json={"name": "Unsent"},
        )
        assert conflict.status_code == 409
        assert conflict.json() == {
            "detail": {"code": "material_revision_conflict", "current_revision": 3}
        }

        retired = client.put(
            f"/materials/{material_id}/retirement",
            headers={
                "X-CareerOS-Write": "1",
                "X-CareerOS-Material-Revision": "3",
            },
        )
        assert retired.status_code == 200
        restored = client.delete(
            f"/materials/{material_id}/retirement",
            headers={
                "X-CareerOS-Write": "1",
                "X-CareerOS-Material-Revision": "4",
            },
        )
        assert restored.status_code == 200

        downloaded = client.get(f"/material-versions/{first_version}/content")
        assert downloaded.status_code == 200
        assert downloaded.content == b"version one"
        assert downloaded.headers["content-type"] == "application/octet-stream"
        assert downloaded.headers["content-disposition"].startswith("attachment;")
        assert downloaded.headers["cache-control"] == "no-store"
        assert downloaded.headers["x-content-type-options"] == "nosniff"

    assert database.is_file()
    assert root.is_dir()
