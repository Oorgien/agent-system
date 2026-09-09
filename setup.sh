#!/usr/bin/env bash
# Этап 0: хранилище памяти + симлинки. Идемпотентно, безопасно перезапускать.
# Запускать в КАЖДОМ worktree и на КАЖДОЙ машине: симлинки gitignored и между
# рабочими деревьями не разделяются.
set -euo pipefail

STORE="${AGENTS_MEMORY_STORE:-$HOME/.agents-memory}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT="${1:-$(basename "$ROOT")}"

say() { printf '  %s\n' "$*"; }

echo "agent-system setup"
echo "  проект:    $PROJECT"
echo "  хранилище: $STORE"
echo

# --- 1. хранилище памяти: одно репо на все проекты -------------------------
if [ ! -d "$STORE" ]; then
  mkdir -p "$STORE"
  say "создано $STORE"
fi

if [ ! -d "$STORE/.git" ]; then
  git -C "$STORE" init -q
  cat > "$STORE/README.md" <<'EOF'
# agents-memory

Приватное хранилище долгосрочной памяти агентов. Один подкаталог на проект.
Один факт — один файл: так кросс-машинные мержи почти не конфликтуют.

Перенос на другую машину:

    git remote add origin <приватный-remote>
    git push -u origin main
EOF
  git -C "$STORE" add README.md
  git -C "$STORE" -c user.email=setup@local -c user.name=setup \
      commit -qm "init memory store" || true
  say "инициализирован git-репозиторий в $STORE"
  say "remote не настроен — добавьте, когда понадобится перенос между машинами"
fi

mkdir -p "$STORE/$PROJECT"
[ -f "$STORE/$PROJECT/.gitkeep" ] || touch "$STORE/$PROJECT/.gitkeep"

# --- 2. симлинки -----------------------------------------------------------
link() {  # link <путь> <цель>
  local path="$1" target="$2"
  if [ -L "$path" ]; then
    if [ "$(readlink "$path")" = "$target" ]; then say "уже на месте: $path"; return; fi
    rm "$path"
  elif [ -e "$path" ]; then
    echo "  ОШИБКА: $path существует и не является симлинком — разберитесь вручную" >&2
    return 1
  fi
  ln -s "$target" "$path"
  say "создан симлинк: $path -> $target"
}

link "$ROOT/.agents/memory" "$STORE/$PROJECT"
link "$ROOT/.claude/skills" "../.agents/skills"

echo
echo "Готово."
echo
echo "Проверка:"
echo "  ls -l .agents/memory .claude/skills"
