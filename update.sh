#!/bin/bash
# Принудительное обновление «Студии» с перезапуском.
# Для терминала или Claude Code: cd ~/translator && ./update.sh
set -e
cd "$(dirname "$0")"

if ! git pull --ff-only; then
  echo "Не обновилось: есть локальные изменения. Смотри git status;"
  echo "спрятать их можно так: git stash && ./update.sh"
  exit 1
fi
.venv/bin/pip install -q -r requirements.txt

# Перезапуск сервера, если он запущен. Паттерн без .venv: питон из venv
# виден в ps как …/Python.framework/…/Python app.py
if pkill -if "python.* app\.py" 2>/dev/null; then
  echo "Перезапускаю сервер…"
  sleep 1
fi
./start.sh
