"use strict";
/* Exam Bank — single-page UI. Hash routes:
   #/                  all questions (table)        #/topics        grouped by topic
   #/course/<code>     one course                   #/practice      practice mode
   #/crop              cropping queue               #/crop/<exam>   cropping tool
   #/tags              tag review                   #/meta          exam metadata
   #/solutions         solution matcher             ?q=<id> on any list opens the drawer */

const S = {
  meta: null, rows: [], byId: new Map(),
  f: { course: "", topic: "", year: "", moed: "", status: "", crop: "", q: "" },
  sort: { key: "year", asc: false },
  visible: [],            // current filtered list (for prev/next in the drawer)
};
const $ = (s, r = document) => r.querySelector(s);
const MOED = { A: "א", B: "ב", C: "ג", unknown: "?" };
const SEM = { A: "A", B: "B", C: "Summer", unknown: "?" };

// ------------------------------------------------------------------ helpers
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function h(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; }
async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (!r.ok) { const t = await r.text(); toast("Error: " + t.slice(0, 160)); throw new Error(t); }
  return r.json();
}
let toastTimer;
function toast(msg) { const t = $("#toast"); t.textContent = msg; t.classList.remove("hidden"); clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add("hidden"), 2200); }
const fileUrl = p => p ? "/files/" + p.replace(/^data\//, "") : "";
function course(code) { return S.meta.courses.find(c => c.code === code); }
function topicName(code, id) {
  const t = (course(code)?.topics || []).find(t => t.id === id);
  return t ? (t.en || t.he) : id;
}
function topicLabel(t) { return [t.en, t.he].filter(Boolean).join(" · "); }
// One hue per topic id (t01, t02, ...). Courses reuse the same palette, so t03 has the same color
// everywhere; neighbouring ids get clearly different hues.
const HUES = [212, 142, 36, 350, 266, 174, 22, 322, 192, 88, 236, 2, 158, 292, 200, 52];
function topicHue(id) { const n = parseInt(String(id).replace(/\D/g, ""), 10) || 0; return HUES[(n + HUES.length - 1) % HUES.length]; }
function topicChip(code, id, state = "", extra = "") {
  return `<span class="chip topic ${esc(state)}" style="--h:${topicHue(id)}" title="${esc(state || "")}" dir="auto">${esc(topicName(code, id))}${extra}</span>`;
}
const tdot = id => `<span class="tdot" style="--h:${topicHue(id)}"></span>`;
const courseTitle = c => `<span class="course-code">${esc(c.code)}</span> ${esc(c.name_en || "")}`;
function examLabel(r) { return `${r.year} · Sem ${SEM[r.semester] ?? r.semester} · Moed ${MOED[r.moed] ?? r.moed}`; }
function stars(n) { return n ? "★".repeat(n) + "☆".repeat(5 - n) : ""; }
function debounce(fn, ms) { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; }

// ------------------------------------------------------------------ data
async function loadAll() {
  [S.meta, S.rows] = await Promise.all([api("/api/meta"), api("/api/questions")]);
  S.byId = new Map(S.rows.map(r => [r.question_id, r]));
  renderNav();
}
function updateRow(row) {
  const i = S.rows.findIndex(r => r.question_id === row.question_id);
  if (i >= 0) S.rows[i] = row; S.byId.set(row.question_id, row);
}

function renderNav() {
  $("#nav-courses").innerHTML = S.meta.courses.map(c =>
    `<a href="#/course/${c.code}" data-nav="course-${c.code}"><span>${courseTitle(c)}</span><span class="sub">${c.questions}</span></a>`).join("");
  const q = S.meta.queue;
  $("#b-crop").textContent = (q.needs_manual + q.unverified) || "";
  $("#b-tags").textContent = q.suggested || "";
  $("#b-meta").textContent = q.unknown_meta || "";
  $("#b-sol").textContent = q.unmatched || "";
}
function setActiveNav(key) {
  document.querySelectorAll("nav a").forEach(a => a.classList.toggle("active", a.dataset.nav === key));
}

// ------------------------------------------------------------------ filtering
function searchText(r) {
  return [r.summary, r.question_text, r.course, r.course_name_en, r.course_name_he, r.year,
    ...r.tags.map(t => { const tp = (course(r.course)?.topics || []).find(x => x.id === t.topic); return tp ? topicLabel(tp) : t.topic; })]
    .join(" ").toLowerCase();
}
function applyFilters(rows, f) {
  const words = f.q.toLowerCase().split(/\s+/).filter(Boolean);
  return rows.filter(r =>
    (!f.course || r.course === f.course) &&
    (!f.topic || r.tags.some(t => t.topic === f.topic)) &&
    (!f.year || r.year === f.year) &&
    (!f.moed || r.moed === f.moed) &&
    (!f.status || r.status === f.status) &&
    (!f.crop || r.crop_status === f.crop) &&
    (!words.length || (s => words.every(w => s.includes(w)))(searchText(r))));
}
const SORTS = {
  course: r => r.course, year: r => `${r.year}${r.semester}${r.moed}${r.exam_id}`,
  moed: r => r.moed, number: r => r.question_number, status: r => S.meta.statuses.indexOf(r.status),
  difficulty: r => r.difficulty ?? 0, topic: r => r.tags[0] ? topicName(r.course, r.tags[0].topic) : "~",
};
function sortRows(rows) {
  const k = SORTS[S.sort.key] || SORTS.year, d = S.sort.asc ? 1 : -1;
  // Ties (e.g. questions of the same exam) always read in exam order: course, exam, question 1,2,3.
  const tie = (a, b) => a.course.localeCompare(b.course) || b.exam_id.localeCompare(a.exam_id) || a.question_number - b.question_number;
  return [...rows].sort((a, b) => { const x = k(a), y = k(b); return x < y ? -d : x > y ? d : tie(a, b); });
}

function filterBar(fixedCourse) {
  const f = S.f;
  const courses = S.meta.courses;
  const topicCourses = fixedCourse ? [course(fixedCourse)] : (f.course ? [course(f.course)] : courses);
  const years = [...new Set(S.rows.filter(r => !fixedCourse || r.course === fixedCourse).map(r => r.year))].sort().reverse();
  const opt = (v, label, cur) => `<option value="${esc(v)}" ${v === cur ? "selected" : ""}>${esc(label)}</option>`;
  return `<div class="toolbar">
    <input type="search" id="f-q" placeholder="Search summaries, topics… (/)" value="${esc(f.q)}">
    ${fixedCourse ? "" : `<select id="f-course">${opt("", "All courses", f.course)}${courses.map(c => opt(c.code, `${c.code} ${c.name_en}`, f.course)).join("")}</select>`}
    <select id="f-topic">${opt("", "Any topic", f.topic)}${topicCourses.map(c =>
      `<optgroup label="${esc(c.code)}">${c.topics.map(t => opt(t.id, (t.en || t.he), f.topic)).join("")}</optgroup>`).join("")}</select>
    <select id="f-year">${opt("", "Any year", f.year)}${years.map(y => opt(y, y, f.year)).join("")}</select>
    <select id="f-moed">${opt("", "Any moed", f.moed)}${["A", "B", "C"].map(m => opt(m, "Moed " + MOED[m], f.moed)).join("")}</select>
    <select id="f-status">${opt("", "Any status", f.status)}${S.meta.statuses.map(s => opt(s, s, f.status)).join("")}</select>
    <select id="f-crop">${opt("", "Any crop", f.crop)}${["unverified", "verified", "manual"].map(s => opt(s, "Crop: " + s, f.crop)).join("")}</select>
    <button id="f-clear" title="Clear filters">Clear</button>
  </div>`;
}
function wireFilters(rerender) {
  const on = (id, key, ev = "change") => $(id)?.addEventListener(ev, e => { S.f[key] = e.target.value; if (key === "course") S.f.topic = ""; rerender(); });
  on("#f-course", "course"); on("#f-topic", "topic"); on("#f-year", "year"); on("#f-moed", "moed");
  on("#f-status", "status"); on("#f-crop", "crop");
  $("#f-q")?.addEventListener("input", debounce(e => { S.f.q = e.target.value; rerender(true); }, 200));
  $("#f-clear")?.addEventListener("click", () => { Object.keys(S.f).forEach(k => S.f[k] = ""); rerender(); });
}

// ------------------------------------------------------------------ table view
function tableHtml(rows, showCourse = true) {
  if (!rows.length) return `<div class="empty">No questions match these filters.</div>`;
  const th = (key, label) => `<th data-sort="${key}" class="${S.sort.key === key ? "sorted" + (S.sort.asc ? " asc" : "") : ""}">${label}</th>`;
  return `<div class="table-wrap"><table class="db"><thead><tr>
      <th></th>${showCourse ? th("course", "Course") : ""}${th("year", "Exam")}${th("number", "Q")}
      ${th("topic", "Topics")}<th>Summary</th>${th("status", "Status")}${th("difficulty", "Difficulty")}<th>Crop</th>
    </tr></thead><tbody>${rows.map(r => `<tr data-q="${esc(r.question_id)}">
      <td class="thumb">${r.images[0] ? `<img loading="lazy" src="${fileUrl(r.images[0].path)}" alt="">` : ""}</td>
      ${showCourse ? `<td>${esc(r.course)}</td>` : ""}
      <td style="white-space:nowrap">${esc(examLabel(r))}</td>
      <td>${r.question_number}</td>
      <td>${r.tags.map(t => topicChip(r.course, t.topic, t.state)).join("")}</td>
      <td class="summary" dir="auto">${esc(r.summary || "")}</td>
      <td><span class="status" data-s="${esc(r.status)}">${esc(r.status)}</span></td>
      <td class="stars">${stars(r.difficulty)}</td>
      <td>${r.crop_status === "unverified" ? `<span class="crop-unverified">unverified</span>` : esc(r.crop_status)}</td>
    </tr>`).join("")}</tbody></table></div>`;
}
function wireTable(root) {
  root.querySelectorAll("th[data-sort]").forEach(th => th.addEventListener("click", () => {
    const k = th.dataset.sort; S.sort = { key: k, asc: S.sort.key === k ? !S.sort.asc : k !== "year" && k !== "difficulty" };
    route();
  }));
  root.querySelectorAll("tr[data-q], .card[data-q]").forEach(el => el.addEventListener("click", () => openDrawer(el.dataset.q)));
}

function viewList({ fixedCourse = null, grouped = false } = {}) {
  const main = $("#main");
  const render = (keepFocus) => {
    const f = { ...S.f, ...(fixedCourse ? { course: fixedCourse } : {}) };
    const rows = sortRows(applyFilters(S.rows, f));
    S.visible = rows.map(r => r.question_id);
    let head;
    if (fixedCourse) {
      const c = course(fixedCourse);
      const all = S.rows.filter(r => r.course === fixedCourse);
      const solved = all.filter(r => r.status === "solved").length;
      const exams = new Set(all.map(r => r.exam_id)).size;
      head = `<h1>${courseTitle(c)} <span class="he-title" dir="rtl">${esc(c.name_he)}</span></h1>
        <div class="subtitle">${esc(c.code)}${c.alt_codes.length ? " (also " + c.alt_codes.join(", ") + ")" : ""}</div>
        <div class="stats">
          <div class="stat"><b>${all.length}</b><span>questions</span></div>
          <div class="stat"><b>${exams}</b><span>exams with questions (${c.exams} total)</span></div>
          <div class="stat"><b>${solved}</b><span>solved</span><div class="bar"><i style="width:${all.length ? 100 * solved / all.length : 0}%"></i></div></div>
          <div class="stat"><b>${all.filter(r => r.status === "review again").length}</b><span>to review again</span></div>
        </div>`;
    } else {
      head = `<h1>${grouped ? "By topic" : "All questions"}</h1><div class="subtitle">Past exams, one row per question.</div>`;
    }
    const viewSeg = `<span class="seg"><button data-v="table" class="${grouped ? "" : "on"}">Table</button><button data-v="topics" class="${grouped ? "on" : ""}">By topic</button></span>`;
    main.innerHTML = head + filterBar(fixedCourse).replace(`<button id="f-clear"`, `${viewSeg}<span class="count">${rows.length} question${rows.length === 1 ? "" : "s"}</span><button id="f-clear"`) +
      (grouped ? groupsHtml(rows) : tableHtml(rows, !fixedCourse));
    wireFilters(render); wireTable(main);
    main.querySelectorAll(".seg button").forEach(b => b.addEventListener("click", () => {
      const v = b.dataset.v;
      if (fixedCourse) { S.courseGrouped = v === "topics"; route(); }
      else location.hash = v === "topics" ? "#/topics" : "#/";
    }));
    main.querySelectorAll(".group-head").forEach(g => g.addEventListener("click", () => g.nextElementSibling.toggleAttribute("hidden")));
    if (keepFocus) { const i = $("#f-q"); i.focus(); i.setSelectionRange(i.value.length, i.value.length); }
  };
  render();
}

function groupsHtml(rows) {
  if (!rows.length) return `<div class="empty">No questions match these filters.</div>`;
  const groups = new Map();
  for (const r of rows) {
    const keys = r.tags.length ? r.tags.map(t => `${r.course}|${t.topic}`) : [`${r.course}|`];
    for (const k of keys) { if (!groups.has(k)) groups.set(k, []); groups.get(k).push(r); }
  }
  const order = [...groups.keys()].sort((a, b) => {
    const [ca, ta] = a.split("|"), [cb, tb] = b.split("|");
    if (ca !== cb) return ca < cb ? -1 : 1;
    if (!ta) return 1; if (!tb) return -1;
    return ta < tb ? -1 : 1;
  });
  return order.map(k => {
    const [c, t] = k.split("|"), list = groups.get(k);
    const tp = (course(c)?.topics || []).find(x => x.id === t);
    const title = t ? (tp ? topicLabel(tp) : t) : "Untagged";
    const done = list.filter(r => r.status === "solved").length;
    return `<div class="group"><div class="group-head">${t ? tdot(t) : ""}<b dir="auto">${esc(title)}</b><span class="n">${esc(c)} ${esc(course(c)?.name_en || "")} · ${list.length} questions · ${done} solved</span></div>
      <div class="cards">${list.map(r => `<div class="card" data-q="${esc(r.question_id)}">
        ${r.images[0] ? `<img loading="lazy" src="${fileUrl(r.images[0].path)}" alt="">` : ""}
        <div class="body">${esc(examLabel(r))} · Q${r.question_number} <span class="status" data-s="${esc(r.status)}">${esc(r.status)}</span>
        ${r.summary ? `<div dir="auto" style="color:var(--muted)">${esc(r.summary)}</div>` : ""}</div></div>`).join("")}</div></div>`;
  }).join("");
}

// ------------------------------------------------------------------ drawer
async function openDrawer(qid) {
  const r = S.byId.get(qid) || await api(`/api/questions/${encodeURIComponent(qid)}`);
  const d = $("#drawer");
  const c = course(r.course);
  const examPdf = fileUrl(r.exam_pdf) + (r.exam_page ? `#page=${r.exam_page}` : "");
  const solLinks = [];
  if (r.solution_images.length) solLinks.push(`<a href="#" id="show-sol">Show solution</a>`);
  if (r.solution_pdf) solLinks.push(`<a href="${fileUrl(r.solution_pdf)}${r.solution_page ? "#page=" + r.solution_page : ""}" target="_blank">Open solution PDF${r.solution_page ? " (p. " + r.solution_page + ")" : ""}</a>`);
  // Further solution files for the same exam (student solutions, parts of a split solution...).
  (r.solution_pdfs || []).filter(s => s.path !== r.solution_pdf).forEach((s, i) => {
    const name = (s.name || "").split("/").pop().replace(/\.pdf$/i, "");
    solLinks.push(`<a href="${fileUrl(s.path)}" target="_blank" title="${esc(name)}" dir="auto">Solution ${i + 2} · ${esc(s.source)}</a>`);
  });
  const idx = S.visible.indexOf(qid);
  const confirmed = r.tags.filter(t => t.state === "confirmed").map(t => t.topic);
  const suggested = r.tags.filter(t => t.state === "suggested").map(t => t.topic);
  d.innerHTML = `
    <button class="close" id="d-close" title="Close (Esc)">✕</button>
    <h2>${esc(r.course)} · ${esc(examLabel(r))} · Question ${r.question_number}</h2>
    <div class="hint">${c ? courseTitle(c) : ""} <span dir="rtl">${esc(c?.name_he || "")}</span> · source: ${esc(r.source)}
      ${idx >= 0 ? ` · ${idx + 1} of ${S.visible.length} <button id="d-prev" ${idx <= 0 ? "disabled" : ""}>↑ Prev (k)</button> <button id="d-next" ${idx >= S.visible.length - 1 ? "disabled" : ""}>Next (j) ↓</button>` : ""}</div>
    <div class="links">
      <a href="${examPdf}" target="_blank">Open original exam${r.exam_page ? " (p. " + r.exam_page + ")" : ""}</a>
      ${solLinks.join("") || `<span class="hint" style="align-self:center">No solution linked yet</span>`}
      <a href="#/crop/${encodeURIComponent(r.exam_id)}">Fix crop</a>
    </div>
    ${r.summary ? `<p dir="auto">${esc(r.summary)}</p>` : ""}
    ${r.question_text ? `<p dir="auto" style="white-space:pre-wrap">${esc(r.question_text)}</p>` : ""}
    <div class="qimgs">${r.images.map(i => `<img src="${fileUrl(i.path)}" alt="Question ${r.question_number}, page ${i.page}">`).join("")}</div>
    <div class="qimgs" id="sol-imgs" hidden>${r.solution_images.map(i => `<img src="${fileUrl(i.path)}" alt="solution">`).join("")}</div>
    <div class="props">
      <div class="k">Status</div><div class="seg" id="d-status">${S.meta.statuses.map(s => `<button data-s="${esc(s)}" class="${s === r.status ? "on" : ""}">${esc(s)}</button>`).join("")}</div>
      <div class="k">Difficulty</div><div class="diff" id="d-diff">${[1, 2, 3, 4, 5].map(n => `<button data-n="${n}" class="${r.difficulty >= n ? "on" : ""}" title="${n}/5">★</button>`).join("")}
        ${r.difficulty ? `<button data-n="0" title="clear" style="font-size:12px">clear</button>` : ""}</div>
      <div class="k">Topics</div><div class="tagedit" id="d-tags">
        ${confirmed.map(t => topicChip(r.course, t, "", `<button data-rm="${esc(t)}" title="remove">✕</button>`)).join("")}
        ${suggested.map(t => topicChip(r.course, t, "suggested")).join("")}
        ${suggested.length ? `<button id="t-accept">Confirm suggested</button>` : ""}
        <select id="t-add"><option value="">+ add topic</option>${(c?.topics || []).filter(t => !confirmed.includes(t.id)).map(t => `<option value="${esc(t.id)}">${esc(t.en || t.he)}</option>`).join("")}</select>
      </div>
      <div class="k">Crop</div><div>${esc(r.crop_status)} ${r.crop_status === "unverified" ? `<button id="d-verify">Looks right ✓</button>` : ""}</div>
    </div>
    <div class="k" style="color:var(--muted);margin-bottom:4px">Notes</div>
    <textarea id="d-notes" dir="auto" placeholder="Your notes (saved automatically)">${esc(r.notes || "")}</textarea>`;
  d.classList.remove("hidden"); d.setAttribute("aria-hidden", "false"); d.scrollTop = 0;
  d.dataset.q = qid;

  const save = async body => { const row = await api(`/api/progress/${encodeURIComponent(qid)}`, { method: "PATCH", body }); updateRow(row); return row; };
  $("#d-close").onclick = closeDrawer;
  $("#d-prev")?.addEventListener("click", () => openDrawer(S.visible[idx - 1]));
  $("#d-next")?.addEventListener("click", () => openDrawer(S.visible[idx + 1]));
  $("#show-sol")?.addEventListener("click", e => { e.preventDefault(); $("#sol-imgs").toggleAttribute("hidden"); });
  d.querySelectorAll("#d-status button").forEach(b => b.onclick = async () => { await save({ status: b.dataset.s }); openDrawer(qid); refreshList(); toast("Saved"); });
  d.querySelectorAll("#d-diff button").forEach(b => b.onclick = async () => { const n = +b.dataset.n; await save({ difficulty: n || null }); openDrawer(qid); refreshList(); });
  $("#d-notes").addEventListener("input", debounce(async e => { await save({ notes: e.target.value }); toast("Notes saved"); }, 600));
  const setTags = async topics => { const row = await api(`/api/questions/${encodeURIComponent(qid)}/tags`, { method: "PUT", body: { topics } }); updateRow(row); openDrawer(qid); refreshList(); refreshMeta(); };
  d.querySelectorAll("[data-rm]").forEach(b => b.onclick = () => setTags(confirmed.filter(t => t !== b.dataset.rm)));
  $("#t-accept")?.addEventListener("click", () => setTags([...confirmed, ...suggested]));
  // Adding a topic by hand confirms your own choice; unconfirmed suggestions are dropped.
  $("#t-add").onchange = e => e.target.value && setTags([...confirmed, e.target.value]);
  $("#d-verify")?.addEventListener("click", async () => { const row = await api(`/api/questions/${encodeURIComponent(qid)}/crop_status`, { method: "POST", body: { status: "verified" } }); updateRow(row); openDrawer(qid); refreshList(); refreshMeta(); });
}
function closeDrawer() { const d = $("#drawer"); d.classList.add("hidden"); d.setAttribute("aria-hidden", "true"); d.dataset.q = ""; }
function refreshList() { const r = currentRoute(); if (["", "topics", "course", "tags"].includes(r[0])) route(true); }
async function refreshMeta() { S.meta = await api("/api/meta"); renderNav(); }

// ------------------------------------------------------------------ practice
function viewPractice() {
  const main = $("#main");
  const st = S.practice || (S.practice = { course: S.meta.courses[0]?.code || "", topics: [], n: 5, incl: false, list: [], i: 0, reveal: false });
  const c = course(st.course);
  if (!st.list.length) {
    main.innerHTML = `<h1>Practice</h1><div class="subtitle">Random questions you haven't solved yet, from the topics you pick.</div>
      <div class="toolbar"><select id="p-course">${S.meta.courses.map(x => `<option value="${x.code}" ${x.code === st.course ? "selected" : ""}>${x.code} ${esc(x.name_en)}</option>`).join("")}</select>
      <label>How many <input type="number" id="p-n" min="1" max="50" value="${st.n}" style="width:70px"></label>
      <label><input type="checkbox" id="p-incl" ${st.incl ? "checked" : ""}> include solved</label>
      <button class="primary" id="p-go">Start</button></div>
      <div class="hint">Topics (none selected = any topic). Topics come from tags, so untagged questions only appear with no topic selected.</div>
      <div class="cards" style="grid-template-columns:repeat(auto-fill,minmax(260px,1fr))">${(c?.topics || []).map(t => `
        <label class="card" style="padding:8px;cursor:pointer"><input type="checkbox" value="${esc(t.id)}" ${st.topics.includes(t.id) ? "checked" : ""}> ${tdot(t.id)} <span dir="auto">${esc(topicLabel(t))}</span></label>`).join("")}</div>`;
    $("#p-course").onchange = e => { st.course = e.target.value; st.topics = []; viewPractice(); };
    $("#p-go").onclick = async () => {
      st.n = +$("#p-n").value || 5; st.incl = $("#p-incl").checked;
      st.topics = [...main.querySelectorAll(".cards input:checked")].map(i => i.value);
      st.list = await api(`/api/practice?course=${st.course}&topics=${st.topics.join(",")}&n=${st.n}&include_solved=${st.incl}`);
      st.i = 0; st.reveal = false;
      if (!st.list.length) toast("No questions match — try other topics or include solved");
      viewPractice();
    };
    return;
  }
  const r = st.list[st.i];
  main.innerHTML = `<div class="practice-q"><h1>Practice <span class="hint">${st.i + 1} / ${st.list.length}</span></h1>
    <div class="subtitle">${esc(r.course)} · ${esc(examLabel(r))} · Question ${r.question_number}
      ${r.tags.length ? " · " + r.tags.map(t => topicChip(r.course, t.topic, t.state)).join("") : ""}</div>
    <div class="qimgs">${r.images.map(i => `<img src="${fileUrl(i.path)}" alt="">`).join("")}</div>
    <div class="links">
      ${r.solution_images.length ? `<a href="#" id="p-sol">${st.reveal ? "Hide" : "Show"} solution</a>` : ""}
      ${r.solution_pdf ? `<a target="_blank" href="${fileUrl(r.solution_pdf)}${r.solution_page ? "#page=" + r.solution_page : ""}">Open solution PDF</a>` : `<span class="hint" style="align-self:center">No solution linked</span>`}
      <a target="_blank" href="${fileUrl(r.exam_pdf)}#page=${r.exam_page || 1}">Original exam</a>
    </div>
    <div class="qimgs" ${st.reveal ? "" : "hidden"}>${r.solution_images.map(i => `<img src="${fileUrl(i.path)}" alt="">`).join("")}</div>
    <div class="toolbar">Mark as: ${S.meta.statuses.map(s => `<button data-s="${esc(s)}" class="${s === r.status ? "primary" : ""}">${esc(s)}</button>`).join("")}</div>
    <div class="toolbar"><button id="p-prev" ${st.i === 0 ? "disabled" : ""}>← Previous</button>
      <button class="primary" id="p-next">${st.i === st.list.length - 1 ? "Finish" : "Next →"}</button>
      <button id="p-stop">Stop</button></div></div>`;
  $("#p-sol")?.addEventListener("click", e => { e.preventDefault(); st.reveal = !st.reveal; viewPractice(); });
  main.querySelectorAll("button[data-s]").forEach(b => b.onclick = async () => {
    const row = await api(`/api/progress/${encodeURIComponent(r.question_id)}`, { method: "PATCH", body: { status: b.dataset.s } });
    updateRow(row); st.list[st.i] = row; viewPractice(); toast("Saved");
  });
  $("#p-prev").onclick = () => { st.i--; st.reveal = false; viewPractice(); };
  $("#p-next").onclick = () => { if (st.i < st.list.length - 1) { st.i++; st.reveal = false; } else { st.list = []; toast("Done!"); } viewPractice(); };
  $("#p-stop").onclick = () => { st.list = []; viewPractice(); };
}

// ------------------------------------------------------------------ crop queue + tool
async function viewCropQueue() {
  const main = $("#main");
  const exams = await api("/api/exams");
  const need = exams.filter(e => e.crop_status === "needs_manual");
  const unv = exams.filter(e => e.crop_status === "auto" && e.n_unverified > 0);
  const done = exams.filter(e => ["verified", "manual"].includes(e.crop_status) || (e.crop_status === "auto" && !e.n_unverified));
  const rowsHtml = list => list.length ? `<div class="table-wrap"><table class="db" style="min-width:600px"><thead><tr><th>Exam</th><th>Pages</th><th>Questions</th><th>Note</th></tr></thead><tbody>
    ${list.map(e => `<tr data-e="${esc(e.id)}"><td style="white-space:nowrap"><b>${esc(e.course)}</b> ${esc(examLabel(e))}${e.version > 1 ? " v" + e.version : ""}</td>
      <td>${e.page_count ?? ""}</td><td>${e.n_questions}${e.n_unverified ? ` <span class="crop-unverified">(${e.n_unverified} unverified)</span>` : ""}</td>
      <td class="hint">${esc(e.crop_note || "")}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty">Nothing here.</div>`;
  main.innerHTML = `<h1>Cropping</h1><div class="subtitle">Draw or fix question crops. Exams the auto-cropper couldn't split come first.</div>
    <h2>Needs manual cropping (${need.length})</h2>${rowsHtml(need)}
    <h2>Auto-cropped, not yet verified (${unv.length})</h2>${rowsHtml(unv)}
    <h2>Done (${done.length})</h2><details><summary class="hint">show</summary>${rowsHtml(done)}</details>`;
  main.querySelectorAll("tr[data-e]").forEach(tr => tr.onclick = () => location.hash = "#/crop/" + encodeURIComponent(tr.dataset.e));
}

const COLORS = ["#2383e2", "#448361", "#d9730d", "#9065b0", "#c14c8a", "#337ea9", "#cb912f", "#64473a"];
async function viewCropTool(eid) {
  const main = $("#main");
  let ex = await api(`/api/exams/${encodeURIComponent(eid)}`);
  const st = { pending: [], number: nextNumber(ex), editing: null };
  function nextNumber(e) { const ns = e.questions.map(q => q.number); let n = 1; while (ns.includes(n)) n++; return n; }

  function render() {
    main.innerHTML = `<div class="toolbar" style="margin-top:0"><a href="#/crop">← Cropping queue</a></div>
      <h1>${esc(ex.course)} · ${esc(examLabel(ex))}</h1>
      <div class="subtitle">${esc(ex.id)} · status: <b>${esc(ex.crop_status)}</b>${ex.crop_note ? " · " + esc(ex.crop_note) : ""} · <a href="${fileUrl(ex.pdf_path)}" target="_blank">open PDF</a></div>
      <div class="crop-layout"><div class="crop-pages">${ex.pages.map((p, i) => `
        <div class="page-wrap" data-page="${i + 1}"><img src="${fileUrl(p)}" alt="page ${i + 1}"><span class="pnum">page ${i + 1}</span></div>`).join("")}</div>
      <div class="crop-panel">
        <h3>${st.editing ? "Editing question " + st.number : "New question"}</h3>
        <div class="row"><label>Question # <input type="number" id="c-num" min="1" value="${st.number}" style="width:70px"></label></div>
        <div class="hint">Drag on a page to draw a box. A question that continues on the next page gets one box per page, in order. Sub-questions (א/ב/ג) stay inside one question.</div>
        <div class="row"><b>${st.pending.length}</b>&nbsp;box(es) drawn
          ${st.pending.length ? `<button id="c-undo">Undo last box</button>` : ""}</div>
        <div class="row"><button class="primary" id="c-save" ${st.pending.length ? "" : "disabled"}>Save question ${st.number}</button>
          ${st.editing || st.pending.length ? `<button id="c-cancel">Cancel</button>` : ""}</div>
        <h3 style="margin-top:16px">Questions (${ex.questions.length})</h3>
        <ul class="qlist">${ex.questions.map(q => `<li><span><span style="color:${COLORS[(q.number - 1) % COLORS.length]}">■</span> Q${q.number}
            <span class="hint">${q.images.length} part(s) · ${esc(q.crop_status)}</span></span>
          <span><button data-edit="${q.number}" title="Redraw">Redraw</button> <button class="danger" data-del="${esc(q.id)}" title="Delete">✕</button></span></li>`).join("") || `<li class="hint">No questions yet.</li>`}</ul>
        <div class="row"><button id="c-done" class="primary" ${ex.questions.length ? "" : "disabled"}>All correct — mark exam done</button></div>
        <div class="hint">"Done" marks every crop here as verified and stops the pipeline from re-cropping this exam.</div>
      </div></div>`;
    drawBoxes(); wire();
  }

  function drawBoxes() {
    main.querySelectorAll(".page-wrap").forEach(wrap => {
      const page = +wrap.dataset.page, img = wrap.querySelector("img");
      const place = () => {
        wrap.querySelectorAll(".box").forEach(b => b.remove());
        const k = img.clientWidth / (img.naturalWidth || 1);
        for (const q of ex.questions) {
          if (st.editing === q.number) continue;
          q.images.filter(i => i.page === page).forEach((im, j) => {
            const c = COLORS[(q.number - 1) % COLORS.length], [x0, y0, x1, y1] = im.bbox;
            const b = h(`<div class="box" style="left:${x0 * k}px;top:${y0 * k}px;width:${(x1 - x0) * k}px;height:${(y1 - y0) * k}px;border-color:${c}">
              <span class="lab" style="background:${c}">Q${q.number}${q.images.length > 1 ? " (" + (q.images.indexOf(im) + 1) + "/" + q.images.length + ")" : ""}${q.crop_status === "unverified" ? " ?" : ""}</span></div>`);
            wrap.appendChild(b);
          });
        }
        st.pending.filter(p => p.page === page).forEach(p => {
          const [x0, y0, x1, y1] = p.bbox;
          wrap.appendChild(h(`<div class="box pending" style="left:${x0 * k}px;top:${y0 * k}px;width:${(x1 - x0) * k}px;height:${(y1 - y0) * k}px">
            <span class="lab" style="background:#e03e3e">Q${st.number} part ${st.pending.indexOf(p) + 1}</span></div>`));
        });
      };
      if (img.complete && img.naturalWidth) place(); else img.addEventListener("load", place, { once: true });
      wrap._place = place;
    });
  }

  function wire() {
    $("#c-num").oninput = e => { st.number = +e.target.value || 1; };
    $("#c-num").onchange = () => render();
    $("#c-undo")?.addEventListener("click", () => { st.pending.pop(); render(); });
    $("#c-cancel")?.addEventListener("click", () => { st.pending = []; st.editing = null; st.number = nextNumber(ex); render(); });
    $("#c-save").onclick = async () => {
      ex = await api(`/api/exams/${encodeURIComponent(eid)}/questions`, { method: "POST", body: { number: st.number, parts: st.pending } });
      toast(`Saved question ${st.number}`);
      st.pending = []; st.editing = null; st.number = nextNumber(ex); render(); dirty = true;
    };
    main.querySelectorAll("[data-edit]").forEach(b => b.onclick = () => {
      st.editing = +b.dataset.edit; st.number = st.editing; st.pending = []; render();
      toast(`Draw the new box(es) for question ${st.number}, then Save`);
    });
    main.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
      if (!confirm("Delete this question crop?")) return;
      ex = await api(`/api/questions/${encodeURIComponent(b.dataset.del)}`, { method: "DELETE" });
      st.number = nextNumber(ex); render(); dirty = true;
    });
    $("#c-done").onclick = async () => { ex = await api(`/api/exams/${encodeURIComponent(eid)}/done`, { method: "POST" }); toast("Exam marked done"); render(); dirty = true; };

    // drawing
    main.querySelectorAll(".page-wrap").forEach(wrap => {
      wrap.addEventListener("mousedown", e => {
        if (e.button !== 0 || e.target.classList.contains("lab")) return;
        const img = wrap.querySelector("img"), rect = img.getBoundingClientRect(), k = (img.naturalWidth || 1) / img.clientWidth;
        const sx = e.clientX - rect.left, sy = e.clientY - rect.top;
        const ghost = h(`<div class="box drawing" style="left:${sx}px;top:${sy}px;width:0;height:0"></div>`);
        wrap.appendChild(ghost);
        const move = ev => {
          const x = Math.max(0, Math.min(rect.width, ev.clientX - rect.left)), y = Math.max(0, Math.min(rect.height, ev.clientY - rect.top));
          Object.assign(ghost.style, { left: Math.min(sx, x) + "px", top: Math.min(sy, y) + "px", width: Math.abs(x - sx) + "px", height: Math.abs(y - sy) + "px" });
        };
        const up = ev => {
          window.removeEventListener("mousemove", move); window.removeEventListener("mouseup", up);
          const x = Math.max(0, Math.min(rect.width, ev.clientX - rect.left)), y = Math.max(0, Math.min(rect.height, ev.clientY - rect.top));
          ghost.remove();
          if (Math.abs(x - sx) < 8 || Math.abs(y - sy) < 8) return;
          st.pending.push({ page: +wrap.dataset.page, bbox: [Math.min(sx, x) * k, Math.min(sy, y) * k, Math.max(sx, x) * k, Math.max(sy, y) * k].map(Math.round) });
          render();
        };
        window.addEventListener("mousemove", move); window.addEventListener("mouseup", up);
        e.preventDefault();
      });
    });
  }
  let dirty = false;
  window.onresize = () => main.querySelectorAll(".page-wrap").forEach(w => w._place?.());
  S.leaveCrop = async () => { if (dirty) { await loadAll(); } };
  render();
}

// ------------------------------------------------------------------ tag review
function viewTags() {
  const main = $("#main");
  const rows = S.rows.filter(r => r.tags.some(t => t.state === "suggested") && !r.tags.some(t => t.state === "confirmed"));
  S.visible = rows.map(r => r.question_id);
  main.innerHTML = `<h1>Tag review</h1><div class="subtitle">Topic tags suggested by the model. Confirm them, or open a question to change them.</div>
    ${rows.length ? `<div class="toolbar"><button class="primary" id="t-all">Confirm all ${rows.length} shown</button></div>` + tableHtml(rows)
      : `<div class="note">Nothing to review. Suggested tags appear here after the tagging stage runs
         (<code>python run.py tag</code>, which needs <code>ANTHROPIC_API_KEY</code> in <code>.env</code>).
         You can also tag questions by hand from any question's detail panel.</div>`}`;
  wireTable(main);
  $("#t-all")?.addEventListener("click", async () => {
    if (!confirm(`Confirm the suggested tags on ${rows.length} questions?`)) return;
    for (const r of rows) {
      const row = await api(`/api/questions/${encodeURIComponent(r.question_id)}/tags`, { method: "PUT", body: { topics: r.tags.map(t => t.topic) } });
      updateRow(row);
    }
    await refreshMeta(); viewTags(); toast("Confirmed");
  });
}

// ------------------------------------------------------------------ exam metadata
async function viewMeta() {
  const main = $("#main");
  const exams = await api("/api/exams");
  const unknown = e => [e.year, e.semester, e.moed].includes("unknown");
  const list = [...exams].sort((a, b) => unknown(b) - unknown(a));
  const sel = (id, field, cur, opts) => `<select data-f="${field}" data-e="${esc(id)}">${opts.map(o => `<option value="${o}" ${o === cur ? "selected" : ""}>${o === "unknown" ? "?" : o}</option>`).join("")}</select>`;
  main.innerHTML = `<h1>Exam metadata</h1><div class="subtitle">Fix year / semester / moed / lecturer. Exams with unknown fields are listed first. Edits are kept on pipeline re-runs.</div>
    <div class="toolbar"><label><input type="checkbox" id="m-only" checked> only exams with unknown fields</label></div>
    <div class="table-wrap"><table class="db" style="min-width:760px"><thead><tr><th>Exam</th><th>Source</th><th>Year</th><th>Semester</th><th>Moed</th><th>Lecturer</th><th></th></tr></thead><tbody>
    ${list.map(e => `<tr data-row="${esc(e.id)}" class="${unknown(e) ? "unk" : "known"}">
      <td><b>${esc(e.course)}</b> <span class="hint">${esc(e.id)}</span>${e.meta_locked ? ' <span class="chip">edited</span>' : ""}</td>
      <td class="hint">${esc(e.source)}${e.original_path ? "<br>" + esc(e.original_path) : ""}</td>
      <td><input type="text" data-f="year" data-e="${esc(e.id)}" value="${esc(e.year === "unknown" ? "" : e.year)}" placeholder="YYYY" style="width:70px"></td>
      <td>${sel(e.id, "semester", e.semester, ["A", "B", "C", "unknown"])}</td>
      <td>${sel(e.id, "moed", e.moed, ["A", "B", "C", "unknown"])}</td>
      <td><input type="text" data-f="lecturer" data-e="${esc(e.id)}" value="${esc(e.lecturer === "unknown" ? "" : e.lecturer)}" dir="auto" style="width:140px"></td>
      <td><a href="${fileUrl(e.pdf_path)}" target="_blank">PDF</a> <button data-save="${esc(e.id)}">Save</button></td></tr>`).join("")}
    </tbody></table></div>`;
  const filter = () => main.querySelectorAll("tr.known").forEach(tr => tr.hidden = $("#m-only").checked);
  $("#m-only").onchange = filter; filter();
  if (!list.some(unknown)) main.querySelector("tbody").insertAdjacentHTML("afterbegin", `<tr><td colspan="7" class="hint">No exams with unknown metadata. Untick the box above to edit any exam.</td></tr>`);
  main.querySelectorAll("[data-save]").forEach(b => b.onclick = async () => {
    const id = b.dataset.save, body = {};
    main.querySelectorAll(`[data-e="${CSS.escape(id)}"]`).forEach(inp => { body[inp.dataset.f] = inp.value.trim() || "unknown"; });
    await api(`/api/exams/${encodeURIComponent(id)}`, { method: "PATCH", body });
    toast("Saved"); await loadAll(); viewMeta();
  });
}

// ------------------------------------------------------------------ solutions
async function viewSolutions() {
  const main = $("#main");
  const [sols, exams] = await Promise.all([api("/api/solutions"), api("/api/exams")]);
  main.innerHTML = `<h1>Solutions</h1><div class="subtitle">Solution PDFs from <code>data/manual/&lt;course&gt;/solutions/</code>. Assign the ones that couldn't be matched automatically.</div>
    ${sols.length ? `<div class="table-wrap"><table class="db" style="min-width:760px"><thead><tr><th>File</th><th>Parsed as</th><th>Matched exam</th><th></th></tr></thead><tbody>
    ${sols.map(s => `<tr><td><a href="${fileUrl(s.pdf_path)}" target="_blank">${esc((s.original_path || s.id).split("/").pop())}</a><div class="hint">${esc(s.course)}</div></td>
      <td class="hint">${esc(s.year)} · Sem ${esc(s.semester)} · Moed ${esc(MOED[s.moed] || s.moed)}</td>
      <td><select data-sol="${esc(s.id)}"><option value="">— unmatched —</option>${exams.filter(e => e.course === s.course).map(e =>
        `<option value="${esc(e.id)}" ${e.id === s.exam_id ? "selected" : ""}>${esc(examLabel(e))}${e.version > 1 ? " v" + e.version : ""}</option>`).join("")}</select>
        <span class="chip">${esc(s.match_status)}</span></td>
      <td><button data-save="${esc(s.id)}">Save</button></td></tr>`).join("")}</tbody></table></div>`
      : `<div class="note">No solutions imported yet. The Aguda academic bank needs a student login, so download solutions yourself into
         <code>data/manual/&lt;course&gt;/solutions/</code> and run <code>python run.py solutions render crop-solutions build</code>.</div>`}`;
  main.querySelectorAll("[data-save]").forEach(b => b.onclick = async () => {
    const v = main.querySelector(`[data-sol="${CSS.escape(b.dataset.save)}"]`).value || null;
    await api(`/api/solutions/${encodeURIComponent(b.dataset.save)}`, { method: "PATCH", body: { exam_id: v } });
    toast("Saved — run the crop-solutions stage to split it per question"); refreshMeta(); viewSolutions();
  });
}

// ------------------------------------------------------------------ router
function currentRoute() { return location.hash.replace(/^#\/?/, "").split("/").map(decodeURIComponent); }
async function route(keepDrawer = false) {
  if (S.leaveCrop && !location.hash.startsWith("#/crop/")) { const f = S.leaveCrop; S.leaveCrop = null; window.onresize = null; await f(); }
  if (!keepDrawer) closeDrawer();
  const [a, b] = currentRoute();
  if (a === "course" && b && course(b)) { setActiveNav("course-" + b); viewList({ fixedCourse: b, grouped: !!S.courseGrouped }); }
  else if (a === "topics") { setActiveNav("topics"); viewList({ grouped: true }); }
  else if (a === "practice") { setActiveNav("practice"); viewPractice(); }
  else if (a === "crop" && b) { setActiveNav("crop"); await viewCropTool(b); }
  else if (a === "crop") { setActiveNav("crop"); await viewCropQueue(); }
  else if (a === "tags") { setActiveNav("tags"); viewTags(); }
  else if (a === "meta") { setActiveNav("meta"); await viewMeta(); }
  else if (a === "solutions") { setActiveNav("solutions"); await viewSolutions(); }
  else { setActiveNav("all"); viewList(); }
  window.scrollTo(0, 0);
}

document.addEventListener("keydown", e => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
  if (e.key === "Escape") { if (!typing) closeDrawer(); else document.activeElement.blur(); }
  if (typing) return;
  const d = $("#drawer");
  if (!d.classList.contains("hidden") && d.dataset.q) {
    const i = S.visible.indexOf(d.dataset.q);
    if (e.key === "j" && i >= 0 && i < S.visible.length - 1) openDrawer(S.visible[i + 1]);
    if (e.key === "k" && i > 0) openDrawer(S.visible[i - 1]);
  }
  if (e.key === "/" && $("#f-q")) { e.preventDefault(); $("#f-q").focus(); }
});
window.addEventListener("hashchange", () => route());

loadAll().then(() => route()).catch(err => { $("#main").innerHTML = `<div class="note">Couldn't load data: ${esc(err.message)}</div>`; });
