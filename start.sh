#!/bin/bash
# Запуск «Студии»: тихо подтягивает свежую версию с GitHub (если есть сеть),
# поднимает сервер, открывает браузер. Его вызывает значок «Студия.command».
cd "$(dirname "$0")"

# Автообновление. --ff-only: при локальных правках не трогаем ничего,
# просто запускаемся как есть. lowSpeed*: без сети не висим, а быстро сдаёмся.
if git remote get-url origin >/dev/null 2>&1; then
  before=$(git rev-parse HEAD 2>/dev/null)
  if git -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=7 pull --ff-only --quiet 2>/dev/null; then
    after=$(git rev-parse HEAD 2>/dev/null)
    if [ -n "$before" ] && [ "$before" != "$after" ]; then
      echo "Обновилась до свежей версии."
      if ! git diff --quiet "$before" "$after" -- requirements.txt; then
        .venv/bin/pip install -q -r requirements.txt
      fi
    fi
  fi
fi

# Уже запущена? Просто открываем окно.
if curl -fsS -m 1 http://localhost:8899/api/state >/dev/null 2>&1; then
  open http://localhost:8899
  exit 0
fi

# Локальный ИИ: поднимаем обратно, если установлен, но выключен
# (значок «выключить» гасит и его)
if command -v ollama >/dev/null 2>&1 && ! pgrep -x ollama >/dev/null 2>&1; then
  brew services start ollama >/dev/null 2>&1 || true
fi

mkdir -p data
nohup .venv/bin/python app.py >> data/app.log 2>&1 &

for i in $(seq 1 120); do
  curl -fsS -m 1 http://localhost:8899 >/dev/null 2>&1 && break
  sleep 0.5
done
open http://localhost:8899
