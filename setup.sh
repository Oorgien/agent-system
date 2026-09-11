#!/usr/bin/env bash
# Этап 0: хранилище памяти + симлинки. Идемпотентно, безопасно перезапускать.
# Запускать в КАЖДОМ worktree и на КАЖДОЙ машине: симлинки gitignored и между
# рабочими деревьями не разделяются.
#
#   ./setup.sh              сохранённый ключ и хранилище; при первом запуске — автоматически
#   ./setup.sh <project>    ключ задан явно (см. ниже, когда это нужно)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Internal CLI integration: select target before any reads or writes.
STORAGE_ONLY=""
NO_COMMIT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --project) ROOT="$(cd "$2" && pwd -P)"; shift 2 ;;
    --storage-only) STORAGE_ONLY=1; shift ;;
    --no-commit) NO_COMMIT=1; shift ;;
    *) break ;;
  esac
done

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
  printf '%s-memory\n' "$(basename "$(dirname "$common")")"
}

OLD_TARGET=""
if [ -L "$ROOT/.agents/memory" ] && [ -d "$ROOT/.agents/memory" ]; then
  OLD_TARGET="$(cd "$ROOT/.agents/memory" && pwd -P)"
fi

FALLBACK=""
if [ $# -gt 0 ] && [ -n "$1" ]; then
  PROJECT="$1"
  ORIGIN="аргумент командной строки"
elif PROJECT="$(git -C "$ROOT" config --local --get agents.memoryKey 2>/dev/null)" && [ -n "$PROJECT" ]; then
  ORIGIN="общий git config: agents.memoryKey"
elif [ -n "$OLD_TARGET" ]; then
  PROJECT="$(basename "$OLD_TARGET")"
  ORIGIN="существующая ссылка памяти"
elif PROJECT="$(project_key)" && [ -n "$PROJECT" ] && [ "$PROJECT" != "/" ]; then
  ORIGIN="имя каталога основного рабочего дерева git"
else
  PROJECT="$(basename "$ROOT")-memory"
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
elif [ -n "$OLD_TARGET" ]; then
  STORE="$(dirname "$OLD_TARGET")"
  STORE_ORIGIN="существующая ссылка памяти"
else
  STORE="$HOME/.agents-memory"
  STORE_ORIGIN="стандартный каталог"
fi

# Проверить схему до mkdir, git config и изменения ссылок.
# Старую общую историю не удаляем и не прячем вложенным git init.
if [ -e "$STORE/.git" ] || [ -L "$STORE/.git" ] || { [ -f "$STORE/HEAD" ] && [ -d "$STORE/objects" ]; }; then
  echo "ОШИБКА: старое общее Git-хранилище: $STORE. Нужен отдельный репозиторий на проект; сохраните историю и перенесите память явно." >&2
  exit 1
fi
TARGET="$STORE/$PROJECT"
if [ -L "$TARGET" ] || { [ -e "$TARGET" ] && [ ! -d "$TARGET" ]; } || [ -L "$TARGET/.git" ] || { [ -e "$TARGET/.git" ] && [ ! -d "$TARGET/.git" ]; } || [ -L "$TARGET/.gitkeep" ] || { [ -e "$TARGET/.gitkeep" ] && [ ! -f "$TARGET/.gitkeep" ]; }; then
  echo "ОШИБКА: небезопасный путь репозитория памяти: $TARGET" >&2
  exit 1
fi
if [ -d "$TARGET/.git" ]; then
  TOP="$(git -C "$TARGET" rev-parse --show-toplevel 2>/dev/null)" || exit 1
  [ "$(cd "$TOP" && pwd -P)" = "$(cd "$TARGET" && pwd -P)" ] || exit 1
elif [ -f "$TARGET/HEAD" ] && [ -d "$TARGET/objects" ]; then
  echo "ОШИБКА: память должна быть рабочим деревом, не bare-репозиторием: $TARGET" >&2
  exit 1
fi
mkdir -p "$STORE"
# Сохраняем физический абсолютный путь, независимый от cwd следующей сессии.
STORE="$(cd "$STORE" && pwd -P)"
TARGET="$STORE/$PROJECT"

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

# --- 1. отдельный Git-репозиторий памяти проекта ----------------------------
mkdir -p "$TARGET"
if [ ! -d "$TARGET/.git" ]; then
  git -C "$TARGET" init -q
  [ -f "$TARGET/.gitkeep" ] || touch "$TARGET/.gitkeep"
  if [ -z "$NO_COMMIT" ]; then
    git -C "$TARGET" add .gitkeep
    git -C "$TARGET" -c user.email=setup@local -c user.name=setup \
        commit -qm "init project memory" || say "ВНИМАНИЕ: начальный коммит не создан; память подключена без коммита"
  fi
  say "инициализирован git-репозиторий памяти в $TARGET"
  say "remote не настроен — при необходимости подключите приватный remote этого проекта"
fi

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

[ -n "$STORAGE_ONLY" ] && exit 0

link "$ROOT/.agents/memory" "$STORE/$PROJECT"
link "$ROOT/.claude/skills" "../.agents/skills"

echo
echo "Готово."
echo
echo "Проверка:"
echo "  ls -l .agents/memory .claude/skills"
