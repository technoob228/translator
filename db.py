"""SQLite-хранилище приложения. Одна лекция = запись в lectures + фразы/заметки/слова."""

import json
import sqlite3
import threading
import time
from pathlib import Path

DATA_DIR = Path(__file__).parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = DATA_DIR / "studio.db"

_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, schedule TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS lectures(
  id INTEGER PRIMARY KEY, course_id INTEGER, title TEXT DEFAULT '',
  started_at REAL, ended_at REAL, audio_path TEXT DEFAULT '',
  summary_md TEXT DEFAULT '', notes_md TEXT DEFAULT '',
  share_slug TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS phrases(
  id INTEGER PRIMARY KEY, lecture_id INTEGER, t0 REAL, t1 REAL,
  es TEXT, ru TEXT, mark TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY, lecture_id INTEGER, t REAL, text TEXT);
CREATE TABLE IF NOT EXISTS words(
  id INTEGER PRIMARY KEY, word TEXT, translation TEXT, context TEXT DEFAULT '',
  lecture_id INTEGER, t REAL, added_at REAL, reps INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS chats(
  id INTEGER PRIMARY KEY, lecture_id INTEGER, role TEXT, content TEXT,
  created_at REAL);
CREATE TABLE IF NOT EXISTS translations(
  id INTEGER PRIMARY KEY, lecture_id INTEGER, src TEXT, dst TEXT,
  source_lang TEXT, target_lang TEXT, created_at REAL);
"""

DEFAULT_SETTINGS = {
    "mic_device": "",            # '' = системный по умолчанию
    "accuracy": "max",           # fast=small | balanced=small+medium | max=small+turbo
    "source_lang": "auto",       # язык лекции: auto | es | en | pt | fr | it | de
    "sensitivity": "normal",     # чувствительность VAD: low | normal | high
    "target_lang": "ru",
    "local_llm_url": "http://localhost:11434/v1",
    "local_llm_model": "qwen3:4b-instruct",   # без thinking — отвечает сразу
    "cloud_llm_url": "https://api.getuno.xyz/v1",  # Uno LLM Gateway
    "cloud_llm_key": "",         # пусто = используется uno_api_key
    "cloud_llm_model": "",       # выбранная в чате; пусто = первая из списка
    # точные имена моделей Uno Gateway (сверены по /v1/models 11.08.2026)
    "cloud_models": '["openai/gpt-5.6-luna", "anthropic/claude-opus-5", '
                    '"google/gemini-3.1-pro-preview", '
                    '"google/gemini-3-flash-preview", "google/gemma-4-31b-it"]',
    "agent_mode": "off",         # инструменты агента только при облаке
    "uno_api_key": "",
}


def conn():
    if not hasattr(_local, "c"):
        _local.c = sqlite3.connect(DB_PATH)
        _local.c.row_factory = sqlite3.Row
        _local.c.executescript(SCHEMA)
    return _local.c


def init():
    c = conn()
    for col in ("notes_md", "share_slug"):   # миграции старых БД
        try:
            c.execute(f"ALTER TABLE lectures ADD COLUMN {col} TEXT DEFAULT ''")
        except sqlite3.OperationalError:
            pass
    if not c.execute("SELECT 1 FROM courses LIMIT 1").fetchone():
        c.execute("INSERT INTO courses(name, schedule) VALUES(?, ?)",
                  ("Мой первый предмет", ""))
    c.commit()


def rows(sql, args=()):
    return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, args=()):
    r = conn().execute(sql, args).fetchone()
    return dict(r) if r else None


def run(sql, args=()):
    cur = conn().execute(sql, args)
    conn().commit()
    return cur.lastrowid


# ---- settings ----

def get_settings():
    s = dict(DEFAULT_SETTINGS)
    for r in rows("SELECT k, v FROM settings"):
        s[r["k"]] = r["v"]
    return s


def set_settings(d):
    for k, v in d.items():
        if k in DEFAULT_SETTINGS:
            run("INSERT INTO settings(k, v) VALUES(?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, str(v)))


# ---- лекции ----

def start_lecture(course_id, title=""):
    return run("INSERT INTO lectures(course_id, title, started_at) VALUES(?,?,?)",
               (course_id, title, time.time()))


def stop_lecture(lecture_id, audio_path=""):
    run("UPDATE lectures SET ended_at = ?, audio_path = ? WHERE id = ?",
        (time.time(), audio_path, lecture_id))


def lecture_stats(lecture_id):
    lec = one("SELECT * FROM lectures WHERE id = ?", (lecture_id,))
    if not lec:
        return None
    ph = one("SELECT COUNT(*) n FROM phrases WHERE lecture_id=? AND mark != 'catchup'", (lecture_id,))
    if (lec.get("notes_md") or "").strip():
        nt = {"n": len([l for l in lec["notes_md"].split("\n") if l.strip()])}
    else:
        nt = one("SELECT COUNT(*) n FROM notes WHERE lecture_id=?", (lecture_id,))
    wd = one("SELECT COUNT(*) n FROM words WHERE lecture_id=?", (lecture_id,))
    st = one("SELECT COUNT(*) n FROM phrases WHERE lecture_id=? AND mark='star'", (lecture_id,))
    q = one("SELECT COUNT(*) n FROM phrases WHERE lecture_id=? AND mark='q'", (lecture_id,))
    dur = (lec["ended_at"] or time.time()) - lec["started_at"]
    return {"lecture": lec, "phrases": ph["n"], "notes": nt["n"], "words": wd["n"],
            "stars": st["n"], "questions": q["n"], "duration": dur}


def transcript_text(lecture_id, with_time=True):
    out = []
    for p in rows("SELECT * FROM phrases WHERE lecture_id=? AND mark != 'catchup' ORDER BY t0",
                  (lecture_id,)):
        ts = time.strftime("%H:%M:%S", time.localtime(
            (one("SELECT started_at s FROM lectures WHERE id=?", (lecture_id,))["s"] or 0) + p["t0"]))
        mark = {"star": " [ВАЖНОЕ]", "q": " [НЕ ПОНЯЛА]"}.get(p["mark"], "")
        prefix = f"[{ts}]{mark} " if with_time else ""
        out.append(f"{prefix}{p['ru'] or p['es']}")
    return "\n".join(out)
