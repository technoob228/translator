"""Studio — сервер приложения. Запуск: .venv/bin/python app.py
Тест без микрофона: .venv/bin/python app.py --demo test_es.aiff --speed 3
UI: http://localhost:8899
"""

import argparse
import asyncio
import faulthandler
import io
import sys
import threading
import time
from pathlib import Path

faulthandler.enable(file=sys.stderr)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles

import db
import engine as eng
import llm

app = FastAPI()
ROOT = Path(__file__).parent
clients = set()
LOOP = None
DEMO_FILE = None
DEMO_SPEED = 3.0
_last_level = 0.0


def emit(event):
    """Thread-safe отправка события всем WS-клиентам (с троттлингом level)."""
    global _last_level
    if event.get("type") == "level":
        now = time.time()
        if now - _last_level < 0.15:
            return
        _last_level = now
    if LOOP:
        asyncio.run_coroutine_threadsafe(_broadcast(event), LOOP)


async def _broadcast(event):
    dead = []
    for ws in clients:
        try:
            await ws.send_json(event)
        except Exception:
            dead.append(ws)
    for ws in dead:
        clients.discard(ws)


engine = eng.Engine(emit)
llm.broker.notify = emit


@app.on_event("startup")
async def startup():
    global LOOP
    LOOP = asyncio.get_running_loop()
    db.init()
    engine.preload()
    if not DEMO_FILE:
        engine.start_monitor()
    asyncio.create_task(_ticker())
    asyncio.create_task(_catchup_loop())


async def _ticker():
    while True:
        await asyncio.sleep(1)
        if engine.running:
            # paused в каждом тике — самокоррекция зависшего баннера в UI
            await _broadcast({"type": "tick", "elapsed": engine.elapsed(),
                              "paused": engine.paused})


async def _catchup_loop():
    while True:
        await asyncio.sleep(600)
        if engine.running and llm.local_available():
            lid = engine.lecture_id
            try:
                text = await asyncio.to_thread(llm.catchup, lid)
            except Exception:
                continue
            if text and engine.running and engine.lecture_id == lid:
                t0 = engine.elapsed()
                pid = db.run("INSERT INTO phrases(lecture_id,t0,t1,es,ru,mark) "
                             "VALUES(?,?,?,?,?, 'catchup')", (lid, t0, t0, "", text))
                await _broadcast({"type": "catchup", "id": pid, "t0": t0, "text": text})


# ------------------------------------------------------------------ ws / state

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    try:
        await ws.send_json({"type": "hello", "state": current_state()})
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        clients.discard(ws)


def current_state():
    lec = None
    if engine.running:
        lec = db.one("SELECT l.*, c.name course FROM lectures l "
                     "LEFT JOIN courses c ON c.id=l.course_id WHERE l.id=?",
                     (engine.lecture_id,))
    return {"running": engine.running, "paused": engine.paused, "lecture": lec,
            "elapsed": engine.elapsed(), "demo": bool(DEMO_FILE),
            "local_llm": llm.local_available(),
            "settings": db.get_settings()}


@app.get("/api/state")
def api_state():
    return current_state()


# ------------------------------------------------------------------ лекция

@app.post("/api/lecture/start")
def lecture_start(body: dict):
    course = db.one("SELECT * FROM courses WHERE id=?", (body["course_id"],))
    n = db.one("SELECT COUNT(*) n FROM lectures WHERE course_id=?", (course["id"],))["n"]
    lid = db.start_lecture(course["id"], f"Лекция {n + 1}")
    engine.stop_monitor()      # освобождаем устройство перед стартом
    try:
        engine.start(lid, demo_file=DEMO_FILE, demo_speed=DEMO_SPEED)
    except Exception as e:
        engine.start_monitor()
        return {"error": str(e)}
    return {"lecture_id": lid, "title": f"Лекция {n + 1}", "course": course["name"]}


@app.post("/api/lecture/pause")
def lecture_pause():
    engine.pause()
    return {"paused": True}


@app.post("/api/lecture/resume")
def lecture_resume():
    engine.resume()
    return {"paused": False}


@app.post("/api/lecture/stop")
def lecture_stop():
    lid = engine.stop()
    engine.start_monitor()
    return db.lecture_stats(lid)


@app.get("/api/lectures")
def lectures(course_id: int = 0):
    where = "WHERE l.course_id=?" if course_id else ""
    args = (course_id,) if course_id else ()
    out = []
    for l in db.rows(f"SELECT l.*, c.name course FROM lectures l "
                     f"LEFT JOIN courses c ON c.id=l.course_id {where} "
                     f"ORDER BY l.started_at DESC", args):
        st = db.lecture_stats(l["id"])
        l.update(phrases=st["phrases"], notes=st["notes"], words=st["words"],
                 stars=st["stars"], questions=st["questions"],
                 duration=st["duration"], has_summary=bool(l["summary_md"]))
        l.pop("summary_md", None)
        out.append(l)
    return out


@app.get("/api/lecture/{lid}")
def lecture_get(lid: int):
    lec = db.one("SELECT l.*, c.name course FROM lectures l "
                 "LEFT JOIN courses c ON c.id=l.course_id WHERE l.id=?", (lid,))
    if lec and not (lec.get("notes_md") or "").strip():
        old = db.rows("SELECT * FROM notes WHERE lecture_id=? ORDER BY t", (lid,))
        if old:   # заметки старого формата (по строкам) — склеиваем в документ
            lec["notes_md"] = "\n".join(n["text"] for n in old)
    return {"lecture": lec,
            "phrases": db.rows("SELECT * FROM phrases WHERE lecture_id=? ORDER BY t0", (lid,))}


@app.get("/api/search")
def search(q: str):
    needle = q.casefold()
    out = []
    for p in db.rows(
            "SELECT p.*, l.title, c.name course FROM phrases p "
            "JOIN lectures l ON l.id=p.lecture_id "
            "LEFT JOIN courses c ON c.id=l.course_id "
            "WHERE p.mark != 'catchup' ORDER BY l.started_at DESC"):
        if needle in (p["ru"] or "").casefold() or needle in (p["es"] or "").casefold():
            out.append(p)
            if len(out) >= 50:
                break
    return out


# ------------------------------------------------------------------ предметы

@app.get("/api/courses")
def courses():
    out = []
    for c in db.rows("SELECT * FROM courses ORDER BY id"):
        st = db.one("SELECT COUNT(*) n, COALESCE(SUM(ended_at-started_at),0) d "
                    "FROM lectures WHERE course_id=? AND ended_at IS NOT NULL", (c["id"],))
        c.update(lectures=st["n"], total_sec=st["d"])
        out.append(c)
    return out


@app.post("/api/courses")
def course_add(body: dict):
    cid = db.run("INSERT INTO courses(name, schedule) VALUES(?,?)",
                 (body["name"].strip(), body.get("schedule", "")))
    return {"id": cid}


# ------------------------------------------------------------------ фразы/заметки

@app.post("/api/phrase/{pid}/mark")
def phrase_mark(pid: int, body: dict):
    cur = db.one("SELECT mark FROM phrases WHERE id=?", (pid,))
    new = "" if cur and cur["mark"] == body["mark"] else body["mark"]
    db.run("UPDATE phrases SET mark=? WHERE id=?", (new, pid))
    return {"mark": new}


@app.post("/api/note")
def note_add(body: dict):
    lid = body.get("lecture_id") or engine.lecture_id
    t = body.get("t", engine.elapsed())
    nid = db.run("INSERT INTO notes(lecture_id,t,text) VALUES(?,?,?)",
                 (lid, t, body["text"]))
    return {"id": nid, "t": t}


@app.post("/api/notes/replace")
def notes_replace(body: dict):
    """Заметки лекции — один документ, сохраняется целиком как есть."""
    lid = body.get("lecture_id") or engine.lecture_id
    if not lid:
        return {"ok": False}
    db.run("UPDATE lectures SET notes_md=? WHERE id=?", (body["text"], lid))
    return {"ok": True}


# ------------------------------------------------------------------ перевод/словарь

@app.post("/api/translate")
def api_translate(body: dict):
    out = eng.translate(body["text"], body.get("source", "ru"),
                        body.get("target", "es"))
    if out:   # история переводов — видно, что уже переводила
        db.run("INSERT INTO translations(lecture_id,src,dst,source_lang,"
               "target_lang,created_at) VALUES(?,?,?,?,?,?)",
               (engine.lecture_id, body["text"], out, body.get("source", "ru"),
                body.get("target", "es"), time.time()))
    return {"result": out or "", "ok": out is not None}


@app.get("/api/translations")
def api_translations(lecture_id: int = 0):
    where = "WHERE lecture_id=?" if lecture_id else ""
    args = (lecture_id,) if lecture_id else ()
    return db.rows(f"SELECT * FROM translations {where} "
                   f"ORDER BY id DESC LIMIT 100", args)


@app.get("/api/word")
def api_word(w: str, ctx: str = ""):
    return llm.word_info(w, ctx)


@app.get("/api/word/enrich")
def api_word_enrich(w: str, ctx: str = ""):
    return llm.word_enrich(w, ctx)


@app.post("/api/word/save")
def word_save(body: dict):
    wid = db.run("INSERT INTO words(word,translation,context,lecture_id,t,added_at) "
                 "VALUES(?,?,?,?,?,?)",
                 (body["word"], body["translation"], body.get("context", ""),
                  engine.lecture_id, engine.elapsed(), time.time()))
    return {"id": wid}


@app.post("/api/word/{wid}")
def word_update(wid: int, body: dict):
    """Правка написания/перевода слова из словаря."""
    db.run("UPDATE words SET word=?, translation=? WHERE id=?",
           (body["word"].strip(), body["translation"].strip(), wid))
    return {"ok": True}


@app.get("/api/vocab")
def vocab():
    return db.rows("SELECT w.*, l.title, c.name course FROM words w "
                   "LEFT JOIN lectures l ON l.id=w.lecture_id "
                   "LEFT JOIN courses c ON c.id=l.course_id "
                   "ORDER BY w.added_at DESC")


@app.get("/api/vocab.tsv")
def vocab_tsv():
    lines = [f"{w['word']}\t{w['translation']}\t{w['context']}"
             for w in db.rows("SELECT * FROM words ORDER BY added_at")]
    return PlainTextResponse("\n".join(lines), headers={
        "Content-Disposition": "attachment; filename=anki.tsv"})


# ------------------------------------------------------------------ Ollama вкл/выкл

@app.post("/api/ollama/start")
def ollama_start():
    res = llm.ollama_start()
    emit({"type": "llm", "running": res["running"]})
    return res


@app.post("/api/ollama/stop")
def ollama_stop():
    res = llm.ollama_stop()
    emit({"type": "llm", "running": res["running"]})
    return res


# ------------------------------------------------------------------ LLM

@app.post("/api/chat")
def api_chat(body: dict):
    mode = body.get("mode", "local")
    model = body.get("model") or None
    msgs = body["messages"]
    lid = body.get("lecture_id") or engine.lecture_id
    if mode == "agent":
        text, steps = llm.agent_chat(msgs, lid, emit, model=model)
        return {"content": text, "steps": steps, "mode": "agent"}
    try:
        text, used = llm.chat(msgs, mode=mode, lecture_id=lid, model=model)
        return {"content": text, "mode": used}
    except Exception as e:
        hint = ("Локальный ИИ выключен. Нажми «Запустить» над чатом "
                "(или в Настройках), либо настрой облачную модель."
                if mode == "local" else f"Ошибка облачной модели: {e}")
        return {"content": hint, "mode": "error"}


@app.post("/api/agent/approve")
def agent_approve(body: dict):
    llm.broker.answer(body["id"], bool(body.get("approve")))
    return {"ok": True}


@app.post("/api/summary/{lid}")
def make_summary(lid: int, body: dict = None):
    mode = (body or {}).get("mode", "local")
    model = (body or {}).get("model") or None
    if mode == "local" and not llm.local_available():
        cfg = llm.config("cloud")
        mode = "cloud" if cfg["mode"] == "cloud" else "local"
    md = llm.summarize_lecture(lid, mode=mode, model=model)
    _share_refresh(lid)
    return {"summary_md": md}


@app.get("/api/cloud/models")
def cloud_models():
    try:
        return llm.gateway_models()
    except Exception as e:
        return {"error": str(e)}


@app.post("/api/summary/{lid}/save")
def save_summary(lid: int, body: dict):
    db.run("UPDATE lectures SET summary_md=? WHERE id=?", (body["summary_md"], lid))
    _share_refresh(lid)
    return {"ok": True}


@app.post("/api/phrase/{pid}/explain")
def explain_phrase(pid: int):
    p = db.one("SELECT * FROM phrases WHERE id=?", (pid,))
    msgs = [{"role": "user", "content":
             f"Объясни простыми словами, что значит фраза лектора: «{p['es']}» "
             f"(перевод: «{p['ru']}»). 2-4 предложения."}]
    mode = "local" if llm.local_available() else "cloud"
    try:
        text, used = llm.chat(msgs, mode=mode, lecture_id=p["lecture_id"])
    except Exception:
        text, used = "Модель недоступна — запусти Ollama или настрой облако.", "error"
    return {"content": text, "mode": used}


# ------------------------------------------------------------------ экспорт/шеринг

def _export_md(lid):
    lec = db.one("SELECT l.*, c.name course FROM lectures l "
                 "LEFT JOIN courses c ON c.id=l.course_id WHERE l.id=?", (lid,))
    date = time.strftime("%d.%m.%Y", time.localtime(lec["started_at"]))
    parts = [lec["summary_md"] or f"# {lec['title']}"]
    parts.append(f"\n*{lec['course'] or ''} · {date}*\n")
    notes = (lec.get("notes_md") or "").strip() or "\n".join(
        n["text"] for n in db.rows(
            "SELECT * FROM notes WHERE lecture_id=? ORDER BY t", (lid,)))
    if notes.strip() and "## Мои заметки" not in parts[0]:
        parts.append("\n## Мои заметки\n" + notes)
    parts.append("\n## Полный транскрипт\n")
    for p in db.rows("SELECT * FROM phrases WHERE lecture_id=? AND mark!='catchup' "
                     "ORDER BY t0", (lid,)):
        m = {"star": " ⭐", "q": " ❓"}.get(p["mark"], "")
        mm, ss = divmod(int(p["t0"]), 60)
        parts.append(f"**[{mm:02d}:{ss:02d}]{m}** {p['ru']}  \n"
                     f"<sub>{p['es']}</sub>\n")
    return "\n".join(parts)


@app.get("/api/export/{lid}.md")
def export_md(lid: int):
    md = _export_md(lid)
    return PlainTextResponse(md, headers={
        "Content-Disposition": f"attachment; filename=lecture_{lid}.md"})


def _md_html(md):
    """Мини-рендер Markdown → HTML для публикации: заголовки, списки,
    **жирный**; <sub> из транскрипта сохраняется."""
    import html as h
    import re as _re

    def inline(s):
        s = h.escape(s, quote=False)
        s = s.replace("&lt;sub&gt;", "<sub>").replace("&lt;/sub&gt;", "</sub>")
        return _re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)

    out, inlist = [], False
    for ln in md.split("\n"):
        l = ln.strip()
        li = l.startswith("- ") or l.startswith("* ")
        if li and not inlist:
            out.append("<ul>"); inlist = True
        if not li and inlist:
            out.append("</ul>"); inlist = False
        if l.startswith("### "):
            out.append(f"<h3>{inline(l[4:])}</h3>")
        elif l.startswith("## "):
            out.append(f"<h2>{inline(l[3:])}</h2>")
        elif l.startswith("# "):
            out.append(f"<h1>{inline(l[2:])}</h1>")
        elif li:
            out.append(f"<li>{inline(l[2:].lstrip())}</li>")
        elif l:
            out.append(f"<p>{inline(l)}</p>")
    if inlist:
        out.append("</ul>")
    return "\n".join(out)


def _deploy_page(lid):
    """Собирает html конспекта и деплоит; slug лекции постоянный —
    повторная публикация ОБНОВЛЯЕТ тот же адрес, а не плодит новые."""
    import requests
    key = db.get_settings()["uno_api_key"]
    if not key:
        raise RuntimeError("Добавь ключ Uno в Настройках")
    lec = db.one("SELECT * FROM lectures WHERE id=?", (lid,))
    md = _export_md(lid)
    page = ("<!doctype html><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>Конспект</title>"
            "<style>body{max-width:760px;margin:40px auto;padding:0 18px;"
            "font:16px/1.65 -apple-system,'SF Pro Text',sans-serif;color:#1c1c1e}"
            "h1{font-size:26px;margin:0 0 6px}h2{font-size:19px;margin:26px 0 8px}"
            "h3{font-size:16px;margin:18px 0 6px}p{margin:7px 0}"
            "ul{margin:6px 0;padding-left:22px}li{margin:4px 0}"
            "sub{display:block;color:#8e8e93;font-size:12.5px;line-height:1.4}"
            "</style>" + _md_html(md))
    path = llm.WORKSPACE / f"conspect_{lid}.html"
    path.write_text(page)
    data = {"slug": lec["share_slug"]} if lec.get("share_slug") else {}
    with open(path, "rb") as f:
        r = requests.post("https://api.getuno.xyz/api/v1/deploy",
                          headers={"Authorization": f"Bearer {key}"},
                          files={"file": ("index.html", f)}, data=data,
                          timeout=60)
    r.raise_for_status()
    j = r.json()
    if j.get("slug") and j["slug"] != lec.get("share_slug"):
        db.run("UPDATE lectures SET share_slug=? WHERE id=?", (j["slug"], lid))
    return j.get("url") or f"https://{j['slug']}.uno4.dev/"


def _share_refresh(lid):
    """После правки/пересборки конспекта — фоновое обновление публикации."""
    lec = db.one("SELECT share_slug FROM lectures WHERE id=?", (lid,))
    if not (lec and lec["share_slug"] and db.get_settings()["uno_api_key"]):
        return
    emit({"type": "share", "lid": lid, "status": "updating"})

    def work():
        try:
            url = _deploy_page(lid)
            emit({"type": "share", "lid": lid, "status": "ok", "url": url})
        except Exception:
            emit({"type": "share", "lid": lid, "status": "err"})
    threading.Thread(target=work, daemon=True).start()


@app.post("/api/share/{lid}")
def share_lecture(lid: int):
    """Кнопка «Поделиться/Обновить публикацию» — синхронный деплой."""
    try:
        return {"url": _deploy_page(lid)}
    except Exception as e:
        return {"error": str(e)}


# ------------------------------------------------------------------ железо/настройки

@app.get("/api/mics")
def mics():
    return eng.mic_list()


@app.get("/api/miclevel")
def miclevel():
    if engine.running:
        return {"rms": 0.01}
    if not getattr(engine, "monitor", None):
        engine.start_monitor()
    err = getattr(engine, "monitor_err", "")
    return {"rms": getattr(engine, "monitor_rms", 0.0),
            **({"error": err} if err else {})}


@app.get("/api/settings")
def settings_get():
    return db.get_settings()


@app.post("/api/settings")
def settings_set(body: dict):
    old_mic = db.get_settings()["mic_device"]
    db.set_settings(body)
    s = db.get_settings()
    if s["mic_device"] != old_mic and not engine.running:
        engine.stop_monitor()
        engine.start_monitor()
    return s


@app.get("/api/audio/{lid}")
def audio(lid: int):
    lec = db.one("SELECT audio_path FROM lectures WHERE id=?", (lid,))
    if lec and lec["audio_path"] and Path(lec["audio_path"]).exists():
        return FileResponse(lec["audio_path"], media_type="audio/flac")
    return Response(status_code=404)


app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", help="аудиофайл вместо микрофона (тест)")
    ap.add_argument("--speed", type=float, default=3.0)
    ap.add_argument("--port", type=int, default=8899)
    args = ap.parse_args()
    DEMO_FILE = args.demo
    DEMO_SPEED = args.speed
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
