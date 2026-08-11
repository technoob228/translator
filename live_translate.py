#!/usr/bin/env python3
"""Живой перевод лекции: микрофон -> Whisper (испанский) -> русские субтитры.

Запуск:  python live_translate.py
Субтитры открываются в браузере на http://localhost:8765
"""

import argparse
import json
import queue
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import numpy as np

SAMPLE_RATE = 16000
BLOCK_SEC = 0.05          # размер блока анализа громкости
SILENCE_SEC = 0.7         # пауза, после которой фраза считается законченной
MIN_SPEECH_SEC = 0.4      # короче этого — считаем шумом, выбрасываем
MAX_CHUNK_SEC = 10.0      # принудительно отправляем в распознавание
CALIBRATION_SEC = 1.5     # первые секунды — замер уровня фонового шума

lines = []                # [{id, es, ru, t}]
lines_lock = threading.Lock()
lines_event = threading.Condition()


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------- сегментация

class Segmenter:
    """Копит звук и режет его на фразы по паузам (простой энергетический VAD)."""

    def __init__(self, out_queue):
        self.out = out_queue
        self.buf = []
        self.voiced = False
        self.silence_blocks = 0
        self.noise_rms = None
        self.calib = []

    def feed(self, block):
        rms = float(np.sqrt(np.mean(block ** 2)) + 1e-9)

        if self.noise_rms is None:
            self.calib.append(rms)
            if len(self.calib) * BLOCK_SEC >= CALIBRATION_SEC:
                self.noise_rms = float(np.median(self.calib))
                log(f"[vad] уровень фона откалиброван: {self.noise_rms:.5f}")
            return

        threshold = max(self.noise_rms * 3.0, 0.004)
        is_speech = rms > threshold

        if is_speech:
            self.buf.append(block)
            self.voiced = True
            self.silence_blocks = 0
        elif self.voiced:
            self.buf.append(block)
            self.silence_blocks += 1
            if self.silence_blocks * BLOCK_SEC >= SILENCE_SEC:
                self.flush()

        if self.voiced and len(self.buf) * BLOCK_SEC >= MAX_CHUNK_SEC:
            self.flush()

    def flush(self):
        if self.buf:
            audio = np.concatenate(self.buf)
            if len(audio) / SAMPLE_RATE >= MIN_SPEECH_SEC:
                self.out.put(audio)
        self.buf = []
        self.voiced = False
        self.silence_blocks = 0


# ------------------------------------------------------- распознавание+перевод

def make_translator(source, target):
    from deep_translator import GoogleTranslator
    tr = GoogleTranslator(source=source, target=target)

    def translate(text):
        try:
            return tr.translate(text)
        except Exception as e:
            log(f"[перевод] ошибка сети: {e}")
            return None

    return translate


def asr_worker(model_name, source, target, audio_queue):
    from faster_whisper import WhisperModel

    log(f"[asr] загружаю модель {model_name} (первый раз скачивается, подожди)...")
    model = WhisperModel(model_name, device="auto", compute_type="int8")
    log("[asr] модель готова, говорите")
    translate = make_translator(source, target)

    while True:
        audio = audio_queue.get()
        if audio is None:
            break
        t0 = time.time()
        segments, _ = model.transcribe(
            audio,
            language=source,
            beam_size=1,
            condition_on_previous_text=False,
            vad_filter=True,
        )
        text = " ".join(s.text.strip() for s in segments).strip()
        if not text:
            continue
        ru = translate(text)
        line = {
            "id": len(lines),
            "es": text,
            "ru": ru or "⚠ нет сети — только оригинал",
            "t": time.strftime("%H:%M:%S"),
        }
        with lines_lock:
            lines.append(line)
        with lines_event:
            lines_event.notify_all()
        log(f"  ES: {text}\n  RU: {line['ru']}   ({time.time() - t0:.1f}s)\n")


# ------------------------------------------------------------------- веб-морда

PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Живой перевод лекции</title>
<style>
  body { background:#111; color:#eee; font-family:-apple-system,sans-serif;
         max-width:900px; margin:0 auto; padding:16px 20px 40vh; }
  h1 { font-size:15px; color:#888; font-weight:normal; }
  .line { margin:14px 0; }
  .ru { font-size:26px; line-height:1.35; }
  .es { font-size:15px; color:#7a8; margin-top:2px; }
  .t  { font-size:11px; color:#555; }
</style></head><body>
<h1>🎙 Живой перевод — оставь эту вкладку открытой, текст добавляется сам</h1>
<div id="out"></div>
<script>
const out = document.getElementById('out');
const es = new EventSource('/events');
es.onmessage = (e) => {
  const d = JSON.parse(e.data);
  const el = document.createElement('div');
  el.className = 'line';
  el.innerHTML = `<div class="t">${d.t}</div>`
    + `<div class="ru">${d.ru}</div><div class="es">${d.es}</div>`;
  out.appendChild(el);
  window.scrollTo(0, document.body.scrollHeight);
};
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.path == "/":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            sent = 0
            try:
                while True:
                    with lines_lock:
                        new = lines[sent:]
                    for line in new:
                        payload = json.dumps(line, ensure_ascii=False)
                        self.wfile.write(f"data: {payload}\n\n".encode())
                        self.wfile.flush()
                        sent += 1
                    with lines_event:
                        lines_event.wait(timeout=5)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_error(404)


# ------------------------------------------------------------------------ main

def run_mic(segmenter, device):
    import sounddevice as sd

    block = int(SAMPLE_RATE * BLOCK_SEC)
    raw_q = queue.Queue()

    def callback(indata, frames, t, status):
        raw_q.put(indata[:, 0].copy())

    with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                        blocksize=block, device=device, callback=callback):
        log("[mic] слушаю микрофон (Ctrl+C — выход)")
        while True:
            segmenter.feed(raw_q.get())


def run_file(segmenter, path):
    """Прогоняет wav/mp3/m4a через тот же конвейер — для проверки без микрофона."""
    from faster_whisper.audio import decode_audio

    audio = decode_audio(path, sampling_rate=SAMPLE_RATE)
    log(f"[file] {path}: {len(audio) / SAMPLE_RATE:.1f}s")
    segmenter.noise_rms = 0.0005  # файл — калибровка не нужна
    block = int(SAMPLE_RATE * BLOCK_SEC)
    for i in range(0, len(audio), block):
        segmenter.feed(audio[i:i + block])
    segmenter.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", default="small",
                    help="whisper-модель: tiny/base/small/medium/large-v3-turbo (default: small)")
    ap.add_argument("--source", default="es", help="язык лектора (default: es)")
    ap.add_argument("--target", default="ru", help="язык перевода (default: ru)")
    ap.add_argument("--device", type=int, default=None, help="номер микрофона")
    ap.add_argument("--list-devices", action="store_true", help="показать микрофоны")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--file", help="прогнать аудиофайл вместо микрофона (тест)")
    ap.add_argument("--keep-open", action="store_true",
                    help="в режиме --file не выходить, оставить страницу с субтитрами")
    args = ap.parse_args()

    if args.list_devices:
        import sounddevice as sd
        print(sd.query_devices())
        return

    audio_q = queue.Queue()
    threading.Thread(target=asr_worker,
                     args=(args.model, args.source, args.target, audio_q),
                     daemon=True).start()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://localhost:{args.port}"
    log(f"[web] субтитры: {url}")
    if not args.no_browser:
        webbrowser.open(url)

    segmenter = Segmenter(audio_q)
    try:
        if args.file:
            run_file(segmenter, args.file)
            while not audio_q.empty():
                time.sleep(0.5)
            time.sleep(5)  # дождаться последнего распознавания
            if args.keep_open:
                log("[web] страница остаётся открытой, Ctrl+C — выход")
                while True:
                    time.sleep(60)
        else:
            run_mic(segmenter, args.device)
    except KeyboardInterrupt:
        log("\nпока!")


if __name__ == "__main__":
    main()
