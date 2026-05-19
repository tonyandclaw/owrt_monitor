package main

import (
	"io"
	"log"
	"net/http"
)

func (s *server) handleDashboard(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodGet {
		writeJSON(w, http.StatusMethodNotAllowed, errorResponse{Error: "GET only"})
		return
	}
	switch r.URL.Path {
	case "/":
		http.Redirect(w, r, "/ui/", http.StatusFound)
	case "/ui":
		http.Redirect(w, r, "/ui/", http.StatusMovedPermanently)
	case "/ui/":
		serveDashboardAsset(w, "text/html; charset=utf-8", dashboardHTML)
	case "/ui/styles.css":
		serveDashboardAsset(w, "text/css; charset=utf-8", dashboardCSS)
	case "/ui/app.js":
		serveDashboardAsset(w, "application/javascript; charset=utf-8", dashboardJS)
	default:
		http.NotFound(w, r)
	}
}

func serveDashboardAsset(w http.ResponseWriter, contentType string, body string) {
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Cache-Control", "no-store")
	w.Header().Set("X-Content-Type-Options", "nosniff")
	w.Header().Set("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'")
	w.WriteHeader(http.StatusOK)
	if _, err := io.WriteString(w, body); err != nil {
		log.Printf("dashboard asset write: %v", err)
	}
}

// handleLocks reads `<artifactsDir>/locks.json` and surfaces the current
// DUT + builder lock state. Python writes the same snapshot while it remains
// the workflow engine; the Go mutation endpoints below use the same shape so

const dashboardHTML = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>owrtd jobs</title>
  <link rel="stylesheet" href="/ui/styles.css">
</head>
<body>
  <main class="shell">
    <nav class="globalnav" aria-label="Dashboard navigation">
      <span class="nav-brand">owrtd</span>
      <button class="nav-link" data-jump="jobs" type="button">Jobs</button>
      <button class="nav-link" data-jump="analysis" type="button">Analysis</button>
      <button class="nav-link" data-jump="logs" type="button">Logs</button>
    </nav>
    <header class="topbar">
      <div class="brand">
        <p class="eyebrow">OpenWrt Lab</p>
        <h1>owrtd jobs</h1>
        <p id="apiStatus" class="api-line"><span class="status-dot"></span><span>Loading</span></p>
      </div>
      <div class="toolbar">
        <label class="toggle"><input id="autoRefresh" type="checkbox" checked><span>Auto refresh</span></label>
        <button id="refreshBtn" class="primary-action" type="button">Refresh</button>
      </div>
    </header>
    <section id="overview" class="overview" aria-label="Job summary"></section>
    <section class="layout" aria-label="Job dashboard">
      <div class="job-pane">
        <div class="pane-head">
          <div>
            <p class="eyebrow">Monitor</p>
            <h2>Recent Jobs</h2>
          </div>
          <span id="jobCount" class="pill">0</span>
        </div>
        <div class="job-controls">
          <label class="search">
            <span>Search</span>
            <input id="jobSearch" type="search" placeholder="Job, state, result">
          </label>
          <div class="segmented" role="tablist" aria-label="Job filter">
            <button class="segment active" data-filter="all" type="button">All</button>
            <button class="segment" data-filter="attention" type="button">Attention</button>
            <button class="segment" data-filter="success" type="button">Success</button>
            <button class="segment" data-filter="dry-run" type="button">Dry-run</button>
          </div>
        </div>
        <div class="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Job</th>
                <th>State</th>
                <th>Result</th>
                <th>Started</th>
                <th>Remove</th>
              </tr>
            </thead>
            <tbody id="jobRows">
              <tr><td colspan="5" class="empty">Loading jobs</td></tr>
            </tbody>
          </table>
        </div>
      </div>
      <div class="detail-pane">
        <div class="pane-head">
          <div>
            <p class="eyebrow">Selected job</p>
            <h2 id="detailTitle">Select a job</h2>
          </div>
          <span id="detailState" class="pill">idle</span>
        </div>
        <div id="summaryGrid" class="summary-grid"></div>
        <div class="tabs" role="tablist" aria-label="Job details">
          <button class="tab active" data-tab="analysis" type="button">Analysis</button>
          <button class="tab" data-tab="report" type="button">Report</button>
          <button class="tab" data-tab="runner" type="button">Runner</button>
          <button class="tab" data-tab="logs" type="button">Logs</button>
        </div>
        <section id="analysisPanel" class="panel active" aria-label="Analysis"></section>
        <section id="reportPanel" class="panel" aria-label="Report"></section>
        <section id="runnerPanel" class="panel" aria-label="Runner"></section>
        <section id="logsPanel" class="panel" aria-label="Logs"></section>
      </div>
    </section>
  </main>
  <script src="/ui/app.js"></script>
</body>
</html>
`

const dashboardCSS = `:root {
  color-scheme: light;
  --bg: #f5f5f7;
  --surface: #ffffff;
  --surface-soft: #fbfbfd;
  --line: #d2d2d7;
  --line-strong: #b9b9bf;
  --text: #1d1d1f;
  --muted: #6e6e73;
  --accent: #0071e3;
  --accent-strong: #005bb5;
  --accent-soft: #e8f2ff;
  --ok: #248a3d;
  --warn: #bf7c00;
  --bad: #b42318;
  --planned: #5856d6;
  --shadow: 0 18px 42px rgba(0, 0, 0, 0.06);
}

* { box-sizing: border-box; }

body {
  margin: 0;
  background: var(--bg);
  color: var(--text);
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", system-ui, sans-serif;
}

button, input { font: inherit; }
h1, h2, h3, p { margin: 0; }

.shell {
  width: min(1500px, 100%);
  min-height: 100vh;
  margin: 0 auto;
  padding: 14px 22px 24px;
}

.globalnav {
  position: sticky;
  top: 0;
  z-index: 5;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 34px;
  min-height: 44px;
  margin: -14px -22px 16px;
  color: rgba(29, 29, 31, 0.8);
  background: rgba(245, 245, 247, 0.82);
  border-bottom: 1px solid rgba(0, 0, 0, 0.08);
  backdrop-filter: saturate(180%) blur(18px);
}

.nav-brand {
  color: var(--text);
  font-size: 15px;
  font-weight: 760;
}

.nav-link {
  min-height: 28px;
  border: 0;
  background: transparent;
  border-radius: 7px;
  padding: 4px 6px;
  color: rgba(29, 29, 31, 0.8);
  font-size: 12px;
  white-space: nowrap;
}

.nav-link:hover,
.nav-link:focus-visible {
  color: var(--text);
  border-color: transparent;
  outline: none;
  background: rgba(0, 0, 0, 0.05);
}

.topbar,
.job-pane,
.detail-pane {
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: var(--shadow);
}

.topbar {
  display: flex;
  justify-content: space-between;
  gap: 18px;
  align-items: center;
  min-height: 168px;
  margin-bottom: 16px;
  padding: 34px 42px;
  text-align: left;
}

.brand {
  display: grid;
  gap: 4px;
}

.eyebrow {
  color: var(--muted);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0;
  text-transform: uppercase;
}

h1 {
  font-size: 54px;
  line-height: 1.02;
  font-weight: 760;
  letter-spacing: 0;
}

h2 {
  font-size: 16px;
  line-height: 1.2;
  font-weight: 760;
}

.muted,
.api-line {
  color: var(--muted);
  margin-top: 4px;
}

.api-line {
  display: flex;
  align-items: center;
  gap: 8px;
}

.api-line.error { color: var(--bad); }

.status-dot {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  background: var(--ok);
  box-shadow: 0 0 0 4px rgba(36, 138, 61, 0.12);
  flex: 0 0 auto;
}

.api-line.error .status-dot {
  background: var(--bad);
  box-shadow: 0 0 0 4px rgba(180, 35, 24, 0.12);
}

.toolbar,
.pane-head,
.tabs {
  display: flex;
  align-items: center;
  gap: 10px;
}

.toolbar {
  flex-wrap: wrap;
  justify-content: flex-end;
}

button {
  min-height: 36px;
  border: 1px solid var(--line-strong);
  background: var(--surface);
  color: var(--text);
  border-radius: 7px;
  padding: 7px 12px;
  cursor: pointer;
}

button:hover,
button:focus-visible,
input:focus-visible {
  border-color: var(--accent);
  outline: 3px solid rgba(0, 113, 227, 0.16);
  outline-offset: 1px;
}

.primary-action {
  color: #ffffff;
  background: var(--accent);
  border-color: var(--accent);
  font-weight: 740;
}

.primary-action:hover,
.primary-action:focus-visible {
  background: var(--accent-strong);
}

.toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  min-height: 36px;
  color: var(--muted);
  padding: 0 4px;
  white-space: nowrap;
}

.toggle input {
  width: 16px;
  height: 16px;
  accent-color: var(--accent);
}

.overview {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 16px;
  margin-bottom: 16px;
}

.metric {
  min-height: 124px;
  padding: 21px 22px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: 8px;
  box-shadow: var(--shadow);
}

.metric-label {
  color: var(--muted);
  font-size: 13px;
  font-weight: 650;
}

.metric-value {
  margin-top: 7px;
  font-size: 42px;
  line-height: 1;
  font-weight: 740;
}

.metric-note {
  margin-top: 6px;
  color: var(--muted);
  font-size: 12px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.metric.ok .metric-value { color: var(--ok); }
.metric.warn .metric-value { color: var(--warn); }
.metric.bad .metric-value { color: var(--bad); }
.metric.planned .metric-value { color: var(--planned); }

.layout {
  display: grid;
  grid-template-columns: minmax(430px, 0.9fr) minmax(680px, 1.5fr);
  gap: 18px;
  align-items: start;
}

.job-pane,
.detail-pane {
  min-width: 0;
  overflow: hidden;
}

.pane-head {
  justify-content: space-between;
  min-height: 72px;
  padding: 17px 20px;
  border-bottom: 1px solid var(--line);
  background: var(--surface);
}

.job-controls {
  display: grid;
  gap: 10px;
  padding: 14px 18px 16px;
  border-bottom: 1px solid var(--line);
  background: var(--surface-soft);
}

.search {
  display: grid;
  gap: 5px;
  color: var(--muted);
  font-size: 11px;
  font-weight: 800;
  text-transform: uppercase;
}

.search input {
  width: 100%;
  min-height: 40px;
  border: 0;
  border-radius: 7px;
  padding: 8px 12px;
  color: var(--text);
  background: #ffffff;
  box-shadow: inset 0 0 0 1px var(--line);
  text-transform: none;
}

.segmented {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 4px;
}

.segment {
  min-height: 34px;
  padding: 5px 8px;
  color: var(--muted);
  border-color: transparent;
  background: transparent;
  font-size: 12px;
  font-weight: 760;
}

.segment.active {
  color: var(--text);
  border-color: var(--line);
  background: #ffffff;
  box-shadow: 0 1px 4px rgba(0, 0, 0, 0.06);
}

.table-wrap {
  overflow: auto;
  max-height: calc(100vh - 315px);
}

table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}

thead th {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #ffffff;
}

th,
td {
  padding: 13px 16px;
  border-bottom: 1px solid var(--line);
  text-align: left;
  vertical-align: middle;
}

th:nth-child(1),
td:nth-child(1) { width: 31%; }
th:nth-child(2),
td:nth-child(2) { width: 17%; white-space: nowrap; }
th:nth-child(3),
td:nth-child(3) { width: 18%; }
th:nth-child(4),
td:nth-child(4) { width: 16%; }
th:nth-child(5),
td:nth-child(5) { width: 18%; text-align: right; white-space: nowrap; }

th {
  color: var(--muted);
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0;
  text-transform: uppercase;
}

td {
  overflow-wrap: anywhere;
  background: #ffffff;
}

tbody tr:hover td { background: #fbfcfb; }
tr.selected td { background: #f5faff; }
tr.selected td:first-child { box-shadow: inset 3px 0 0 var(--accent); }

.empty {
  color: var(--muted);
  text-align: center;
  padding: 32px;
}

.job-link {
  width: 100%;
  min-height: 34px;
  border: 0;
  background: transparent;
  padding: 0;
  text-align: left;
  color: var(--text);
  font-weight: 760;
}

.job-link:hover,
.job-link:focus-visible {
  outline: none;
  color: var(--accent);
  text-decoration: underline;
  text-decoration-thickness: 2px;
  text-underline-offset: 3px;
}

.job-id {
  display: block;
  line-height: 1.25;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.remove-button {
  min-height: 30px;
  border-color: transparent;
  border-radius: 999px;
  padding: 4px 10px;
  color: var(--bad);
  background: transparent;
  font-size: 12px;
  font-weight: 760;
  white-space: nowrap;
}

.remove-button:hover,
.remove-button:focus-visible {
  outline: none;
  border-color: #efaaa4;
  background: #fff1ef;
}

.pill {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-height: 25px;
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 3px 9px;
  color: var(--muted);
  background: #ffffff;
  font-size: 12px;
  font-weight: 760;
  white-space: nowrap;
}

.pill.ok { color: var(--ok); border-color: #9fceaf; background: #eef8f1; }
.pill.warn { color: var(--warn); border-color: #dfbf7d; background: #fff7e8; }
.pill.bad { color: var(--bad); border-color: #efaaa4; background: #fff1ef; }
.pill.planned { color: var(--planned); border-color: #c4c3f3; background: #f4f4ff; }

.summary-grid {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  border-bottom: 1px solid var(--line);
  background: var(--surface-soft);
}

.summary-item {
  min-height: 96px;
  padding: 18px 20px;
  background: #ffffff;
  border-right: 1px solid var(--line);
}

.summary-item:last-child { border-right: 0; }

.summary-label {
  color: var(--muted);
  font-size: 11px;
  font-weight: 800;
  margin-bottom: 6px;
  text-transform: uppercase;
}

.summary-value {
  font-weight: 760;
  overflow-wrap: anywhere;
}

.tabs {
  gap: 6px;
  padding: 12px 14px;
  border-bottom: 1px solid var(--line);
  background: #ffffff;
}

.tab {
  min-height: 34px;
  color: var(--muted);
  border-color: transparent;
  background: transparent;
  font-weight: 720;
}

.tab.active {
  color: var(--text);
  border-color: var(--line);
  background: var(--surface-soft);
}

.panel {
  display: none;
  min-height: 360px;
  padding: 16px;
}

.panel.active { display: block; }

.kv {
  display: grid;
  grid-template-columns: 180px minmax(0, 1fr);
  gap: 8px 14px;
  align-items: baseline;
}

.kv div:nth-child(odd) {
  color: var(--muted);
  font-size: 12px;
  font-weight: 760;
}

.kv div:nth-child(even) {
  overflow-wrap: anywhere;
}

.list {
  margin: 8px 0 16px;
  padding-left: 20px;
}

.list li { margin: 5px 0; }

pre {
  margin: 0;
  padding: 13px 14px;
  max-height: 500px;
  overflow: auto;
  background: #10171d;
  color: #f4f7f6;
  border: 1px solid #25313a;
  border-radius: 7px;
  font: 12px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}

.split {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

.section-title {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--muted);
  font-weight: 800;
  text-transform: uppercase;
}

@media (max-width: 1120px) {
  .overview { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .layout { grid-template-columns: 1fr; }
  .table-wrap { max-height: 420px; }
}

@media (max-width: 760px) {
  .shell { padding: 12px; }
  .globalnav {
    gap: 22px;
    margin: -12px -12px 12px;
  }
  .topbar {
    align-items: start;
    flex-direction: column;
    min-height: 0;
    padding: 24px 18px;
  }
  h1 { font-size: 38px; }
  .toolbar { justify-content: flex-start; }
  .summary-grid,
  .split { grid-template-columns: 1fr; }
  .metric {
    min-height: 112px;
  }
  .segmented { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .summary-item {
    min-height: auto;
    border-right: 0;
    border-bottom: 1px solid var(--line);
  }
  .summary-item:last-child { border-bottom: 0; }
  .kv { grid-template-columns: 1fr; }
  th, td { padding: 9px 10px; }
  th:nth-child(1),
  td:nth-child(1) { width: 35%; }
  th:nth-child(2),
  td:nth-child(2) { width: 22%; }
  th:nth-child(3),
  td:nth-child(3) { width: 25%; }
  th:nth-child(4),
  td:nth-child(4) { display: none; }
  th:nth-child(5),
  td:nth-child(5) { width: 18%; padding-left: 4px; }
  .remove-button { padding: 4px 6px; }
}
`

const dashboardJS = `(function () {
  var state = { jobs: [], selected: null, autoTimer: null, filter: "all", search: "" };

  function byId(id) {
    return document.getElementById(id);
  }

  function escapeHTML(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function classForJob(job) {
    if (job && job.dry_run) return "planned";
    if (job && job.success === true) return "ok";
    if (job && job.success === false) return "bad";
    return "warn";
  }

  function classForSeverity(value) {
    if (value === "success") return "ok";
    if (value === "error" || value === "failed") return "bad";
    if (value === "planned" || value === "info") return "planned";
    return "warn";
  }

  function statusText(job) {
    if (!job) return "";
    if (job.dry_run) return "dry-run";
    if (job.success === true) return "success";
    if (job.success === false) return "failed";
    return "pending";
  }

  function displayState(value) {
    return String(value || "")
      .toLowerCase()
      .replace(/_/g, " ")
      .replace(/\b\w/g, function (letter) { return letter.toUpperCase(); });
  }

  function isAttention(job) {
    if (!job) return false;
    if (job.success === false) return true;
    var current = String(job.state || "").toUpperCase();
    return current === "FAILED" || current === "CANCELLED";
  }

  function matchesFilter(job) {
    if (state.filter === "attention") return isAttention(job);
    if (state.filter === "success") return job && job.success === true && !job.dry_run;
    if (state.filter === "dry-run") return job && job.dry_run === true;
    return true;
  }

  function matchesSearch(job) {
    var needle = state.search.trim().toLowerCase();
    if (!needle) return true;
    return [
      job.job_id,
      job.state,
      statusText(job),
      job.run_dir
    ].join(" ").toLowerCase().indexOf(needle) !== -1;
  }

  function filteredJobs() {
    return state.jobs.filter(function (job) {
      return matchesFilter(job) && matchesSearch(job);
    });
  }

  function jobCounts() {
    var attention = state.jobs.filter(isAttention).length;
    var success = state.jobs.filter(function (job) { return job.success === true && !job.dry_run; }).length;
    var dryRuns = state.jobs.filter(function (job) { return job.dry_run; }).length;
    return {
      total: state.jobs.length,
      attention: attention,
      success: success,
      dryRuns: dryRuns
    };
  }

  function formatDate(value) {
    if (!value) return "";
    var date = new Date(value);
    if (Number.isNaN(date.getTime())) return value;
    return date.toLocaleString([], {
      month: "short",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit"
    });
  }

  function plural(value, label) {
    return value === 1 ? value + " " + label : value + " " + label + "s";
  }

  function fetchJSON(path) {
    return fetch(path, { cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error(path + " " + response.status);
      return response.json();
    });
  }

  function fetchText(path) {
    return fetch(path, { cache: "no-store" }).then(function (response) {
      if (!response.ok) throw new Error(path + " " + response.status);
      return response.text();
    });
  }

  function renderOverview() {
    var counts = jobCounts();
    byId("overview").innerHTML = [
      metricItem("Total jobs", counts.total, "Last 50 from artifacts", ""),
      metricItem("Attention", counts.attention, counts.attention ? "Needs review" : "No blocking jobs", counts.attention ? "bad" : "ok"),
      metricItem("Succeeded", counts.success, plural(counts.success, "completed run"), "ok"),
      metricItem("Dry runs", counts.dryRuns, plural(counts.dryRuns, "planned run"), "planned")
    ].join("");
  }

  function metricItem(label, value, note, tone) {
    return '<div class="metric ' + escapeHTML(tone || "") + '">' +
      '<div class="metric-label">' + escapeHTML(label) + "</div>" +
      '<div class="metric-value">' + escapeHTML(value) + "</div>" +
      '<div class="metric-note">' + escapeHTML(note) + "</div>" +
      "</div>";
  }

  function renderJobs() {
    var rows = byId("jobRows");
    var visibleJobs = filteredJobs();
    byId("jobCount").textContent = visibleJobs.length === state.jobs.length ?
      String(state.jobs.length) :
      String(visibleJobs.length) + "/" + String(state.jobs.length);
    if (!state.jobs.length) {
      rows.innerHTML = '<tr><td colspan="5" class="empty">No jobs found</td></tr>';
      return;
    }
    if (!visibleJobs.length) {
      rows.innerHTML = '<tr><td colspan="5" class="empty">No matching jobs</td></tr>';
      return;
    }
    rows.innerHTML = visibleJobs.map(function (job) {
      var selected = job.job_id === state.selected ? ' class="selected"' : "";
      return "<tr" + selected + ">" +
        '<td><button class="job-link" data-job="' + escapeHTML(job.job_id) + '" title="' + escapeHTML(job.job_id) + '" type="button">' +
        '<span class="job-id">' + escapeHTML(job.job_id) + "</span></button></td>" +
        "<td>" + escapeHTML(displayState(job.state)) + "</td>" +
        '<td><span class="pill ' + classForJob(job) + '">' + escapeHTML(statusText(job)) + "</span></td>" +
        "<td>" + escapeHTML(formatDate(job.started_at)) + "</td>" +
        '<td><button class="remove-button" data-remove="' + escapeHTML(job.job_id) + '" type="button">Remove</button></td>' +
        "</tr>";
    }).join("");
    Array.prototype.forEach.call(rows.querySelectorAll("[data-job]"), function (button) {
      button.addEventListener("click", function () {
        state.selected = button.getAttribute("data-job");
        renderJobs();
        loadDetail(state.selected);
      });
    });
    Array.prototype.forEach.call(rows.querySelectorAll("[data-remove]"), function (button) {
      button.addEventListener("click", function () {
        removeJob(button.getAttribute("data-remove"));
      });
    });
  }

  function setStatus(message, ok) {
    var status = byId("apiStatus");
    status.innerHTML = '<span class="status-dot"></span><span>' + escapeHTML(message) + "</span>";
    status.className = ok ? "api-line" : "api-line error";
  }

  function loadJobs() {
    return fetchJSON("/v1/jobs?limit=50").then(function (jobs) {
      state.jobs = jobs || [];
      if (!state.selected && state.jobs.length) state.selected = state.jobs[0].job_id;
      if (state.selected && !state.jobs.some(function (job) { return job.job_id === state.selected; })) {
        state.selected = state.jobs.length ? state.jobs[0].job_id : null;
      }
      renderOverview();
      renderJobs();
      setStatus("Connected. " + state.jobs.length + " job(s).", true);
      if (state.selected) return loadDetail(state.selected);
      renderEmptyDetail();
      return null;
    }).catch(function (err) {
      setStatus("API error: " + err.message, false);
      byId("jobRows").innerHTML = '<tr><td colspan="5" class="empty">Could not load jobs</td></tr>';
    });
  }

  function removeJob(jobID) {
    if (!jobID) return Promise.resolve();
    if (!window.confirm("Remove job " + jobID + "? This deletes its run directory and logs.")) {
      return Promise.resolve();
    }
    var path = "/v1/jobs/" + encodeURIComponent(jobID);
    setStatus("Removing " + jobID + "...", true);
    return fetch(path, { method: "DELETE", cache: "no-store" }).then(function (response) {
      if (response.ok) return response.json();
      return response.json().catch(function () {
        return {};
      }).then(function (body) {
        throw new Error(body.error || path + " " + response.status);
      });
    }).then(function () {
      if (state.selected === jobID) {
        state.selected = null;
        renderEmptyDetail();
      }
      return loadJobs();
    }).catch(function (err) {
      setStatus("Remove failed: " + err.message, false);
      window.alert("Remove failed: " + err.message);
    });
  }

  function renderEmptyDetail() {
    byId("detailTitle").textContent = "Select a job";
    byId("detailState").textContent = "idle";
    byId("detailState").className = "pill";
    byId("summaryGrid").innerHTML = "";
    byId("analysisPanel").innerHTML = '<p class="muted">No job selected.</p>';
    byId("reportPanel").innerHTML = "";
    byId("runnerPanel").innerHTML = "";
    byId("logsPanel").innerHTML = "";
  }

  function settledValue(result) {
    return result && result.status === "fulfilled" ? result.value : null;
  }

  function loadDetail(jobID) {
    byId("detailTitle").textContent = jobID;
    byId("detailState").textContent = "loading";
    byId("detailState").className = "pill warn";
    return Promise.allSettled([
      fetchJSON("/v1/jobs/" + encodeURIComponent(jobID)),
      fetchJSON("/v1/jobs/" + encodeURIComponent(jobID) + "/runner"),
      fetchJSON("/v1/jobs/" + encodeURIComponent(jobID) + "/analysis"),
      fetchText("/v1/jobs/" + encodeURIComponent(jobID) + "/events"),
      fetchText("/v1/jobs/" + encodeURIComponent(jobID) + "/runner-output?tail=80")
    ]).then(function (results) {
      var report = settledValue(results[0]) || {};
      var runner = settledValue(results[1]);
      var analysis = settledValue(results[2]);
      var eventsText = settledValue(results[3]);
      var runnerText = settledValue(results[4]);
      renderDetail(jobID, report, runner, analysis, eventsText, runnerText);
    });
  }

  function renderDetail(jobID, report, runner, analysis, eventsText, runnerText) {
    var job = state.jobs.find(function (entry) { return entry.job_id === jobID; }) || {};
    var build = report.build_summary || {};
    var artifact = report.artifact || {};
    var metrics = report.metrics || {};
    var severity = analysis && analysis.ui_summary ? analysis.ui_summary.severity : statusText(job);

    byId("detailState").textContent = displayState(report.state || job.state || "unknown");
    byId("detailState").className = "pill " + classForSeverity(severity);
    byId("summaryGrid").innerHTML = [
      summaryItem("Result", statusText(job)),
      summaryItem("Build", displayState(build.classification || "n/a")),
      summaryItem("Artifact", artifact.filename || "n/a"),
      summaryItem("Duration", metrics.total_duration_sec ? Number(metrics.total_duration_sec).toFixed(2) + "s" : "n/a")
    ].join("");
    renderAnalysis(analysis);
    renderReport(report);
    renderRunner(runner);
    renderLogs(eventsText, runnerText);
  }

  function summaryItem(label, value) {
    return '<div class="summary-item"><div class="summary-label">' + escapeHTML(label) +
      '</div><div class="summary-value">' + escapeHTML(value) + "</div></div>";
  }

  function renderAnalysis(analysis) {
    if (!analysis) {
      byId("analysisPanel").innerHTML = '<p class="muted">No analysis.json for this job. Run owrt-monitor analyze &lt;job_id&gt; to create one.</p>';
      return;
    }
    var verdict = analysis.verdict || {};
    var guardrails = analysis.guardrails || {};
    var findings = analysis.findings || [];
    var actions = analysis.next_actions || [];
    var draft = analysis.bug_report_draft || null;
    byId("analysisPanel").innerHTML =
      '<div class="kv">' +
      "<div>Verdict</div><div>" + escapeHTML(verdict.status || "unknown") + "</div>" +
      "<div>Summary</div><div>" + escapeHTML(verdict.summary || "") + "</div>" +
      "<div>Advisory only</div><div>" + escapeHTML(String(guardrails.advisory_only === true)) + "</div>" +
      "<div>Dangerous actions</div><div>" + escapeHTML(String(guardrails.dangerous_actions_allowed === true)) + "</div>" +
      "</div>" +
      renderList("Findings", findings.map(function (item) { return (item.severity || "info") + ": " + (item.summary || ""); })) +
      renderList("Next actions", actions) +
      renderBugDraft(draft);
  }

  function renderReport(report) {
    var artifact = report.artifact || {};
    var build = report.build_summary || {};
    var metadata = report.build_metadata || {};
    byId("reportPanel").innerHTML =
      '<div class="kv">' +
      "<div>State</div><div>" + escapeHTML(report.state || "") + "</div>" +
      "<div>Success</div><div>" + escapeHTML(String(report.success)) + "</div>" +
      "<div>Dry run</div><div>" + escapeHTML(String(report.dry_run)) + "</div>" +
      "<div>Profile</div><div>" + escapeHTML(metadata.profile || "n/a") + "</div>" +
      "<div>Make target</div><div>" + escapeHTML(metadata.make_target || "n/a") + "</div>" +
      "<div>Build class</div><div>" + escapeHTML(build.classification || "n/a") + "</div>" +
      "<div>Artifact</div><div>" + escapeHTML(artifact.filename || "n/a") + "</div>" +
      "<div>SHA256</div><div>" + escapeHTML(artifact.sha256 || "n/a") + "</div>" +
      "</div>";
  }

  function renderRunner(runner) {
    if (!runner) {
      byId("runnerPanel").innerHTML = '<p class="muted">No runner.json for this job.</p>';
      return;
    }
    byId("runnerPanel").innerHTML =
      '<div class="kv">' +
      "<div>Status</div><div>" + escapeHTML(runner.status || "") + "</div>" +
      "<div>PID</div><div>" + escapeHTML(runner.pid || "n/a") + "</div>" +
      "<div>Exit code</div><div>" + escapeHTML(runner.exit_code == null ? "n/a" : runner.exit_code) + "</div>" +
      "<div>Started</div><div>" + escapeHTML(runner.started_at || "") + "</div>" +
      "<div>Updated</div><div>" + escapeHTML(runner.updated_at || "") + "</div>" +
      "<div>Command</div><div>" + escapeHTML((runner.command || []).join(" ")) + "</div>" +
      "</div>";
  }

  function renderLogs(eventsText, runnerText) {
    byId("logsPanel").innerHTML =
      '<div class="split">' +
      '<div><h3 class="section-title">events.jsonl</h3><pre>' + escapeHTML(tailLines(eventsText || "not available", 80)) + "</pre></div>" +
      '<div><h3 class="section-title">runner.output.jsonl</h3><pre>' + escapeHTML(formatRunnerOutput(runnerText || "not available")) + "</pre></div>" +
      "</div>";
  }

  function renderList(title, values) {
    if (!values || !values.length) return "";
    return '<h3 class="section-title">' + escapeHTML(title) + '</h3><ul class="list">' +
      values.map(function (value) { return "<li>" + escapeHTML(value) + "</li>"; }).join("") +
      "</ul>";
  }

  function renderBugDraft(draft) {
    if (!draft) return "";
    var labels = (draft.labels || []).join(", ");
    return '<h3 class="section-title">Bug report draft</h3>' +
      '<div class="kv"><div>Title</div><div>' + escapeHTML(draft.title || "") + "</div>" +
      "<div>Labels</div><div>" + escapeHTML(labels) + "</div></div>" +
      "<pre>" + escapeHTML(draft.body || "") + "</pre>";
  }

  function tailLines(text, limit) {
    var lines = String(text || "").split(/\r?\n/).filter(function (line) { return line.length; });
    return lines.slice(Math.max(0, lines.length - limit)).join("\n");
  }

  function formatRunnerOutput(text) {
    return tailLines(text, 80).split(/\r?\n/).map(function (line) {
      try {
        var event = JSON.parse(line);
        return "[" + (event.stream || "log") + "] " + (event.line || "");
      } catch (err) {
        return line;
      }
    }).join("\n");
  }

  function setActiveTab(tabName) {
    Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
      tab.classList.toggle("active", tab.getAttribute("data-tab") === tabName);
    });
    Array.prototype.forEach.call(document.querySelectorAll(".panel"), function (panel) {
      panel.classList.toggle("active", panel.id === tabName + "Panel");
    });
  }

  function scrollToSection(selector) {
    var target = document.querySelector(selector);
    if (target) target.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function handleNavJump(target) {
    if (target === "jobs") {
      scrollToSection(".job-pane");
      return;
    }
    if (target === "analysis" || target === "logs") {
      setActiveTab(target);
      scrollToSection(".detail-pane");
    }
  }

  function setFilter(filterName) {
    state.filter = filterName || "all";
    Array.prototype.forEach.call(document.querySelectorAll(".segment"), function (segment) {
      segment.classList.toggle("active", segment.getAttribute("data-filter") === state.filter);
    });
    renderJobs();
  }

  function configureAutoRefresh() {
    if (state.autoTimer) window.clearInterval(state.autoTimer);
    if (byId("autoRefresh").checked) {
      state.autoTimer = window.setInterval(loadJobs, 5000);
    }
  }

  byId("refreshBtn").addEventListener("click", loadJobs);
  byId("autoRefresh").addEventListener("change", configureAutoRefresh);
  byId("jobSearch").addEventListener("input", function (event) {
    state.search = event.target.value || "";
    renderJobs();
  });
  Array.prototype.forEach.call(document.querySelectorAll(".segment"), function (segment) {
    segment.addEventListener("click", function () {
      setFilter(segment.getAttribute("data-filter"));
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-jump]"), function (nav) {
    nav.addEventListener("click", function () {
      handleNavJump(nav.getAttribute("data-jump"));
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll(".tab"), function (tab) {
    tab.addEventListener("click", function () {
      setActiveTab(tab.getAttribute("data-tab"));
    });
  });

  renderOverview();
  configureAutoRefresh();
  loadJobs();
})();
`
