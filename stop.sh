#!/bin/bash
# Полное выключение «Студии»: сервер + локальный ИИ.
# Закрытие вкладки браузера сервер не трогает (это специально — лекция
# переживает случайно закрытое окно); выключение — вот этим скриптом.
cd "$(dirname "$0")"

# идёт лекция — сначала штатный стоп, чтобы дописались последние фразы
st=$(curl -fsS -m 2 http://localhost:8899/api/state 2>/dev/null)
if echo "$st" | grep -q '"running": *true'; then
  echo "Завершаю лекцию (дописываю последние фразы)…"
  curl -fsS -m 120 -X POST http://localhost:8899/api/lecture/stop \
    >/dev/null 2>&1
fi

if pkill -if "python.* app\.py" 2>/dev/null; then
  echo "Студия выключена."
else
  echo "Студия и так не работала."
fi

if pgrep -x ollama >/dev/null 2>&1; then
  brew services stop ollama >/dev/null 2>&1 || pkill -x ollama
  echo "Локальный ИИ выключен."
fi
