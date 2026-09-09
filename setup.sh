#!/usr/bin/env bash
# Этап 0: хранилище памяти + симлинки. Идемпотентно, безопасно перезапускать.
# Запускать в КАЖДОМ worktree и на КАЖДОЙ машине: симлинки gitignored и между
# рабочими деревьями не разделяются.
#
#   ./setup.sh              сохранённый ключ и хранилище; при первом запуске — автоматически
#   ./setup.sh <project>    ключ задан явно (см. ниже, когда это нужно)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

say() { printf '  %s\n' "$*"; }

# --- ключ проекта ----------------------------------------------------------
# Ключ ОБЯЗАН совпадать во всех worktrees одного репозитория. Иначе repo/ и
# repo-billing/ линкуются в разные каталоги хранилища, и память, добытая в одном
# дереве, во втором просто не существует — при том что AGENTS.md §1 обещает
# обратное. Молчаливое расхождение: симлинки на месте, ошибки нет.
#
# Имя текущего каталога для этого не годится: у worktree оно другое по построению.
# Берём имя каталога ОСНОВНОГО рабочего дерева. git-common-dir у всех worktrees
# один и тот же и указывает в .git основного репозитория, поэтому ключ стабилен.
project_key() {
  local common
  common="$(git -C "$ROOT" rev-parse --git-common-dir 2>/dev/null)" || return 1
  [ -n "$common" ] || return 1
  common="$(cd "$ROOT" && cd "$common" 2>/dev/null && pwd)" || return 1
  basename "$(dirname "$common")"
}

FALLBACK=""
if [ $# -gt 0 ] && [ -n "$1" ]; then
  PROJECT="$1"
  ORIGIN="аргумент командной строки"
elif PROJECT="$(git -C "$ROOT" config --local --get agents.memoryKey 2>/dev/null)" && [ -n "$PROJECT" ]; then
  ORIGIN="общий git config: agents.memoryKey"
elif PROJECT="$(project_key)" && [ -n "$PROJECT" ] && [ "$PROJECT" != "/" ]; then
  ORIGIN="имя каталога основного рабочего дерева git"
else
  PROJECT="$(basename "$ROOT")"
  ORIGIN="имя текущего каталога"
  FALLBACK=1
fi

# Ключ — имя одного подкаталога, не путь вне хранилища.
case "$PROJECT" in
  ""|.|..|*/*|*\\*|*$'\n'*|*$'\r'*)
    echo "ОШИБКА: ключ проекта должен быть непустым именем каталога, не путём" >&2
    exit 1 ;;
esac

# --- хранилище -------------------------------------------------------------
# Приоритет тот же, что у ключа: явное значение -> сохранённое -> стандартное.
# Читать сохранённое обязательно: иначе повторный запуск без переменной вернул бы
# память в стандартный каталог — тот же тихий переезд, что и при смене ключа.
IN_GIT=""
git -C "$ROOT" rev-parse --git-common-dir >/dev/null 2>&1 && IN_GIT=1

SAVED_STORE=""
if [ -n "$IN_GIT" ]; then
  SAVED_STORE="$(git -C "$ROOT" config --local --get agents.memoryStore 2>/dev/null || true)"
fi

if [ -n "${AGENTS_MEMORY_STORE:-}" ]; then
  STORE="$AGENTS_MEMORY_STORE"
  STORE_ORIGIN="переменная AGENTS_MEMORY_STORE"
elif [ -n "$SAVED_STORE" ]; then
  STORE="$SAVED_STORE"
  STORE_ORIGIN="общий git config: agents.memoryStore"
else
  STORE="$HOME/.agents-memory"
  STORE_ORIGIN="стандартный каталог"
fi

STORE_EXISTED=1
[ -d "$STORE" ] || STORE_EXISTED=""
mkdir -p "$STORE"

# Абсолютный физический путь. Относительное значение переменной разрешается здесь
# и сейчас: сохранить его как есть значит дать следующей сессии истолковать его
# от другого каталога. По той же причине -P: сравнение в check_state.py идёт
# по разрешённым путям.
STORE="$(cd "$STORE" && pwd -P)"

# --local читает и пишет общий repository config, не global/config.worktree.
# Ошибка сохранения должна остановить setup до изменения симлинков.
if [ -n "$IN_GIT" ]; then
  git -C "$ROOT" config --local --replace-all agents.memoryKey "$PROJECT"
  git -C "$ROOT" config --local --replace-all agents.memoryStore "$STORE"
fi

echo "agent-system setup"
echo "  проект:    $PROJECT   ($ORIGIN)"
echo "  хранилище: $STORE   ($STORE_ORIGIN)"
echo

if [ -n "$FALLBACK" ]; then
  say "ВНИМАНИЕ: не удалось определить основное рабочее дерево git."
  say "Ключ взят из имени каталога, а у worktree оно другое — память окажется"
  say "разной в разных деревьях. Задайте ключ явно: ./setup.sh <project>"
  echo
fi

# --- 1. хранилище памяти: одно репо на все проекты -------------------------
[ -n "$STORE_EXISTED" ] || say "создано $STORE"

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
  mkdir -p "$(dirname "$path")"
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
