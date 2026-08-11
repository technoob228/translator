"""LLM-слой: один OpenAI-совместимый клиент на два режима.

  local  — Ollama (qwen3:4b), БЕЗ инструментов: чат, конспекты, catch-up.
  cloud  — любой OpenAI-совместимый провайдер из настроек; в режиме агента
           получает инструменты: файлы в workspace, web_search, fetch_url,
           terminal (каждая команда ждёт «Разрешить» в UI), share (Uno).
"""

import atexit
import html
import json
import re
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path

import requests

import db

WORKSPACE = db.DATA_DIR / "workspace"
WORKSPACE.mkdir(exist_ok=True)

TIMEOUT = 180


# ------------------------------------------------------------- конфигурация

def cloud_model_list(s=None):
    try:
        return [m for m in json.loads((s or db.get_settings())["cloud_models"])
                if isinstance(m, str) and m.strip()]
    except Exception:
        return []


def config(mode, model=None):
    s = db.get_settings()
    if mode == "cloud":
        key = s["cloud_llm_key"] or s["uno_api_key"]   # один ключ Uno на всё
        mdl = model or s["cloud_llm_model"] or \
            (cloud_model_list(s)[0] if cloud_model_list(s) else "")
        if s["cloud_llm_url"] and key and mdl:
            return {"base": s["cloud_llm_url"].rstrip("/"), "key": key,
                    "model": mdl, "mode": "cloud"}
    return {"base": s["local_llm_url"].rstrip("/"), "key": "ollama",
            "model": s["local_llm_model"], "mode": "local"}


def gateway_models():
    """Все модели облачного гейтвея — для подбора точных имён в настройках."""
    s = db.get_settings()
    key = s["cloud_llm_key"] or s["uno_api_key"]
    if not (s["cloud_llm_url"] and key):
        return {"error": "добавь Ключ Uno (или ключ облака) в Настройках"}
    r = requests.get(s["cloud_llm_url"].rstrip("/") + "/models",
                     headers={"Authorization": f"Bearer {key}"}, timeout=15)
    r.raise_for_status()
    data = r.json()
    items = data.get("data", data if isinstance(data, list) else [])
    return {"models": [m.get("id") for m in items if m.get("id")]}


def local_available():
    try:
        s = db.get_settings()
        r = requests.get(s["local_llm_url"].rstrip("/") + "/models", timeout=2)
        return r.ok
    except Exception:
        return False


# ------------------------------------------------------------- управление Ollama

_ollama_proc = None     # `ollama serve`, запущенный кнопкой из приложения


def _ollama_bin():
    for p in (shutil.which("ollama"), "/opt/homebrew/bin/ollama",
              "/usr/local/bin/ollama"):
        if p and Path(p).exists():
            return p
    return None


def ollama_start():
    """Поднимает `ollama serve` и ждёт готовности (до ~15 с)."""
    global _ollama_proc
    if local_available():
        return {"running": True}
    binpath = _ollama_bin()
    if not binpath:
        return {"running": False, "error":
                "Ollama не установлена. В Терминале: brew install ollama, "
                "затем ollama pull qwen3:4b"}
    log = open(db.DATA_DIR / "ollama.log", "ab")
    _ollama_proc = subprocess.Popen([binpath, "serve"], stdout=log, stderr=log,
                                    start_new_session=True)
    for _ in range(30):
        time.sleep(0.5)
        if local_available():
            return {"running": True}
        if _ollama_proc.poll() is not None:
            break
    return {"running": local_available(),
            "error": "Ollama не запустилась — детали в data/ollama.log"}


def ollama_stop():
    """Гасит Ollama: наш процесс — сигналом, посторонний — pkill."""
    global _ollama_proc
    if _ollama_proc and _ollama_proc.poll() is None:
        _ollama_proc.terminate()
        try:
            _ollama_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            _ollama_proc.kill()
    _ollama_proc = None
    if local_available():   # запущена не нами (вручную или Ollama.app)
        subprocess.run(["pkill", "-x", "ollama"], capture_output=True)
        for _ in range(10):
            time.sleep(0.3)
            if not local_available():
                break
    return {"running": local_available()}


@atexit.register
def _ollama_cleanup():
    """Выход из приложения гасит Ollama, если её запустили мы."""
    if _ollama_proc and _ollama_proc.poll() is None:
        _ollama_proc.terminate()


def _strip_think(text):
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


def _post(cfg, payload):
    r = requests.post(cfg["base"] + "/chat/completions",
                      headers={"Authorization": f"Bearer {cfg['key']}"},
                      json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]


def _mute_thinking(messages, cfg):
    """qwen3 без 'instruct' по умолчанию долго «думает» (<think>-токены) —
    для чата/словаря это минута ожидания; глушим мягким переключателем."""
    m = cfg["model"]
    if cfg["mode"] != "local" or not m.startswith("qwen3") or "instruct" in m:
        return messages
    messages = [dict(x) for x in messages]
    if messages and messages[0]["role"] == "system":
        messages[0]["content"] = "/no_think " + messages[0]["content"]
    else:
        messages.insert(0, {"role": "system", "content": "/no_think"})
    return messages


def complete(messages, mode="local", tools=None, model=None):
    cfg = config(mode, model)
    payload = {"model": cfg["model"], "messages": _mute_thinking(messages, cfg)}
    if tools and cfg["mode"] == "cloud":
        payload["tools"] = tools
    msg = _post(cfg, payload)
    msg["content"] = _strip_think(msg.get("content"))
    return msg, cfg


# ------------------------------------------------------------- контекст

def lecture_context(lecture_id, max_phrases=60):
    if not lecture_id:
        return ""
    ph = db.rows("SELECT * FROM phrases WHERE lecture_id=? AND mark!='catchup' "
                 "ORDER BY t0 DESC LIMIT ?", (lecture_id, max_phrases))
    lines = [f"[{int(p['t0']//60):02d}:{int(p['t0']%60):02d}] {p['es']} — {p['ru']}"
             for p in reversed(ph)]
    return "\n".join(lines)


SYSTEM_TUTOR = (
    "Ты — помощница студентки Кати на лекции в аргентинском университете. "
    "Лекции идут на испанском, Катя — русскоязычная. Отвечай по-русски, "
    "коротко и простыми словами. Тебе дан транскрипт последней части лекции "
    "(испанский — русский перевод); опирайся на него и ссылайся на времена [мм:сс]."
)


def chat(user_messages, mode="local", lecture_id=None, model=None):
    """Обычный чат (без инструментов). user_messages: [{role, content}...]"""
    ctx = lecture_context(lecture_id)
    sys = SYSTEM_TUTOR + ("\n\nТранскрипт:\n" + ctx if ctx else "")
    msg, cfg = complete([{"role": "system", "content": sys}] + user_messages,
                        mode=mode, model=model)
    return msg["content"], cfg["mode"]


# ------------------------------------------------------------- генерация

def _chunk_lines(text, size=11000):
    """Режет текст на куски ~size символов по границам строк."""
    chunks, cur = [], ""
    for ln in text.split("\n"):
        if cur and len(cur) + len(ln) > size:
            chunks.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur.strip():
        chunks.append(cur)
    return chunks


def _summary_prompt(transcript, notes, words):
    return f"""Ты помогаешь студентке готовиться по записи лекции. Ниже транскрипт
(автораспознавание речи: в нём бывают ослышки, обрывки и повторы — игнорируй
мусор и восстанавливай смысл по контексту; не пересказывай рекламу, приветствия
и болтовню не по теме).

Составь конспект по-русски в Markdown СТРОГО такой структуры, без вступлений,
пояснений от себя и текста вне этих разделов:

## Коротко
(5-10 пунктов главной сути; фразы с пометкой [ВАЖНОЕ] включи обязательно;
в конце пункта можно указать метку времени из транскрипта, чтобы найти место
в записи)

## Термины
(каждый с новой строки: **испанский термин** — перевод — определение в одну
строку{'; обязательно включи слова: ' + words if words else ''})

## Разобрать
(объясни простыми словами места с пометкой [НЕ ПОНЯЛА] — по одному абзацу;
если таких пометок нет, пропусти весь раздел вместе с заголовком)

## Возможные вопросы к экзамену
(3-5 вопросов строго по материалу лекции)

Правила: только факты из транскрипта, ничего не выдумывай; пиши коротко и
конкретно, без воды.

Транскрипт:
{transcript}

Заметки студентки (учитывай при выборе важного):
{notes or '(нет)'}"""


def summarize_lecture(lecture_id, mode="local", model=None):
    st = db.lecture_stats(lecture_id)
    transcript = db.transcript_text(lecture_id)
    notes = (st["lecture"].get("notes_md") or "").strip() or \
        "\n".join(f"- {n['text']}" for n in db.rows(
            "SELECT * FROM notes WHERE lecture_id=? ORDER BY t", (lecture_id,)))
    words = ", ".join(f"{w['word']} ({w['translation']})" for w in db.rows(
        "SELECT * FROM words WHERE lecture_id=?", (lecture_id,)))
    notify = broker.notify or (lambda e: None)

    # длинная лекция — сначала выжимки по частям, потом конспект по выжимкам
    # (иначе локальная модель захлёбывается контекстом и работает минуты)
    if len(transcript) > 14000:
        chunks = _chunk_lines(transcript)
        digests = []
        for i, ch in enumerate(chunks, 1):
            notify({"type": "summary_progress",
                    "text": f"читаю запись: часть {i} из {len(chunks)}…"})
            msg, _ = complete([{"role": "user", "content":
                "Фрагмент транскрипта лекции (автораспознавание, возможны "
                "ошибки). Выпиши по-русски 5-10 пунктов сути с метками "
                "времени и термины (испанский — перевод). Пометки [ВАЖНОЕ] "
                "и [НЕ ПОНЯЛА] сохраняй. Только пункты:\n\n" + ch}],
                mode=mode, model=model)
            digests.append(msg["content"])
        transcript = "\n\n".join(digests)
        notify({"type": "summary_progress", "text": "собираю конспект…"})

    prompt = _summary_prompt(transcript[:24000], notes, words)
    msg, _ = complete([{"role": "user", "content": prompt}], mode=mode,
                      model=model)
    md = msg["content"]
    title = st["lecture"]["title"] or "Лекция"
    md = f"# {title}\n\n{md}"
    if notes:
        md += f"\n\n## Мои заметки\n{notes}"
    db.run("UPDATE lectures SET summary_md=? WHERE id=?", (md, lecture_id))
    return md


def catchup(lecture_id, minutes=10, mode="local"):
    ph = db.rows("SELECT * FROM phrases WHERE lecture_id=? AND mark!='catchup' "
                 "ORDER BY t0 DESC LIMIT 40", (lecture_id,))
    if len(ph) < 5:
        return None
    text = "\n".join(p["ru"] or p["es"] for p in reversed(ph))
    msg, _ = complete([{"role": "user", "content":
        "Перескажи в 2-3 предложениях по-русски, о чём были эти фразы лекции "
        "(для студентки, которая отвлеклась):\n" + text[:6000]}], mode=mode)
    return msg["content"]


def word_info(word, context=""):
    """Быстрый перевод для поповера/словаря — argos/Google, БЕЗ LLM.
    (Раньше здесь же ждали LLM — поповер висел; теперь это word_enrich.)"""
    from engine import translate
    s = db.get_settings()
    src = s["source_lang"] if s["source_lang"] != "auto" else "es"
    tr = translate(word, src, s["target_lang"], prefer="argos") or "?"
    return {"word": word, "translation": tr, "base": "", "pos": ""}


def word_enrich(word, context=""):
    """Базовая форма и часть речи от локальной LLM — отдельным запросом."""
    info = {"base": "", "pos": ""}
    if not local_available():
        return info
    try:
        msg, _ = complete([{"role": "user", "content":
            f'Слово «{word}» из лекции (контекст: «{context}»). Ответь строго JSON: '
            '{"base": "начальная форма", "pos": "часть речи по-русски"}'}])
        m = re.search(r"\{.*\}", msg["content"], re.S)
        if m:
            j = json.loads(m.group(0))
            info["base"] = str(j.get("base", ""))[:40]
            info["pos"] = str(j.get("pos", ""))[:40]
    except Exception:
        pass
    return info


# ------------------------------------------------------------- агент (облако)

class ApprovalBroker:
    """Терминальные команды агента ждут «Разрешить» из UI."""

    def __init__(self):
        self.pending = {}   # id -> {event, decision, command}
        self.notify = None  # callback(dict) -> WS

    def ask(self, command):
        aid = uuid.uuid4().hex[:10]
        ev = threading.Event()
        self.pending[aid] = {"event": ev, "decision": None, "command": command}
        if self.notify:
            self.notify({"type": "approval", "id": aid, "command": command})
        ev.wait(timeout=300)
        p = self.pending.pop(aid, None)
        return bool(p and p["decision"])

    def answer(self, aid, approve):
        p = self.pending.get(aid)
        if p:
            p["decision"] = approve
            p["event"].set()


broker = ApprovalBroker()


def _safe_path(rel):
    p = (WORKSPACE / rel).resolve()
    if not str(p).startswith(str(WORKSPACE.resolve())):
        raise ValueError("путь вне рабочей папки запрещён")
    return p


def t_list_files(path=""):
    p = _safe_path(path or ".")
    return "\n".join(sorted(
        f"{x.name}/" if x.is_dir() else f"{x.name} ({x.stat().st_size}b)"
        for x in p.iterdir())) or "(пусто)"


def t_read_file(path):
    return _safe_path(path).read_text()[:40000]


def t_write_file(path, content):
    p = _safe_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"записано {len(content)} символов в {path}"


def t_web_search(query):
    r = requests.post("https://html.duckduckgo.com/html/",
                      data={"q": query}, timeout=15,
                      headers={"User-Agent": "Mozilla/5.0"})
    out = []
    for m in re.finditer(
            r'result__a[^>]*href="([^"]+)"[^>]*>(.*?)</a>.*?result__snippet[^>]*>(.*?)</',
            r.text, re.S):
        url, title, snip = m.groups()
        clean = lambda s: html.unescape(re.sub(r"<[^>]+>", "", s)).strip()
        out.append(f"- {clean(title)}\n  {url}\n  {clean(snip)}")
        if len(out) >= 6:
            break
    return "\n".join(out) or "ничего не найдено"


def t_fetch_url(url):
    if not url.startswith(("http://", "https://")):
        return "только http(s)"
    r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"},
                     stream=True)
    raw = r.raw.read(300_000, decode_content=True)
    text = raw.decode(r.encoding or "utf-8", errors="replace")
    if "<html" in text[:2000].lower():
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", text, flags=re.S)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
        text = re.sub(r"\s+", " ", text)
    return text[:20000]


def t_terminal(command):
    if not broker.ask(command):
        return "ПОЛЬЗОВАТЕЛЬ ОТКЛОНИЛ команду. Не повторяй её."
    try:
        p = subprocess.run(command, shell=True, cwd=WORKSPACE,
                           capture_output=True, text=True, timeout=120)
        out = (p.stdout + p.stderr)[:20000]
        return out or f"(exit {p.returncode}, вывода нет)"
    except subprocess.TimeoutExpired:
        return "команда не уложилась в 120с и была прервана"


def t_share_file(path):
    key = db.get_settings()["uno_api_key"]
    if not key:
        return "не задан ключ Uno в настройках — попроси пользователя добавить"
    p = _safe_path(path)
    fname = p.name if p.name.endswith(".html") else "index.html"
    with open(p, "rb") as f:
        r = requests.post("https://api.getuno.xyz/api/v1/deploy",
                          headers={"Authorization": f"Bearer {key}"},
                          files={"files": (fname, f)}, timeout=60)
    r.raise_for_status()
    j = r.json()
    return json.dumps(j, ensure_ascii=False)


TOOLS_IMPL = {
    "list_files": t_list_files, "read_file": t_read_file,
    "write_file": t_write_file, "web_search": t_web_search,
    "fetch_url": t_fetch_url, "terminal": t_terminal, "share_file": t_share_file,
}

TOOLS_SPEC = [
    {"type": "function", "function": {"name": "list_files",
     "description": "Список файлов рабочей папки (конспекты, презентации)",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string", "description": "подпапка, по умолчанию корень"}}}}},
    {"type": "function", "function": {"name": "read_file",
     "description": "Прочитать файл из рабочей папки",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "write_file",
     "description": "Создать/перезаписать файл в рабочей папке (md-конспекты, html-презентации)",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}},
         "required": ["path", "content"]}}},
    {"type": "function", "function": {"name": "web_search",
     "description": "Поиск в интернете", "parameters": {"type": "object",
     "properties": {"query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "fetch_url",
     "description": "Скачать страницу/файл по URL (текст)",
     "parameters": {"type": "object", "properties": {
         "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "terminal",
     "description": "Выполнить shell-команду в рабочей папке. Пользователь "
                    "видит команду и должен разрешить её. curl доступен.",
     "parameters": {"type": "object", "properties": {
         "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "share_file",
     "description": "Опубликовать файл из рабочей папки в интернете (статичный "
                    "хостинг), вернёт публичную ссылку для шеринга",
     "parameters": {"type": "object", "properties": {
         "path": {"type": "string"}}, "required": ["path"]}}},
]

AGENT_EXTRA = (
    "\n\nУ тебя есть инструменты: файлы в рабочей папке (конспекты, "
    "презентации reveal.js в одном html), веб-поиск, загрузка страниц, "
    "терминал (команду должен одобрить пользователь) и публикация файла "
    "по ссылке. Создавая презентации, делай один самодостаточный html. "
    "Работай только в рабочей папке.")


def agent_chat(user_messages, lecture_id=None, emit=None, model=None):
    """Облачный чат с инструментами. emit — прогресс в UI."""
    ctx = lecture_context(lecture_id)
    sys = SYSTEM_TUTOR + AGENT_EXTRA + ("\n\nТранскрипт:\n" + ctx if ctx else "")
    messages = [{"role": "system", "content": sys}] + user_messages
    cfg = config("cloud", model)
    if cfg["mode"] != "cloud":
        return "Облачная модель не настроена (Настройки → Облачная LLM).", []
    steps = []
    for _ in range(12):
        payload = {"model": cfg["model"], "messages": messages,
                   "tools": TOOLS_SPEC}
        msg = _post(cfg, payload)
        calls = msg.get("tool_calls")
        if not calls:
            return _strip_think(msg.get("content")), steps
        messages.append(msg)
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"]["arguments"] or "{}")
            except json.JSONDecodeError:
                args = {}
            if emit:
                emit({"type": "agent_step", "tool": name, "args": args})
            steps.append({"tool": name, "args": args})
            try:
                result = TOOLS_IMPL[name](**args)
            except Exception as e:
                result = f"ошибка инструмента: {e}"
            messages.append({"role": "tool", "tool_call_id": c["id"],
                             "content": str(result)[:30000]})
    return "Слишком длинная цепочка инструментов, остановилась.", steps
