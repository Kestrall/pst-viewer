"use strict";

const PAGE = 100;
const state = { archive: null, folder: null, folderName: "", query: "", offset: 0, total: 0, active: null };
const $ = (sel) => document.querySelector(sel);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else if (v !== undefined && v !== null) node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null) node.append(c);
  return node;
}

async function api(path) {
  const res = await fetch(path);
  if (!res.ok) throw new Error(`${res.status} ${path}`);
  return res.json();
}

const dateFmt = new Intl.DateTimeFormat("fr-FR", { dateStyle: "short" });
const timeFmt = new Intl.DateTimeFormat("fr-FR", { timeStyle: "short" });
const fullFmt = new Intl.DateTimeFormat("fr-FR", { dateStyle: "full", timeStyle: "short" });

function shortDate(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  return d.toDateString() === new Date().toDateString() ? timeFmt.format(d) : dateFmt.format(d);
}

function humanSize(n) {
  if (n < 1024) return `${n} o`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} Ko`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} Mo`;
  return `${(n / 1024 ** 3).toFixed(1)} Go`;
}

function displayName(raw) {
  if (!raw) return "(inconnu)";
  const m = raw.match(/^\s*"?([^"<]+?)"?\s*<[^>]+>/);
  return m ? m[1] : raw.replace(/[<>]/g, "");
}

function people(list, max = 6) {
  const fmt = (p) => (p.name ? `${p.name} <${p.email}>` : p.email);
  if (list.length <= max) return list.map(fmt).join(", ");
  const span = el("span", {}, list.slice(0, max).map((p) => p.name || p.email).join(", "), " ");
  span.append(el("a", { href: "#", onclick: (e) => { e.preventDefault(); span.textContent = list.map(fmt).join(", "); } },
    `+ ${list.length - max} autres`));
  return span;
}

/* ---------- Arborescence ---------- */

function buildTree(folders) {
  const tree = $("#tree");
  tree.replaceChildren();
  const children = new Map();
  for (const f of folders) {
    const key = f.parent ?? "root";
    if (!children.has(key)) children.set(key, []);
    children.get(key).push(f);
  }
  const render = (parent, depth) => {
    const items = children.get(parent) || [];
    if (!items.length) return null;
    return el("ul", {}, items.map((f) => {
      const sub = render(f.id, depth + 1);
      const li = el("li", { class: depth > 0 && sub ? "collapsed" : "" });
      const caret = el("span", { class: "caret" }, sub ? (depth > 0 ? "▶" : "▼") : "");
      caret.addEventListener("click", (e) => {
        e.stopPropagation();
        li.classList.toggle("collapsed");
        caret.textContent = li.classList.contains("collapsed") ? "▶" : "▼";
      });
      const node = el("div", { class: "node", "data-key": `${state.archive}:${f.id}`, title: f.name },
        caret,
        el("span", { class: "name" }, f.name),
        el("span", { class: "badge" }, f.count ? String(f.count) : ""));
      node.addEventListener("click", () => selectFolder(state.archive, f.id, f.name));
      li.append(node);
      if (sub) li.append(sub);
      return li;
    }));
  };
  const ul = render("root", 0);
  if (ul) tree.append(ul);
}

function selectFolder(archive, folder, name) {
  document.querySelectorAll(".node.active").forEach((n) => n.classList.remove("active"));
  document.querySelector(`.node[data-key="${archive}:${folder}"]`)?.classList.add("active");
  Object.assign(state, { archive, folder, folderName: name, query: "" });
  $("#search").value = "";
  document.body.classList.remove("show-tree", "reading");
  loadList(true);
}

/* ---------- Liste ---------- */

async function loadList(reset) {
  if (reset) {
    state.offset = 0;
    $("#list").replaceChildren();
    $("#list").scrollTop = 0;
  }
  const params = new URLSearchParams({ offset: state.offset, limit: PAGE });
  const searchAll = state.query && $("#search-all").checked;
  if (state.folder && !searchAll) params.set("folder", state.folder);
  if (state.query) params.set("q", state.query);

  const data = await api(`/api/${state.archive}/messages?${params}`);
  state.total = data.total;
  state.offset += data.items.length;

  $("#list-title").textContent = state.query
    ? `Résultats pour « ${state.query} »`
    : state.folderName || "Messages";
  $("#list-count").textContent = `${data.total} élément${data.total > 1 ? "s" : ""}`;

  const showFolder = Boolean(state.query && searchAll);
  for (const m of data.items) {
    const li = el("li", { class: "item", role: "option", "data-id": m.id },
      el("span", { class: "from" }, m.kind === "event" ? "📅 Événement" : displayName(m.sender)),
      el("span", { class: "date" }, `${m.has_att ? "📎 " : ""}${shortDate(m.date)}`),
      el("span", { class: "subject" }, m.subject || "(sans objet)"),
      el("span", { class: "snippet" },
        showFolder ? el("span", { class: "folder-tag" }, m.folder_name) : null,
        m.snippet || ""));
    li.addEventListener("click", () => openMessage(m.id, li));
    $("#list").append(li);
  }
  if (!data.items.length && reset) {
    $("#list").append(el("li", { class: "item muted" }, "Aucun message"));
  }
  $("#more").hidden = state.offset >= state.total;
}

/* ---------- Lecture ---------- */

async function openMessage(id, li) {
  document.querySelectorAll(".item.active").forEach((n) => n.classList.remove("active"));
  li?.classList.add("active");
  state.active = id;
  document.body.classList.add("reading");
  const reader = $("#reader");
  reader.replaceChildren(el("div", { class: "empty muted" }, "Chargement…"));

  const m = await api(`/api/${state.archive}/message/${id}`);
  if (state.active !== id) return;
  const back = el("button", { class: "btn back", onclick: () => document.body.classList.remove("reading") }, "← Retour");

  if (m.kind === "event") {
    const meta = el("dl", { class: "meta" },
      el("dt", {}, "Début"), el("dd", {}, m.start ? fullFmt.format(new Date(m.start * 1000)) : "?"),
      el("dt", {}, "Fin"), el("dd", {}, m.end ? fullFmt.format(new Date(m.end * 1000)) : "?"),
      m.location ? [el("dt", {}, "Lieu"), el("dd", {}, m.location)] : null,
      m.organizer ? [el("dt", {}, "Organisateur"), el("dd", {}, m.organizer)] : null);
    reader.replaceChildren(
      el("div", { class: "msg-head" }, back, el("h2", {}, `📅 ${m.subject || "(sans titre)"}`), meta),
      el("div", { class: "body" }, el("pre", {}, m.text || "")));
    return;
  }

  const meta = el("dl", { class: "meta" },
    el("dt", {}, "De"), el("dd", {}, people(m.from) || "(inconnu)"),
    m.to.length ? [el("dt", {}, "À"), el("dd", {}, people(m.to))] : null,
    m.cc.length ? [el("dt", {}, "Cc"), el("dd", {}, people(m.cc))] : null,
    el("dt", {}, "Date"), el("dd", {}, m.date ? fullFmt.format(new Date(m.date * 1000)) : m.date_raw || "?"));

  const body = el("div", { class: "body" });
  const headersBox = el("pre", { class: "headers", hidden: "" },
    m.headers.map(([k, v]) => `${k}: ${v}`).join("\n"));
  const actions = el("div", { class: "actions" });

  let mode = m.html ? "html" : "text";
  let remote = false;
  const render = () => {
    body.replaceChildren();
    if (mode === "html") {
      const notice = remote ? null : el("div", { class: "notice" },
        "Images distantes bloquées pour protéger votre vie privée. ",
        el("a", { href: "#", onclick: (e) => { e.preventDefault(); remote = true; render(); } }, "Les afficher"));
      const frame = el("iframe", {
        sandbox: "allow-same-origin allow-popups allow-popups-to-escape-sandbox",
        src: `${m.html_url}${remote ? "?remote=1" : ""}`,
        title: "Contenu du message",
      });
      frame.addEventListener("load", () => {
        try {
          const doc = frame.contentDocument;
          frame.style.height = `${Math.max(400, doc.documentElement.scrollHeight + 20)}px`;
        } catch (_) { /* hauteur par défaut */ }
      });
      if (notice) body.append(notice);
      body.append(frame);
    } else {
      body.append(el("pre", {}, m.text || "(message vide)"));
    }
    actions.querySelectorAll("[data-mode]").forEach((b) => b.classList.toggle("on", b.dataset.mode === mode));
  };

  if (m.html && m.text) {
    for (const [key, label] of [["html", "HTML"], ["text", "Texte"]]) {
      actions.append(el("button", { class: "btn", "data-mode": key, onclick: () => { mode = key; render(); } }, label));
    }
  }
  actions.append(
    el("button", { class: "btn", onclick: () => headersBox.toggleAttribute("hidden") }, "En-têtes"),
    el("a", { class: "btn", href: m.raw }, "Télécharger .eml"));

  const atts = m.attachments.length
    ? el("div", { class: "atts" }, m.attachments.map((a) =>
        el("a", { class: "att", href: a.url, target: "_blank", rel: "noopener", title: a.name },
          "📎", el("span", { class: "att-name" }, a.name), el("span", { class: "att-size" }, humanSize(a.size)))))
    : null;

  reader.replaceChildren(
    el("div", { class: "msg-head" }, back, el("h2", {}, m.subject || "(sans objet)"), meta, actions),
    headersBox, atts, body);
  reader.scrollTop = 0;
  render();
}

/* ---------- Démarrage ---------- */

let searchTimer;
$("#search").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = $("#search").value.trim();
    loadList(true);
  }, 300);
});
$("#search-form").addEventListener("submit", (e) => {
  e.preventDefault();
  state.query = $("#search").value.trim();
  loadList(true);
});
$("#search-all").addEventListener("change", () => state.query && loadList(true));
$("#more").addEventListener("click", () => loadList(false));
$("#toggle-tree").addEventListener("click", () => document.body.classList.toggle("show-tree"));

document.addEventListener("keydown", (e) => {
  if (e.target.matches("input")) return;
  if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "j" && e.key !== "k") return;
  const items = [...document.querySelectorAll(".item[data-id]")];
  const i = items.findIndex((n) => n.classList.contains("active"));
  const next = items[(e.key === "ArrowDown" || e.key === "j") ? i + 1 : Math.max(0, i - 1)];
  if (next) {
    e.preventDefault();
    next.scrollIntoView({ block: "nearest" });
    openMessage(Number(next.dataset.id), next);
  }
});

/* ---------- Écrans ---------- */

function show(screen) {
  for (const id of ["welcome", "loading", "app"]) $(`#${id}`).hidden = id !== screen;
}

let pollTimer;
async function route() {
  clearTimeout(pollTimer);
  const m = location.hash.match(/^#\/archive\/(\d+)/);
  if (!m) return showWelcome();
  try {
    const arc = await api(`/api/${m[1]}/status`);
    if (arc.state === "ready") return showArchive(arc);
    showLoading(arc);
  } catch (_) {
    location.hash = "#/";
  }
}

function showLoading(arc) {
  show("loading");
  document.title = `Ouverture de ${arc.name} – PST Viewer`;
  $("#loading-title").textContent = arc.name;
  const bar = $("#progress-bar");
  const progress = bar.parentElement;
  if (arc.state === "error") {
    $("#loading-step").textContent = `Erreur : ${arc.error}`;
    $(".spinner").hidden = true;
    progress.hidden = true;
    return;
  }
  $(".spinner").hidden = false;
  progress.hidden = false;
  if (arc.state === "indexing" && arc.total) {
    progress.classList.remove("indeterminate");
    bar.style.width = `${(100 * arc.done / arc.total).toFixed(1)}%`;
    $("#loading-step").textContent = `Étape 2/2 : indexation ${arc.done.toLocaleString("fr-FR")} / ${arc.total.toLocaleString("fr-FR")} éléments`;
  } else {
    progress.classList.add("indeterminate");
    bar.style.width = "";
    $("#loading-step").textContent = arc.done
      ? `Étape 1/2 : extraction (${arc.done.toLocaleString("fr-FR")} éléments, ${humanSize(arc.size)})`
      : `Étape 1/2 : extraction de l'archive (${humanSize(arc.size)})…`;
  }
  pollTimer = setTimeout(route, 1000);
}

async function showArchive(arc) {
  const fresh = state.archive !== arc.id || !$("#tree").childElementCount;
  show("app");
  document.title = `${arc.name} – PST Viewer`;
  $("#archive-name").textContent = arc.name;
  $("#archive-name").title = arc.path;
  if (!fresh) return;
  Object.assign(state, { archive: arc.id, folder: null, folderName: "", query: "", active: null });
  $("#search").value = "";
  $("#list").replaceChildren();
  $("#reader").replaceChildren(el("div", { class: "empty muted" }, "Aucun message sélectionné"));
  const folders = await api(`/api/${arc.id}/folders`);
  buildTree(folders);
  const first = folders.find((f) => f.count > 0);
  if (first) selectFolder(arc.id, first.id, first.name);
}

/* ---------- Accueil ---------- */

async function showWelcome() {
  show("welcome");
  document.title = "PST Viewer";
  $("#welcome-error").hidden = true;
  const recent = await api("/api/recent").catch(() => []);
  $("#recent-block").hidden = !recent.length;
  $("#recent").replaceChildren(...recent.map((r) => fileRow("🗂️", r.name, r.path,
    `${humanSize(r.size)} · ${dateFmt.format(new Date(r.opened * 1000))}`, () => openPst(r.path), "pst")));
  if (!$("#browser").childElementCount) browseTo(localStorage.getItem("pst-viewer:dir") || "");
}

function fileRow(icon, name, title, meta, onclick, cls = "") {
  return el("li", { class: cls, title, onclick },
    el("span", {}, icon), el("span", { class: "fname" }, name),
    meta ? el("span", { class: "fmeta" }, meta) : null);
}

async function browseTo(path) {
  const data = await api(`/api/browse?path=${encodeURIComponent(path)}`);
  try { localStorage.setItem("pst-viewer:dir", data.path); } catch (_) { /* stockage indisponible */ }

  $("#places").replaceChildren(...data.places.map((p) =>
    el("button", { class: "place", type: "button", title: p.path, onclick: () => browseTo(p.path) }, p.name)));

  $("#up").disabled = !data.parent;
  $("#up").onclick = () => data.parent && browseTo(data.parent);

  const segments = data.path.split("/").filter(Boolean);
  const crumbs = [el("button", { type: "button", onclick: () => browseTo("/") }, "/")];
  segments.forEach((seg, i) => {
    const target = "/" + segments.slice(0, i + 1).join("/");
    if (i) crumbs.push(el("span", { class: "sep" }, "/"));
    crumbs.push(el("button", { type: "button", onclick: () => browseTo(target) }, seg));
  });
  $("#crumbs").replaceChildren(...crumbs);
  $("#crumbs").scrollLeft = $("#crumbs").scrollWidth;

  const rows = data.entries.map((e) => e.type === "dir"
    ? fileRow("📁", e.name, e.path, "", () => browseTo(e.path))
    : fileRow("📦", e.name, e.path, humanSize(e.size), () => openPst(e.path), "pst"));
  if (!rows.length) rows.push(el("li", { class: "empty-row" }, "Aucun sous-dossier ni fichier .pst ici"));
  $("#browser").replaceChildren(...rows);
  $("#browser").scrollTop = 0;
}

async function openPst(path) {
  $("#welcome-error").hidden = true;
  const res = await fetch("/api/open", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ path }),
  });
  const data = await res.json();
  if (!res.ok) {
    $("#welcome-error").textContent = data.error || "Impossible d'ouvrir ce fichier.";
    $("#welcome-error").hidden = false;
    return;
  }
  location.hash = `#/archive/${data.id}`;
}

$("#path-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const path = $("#path-input").value.trim();
  if (path) openPst(path);
});
$("#loading-back").addEventListener("click", () => { location.hash = "#/"; });

window.addEventListener("hashchange", route);
route();
