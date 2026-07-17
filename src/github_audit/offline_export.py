"""Self-contained offline HTML export.

Produces a single .html file with the audit data embedded as JSON and a small
vanilla-JS filter UI (search, per-column multi-select, sorting). Opens by
double-click in any browser — no Streamlit, terminal, or network needed.
"""

from __future__ import annotations

import html
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Literal

CellValue = str | int


@dataclass(frozen=True)
class OfflineColumn:
    key: str
    label: str
    kind: Literal["text", "badge", "link", "number"] = "text"
    filterable: bool = False
    # Multi-valued cells ("a, b") contribute each part as its own filter option.
    split: bool = False
    # Numeric column gets a "≥ N" input filter (e.g. minimum branch age).
    min_filter: bool = False


def render_offline_html(
    title: str,
    subtitle: str,
    columns: Sequence[OfflineColumn],
    rows: Sequence[Mapping[str, CellValue]],
) -> str:
    for row in rows:
        for column in columns:
            if column.key not in row:
                msg = f"row is missing column {column.key!r}"
                raise ValueError(msg)
    # "</" must not appear inside the inline <script> blocks ("</script>" would
    # terminate them mid-JSON); "<\/" is identical after JSON.parse.
    columns_json = json.dumps([asdict(c) for c in columns], ensure_ascii=False)
    rows_json = json.dumps([dict(r) for r in rows], ensure_ascii=False)
    return (
        _TEMPLATE.replace("__TITLE__", html.escape(title))
        .replace("__SUBTITLE__", html.escape(subtitle))
        .replace("__COLUMNS__", columns_json.replace("</", "<\\/"))
        .replace("__ROWS__", rows_json.replace("</", "<\\/"))
    )


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 24px; background: #f6f8fa; color: #1f2328;
  font: 14px/1.5 -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
.subtitle { color: #59636e; margin: 0 0 16px; }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 12px; }
.toolbar input[type="search"] { flex: 1 1 220px; max-width: 340px; padding: 6px 10px;
  border: 1px solid #d1d9e0; border-radius: 6px; font-size: 14px; }
.toolbar input[type="number"] { width: 70px; padding: 6px 8px;
  border: 1px solid #d1d9e0; border-radius: 6px; font-size: 14px; }
.dd { position: relative; }
.dd > button { padding: 6px 10px; border: 1px solid #d1d9e0; border-radius: 6px;
  background: #fff; cursor: pointer; font-size: 13px; }
.dd > button.active { border-color: #0969da; color: #0969da; font-weight: 600; }
.dd-menu { display: none; position: absolute; z-index: 10; top: calc(100% + 4px); left: 0;
  min-width: 220px; max-height: 320px; overflow-y: auto; background: #fff;
  border: 1px solid #d1d9e0; border-radius: 6px; box-shadow: 0 8px 24px rgba(31,35,40,.12);
  padding: 6px; }
.dd.open .dd-menu { display: block; }
.dd-menu label { display: flex; gap: 8px; align-items: center; padding: 4px 6px;
  border-radius: 4px; cursor: pointer; white-space: nowrap; }
.dd-menu label:hover { background: #f6f8fa; }
.reset { border: none; background: none; color: #0969da; cursor: pointer; font-size: 13px; }
.count { margin-left: auto; color: #59636e; font-size: 13px; }
.tablewrap { overflow-x: auto; background: #fff; border: 1px solid #d1d9e0; border-radius: 8px; }
table { border-collapse: collapse; width: 100%; }
th, td { padding: 7px 12px; border-bottom: 1px solid #d1d9e0; text-align: left;
  vertical-align: top; }
th { position: sticky; top: 0; background: #f6f8fa; font-size: 12px; color: #59636e;
  cursor: pointer; user-select: none; white-space: nowrap; }
th .arrow { color: #0969da; }
tbody tr:hover { background: #f6f8fa; }
tbody tr:last-child td { border-bottom: none; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 1px 9px; border-radius: 999px; font-size: 12px;
  font-weight: 600; white-space: nowrap; }
.badge.b-open { color: #1a7f37; background: #dafbe1; }
.badge.b-draft, .badge.b-none, .badge.b-active { color: #59636e; background: #eff2f5; }
.badge.b-merged { color: #8250df; background: #fbefff; }
.badge.b-closed { color: #cf222e; background: #ffebe9; }
.badge.b-issue { color: #0969da; background: #ddf4ff; }
.badge.b-pr { color: #1a7f37; background: #dafbe1; }
.badge.b-stale { color: #9a6700; background: #fff8c5; }
.empty { padding: 32px; text-align: center; color: #59636e; }
a { color: #0969da; text-decoration: none; }
a:hover { text-decoration: underline; }
</style>
</head>
<body>
<h1>__TITLE__</h1>
<p class="subtitle">__SUBTITLE__</p>
<div class="toolbar" id="toolbar">
  <input type="search" id="search" placeholder="Search…">
  <span class="count" id="count"></span>
</div>
<div class="tablewrap"><table>
  <thead><tr id="headrow"></tr></thead>
  <tbody id="body"></tbody>
</table></div>
<script id="columns" type="application/json">__COLUMNS__</script>
<script id="rows" type="application/json">__ROWS__</script>
<script>
"use strict";
const COLS = JSON.parse(document.getElementById("columns").textContent);
const ROWS = JSON.parse(document.getElementById("rows").textContent);
const state = { search: "", facets: {}, mins: {}, sortKey: null, sortDir: 1 };

function partsOf(value, split) {
  const text = String(value);
  return split ? text.split(",").map(p => p.trim()).filter(Boolean) : [text];
}

function rowVisible(row) {
  for (const col of COLS) {
    const selected = state.facets[col.key];
    if (selected && selected.size) {
      if (!partsOf(row[col.key], col.split).some(p => selected.has(p))) return false;
    }
    const min = state.mins[col.key];
    if (min !== undefined && min !== "" && Number(row[col.key]) < Number(min)) return false;
  }
  if (state.search) {
    const needle = state.search.toLowerCase();
    if (!COLS.some(c => String(row[c.key]).toLowerCase().includes(needle))) return false;
  }
  return true;
}

function renderCell(td, col, value) {
  if (col.kind === "link") {
    const url = String(value);
    if (/^https?:\\/\\//.test(url)) {
      const a = document.createElement("a");
      a.href = url; a.target = "_blank"; a.rel = "noopener";
      a.textContent = "Open \\u2197";
      td.appendChild(a);
    }
    return;
  }
  if (col.kind === "badge" && String(value)) {
    const span = document.createElement("span");
    span.className = "badge b-" + String(value).toLowerCase().replace(/[^a-z0-9]+/g, "-");
    span.textContent = String(value);
    td.appendChild(span);
    return;
  }
  if (col.kind === "number") td.className = "num";
  td.textContent = String(value);
}

function render() {
  const visible = ROWS.filter(rowVisible);
  if (state.sortKey) {
    const col = COLS.find(c => c.key === state.sortKey);
    visible.sort((a, b) => {
      const x = a[state.sortKey], y = b[state.sortKey];
      const cmp = col.kind === "number"
        ? Number(x) - Number(y)
        : String(x).localeCompare(String(y));
      return cmp * state.sortDir;
    });
  }
  const body = document.getElementById("body");
  body.textContent = "";
  for (const row of visible) {
    const tr = document.createElement("tr");
    for (const col of COLS) {
      const td = document.createElement("td");
      renderCell(td, col, row[col.key]);
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  if (!visible.length) {
    const tr = document.createElement("tr");
    const td = document.createElement("td");
    td.colSpan = COLS.length; td.className = "empty";
    td.textContent = "No rows match the current filters.";
    tr.appendChild(td);
    body.appendChild(tr);
  }
  document.getElementById("count").textContent =
    "Showing " + visible.length + " of " + ROWS.length;
  for (const col of COLS) {
    const btn = document.querySelector('[data-facet-btn="' + col.key + '"]');
    if (!btn) continue;
    const n = (state.facets[col.key] || new Set()).size;
    btn.classList.toggle("active", n > 0);
    btn.textContent = col.label + (n ? " (" + n + ")" : "") + " \\u25BE";
  }
}

function buildHeader() {
  const tr = document.getElementById("headrow");
  for (const col of COLS) {
    const th = document.createElement("th");
    th.textContent = col.label;
    th.addEventListener("click", () => {
      state.sortDir = state.sortKey === col.key ? -state.sortDir : 1;
      state.sortKey = col.key;
      for (const other of tr.children) {
        other.textContent = other.dataset.label;
      }
      const arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = state.sortDir === 1 ? " \\u2191" : " \\u2193";
      th.appendChild(arrow);
      render();
    });
    th.dataset.label = col.label;
    tr.appendChild(th);
  }
}

function buildToolbar() {
  const toolbar = document.getElementById("toolbar");
  const count = document.getElementById("count");
  for (const col of COLS) {
    if (col.min_filter) {
      const wrap = document.createElement("label");
      wrap.append(col.label + " \\u2265 ");
      const input = document.createElement("input");
      input.type = "number"; input.min = "0";
      input.addEventListener("input", () => { state.mins[col.key] = input.value; render(); });
      wrap.appendChild(input);
      toolbar.insertBefore(wrap, count);
      continue;
    }
    if (!col.filterable) continue;
    const values = new Set();
    for (const row of ROWS) for (const p of partsOf(row[col.key], col.split)) values.add(p);
    const dd = document.createElement("div");
    dd.className = "dd";
    const btn = document.createElement("button");
    btn.dataset.facetBtn = col.key;
    btn.addEventListener("click", e => {
      e.stopPropagation();
      const wasOpen = dd.classList.contains("open");
      document.querySelectorAll(".dd.open").forEach(d => d.classList.remove("open"));
      if (!wasOpen) dd.classList.add("open");
    });
    const menu = document.createElement("div");
    menu.className = "dd-menu";
    menu.addEventListener("click", e => e.stopPropagation());
    state.facets[col.key] = new Set();
    for (const value of [...values].sort((a, b) => a.localeCompare(b))) {
      const label = document.createElement("label");
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.addEventListener("change", () => {
        if (cb.checked) state.facets[col.key].add(value);
        else state.facets[col.key].delete(value);
        render();
      });
      label.appendChild(cb);
      label.append(value);
      menu.appendChild(label);
    }
    dd.appendChild(btn);
    dd.appendChild(menu);
    toolbar.insertBefore(dd, count);
  }
  const reset = document.createElement("button");
  reset.className = "reset";
  reset.textContent = "Reset filters";
  reset.addEventListener("click", () => {
    state.search = "";
    document.getElementById("search").value = "";
    for (const key of Object.keys(state.facets)) state.facets[key].clear();
    state.mins = {};
    document.querySelectorAll(".dd-menu input").forEach(cb => { cb.checked = false; });
    document.querySelectorAll('#toolbar input[type="number"]').forEach(i => { i.value = ""; });
    render();
  });
  toolbar.insertBefore(reset, count);
}

document.addEventListener("click", () => {
  document.querySelectorAll(".dd.open").forEach(d => d.classList.remove("open"));
});
document.getElementById("search").addEventListener("input", e => {
  state.search = e.target.value.trim();
  render();
});
buildHeader();
buildToolbar();
render();
</script>
</body>
</html>
"""
