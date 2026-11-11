/* Photo Haven report (docs/07). Vanilla JS, works from file://.
   Decisions live in localStorage until Export writes decisions-<report_id>.jsonl (03 §5.3).
   Suggestions are never decisions: only values you set (or accept) are exported. */
(function () {
  "use strict";
  const D = window.PH_DATA;
  const F = D.folders;
  const STORE = "photohaven:" + D.reportId;
  const $ = (s, el) => (el || document).querySelector(s);
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ------------------------------------------------------------ decisions
  let decisions = {};
  try { decisions = JSON.parse(localStorage.getItem(STORE) || "{}"); } catch (e) { decisions = {}; }
  function save() {
    try { localStorage.setItem(STORE, JSON.stringify(decisions)); } catch (e) { /* storage full or blocked */ }
    const n = Object.keys(decisions).length;
    $("#count").textContent = n + (n === 1 ? " decision" : " decisions");
  }
  function toast(msg) {
    let el = $("#toast");
    if (!el) { el = document.createElement("div"); el.id = "toast"; el.setAttribute("role", "status"); document.body.appendChild(el); }
    el.textContent = msg; el.className = "show";
    clearTimeout(toast.t); toast.t = setTimeout(() => { el.className = ""; }, 4000);
  }
  function nowTs() { return new Date().toISOString().replace(/\.(\d{3})Z$/, ".$1000Z"); }
  function folderKey(f, scope) { return ["F", f.root, f.path, scope].join("|"); }
  function fileKey(f, file) { return ["f", f.root, join(f.path, file.name), file.sha].join("|"); }
  function join(a, b) { return a ? a + "/" + b : b; }
  function setDecision(key, target, action) {
    if (!action) delete decisions[key];
    else decisions[key] = { target: target, action: action, at: nowTs() };
    save();
  }
  function folderTarget(f, scope) { return { kind: "folder", root_id: f.root, path: f.path, scope: scope }; }
  function fileTarget(f, file) { return { kind: "file", root_id: f.root, path: join(f.path, file.name), sha256: file.sha, size: file.size }; }

  function canonical(v) {
    if (v === null || typeof v !== "object") return JSON.stringify(v);
    if (Array.isArray(v)) return "[" + v.map(canonical).join(",") + "]";
    return "{" + Object.keys(v).sort().map((k) => JSON.stringify(k) + ":" + canonical(v[k])).join(",") + "}";
  }
  function exportText() {
    const lines = [canonical({ record: "header", schema: 1, kind: "decision-export", library_id: D.libraryId,
      report_id: D.reportId, scan_id: D.scanId, generated_at: nowTs() })];
    const items = Object.values(decisions).sort((a, b) => a.at < b.at ? -1 : a.at > b.at ? 1 : 0);
    for (const d of items) {
      lines.push(canonical({ record: "decision", at: d.at, phase: "dedupe", target: d.target, action: d.action,
        origin: "report", report_id: D.reportId }));
    }
    return lines.join("\n") + "\n";
  }
  window.PH_EXPORT = exportText;   // for tests
  $("#export").onclick = function () {
    if (!Object.keys(decisions).length) { toast("No decisions yet. Set some dropdowns (or accept suggestions) first."); return; }
    const blob = new Blob([exportText()], { type: "application/x-ndjson" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = "decisions-" + D.reportId + ".jsonl";
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  };
  $("#clear").onclick = function () {
    if (confirm("Forget every decision made in this report? (Already-exported files are not affected.)")) {
      decisions = {}; save(); render();
    }
  };

  // ------------------------------------------------------------ formatting
  function bytes(n) {
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i ? n.toFixed(1) : n) + " " + u[i];
  }
  const rootName = (id) => D.roots[id] || id.slice(0, 8);
  const pathHtml = (f) => '<span class="path">' + esc(rootName(f.root)) + ":" + esc(f.path || "/") + "</span>";
  function suggHtml(s) {
    if (!s) return "";
    return '<span class="sugg-' + s.a + '">' + esc(s.a) + "</span>" + (s.l ? ' <span class="tag">locked</span>' : "") +
      (s.r ? '<div class="reason">' + esc(s.r) + "</div>" : "");
  }
  function thumbsHtml(list) {
    if (!list || !list.length) return "";
    return '<div class="thumbs">' + list.map((t) => '<img loading="lazy" src="' + esc(t) + '" alt="">').join("") + "</div>";
  }
  function picker(key, target, sugg, locked) {
    const cur = decisions[key] ? decisions[key].action : "";
    const opts = [["", "— (suggest: " + (sugg ? sugg.a : "none") + ")"], ["keep", "keep"], ["remove", "remove"], ["undecided", "undecided"]];
    return '<select data-key="' + esc(key) + '" data-target="' + esc(JSON.stringify(target)) + '"' +
      (locked ? " disabled title=\"Protected: only file-level decisions apply here\"" : "") +
      ' class="' + (cur ? "set-" + cur : "") + '">' +
      opts.map((o) => '<option value="' + o[0] + '"' + (o[0] === cur ? " selected" : "") + ">" + esc(o[1]) + "</option>").join("") +
      "</select>";
  }
  function folderPicker(f, scope) {
    const s = scope === "subtree" ? f.subsugg : f.sugg;
    return picker(folderKey(f, scope), folderTarget(f, scope), s, f.locked && scope === "folder" && s && s.l);
  }
  document.addEventListener("change", function (e) {
    const el = e.target;
    if (el.tagName !== "SELECT" || !el.dataset.key) return;
    setDecision(el.dataset.key, JSON.parse(el.dataset.target), el.value);
    el.className = el.value ? "set-" + el.value : "";
    document.querySelectorAll('select[data-key="' + CSS.escape(el.dataset.key) + '"]').forEach((o) => {
      if (o !== el) { o.value = el.value; o.className = el.className; }
    });
  });
  function acceptAll(ids, scope) {
    let n = 0;
    for (const id of ids) {
      const f = F[id]; const s = scope === "subtree" ? f.subsugg : f.sugg;
      if (!s || s.l || s.a === "undecided") continue;
      const key = folderKey(f, scope);
      if (decisions[key]) continue;
      decisions[key] = { target: folderTarget(f, scope), action: s.a, at: nowTs() }; n++;
    }
    save(); render();
    return n;
  }

  // ------------------------------------------------------------ sections
  function head(id, title, text, acceptIds, scope) {
    return '<div class="section-head" id="' + id + '"><h2>' + esc(title) + "</h2>" +
      (acceptIds && acceptIds.length ? '<button data-accept="' + id + '" data-scope="' + (scope || "folder") + '">Accept suggestions in this section</button>' : "") +
      "</div><p class=\"muted\">" + text + "</p>";
  }
  function folderRows(ids, scope, extra) {
    if (!ids.length) return '<p class="empty">None.</p>';
    const rows = ids.map((id) => {
      const f = F[id];
      const sub = scope === "subtree";
      const files = sub ? f.sub.files : f.files, size = sub ? f.sub.bytes : f.bytes, rec = sub ? f.sub.dupBytes : f.reclaim;
      return "<tr><td>" + pathHtml(f) + (f.event ? ' <span class="tag">' + esc(f.event) + "</span>" : "") +
        (extra ? extra(f) : "") + thumbsHtml(f.thumbs) + '</td><td class="num">' + files + '</td><td class="num">' + bytes(size) +
        '</td><td class="num">' + bytes(rec) + "</td><td>" + suggHtml(sub ? f.subsugg : f.sugg) + "</td><td>" + folderPicker(f, scope) +
        ' <button class="link" data-detail="' + id + '">files</button></td></tr>';
    });
    return '<table><thead><tr><th>Folder</th><th class="num">Files</th><th class="num">Size</th><th class="num">Reclaimable</th><th>Suggestion</th><th>Decision</th></tr></thead><tbody>' +
      rows.join("") + "</tbody></table>";
  }
  function containedIn(f) {
    if (!f.sugg || f.sugg.o == null) return "";
    return '<div class="reason">contained in ' + pathHtml(F[f.sugg.o]) + "</div>";
  }
  function copiesIn(f) {
    if (!f.copies) return "";
    return '<div class="reason">copies live in: ' + f.copies.map((c) => pathHtml(F[c[0]]) + " (" + Math.round(c[1] * 100) + "%)").join(", ") + "</div>";
  }

  function render() {
    const S = D.sections, s = D.summary;
    $("#subtitle").textContent = (D.libraryName || D.libraryId) + " · report " + D.generatedAt.slice(0, 19).replace("T", " ") + " UTC · " + D.machine;
    const nav = [["summary", "Summary"], ["contained", "Easiest wins (" + S.contained.length + ")"], ["trees", "Redundant trees (" + S.trees.length + ")"],
      ["redundant", "Fully redundant (" + S.redundant.length + ")"], ["overlap", "Overlap (" + S.overlap.length + ")"],
      ["internal", "Copies inside a folder (" + S.internal.length + ")"], ["contentEquiv", "Same picture, different bytes (" + S.contentEquiv.length + ")"],
      ["warnings", "Warnings (" + (S.warnings.length + S.orphaned.length) + ")"], ["tree", "All folders"]];
    $("#nav").innerHTML = nav.map((n) => '<a href="#' + n[0] + '">' + esc(n[1]) + "</a>").join("");
    let h = '<h2 id="summary">Summary</h2><div class="cards">' + [
      [s.files.toLocaleString(), "files"], [bytes(s.bytes), "total size"], [s.dupGroups.toLocaleString(), "duplicate groups"],
      [s.dupInstances.toLocaleString(), "files in duplicate groups"], [bytes(s.reclaimable), "reclaimable (exact copies)"],
      [s.contentGroups.toLocaleString(), "same picture, different bytes"], [s.cloudOnly.toLocaleString(), "cloud-only (" + s.cloudOnlyUnhashed + " without facts)"],
      [s.unhashed.toLocaleString(), "not hashed yet (pass: " + s.hashPass + ")"]].map((c) => '<div class="card"><div class="v">' + esc(c[0]) + '</div><div class="k">' + esc(c[1]) + "</div></div>").join("") +
      '</div><p class="muted">Last scan ' + esc(s.scanAt || "?") + "; facts from " + esc(s.machines.join(", ") || "this machine") +
      ". Nothing happens until you export decisions and run <code>photohaven decisions import FILE</code>, then <code>photohaven plan</code>.</p>";
    h += head("contained", "Easiest wins: folders contained in another folder", "Every file here is also in another folder. Sorted by space reclaimed.", S.contained);
    h += folderRows(S.contained, "folder", containedIn);
    h += head("trees", "Redundant trees", "Every file in the whole tree exists outside it. A decision here covers the tree (scope: subtree).", S.trees, "subtree");
    h += folderRows(S.trees, "subtree");
    h += head("redundant", "Fully redundant folders", "Every file exists elsewhere, spread over several folders.", S.redundant);
    h += folderRows(S.redundant, "folder", copiesIn);
    h += head("overlap", "Heavy overlap", "At least half of one folder is in the other. Each side keeps the files unique to it.");
    h += S.overlap.length ? "<table><thead><tr><th>Folder A</th><th>Folder B</th><th class=\"num\">Shared</th><th class=\"num\">A in B</th><th class=\"num\">B in A</th><th>Decide A</th><th>Decide B</th></tr></thead><tbody>" +
      S.overlap.map((o) => { const a = F[o[0]], b = F[o[1]]; return "<tr><td>" + pathHtml(a) + '<div class="reason">' + a.unique + ' unique</div></td><td>' + pathHtml(b) + '<div class="reason">' + b.unique + " unique</div></td><td class=\"num\">" + o[2] + " (" + bytes(o[5]) + ")</td><td class=\"num\">" + Math.round(o[3] * 100) + "%</td><td class=\"num\">" + Math.round(o[4] * 100) + "%</td><td>" + folderPicker(a, "folder") + "</td><td>" + folderPicker(b, "folder") + "</td></tr>"; }).join("") + "</tbody></table>"
      : '<p class="empty">None.</p>';
    h += head("internal", "Copies inside one folder", "Identical files whose only copies are in the same folder (for example “- Copy” or “(1)”). Decide per file in the folder's file list; one copy always stays.");
    h += folderRows(S.internal, "folder");
    h += head("contentEquiv", "Same picture, different bytes", "Same image data, different metadata (ratings, keywords, dates, GPS). Report only: these are never removed by folder decisions." );
    h += S.contentEquiv.length ? S.contentEquiv.map((g, i) => "<h3>Group " + (i + 1) + (g.differs.length ? ' <span class="tag">differs: ' + esc(g.differs.join(", ")) + "</span>" : "") +
      (g.sameTime ? "" : ' <span class="tag red">different capture times: maybe different photos</span>') + "</h3><table><thead><tr><th>File</th><th>Taken</th><th>Camera</th><th>GPS</th><th class=\"num\">Size</th></tr></thead><tbody>" +
      g.members.map((m) => "<tr><td>" + pathHtml({ root: m.root, path: m.path }) + '</td><td class="' + (g.differs.includes("taken") ? "diff" : "") + '">' + esc(m.taken || "") + (m.offset ? " " + esc(m.offset) : "") +
        '</td><td class="' + (g.differs.includes("camera") ? "diff" : "") + '">' + esc(m.camera || "") + '</td><td class="' + (g.differs.includes("gps") ? "diff" : "") + '">' + esc(m.gps || "") + '</td><td class="num">' + bytes(m.size) + "</td></tr>").join("") + "</tbody></table>").join("")
      : '<p class="empty">None.</p>';
    h += head("warnings", "Warnings", "Files to look at, and decisions whose target no longer exists.");
    h += S.warnings.length || S.orphaned.length ? "<table><tbody>" +
      S.warnings.map((w) => '<tr class="warn"><td>' + esc(w.label) + "</td><td>" + (w.path != null ? pathHtml({ root: w.root, path: w.path }) : "") + '</td><td class="reason">' + esc(w.detail || "") + "</td></tr>").join("") +
      S.orphaned.map((o) => '<tr class="warn"><td>Orphaned decision</td><td>' + pathHtml({ root: o.root, path: o.path }) + '</td><td class="reason">' + esc(o.kind + " " + o.action + " at " + o.at) + "</td></tr>").join("") + "</tbody></table>"
      : '<p class="empty">None.</p>';
    h += head("tree", "All folders", "Totals include subfolders. Use the first dropdown for the files directly in a folder and the second for the whole tree.");
    h += '<div class="tree">' + treeHtml() + "</div>";
    $("#main").innerHTML = h;
    save();
  }

  function treeHtml() {
    const kids = {};
    const tops = [];
    for (const id in F) {
      const f = F[id];
      if (f.parent != null && F[f.parent]) (kids[f.parent] = kids[f.parent] || []).push(f);
      else tops.push(f);
    }
    const sortf = (a, b) => (a.path.toLowerCase() < b.path.toLowerCase() ? -1 : 1);
    function node(f) {
      const ch = (kids[f.id] || []).sort(sortf);
      const st = f.sub;
      return "<li><div class=\"node\"><span class=\"tog\" data-tog>" + (ch.length ? "▸" : "") + "</span>" +
        '<span class="path">' + esc(f.depth ? f.name : rootName(f.root) + ":/") + "</span>" +
        '<span class="stats">' + st.files + " files · " + bytes(st.bytes) + (st.dup ? " · " + st.dup + " with copies outside" : "") + (st.redundant ? ' · <span class="tag red">redundant tree</span>' : "") + "</span>" +
        (f.files ? folderPicker(f, "folder") + ' <button class="link" data-detail="' + f.id + '">files</button>' : "") +
        (ch.length ? " tree: " + folderPicker(f, "subtree") : "") +
        "</div>" + (ch.length ? '<ul hidden>' + ch.map(node).join("") + "</ul>" : "") + "</li>";
    }
    return "<ul>" + tops.sort(sortf).map(node).join("") + "</ul>";
  }

  document.addEventListener("click", function (e) {
    const t = e.target;
    if (t.dataset && t.dataset.tog !== undefined) {
      const ul = t.closest("li").querySelector(":scope > ul");
      if (ul) { ul.hidden = !ul.hidden; t.textContent = ul.hidden ? "▸" : "▾"; }
    } else if (t.dataset && t.dataset.accept) {
      const n = acceptAll(D.sections[t.dataset.accept], t.dataset.scope);
      toast(n + " suggestion(s) accepted as decisions. Review them, then Export.");
    } else if (t.dataset && t.dataset.detail) {
      openDetail(+t.dataset.detail);
    }
  });

  // ------------------------------------------------------------ detail
  const loaded = {};
  window.PH_DETAIL = function (d) { loaded[d.id] = d; if (pending === d.id) showDetail(d.id); };
  let pending = null;
  function openDetail(id) {
    pending = id;
    if (loaded[id]) { showDetail(id); return; }
    const s = document.createElement("script");
    s.src = "detail/" + id + ".js";
    s.onerror = function () { toast("No file list for this folder."); };
    document.body.appendChild(s);
  }
  function showDetail(id) {
    const f = F[id], d = loaded[id];
    $("#detail-title").innerHTML = pathHtml(f);
    $("#detail-body").innerHTML = '<p class="muted">Folder decision: ' + folderPicker(f, "folder") + " " + suggHtml(f.sugg) +
      '</p><p class="muted">A file decision overrides the folder decision for that file, and only applies while the file has the same bytes.</p>' +
      "<table><thead><tr><th></th><th>File</th><th>Taken</th><th class=\"num\">Size</th><th>Other copies</th><th>Decision</th></tr></thead><tbody>" +
      d.files.map((x) => "<tr><td>" + (x.thumb ? '<img class="big" loading="lazy" src="' + esc(x.thumb) + '" alt="">' : "") + "</td><td><span class=\"path\">" + esc(x.name) + "</span>" +
        (x.flags ? " " + x.flags.map((g) => '<span class="tag red">' + esc(g) + "</span>").join(" ") : "") + (x.state !== "present" ? ' <span class="tag">' + esc(x.state) + "</span>" : "") +
        (x.sugg ? "<div>" + suggHtml(x.sugg) + "</div>" : "") + "</td><td>" + esc(x.taken || "") + '</td><td class="num">' + bytes(x.size) + "</td><td>" +
        (x.ncopies ? (x.copies || []).map((c) => '<div class="path">' + esc(c) + "</div>").join("") + (x.ncopies > (x.copies || []).length ? '<div class="reason">and ' + (x.ncopies - x.copies.length) + " more</div>" : "") : '<span class="muted">none</span>') +
        "</td><td>" + (x.sha ? picker(fileKey(f, x), fileTarget(f, x), x.sugg, false) : '<span class="muted">not hashed</span>') + "</td></tr>").join("") +
      "</tbody></table>";
    $("#detail").classList.remove("hidden");
  }
  $("#detail-close").onclick = () => $("#detail").classList.add("hidden");
  $("#detail").onclick = (e) => { if (e.target.id === "detail") $("#detail").classList.add("hidden"); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") $("#detail").classList.add("hidden"); });

  render();
})();
