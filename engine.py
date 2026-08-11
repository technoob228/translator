"""Аудио-конвейер v2.

Нейронный VAD (Silero, идёт с faster-whisper) вместо энергетического порога:
не зависит от громкости и фонового шума, калибровка не нужна.

Каждые 0.5 с по несведённому куску аудио считаются речевые сегменты:
  - сегмент, за которым ≥0.6 с не-речи, считается завершённым →
    уходит в финальное распознавание (large-v3-turbo) → карточка фразы;
    это происходит СРАЗУ, даже если лектор уже говорит следующую фразу;
  - незавершённый хвост каждые полсекунды гонится через small →
    черновые слова в live-зоне (перевод черновика — с дебаунсом 1.2 с);
  - если пауз нет больше 10 с — принудительная нарезка по границе сегментов;
    длинный финал (>8 с) режется на фразы по концам предложений
    (таймкоды сегментов whisper) — карточки не разрастаются в простыни.

Пауза: pause() закрывает микрофон и дописывает завершённую речь, resume()
продолжает ту же лекцию; flac пишется инкрементально с flush ~каждые 2 с.

События emit(dict): partial{es,ru} / queued{qid,t0,t1,es,ru} (сегмент
нарезан и ждёт финализации — UI сразу ставит серую карточку с текстом
черновика: распознанное не исчезает с экрана) / final{qid,phrase,extra}
(extra — хвост фраз, если длинный кусок разрезан на предложения) /
final_drop{qid} (финал пустой — убрать заглушку) / level{rms} /
status{text} / paused{on}.
"""

import queue
import re
import threading
import time

import numpy as np

import db

SR = 16000
BLOCK_SEC = 0.05
TICK = 0.5
SIL_END = 0.6          # трейлинг тишины => сегмент завершён
MAX_PENDING = 10.0     # принудительная нарезка длинной речи
DRAFT_WINDOW = 6.0
MIN_PHRASE = 0.6
PAD = 0.25             # запас аудио по краям сегмента для ASR, сек
SPLIT_OVER = 8.0       # финал длиннее — режем на фразы по предложениям

FINAL_MODEL = {"fast": None, "balanced": "medium", "max": "large-v3-turbo"}
MLX_REPO = "mlx-community/whisper-large-v3-turbo"
SENS_MAP = {"high": 0.22, "normal": 0.35, "low": 0.5}   # порог silero


# ------------------------------------------------------------------ перевод

_gt_cache = {}


def _looks_untranslated(src_text, out, source, target):
    """Google под лимитом возвращает исходник (иногда с иным регистром)."""
    if not out or source == target:
        return False
    return out.strip().casefold() == src_text.strip().casefold()


def translate(text, source="es", target="ru", prefer="google"):
    """prefer='google': Google → argos.  prefer='argos': argos → Google.
    Результат, совпадающий с исходником, считается фейлом Google."""
    if not text.strip():
        return ""
    key = (text, source, target)
    if key in _gt_cache:
        return _gt_cache[key]

    def _google():
        from deep_translator import GoogleTranslator
        out = GoogleTranslator(source=source, target=target).translate(text)
        return None if _looks_untranslated(text, out, source, target) else out

    def _local():
        return _argos(text, source, target)

    order = (_local, _google) if prefer == "argos" else (_google, _local)
    for fn in order:
        try:
            out = fn()
        except Exception:
            out = None
        if out:
            _gt_cache[key] = out
            return out
    return None


_argos_langs = None


def _argos(text, source, target):
    global _argos_langs
    from argostranslate import translate as at
    if _argos_langs is None:
        _argos_langs = {l.code: l for l in at.get_installed_languages()}
    src, en, tgt = (_argos_langs.get(c) for c in (source, "en", target))
    if src and tgt:
        direct = src.get_translation(tgt)
        if direct:
            return direct.translate(text)
        if en:
            t1, t2 = src.get_translation(en), en.get_translation(tgt)
            if t1 and t2:
                return t2.translate(t1.translate(text))
    return None


def clean_asr(text):
    """Против галлюцинаций Whisper на шуме/тишине («Kh Kh Kh…», «X X X…»):
    хвостовая серия одного токена отрезается целиком, внутренние серии
    ужимаются, фразы почти без уникальных слов выбрасываются."""
    key = lambda w: w.casefold().strip(".,!?¡¿:;—-")
    words = text.split()
    while len(words) >= 3 and key(words[-1]) == key(words[-2]) == key(words[-3]):
        k = key(words[-1])
        while words and key(words[-1]) == k:
            words.pop()
    out, prev, run = [], None, 0
    for w in words:
        if key(w) == prev:
            run += 1
            if run >= 2:
                continue
        else:
            prev, run = key(w), 0
        out.append(w)
    cleaned = " ".join(out)
    ws = cleaned.split()
    uniq = {key(w) for w in ws}
    if len(ws) >= 6 and len(uniq) / len(ws) < 0.35:
        return ""
    if len(ws) >= 2 and len(uniq) == 1:
        return ""
    if not re.search(r"[^\W\d_]{2,}", cleaned):
        return ""
    return cleaned


# ------------------------------------------------------------------ движок

class Engine:
    LANG_NAMES = {"es": "испанский", "en": "английский", "pt": "португальский",
                  "fr": "французский", "it": "итальянский", "de": "немецкий",
                  "ru": "русский"}

    def __init__(self, emit):
        self.emit = emit
        self.mlock = threading.Lock()
        self.alock = threading.Lock()
        self.running = False
        self.paused = False
        self.lecture_id = None
        self.models = {}
        self.final_q = queue.Queue()
        self.final_busy = False
        self.detected_lang = None
        self.recorder = None
        self.stream = None
        self.pending = np.zeros(0, dtype=np.float32)
        self.base_t = 0.0        # время начала pending, сек от старта лекции
        self.samples = 0
        self._tr_misses = 0
        self._flushcnt = 0
        self._qid = 0
        self._mlx = None
        self._last_partial = ""
        self._last_tr = (0.0, "", "")   # (time, es, ru)
        self._drafts = []               # [t0_абс, es, ru] показанных черновиков

    # ---------- модели ----------

    def model(self, name):
        with self.mlock:
            if name not in self.models:
                from faster_whisper import WhisperModel
                self.emit({"type": "status", "text": f"загружаю модель {name}…"})
                self.models[name] = WhisperModel(name, device="auto",
                                                 compute_type="int8")
                self.emit({"type": "status", "text": f"модель {name} готова"})
            return self.models[name]

    def _mlx_available(self):
        """MLX (Apple GPU) для финальной turbo-модели: ~8x быстрее CPU."""
        if self._mlx is None:
            try:
                import mlx_whisper  # noqa: F401
                self._mlx = True
            except Exception:
                self._mlx = False
        return self._mlx

    def preload(self):
        def load():
            self.model("small")           # черновик — CPU (CTranslate2)
            fm = FINAL_MODEL.get(db.get_settings()["accuracy"])
            if fm == "large-v3-turbo" and self._mlx_available():
                try:                      # прогрев MLX на GPU
                    import mlx_whisper
                    mlx_whisper.transcribe(np.zeros(SR // 2, dtype=np.float32),
                                           path_or_hf_repo=MLX_REPO)
                    self.emit({"type": "status",
                               "text": "финальная модель готова (GPU)"})
                except Exception:
                    self._mlx = False
                    self.model(fm)
            elif fm:
                self.model(fm)
            try:
                _argos("hola", "es", db.get_settings()["target_lang"])  # прогрев
            except Exception:
                pass
        threading.Thread(target=load, daemon=True).start()

    def _lang_setting(self):
        s = db.get_settings()["source_lang"]
        return None if s == "auto" else s

    def _transcribe(self, name, audio, beam, lang):
        """→ (сегменты [(start, end, text)], язык)."""
        segments, info = self.model(name).transcribe(
            audio, language=lang, beam_size=beam,
            condition_on_previous_text=False)
        segs = [(s.start, s.end, s.text.strip()) for s in segments]
        return segs, info.language

    # ---------- приём аудио ----------

    def _feed(self, block):
        self.samples += len(block)
        if self.recorder is not None:
            self.recorder.write(block)
            self._flushcnt += 1
            if self._flushcnt >= 40:      # ~2 c — чтобы ничего не пропало
                self._flushcnt = 0
                try:
                    self.recorder.flush()
                except Exception:
                    pass
        rms = float(np.sqrt(np.mean(block ** 2)) + 1e-9)
        self.emit({"type": "level", "rms": round(rms, 5)})
        with self.alock:
            self.pending = np.concatenate([self.pending, block])

    def _advance(self, n_samples):
        with self.alock:
            n = min(n_samples, len(self.pending))
            self.pending = self.pending[n:]
            self.base_t += n / SR

    # ---------- основной цикл ----------

    def _vad(self, audio):
        from faster_whisper.vad import get_speech_timestamps, VadOptions
        sens = SENS_MAP.get(db.get_settings().get("sensitivity", "normal"), 0.35)
        opts = VadOptions(threshold=sens, min_silence_duration_ms=450,
                          min_speech_duration_ms=250, speech_pad_ms=150)
        return get_speech_timestamps(audio, opts)

    def _tick_loop(self):
        while self.running:
            time.sleep(TICK)
            if self.paused:
                continue
            try:
                self._tick()
            except Exception as e:
                self.emit({"type": "status", "text": f"ошибка: {e}"})

    def _tick(self):
        with self.alock:
            audio = self.pending.copy()
            base = self.base_t
        if len(audio) < int(0.4 * SR):
            return
        ts = self._vad(audio)
        if not ts:
            if len(audio) > 4 * SR:
                self._advance(len(audio) - SR)
            self._emit_partial("", "")
            return

        trailing = len(audio) - ts[-1]["end"]
        done = ts[:-1] if trailing < int(SIL_END * SR) else ts
        forced = False
        if not done and len(audio) >= int(MAX_PENDING * SR):
            done, forced = ts, True     # пауз нет — режем по границе речи

        if done:
            cut = done[-1]["end"]
            self._enqueue(audio, base, self._merge(done))
            self._advance(cut)
            if done is ts and not forced:
                self._emit_partial("", "")
                return

        # черновик незавершённого хвоста
        with self.alock:
            audio = self.pending.copy()
            base = self.base_t
        ts = self._vad(audio)
        if not ts:
            return
        start = max(ts[-1]["start"], len(audio) - int(DRAFT_WINDOW * SR))
        chunk = audio[start:]
        if len(chunk) < int(0.4 * SR):
            return
        lang = self._lang_setting() or self.detected_lang
        try:
            segs, _ = self._transcribe("small", chunk, beam=1, lang=lang)
        except Exception:
            return
        es = clean_asr(" ".join(t for _, _, t in segs).strip())
        if not es or not self.running or self.paused:
            return
        # перевод черновика с дебаунсом
        now = time.time()
        t_prev, es_prev, ru_prev = self._last_tr
        if es == es_prev:
            ru = ru_prev
        elif now - t_prev >= 1.2:
            src = lang or self.detected_lang or "es"
            ru = translate(es, src, db.get_settings()["target_lang"],
                           prefer="argos") or ""
            self._last_tr = (now, es, ru)
        else:
            ru = ru_prev
        self._emit_partial(es, ru)
        self._remember_draft(base + start / SR, es, ru)

    def _remember_draft(self, t0, es, ru):
        """Помнит, какой черновик показан для куска речи, начавшегося в t0:
        при нарезке этот текст сразу уедет в серую карточку-заглушку."""
        d = self._drafts
        if d and abs(d[-1][0] - t0) < 1.5:
            d[-1] = [t0, es, ru]     # тот же кусок — черновик дозрел
        else:
            d.append([t0, es, ru])
            del d[:-8]

    def _take_draft(self, t0, t1):
        """Черновик, показанный для сегмента [t0,t1] (если был)."""
        for i in range(len(self._drafts) - 1, -1, -1):
            if t0 - 0.8 <= self._drafts[i][0] < t1:
                _, es, ru = self._drafts.pop(i)
                return es, ru
        return "", ""

    def _enqueue(self, audio, base, groups):
        """Ставит нарезанные сегменты в финальную очередь; на каждый сразу
        эмитится queued с текстом черновика — UI показывает карточку
        с уже распознанным текстом, не дожидаясь turbo."""
        for g in groups:
            t0, t1 = base + g[0] / SR, base + g[1] / SR
            if t1 - t0 >= MIN_PHRASE:
                p0 = max(0, g[0] - int(PAD * SR))
                p1 = min(len(audio), g[1] + int(PAD * SR))
                self._qid += 1
                es, ru = self._take_draft(t0, t1)
                self.emit({"type": "queued", "qid": self._qid,
                           "t0": round(t0, 2), "t1": round(t1, 2),
                           "es": es, "ru": ru})
                self.final_q.put((self._qid, t0, t1, audio[p0:p1],
                                  base + p0 / SR))

    @staticmethod
    def _merge(segs, gap=0.35):
        """Склеивает соседние речевые сегменты с зазором < gap сек."""
        out = []
        for s in segs:
            if out and s["start"] - out[-1][1] < gap * SR:
                out[-1][1] = s["end"]
            else:
                out.append([s["start"], s["end"]])
        return out

    def _emit_partial(self, es, ru):
        if (es, ru) != self._last_partial:
            self._last_partial = (es, ru)
            self.emit({"type": "partial", "es": es, "ru": ru})

    # ---------- финализация ----------

    def _final_loop(self):
        while self.running or not self.final_q.empty():
            try:
                qid, t0, t1, audio, off = self.final_q.get(timeout=0.4)
            except queue.Empty:
                continue
            self.final_busy = True
            try:
                self._finalize(qid, t0, t1, audio, off)
            finally:
                self.final_busy = False

    @staticmethod
    def _sentences(segs, t0, t1, off):
        """Короткий финал — одна фраза; длинный (принудительная нарезка
        речи без пауз) режется на карточки по концам предложений.
        segs — [(start, end, text)] от whisper, off — абсолютное время
        начала куска аудио. → [(t0, t1, text)], уже через clean_asr."""
        if t1 - t0 <= SPLIT_OVER:
            es = clean_asr(" ".join(t for _, _, t in segs).strip())
            return [(t0, t1, es)] if es else []
        out, cur, cs = [], [], None
        for a, b, txt in segs:
            if not txt.strip():
                continue
            if cs is None:
                cs = a
            cur.append((b, txt))
            dur = b - cs
            if (dur >= 2.5 and txt.rstrip()[-1:] in ".!?…") or dur >= 9:
                out.append((cs, b, " ".join(t for _, t in cur)))
                cur, cs = [], None
        if cur:
            out.append((cs, cur[-1][0], " ".join(t for _, t in cur)))
        parts = []
        for a, b, txt in out:
            es = clean_asr(txt.strip())
            if es:
                parts.append((max(t0, off + a), min(t1, off + b), es))
        return parts

    def _finalize(self, qid, t0, t1, audio, off):
        s = db.get_settings()
        name = FINAL_MODEL.get(s["accuracy"]) or "small"
        lang = self._lang_setting()
        segs = None
        if name == "large-v3-turbo" and self._mlx_available():
            try:
                import mlx_whisper
                r = mlx_whisper.transcribe(
                    audio, path_or_hf_repo=MLX_REPO, language=lang,
                    condition_on_previous_text=False)
                segs = [(sg["start"], sg["end"], sg["text"].strip())
                        for sg in r["segments"]]
                detected = r.get("language") or "es"
            except Exception:
                segs = None     # GPU-путь сломался — тихо падаем на CPU
        if segs is None:
            try:
                segs, detected = self._transcribe(name, audio, beam=5, lang=lang)
            except Exception as e:
                self.emit({"type": "status", "text": f"ошибка ASR: {e}"})
                self.emit({"type": "final_drop", "qid": qid})
                return
        parts = self._sentences(segs, t0, t1, off)
        if not parts:
            self.emit({"type": "final_drop", "qid": qid})
            return
        if lang is None and detected and detected != self.detected_lang:
            self.detected_lang = detected
            self.emit({"type": "status", "text":
                       "определён язык: " +
                       self.LANG_NAMES.get(detected, detected)})
        src = lang or detected or "auto"
        phrases = []
        for p0, p1, es in parts:
            ru = None if src == s["target_lang"] else translate(
                es, src, s["target_lang"], prefer="argos")
            if src != s["target_lang"]:
                if not ru or ru.strip().casefold() == es.strip().casefold():
                    self._tr_misses += 1
                    if self._tr_misses == 3:
                        cur = self.LANG_NAMES.get(src, src)
                        self.emit({"type": "status", "text":
                                   f"⚠ перевод не получается — проверь язык "
                                   f"лекции (сейчас: {cur}) и интернет"})
                else:
                    self._tr_misses = 0
            pid = db.run(
                "INSERT INTO phrases(lecture_id,t0,t1,es,ru) VALUES(?,?,?,?,?)",
                (self.lecture_id, round(p0, 2), round(p1, 2), es, ru or ""))
            phrases.append({
                "id": pid, "qid": qid, "t0": round(p0, 2), "t1": round(p1, 2),
                "es": es, "ru": ru or "", "mark": "",
                "no_net": ru is None})
        self.emit({"type": "final", "qid": qid,
                   "phrase": phrases[0], "extra": phrases[1:]})

    # ---------- управление ----------

    @staticmethod
    def _valid_device(setting):
        """Индексы устройств меняются между сессиями — проверяем."""
        if not setting:
            return None
        import sounddevice as sd
        try:
            idx = int(setting)
            if sd.query_devices(idx)["max_input_channels"] > 0:
                return idx
        except Exception:
            pass
        return None

    @staticmethod
    def _to16k(x, rate):
        if rate == SR:
            return x
        n = max(1, int(round(rate / SR)))
        if n > 1:                       # грубый антиалиас перед прореживанием
            x = np.convolve(x, np.ones(n) / n, mode="same")
        idx = np.arange(0, len(x) - 1, rate / SR)
        return np.interp(idx, np.arange(len(x)), x).astype(np.float32)

    def _open_input(self, cb):
        """Открывает вход: 16 кГц, при отказе — родная частота устройства."""
        import sounddevice as sd
        device = self._valid_device(db.get_settings()["mic_device"])
        try:
            st = sd.InputStream(samplerate=SR, channels=1, dtype="float32",
                                blocksize=int(SR * BLOCK_SEC),
                                device=device, callback=cb)
            st.start()
            return st, SR
        except Exception:
            info = sd.query_devices(device, "input")
            rate = int(info["default_samplerate"])
            st = sd.InputStream(samplerate=rate, channels=1, dtype="float32",
                                blocksize=int(rate * BLOCK_SEC),
                                device=device, callback=cb)
            st.start()
            return st, rate

    def _open_stream(self):
        raw_q = queue.Queue()

        def cb(indata, frames, t, status):
            raw_q.put(indata[:, 0].copy())

        self.stream, rate = self._open_input(cb)
        if rate != SR:
            self.emit({"type": "status",
                       "text": f"микрофон открыт на {rate} Гц (пересчитываю в 16k)"})

        def pump():
            while self.running and self.stream is not None:
                try:
                    block = raw_q.get(timeout=0.3)
                except queue.Empty:
                    continue
                if not self.paused:
                    self._feed(self._to16k(block, rate))
        threading.Thread(target=pump, daemon=True).start()

    # ---------- фоновый монитор уровня (для стартового экрана) ----------

    def start_monitor(self):
        if self.running or getattr(self, "monitor", None):
            return
        self.monitor_rms = 0.0
        self.monitor_err = ""

        def cb(indata, frames, t, status):
            x = indata[:, 0]
            self.monitor_rms = float(np.sqrt(np.mean(x ** 2)) + 1e-9)

        try:
            self.monitor, _ = self._open_input(cb)
        except Exception as e:
            self.monitor = None
            self.monitor_err = str(e)

    def stop_monitor(self):
        m = getattr(self, "monitor", None)
        if m:
            try:
                m.stop(); m.close()
            except Exception:
                pass
        self.monitor = None

    def start(self, lecture_id, demo_file=None, demo_speed=2.0):
        if self.running:
            raise RuntimeError("лекция уже идёт")
        self.lecture_id = lecture_id
        self.samples = 0
        self.base_t = 0.0
        self.pending = np.zeros(0, dtype=np.float32)
        self.detected_lang = None
        self.paused = False
        self._last_partial = ""
        self._last_tr = (0.0, "", "")
        self._drafts = []
        self._tr_misses = 0
        self._qid = 0
        self.running = True

        import soundfile as sf
        audio_path = str(db.DATA_DIR / f"lecture_{lecture_id}.flac")
        self.audio_path = audio_path
        self.recorder = sf.SoundFile(audio_path, "w", samplerate=SR,
                                     channels=1, format="FLAC",
                                     subtype="PCM_16")

        threading.Thread(target=self._tick_loop, daemon=True).start()
        threading.Thread(target=self._final_loop, daemon=True).start()

        if demo_file:
            threading.Thread(target=self._demo_feed,
                             args=(demo_file, demo_speed), daemon=True).start()
        else:
            try:
                self._open_stream()
            except Exception as e:
                # откат: не оставляем «полузапущенную» лекцию
                self.running = False
                if self.recorder:
                    self.recorder.close()
                    self.recorder = None
                self.lecture_id = None
                db.run("DELETE FROM lectures WHERE id=?", (lecture_id,))
                raise RuntimeError(f"не удалось открыть микрофон: {e}") from e

    def _demo_feed(self, path, speed):
        from faster_whisper.audio import decode_audio
        audio = decode_audio(path, sampling_rate=SR)
        block = int(SR * BLOCK_SEC)
        for i in range(0, len(audio), block):
            while self.running and self.paused:
                time.sleep(0.2)
            if not self.running:
                return
            self._feed(audio[i:i + block])
            time.sleep(BLOCK_SEC / speed)

    def pause(self):
        """Пауза: микрофон закрыт, всё завершённое дописывается, лекция жива."""
        if not self.running or self.paused:
            return
        self.paused = True
        if self.stream:
            self.stream.stop(); self.stream.close(); self.stream = None
        # дописать завершённую речь из хвоста
        with self.alock:
            audio = self.pending.copy()
            base = self.base_t
        ts = self._vad(audio) if len(audio) > int(0.4 * SR) else []
        if ts:
            self._enqueue(audio, base, self._merge(ts))
            self._advance(ts[-1]["end"])
        self._drafts = []
        if self.recorder:
            try:
                self.recorder.flush()
            except Exception:
                pass
        self._emit_partial("", "")
        self.emit({"type": "paused", "on": True})
        if not self.final_q.empty() or self.final_busy:
            self.emit({"type": "status", "text":
                       "микрофон выключен; дописываю фразы, услышанные до паузы…"})

    def resume(self):
        if not self.running:
            return
        if not self.paused:   # UI мог зависнуть с плашкой — подтверждаем
            self.emit({"type": "paused", "on": False})
            return
        try:                      # сначала микрофон, потом снятие паузы —
            self._open_stream()   # иначе тихий рассинхрон UI и движка
        except Exception as e:
            self.emit({"type": "paused", "on": True})
            self.emit({"type": "status",
                       "text": f"не удалось включить микрофон: {e} — "
                               "попробуй ▶ ещё раз"})
            return
        self.paused = False
        self.emit({"type": "paused", "on": False})

    def stop(self):
        self.paused = False
        self.running = False
        if self.stream:
            self.stream.stop(); self.stream.close(); self.stream = None
        # добить остатки речи
        with self.alock:
            audio = self.pending.copy()
            base = self.base_t
        ts = self._vad(audio) if len(audio) > int(0.4 * SR) else []
        self._enqueue(audio, base, self._merge(ts))
        deadline = time.time() + 90
        while (not self.final_q.empty() or self.final_busy) \
                and time.time() < deadline:
            time.sleep(0.3)
        if self.recorder:
            self.recorder.close()
            self.recorder = None
        db.stop_lecture(self.lecture_id, self.audio_path)
        lid, self.lecture_id = self.lecture_id, None
        return lid

    def elapsed(self):
        return self.samples / SR if self.running else 0


def mic_level(device=None, dur=0.35):
    import sounddevice as sd
    device = Engine._valid_device(device if device is None else str(device))
    try:
        rec = sd.rec(int(SR * dur), samplerate=SR, channels=1,
                     dtype="float32", device=device)
        sd.wait()
    except Exception:
        info = sd.query_devices(device, "input")
        rate = int(info["default_samplerate"])
        rec = sd.rec(int(rate * dur), samplerate=rate, channels=1,
                     dtype="float32", device=device)
        sd.wait()
    return float(np.sqrt(np.mean(rec ** 2)) + 1e-9)


def mic_list():
    import sounddevice as sd
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            out.append({"id": i, "name": d["name"]})
    return out
