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
PY="$(brew --prefix 2>/dev/null)/bin/python3"
[ -x "$PY" ] || PY="$(command -v python3)"

say "Окружение Python"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install -q --upgrade pip
if ! .venv/bin/pip install -q -r requirements.txt; then
  # у пакета av свежие готовые сборки только для macOS 14+;
  # на системе постарше берём версию 15.1.0 (собрана для macOS 13+),
  # в крайнем случае — собираем из исходников с ffmpeg
  echo "Подбираю совместимую версию av для этой macOS…"
  .venv/bin/pip install -q "av==15.1.0" || {
    echo "Ставлю ffmpeg и собираю av (это надолго, но один раз)…"
    brew install ffmpeg pkg-config
    .venv/bin/pip install -q av
  }
  .venv/bin/pip install -q -r requirements.txt
fi

say "Офлайн-перевод (модели argos: испанский → английский → русский)"
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

say "Модели распознавания речи (~2.5 ГБ, один раз; лучше по домашнему Wi-Fi)"
HF_HUB_DISABLE_PROGRESS_BARS=1 .venv/bin/python - <<'EOF'
from faster_whisper import WhisperModel
print("  черновая модель (small)…")
WhisperModel("small", device="auto", compute_type="int8")
print("  точная модель (large-v3-turbo, MLX)…")
from huggingface_hub import snapshot_download
snapshot_download("mlx-community/whisper-large-v3-turbo")
print("  распознавание готово")
EOF

say "Локальный ИИ (Ollama — чат и конспекты без интернета, ~3 ГБ)"
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

say "Значки на рабочем столе"
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
