/* ============================================================================
   AGENT LIBRARY — APPLICATION SCRIPT
   Wings® Global Travel — Internal AI Asset Registry & Catalog
   ----------------------------------------------------------------------------
   FILE MAP
     1. BOOTSTRAP & API CLIENT  — fetch wrapper, CSRF, HTTP error handling
     2. UTILITIES                — dates, badges, icons, clipboard, toast, escaping
     3. APP STATE & ROUTER        — current view / filters / wizard state / history
     4. AUTH UI                    — sign in, sign out, change password
     5. SIDEBAR & TOPBAR            — chrome rendering
     6. LIBRARY VIEW                 — search, filter ribbon, cards/list, sections
     7. DETAIL PANEL                  — overview / how-to / config / files / versions
     8. WIZARD (ADD TO LIBRARY)        — two-step submission flow
     9. MY SUBMISSIONS                  — per-user buckets
    10. ADMIN / REVIEW                   — governance dashboard
    11. INIT                              — bootstrap

   WHERE THE DATA COMES FROM
   Nothing is stored in the browser. Every read and write goes through the
   `Api` object in section 1, which talks to the Flask JSON API over fetch()
   with `credentials: "same-origin"` so the HttpOnly session cookie travels
   with each request. There is no localStorage, no mock data, and no
   client-side password check anywhere in this file — authorisation decisions
   shown in the UI are *hints*; the server enforces all of them again.

   RENDERING SAFETY
   Views are built as HTML strings and assigned with innerHTML, matching the
   original prototype's structure. Every value that originates with a user is
   passed through `h()` (escape) or `Util.highlight()` (escape, then wrap
   matches in <mark>) before it reaches the string. Nothing raw is ever
   interpolated. Attribute values are always quoted, and `h()` escapes both
   quote characters, so an attribute cannot be broken out of.
   ============================================================================ */

"use strict";

/* ============================================================================
   1. BOOTSTRAP & API CLIENT
   ============================================================================ */

/** Server-rendered bootstrap payload (see templates/index.html). */
const BOOT = (function readBootstrap(){
  const node = document.getElementById("bootstrap-data");
  if (!node) return { user: null, version: "" };
  try { return JSON.parse(node.textContent) || { user: null, version: "" }; }
  catch (e) { return { user: null, version: "" }; }
})();

/** Reference data from GET /api/config. Populated during init(); the app
    refuses to render before it arrives, so no list is ever hard-coded here. */
let CONFIG = null;

const HTTP_MESSAGES = {
  400: "That request wasn't valid.",
  401: "Your session has ended — sign in to continue.",
  403: "You don't have permission to do that.",
  404: "That item no longer exists. It may have been removed.",
  409: "That conflicts with a change someone else made. Reload and try again.",
  413: "That file is too large to upload.",
  429: "Too many requests — wait a moment and try again.",
  500: "The server hit an error. It has been logged; please try again.",
  502: "The server is unavailable right now.",
  503: "The service is temporarily unavailable.",
};

/** Structured failure raised by Api. Carries the server's error envelope so
    callers can show per-field messages inside the wizard. */
class ApiError extends Error {
  constructor(status, code, message, fields){
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code || "ERROR";
    this.fields = fields || {};
  }
  get isAuth(){ return this.status === 401; }
  get isForbidden(){ return this.status === 403; }
  get isValidation(){ return this.status === 400 && Object.keys(this.fields).length > 0; }
}

const Api = {
  /** CSRF token: seeded from the meta tag, refreshed from the readable
      csrf_token cookie (the session cookie stays HttpOnly) and from any
      login/logout response, because those rotate the session. */
  _csrf: (document.querySelector('meta[name="csrf-token"]') || {}).content || "",

  csrfToken(){
    const cookie = document.cookie.split("; ").find(c => c.startsWith("csrf_token="));
    if (cookie){
      const value = decodeURIComponent(cookie.slice("csrf_token=".length));
      if (value) this._csrf = value;
    }
    return this._csrf;
  },

  setCsrf(token){ if (token) this._csrf = token; },

  /** Core request. Always sends credentials so the session cookie is
      included; always parses the {ok, data|error} envelope. */
  async request(path, options){
    const opts = Object.assign({ method: "GET" }, options || {});
    const headers = Object.assign({ "Accept": "application/json" }, opts.headers || {});
    const method = opts.method.toUpperCase();

    if (method !== "GET" && method !== "HEAD"){
      headers["X-CSRFToken"] = this.csrfToken();
    }
    if (opts.json !== undefined){
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }
    // FormData sets its own multipart boundary — never override it.
    opts.headers = headers;
    opts.credentials = "same-origin";

    let response;
    try {
      response = await fetch(path, opts);
    } catch (networkError){
      throw new ApiError(0, "NETWORK_ERROR",
        "Couldn't reach the server. Check your connection and try again.");
    }

    let payload = null;
    const contentType = response.headers.get("Content-Type") || "";
    if (contentType.indexOf("application/json") !== -1){
      try { payload = await response.json(); } catch (e) { payload = null; }
    }

    if (response.ok && payload && payload.ok === true) return payload.data;

    if (payload && payload.error){
      const err = payload.error;
      const error = new ApiError(response.status, err.code, err.message, err.fields);
      this._handleGlobal(error);
      throw error;
    }

    const error = new ApiError(
      response.status, "HTTP_" + response.status,
      HTTP_MESSAGES[response.status] || ("Unexpected response (" + response.status + ")."));
    this._handleGlobal(error);
    throw error;
  },

  /** Cross-cutting reactions: a 401 means the session died underneath us, so
      drop the cached identity and re-render the chrome. */
  _handleGlobal(error){
    if (error.status === 401 && State.user){
      State.user = null;
      renderSidebarNav();
      renderTopbarActions();
    }
    if (error.status === 400 && error.code === "CSRF_INVALID"){
      // Pull a fresh token so the user's retry succeeds.
      this.request("/api/auth/csrf").then(d => this.setCsrf(d.csrfToken)).catch(() => {});
    }
  },

  get(path, params){
    const url = params ? path + "?" + new URLSearchParams(
      Object.entries(params).filter(([, v]) => v !== "" && v !== null && v !== undefined)
    ).toString() : path;
    return this.request(url);
  },
  post(path, json){ return this.request(path, { method: "POST", json: json || {} }); },
  put(path, json){ return this.request(path, { method: "PUT", json: json || {} }); },
  del(path){ return this.request(path, { method: "DELETE" }); },
  upload(path, formData){ return this.request(path, { method: "POST", body: formData }); },
};

/** Turns any thrown error into a toast, and returns it for further handling. */
function reportError(error, fallback){
  if (error instanceof ApiError){
    if (error.isAuth){
      Toast.show(error.message || HTTP_MESSAGES[401], "error");
      openLoginModal();
      return error;
    }
    Toast.show(error.message || fallback || "Something went wrong.", "error");
    return error;
  }
  console.error(error);
  Toast.show(fallback || "Something went wrong.", "error");
  return error;
}

/* ============================================================================
   2. UTILITIES
   ============================================================================ */
const Util = {
  /** The single escaping choke point. Everything user-supplied goes through
      this before it is interpolated into an HTML string — including into
      attribute values, which is why both quote characters are escaped. */
  escapeHtml(str){
    return String(str == null ? "" : str).replace(/[&<>"']/g, c => ({
      "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"
    }[c]));
  },

  /** Defensive cleanup for display: the WGT code lives in its own field and is
      shown separately wherever a name appears, so strip any legacy prefix that
      an older record baked into the name text. */
  stripWgtPrefix(name){
    return String(name || "").replace(/^WGT\d{3,4}\s*/i, "").trim();
  },

  formatDate(iso){
    if (!iso) return "—";
    const d = new Date(String(iso).slice(0,10) + "T00:00:00");
    if (isNaN(d)) return String(iso);
    return d.toLocaleDateString("en-GB", { day:"numeric", month:"long", year:"numeric" });
  },

  formatDateShort(iso){
    if (!iso) return "—";
    const d = new Date(String(iso).slice(0,10) + "T00:00:00");
    if (isNaN(d)) return String(iso);
    return d.toLocaleDateString("en-GB", { day:"numeric", month:"short", year:"numeric" });
  },

  formatTimestamp(iso){
    if (!iso) return "—";
    const d = new Date(iso);
    if (isNaN(d)) return String(iso);
    return d.toLocaleString("en-GB", { day:"numeric", month:"short", year:"numeric",
                                       hour:"2-digit", minute:"2-digit" });
  },

  today(){ return new Date().toISOString().slice(0,10); },

  isOwnerRequired(asset){ return !asset.owner || !String(asset.owner).trim(); },

  /** The server computes this (Asset.freshness_badge) so the whole estate
      agrees on one clock; this is only the fallback for older payloads. */
  freshnessBadge(asset){ return asset.freshness || null; },

  statusBadgeClass(status){
    return {
      "Approved":"badge-approved", "Pending Review":"badge-pending-review",
      "Rejected":"badge-rejected", "Deprecated":"badge-deprecated", "Archived":"badge-archived",
    }[status] || "badge-outline";
  },

  typeMeta(typeId){
    const types = (CONFIG && CONFIG.assetTypes) || [];
    return types.find(t => t.id === typeId) || types[types.length - 1] ||
      { id:"other", label:"Other", short:"Other", colorClass:"type-other", railClass:"tl-other" };
  },

  debounce(fn, ms){
    let t; return function(...args){ clearTimeout(t); t = setTimeout(() => fn.apply(this,args), ms || 200); };
  },

  uniq(arr){ return Array.from(new Set(arr)); },

  /** Escape FIRST, then wrap matches — so a query of "<script>" highlights the
      escaped text rather than injecting a tag. */
  highlight(text, query){
    const safe = this.escapeHtml(text);
    const q = String(query || "").trim();
    if (!q) return safe;
    try {
      const re = new RegExp("(" + this.escapeHtml(q).replace(/[.*+?^${}()|[\]\\]/g, "\\$&") + ")", "ig");
      return safe.replace(re, "<mark>$1</mark>");
    } catch(e){ return safe; }
  },

  /** Only ever hand http/https URLs to window.open — never javascript: or
      data:, even though the server already refuses to store them. */
  safeExternalUrl(url){
    const raw = String(url || "").trim();
    if (!raw) return null;
    try {
      const parsed = new URL(raw, window.location.origin);
      return (parsed.protocol === "http:" || parsed.protocol === "https:") ? parsed.href : null;
    } catch(e){ return null; }
  },

  openExternal(url){
    const safe = this.safeExternalUrl(url);
    if (!safe){ Toast.show("That link isn't a valid web address.", "error"); return; }
    window.open(safe, "_blank", "noopener,noreferrer");
  },

  copyToClipboard(text){
    if (navigator.clipboard && navigator.clipboard.writeText){
      navigator.clipboard.writeText(text).then(() => Toast.show("Copied to clipboard", "success"))
        .catch(() => Toast.show("Couldn't copy — select and copy manually", "error"));
    } else {
      Toast.show("Clipboard unavailable in this browser", "error");
    }
  },

  fileIconLabel(ext){
    const icons = (CONFIG && CONFIG.fileTypeIcons) || {};
    return icons[ext] || "FILE";
  },

  announce(message){
    const region = document.getElementById("live-region");
    if (region) region.textContent = message;
  },
};

/** Short alias — used everywhere a value is interpolated into markup. */
const h = (value) => Util.escapeHtml(value);

/* ---- tiny inline icon set (stroke-based, matches the topbar/button icons) */
const Icon = {
  search: `<svg width="17" height="17" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="11" cy="11" r="7" stroke="currentColor" stroke-width="2"/><path d="M21 21l-4.3-4.3" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`,
  filter: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 6h16M7 12h10M10 18h4" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>`,
  grid: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3" y="3" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="2"/><rect x="14" y="3" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="2"/><rect x="3" y="14" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="2"/><rect x="14" y="14" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="2"/></svg>`,
  list: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M8 6h13M8 12h13M8 18h13" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/><circle cx="3.5" cy="6" r="1.6" fill="currentColor"/><circle cx="3.5" cy="12" r="1.6" fill="currentColor"/><circle cx="3.5" cy="18" r="1.6" fill="currentColor"/></svg>`,
  arrowFwd: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  chevDown: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 9l6 6 6-6" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  chevLeft: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M15 6l-6 6 6 6" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  chevRight: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M9 6l6 6-6 6" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  close: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" stroke="currentColor" stroke-width="2.2" stroke-linecap="round"/></svg>`,
  copy: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="8" y="8" width="12" height="12" rx="2" stroke="currentColor" stroke-width="2"/><path d="M4 16V5a1 1 0 011-1h11" stroke="currentColor" stroke-width="2"/></svg>`,
  plus: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 5v14M5 12h14" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"/></svg>`,
  trash: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 7h16M9 7V5a1 1 0 011-1h4a1 1 0 011 1v2m2 0-1 13a1 1 0 01-1 1H8a1 1 0 01-1-1L6 7" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  warn: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 9v4m0 4h.01M10.3 4.5L2.7 18a1.5 1.5 0 001.3 2.2h16a1.5 1.5 0 001.3-2.2L13.7 4.5a1.5 1.5 0 00-2.7 0z" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  check: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M5 13l4 4L19 7" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
  lock: `<svg width="12" height="12" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="5" y="11" width="14" height="10" rx="2" stroke="currentColor" stroke-width="2"/><path d="M8 11V7a4 4 0 018 0v4" stroke="currentColor" stroke-width="2"/></svg>`,
  file: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M6 2h9l5 5v14a1 1 0 01-1 1H6a1 1 0 01-1-1V3a1 1 0 011-1z" stroke="currentColor" stroke-width="1.8"/><path d="M14 2v6h6" stroke="currentColor" stroke-width="1.8"/></svg>`,
  info: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8"/><path d="M12 11v5M12 8h.01" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`,
  library: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M4 19V5a1 1 0 011-1h4v16H5a1 1 0 01-1-1z" stroke="currentColor" stroke-width="1.8"/><path d="M13 4h5a1 1 0 011 1v14a1 1 0 01-1 1h-5" stroke="currentColor" stroke-width="1.8"/><path d="M9 4v16" stroke="currentColor" stroke-width="1.8"/></svg>`,
  addSquare: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true"><rect x="3" y="3" width="18" height="18" rx="4" stroke="currentColor" stroke-width="1.8"/><path d="M12 8v8M8 12h8" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`,
  user: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="8" r="3.5" stroke="currentColor" stroke-width="1.8"/><path d="M4.5 20a7.5 7.5 0 0115 0" stroke="currentColor" stroke-width="1.8"/></svg>`,
  shield: `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z" stroke="currentColor" stroke-width="1.8"/></svg>`,
  clock: `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" aria-hidden="true"><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.8"/><path d="M12 7v5l3.5 2" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`,
  upload: `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M12 16V4m0 0L7 9m5-5l5 5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/><path d="M4 17v2a1 1 0 001 1h14a1 1 0 001-1v-2" stroke="currentColor" stroke-width="2" stroke-linecap="round"/></svg>`,
  signout: `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" aria-hidden="true"><path d="M14 8V6a1 1 0 00-1-1H6a1 1 0 00-1 1v12a1 1 0 001 1h7a1 1 0 001-1v-2" stroke="currentColor" stroke-width="1.9" stroke-linecap="round"/><path d="M10 12h10m0 0l-3-3m3 3l-3 3" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/></svg>`,
};

/* ---- toast notifications ---------------------------------------------- */
const Toast = {
  show(msg, kind){
    const el = document.createElement("div");
    el.className = "toast" + (kind ? " " + kind : "");
    el.setAttribute("role", kind === "error" ? "alert" : "status");
    const icon = kind === "success" ? Icon.check : kind === "error" ? Icon.warn : Icon.info;
    // Icon markup is a trusted constant; the message is escaped.
    el.innerHTML = icon + `<span>${h(msg)}</span>`;
    const container = document.getElementById("toast-container");
    if (!container) return;
    container.appendChild(el);
    Util.announce(String(msg));
    setTimeout(() => {
      el.style.opacity = "0";
      el.style.transition = "opacity .2s ease";
      setTimeout(() => el.remove(), 220);
    }, 3600);
  },
};

/* ============================================================================
   3. APP STATE & ROUTER
   ============================================================================ */
const State = {
  view: "library",              // library | add | my | admin
  user: BOOT.user || null,      // {id,email,fullName,role} or null
  detailAssetId: null,
  detailTab: "overview",
  loading: false,

  library: {
    query: "",
    viewMode: "card",           // card | list
    filters: { platform: "", department: "", tag: "" },
    sort: "-last_updated_at",
    page: 1,
    result: null,               // last /api/assets payload
  },

  my: { query: "", result: null },

  admin: {
    tab: "pending",             // pending | manage | activity
    librarySearch: "",
    pending: null,
    manage: null,
    activity: null,
    stats: null,
  },

  wizard: null,
  detailAsset: null,            // full detail payload for the open asset
  detailFiles: null,
};

function isAuthenticated(){ return !!State.user; }
function roleRank(role){ return { user:10, reviewer:20, admin:30 }[role] || 0; }
function hasRole(minimum){ return isAuthenticated() && roleRank(State.user.role) >= roleRank(minimum); }
function isReviewer(){ return hasRole("reviewer"); }
function isAdmin(){ return hasRole("admin"); }

/* ---- Browser back/forward support --------------------------------------
   Hash bookkeeping, extended from the prototype so an open asset is
   linkable too: #library, #add, #my, #admin, #asset/<id>. Every view change
   writes a history entry and a hashchange listener replays it, so Back and
   Forward behave the way they did before. _suppressHashChange stops the
   write we make ourselves from bouncing back in as an incoming event. ---- */
let _suppressHashChange = false;
const VIEWS = ["library", "add", "my", "admin"];

function updateHashForView(){
  const hash = State.detailAssetId ? ("asset/" + State.detailAssetId) : State.view;
  if (location.hash.slice(1) !== hash){
    _suppressHashChange = true;
    location.hash = hash;
  }
}

function applyViewFromHash(){
  const raw = (location.hash || "").replace(/^#/, "");
  const assetMatch = raw.match(/^asset\/(\d+)$/);
  if (assetMatch){
    if (State.view !== "library" && !State.detailAssetId) setView("library", true);
    openDetail(Number(assetMatch[1]), true);
    return;
  }
  if (State.detailAssetId) closeDetail(true);
  setView(VIEWS.indexOf(raw) !== -1 ? raw : "library", true);
}

const VIEW_TITLES = {
  library: "Library", add: "Add to Library",
  my: "My Submissions", admin: "Admin / Review",
};
const VIEW_CRUMBS = {
  library: "Agent Library",
  add: "Agent Library · New submission",
  my: "Agent Library · Your submissions",
  admin: "Agent Library · Governance",
};

/** Views that need a signed-in user before they will render anything. */
const VIEW_REQUIREMENTS = { add: "user", my: "user", admin: "reviewer" };

function setView(view, skipHash){
  const requirement = VIEW_REQUIREMENTS[view];
  if (requirement && !hasRole(requirement)){
    if (!isAuthenticated()){
      openLoginModal(() => setView(view));
      // Leave the chrome on a view the visitor is allowed to see.
      if (!VIEW_REQUIREMENTS[State.view]) return;
      view = "library";
    } else {
      Toast.show("The " + VIEW_TITLES[view] + " area needs the " + requirement + " role.", "error");
      view = "library";
    }
  }

  if (view === "admin" && State.view !== "admin"){
    // Arrive with a clean Manage Library search box rather than a stale query.
    State.admin.librarySearch = "";
    State.admin.tab = "pending";
  }
  if (view === "library" && State.view !== "library") State.library.page = 1;

  State.view = view;
  document.querySelectorAll(".nav-item").forEach(n =>
    n.classList.toggle("active", n.dataset.view === view));
  document.getElementById("topbar-title").textContent = VIEW_TITLES[view];
  document.getElementById("topbar-crumb").textContent = VIEW_CRUMBS[view];
  Util.announce(VIEW_TITLES[view] + " view");

  render();
  const root = document.getElementById("view-root");
  if (root) root.focus();
  window.scrollTo(0, 0);
  if (!skipHash) updateHashForView();
}

/** Renders the current view. Each renderer is async because its data comes
    from the API; a skeleton goes up first so the screen never sits blank. */
async function render(){
  const root = document.getElementById("view-root");
  if (!root) return;
  root.innerHTML = renderLoading();
  try {
    if (State.view === "library") await renderLibraryView();
    else if (State.view === "add") await renderWizardView();
    else if (State.view === "my") await renderMySubmissionsView();
    else if (State.view === "admin") await renderAdminView();
    renderSidebarNav();
  } catch (error){
    // Surface render failures instead of leaving a half-drawn screen, exactly
    // as the prototype did — but without a "reset saved data" button, because
    // there is no client-side data to reset any more.
    console.error("Render failed:", error);
    const message = error instanceof ApiError ? error.message : (error.message || String(error));
    root.innerHTML = `
      <div class="empty-state">
        <h3>Something went wrong displaying this page</h3>
        <p>${h(message)}</p>
        <button class="btn btn-primary" id="render-retry">Try again</button>
      </div>`;
    const retry = document.getElementById("render-retry");
    if (retry) retry.onclick = () => render();
  }
}

function renderLoading(label){
  return `<div class="empty-state" aria-busy="true">
    <div class="em-icon">${Icon.clock}</div>
    <h3>${h(label || "Loading…")}</h3>
  </div>`;
}

/* ============================================================================
   4. AUTH UI
   Replaces the prototype's AdminAuth password prompt. No password is ever
   compared here — the browser posts credentials and the server decides.
   ============================================================================ */
async function refreshSession(){
  try {
    const data = await Api.get("/api/auth/me");
    State.user = data.user;
    Api.setCsrf(data.csrfToken);
  } catch (e){
    State.user = null;
  }
  return State.user;
}

function openLoginModal(onSuccess){
  showModal("Sign in", `
    <p class="hint" style="margin-top:0;">Sign in with your Wings account to submit
      assets or review submissions.</p>
    <form id="login-form" novalidate>
      <div class="field">
        <label for="login-email">Email</label>
        <input class="input" type="email" id="login-email" autocomplete="username"
               required aria-describedby="login-error">
      </div>
      <div class="field">
        <label for="login-password">Password</label>
        <input class="input" type="password" id="login-password"
               autocomplete="current-password" required aria-describedby="login-error">
      </div>
      <div id="login-error" class="warn-banner" role="alert"
           style="display:none;margin-top:12px;">${Icon.warn}<span></span></div>
      <button type="submit" class="sr-only">Sign in</button>
    </form>
  `, [
    { label:"Cancel", cls:"btn-secondary", onClick: closeModal },
    { label:"Sign in", cls:"btn-primary", id:"login-submit", onClick: () => submitLogin(onSuccess) },
  ]);

  const form = document.getElementById("login-form");
  if (form) form.addEventListener("submit", e => { e.preventDefault(); submitLogin(onSuccess); });
  const email = document.getElementById("login-email");
  if (email) email.focus();
}

async function submitLogin(onSuccess){
  const emailField = document.getElementById("login-email");
  const passwordField = document.getElementById("login-password");
  const errorBox = document.getElementById("login-error");
  const button = document.getElementById("login-submit");
  if (!emailField || !passwordField) return;

  const showError = (message) => {
    if (!errorBox) return;
    errorBox.style.display = "flex";
    errorBox.querySelector("span").textContent = message;
    passwordField.value = "";
    passwordField.focus();
  };

  if (!emailField.value.trim() || !passwordField.value){
    showError("Enter your email address and password.");
    return;
  }

  if (button){ button.disabled = true; button.textContent = "Signing in…"; }
  try {
    const data = await Api.post("/api/auth/login", {
      email: emailField.value.trim(),
      password: passwordField.value,
    });
    State.user = data.user;
    Api.setCsrf(data.csrfToken);
    closeModal();
    Toast.show("Signed in as " + data.user.fullName, "success");
    renderSidebarNav();
    renderTopbarActions();
    // The Admin pending pill is role-dependent, so it has to be recomputed for
    // the identity that just signed in — not carried over from the last one.
    refreshPendingCount();
    if (typeof onSuccess === "function") onSuccess();
    else render();
  } catch (error){
    showError(error instanceof ApiError ? error.message : "Sign-in failed.");
  } finally {
    if (button){ button.disabled = false; button.textContent = "Sign in"; }
  }
}

async function doLogout(){
  try {
    const data = await Api.post("/api/auth/logout");
    Api.setCsrf(data.csrfToken);
  } catch (e){ /* the session is going away regardless */ }
  State.user = null;
  _pendingCount = 0;
  Toast.show("Signed out", "info");
  renderSidebarNav();
  renderTopbarActions();
  setView("library");
}

function openChangePasswordModal(){
  const minimum = (CONFIG && CONFIG.limits && CONFIG.limits.minPasswordLength) || 12;
  showModal("Change password", `
    <form id="pw-form" novalidate>
      <div class="field"><label for="pw-current">Current password</label>
        <input class="input" type="password" id="pw-current" autocomplete="current-password"></div>
      <div class="field"><label for="pw-new">New password</label>
        <input class="input" type="password" id="pw-new" autocomplete="new-password">
        <div class="hint">At least ${h(minimum)} characters.</div></div>
      <div class="field"><label for="pw-confirm">Confirm new password</label>
        <input class="input" type="password" id="pw-confirm" autocomplete="new-password"></div>
      <div id="pw-error" class="warn-banner" role="alert"
           style="display:none;margin-top:12px;">${Icon.warn}<span></span></div>
      <button type="submit" class="sr-only">Change password</button>
    </form>
  `, [
    { label:"Cancel", cls:"btn-ghost", onClick: closeModal },
    { label:"Change password", cls:"btn-primary", id:"pw-submit", onClick: submitPasswordChange },
  ]);
  const form = document.getElementById("pw-form");
  if (form) form.addEventListener("submit", e => { e.preventDefault(); submitPasswordChange(); });
  const first = document.getElementById("pw-current");
  if (first) first.focus();
}

async function submitPasswordChange(){
  const errorBox = document.getElementById("pw-error");
  const button = document.getElementById("pw-submit");
  const showError = (message) => {
    if (!errorBox) return;
    errorBox.style.display = "flex";
    errorBox.querySelector("span").textContent = message;
  };
  const current = document.getElementById("pw-current").value;
  const next = document.getElementById("pw-new").value;
  const confirm = document.getElementById("pw-confirm").value;

  if (next !== confirm){ showError("The two new passwords don't match."); return; }

  if (button){ button.disabled = true; button.textContent = "Saving…"; }
  try {
    const data = await Api.post("/api/auth/change-password", {
      currentPassword: current, newPassword: next, confirmPassword: confirm,
    });
    Api.setCsrf(data.csrfToken);
    State.user = data.user;
    closeModal();
    Toast.show("Password updated", "success");
  } catch (error){
    const message = error instanceof ApiError
      ? (Object.values(error.fields)[0] || error.message)
      : "Couldn't change the password.";
    showError(message);
  } finally {
    if (button){ button.disabled = false; button.textContent = "Change password"; }
  }
}

/* ============================================================================
   5. SIDEBAR & TOPBAR
   ============================================================================ */
let _pendingCount = 0;

function renderSidebarNav(){
  const items = [
    { view:"library", label:"Library", icon:Icon.library },
    { view:"add", label:"Add to Library", icon:Icon.addSquare, locked: !isAuthenticated() },
    { view:"my", label:"My Submissions", icon:Icon.user, locked: !isAuthenticated() },
    { view:"admin", label:"Admin / Review", icon:Icon.shield,
      count: _pendingCount, locked: !isReviewer() },
  ];
  const nav = document.getElementById("nav-list");
  if (!nav) return;
  nav.innerHTML = items.map(it => `
    <li>
      <button class="nav-item ${State.view === it.view ? "active" : ""}"
              data-view="${h(it.view)}"
              ${it.locked ? 'title="Sign in required" aria-describedby="live-region"' : ""}>
        ${it.icon}<span>${h(it.label)}</span>
        ${it.locked ? `<span class="nav-lock" aria-label="Sign in required">${Icon.lock}</span>` : ""}
        ${(!it.locked && it.count) ? `<span class="count-pill">${h(it.count)}</span>` : ""}
      </button>
    </li>`).join("");
  nav.querySelectorAll(".nav-item").forEach(btn => {
    btn.addEventListener("click", () => setView(btn.dataset.view));
  });
  renderSidebarAccount();
}

function renderSidebarAccount(){
  const box = document.getElementById("sidebar-account");
  if (!box) return;
  if (!isAuthenticated()){
    box.innerHTML = `<button class="sidebar-signin" id="sidebar-signin">${Icon.user}
      <span>Sign in</span></button>`;
    const btn = document.getElementById("sidebar-signin");
    if (btn) btn.onclick = () => openLoginModal();
    return;
  }
  box.innerHTML = `
    <div class="sidebar-user">
      <div class="su-name">${h(State.user.fullName || State.user.email)}</div>
      <div class="su-role">${h(State.user.role)}</div>
      <div class="su-actions">
        <button class="su-link" id="sidebar-change-pw">Change password</button>
        <button class="su-link" id="sidebar-signout">${Icon.signout} Sign out</button>
      </div>
    </div>`;
  const pw = document.getElementById("sidebar-change-pw");
  if (pw) pw.onclick = openChangePasswordModal;
  const out = document.getElementById("sidebar-signout");
  if (out) out.onclick = doLogout;
}

function renderTopbarActions(){
  const box = document.getElementById("topbar-actions");
  if (box) box.innerHTML = "";
  const addBtn = document.getElementById("topbar-add-btn");
  if (addBtn) addBtn.hidden = false;
}

/** Keeps the Admin nav pill honest without pulling the whole queue. */
async function refreshPendingCount(){
  if (!isReviewer()){ _pendingCount = 0; return; }
  try {
    const data = await Api.get("/api/assets", { status: "Pending Review", per_page: 1 });
    _pendingCount = data.total || 0;
  } catch (e){ _pendingCount = 0; }
  renderSidebarNav();
}

/* ============================================================================
   6. LIBRARY VIEW
   ============================================================================ */
function isDefaultFilterState(){
  const f = State.library.filters;
  return !State.library.query.trim() && !f.platform && !f.department && !f.tag.trim();
}

function activeFilterChips(){
  const f = State.library.filters; const chips = [];
  if (f.platform) chips.push({ group:"platform", value:f.platform, label:Util.typeMeta(f.platform).label });
  if (f.department) chips.push({ group:"department", value:f.department, label:f.department });
  if (f.tag.trim()) chips.push({ group:"tag", value:f.tag, label:"Tag: " + f.tag.trim() });
  return chips;
}

function removeFilterChip(group){
  State.library.filters[group] = "";
  State.library.page = 1;
  render();
}

function clearAllFilters(){
  State.library.query = "";
  State.library.filters = { platform:"", department:"", tag:"" };
  State.library.page = 1;
  render();
}

function renderStatusBadge(asset){
  return `<span class="badge ${h(Util.statusBadgeClass(asset.status))}">${h(asset.status)}</span>`;
}
function renderVerifiedBadge(asset){
  if (asset.status === "Approved") return `<span class="badge badge-verified">${Icon.check}Verified</span>`;
  return "";
}
function renderFreshnessBadge(asset){
  const f = Util.freshnessBadge(asset);
  if (!f) return "";
  return `<span class="badge ${f === "New" ? "badge-new" : "badge-updated"}">${h(f)}</span>`;
}
function renderOwnerFlag(asset){
  return Util.isOwnerRequired(asset) ? `<span class="ac-owner-flag">${Icon.warn} Owner required</span>` : "";
}
function renderTypePill(asset){
  const m = Util.typeMeta(asset.type);
  return `<span class="type-pill ${h(m.colorClass)}"><span class="dot"></span>${h(m.short)}</span>`;
}

function renderFreshnessBanner(asset){
  // Always reserve the row's height so cards with and without a banner line
  // up; &nbsp; gives the empty one a real line-box (see the original note).
  const f = Util.freshnessBadge(asset);
  if (!f) return `<div class="ac-freshness-banner is-empty">&nbsp;</div>`;
  return `<div class="ac-freshness-banner ${f === "New" ? "is-new" : "is-updated"}">${
    f === "New" ? "✦ New" : "↻ Updated"}</div>`;
}

function renderAssetCard(asset, query){
  const m = Util.typeMeta(asset.type);
  const tags = (asset.tags || []).slice(0,4)
    .map(t => `<span class="chip tag-chip">${h(t)}</span>`).join("");
  const wasActuallyUpdated = !!(asset.lastUpdated && asset.publishedDate &&
    asset.lastUpdated !== asset.publishedDate);
  const versionMeta = `<strong>v${h(asset.currentVersion)}</strong> · Published ${
    h(Util.formatDateShort(asset.publishedDate))}` +
    (wasActuallyUpdated ? ` · Updated ${h(Util.formatDateShort(asset.lastUpdated))}` : "");
  const hasUrl = !!Util.safeExternalUrl(asset.directUrl);
  return `
  <article class="asset-card ${h(m.railClass)}" data-asset-id="${h(asset.id)}">
    ${renderFreshnessBanner(asset)}
    <div class="ac-body">
      <div class="ac-top">
        ${renderTypePill(asset)}
        ${renderStatusBadge(asset)}
      </div>
      <div class="ac-name" data-open-detail="${h(asset.id)}" tabindex="0" role="button">${
        Util.highlight(Util.stripWgtPrefix(asset.name), query)}</div>
      <div class="ac-wgt-code">${h(asset.wgtCode)}</div>
      <div class="ac-desc">${Util.highlight(asset.description, query)}</div>
      <div class="ac-meta-row">
        <span>${h(asset.department)}</span>
        <span class="dot-sep">${h(asset.platform)}</span>
        <span class="dot-sep">Owner: ${asset.owner ? h(asset.owner) : "—"}</span>
      </div>
      <div class="ac-tags">${tags}</div>
    </div>
    <div class="ac-perforation"></div>
    <div class="ac-stub">
      <div class="stub-row-top">
        <div class="stub-version">${versionMeta}</div>
        <div class="stub-status-flags">${renderVerifiedBadge(asset)}${renderOwnerFlag(asset)}</div>
      </div>
      <div class="stub-row-actions">
        <button class="btn-secondary btn btn-sm" data-open-detail="${h(asset.id)}">View Details</button>
        <button class="ac-open-btn" data-open-tool="${h(asset.id)}" ${hasUrl ? "" : "disabled"}
                title="${hasUrl ? "Open Tool" : "No direct access URL on record"}">
          Open ${Icon.arrowFwd}
        </button>
      </div>
    </div>
  </article>`;
}

/** One titled section: Featured, Recently Added, or a department group.
    Card view renders it as a horizontal rail; list view as a dense table —
    the same groups exist in both, only the layout changes. */
function renderSection(title, desc, items, query, sectionId, viewMode){
  const body = viewMode === "card"
    ? `<div class="rail-wrap" data-rail-wrap="${h(sectionId)}">
        <button class="rail-arrow rail-arrow-left" data-rail-scroll="${h(sectionId)}" data-dir="-1"
                aria-label="Scroll ${h(title)} left">${Icon.chevLeft}</button>
        <div class="rail" id="rail-${h(sectionId)}">${items.map(a => renderAssetCard(a, query)).join("")}</div>
        <button class="rail-arrow rail-arrow-right" data-rail-scroll="${h(sectionId)}" data-dir="1"
                aria-label="Scroll ${h(title)} right">${Icon.chevRight}</button>
      </div>`
    : `<div class="list-view">${renderAssetRowHead()}${items.map(a => renderAssetRow(a, query)).join("")}</div>`;
  return `
  <div class="section-block">
    <div class="section-head"><h2>${h(title)}</h2>${
      desc ? `<span class="section-desc">${h(desc)}</span>` : ""}</div>
    ${body}
  </div>`;
}

function wireRailArrows(){
  document.querySelectorAll("[data-rail-scroll]").forEach(btn => {
    btn.onclick = () => {
      const rail = document.getElementById("rail-" + btn.dataset.railScroll);
      if (!rail) return;
      rail.scrollBy({ left: rail.clientWidth * 0.9 * Number(btn.dataset.dir), behavior: "smooth" });
    };
  });
  document.querySelectorAll("[data-rail-wrap]").forEach(wrap => {
    const rail = wrap.querySelector(".rail");
    if (rail) wrap.classList.toggle("no-overflow", rail.scrollWidth <= rail.clientWidth + 4);
  });
}

function renderAssetRowHead(){
  return `<div class="asset-row asset-row-head"><span></span><span>Name</span><span>Type</span>` +
    `<span>Department</span><span>Owner</span><span>Updated</span><span>Status</span><span></span></div>`;
}

function renderAssetRow(asset, query){
  const m = Util.typeMeta(asset.type);
  const hasUrl = !!Util.safeExternalUrl(asset.directUrl);
  return `
  <div class="asset-row ${h(m.railClass)}" data-asset-id="${h(asset.id)}">
    <span class="type-pill ${h(m.colorClass)}"><span class="dot"></span></span>
    <span>
      <div class="row-name" data-open-detail="${h(asset.id)}" tabindex="0" role="button">${
        Util.highlight(Util.stripWgtPrefix(asset.name), query)} ${renderFreshnessBadge(asset)}</div>
      <div class="row-sub">${h(asset.wgtCode)} · ${h((asset.tags || []).slice(0,3).join(" · "))}</div>
    </span>
    <span>${h(m.short)}</span>
    <span>${h(asset.department)}</span>
    <span>${asset.owner ? h(asset.owner) : `<span class="ac-owner-flag">${Icon.warn} Required</span>`}</span>
    <span>${h(Util.formatDateShort(asset.lastUpdated))}</span>
    <span>${renderStatusBadge(asset)}</span>
    <span class="row-actions">
      <button class="btn btn-secondary btn-sm" data-open-detail="${h(asset.id)}">View Details</button>
      <button class="ac-open-btn" data-open-tool="${h(asset.id)}" ${hasUrl ? "" : "disabled"}>Open ${Icon.arrowFwd}</button>
    </span>
  </div>`;
}

function renderEmptyState(){
  return `
  <div class="empty-state">
    <div class="em-icon">${Icon.search}</div>
    <h3>No assets match those filters</h3>
    <p>Try widening your search, or clearing a filter — pending, rejected, deprecated,
       and archived assets are always hidden here.</p>
    <button class="btn btn-secondary" id="empty-clear-btn">Clear all filters</button>
    <div class="empty-note">nothing here yet — but that's easy to fix ✈</div>
  </div>`;
}

/** Fetch the approved catalogue for the current filter state. */
async function fetchLibrary(){
  const f = State.library.filters;
  const perPage = isDefaultFilterState()
    // No filters: pull a big page so the curated grouping (Featured / Recently
    // Added / one rail per department) has everything it needs in one round trip.
    ? ((CONFIG.limits && CONFIG.limits.maxPageSize) || 200)
    : ((CONFIG.limits && CONFIG.limits.defaultPageSize) || 48);
  const data = await Api.get("/api/assets", {
    status: "Approved",
    q: State.library.query.trim(),
    asset_type: f.platform,
    department: f.department,
    tag: f.tag.trim(),
    sort: State.library.sort,
    page: State.library.page,
    per_page: perPage,
  });
  State.library.result = data;
  return data;
}

async function renderLibraryView(){
  const data = await fetchLibrary();
  const list = data.items || [];
  const q = State.library.query.trim();
  const showSections = isDefaultFilterState();
  const chips = activeFilterChips();
  const viewMode = State.library.viewMode;
  const f = State.library.filters;

  const tagSuggestions = Util.uniq(list.flatMap(a => a.tags || [])).sort();
  const departments = (CONFIG.departments || []);

  const cardsOf = arr => viewMode === "card"
    ? `<div class="card-grid">${arr.map(a => renderAssetCard(a, q)).join("")}</div>`
    : `<div class="list-view">${renderAssetRowHead()}${arr.map(a => renderAssetRow(a, q)).join("")}</div>`;

  let sectionsHtml = "";
  if (showSections){
    const recentlyAdded = list
      .filter(a => !!Util.freshnessBadge(a))
      .sort((a,b) => String(b.createdDate).localeCompare(String(a.createdDate)))
      .slice(0,8);
    const featured = list.filter(a => a.featured).slice(0,8);
    const deptGroups = departments
      .map(d => ({ dept: d, items: list.filter(a => a.department === d) }))
      .filter(s => s.items.length > 0);

    const sections = [];
    if (featured.length) sections.push({ title:"Featured / Recommended",
      desc:"Highlighted by the governance team", items:featured, id:"featured" });
    if (recentlyAdded.length) sections.push({ title:"Recently Added",
      desc:"Submitted or updated in the last 30 days", items:recentlyAdded, id:"recent" });
    deptGroups.forEach(s => sections.push({ title:s.dept, desc:"", items:s.items,
      id:"dept-" + s.dept.replace(/\s+/g,"-") }));

    sectionsHtml = sections.length
      ? sections.map(s => renderSection(s.title, s.desc, s.items, q, s.id, viewMode)).join("")
      : `<div class="section-block">${renderEmptyState()}</div>`;
  } else {
    sectionsHtml = `<div class="section-block">${list.length ? cardsOf(list) : renderEmptyState()}</div>`;
  }

  const hasMore = (data.page || 1) < (data.pages || 1);

  document.getElementById("view-root").innerHTML = `
  <div class="review-note">${Icon.info} Outputs and asset records must be reviewed by a
    human before being relied on or acted upon.</div>
  <div class="page-head">
    <h1>Agent Library</h1>
    <p class="sub">Discover approved ChatGPT, Claude, and Copilot assets built across WGT.</p>
  </div>
  <div class="search-bar">
    ${Icon.search}
    <input type="text" id="library-search"
           placeholder="Search agents, capabilities, departments, creators, or use cases..."
           value="${h(State.library.query)}" aria-label="Search the Agent Library">
    <span class="kbd" aria-hidden="true">/</span>
  </div>
  <div class="filter-ribbon">
    <div class="ribbon-group">
      <span class="ribbon-label" id="ribbon-platform-label">Platform</span>
      <div class="pill-toggle-group" role="group" aria-labelledby="ribbon-platform-label">
        <button class="pill-toggle ${!f.platform ? "active" : ""}" data-platform=""
                aria-pressed="${!f.platform}">All</button>
        ${(CONFIG.assetTypes || []).map(t => `
          <button class="pill-toggle ${f.platform === t.id ? "active" : ""}"
                  data-platform="${h(t.id)}" aria-pressed="${f.platform === t.id}">${h(t.label)}</button>`).join("")}
      </div>
    </div>
    <div class="ribbon-group">
      <label class="ribbon-label" for="ribbon-department">Department</label>
      <select class="select" id="ribbon-department">
        <option value="">All departments</option>
        ${departments.map(d => `<option value="${h(d)}" ${f.department === d ? "selected" : ""}>${h(d)}</option>`).join("")}
      </select>
    </div>
    <div class="ribbon-group">
      <label class="ribbon-label" for="ribbon-tag">Tag</label>
      <input class="input" id="ribbon-tag" list="ribbon-tag-list"
             placeholder="e.g. Accounts Payable" value="${h(f.tag)}">
      <datalist id="ribbon-tag-list">${
        tagSuggestions.map(s => `<option value="${h(s)}"></option>`).join("")}</datalist>
    </div>
    <span class="spacer"></span>
    <div class="view-toggle" role="group" aria-label="Switch view">
      <button class="${viewMode === "card" ? "active" : ""}" data-view-mode="card"
              aria-label="Card view" aria-pressed="${viewMode === "card"}">${Icon.grid}</button>
      <button class="${viewMode === "list" ? "active" : ""}" data-view-mode="list"
              aria-label="Compact list view" aria-pressed="${viewMode === "list"}">${Icon.list}</button>
    </div>
  </div>
  <div class="toolbar-row">
    <span class="result-count">${h(data.total)} asset${data.total === 1 ? "" : "s"} found</span>
    ${chips.length ? `<div class="chip-bar" style="margin:0;">
      ${chips.map(c => `<span class="chip">${h(c.label)}<button data-remove-chip="${h(c.group)}"
        aria-label="Remove ${h(c.label)} filter">${Icon.close}</button></span>`).join("")}
      <button class="clear-all" id="clear-all-filters">Clear all</button>
    </div>` : ""}
  </div>
  <div class="rails-wrap">${sectionsHtml}</div>
  ${hasMore ? `<div class="load-more-row">
    <button class="btn btn-secondary" id="load-more">Show more
      (${h(list.length)} of ${h(data.total)})</button></div>` : ""}
  `;

  wireLibraryEvents();
}

function wireLibraryEvents(){
  const f = State.library.filters;

  const searchInput = document.getElementById("library-search");
  if (searchInput){
    searchInput.addEventListener("input", Util.debounce(async e => {
      State.library.query = e.target.value;
      State.library.page = 1;
      await render();
      // Re-focus and restore the caret; a full re-render replaces the node.
      const again = document.getElementById("library-search");
      if (again){ again.focus(); again.selectionStart = again.selectionEnd = again.value.length; }
    }, 220));
  }

  document.querySelectorAll("[data-platform]").forEach(b => b.onclick = () => {
    f.platform = b.dataset.platform; State.library.page = 1; render();
  });

  const deptSelect = document.getElementById("ribbon-department");
  if (deptSelect) deptSelect.onchange = e => {
    f.department = e.target.value; State.library.page = 1; render();
  };

  const tagInput = document.getElementById("ribbon-tag");
  if (tagInput){
    tagInput.addEventListener("input", Util.debounce(async e => {
      f.tag = e.target.value; State.library.page = 1;
      await render();
      const again = document.getElementById("ribbon-tag");
      if (again){ again.focus(); again.selectionStart = again.selectionEnd = again.value.length; }
    }, 220));
  }

  document.querySelectorAll("[data-view-mode]").forEach(b => b.onclick = () => {
    State.library.viewMode = b.dataset.viewMode; render();
  });
  document.querySelectorAll("[data-remove-chip]").forEach(b =>
    b.onclick = () => removeFilterChip(b.dataset.removeChip));
  const clearAllBtn = document.getElementById("clear-all-filters");
  if (clearAllBtn) clearAllBtn.onclick = clearAllFilters;
  const emptyClear = document.getElementById("empty-clear-btn");
  if (emptyClear) emptyClear.onclick = clearAllFilters;
  const loadMore = document.getElementById("load-more");
  if (loadMore) loadMore.onclick = () => { State.library.page += 1; render(); };

  wireOpenHandlers(document);
  wireRailArrows();
}

/** Shared by every view that lists assets. */
function wireOpenHandlers(scope){
  scope.querySelectorAll("[data-open-detail]").forEach(el => {
    el.addEventListener("click", () => openDetail(Number(el.dataset.openDetail)));
    el.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === " "){ e.preventDefault(); openDetail(Number(el.dataset.openDetail)); }
    });
  });
  scope.querySelectorAll("[data-open-tool]").forEach(btn => {
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const id = Number(btn.dataset.openTool);
      const asset = findLoadedAsset(id);
      if (asset) Util.openExternal(asset.directUrl);
    });
  });
}

/** Look up an asset already in a rendered result set, so "Open Tool" does not
    need an extra round trip. */
function findLoadedAsset(id){
  const pools = [
    State.library.result && State.library.result.items,
    State.my.result && State.my.result.items,
    State.admin.pending && State.admin.pending.items,
    State.admin.manage && State.admin.manage.items,
    State.detailAsset ? [State.detailAsset] : null,
  ];
  for (const pool of pools){
    if (!pool) continue;
    const hit = pool.find(a => Number(a.id) === Number(id));
    if (hit) return hit;
  }
  return null;
}

/* ============================================================================
   7. DETAIL PANEL
   ============================================================================ */
let _lastFocusBeforeOverlay = null;

async function openDetail(assetId, skipHash){
  _lastFocusBeforeOverlay = document.activeElement;
  State.detailAssetId = assetId;
  State.detailTab = "overview";
  State.detailAsset = null;
  State.detailFiles = null;

  const overlay = document.getElementById("detail-overlay");
  const panel = document.getElementById("detail-panel");
  panel.innerHTML = renderLoading("Loading asset…");
  overlay.classList.add("open");
  document.body.classList.add("overlay-open");
  if (!skipHash) updateHashForView();

  try {
    const data = await Api.get("/api/assets/" + encodeURIComponent(assetId));
    State.detailAsset = data.asset;
    renderDetail();
  } catch (error){
    reportError(error, "Couldn't load that asset.");
    closeDetail();
  }
}

function closeDetail(skipHash){
  const overlay = document.getElementById("detail-overlay");
  if (overlay) overlay.classList.remove("open");
  document.body.classList.remove("overlay-open");
  document.getElementById("detail-panel").innerHTML = "";
  State.detailAssetId = null;
  State.detailAsset = null;
  State.detailFiles = null;
  if (!skipHash) updateHashForView();
  if (_lastFocusBeforeOverlay && document.contains(_lastFocusBeforeOverlay)){
    _lastFocusBeforeOverlay.focus();
  }
  _lastFocusBeforeOverlay = null;
}

function renderDetail(){
  const asset = State.detailAsset;
  const panel = document.getElementById("detail-panel");
  if (!asset){ panel.innerHTML = ""; return; }

  const m = Util.typeMeta(asset.type);
  const tabs = [
    ["overview","Overview"], ["howto","How to Use"], ["config","Configuration"],
    ["files","Files & Resources"], ["versions","Version History"],
  ];
  const hasUrl = !!Util.safeExternalUrl(asset.directUrl);
  // Server-supplied capability hints. The server re-checks every one of these
  // when the request actually arrives — hiding a button is a courtesy, not a
  // control.
  const canReview = !!asset.canReview;
  const canEdit = !!asset.canEdit;

  panel.innerHTML = `
    <div class="detail-head">
      <div class="detail-head-top">
        <div>
          <div class="detail-eyebrow">
            <span class="type-pill ${h(m.colorClass)}"><span class="dot"></span>${h(m.label)}</span>
            ${renderStatusBadge(asset)}
            ${renderVerifiedBadge(asset)}
          </div>
          <div class="detail-title" id="detail-title">${h(Util.stripWgtPrefix(asset.name))}</div>
          <div class="detail-idline">${h(asset.wgtCode)} · ${h(asset.platform)} · v${h(asset.currentVersion)}</div>
        </div>
        <button class="detail-close" id="detail-close-btn" aria-label="Close details">${Icon.close}</button>
      </div>
      <div class="detail-actions">
        ${hasUrl
          ? `<button class="btn btn-accent" id="detail-open-tool">Open Tool ${Icon.arrowFwd}</button>`
          : `<button class="btn btn-accent" disabled title="No direct access URL on record">Open Tool ${Icon.arrowFwd}</button>`}
        ${canReview && asset.status === "Pending Review" ? `
          <button class="btn btn-primary" data-approve="${h(asset.id)}">Approve</button>
          <button class="btn btn-danger-ghost" data-reject="${h(asset.id)}">Reject</button>` : ""}
        ${canReview && asset.status === "Approved" ? `
          <button class="btn btn-danger-ghost" data-remove-live="${h(asset.id)}">Remove from Library</button>` : ""}
        ${canEdit ? `<button class="btn btn-secondary" data-edit-asset="${h(asset.id)}">Edit</button>` : ""}
        ${canEdit ? `<button class="btn btn-secondary" id="detail-add-version">Add New Version</button>` : ""}
      </div>
      <div class="detail-tabs" role="tablist" aria-label="Asset detail sections">
        ${tabs.map(([id,label]) => `
          <button class="detail-tab ${State.detailTab === id ? "active" : ""}" role="tab"
                  id="tab-${h(id)}" aria-controls="pane-${h(id)}"
                  aria-selected="${State.detailTab === id}"
                  tabindex="${State.detailTab === id ? "0" : "-1"}"
                  data-tab="${h(id)}">${h(label)}</button>`).join("")}
      </div>
    </div>
    <div class="detail-scroll">
      ${tabs.map(([id]) => `
        <div class="detail-pane ${State.detailTab === id ? "active" : ""}" role="tabpanel"
             id="pane-${h(id)}" aria-labelledby="tab-${h(id)}"
             ${State.detailTab === id ? "" : "hidden"}
             data-pane="${h(id)}">${renderPane(id, asset)}</div>`).join("")}
    </div>`;

  wireDetailEvents(asset);
  const panelEl = document.getElementById("detail-panel");
  if (panelEl) panelEl.focus();
}

function renderPane(id, asset){
  if (id === "overview") return renderPaneOverview(asset);
  if (id === "howto") return renderPaneHowTo(asset);
  if (id === "config") return renderPaneConfig(asset);
  if (id === "files") return renderPaneFiles(asset);
  if (id === "versions") return renderPaneVersions(asset);
  return "";
}

function dlItem(k, v){ return `<div class="dl-item"><div class="k">${h(k)}</div><div class="v">${v}</div></div>`; }

function renderPaneOverview(asset){
  return `
  <div class="dl-grid">
    ${dlItem("WGT ID", `<span class="mono-cell">${h(asset.wgtCode)}</span>`)}
    ${dlItem("Type", h(Util.typeMeta(asset.type).label))}
    ${dlItem("Platform", h(asset.platform))}
    ${dlItem("Department", h(asset.department))}
    ${dlItem("Tags", (asset.tags || []).length
      ? (asset.tags).map(t => `<span class="chip tag-chip">${h(t)}</span>`).join(" ") : "—")}
    ${dlItem("Creator / Owner", asset.owner
      ? h(asset.owner) : `<span class="ac-owner-flag">${Icon.warn} Owner required</span>`)}
    ${dlItem("Creator / Owner Email", h(asset.ownerEmail || "—"))}
    ${dlItem("Current version", "v" + h(asset.currentVersion))}
    ${dlItem("Published", h(Util.formatDateShort(asset.publishedDate)))}
    ${dlItem("Last updated", h(Util.formatDateShort(asset.lastUpdated)))}
    ${dlItem("Next review", h(Util.formatDateShort(asset.nextReviewDate)))}
    ${dlItem("Status", renderStatusBadge(asset))}
    ${dlItem("Access level", h(asset.accessLevel))}
    ${dlItem("Sensitivity", h(asset.sensitivity))}
  </div>
  ${asset.rejectionReason ? `<div class="warn-banner">${Icon.warn}<span>
    <strong>Rejection note:</strong> ${h(asset.rejectionReason)}</span></div>` : ""}
  <div class="panel-card">
    <h4>Description</h4>
    <p class="body-text">${h(asset.description)}</p>
  </div>
  <div class="panel-card">
    <h4>Primary use case</h4>
    <p class="body-text">${h(asset.useCase)}</p>
  </div>
  ${asset.problemSolved ? `<div class="panel-card"><h4>What problem does this solve?</h4>
    <p class="body-text">${h(asset.problemSolved)}</p></div>` : ""}`;
}

function renderPaneHowTo(asset){
  const c = asset.configuration || {};
  return `
  <div class="panel-card"><h4>${Icon.info} What this tool does</h4>
    <p class="body-text">${h(asset.description)}</p></div>
  <div class="panel-card"><h4>${Icon.info} When to use it</h4>
    <p class="body-text">${h(c.whenToUse || asset.useCase)}</p></div>
  <div class="panel-card"><h4>${Icon.info} What you need to provide</h4>
    <p class="body-text">${h(asset.inputRequirements || "Not specified.")}</p></div>
  <div class="panel-card"><h4>${Icon.info} Expected output</h4>
    <p class="body-text">${h(asset.expectedOutput || "Not specified.")}</p></div>
  ${asset.exampleUse ? `<div class="panel-card"><h4>${Icon.info} Example use</h4>
    <div class="example-block">${h(asset.exampleUse)}</div></div>` : ""}
  ${renderExampleTriggersOrStarters(asset)}`;
}

function renderExampleTriggersOrStarters(asset){
  const c = asset.configuration || {};
  let list = [];
  if (asset.type === "gpt") list = c.conversationStarters || [];
  if (asset.type === "skill") list = c.exampleTriggers || [];
  if (!list.length) return "";
  return `<div class="panel-card"><h4>${Icon.info} ${
    asset.type === "gpt" ? "Conversation starters" : "Example trigger prompts"}</h4>
    ${list.map(p => `<div class="example-block">${h(p)}</div>`).join("")}
  </div>`;
}

/** Copy buttons stash the payload in a module-level map keyed by index rather
    than embedding whole instruction bodies in a data- attribute. */
const _copyPayloads = [];
function copyBtn(text, label){
  const index = _copyPayloads.push(String(text == null ? "" : text)) - 1;
  return `<button class="btn btn-secondary btn-sm" data-copy-index="${index}">${Icon.copy} ${h(label || "Copy")}</button>`;
}

function renderExpandable(id, title, bodyHtml, openByDefault){
  return `<div class="expandable ${openByDefault ? "open" : ""}" data-expandable="${h(id)}">
    <div class="expandable-head" data-toggle-expandable="${h(id)}" role="button" tabindex="0"
         aria-expanded="${!!openByDefault}"><span>${h(title)}</span>${Icon.chevDown}</div>
    <div class="expandable-body">${bodyHtml}</div>
  </div>`;
}

function renderPaneConfig(asset){
  const c = asset.configuration || {};
  _copyPayloads.length = 0;

  if (asset.type === "gpt"){
    return `
    <div class="panel-card">
      <h4>GPT identity</h4>
      <div class="dl-grid">
        ${dlItem("GPT name", h(c.gptName || "—"))}
        ${dlItem("Recommended model", h(c.recommendedModel || "—"))}
      </div>
      <p class="body-text">${h(c.gptDescription || "")}</p>
    </div>
    ${renderExpandable("instructions", "Instructions", `
      <div class="copy-row">${copyBtn(c.instructions || "", "Copy instructions")}</div>
      <div class="code-block">${h(c.instructions || "No instructions recorded.")}</div>
    `, true)}
    ${c.knowledgeBase ? `<div class="panel-card"><h4>Knowledge base</h4>
      <p class="body-text">${h(c.knowledgeBase)}</p></div>` : ""}
    <div class="panel-card">
      <h4>Capabilities</h4>
      <div class="ac-tags">${(c.capabilities || []).map(cap =>
        `<span class="chip">${Icon.check} ${h(cap)}</span>`).join("")
        || "<span class='help-text'>None enabled.</span>"}</div>
    </div>
    <div class="panel-card">
      <h4>External integrations</h4>
      <p class="body-text">Connects to an external app or API:
        <strong>${h(c.externalIntegration || "No")}</strong></p>
      ${(c.integrations || []).map(i => `<div class="example-block">
        <strong>${h(i.name || "Untitled integration")}</strong> — ${h(i.purpose || "")}${
        i.authNotes ? `<br>Auth notes: ${h(i.authNotes)}` : ""}</div>`).join("")}
      <div class="warn-banner">${Icon.warn}<span>Never store credentials, API keys,
        passwords, or access tokens in the Agent Library.</span></div>
    </div>`;
  }

  if (asset.type === "skill"){
    return `
    <div class="panel-card">
      <h4>Skill identity</h4>
      ${dlItem("Skill name", h(c.skillName || "—"))}
      <p class="body-text" style="margin-top:8px;">${h(c.skillDescription || "")}</p>
    </div>
    ${renderExpandable("skillmd", "SKILL.md", `
      <div class="copy-row">${copyBtn(c.skillMd || "", "Copy SKILL.md")}</div>
      <div class="code-block">${h(c.skillMd || "No SKILL.md recorded.")}</div>
    `, true)}
    ${c.instructions && c.instructions !== c.skillMd ? renderExpandable("skillinstr", "Instructions", `
      <div class="copy-row">${copyBtn(c.instructions, "Copy instructions")}</div>
      <div class="code-block">${h(c.instructions)}</div>
    `, false) : ""}
    <div class="panel-card">
      <h4>Dependencies</h4>
      ${(c.dependencies || []).length
        ? `<ul style="padding-left:18px;">${c.dependencies.map(d =>
            `<li class="body-text">${h(d)}</li>`).join("")}</ul>`
        : "<p class='help-text'>None declared.</p>"}
    </div>
    <div class="panel-card">
      <h4>Supported surfaces</h4>
      <div class="ac-tags">${(c.supportedSurfaces || []).map(s =>
        `<span class="chip">${h(s)}</span>`).join("")
        || "<span class='help-text'>Not specified.</span>"}</div>
    </div>
    ${(c.scripts || []).length ? `<div class="warn-banner">${Icon.warn}<span>
      This Skill includes executable script components (${
        c.scripts.map(s => h(s.name)).join(", ")}). Review before organizational
      deployment.</span></div>` : ""}`;
  }

  if (asset.type === "agent"){
    return `
    <div class="panel-card">
      <h4>Runtime</h4>
      <div class="dl-grid">
        ${dlItem("Platform / runtime", h(c.platformRuntime || "—"))}
        ${dlItem("Trigger type", h(c.triggerType || "—"))}
        ${dlItem("Human approval required", c.humanApprovalRequired ? "Yes" : "No")}
      </div>
      ${c.humanApprovalRequired ? `<p class="body-text" style="margin-top:8px;">
        <strong>Approval point:</strong> ${h(c.approvalLocation || "Not specified")}</p>` : ""}
    </div>
    ${renderExpandable("sysprompt", "Agent instructions / system prompt", `
      <div class="copy-row">${copyBtn(c.systemPrompt || "", "Copy prompt")}</div>
      <div class="code-block">${h(c.systemPrompt || "No system prompt recorded.")}</div>
    `, true)}
    <div class="panel-card"><h4>Inputs</h4>
      ${(c.inputs || []).map(i => `<div class="example-block"><strong>${h(i.name)}</strong>${
        i.desc ? " — " + h(i.desc) : ""}</div>`).join("") || "<p class='help-text'>None declared.</p>"}
    </div>
    <div class="panel-card"><h4>Outputs</h4>
      ${(c.outputs || []).map(o => `<div class="example-block"><strong>${h(o.name)}</strong>${
        o.desc ? " — " + h(o.desc) : ""}</div>`).join("") || "<p class='help-text'>None declared.</p>"}
    </div>
    <div class="panel-card"><h4>Tools / integrations</h4>
      ${(c.tools || []).map(t => `<div class="example-block"><strong>${h(t.name)}</strong> — ${
        h(t.purpose || "")} <span class="help-text">(${h(t.system || "")})</span></div>`).join("")
        || "<p class='help-text'>None declared.</p>"}
    </div>
    <div class="dl-grid">
      ${dlItem("Knowledge sources", h(c.knowledgeSources || "—"))}
      ${dlItem("Dependencies", h(c.dependencies || "—"))}
    </div>`;
  }

  return `
  ${renderExpandable("otherinstr", "Instructions", `
    <div class="copy-row">${copyBtn(c.instructions || "", "Copy instructions")}</div>
    <div class="code-block">${h(c.instructions || "No instructions recorded.")}</div>
  `, true)}
  <div class="dl-grid">
    ${dlItem("Input", h(c.input || "—"))}
    ${dlItem("Output", h(c.output || "—"))}
    ${dlItem("Platform", h(c.platform || "—"))}
    ${dlItem("Dependencies", h(c.dependencies || "—"))}
  </div>
  ${c.notes ? `<div class="panel-card"><h4>Notes</h4>
    <p class="body-text">${h(c.notes)}</p></div>` : ""}`;
}

function fileKindLabel(kind){
  const kinds = (CONFIG && CONFIG.fileKinds) || {};
  return (kinds[kind] || {}).label || kind;
}

function renderFileRow(f, canManage){
  const scanFlag = f.scanStatus === "infected"
    ? `<span class="ac-owner-flag">${Icon.warn} Quarantined</span>` : "";
  return `<div class="file-row">
    <div class="file-icon">${h(Util.fileIconLabel(f.ext))}</div>
    <div class="file-meta">
      <div class="file-name">${h(f.name)}</div>
      <div class="file-sub">${h(fileKindLabel(f.kind))}${
        f.category ? " · " + h(f.category) : ""} · ${h(f.size || "—")}${
        f.uploadedDate ? " · " + h(Util.formatDateShort(f.uploadedDate)) : ""}</div>
    </div>
    ${scanFlag}
    <span class="file-cat">${h(String(f.ext || "file").toUpperCase())}</span>
    <button class="btn btn-ghost btn-sm" data-view-file="${h(f.id)}">View</button>
    <button class="btn btn-ghost btn-sm" data-download-file="${h(f.id)}">Download</button>
    ${canManage ? `<button class="btn btn-ghost btn-sm" data-delete-file="${h(f.id)}"
      aria-label="Delete ${h(f.name)}">${Icon.trash}</button>` : ""}
  </div>`;
}

function renderPaneFiles(asset){
  const files = (State.detailFiles && State.detailFiles.files) || asset.files || [];
  const canUpload = State.detailFiles ? !!State.detailFiles.canUpload : !!asset.canEdit;
  const kinds = Object.keys((CONFIG && CONFIG.fileKinds) || {});
  const limits = (CONFIG && CONFIG.limits) || {};

  const groups = kinds.map(kind => {
    const inKind = files.filter(f => f.kind === kind);
    if (!inKind.length) return "";
    return `<div class="panel-card">
      <h4>${h(fileKindLabel(kind))}</h4>
      <p class="help-text" style="margin-bottom:10px;">${h((CONFIG.fileKinds[kind] || {}).desc || "")}</p>
      ${inKind.map(f => renderFileRow(f, canUpload)).join("")}
    </div>`;
  }).join("");

  const uploadForm = canUpload ? `
    <div class="panel-card">
      <h4>${Icon.upload} Attach a file</h4>
      <form id="file-upload-form" enctype="multipart/form-data" novalidate>
        <div class="field-row">
          <div class="field">
            <label for="fu-kind">What kind of file is this?</label>
            <select class="select" id="fu-kind" name="kind">
              ${kinds.map(k => `<option value="${h(k)}" ${k === "documentation" ? "selected" : ""}>${
                h(fileKindLabel(k))}</option>`).join("")}
            </select>
          </div>
          <div class="field">
            <label for="fu-category">Category (optional)</label>
            <select class="select" id="fu-category" name="category">
              <option value="">None</option>
              ${(CONFIG.resourceCategories || []).map(c =>
                `<option value="${h(c)}">${h(c)}</option>`).join("")}
            </select>
          </div>
        </div>
        <div class="field">
          <label for="fu-file">File</label>
          <input class="input" type="file" id="fu-file" name="file" required>
          <div class="hint">Allowed: ${h((limits.allowedUploadExtensions || []).join(", "))}.
            Maximum ${h(Math.round((limits.maxContentLength || 0) / (1024 * 1024)))} MB.</div>
        </div>
        <div id="fu-error" class="warn-banner" role="alert"
             style="display:none;">${Icon.warn}<span></span></div>
        <button type="submit" class="btn btn-primary" id="fu-submit">Upload file</button>
      </form>
      <div class="warn-banner" style="margin-top:12px;">${Icon.warn}<span>Never upload
        credentials, API keys, or access tokens.</span></div>
    </div>` : "";

  const body = groups || `<div class="empty-state"><p>No files attached to this asset yet.</p></div>`;
  return body + uploadForm;
}

function renderPaneVersions(asset){
  const versions = (asset.versions || []).slice();
  if (!versions.length) return `<div class="empty-state"><p>No version history recorded.</p></div>`;
  return `<div class="version-timeline">
    ${versions.map(v => `
      <div class="version-item ${v.isCurrent ? "current" : ""}">
        <div class="version-head"><span class="version-num">v${h(v.versionNumber)}${
          v.isCurrent ? " — Current" : ""}</span></div>
        <div class="version-date">${h(Util.formatDate(v.date))}</div>
        <div class="version-summary">${h(v.summary)}</div>
        <div class="version-by">Updated by ${h(v.updatedBy || "—")}</div>
      </div>`).join("")}
  </div>
  <p class="help-text" style="margin-top:16px;">Each version preserves its own
    instructions snapshot on the server rather than overwriting prior history.</p>`;
}

function wireDetailEvents(asset){
  const panel = document.getElementById("detail-panel");
  document.getElementById("detail-close-btn").onclick = () => closeDetail();

  const tabButtons = Array.from(panel.querySelectorAll(".detail-tab"));
  tabButtons.forEach((t, index) => {
    t.onclick = () => selectDetailTab(t.dataset.tab);
    // Arrow-key navigation between tabs, per the ARIA tabs pattern.
    t.onkeydown = (e) => {
      let target = null;
      if (e.key === "ArrowRight") target = tabButtons[(index + 1) % tabButtons.length];
      else if (e.key === "ArrowLeft") target = tabButtons[(index - 1 + tabButtons.length) % tabButtons.length];
      else if (e.key === "Home") target = tabButtons[0];
      else if (e.key === "End") target = tabButtons[tabButtons.length - 1];
      if (target){ e.preventDefault(); selectDetailTab(target.dataset.tab); }
    };
  });

  const openBtn = document.getElementById("detail-open-tool");
  if (openBtn) openBtn.onclick = () => Util.openExternal(asset.directUrl);

  const addVersionBtn = document.getElementById("detail-add-version");
  if (addVersionBtn) addVersionBtn.onclick = () => openAddVersionModal(asset);

  panel.querySelectorAll("[data-toggle-expandable]").forEach(head => {
    const toggle = () => {
      const box = head.closest(".expandable");
      box.classList.toggle("open");
      head.setAttribute("aria-expanded", box.classList.contains("open"));
    };
    head.onclick = toggle;
    head.onkeydown = e => { if (e.key === "Enter" || e.key === " "){ e.preventDefault(); toggle(); } };
  });

  panel.querySelectorAll("[data-copy-index]").forEach(b =>
    b.onclick = () => Util.copyToClipboard(_copyPayloads[Number(b.dataset.copyIndex)] || ""));

  // Files: real downloads through the authorised endpoint.
  panel.querySelectorAll("[data-view-file]").forEach(b =>
    b.onclick = () => window.open("/api/files/" + encodeURIComponent(b.dataset.viewFile) +
      "/download?disposition=inline", "_blank", "noopener,noreferrer"));
  panel.querySelectorAll("[data-download-file]").forEach(b =>
    b.onclick = () => { window.location.href = "/api/files/" +
      encodeURIComponent(b.dataset.downloadFile) + "/download"; });
  panel.querySelectorAll("[data-delete-file]").forEach(b =>
    b.onclick = () => confirmDeleteFile(Number(b.dataset.deleteFile)));

  const uploadForm = document.getElementById("file-upload-form");
  if (uploadForm) uploadForm.addEventListener("submit", submitFileUpload);

  // Governance actions. Scoped to the panel so the admin table underneath does
  // not get its own buttons re-bound.
  panel.querySelectorAll("[data-approve]").forEach(b =>
    b.onclick = () => approveAsset(Number(b.dataset.approve), () => { closeDetail(); render(); }));
  panel.querySelectorAll("[data-reject]").forEach(b =>
    b.onclick = () => rejectAssetWithConfirm(Number(b.dataset.reject), () => { closeDetail(); render(); }));
  panel.querySelectorAll("[data-remove-live]").forEach(b =>
    b.onclick = () => removeFromLibraryWithConfirm(Number(b.dataset.removeLive), () => { closeDetail(); render(); }));
  panel.querySelectorAll("[data-edit-asset]").forEach(b =>
    b.onclick = () => { const id = Number(b.dataset.editAsset); closeDetail(); startEditAsset(id); });
}

async function selectDetailTab(tab){
  State.detailTab = tab;
  if (tab === "files" && !State.detailFiles){
    try {
      State.detailFiles = await Api.get("/api/assets/" +
        encodeURIComponent(State.detailAssetId) + "/files");
    } catch (error){ reportError(error, "Couldn't load the file list."); }
  }
  renderDetail();
  const active = document.getElementById("tab-" + tab);
  if (active) active.focus();
}

async function submitFileUpload(event){
  event.preventDefault();
  const input = document.getElementById("fu-file");
  const errorBox = document.getElementById("fu-error");
  const button = document.getElementById("fu-submit");
  const showError = (message) => {
    if (!errorBox) return;
    errorBox.style.display = "flex";
    errorBox.querySelector("span").textContent = message;
  };
  if (errorBox) errorBox.style.display = "none";

  if (!input || !input.files || !input.files.length){
    showError("Choose a file first."); return;
  }
  const file = input.files[0];
  const limit = (CONFIG.limits && CONFIG.limits.maxContentLength) || 0;
  if (limit && file.size > limit){
    // Fail fast in the browser; the server enforces the same cap regardless.
    showError("That file is larger than the " +
      Math.round(limit / (1024 * 1024)) + " MB limit.");
    return;
  }

  const body = new FormData();
  body.append("file", file);
  body.append("kind", document.getElementById("fu-kind").value);
  const category = document.getElementById("fu-category").value;
  if (category) body.append("category", category);

  if (button){ button.disabled = true; button.textContent = "Uploading…"; }
  try {
    await Api.upload("/api/assets/" + encodeURIComponent(State.detailAssetId) + "/files", body);
    State.detailFiles = await Api.get("/api/assets/" +
      encodeURIComponent(State.detailAssetId) + "/files");
    Toast.show("File uploaded", "success");
    renderDetail();
  } catch (error){
    const message = error instanceof ApiError
      ? (Object.values(error.fields)[0] || error.message) : "Upload failed.";
    showError(message);
  } finally {
    if (button){ button.disabled = false; button.textContent = "Upload file"; }
  }
}

function confirmDeleteFile(fileId){
  showModal("Remove file", `<p class="body-text">Remove this file from the asset?
    It will be deleted from the file store and cannot be recovered.</p>`, [
    { label:"Cancel", cls:"btn-ghost", onClick: closeModal },
    { label:"Remove file", cls:"btn-danger", onClick: async () => {
      try {
        await Api.del("/api/files/" + encodeURIComponent(fileId));
        State.detailFiles = await Api.get("/api/assets/" +
          encodeURIComponent(State.detailAssetId) + "/files");
        Toast.show("File removed", "info");
        closeModal();
        renderDetail();
      } catch (error){ closeModal(); reportError(error, "Couldn't remove that file."); }
    }},
  ]);
}

/* ---- Add New Version ---------------------------------------------------- */
function openAddVersionModal(asset){
  const nextNum = (parseFloat(asset.currentVersion) + 0.1).toFixed(1);
  showModal("Add New Version", `
    <form id="nv-form" novalidate>
      <div class="field"><label for="nv-number">New version number</label>
        <input class="input" id="nv-number" value="${h(isNaN(parseFloat(nextNum)) ? "1.1" : nextNum)}"></div>
      <div class="field"><label for="nv-summary">Change summary <span class="req">*</span></label>
        <textarea class="textarea" id="nv-summary" placeholder="What changed in this version?"></textarea></div>
      <div class="field"><label for="nv-by">Updated by</label>
        <input class="input" id="nv-by" value="${h((State.user && State.user.fullName) || asset.owner || "")}"
               placeholder="Your name"></div>
      <div id="nv-error" class="warn-banner" role="alert" style="display:none;">${Icon.warn}<span></span></div>
      <p class="help-text">This creates a new version entry and preserves the current
        version in history — it does not overwrite it.</p>
      <button type="submit" class="sr-only">Add version</button>
    </form>
  `, [
    { label:"Cancel", cls:"btn-ghost", onClick: closeModal },
    { label:"Add Version", cls:"btn-primary", id:"nv-submit", onClick: () => submitNewVersion(asset) },
  ]);
  const form = document.getElementById("nv-form");
  if (form) form.addEventListener("submit", e => { e.preventDefault(); submitNewVersion(asset); });
  const summary = document.getElementById("nv-summary");
  if (summary) summary.focus();
}

async function submitNewVersion(asset){
  const errorBox = document.getElementById("nv-error");
  const button = document.getElementById("nv-submit");
  const showError = (message) => {
    if (!errorBox) return;
    errorBox.style.display = "flex";
    errorBox.querySelector("span").textContent = message;
  };
  const summary = document.getElementById("nv-summary").value.trim();
  if (!summary){ showError("Add a change summary first."); return; }

  if (button){ button.disabled = true; button.textContent = "Saving…"; }
  try {
    const data = await Api.post("/api/assets/" + encodeURIComponent(asset.id) + "/versions", {
      versionNumber: document.getElementById("nv-number").value.trim(),
      summary: summary,
      updatedBy: document.getElementById("nv-by").value.trim(),
    });
    State.detailAsset = data.asset;
    closeModal();
    Toast.show("New version added", "success");
    renderDetail();
  } catch (error){
    const message = error instanceof ApiError
      ? (Object.values(error.fields)[0] || error.message) : "Couldn't add the version.";
    showError(message);
  } finally {
    if (button){ button.disabled = false; button.textContent = "Add Version"; }
  }
}

/* ---- generic modal helper ---------------------------------------------- */
let _lastFocusBeforeModal = null;

function showModal(title, bodyHtml, buttons){
  _lastFocusBeforeModal = document.activeElement;
  const box = document.getElementById("modal-box");
  box.innerHTML = `
    <div class="modal-head"><h3 id="modal-title">${h(title)}</h3>
      <button class="icon-btn" id="modal-close-x" aria-label="Close dialog">${Icon.close}</button></div>
    <div class="modal-body">${bodyHtml}</div>
    <div class="modal-foot">${buttons.map((b, i) =>
      `<button class="btn ${h(b.cls || "btn-secondary")}" data-modal-btn="${i}"${
        b.id ? ` id="${h(b.id)}"` : ""}>${h(b.label)}</button>`).join("")}</div>`;
  document.getElementById("modal-overlay").classList.add("open");
  document.body.classList.add("overlay-open");
  document.getElementById("modal-close-x").onclick = closeModal;
  buttons.forEach((b, i) => {
    const el = box.querySelector(`[data-modal-btn="${i}"]`);
    if (el) el.onclick = b.onClick;
  });
  box.focus();
  trapFocus(box);
}

function closeModal(){
  document.getElementById("modal-overlay").classList.remove("open");
  document.getElementById("modal-box").innerHTML = "";
  if (!document.getElementById("detail-overlay").classList.contains("open")){
    document.body.classList.remove("overlay-open");
  }
  if (_lastFocusBeforeModal && document.contains(_lastFocusBeforeModal)){
    _lastFocusBeforeModal.focus();
  }
  _lastFocusBeforeModal = null;
}

/** Keeps Tab inside an open dialog — required for keyboard and screen-reader
    users, who would otherwise tab into the inert page behind it. */
function trapFocus(container){
  const selector = 'a[href], button:not([disabled]), input:not([disabled]), ' +
    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
  container.onkeydown = (e) => {
    if (e.key !== "Tab") return;
    const focusable = Array.from(container.querySelectorAll(selector))
      .filter(el => el.offsetParent !== null);
    if (!focusable.length) return;
    const first = focusable[0], last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first){ e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last){ e.preventDefault(); first.focus(); }
  };
}

/* ============================================================================
   8. ADD TO LIBRARY — TWO-STEP SUBMISSION FORM
   ----------------------------------------------------------------------------
   Step 1 asks "what are you submitting?"; step 2 is a form tailored to that
   type. Client-side validation is retained for fast feedback, but the server
   validates the same rules again in app/validators.py and is the only
   authority — a submission that bypasses this form still gets checked.
   ============================================================================ */
function newWizardState(){
  const models = (CONFIG && CONFIG.recommendedModels) || [];
  return {
    step: 1,
    type: null,
    name: "",
    description: "",
    instructions: "",
    knowledgeBase: "",
    preferredModel: models[0] || "",
    otherPlatform: "",
    link: "",
    department: "",
    tags: [],
    ownerName: (State.user && State.user.fullName) || "",
    ownerEmail: (State.user && State.user.email) || "",
    featured: false,
    _editingAssetId: null,
    _wgtPrefix: "",
    _fieldErrors: {},
    _submitting: false,
  };
}

function ensureWizard(){ if (!State.wizard) State.wizard = newWizardState(); return State.wizard; }
function startNewSubmission(){ State.wizard = newWizardState(); setView("add"); }

/** Opens the same form pre-filled from an existing asset. Platform, department
    and the WGT code are locked — the code is tied to the department digit and
    must never change once assigned. */
async function startEditAsset(assetId){
  try {
    const data = await Api.get("/api/assets/" + encodeURIComponent(assetId));
    const asset = data.asset;
    const c = asset.configuration || {};
    const w = newWizardState();
    w.step = 2;
    w.type = asset.type;
    w.name = Util.stripWgtPrefix(asset.name);
    w.description = asset.description;
    w.instructions = c.instructions || c.systemPrompt || c.skillMd || "";
    w.knowledgeBase = c.knowledgeBase || "";
    w.preferredModel = c.recommendedModel || w.preferredModel;
    w.otherPlatform = c.platform || "";
    w.link = asset.directUrl || "";
    w.department = asset.department;
    w.tags = (asset.tags || []).slice();
    w.ownerName = asset.owner || "";
    w.ownerEmail = asset.ownerEmail || "";
    w.featured = !!asset.featured;
    w._editingAssetId = asset.id;
    w._wgtPrefix = asset.wgtCode;
    State.wizard = w;
    setView("add");
  } catch (error){ reportError(error, "Couldn't open that asset for editing."); }
}

function detectPlatformFromLink(link){
  const l = String(link || "").trim().toLowerCase();
  if (!l) return null;
  if (l.includes("chatgpt.com") || l.includes("chat.openai.com")) return "gpt";
  if (l.includes("claude.ai")) return "skill";
  if (l.includes("copilot.microsoft.com") || l.includes("copilot.cloud.microsoft") ||
      l.includes("powerva.microsoft.com") || l.includes("powerautomate.com") ||
      l.includes("copilotstudio")) return "agent";
  return "other";
}

async function renderWizardView(){
  const w = ensureWizard();
  const editing = !!w._editingAssetId;
  const onStep2 = editing || w.step === 2;

  document.getElementById("view-root").innerHTML = `
  ${editing
    ? `<button class="btn btn-secondary" id="wiz-cancel-edit-top" style="margin-bottom:16px;">${Icon.chevLeft} Cancel</button>`
    : onStep2
      ? `<button class="btn btn-secondary btn-sm" id="wiz-back-top" style="margin-bottom:16px;">${Icon.chevLeft} Back</button>`
      : ""}
  <div class="page-head">
    <h1>${editing ? "Edit Submission" : "Add to Library"}</h1>
    <p class="sub">${editing
      ? "Update this asset's details. Platform, department, and its WGT code are fixed once assigned."
      : "Submit an asset and it goes to Admin / Review before it appears in the Library."}</p>
  </div>
  <div class="wizard-shell">
    <div class="wizard-body" id="wizard-step-body">${
      onStep2 ? renderWizStepDetails(w) : renderWizStepType(w)}</div>
    ${onStep2 ? `
    <div class="wizard-foot">
      ${editing ? `<button class="btn btn-secondary" id="wiz-cancel-edit">Cancel</button>`
                : `<button class="btn btn-secondary" id="wiz-back">Back</button>`}
      <span class="spacer"></span>
      <button class="btn btn-primary" id="wiz-submit">${
        editing ? "Save Changes" : "Submit for Review"}</button>
    </div>` : ""}
  </div>`;

  wireWizardStepEvents();
  wireWizardNav();
  if (onStep2 && !editing && w.department) refreshWgtPreview();
}

function renderWizStepType(w){
  return `
  <h2>What are you submitting?</h2>
  <p class="step-desc">Pick one — we'll show the right fields for it.</p>
  <div class="type-card-grid">
    ${(CONFIG.assetTypes || []).map(t => `
      <div class="type-card" data-select-type="${h(t.id)}" tabindex="0" role="button">
        <div class="tc-icon ${h(t.colorClass)}">${
          t.id === "gpt" ? Icon.library : t.id === "skill" ? Icon.addSquare
          : t.id === "agent" ? Icon.shield : Icon.file}</div>
        <h3>${h(t.label)}</h3>
        <p>${h(t.blurb)}</p>
      </div>`).join("")}
  </div>`;
}

/** Inline per-field error, populated from the server's VALIDATION_ERROR fields. */
function fieldError(w, field){
  const message = w._fieldErrors && w._fieldErrors[field];
  return message ? `<div class="field-error" role="alert">${h(message)}</div>` : "";
}

function renderWizStepDetails(w){
  const t = Util.typeMeta(w.type);
  const editing = !!w._editingAssetId;
  const maxTags = (CONFIG.limits && CONFIG.limits.maxTagsPerAsset) || 4;

  return `
  <div class="wiz-step2-head">
    <span class="type-pill ${h(t.colorClass)}"><span class="dot"></span>${h(t.label)}</span>
    ${w._wgtPrefix ? `<span class="mono-cell" style="margin-left:10px;">${h(w._wgtPrefix)}</span>` : ""}
  </div>
  ${w.type === "other" ? `
  <div class="field">
    <label for="f-other-platform">What tool is this? <span class="req">*</span></label>
    <div class="hint">"Other" doesn't say much on its own — name the actual tool or platform this runs on.</div>
    <input class="input" id="f-other-platform" value="${h(w.otherPlatform)}"
           placeholder="e.g. n8n, Zapier, an internal PowerApp">
    ${fieldError(w, "otherPlatform")}
  </div>` : ""}
  ${(editing && isReviewer()) ? `
  <div class="field featured-field">
    <label class="checkbox-row checkbox-row-lg">
      <input type="checkbox" id="f-featured" class="checkbox-lg" ${w.featured ? "checked" : ""}>
      Feature this in the Library (shows in the Featured / Recommended row)
    </label>
  </div>` : ""}
  <div class="field-row">
    <div class="field">
      <label for="f-owner-name">Creator / Owner Name <span class="req">*</span></label>
      <input class="input" id="f-owner-name" value="${h(w.ownerName)}" placeholder="Full name">
      ${fieldError(w, "ownerName")}
    </div>
    <div class="field">
      <label for="f-owner-email">Creator / Owner Email <span class="req">*</span></label>
      <input class="input" type="email" id="f-owner-email" value="${h(w.ownerEmail)}"
             placeholder="name@wingsglobaltravel.com">
      ${fieldError(w, "ownerEmail")}
    </div>
  </div>
  <div class="hint" style="margin-bottom:14px;">This person is responsible for the asset
    and is the contact if changes are needed during review.</div>
  ${editing ? `
  <div class="field">
    <label for="f-department-locked">Department</label>
    <div class="hint">Department (and its WGT code) can't be changed once assigned.
      An administrator can perform a controlled migration if it's genuinely wrong.</div>
    <input class="input" id="f-department-locked" value="${h(w.department)}" disabled>
  </div>` : `
  <div class="field">
    <label for="f-department">Department <span class="req">*</span></label>
    <div class="hint">Pick this first — it determines the WGT code this asset will carry.</div>
    <select class="select" id="f-department">
      <option value="">Select a department</option>
      ${(CONFIG.departments || []).map(d =>
        `<option value="${h(d)}" ${w.department === d ? "selected" : ""}>${h(d)}</option>`).join("")}
    </select>
    ${fieldError(w, "department")}
  </div>`}
  <div class="field">
    <label for="f-name">Name <span class="req">*</span></label>
    ${editing ? "" : `<div class="hint" id="name-field-hint">${w.department
      ? `Just the name — its WGT code (<strong class="mono-cell">${h(w._wgtPrefix || "…")}</strong>)
         shows separately, not as part of the name.`
      : "Pick a department above first."}</div>`}
    <input class="input" id="f-name" value="${h(w.name)}"
           placeholder="${w.department || editing ? "Add a name" : "Pick a department above first"}"
           ${!editing && !w.department ? "disabled" : ""}>
    ${fieldError(w, "name")}
  </div>
  <div class="field">
    <label for="f-desc">Description <span class="req">*</span></label>
    <div class="hint">What does this do, in a sentence or two?</div>
    <textarea class="textarea" id="f-desc"
      placeholder="Drafts first-pass management commentary from uploaded financial results.">${h(w.description)}</textarea>
    ${fieldError(w, "description")}
  </div>
  <div class="field">
    <label for="f-instructions">Instructions <span class="req">*</span></label>
    <div class="hint">The prompt / system instructions — this is the main thing that makes it work.</div>
    <textarea class="textarea code" id="f-instructions" rows="10"
      placeholder="Paste the full prompt or system instructions here...">${h(w.instructions)}</textarea>
    ${fieldError(w, "instructions")}
  </div>
  ${w.type === "gpt" ? `
  <div class="field">
    <label for="f-knowledge">Knowledge Base</label>
    <div class="hint">Optional — what files or reference material has this GPT been given, if any?</div>
    <textarea class="textarea" id="f-knowledge"
      placeholder="e.g. Prior 6 months of board packs, WGT chart of accounts.">${h(w.knowledgeBase)}</textarea>
  </div>
  <div class="field">
    <label for="f-model">Preferred Model</label>
    <div class="hint">Optional — defaults to the org's recommended tier if you're not sure.</div>
    <select class="select" id="f-model">
      ${(CONFIG.recommendedModels || []).map(m =>
        `<option value="${h(m)}" ${w.preferredModel === m ? "selected" : ""}>${h(m)}</option>`).join("")}
    </select>
  </div>` : ""}
  <div class="field">
    <label for="f-link">Link to the artifact <span class="req">*</span></label>
    <div class="hint">Paste the ChatGPT / Claude / Copilot link. "Open" will take people
      straight there once approved.</div>
    <input class="input" id="f-link" value="${h(w.link)}"
           placeholder="https://chatgpt.com/g/... or https://claude.ai/...">
    ${fieldError(w, "link")}
  </div>
  <div class="field">
    <label id="tag-label">Tags</label>
    <div class="hint">Add up to ${h(maxTags)}, e.g. Accounts Payable, Reporting,
      Client Onboarding. Press Enter to add.</div>
    <div id="tag-input-wrap">${renderTagChips(w)}</div>
    ${fieldError(w, "tags")}
  </div>
  <div class="warn-banner">${Icon.warn}<span>Never paste credentials, API keys, or
    passwords into any field here.</span></div>
  ${editing
    ? `<div class="review-note">${Icon.info} Saving records a new version in this asset's
        history — the previous version stays visible under Version History.</div>`
    : `<div class="review-note">${Icon.info} This submission goes to Admin / Review and
        won't appear in the Library until it's approved.</div>`}
  <div class="field" style="margin-top:14px;">
    <label for="f-change-summary">${editing ? "What changed?" : "Notes for the reviewer"}</label>
    <input class="input" id="f-change-summary" value=""
           placeholder="${editing ? "Recorded in the version history" : "Optional"}">
  </div>`;
}

function renderTagChips(w){
  const maxTags = (CONFIG.limits && CONFIG.limits.maxTagsPerAsset) || 4;
  const atLimit = w.tags.length >= maxTags;
  return `
    ${w.tags.map((tag, i) => `<span class="chip">${h(tag)}<button data-remove-tag="${i}"
      aria-label="Remove tag ${h(tag)}">${Icon.close}</button></span>`).join("")}
    ${atLimit
      ? `<span class="help-text" id="tag-limit-note">Maximum ${h(maxTags)} tags — remove one to add another.</span>`
      : `<input type="text" id="tag-input-field" aria-labelledby="tag-label"
           placeholder="${w.tags.length ? "Add another…" : "Type a tag and press Enter"}">`}`;
}

function wireWizardStepEvents(){
  const w = State.wizard;
  if (!w) return;

  if (!w._editingAssetId && w.step === 1){
    document.querySelectorAll("[data-select-type]").forEach(card => {
      const choose = () => { w.type = card.dataset.selectType; w.step = 2; render(); };
      card.addEventListener("click", choose);
      card.addEventListener("keydown", e => {
        if (e.key === "Enter" || e.key === " "){ e.preventDefault(); choose(); }
      });
    });
    return;
  }

  const on = (id, evt, fn) => { const el = document.getElementById(id); if (el) el.addEventListener(evt, fn); };
  on("f-name", "input", e => w.name = e.target.value);
  on("f-desc", "input", e => w.description = e.target.value);
  on("f-instructions", "input", e => w.instructions = e.target.value);
  on("f-knowledge", "input", e => w.knowledgeBase = e.target.value);
  on("f-model", "change", e => w.preferredModel = e.target.value);
  on("f-other-platform", "input", e => w.otherPlatform = e.target.value);
  on("f-link", "input", e => w.link = e.target.value);
  on("f-owner-name", "input", e => w.ownerName = e.target.value);
  on("f-owner-email", "input", e => w.ownerEmail = e.target.value);
  on("f-featured", "change", e => w.featured = e.target.checked);

  on("f-department", "change", async e => {
    w.department = e.target.value;
    const nameField = document.getElementById("f-name");
    if (nameField){
      nameField.disabled = !w.department;
      nameField.placeholder = w.department ? "Add a name" : "Pick a department above first";
      if (w.department) nameField.focus();
    }
    await refreshWgtPreview();
  });

  wireTagInput();
}

/** Asks the server what the next code would be. Explicitly a preview: the
    authoritative code is allocated inside the insert transaction, so two
    people looking at this at the same time will still get distinct codes. */
async function refreshWgtPreview(){
  const w = State.wizard;
  if (!w || !w.department || w._editingAssetId) return;
  try {
    const data = await Api.get("/api/assets/next-code", { department: w.department });
    w._wgtPrefix = data.preview;
  } catch (error){ w._wgtPrefix = ""; }
  const hint = document.getElementById("name-field-hint");
  if (hint){
    // Built from a constant template plus one escaped value.
    hint.innerHTML = w.department
      ? `Just the name — its WGT code (<strong class="mono-cell">${h(w._wgtPrefix || "…")}</strong>)
         shows separately, not as part of the name.`
      : "Pick a department above first.";
  }
  const head = document.querySelector(".wiz-step2-head .mono-cell");
  if (head) head.textContent = w._wgtPrefix || "";
}

function wireTagInput(){
  const w = State.wizard;
  const maxTags = (CONFIG.limits && CONFIG.limits.maxTagsPerAsset) || 4;
  const field = document.getElementById("tag-input-field");
  if (field){
    field.addEventListener("keydown", e => {
      if (e.key === "Enter" || e.key === ","){
        e.preventDefault();
        const val = field.value.trim();
        if (w.tags.length >= maxTags){
          Toast.show("Maximum " + maxTags + " tags — remove one to add another.", "error");
          return;
        }
        if (val && !w.tags.some(t => t.toLowerCase() === val.toLowerCase())){
          w.tags.push(val); rerenderTagInput();
        } else { field.value = ""; }
      }
    });
  }
  document.querySelectorAll("[data-remove-tag]").forEach(btn => {
    btn.addEventListener("click", () => {
      w.tags.splice(Number(btn.dataset.removeTag), 1); rerenderTagInput();
    });
  });
}

function rerenderTagInput(){
  const w = State.wizard;
  const wrap = document.getElementById("tag-input-wrap");
  if (!wrap) return;
  wrap.innerHTML = renderTagChips(w);
  wireTagInput();
  const field = document.getElementById("tag-input-field");
  if (field) field.focus();
}

/** Fast client-side feedback. The server re-validates all of this. */
function validateWizardForSubmit(w){
  if (!w._editingAssetId && !w.department) return "Department is required.";
  if (!w.name.trim()) return "Name is required.";
  if (!w.description.trim()) return "Description is required.";
  if (!w.instructions.trim()) return "Instructions are required — that's the prompt that makes it work.";
  if (w.type === "other" && !w.otherPlatform.trim())
    return "Please say what tool this actually is — \"Other\" isn't specific enough on its own.";
  if (!w.link.trim()) return "Link to the artifact is required — that's what people open once this is approved.";
  if (!Util.safeExternalUrl(w.link.trim())) return "The artifact link must be a full http:// or https:// address.";
  if (!w.ownerName.trim()) return "Creator / Owner Name is required.";
  if (!w.ownerEmail.trim()) return "Creator / Owner Email is required.";
  if (!/^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/.test(w.ownerEmail.trim()))
    return "Creator / Owner Email doesn't look like a valid email address.";
  return null;
}

function wireWizardNav(){
  const w = State.wizard;
  if (!w) return;
  const goBack = () => { w.step = 1; w._fieldErrors = {}; render(); };
  const backBtn = document.getElementById("wiz-back"); if (backBtn) backBtn.onclick = goBack;
  const backTop = document.getElementById("wiz-back-top"); if (backTop) backTop.onclick = goBack;
  const cancel = () => { const editing = !!w._editingAssetId; State.wizard = null;
    setView(editing && isReviewer() ? "admin" : "my"); };
  const cancelBtn = document.getElementById("wiz-cancel-edit"); if (cancelBtn) cancelBtn.onclick = cancel;
  const cancelTop = document.getElementById("wiz-cancel-edit-top"); if (cancelTop) cancelTop.onclick = cancel;
  const submit = document.getElementById("wiz-submit");
  if (submit) submit.onclick = () => (w._editingAssetId ? finalizeEditAsset() : finalizeWizard());
}

function wizardPayload(w){
  return {
    type: w.type,
    department: w._editingAssetId ? undefined : w.department,
    name: w.name.trim(),
    description: w.description.trim(),
    instructions: w.instructions,
    knowledgeBase: w.knowledgeBase,
    preferredModel: w.preferredModel,
    otherPlatform: w.otherPlatform.trim(),
    link: w.link.trim(),
    ownerName: w.ownerName.trim(),
    ownerEmail: w.ownerEmail.trim(),
    tags: w.tags.slice(),
    featured: w.featured,
    changeSummary: (document.getElementById("f-change-summary") || {}).value || "",
  };
}

/** Renders server-side field errors back into the form. */
function applyServerFieldErrors(error){
  const w = State.wizard;
  if (!(error instanceof ApiError) || !error.isValidation) return false;
  w._fieldErrors = error.fields;
  render();
  Toast.show(error.message, "error");
  const firstField = Object.keys(error.fields)[0];
  const map = { name:"f-name", description:"f-desc", instructions:"f-instructions",
    link:"f-link", ownerName:"f-owner-name", ownerEmail:"f-owner-email",
    department:"f-department", otherPlatform:"f-other-platform" };
  const el = document.getElementById(map[firstField]);
  if (el) el.focus();
  return true;
}

async function finalizeWizard(){
  const w = State.wizard;
  const err = validateWizardForSubmit(w);
  if (err){ Toast.show(err, "error"); return; }

  // Ask the server whether this looks like something already registered.
  let duplicates = [];
  try {
    const data = await Api.get("/api/assets/duplicate-check", {
      department: w.department, name: w.name.trim(),
    });
    duplicates = data.duplicates || [];
  } catch (e){ /* a duplicate-check failure must not block a submission */ }

  if (duplicates.length){
    showModal("This looks similar to an existing asset", `
      <p class="body-text">${duplicates.length === 1 ? "One asset" : h(duplicates.length) + " assets"}
        already in the ${h(w.department)} department ${duplicates.length === 1 ? "looks" : "look"}
        similar to this submission:</p>
      <ul class="dupe-list">
        ${duplicates.slice(0,4).map(d => `<li><span class="mono-cell">${h(d.wgtCode)}</span> —
          ${h(Util.stripWgtPrefix(d.name))} <span class="help-text">(${h(d.status)})</span></li>`).join("")}
      </ul>
      <p class="body-text">You can still submit — a governance admin will take a look during
        review and can reject it if it's truly redundant.</p>
    `, [
      { label:"Cancel", cls:"btn-ghost", onClick: closeModal },
      { label:"Submit Anyway", cls:"btn-primary", onClick: () => { closeModal(); submitNewAsset(w); } },
    ]);
    return;
  }
  submitNewAsset(w);
}

async function submitNewAsset(w){
  if (w._submitting) return;
  w._submitting = true;
  const button = document.getElementById("wiz-submit");
  if (button){ button.disabled = true; button.textContent = "Submitting…"; }
  try {
    const data = await Api.post("/api/assets", wizardPayload(w));
    State.wizard = null;
    Toast.show("Submitted for review — your reference is " + data.asset.wgtCode, "success");
    await refreshPendingCount();
    setView("my");
  } catch (error){
    w._submitting = false;
    if (!applyServerFieldErrors(error)) reportError(error, "Couldn't submit that asset.");
  } finally {
    if (button){ button.disabled = false; button.textContent = "Submit for Review"; }
    if (State.wizard) State.wizard._submitting = false;
  }
}

async function finalizeEditAsset(){
  const w = State.wizard;
  const err = validateWizardForSubmit(w);
  if (err){ Toast.show(err, "error"); return; }
  if (w._submitting) return;
  w._submitting = true;

  const button = document.getElementById("wiz-submit");
  if (button){ button.disabled = true; button.textContent = "Saving…"; }
  try {
    const data = await Api.put("/api/assets/" + encodeURIComponent(w._editingAssetId), wizardPayload(w));
    const wasReviewer = isReviewer();
    State.wizard = null;
    Toast.show(Util.stripWgtPrefix(data.asset.name) + " updated", "success");
    setView(wasReviewer ? "admin" : "my");
  } catch (error){
    w._submitting = false;
    if (!applyServerFieldErrors(error)) reportError(error, "Couldn't save those changes.");
  } finally {
    if (button){ button.disabled = false; button.textContent = "Save Changes"; }
    if (State.wizard) State.wizard._submitting = false;
  }
}

/* ============================================================================
   9. MY SUBMISSIONS
   The prototype only mentioned this view in a comment — it was never built.
   It is backed by GET /api/my-submissions, which scopes rows to the signed-in
   user on the server; there is no client-side filtering to bypass.
   ============================================================================ */
async function renderMySubmissionsView(){
  const data = await Api.get("/api/my-submissions", { q: State.my.query.trim() });
  State.my.result = data;

  const order = ["Pending Review", "Approved", "Rejected", "Deprecated", "Archived"];
  const buckets = data.buckets || {};

  const bucketHtml = order.map(status => {
    const items = buckets[status] || [];
    if (!items.length) return "";
    return `
    <div class="admin-section">
      <div class="admin-section-head">
        <h2>${h(status)}</h2>
        <span class="help-text">${h(items.length)} asset${items.length === 1 ? "" : "s"}</span>
      </div>
      <div class="table-wrap"><table class="data-table">
        <caption class="sr-only">${h(status)} submissions</caption>
        <thead><tr><th scope="col">WGT ID</th><th scope="col">Asset Name</th>
          <th scope="col">Type</th><th scope="col">Department</th>
          <th scope="col">Updated</th><th scope="col">Version</th>
          <th scope="col">Actions</th></tr></thead>
        <tbody>
          ${items.map(a => `
          <tr>
            <td class="mono-cell">${h(a.wgtCode)}</td>
            <td><span class="row-name" data-open-detail="${h(a.id)}" tabindex="0" role="button">${
              h(Util.stripWgtPrefix(a.name))}</span></td>
            <td>${h(Util.typeMeta(a.type).label)}</td>
            <td>${h(a.department)}</td>
            <td>${h(Util.formatDateShort(a.lastUpdated))}</td>
            <td>v${h(a.currentVersion)}</td>
            <td class="table-actions">
              <button class="btn btn-ghost btn-sm" data-open-detail="${h(a.id)}">View</button>
              ${a.canEdit ? `<button class="btn btn-secondary btn-sm" data-edit-asset="${h(a.id)}">Edit</button>` : ""}
            </td>
          </tr>`).join("")}
        </tbody>
      </table></div>
    </div>`;
  }).join("");

  document.getElementById("view-root").innerHTML = `
  <div class="page-head">
    <h1>My Submissions</h1>
    <p class="sub">Everything you've submitted or own, grouped by where it is in the
      review process.</p>
  </div>
  <div class="search-bar">
    ${Icon.search}
    <input type="text" id="my-search" placeholder="Search your submissions..."
           value="${h(State.my.query)}" aria-label="Search your submissions">
  </div>
  <div class="toolbar-row">
    <span class="result-count">${h(data.total)} submission${data.total === 1 ? "" : "s"}</span>
  </div>
  ${bucketHtml || `<div class="empty-state">
    <div class="em-icon">${Icon.addSquare}</div>
    <h3>You haven't submitted anything yet</h3>
    <p>Register a GPT, Claude Skill, Copilot Agent, or any other automation you've built.</p>
    <button class="btn btn-primary" id="my-empty-add">Add to Library</button>
    <div class="empty-note">nothing here yet — but that's easy to fix ✈</div>
  </div>`}`;

  const search = document.getElementById("my-search");
  if (search){
    search.addEventListener("input", Util.debounce(async e => {
      State.my.query = e.target.value;
      await render();
      const again = document.getElementById("my-search");
      if (again){ again.focus(); again.selectionStart = again.selectionEnd = again.value.length; }
    }, 220));
  }
  const emptyAdd = document.getElementById("my-empty-add");
  if (emptyAdd) emptyAdd.onclick = startNewSubmission;
  document.querySelectorAll("[data-edit-asset]").forEach(b =>
    b.onclick = () => startEditAsset(Number(b.dataset.editAsset)));
  wireOpenHandlers(document);
}

/* ============================================================================
   10. ADMIN / REVIEW
   ============================================================================ */
async function renderAdminView(){
  if (!isReviewer()){
    document.getElementById("view-root").innerHTML = `
      <div class="empty-state"><h3>Reviewer access required</h3>
      <p>Sign in with a reviewer or administrator account to view the review queue.</p>
      <button class="btn btn-primary" id="admin-signin">Sign in</button></div>`;
    const btn = document.getElementById("admin-signin");
    if (btn) btn.onclick = () => openLoginModal(() => setView("admin"));
    return;
  }

  const tab = State.admin.tab;
  const [stats, pending] = await Promise.all([
    Api.get("/api/admin/stats"),
    Api.get("/api/assets", { status: "Pending Review", per_page: 100, sort: "created_at" }),
  ]);
  State.admin.stats = stats;
  State.admin.pending = pending;
  _pendingCount = pending.total || 0;

  let manage = { items: [], total: 0 };
  const query = State.admin.librarySearch.trim();
  if (tab === "manage" && query){
    manage = await Api.get("/api/assets", { status: "Approved", q: query, per_page: 50 });
  }
  State.admin.manage = manage;

  let activity = { items: [], total: 0 };
  if (tab === "activity"){
    activity = await Api.get("/api/admin/activity", { per_page: 50 });
    State.admin.activity = activity;
  }

  document.getElementById("view-root").innerHTML = `
  <div class="page-head">
    <h1>Admin / Review</h1>
    <p class="sub">Approve or reject submissions before they appear in the Library.</p>
  </div>
  <div class="review-note">${Icon.info} Every action here is recorded in the activity log
    against your account and must reflect a real human decision.</div>

  <div class="kpi-grid">
    <div class="kpi-card"><div class="kpi-num">${h(stats.statusCounts["Pending Review"] || 0)}</div>
      <div class="kpi-label">Pending review</div></div>
    <div class="kpi-card"><div class="kpi-num">${h(stats.statusCounts["Approved"] || 0)}</div>
      <div class="kpi-label">Live in the Library</div></div>
    <div class="kpi-card ${stats.reviewDue ? "warn" : ""}"><div class="kpi-num">${h(stats.reviewDue)}</div>
      <div class="kpi-label">Review date passed</div></div>
    <div class="kpi-card ${stats.missingOwner ? "warn" : ""}"><div class="kpi-num">${h(stats.missingOwner)}</div>
      <div class="kpi-label">Missing an owner</div></div>
    <div class="kpi-card"><div class="kpi-num">${h(stats.totalFiles)}</div>
      <div class="kpi-label">Attached files</div></div>
  </div>

  <div class="bucket-tabs" role="tablist" aria-label="Admin sections">
    ${[["pending","Pending Reviews"],["manage","Manage Library"],["activity","Activity Log"]]
      .map(([id,label]) => `<button class="bucket-tab ${tab === id ? "active" : ""}" role="tab"
        aria-selected="${tab === id}" data-admin-tab="${h(id)}">${h(label)}</button>`).join("")}
  </div>

  ${tab === "pending" ? renderAdminPending(pending) : ""}
  ${tab === "manage" ? renderAdminManage(manage, query) : ""}
  ${tab === "activity" ? renderAdminActivity(activity) : ""}

  ${isAdmin() ? `<div class="admin-footer-actions">
    <button class="btn btn-secondary btn-sm" id="open-change-password">Change my password</button>
  </div>` : ""}`;

  wireAdminEvents();
}

function renderAdminPending(pending){
  const rows = pending.items || [];
  return `
  <div class="admin-section">
    <div class="admin-section-head"><h2>Pending Reviews</h2>
      <span class="help-text">${h(pending.total)} awaiting decision</span></div>
    <div class="table-wrap"><table class="data-table">
      <caption class="sr-only">Submissions awaiting a governance decision</caption>
      <thead><tr><th scope="col">WGT ID</th><th scope="col">Asset Name</th><th scope="col">Type</th>
        <th scope="col">Department</th><th scope="col">Tags</th><th scope="col">Creator / Owner</th>
        <th scope="col">Submitted</th><th scope="col">Actions</th></tr></thead>
      <tbody>
        ${rows.length ? rows.map(a => `
          <tr>
            <td class="mono-cell">${h(a.wgtCode)}</td>
            <td><span class="row-name" data-open-detail="${h(a.id)}" tabindex="0" role="button">${
              h(Util.stripWgtPrefix(a.name))}</span></td>
            <td>${h(Util.typeMeta(a.type).label)}</td>
            <td>${h(a.department)}</td>
            <td>${(a.tags || []).map(t => `<span class="chip tag-chip">${h(t)}</span>`).join(" ") || "—"}</td>
            <td>${h(a.owner || "—")}<div class="row-sub">${h(a.ownerEmail || "—")}</div></td>
            <td>${h(Util.formatDateShort(a.submissionDate))}</td>
            <td class="table-actions">
              <button class="btn btn-ghost btn-sm" data-open-detail="${h(a.id)}">Review</button>
              <button class="btn btn-primary btn-sm" data-approve="${h(a.id)}">Approve</button>
              <button class="btn btn-danger-ghost btn-sm" data-reject="${h(a.id)}">Reject</button>
            </td>
          </tr>`).join("")
          : `<tr><td colspan="8" class="help-text">Nothing pending — the queue is clear.</td></tr>`}
      </tbody>
    </table></div>
  </div>`;
}

function renderAdminManage(manage, query){
  const rows = manage.items || [];
  return `
  <div class="admin-section">
    <div class="admin-section-head"><h2>Manage Library</h2>
      <span class="help-text">${h(State.admin.stats.statusCounts["Approved"] || 0)} live in the Library</span></div>
    <div class="hint" style="margin-bottom:10px;">Search by WGT ID or name to find a
      published asset and take it down.</div>
    <label class="sr-only" for="admin-library-search">Search published assets</label>
    <input class="input" id="admin-library-search" placeholder="e.g. WGT201 or Financial Commentary"
           value="${h(State.admin.librarySearch)}" style="max-width:360px;margin-bottom:14px;">
    <div class="table-wrap"><table class="data-table">
      <caption class="sr-only">Published assets matching your search</caption>
      <thead><tr><th scope="col">WGT ID</th><th scope="col">Asset Name</th><th scope="col">Type</th>
        <th scope="col">Department</th><th scope="col">Owner</th><th scope="col">Actions</th></tr></thead>
      <tbody>
        ${rows.length ? rows.map(a => `
          <tr>
            <td class="mono-cell">${h(a.wgtCode)}</td>
            <td><span class="row-name" data-open-detail="${h(a.id)}" tabindex="0" role="button">${
              h(Util.stripWgtPrefix(a.name))}</span></td>
            <td>${h(Util.typeMeta(a.type).label)}</td>
            <td>${h(a.department)}</td>
            <td>${h(a.owner || "—")}</td>
            <td class="table-actions">
              <button class="btn btn-ghost btn-sm" data-open-detail="${h(a.id)}">Review</button>
              <button class="btn btn-secondary btn-sm" data-edit-asset="${h(a.id)}">Edit</button>
              <button class="btn btn-danger-ghost btn-sm" data-remove-live="${h(a.id)}">Remove from Library</button>
            </td>
          </tr>`).join("")
          : `<tr><td colspan="6" class="help-text">${query
              ? "No published assets match that search."
              : "Type a WGT ID or name above to find a published asset."}</td></tr>`}
      </tbody>
    </table></div>
  </div>`;
}

function renderAdminActivity(activity){
  const rows = activity.items || [];
  return `
  <div class="admin-section">
    <div class="admin-section-head"><h2>Activity Log</h2>
      <span class="help-text">${h(activity.total)} recorded events</span></div>
    <div class="hint" style="margin-bottom:10px;">Every login, submission, approval,
      rejection, edit, upload, download and password change is recorded here.</div>
    <div class="table-wrap"><table class="data-table">
      <caption class="sr-only">Audit trail</caption>
      <thead><tr><th scope="col">When</th><th scope="col">Action</th><th scope="col">Who</th>
        <th scope="col">Object</th><th scope="col">Detail</th></tr></thead>
      <tbody>
        ${rows.length ? rows.map(e => `
          <tr>
            <td>${h(Util.formatTimestamp(e.ts))}</td>
            <td class="mono-cell">${h(e.action)}</td>
            <td>${h(e.actor || "—")}</td>
            <td>${h(e.objectLabel || (e.objectType ? e.objectType + " " + e.objectId : "—"))}</td>
            <td class="row-sub">${h(summariseDetail(e.detail))}</td>
          </tr>`).join("")
          : `<tr><td colspan="5" class="help-text">No activity recorded yet.</td></tr>`}
      </tbody>
    </table></div>
  </div>`;
}

function summariseDetail(detail){
  if (!detail || typeof detail !== "object") return "—";
  const parts = Object.keys(detail).slice(0,4).map(key => {
    const value = detail[key];
    const rendered = (value && typeof value === "object") ? JSON.stringify(value) : String(value);
    return key + ": " + rendered.slice(0, 60);
  });
  return parts.join(" · ") || "—";
}

function wireAdminEvents(){
  document.querySelectorAll("[data-admin-tab]").forEach(b => b.onclick = () => {
    State.admin.tab = b.dataset.adminTab; render();
  });

  document.querySelectorAll("[data-approve]").forEach(b =>
    b.onclick = () => approveAsset(Number(b.dataset.approve), render));
  document.querySelectorAll("[data-reject]").forEach(b =>
    b.onclick = () => rejectAssetWithConfirm(Number(b.dataset.reject), render));
  document.querySelectorAll("[data-remove-live]").forEach(b =>
    b.onclick = () => removeFromLibraryWithConfirm(Number(b.dataset.removeLive), render));
  document.querySelectorAll("[data-edit-asset]").forEach(b =>
    b.onclick = () => startEditAsset(Number(b.dataset.editAsset)));

  const search = document.getElementById("admin-library-search");
  if (search){
    search.addEventListener("input", Util.debounce(async e => {
      State.admin.librarySearch = e.target.value;
      await render();
      const again = document.getElementById("admin-library-search");
      if (again){ again.focus(); again.selectionStart = again.selectionEnd = again.value.length; }
    }, 250));
  }

  const pw = document.getElementById("open-change-password");
  if (pw) pw.onclick = openChangePasswordModal;

  wireOpenHandlers(document);
}

/* ---- shared governance actions ------------------------------------------
   Each posts to its own endpoint; the server checks the reviewer role again
   and records the decision in the activity log. -------------------------- */
async function approveAsset(id, onDone){
  try {
    const data = await Api.post("/api/assets/" + encodeURIComponent(id) + "/approve");
    Toast.show(Util.stripWgtPrefix(data.asset.name) + " approved and published", "success");
    await refreshPendingCount();
    if (typeof onDone === "function") onDone();
  } catch (error){ reportError(error, "Approve failed."); }
}

function rejectAssetWithConfirm(id, onDone){
  const asset = findLoadedAsset(id) || {};
  showModal("Reject submission", `
    <p class="body-text">Reject "${h(Util.stripWgtPrefix(asset.name || "this submission"))}"?
      It will be removed from the review queue and will not appear in the Library.
      Contact the Creator/Owner (${h(asset.ownerEmail || "no email on record")}) separately
      to resolve any issues — no automated email is sent.</p>
    <div class="field"><label for="reject-reason">Reason (recorded in the audit log)</label>
      <textarea class="textarea" id="reject-reason" placeholder="Why is this being rejected?"></textarea></div>
  `, [
    { label:"Cancel", cls:"btn-ghost", onClick: closeModal },
    { label:"Reject", cls:"btn-danger", onClick: async () => {
      const reason = (document.getElementById("reject-reason") || {}).value || "";
      try {
        const data = await Api.post("/api/assets/" + encodeURIComponent(id) + "/reject",
          { reason: reason.trim() });
        Toast.show(Util.stripWgtPrefix(data.asset.name) + " rejected", "info");
        await refreshPendingCount();
      } catch (error){ reportError(error, "Reject failed."); }
      finally { closeModal(); if (typeof onDone === "function") onDone(); }
    }},
  ]);
}

function removeFromLibraryWithConfirm(id, onDone){
  const asset = findLoadedAsset(id) || {};
  showModal("Remove from Library", `
    <p class="body-text">Remove "${h(Util.stripWgtPrefix(asset.name || "this asset"))}"
      (${h(asset.wgtCode || "")}) from the Library? It will no longer be visible to anyone
      browsing, but its record and history are kept.</p>
    <div class="field"><label for="archive-reason">Reason (recorded in the audit log)</label>
      <textarea class="textarea" id="archive-reason" placeholder="Why is this being taken down?"></textarea></div>
  `, [
    { label:"Cancel", cls:"btn-ghost", onClick: closeModal },
    { label:"Remove from Library", cls:"btn-danger", onClick: async () => {
      const reason = (document.getElementById("archive-reason") || {}).value || "";
      try {
        const data = await Api.post("/api/assets/" + encodeURIComponent(id) + "/archive",
          { reason: reason.trim() });
        Toast.show(Util.stripWgtPrefix(data.asset.name) + " removed from the Library", "info");
      } catch (error){ reportError(error, "Remove failed."); }
      finally { closeModal(); if (typeof onDone === "function") onDone(); }
    }},
  ]);
}

/* ============================================================================
   11. INIT
   ============================================================================ */
function wireGlobalChrome(){
  const addBtn = document.getElementById("topbar-add-btn");
  if (addBtn) addBtn.addEventListener("click", () => {
    if (!isAuthenticated()){ openLoginModal(startNewSubmission); return; }
    startNewSubmission();
  });

  document.getElementById("detail-overlay").addEventListener("click", e => {
    if (e.target.id === "detail-overlay") closeDetail();
  });
  document.getElementById("modal-overlay").addEventListener("click", e => {
    if (e.target.id === "modal-overlay") closeModal();
  });

  document.addEventListener("keydown", e => {
    if (e.key === "Escape"){
      if (document.getElementById("modal-overlay").classList.contains("open")) closeModal();
      else if (document.getElementById("detail-overlay").classList.contains("open")) closeDetail();
    }
    if (e.key === "/" && State.view === "library" &&
        document.activeElement.tagName !== "INPUT" &&
        document.activeElement.tagName !== "TEXTAREA" &&
        !document.getElementById("modal-overlay").classList.contains("open")){
      const s = document.getElementById("library-search");
      if (s){ e.preventDefault(); s.focus(); }
    }
  });

  if (window.matchMedia){
    const mq = window.matchMedia("(max-width: 860px)");
    const toggle = document.getElementById("mobile-nav-toggle");
    const sync = () => { toggle.style.display = mq.matches ? "flex" : "none"; };
    sync();
    if (mq.addEventListener) mq.addEventListener("change", sync);
    else if (mq.addListener) mq.addListener(sync);
  }
  const navToggle = document.getElementById("mobile-nav-toggle");
  if (navToggle) navToggle.addEventListener("click", () => {
    const sidebar = document.getElementById("sidebar");
    const open = sidebar.classList.toggle("open");
    navToggle.setAttribute("aria-expanded", String(open));
  });
}

async function initApp(){
  // Global safety net: surface unexpected failures instead of letting a click
  // silently do nothing.
  window.addEventListener("error", e => {
    console.error("Unhandled error:", e.error || e.message);
    try { Toast.show("Something went wrong: " +
      ((e.error && e.error.message) || e.message), "error"); } catch(_){}
  });
  window.addEventListener("unhandledrejection", e => {
    console.error("Unhandled promise rejection:", e.reason);
    const reason = e.reason;
    // ApiError rejections are already reported to the user by reportError().
    if (reason instanceof ApiError) return;
    try { Toast.show("Something went wrong: " +
      ((reason && reason.message) || reason), "error"); } catch(_){}
  });

  try {
    CONFIG = await Api.get("/api/config");
  } catch (error){
    document.getElementById("view-root").innerHTML = `
      <div class="empty-state"><h3>Couldn't reach the Agent Library service</h3>
      <p>The catalogue configuration failed to load. Refresh the page, or contact the
         Tech team if this persists.</p></div>`;
    return;
  }

  await refreshSession();
  renderSidebarNav();
  renderTopbarActions();
  wireGlobalChrome();

  window.addEventListener("hashchange", () => {
    if (_suppressHashChange){ _suppressHashChange = false; return; }
    applyViewFromHash();
  });

  applyViewFromHash();
  refreshPendingCount();
}

document.addEventListener("DOMContentLoaded", initApp);

/* ----------------------------------------------------------------------------
   Debug surface. Read-only conveniences for a developer at the console. It
   deliberately exposes no credentials, no tokens and no mutation helpers —
   every write still has to go through the API with a valid session and CSRF
   token, which the server checks.
   -------------------------------------------------------------------------- */
window.AgentLibrary = {
  get config(){ return CONFIG; },
  get state(){ return State; },
  Util,
  version: BOOT.version,
};
