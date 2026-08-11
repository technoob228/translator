#!/bin/bash
# Установка «Студии» на этом компьютере (вторая ступень, запускается
# бутстрапом из gist или вручную: cd ~/translator && ./install.sh).
# Ставит зависимости Python, модели перевода и распознавания, локальный ИИ
# и кладёт на рабочий стол значок «Студия». Повторный запуск безопасен.
set -e
cd "$(dirname "$0")"

say() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

if [ "$(uname -m)" != "arm64" ]; then
  echo "Нужен Mac с чипом Apple (M1 и новее) — распознавание речи иначе не заработает."
  exit 1
fi

eval "$(/opt/homebrew/bin/brew shellenv)" 2>/dev/null || true
export HOMEBREW_NO_AUTO_UPDATE=1

say "Шаг 1 из 5: окружение Python (~5 мин)"
OSVER=$(sw_vers -productVersion); OSMAJ=${OSVER%%.*}
echo "macOS $OSVER"
# строго Python 3.13: у всех наших пакетов есть готовые сборки под него
# на любой macOS от 11 — ничего не компилируется из исходников
brew list python@3.13 >/dev/null 2>&1 || brew install python@3.13
PY="$(brew --prefix)/opt/python@3.13/bin/python3.13"
if [ -x .venv/bin/python ] && ! .venv/bin/python -c \
    'import sys; sys.exit(0 if sys.version_info[:2] == (3, 13) else 1)'; then
  echo "Пересоздаю окружение под Python 3.13…"
  rm -rf .venv
fi
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
# av (декодер аудио): выбираем версию с готовой сборкой под эту macOS
if [ "$OSMAJ" -ge 14 ]; then AV="av"
elif [ "$OSMAJ" -eq 13 ]; then AV="av<16"
else AV="av<14"; fi
.venv/bin/pip install -q "$AV" || {
  echo "Готовой сборки av не нашлось — ставлю ffmpeg и собираю (долго, один раз)…"
  brew install ffmpeg pkg-config
  .venv/bin/pip install -q av
}
# без -q: здесь качаются сотни мегабайт — пусть виден прогресс
.venv/bin/pip install -r requirements.txt
# GPU-ускорение точной модели (MLX) — есть только на macOS 13.5+
if ! .venv/bin/pip install mlx-whisper; then
  echo "⚠ GPU-ускорение (MLX) на этой macOS недоступно — точное распознавание"
  echo "  пойдёт по процессору, чуть медленнее. Всё остальное работает."
fi

say "Шаг 2 из 5: офлайн-перевод, ~500 МБ (испанский → английский → русский)"
.venv/bin/python - <<'EOF'
from argostranslate import package, translate
have = {(l.code) for l in translate.get_installed_languages()}
need = [("es", "en"), ("en", "ru")]
if not ({"es", "en", "ru"} <= have):
    package.update_package_index()
    avail = package.get_available_packages()
    for a, b in need:
        p = next(x for x in avail if x.from_code == a and x.to_code == b)
        print(f"  качаю {a}->{b}…")
        package.install_from_path(p.download())
print("  перевод готов:", translate.translate("hola mundo", "es", "ru"))
EOF

say "Шаг 3 из 5: модели распознавания речи, ~2.5 ГБ (самый долгий шаг)"
.venv/bin/python - <<'EOF'
from faster_whisper import WhisperModel
print("  черновая модель (small)…")
WhisperModel("small", device="auto", compute_type="int8")
try:
    import mlx_whisper  # noqa: F401
    print("  точная модель (large-v3-turbo, GPU)…")
    from huggingface_hub import snapshot_download
    snapshot_download("mlx-community/whisper-large-v3-turbo")
except ImportError:
    print("  точная модель (large-v3-turbo, CPU)…")
    WhisperModel("large-v3-turbo", device="auto", compute_type="int8")
print("  распознавание готово")
EOF

say "Шаг 4 из 5: локальный ИИ Ollama, ~3 ГБ (по желанию)"
OLLAMA=yes
if [ -t 0 ] || [ -e /dev/tty ]; then
  printf "Поставить локальный ИИ? Без него ИИ работает только через облако [Y/n] "
  read -r ans < /dev/tty || ans=""
  case "$ans" in [nNнН]*) OLLAMA=no ;; esac
fi
if [ "$OLLAMA" = yes ]; then
  command -v ollama >/dev/null 2>&1 || brew install ollama
  brew services start ollama >/dev/null 2>&1 || true
  for i in $(seq 1 20); do
    curl -fsS -m 1 http://localhost:11434 >/dev/null 2>&1 && break
    sleep 1
  done
  ollama pull qwen3:4b-instruct || echo "Модель не скачалась — можно повторить позже: ollama pull qwen3:4b-instruct"
fi

say "Шаг 5 из 5: значки на рабочем столе"
cat > "$HOME/Desktop/Студия.command" <<EOF
#!/bin/bash
exec "\$HOME/translator/start.sh"
EOF
chmod +x "$HOME/Desktop/Студия.command"
cat > "$HOME/Desktop/Студия — выключить.command" <<EOF
#!/bin/bash
exec "\$HOME/translator/stop.sh"
EOF
chmod +x "$HOME/Desktop/Студия — выключить.command"

say "Готово"
echo "Запускаю приложение… (в первый раз macOS спросит разрешение на микрофон — нажать «Разрешить»)"
echo "Дальше просто двойной клик по значку «Студия» на рабочем столе."
echo "Ключ облачного ИИ вставляется в приложении: Настройки → ключ Uno."
./start.sh
