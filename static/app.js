/* Студия — фронтенд. Простой vanilla-JS поверх REST + WebSocket. */
"use strict";
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];

const S = {
  screen: "start", courses: [], selCourse: null,
  lecture: null,                 // текущая идущая лекция {id, title, course}
  lastPhrase: null, dictWords: [],
  chatMode: "local", chatHist: [], localLLM: false,
  review: null, reviewMd: "", settings: {}, trHist: [],
  micTimer: null, noteTimer: null,
};

async function api(path, method = "GET", body = null) {
  const opt = { method, headers: { "Content-Type": "application/json" } };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch("/api" + path, opt);
  return r.json();
}

function toast(text, ms = 2600) {
  $("#toast").textContent = text;
  $("#toast").classList.add("on");
  setTimeout(() => $("#toast").classList.remove("on"), ms);
}

const fmt = sec => {
  sec = Math.max(0, Math.floor(sec));
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`
           : `${m}:${String(s).padStart(2, "0")}`;
};
const esc = t => (t || "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ── экраны ─────────────────────────────────────────── */
function show(name) {
  S.screen = name;
  $$(".scr").forEach(x => x.classList.remove("on"));
  $("#scr-" + name).classList.add("on");
  const railMap = { start: "lecture", live: "lecture", summary: "lecture",
    review: "lib", lib: "lib", vocab: "vocab", consp: "consp", set: "set" };
  $$("#rail .ic").forEach(b =>
    b.classList.toggle("on", b.dataset.s === railMap[name]));
  clearInterval(S.micTimer);
  if (name === "start") micLoop();
  if (name === "lib") loadLib();
  if (name === "vocab") loadVocab();
  if (name === "consp") loadConsp();
  if (name === "set") loadSettings();
}
$$("#rail .ic").forEach(b => b.onclick = () => {
  if (b.dataset.s === "lecture") show(S.lecture ? "live" : "start");
  else show(b.dataset.s);
});

/* ── WebSocket ──────────────────────────────────────── */
let ws;
function connect() {
  ws = new WebSocket(`ws://${location.host}/ws`);
  ws.onmessage = e => handle(JSON.parse(e.data));
  ws.onclose = () => setTimeout(connect, 1500);
}
function handle(ev) {
  if (ev.type === "hello") {
    // сервер перезапустился (например, после обновления) — обновляем
    // и страницу, чтобы вкладка не жила неделями со старым интерфейсом
    if (S.boot && ev.state.boot && ev.state.boot !== S.boot) {
      location.reload();
      return;
    }
    S.boot = ev.state.boot;
    S.settings = ev.state.settings;
    S.localLLM = !!ev.state.local_llm;
    renderLlm();
    refreshAiSelects();
    $("#live-src").value = S.settings.source_lang || "auto";
    if (ev.state.demo) {
      $("#start-status").textContent =
        "⚠ ДЕМО-РЕЖИМ: микрофон отключён, вместо него проиграется тестовый файл. " +
        "Для настоящей лекции запусти app.py без --demo";
      $("#start-status").className = "warnbox";
      $("#demo-badge").style.display = "";
    }
    if (ev.state.running && ev.state.lecture) {
      S.lecture = { id: ev.state.lecture.id, title: ev.state.lecture.title,
                    course: ev.state.lecture.course };
      resumeLive();
      setPaused(ev.state.paused);
    }
  }
  else if (ev.type === "paused") setPaused(ev.on);
  else if (ev.type === "partial") {
    renderPartialEs(ev.es || "");
    $("#p-ru").textContent = ev.ru || "";
  }
  else if (ev.type === "queued") addPendingCard(ev);
  else if (ev.type === "final") addFinal(ev);
  else if (ev.type === "final_drop") dropPending(ev.qid);
  else if (ev.type === "catchup") addCatchup(ev.text, ev.t0, true);
  else if (ev.type === "tick") {
    $("#timer").textContent = fmt(ev.elapsed);
    if (ev.paused !== undefined) {   // самокоррекция зависшей плашки паузы
      const shown = $("#pause-banner").style.display !== "none";
      if (shown !== ev.paused) setPaused(ev.paused);
    }
  }
  else if (ev.type === "level") {
    const pct = Math.min(100, ev.rms * 1800);
    $("#live-lvl").style.width = pct + "%";
  }
  else if (ev.type === "status") $("#statusline").textContent = ev.text;
  else if (ev.type === "share") {
    if (S.review?.lecture?.id === ev.lid) {
      if (ev.status === "updating")
        $("#share-status").textContent = "обновляю ссылку…";
      else if (ev.status === "ok") renderShare(ev.url, "обновлено ✓");
      else $("#share-status").textContent =
        "не обновилось (нет сети?) — нажми «Обновить публикацию»";
    }
  }
  else if (ev.type === "summary_progress") {
    $("#sum-status").textContent = ev.text;
    if (S.screen === "review")
      $("#rev-summary").innerHTML =
        `<p style="color:var(--dim)">${esc(ev.text)}</p>`;
  }
  else if (ev.type === "approval") {
    $("#approve-cmd").textContent = ev.command;
    $("#approve").dataset.id = ev.id;
    $("#approve").classList.add("on");
  }
  else if (ev.type === "llm") {
    S.localLLM = !!ev.running;
    renderLlm();
  }
  else if (ev.type === "agent_step") {
    chatMsg("step", `⚙ ${ev.tool} ${JSON.stringify(ev.args).slice(0, 120)}`);
  }
}
$("#approve-yes").onclick = () => answerApprove(true);
$("#approve-no").onclick = () => answerApprove(false);
function answerApprove(ok) {
  api("/agent/approve", "POST", { id: $("#approve").dataset.id, approve: ok });
  $("#approve").classList.remove("on");
}

/* ── старт ──────────────────────────────────────────── */
async function loadCourses() {
  S.courses = await api("/courses");
  if (!S.selCourse && S.courses.length) S.selCourse = S.courses[0].id;
  const box = $("#course-pick");
  box.innerHTML = "";
  for (const c of S.courses) {
    const b = document.createElement("button");
    b.className = "cb" + (c.id === S.selCourse ? " on" : "");
    b.innerHTML = `${esc(c.name)}<small>${c.lectures} лекц. · ${fmt(c.total_sec)}</small>`;
    b.onclick = () => { S.selCourse = c.id; loadCourses(); };
    box.appendChild(b);
  }
  const add = document.createElement("button");
  add.className = "cb"; add.style.borderStyle = "dashed";
  add.textContent = "+ предмет";
  add.onclick = async () => {
    const name = prompt("Название предмета:");
    if (name && name.trim()) {
      const r = await api("/courses", "POST", { name });
      S.selCourse = r.id; loadCourses();
    }
  };
  box.appendChild(add);
}

async function loadMics() {
  const mics = await api("/mics");
  const sel = $("#mic-sel");
  sel.innerHTML = `<option value="">по умолчанию</option>` +
    mics.map(m => `<option value="${m.id}">${esc(m.name)}</option>`).join("");
  sel.value = S.settings.mic_device || "";
  sel.onchange = () => api("/settings", "POST", { mic_device: sel.value });
}

function micLoop() {
  clearInterval(S.micTimer);
  const poll = async () => {
    if (S.screen !== "start") return;
    const r = await api("/miclevel");
    const pct = Math.min(100, (r.rms || 0) * 1800);
    $("#mic-lvl").style.width = pct + "%";
    const ok = (r.rms || 0) > 0.002;
    $("#mic-ok").textContent = r.error ? "нет доступа к микрофону" :
      ok ? "вас слышно" : "тихо…";
    $("#mic-ok").className = ok ? "ok" : "bad";
  };
  poll();
  S.micTimer = setInterval(poll, 1300);
}

$("#btn-start").onclick = async () => {
  $("#btn-start").disabled = true;
  $("#btn-start").textContent = "Подключаю микрофон…";
  try {
    const r = await api("/lecture/start", "POST", { course_id: S.selCourse });
    if (!r.lecture_id) throw new Error(r.error || r.detail || "не удалось начать");
    S.lecture = { id: r.lecture_id, title: r.title, course: r.course };
    S.chatHist = []; S.dictWords = []; S.trHist = [];
    pendingCards.clear();
    renderTrHist();
    $("#feed").innerHTML = ""; $("#notes").value = "";
    $("#chat-log").innerHTML = ""; $("#dict-list").innerHTML = "";
    $("#live-course").textContent = `${r.course} · ${r.title}`;
    $("#timer").textContent = "0:00";
    setPaused(false);   // не тащить плашку паузы из прошлой лекции
    show("live");
  } catch (e) { toast("Ошибка: " + e.message); }
  $("#btn-start").disabled = false;
  $("#btn-start").textContent = "Начать лекцию";
};

async function resumeLive() {
  const data = await api("/lecture/" + S.lecture.id);
  $("#feed").innerHTML = "";
  pendingCards.clear();
  for (const p of data.phrases)
    p.mark === "catchup" ? addCatchup(p.ru, p.t0, false) : addPhrase(p, false);
  $("#notes").value = data.lecture.notes_md || "";
  // восстановить словарь лекции и историю переводов
  S.dictWords = (await api("/vocab"))
    .filter(w => w.lecture_id === S.lecture.id)
    .map(w => ({ id: w.id, word: w.word, translation: w.translation }));
  renderDict();
  S.trHist = (await api("/translations?lecture_id=" + S.lecture.id))
    .map(t => ({ src: t.src, dst: t.dst }));
  renderTrHist();
  $("#live-course").textContent =
    `${S.lecture.course || ""} · ${S.lecture.title || ""}`;
  show("live");
  $("#feed").scrollTop = $("#feed").scrollHeight;
}

/* ── лента фраз ─────────────────────────────────────── */
function wrapWords(es) {
  return es.split(/(\s+)/).map(tok =>
    /[a-záéíóúñü]{3,}/i.test(tok)
      ? `<span class="es-w">${esc(tok)}</span>` : esc(tok)).join("");
}

function cardFill(div, p) {
  div.dataset.id = p.id; div.dataset.t0 = p.t0;
  div.dataset.es = p.es; div.dataset.ru = p.ru;
  const sameLang = (p.ru || "").trim() === (p.es || "").trim();
  div.innerHTML = `<div class="t">${fmt(p.t0)}</div>
    <div class="ru">${esc(p.ru || p.es)}${p.no_net ? " <span style='color:var(--dimmer)'>(без сети — оригинал)</span>" : ""}</div>
    ${sameLang ? "" : `<div class="es">${wrapWords(p.es)}</div>`}
    <button class="dots">⋯</button>`;
}

function phraseCard(p) {
  const div = document.createElement("div");
  div.className = "card" + (p.mark ? " " + p.mark : "");
  cardFill(div, p);
  return div;
}

/* серые карточки-заглушки: фраза встаёт в транскрипт СРАЗУ после нарезки —
   с текстом черновика, который движок прислал в queued (распознанное
   не пропадает с экрана) — и «дозревает» на месте, когда готов финал */
const pendingCards = new Map();
function addPendingCard(ev) {
  const feed = $("#feed");
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 200;
  const div = document.createElement("div");
  div.className = "card pending enter";
  div.dataset.qid = ev.qid;
  const es = ev.es || "", ru = ev.ru || "";
  div.innerHTML = `<div class="t">${fmt(ev.t0)}</div>
    <div class="ru">${esc(ru || es || "…")}</div>
    ${es && ru && ru !== es ? `<div class="es">${esc(es)}</div>` : ""}`;
  pendingCards.set(ev.qid, div);
  feed.appendChild(div);
  if (nearBottom) feed.scrollTop = feed.scrollHeight;
  setTimeout(() => {    // страховка: финал не пришёл — убираем заглушку
    if (pendingCards.get(ev.qid) === div) {
      pendingCards.delete(ev.qid);
      div.remove();
    }
  }, 60000);
}

/* финал: «дозревает» заглушка (или карточка добавляется в конец), а если
   движок разрезал длинный кусок на предложения — хвост (extra) встаёт
   сразу за первой карточкой, не после чужих заглушек */
function addFinal(ev) {
  const feed = $("#feed");
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 200;
  let anchor = ripenCard(ev.phrase.qid, ev.phrase);
  if (!anchor) {
    addPhrase(ev.phrase, true);
    anchor = feed.lastElementChild;
  }
  for (const p of ev.extra || []) {
    const c = phraseCard(p);
    c.classList.add("ripen");
    anchor.after(c);
    anchor = c;
    S.lastPhrase = p;
  }
  if ((ev.extra || []).length && nearBottom)
    feed.scrollTop = feed.scrollHeight;
}

function ripenCard(qid, p) {
  const div = pendingCards.get(qid);
  if (!div) return null;
  pendingCards.delete(qid);
  div.classList.remove("pending", "enter");
  div.classList.add("ripen");
  cardFill(div, p);
  S.lastPhrase = p;
  return div;
}

function dropPending(qid) {
  const div = pendingCards.get(qid);
  if (div) { pendingCards.delete(qid); div.remove(); }
}

function addPhrase(p, live) {
  const feed = $("#feed");
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 200;
  const card = phraseCard(p);
  if (live) card.classList.add("enter");   // фраза «дозрела» — спокойно въезжает
  feed.appendChild(card);
  S.lastPhrase = p;
  if (live && nearBottom) feed.scrollTop = feed.scrollHeight;
}

/* live-зона: слова появляются по одному (stable-prefix diff — без мельтешения).
   Уже показанные слова не переанимируются, даже если ASR их поправил;
   пустой partial (финализация) убирает текст мягким затуханием. */
let liveWords = [];
function renderPartialEs(text) {
  const el = $("#p-es");
  const words = text ? text.trim().split(/\s+/) : [];
  if (!words.length) {
    if (liveWords.length) {
      liveWords = [];
      el.classList.add("clearing");
      setTimeout(() => {
        if (!liveWords.length) el.innerHTML = "";
        el.classList.remove("clearing");
      }, 240);
    }
    return;
  }
  el.classList.remove("clearing");
  const spans = el.children;
  for (let i = 0; i < Math.min(spans.length, words.length); i++)
    if (spans[i].textContent !== words[i]) spans[i].textContent = words[i];
  while (el.children.length > words.length) el.lastChild.remove();
  const from = el.children.length;
  for (let i = from; i < words.length; i++) {
    const sp = document.createElement("span");
    sp.className = "w-new";
    sp.style.animationDelay = Math.min((i - from) * 60, 300) + "ms";
    sp.textContent = words[i];
    el.appendChild(sp);
  }
  liveWords = words;
}

function addCatchup(text, t0, live) {
  const feed = $("#feed");
  const div = document.createElement("div");
  div.className = "card catchup";
  div.innerHTML = `<div class="t">СВОДКА · ${fmt(t0)} · ИИ</div>
    <div class="ru">${esc(text)}</div>`;
  feed.appendChild(div);
  if (live) feed.scrollTop = feed.scrollHeight;
}

/* меню «⋯» */
let menuCard = null;
document.addEventListener("click", e => {
  const d = e.target.closest(".dots");
  if (d) {
    menuCard = d.closest(".card");
    const r = d.getBoundingClientRect(), m = $("#menu");
    m.classList.add("on");
    m.style.top = Math.min(r.bottom + 4, innerHeight - 280) + "px";
    m.style.left = Math.max(8, Math.min(r.left - 220, innerWidth - 270)) + "px";
    e.stopPropagation(); return;
  }
  if (!e.target.closest(".menu")) $("#menu").classList.remove("on");
  if (!e.target.closest(".pop") && !e.target.closest(".es-w"))
    $("#pop").classList.remove("on");
});

$$("#menu .mi").forEach(b => b.onclick = () => doAction(b.dataset.a, menuCard));

async function doAction(a, card) {
  $("#menu").classList.remove("on");
  if (!card) return;
  const id = card.dataset.id;
  if (a === "star" || a === "q") {
    const r = await api(`/phrase/${id}/mark`, "POST", { mark: a });
    card.classList.remove("star", "q");
    if (r.mark) card.classList.add(r.mark);
  }
  else if (a === "note") {
    const quote = `> «${card.dataset.es}» · ${fmt(card.dataset.t0)}`;
    $("#notes").value = ($("#notes").value + "\n" + quote).trim();
    saveNotesSoon(); switchTab("notes");
  }
  else if (a === "explain") {
    switchTab("chat");
    chatMsg("me", `объясни: «${card.dataset.ru || card.dataset.es}»`);
    const wait = chatMsg("", "…");
    const r = await api(`/phrase/${id}/explain`, "POST", {});
    wait.querySelector(".body").innerHTML = mdRender(r.content);
  }
  else if (a === "copy") {
    navigator.clipboard.writeText(`${card.dataset.ru}\n${card.dataset.es}`);
    toast("Скопировано");
  }
}

/* пауза */
function setPaused(on) {
  $("#pause-banner").style.display = on ? "" : "none";
  $("#btn-pause").style.display = on ? "none" : "";
  $("#live-lb").textContent = on ? "НА ПАУЗЕ" : "СЕЙЧАС ГОВОРИТ";
  document.querySelector("#scr-live .dot").style.animationPlayState =
    on ? "paused" : "running";
}
$("#btn-pause").onclick = async () => {
  await api("/lecture/pause", "POST", {});
  toast("Пауза — всё сохранено");
};
$("#btn-resume").onclick = () => api("/lecture/resume", "POST", {});

/* язык лекции на лету (синхронно с настройками) */
$("#live-src").onchange = async () => {
  S.settings = await api("/settings", "POST",
    { source_lang: $("#live-src").value });
  toast($("#live-src").value === "auto"
    ? "Язык определится автоматически"
    : "Язык лекции: " + $("#live-src").selectedOptions[0].text.replace("язык: ", ""));
};

/* хоткеи */
document.addEventListener("keydown", e => {
  if (!(e.metaKey || e.ctrlKey) || S.screen !== "live") return;
  const last = $("#feed .card:not(.catchup):last-of-type") ||
               [...$$("#feed .card")].filter(c => !c.classList.contains("catchup")).pop();
  if (e.key === "1") { doAction("star", last); e.preventDefault(); }
  if (e.key === "2") { doAction("q", last); e.preventDefault(); }
  if (e.key.toLowerCase() === "l") { doAction("note", last); e.preventDefault(); }
});

/* клик по слову */
document.addEventListener("click", async e => {
  const w = e.target.closest(".es-w");
  if (!w) return;
  const word = w.textContent.replace(/[.,;:!?¡¿]/g, "");
  const ctx = w.closest(".card")?.dataset.es || "";
  const r0 = w.getBoundingClientRect(), p = $("#pop");
  const info = { word, translation: "", base: "", pos: "" };
  $("#pop-w").textContent = word;
  $("#pop-pos").textContent = ""; $("#pop-tr").textContent = "…";
  p.classList.add("on");
  p.style.top = Math.min(r0.bottom + 6, innerHeight - 130) + "px";
  p.style.left = Math.max(8, Math.min(r0.left, innerWidth - 250)) + "px";
  // сохранить можно сразу, не дожидаясь перевода
  $("#pop-save").onclick = async () => {
    if (!info.translation)
      Object.assign(info, await api(
        `/word?w=${encodeURIComponent(word)}&ctx=${encodeURIComponent(ctx.slice(0, 120))}`));
    const tr = info.translation === "?" ? "" : info.translation;
    const saved = await api("/word/save", "POST",
      { word: info.base || word, translation: tr, context: ctx });
    S.dictWords.unshift({ id: saved.id, word: info.base || word, translation: tr });
    renderDict(); p.classList.remove("on"); toast("Слово в словаре");
  };
  const q = `w=${encodeURIComponent(word)}&ctx=${encodeURIComponent(ctx.slice(0, 120))}`;
  const r = await api(`/word?${q}`);          // быстрый перевод (argos)
  if ($("#pop-w").textContent !== word) return;   // уже открыли другое слово
  Object.assign(info, r);
  $("#pop-tr").textContent = info.translation;
  api(`/word/enrich?${q}`).then(en => {       // базовая форма от ИИ — добежит позже
    if ($("#pop-w").textContent !== word) return;
    Object.assign(info, en);
    $("#pop-pos").textContent =
      [en.base && en.base !== word ? "→ " + en.base : "", en.pos]
        .filter(Boolean).join(" · ");
  });
});

/* ── правая панель ──────────────────────────────────── */
function switchTab(name) {
  $$("#side-tabs .tab").forEach(t => t.classList.toggle("on", t.dataset.p === name));
  $$("#scr-live .panel").forEach(p => p.classList.toggle("on", p.id === "p-" + name));
}
$$("#side-tabs .tab").forEach(t => t.onclick = () => switchTab(t.dataset.p));

/* заметки: автосохранение */
function saveNotesSoon() {
  clearTimeout(S.noteTimer);
  $("#notes-status").textContent = "…";
  S.noteTimer = setTimeout(async () => {
    await api("/notes/replace", "POST",
              { lecture_id: S.lecture?.id, text: $("#notes").value });
    $("#notes-status").textContent = "сохранено ✓";
    setTimeout(() => {
      if ($("#notes-status").textContent === "сохранено ✓")
        $("#notes-status").textContent = "";
    }, 2000);
  }, 1200);
}
$("#notes").addEventListener("input", saveNotesSoon);

/* переводчик */
let trPair = ["ru", "es"];
$$("#tr-langs button").forEach(b => b.onclick = () => {
  $$("#tr-langs button").forEach(x => x.classList.remove("on"));
  b.classList.add("on");
  trPair = b.dataset.l.split(":");
});
$("#tr-in").addEventListener("keydown", async e => {
  if (e.key !== "Enter" || e.shiftKey) return;
  e.preventDefault();
  const text = $("#tr-in").value.trim();
  if (!text) return;
  const r = await api("/translate", "POST",
    { text, source: trPair[0], target: trPair[1] });
  const out = $("#tr-out");
  out.style.display = "block";
  out.innerHTML = `${esc(r.result || "нет сети")}<div class="src">${trPair[0]} → ${trPair[1]}</div>`;
  out.dataset.text = r.result || "";
  if (r.result) {
    S.trHist.unshift({ src: text, dst: r.result });
    renderTrHist();
  }
});

/* история переводов (в рамках лекции) */
function renderTrHist() {
  $("#tr-hist").innerHTML = S.trHist.length
    ? `<div class="thh">переводила раньше</div>` + S.trHist.map((h, i) =>
        `<div class="thr" data-i="${i}"><span class="s">${esc(h.src)}</span>
         <span class="d">${esc(h.dst)}</span></div>`).join("")
    : "";
  $$("#tr-hist .thr").forEach(rw => rw.onclick = () => {
    const h = S.trHist[+rw.dataset.i];
    $("#tr-in").value = h.src;
    const out = $("#tr-out");
    out.style.display = "block";
    out.innerHTML = esc(h.dst);
    out.dataset.text = h.dst;
  });
}
$("#btn-big").onclick = () => {
  const t = $("#tr-out").dataset.text || $("#tr-in").value;
  if (!t) return;
  $("#bigph-text").textContent = t;
  $("#bigph").classList.add("on");
};
$("#bigph").onclick = () => $("#bigph").classList.remove("on");

/* словарь лекции (✎ — правка написания/перевода) */
function renderDict() {
  $("#dict-list").innerHTML = S.dictWords.map((w, i) =>
    `<div class="wrow" data-i="${i}"><b>${esc(w.word)}</b><span class="tr">${esc(w.translation)}</span><button class="wedit" title="исправить">✎</button></div>`
  ).join("") || `<div class="phint" style="padding:4px 0">пока пусто</div>`;
  $$("#dict-list .wedit").forEach(b =>
    b.onclick = () => editDictRow(b.closest(".wrow")));
}

function editDictRow(row) {
  const w = S.dictWords[+row.dataset.i];
  row.innerHTML = `<input class="inp de-w" value="${esc(w.word)}">
    <input class="inp de-t" value="${esc(w.translation)}">
    <button class="hbtn de-ok">OK</button>`;
  const save = async () => {
    const nw = row.querySelector(".de-w").value.trim();
    const nt = row.querySelector(".de-t").value.trim();
    if (nw) { w.word = nw; w.translation = nt; }
    if (w.id)
      await api(`/word/${w.id}`, "POST",
                { word: w.word, translation: w.translation });
    renderDict();
  };
  row.querySelector(".de-ok").onclick = save;
  row.querySelectorAll("input").forEach(inp => inp.addEventListener(
    "keydown", e => { if (e.key === "Enter") save(); }));
  row.querySelector(".de-w").focus();
}

/* облачные модели (Uno Gateway или свой провайдер) */
function cloudModels() {
  try { return JSON.parse(S.settings.cloud_models || "[]").filter(Boolean); }
  catch { return []; }
}
function cloudReady() {
  return !!(S.settings.cloud_llm_url && cloudModels().length &&
    (S.settings.cloud_llm_key || S.settings.uno_api_key));
}
function fillAiSelect(sel) {
  const cur = sel.value;
  sel.innerHTML = `<option value="local">ИИ на ноутбуке</option>` +
    cloudModels().map(m =>
      `<option value="cloud:${esc(m)}">☁ ${esc(m)}</option>`).join("");
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
}
function aiChoice(sel) {
  const v = sel.value || "local";
  return v === "local" ? { mode: "local" } : { mode: "cloud", model: v.slice(6) };
}
function fillChatModel() {
  const sel = $("#chat-model");
  sel.innerHTML = cloudModels().map(m => `<option>${esc(m)}</option>`).join("");
  if (cloudModels().includes(S.settings.cloud_llm_model))
    sel.value = S.settings.cloud_llm_model;
}
function refreshAiSelects() {
  fillAiSelect($("#sum-ai"));
  fillAiSelect($("#rev-ai"));
  fillChatModel();
}
$("#chat-model").onchange = () =>   // выбор запоминается как дефолтный
  api("/settings", "POST", { cloud_llm_model: $("#chat-model").value });

/* чат */
$$("#chat-modes button").forEach(b => b.onclick = () => {
  const m = b.dataset.m;
  if (m === "cloud" || m === "agent") {
    if (!cloudReady())
      return toast("Добавь Ключ Uno (или ключ облака) в Настройках");
    if (m === "agent" && S.settings.agent_mode !== "on")
      return toast("Включи режим агента в Настройках");
  }
  S.chatMode = m;
  $$("#chat-modes button").forEach(x => x.classList.toggle("on", x === b));
  $("#chat-model").style.display =
    (m === "cloud" || m === "agent") ? "" : "none";
  renderLlm();
});

/* локальный ИИ: вкл/выкл кнопкой (Ollama живёт, только пока нужна) */
function renderLlm() {
  const bar = $("#llm-bar");
  bar.style.display = S.chatMode === "local" ? "flex" : "none";
  bar.classList.toggle("on", S.localLLM);
  $("#llm-bar-text").textContent = S.localLLM
    ? "локальный ИИ работает" : "локальный ИИ выключен";
  $("#llm-bar-btn").textContent = S.localLLM ? "Выключить" : "Запустить";
  $("#s-local-status").textContent = S.localLLM
    ? "Ollama работает ✓ — выключи, если память нужна для другого"
    : "Ollama выключена — локальный чат и конспекты недоступны";
  $("#btn-ollama").textContent = S.localLLM ? "Выключить" : "Запустить";
}

async function toggleLlm() {
  const starting = !S.localLLM;
  const btns = [$("#llm-bar-btn"), $("#btn-ollama")];
  btns.forEach(b => { b.disabled = true; });
  $("#llm-bar-text").textContent = starting
    ? "запускаю ИИ (первый ответ может думать дольше)…" : "выключаю…";
  const r = await api(starting ? "/ollama/start" : "/ollama/stop", "POST", {});
  btns.forEach(b => { b.disabled = false; });
  S.localLLM = !!r.running;
  if (r.error) toast(r.error, 5000);
  renderLlm();
}
$("#llm-bar-btn").onclick = toggleLlm;
$("#btn-ollama").onclick = toggleLlm;

function chatMsg(cls, text) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  // ответы ИИ (cls === "") — с маркдаун-разметкой, остальное как текст
  const body = cls ? esc(text) : mdRender(text);
  div.innerHTML = `<div class="who">${cls === "me" ? "Катя" : cls === "step" ? "" : "ИИ"}</div><span class="body">${body}</span>`;
  $("#chat-log").appendChild(div);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  return div;
}

async function sendChat(text) {
  chatMsg("me", text);
  S.chatHist.push({ role: "user", content: text });
  const wait = chatMsg("", "думаю…");
  const r = await api("/chat", "POST", {
    messages: S.chatHist.slice(-12), mode: S.chatMode,
    model: S.chatMode !== "local" ? $("#chat-model").value : undefined,
    lecture_id: S.lecture?.id || S.review?.lecture?.id });
  wait.querySelector(".body").innerHTML = mdRender(r.content);
  $("#chat-log").scrollTop = $("#chat-log").scrollHeight;
  S.chatHist.push({ role: "assistant", content: r.content });
}
$("#chat-in").addEventListener("keydown", e => {
  if (e.key !== "Enter") return;
  const t = $("#chat-in").value.trim();
  if (!t) return;
  $("#chat-in").value = "";
  sendChat(t);
});
$$(".quick button").forEach(b => b.onclick = () => sendChat(b.dataset.q));

/* ── стоп → итог ────────────────────────────────────── */
$("#btn-stop").onclick = async () => {
  $("#btn-stop").disabled = true;
  $("#statusline").textContent = "дописываю последние фразы…";
  const st = await api("/lecture/stop", "POST", {});
  $("#btn-stop").disabled = false;
  S.stopped = st;
  $("#sum-title").textContent =
    `${S.lecture.course} · ${S.lecture.title} · сохранено локально`;
  $("#sum-stats").innerHTML = `
    <div class="st"><b>${fmt(st.duration)}</b><span>длительность</span></div>
    <div class="st"><b>${st.phrases}</b><span>фраз</span></div>
    <div class="st"><b>${st.notes}</b><span>заметок</span></div>
    <div class="st"><b>${st.words}</b><span>новых слов</span></div>
    <div class="st"><b>${st.stars} + ${st.questions}</b><span>важное · не поняла</span></div>`;
  const lid = S.lecture.id;
  S.lecture = null;
  $("#btn-consp").onclick = () => makeSummary(lid);
  show("summary");
};
$("#btn-later").onclick = () => show("lib");

function summaryEta(ai) {
  const dur = S.stopped?.duration ??
    ((S.review?.lecture?.ended_at || 0) - (S.review?.lecture?.started_at || 0));
  const min = (dur || 0) / 60;
  if (ai.mode !== "local") return "обычно до минуты";
  return min > 25 ? "длинная лекция — до 3-4 минут"
       : min > 10 ? "пару минут" : "около минуты";
}

async function makeSummary(lid) {
  const ai = aiChoice($("#sum-ai"));
  $("#btn-consp").disabled = true;
  $("#sum-status").textContent = `ИИ пишет конспект (${summaryEta(ai)})…`;
  try {
    await api(`/summary/${lid}`, "POST", ai);
    openReview(lid);
  } catch (e) { $("#sum-status").textContent = "ошибка: " + e.message; }
  $("#btn-consp").disabled = false;
  $("#sum-status").textContent = "";
}

/* ── повторение ─────────────────────────────────────── */
/* contenteditable-конспект → обратно в Markdown */
function htmlToMd(root) {
  const inline = node => {
    let s = "";
    for (const n of node.childNodes) {
      if (n.nodeType === 3) s += n.textContent;
      else if (n.nodeName === "B" || n.nodeName === "STRONG")
        s += `**${inline(n).trim()}**`;
      else if (n.nodeName === "SUB") s += `<sub>${inline(n)}</sub>`;
      else if (n.nodeName === "BR") s += "\n";
      else s += inline(n);
    }
    return s;
  };
  let md = "";
  for (const el of root.children) {
    const t = el.nodeName;
    if (t === "H1") md += `# ${inline(el).trim()}\n\n`;
    else if (t === "H2") md += `## ${inline(el).trim()}\n\n`;
    else if (t === "H3") md += `### ${inline(el).trim()}\n\n`;
    else if (t === "UL")
      md += [...el.children].map(li => `- ${inline(li).trim()}`).join("\n") + "\n\n";
    else {
      const s = inline(el).trim();
      if (s) md += s + "\n\n";
    }
  }
  return md.trim();
}
$$("#mdbar button").forEach(b => b.onmousedown = e => {
  e.preventDefault();   // не сбрасывать выделение текста
  const c = b.dataset.c;
  if (c === "h2") document.execCommand("formatBlock", false, "h2");
  else document.execCommand(c, false, null);
});

/* панель конспекта тянется мышкой, ширина запоминается */
(() => {
  const side = $("#rev-side");
  const saved = +localStorage.revSideW;
  if (saved >= 280 && saved <= 720) side.style.width = saved + "px";
  $("#rev-split").onmousedown = e => {
    e.preventDefault();
    document.body.style.cursor = "col-resize";
    const move = ev => {
      const w = Math.min(720, Math.max(280, innerWidth - ev.clientX));
      side.style.width = w + "px";
      localStorage.revSideW = w;
    };
    const up = () => {
      document.body.style.cursor = "";
      removeEventListener("mousemove", move);
      removeEventListener("mouseup", up);
    };
    addEventListener("mousemove", move);
    addEventListener("mouseup", up);
  };
})();

function mdRender(md) {
  const lines = (md || "").split("\n");
  let out = "", inList = false;
  for (const ln of lines) {
    const inline = s => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
      .replace(/<sub>|<\/sub>/g, m => m); // sub уже экранирован — вернём
    let l = ln.trim();
    const li = l.startsWith("- ") || l.startsWith("* ");
    if (li && !inList) { out += "<ul>"; inList = true; }
    if (!li && inList) { out += "</ul>"; inList = false; }
    if (l.startsWith("### ")) out += `<h3>${inline(l.slice(4))}</h3>`;
    else if (l.startsWith("## ")) out += `<h2>${inline(l.slice(3))}</h2>`;
    else if (l.startsWith("# ")) out += `<h1>${inline(l.slice(2))}</h1>`;
    else if (li) out += `<li>${inline(l.slice(2))}</li>`;
    else if (l) out += `<p>${inline(l)}</p>`;
  }
  return out + (inList ? "</ul>" : "");
}

async function openReview(lid) {
  const data = await api("/lecture/" + lid);
  S.review = data;
  S.chatHist = [];
  const feed = $("#rev-feed");
  feed.innerHTML = "";
  for (const p of data.phrases) {
    if (p.mark === "catchup") continue;
    const c = phraseCard(p);
    c.addEventListener("click", e => {
      if (e.target.closest(".dots") || e.target.closest(".es-w")) return;
      const a = $("#player");
      a.currentTime = p.t0; a.play();
    });
    feed.appendChild(c);
  }
  $("#player").src = `/api/audio/${lid}`;
  $("#player").ontimeupdate = () => {
    const t = $("#player").currentTime;
    $$("#rev-feed .card").forEach(c => {
      const on = t >= +c.dataset.t0 - 0.3 &&
        t <= +c.dataset.t0 + 15 &&
        +c.dataset.t0 <= t;
      c.classList.toggle("now", false);
    });
    const cur = [...$$("#rev-feed .card")].filter(c => +c.dataset.t0 <= t).pop();
    if (cur) cur.classList.add("now");
  };
  $("#rev-summary").innerHTML = data.lecture.summary_md
    ? mdRender(data.lecture.summary_md)
    : `<p style="color:var(--dim)">Конспекта ещё нет — нажми «⟳ Конспект ИИ».</p>`;
  $("#rev-notes").innerHTML = (data.lecture.notes_md || "").trim()
    ? `<div class="notesdoc">${esc(data.lecture.notes_md)}</div>`
    : `<div class="phint">заметок не было</div>`;
  $("#btn-md").href = `/api/export/${lid}.md`;
  $("#btn-gen").onclick = async () => {
    const ai = aiChoice($("#rev-ai"));
    $("#btn-gen").disabled = true;
    $("#rev-summary").innerHTML =
      `<p style="color:var(--dim)">ИИ пишет конспект (${summaryEta(ai)})…</p>`;
    const r = await api(`/summary/${lid}`, "POST", ai);
    $("#rev-summary").innerHTML = mdRender(r.summary_md);
    $("#btn-gen").disabled = false;
  };
  /* правка конспекта без ручного маркдауна */
  $("#btn-edit").onclick = () => {
    const el = $("#rev-summary");
    if (el.contentEditable !== "true") {
      el.contentEditable = "true";
      el.focus();
      $("#mdbar").style.display = "flex";
      $("#btn-edit").textContent = "✓ Готово";
      $("#btn-edit").classList.add("primary");
    } else {
      el.contentEditable = "false";
      $("#mdbar").style.display = "none";
      $("#btn-edit").textContent = "✎ Править";
      $("#btn-edit").classList.remove("primary");
      const md = htmlToMd(el);
      api(`/summary/${lid}/save`, "POST", { summary_md: md });
      el.innerHTML = mdRender(md);
      toast("Конспект сохранён");
    }
  };
  $("#btn-pdf").onclick = () => {
    const w = window.open("", "_blank");
    w.document.write(`<meta charset=utf-8><style>body{max-width:720px;margin:30px auto;font:15px/1.6 -apple-system,sans-serif}</style>${$("#rev-summary").innerHTML}`);
    w.document.close(); w.print();
  };
  renderShare(data.lecture.share_slug
    ? `https://${data.lecture.share_slug}.uno4.dev/` : "", "");
  $("#btn-share").textContent =
    data.lecture.share_slug ? "Обновить публикацию" : "Поделиться";
  $("#btn-share").onclick = async () => {
    $("#btn-share").disabled = true;
    if ($("#share-box").style.display !== "none")
      $("#share-status").textContent = "обновляю…";
    else toast("Публикую…");
    const r = await api(`/share/${lid}`, "POST", {});
    $("#btn-share").disabled = false;
    if (r.url) {
      renderShare(r.url, "опубликовано ✓");
      $("#btn-share").textContent = "Обновить публикацию";
    } else toast(r.error || "не получилось", 5000);
  };
  show("review");
}

/* публикация: постоянная ссылка + статус */
function renderShare(url, status) {
  const box = $("#share-box");
  if (!url) { box.style.display = "none"; return; }
  box.style.display = "flex";
  $("#share-url").href = url;
  $("#share-url").textContent = url.replace("https://", "").replace(/\/$/, "");
  $("#share-status").textContent = status || "";
}
$("#share-open").onclick = () => window.open($("#share-url").href, "_blank");
$("#share-copy").onclick = () => {
  navigator.clipboard.writeText($("#share-url").href);
  toast("Ссылка скопирована");
};
$("#btn-back-lib").onclick = () => show("lib");
$$("#rev-tabs .tab").forEach(t => t.onclick = () => {
  $$("#rev-tabs .tab").forEach(x => x.classList.toggle("on", x === t));
  $("#rp-sum").classList.toggle("on", t.dataset.p === "sum");
  $("#rp-mynotes").classList.toggle("on", t.dataset.p === "mynotes");
});

/* ── библиотека ─────────────────────────────────────── */
let libCourse = 0;
async function loadLib() {
  const courses = await api("/courses");
  $("#lib-courses").innerHTML = "";
  const all = document.createElement("button");
  all.className = "cb" + (libCourse === 0 ? " on" : "");
  all.textContent = "Все предметы";
  all.onclick = () => { libCourse = 0; loadLib(); };
  $("#lib-courses").appendChild(all);
  for (const c of courses) {
    const b = document.createElement("button");
    b.className = "cb" + (c.id === libCourse ? " on" : "");
    b.innerHTML = `${esc(c.name)}<small>${c.lectures} лекц. · ${fmt(c.total_sec)}</small>`;
    b.onclick = () => { libCourse = c.id; loadLib(); };
    $("#lib-courses").appendChild(b);
  }
  const lecs = await api("/lectures" + (libCourse ? "?course_id=" + libCourse : ""));
  const tb = $("#lib-table tbody");
  tb.innerHTML = `<tr><th>ЛЕКЦИЯ</th><th>ДАТА</th><th>ДЛИТ.</th><th>МЕТКИ</th><th>КОНСПЕКТ</th></tr>` +
    (lecs.filter(l => l.ended_at).map(l => `
      <tr class="click" data-id="${l.id}">
        <td>${esc(l.title)} · ${esc(l.course || "")}<div class="sub">${l.phrases} фраз · ${l.notes} заметок · ${l.words} слов</div></td>
        <td class="sub">${new Date(l.started_at * 1000).toLocaleDateString("ru")}</td>
        <td>${fmt(l.duration)}</td>
        <td class="sub">${l.stars ? "важное " + l.stars : ""} ${l.questions ? "· вопросы " + l.questions : ""}</td>
        <td><span class="badge ${l.has_summary ? "ok" : ""}">${l.has_summary ? "готов" : "создать"}</span></td>
      </tr>`).join("") ||
     `<tr><td colspan="5" class="sub">пока нет записанных лекций</td></tr>`);
  $$("#lib-table tr.click").forEach(r =>
    r.onclick = () => openReview(+r.dataset.id));
}
$("#lib-search").addEventListener("keydown", async e => {
  if (e.key !== "Enter") return;
  const q = $("#lib-search").value.trim();
  const out = $("#lib-search-out");
  if (!q) { out.innerHTML = ""; return; }
  const res = await api("/search?q=" + encodeURIComponent(q));
  out.innerHTML = res.length
    ? `<div style="margin-bottom:18px">` + res.map(p =>
        `<div class="wrow click" data-id="${p.lecture_id}" style="cursor:pointer">
          <span class="tr">${esc(p.ru || p.es)}</span>
          <span class="n">${esc(p.title || "")} · ${fmt(p.t0)}</span></div>`).join("") + "</div>"
    : `<div class="phint" style="padding:0 0 14px">ничего не найдено</div>`;
  $$("#lib-search-out .wrow").forEach(r =>
    r.onclick = () => openReview(+r.dataset.id));
});

/* ── словарь ────────────────────────────────────────── */
let vocabAll = [];
async function loadVocab() {
  vocabAll = await api("/vocab");
  $("#vocab-title").textContent = `Мой словарь · ${vocabAll.length} слов`;
  $("#vocab-table tbody").innerHTML =
    `<tr><th>СЛОВО</th><th>ПЕРЕВОД</th><th>ОТКУДА</th><th>КОНТЕКСТ</th></tr>` +
    (vocabAll.map(w => `
      <tr data-id="${w.id}"><td class="ed" data-f="word" title="нажми, чтобы исправить"><b>${esc(w.word)}</b></td>
      <td class="ed" data-f="translation" title="нажми, чтобы исправить">${esc(w.translation)}</td>
      <td class="sub">${esc(w.course || "")} ${esc(w.title || "")}</td>
      <td class="sub">${esc((w.context || "").slice(0, 60))}</td></tr>`).join("") ||
     `<tr><td colspan="4" class="sub">слова появятся по клику на испанские слова в лекции</td></tr>`);
  $$("#vocab-table td.ed").forEach(td => td.onclick = () => editVocabCell(td));
}

function editVocabCell(td) {
  if (td.querySelector("input")) return;
  const id = +td.closest("tr").dataset.id;
  const w = vocabAll.find(x => x.id === id);
  const f = td.dataset.f;
  td.innerHTML = `<input class="inp" value="${esc(w[f])}" style="font-size:13px">`;
  const inp = td.querySelector("input");
  inp.focus(); inp.select();
  inp.addEventListener("keydown", e => {
    if (e.key === "Enter") inp.blur();
    if (e.key === "Escape") { inp.value = w[f]; inp.blur(); }
  });
  inp.addEventListener("blur", async () => {
    const v = inp.value.trim();
    if (v && v !== w[f]) {
      w[f] = v;
      await api(`/word/${id}`, "POST",
                { word: w.word, translation: w.translation });
      toast("Исправлено");
    }
    loadVocab();
  });
}

/* ручное добавление слова: ИИ предлагает перевод, можно править и комментировать */
function openAddWord() {
  $("#aw-word").value = $("#aw-tr").value = $("#aw-comment").value = "";
  $("#addword").classList.add("on");
  $("#aw-word").focus();
}
$("#btn-addword").onclick = openAddWord;
$("#btn-addword-live").onclick = openAddWord;
$("#aw-cancel").onclick = () => $("#addword").classList.remove("on");
async function suggestTranslation() {
  const w = $("#aw-word").value.trim();
  if (!w) return;
  $("#aw-tr").value = "…";
  try {
    const info = await api(`/word?w=${encodeURIComponent(w)}`);
    $("#aw-tr").value = info.translation === "?" ? "" : info.translation;
    if (info.translation === "?")
      toast("Не удалось перевести — впиши перевод вручную");
  } catch {
    $("#aw-tr").value = "";
    toast("Перевод недоступен — впиши вручную");
  }
}
$("#aw-ai").onclick = suggestTranslation;
$("#aw-word").addEventListener("keydown", e => {
  if (e.key === "Enter") { e.preventDefault(); suggestTranslation(); }
});
$("#aw-save").onclick = async () => {
  const w = $("#aw-word").value.trim(), tr = $("#aw-tr").value.trim();
  if (!w || !tr || tr === "…") return toast("Нужны слово и перевод");
  const saved = await api("/word/save", "POST",
    { word: w, translation: tr, context: $("#aw-comment").value.trim() });
  $("#addword").classList.remove("on");
  S.dictWords.unshift({ id: saved.id, word: w, translation: tr });
  renderDict();
  if (S.screen === "vocab") loadVocab();
  toast("Слово в словаре");
};

/* карточки */
let fc = { list: [], i: 0 };
$("#btn-cards").onclick = () => {
  if (!vocabAll.length) return toast("Словарь пока пуст");
  fc.list = [...vocabAll].sort(() => Math.random() - 0.5).slice(0, 15);
  fc.i = 0;
  showCard();
  $("#flash").classList.add("on");
};
function showCard() {
  const w = fc.list[fc.i];
  $("#f-word").textContent = w.word;
  $("#f-answer").textContent = "";
  $("#f-show").style.display = "";
  $("#f-yes").style.display = $("#f-no").style.display = "none";
}
$("#f-show").onclick = () => {
  $("#f-answer").textContent = fc.list[fc.i].translation;
  $("#f-show").style.display = "none";
  $("#f-yes").style.display = $("#f-no").style.display = "";
};
function nextCard() {
  fc.i++;
  if (fc.i >= fc.list.length) {
    $("#flash").classList.remove("on");
    toast("Готово! Повторили " + fc.list.length + " слов");
  } else showCard();
}
$("#f-yes").onclick = nextCard;
$("#f-no").onclick = () => { fc.list.push(fc.list[fc.i]); nextCard(); };
$("#f-close").onclick = () => $("#flash").classList.remove("on");

/* ── конспекты ──────────────────────────────────────── */
async function loadConsp() {
  const lecs = await api("/lectures");
  $("#consp-table tbody").innerHTML =
    `<tr><th>КОНСПЕКТ</th><th>ПРЕДМЕТ</th><th>ДАТА</th><th></th></tr>` +
    (lecs.filter(l => l.ended_at).map(l => `
      <tr class="click" data-id="${l.id}">
        <td>${esc(l.title)}</td><td class="sub">${esc(l.course || "")}</td>
        <td class="sub">${new Date(l.started_at * 1000).toLocaleDateString("ru")}</td>
        <td><span class="badge ${l.has_summary ? "ok" : ""}">${l.has_summary ? "открыть" : "создать"}</span></td>
      </tr>`).join("") ||
     `<tr><td colspan="4" class="sub">конспекты появятся после первой лекции</td></tr>`);
  $$("#consp-table tr.click").forEach(r =>
    r.onclick = () => openReview(+r.dataset.id));
}

/* ── настройки ──────────────────────────────────────── */
async function loadSettings() {
  const s = await api("/settings");
  S.settings = s;
  const mics = await api("/mics");
  $("#s-mic").innerHTML = `<option value="">по умолчанию</option>` +
    mics.map(m => `<option value="${m.id}">${esc(m.name)}</option>`).join("");
  $("#s-mic").value = s.mic_device;
  $("#s-acc").value = s.accuracy;
  $("#s-sens").value = s.sensitivity || "normal";
  $("#s-src").value = s.source_lang || "auto";
  $("#s-lang").value = s.target_lang;
  $("#s-lmodel").value = s.local_llm_model;
  $("#s-curl").value = s.cloud_llm_url;
  $("#s-ckey").value = s.cloud_llm_key;
  $("#s-cmodels").value = cloudModels().join(", ");
  $("#s-agent").value = s.agent_mode;
  $("#s-uno").value = s.uno_api_key;
  const st = await api("/state");
  S.localLLM = !!st.local_llm;
  renderLlm();
}
$("#btn-save-set").onclick = async () => {
  S.settings = await api("/settings", "POST", {
    mic_device: $("#s-mic").value, accuracy: $("#s-acc").value,
    sensitivity: $("#s-sens").value, source_lang: $("#s-src").value,
    target_lang: $("#s-lang").value, local_llm_model: $("#s-lmodel").value,
    cloud_llm_url: $("#s-curl").value, cloud_llm_key: $("#s-ckey").value,
    cloud_models: JSON.stringify($("#s-cmodels").value.split(",")
      .map(x => x.trim()).filter(Boolean)),
    agent_mode: $("#s-agent").value,
    uno_api_key: $("#s-uno").value,
  });
  refreshAiSelects();
  $("#set-status").textContent = "сохранено ✓";
  setTimeout(() => $("#set-status").textContent = "", 2000);
};

/* подбор точных имён моделей на гейтвее */
function matchModel(want, all) {
  if (all.includes(want)) return want;
  const toks = want.toLowerCase().split(/[-_.\s\/:]+/).filter(Boolean);
  let best = null, bestScore = 0;
  for (const id of all) {
    const l = id.toLowerCase();
    const score = toks.reduce((a, t) => a + (l.includes(t) ? t.length : 0), 0);
    if (score > bestScore ||
        (score === bestScore && best && score > 0 && id.length < best.length)) {
      bestScore = score; best = id;
    }
  }
  return bestScore >= 4 ? best : null;
}
$("#btn-checkmodels").onclick = async () => {
  const st = $("#s-cmodels-status");
  st.textContent = "спрашиваю гейтвей…";
  await api("/settings", "POST", {   // URL/ключи нужны серверу до проверки
    cloud_llm_url: $("#s-curl").value, cloud_llm_key: $("#s-ckey").value,
    uno_api_key: $("#s-uno").value });
  const r = await api("/cloud/models");
  if (r.error) { st.textContent = r.error; return; }
  const all = r.models || [];
  const want = $("#s-cmodels").value.split(",").map(x => x.trim()).filter(Boolean);
  const found = [], missing = [];
  for (const w of want) {
    const m = matchModel(w, all);
    if (m) found.push(m); else missing.push(w);
  }
  $("#s-cmodels").value = [...new Set(found)].join(", ");
  st.textContent = `моделей на гейтвее: ${all.length}; подобрано: ${found.length}` +
    (missing.length ? `; не нашлось: ${missing.join(", ")}` : " ✓") +
    " — нажми «Сохранить»";
};

/* ── init ───────────────────────────────────────────── */
connect();
loadCourses().then(loadMics);
micLoop();
renderDict();
