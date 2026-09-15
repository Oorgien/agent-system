#!/usr/bin/env bash
# Stage 0: memory storage and symlinks. Idempotent and safe to rerun.
# Run in EVERY worktree and on EVERY machine: symlinks are gitignored and are
# not shared between worktrees.
#
#   ./setup.sh              saved key and store; selected automatically on first run
#   ./setup.sh <project>    explicit key (see below for when this is needed)
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

# --- project key -----------------------------------------------------------
# The key MUST match across all worktrees of a repository. Otherwise repo/ and
# repo-billing/ point to different storage directories, and memory collected in
# one tree is absent from the other, contrary to the contract in AGENTS.md §1.
# This divergence is silent: both symlinks exist and no error is reported.
#
# The current directory name is unsuitable: worktrees have different names.
# Use the MAIN worktree's directory name. All worktrees share git-common-dir,
# which points to the main repository's .git directory, keeping the key stable.
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
  ORIGIN="command-line argument"
elif PROJECT="$(git -C "$ROOT" config --local --get agents.memoryKey 2>/dev/null)" && [ -n "$PROJECT" ]; then
  ORIGIN="shared git config: agents.memoryKey"
elif [ -n "$OLD_TARGET" ]; then
  PROJECT="$(basename "$OLD_TARGET")"
  ORIGIN="existing memory symlink"
elif PROJECT="$(project_key)" && [ -n "$PROJECT" ] && [ "$PROJECT" != "/" ]; then
  ORIGIN="main Git worktree directory name"
else
  PROJECT="$(basename "$ROOT")-memory"
  ORIGIN="current directory name"
  FALLBACK=1
fi

# The key is a single subdirectory name, not a path outside the store.
case "$PROJECT" in
  ""|.|..|*/*|*\\*|*$'\n'*|*$'\r'*)
    echo "ERROR: project key must be a nonempty directory name, not a path" >&2
    exit 1 ;;
esac

# --- memory store ----------------------------------------------------------
# Same precedence as the key: explicit value -> saved value -> default.
# Always read the saved value: rerunning without the variable would otherwise
# redirect memory to the default directory, as silently as changing the key.
IN_GIT=""
git -C "$ROOT" rev-parse --git-common-dir >/dev/null 2>&1 && IN_GIT=1

SAVED_STORE=""
if [ -n "$IN_GIT" ]; then
  SAVED_STORE="$(git -C "$ROOT" config --local --get agents.memoryStore 2>/dev/null || true)"
fi

if [ -n "${AGENTS_MEMORY_STORE:-}" ]; then
  STORE="$AGENTS_MEMORY_STORE"
  STORE_ORIGIN="AGENTS_MEMORY_STORE environment variable"
elif [ -n "$SAVED_STORE" ]; then
  STORE="$SAVED_STORE"
  STORE_ORIGIN="shared git config: agents.memoryStore"
elif [ -n "$OLD_TARGET" ]; then
  STORE="$(dirname "$OLD_TARGET")"
  STORE_ORIGIN="existing memory symlink"
else
  STORE="$HOME/.agents-memory"
  STORE_ORIGIN="default directory"
fi

# Validate the layout before mkdir, git config, or symlink changes.
# Do not delete the old shared history or hide it behind a nested git init.
if [ -e "$STORE/.git" ] || [ -L "$STORE/.git" ] || { [ -f "$STORE/HEAD" ] && [ -d "$STORE/objects" ]; }; then
  echo "ERROR: legacy shared Git store: $STORE. A separate repository per project is required; preserve the history and migrate memory explicitly." >&2
  exit 1
fi
TARGET="$STORE/$PROJECT"
if [ -L "$TARGET" ] || { [ -e "$TARGET" ] && [ ! -d "$TARGET" ]; } || [ -L "$TARGET/.git" ] || { [ -e "$TARGET/.git" ] && [ ! -d "$TARGET/.git" ]; } || [ -L "$TARGET/.gitkeep" ] || { [ -e "$TARGET/.gitkeep" ] && [ ! -f "$TARGET/.gitkeep" ]; }; then
  echo "ERROR: unsafe memory repository path: $TARGET" >&2
  exit 1
fi
if [ -d "$TARGET/.git" ]; then
  TOP="$(git -C "$TARGET" rev-parse --show-toplevel 2>/dev/null)" || exit 1
  [ "$(cd "$TOP" && pwd -P)" = "$(cd "$TARGET" && pwd -P)" ] || exit 1
elif [ -f "$TARGET/HEAD" ] && [ -d "$TARGET/objects" ]; then
  echo "ERROR: memory must be a worktree, not a bare repository: $TARGET" >&2
  exit 1
fi
mkdir -p "$STORE"
# Save the physical absolute path, independent of the next session's cwd.
STORE="$(cd "$STORE" && pwd -P)"
TARGET="$STORE/$PROJECT"

# --local reads and writes shared repository config, not global/config.worktree.
# A failure to save must stop setup before any symlink changes.
if [ -n "$IN_GIT" ]; then
  git -C "$ROOT" config --local --replace-all agents.memoryKey "$PROJECT"
  git -C "$ROOT" config --local --replace-all agents.memoryStore "$STORE"
fi

echo "agent-system setup"
echo "  project: $PROJECT   ($ORIGIN)"
echo "  store:   $STORE   ($STORE_ORIGIN)"
echo

if [ -n "$FALLBACK" ]; then
  say "WARNING: could not determine the main Git worktree."
  say "The key uses the directory name, which differs between worktrees; memory will"
  say "differ between trees. Set the key explicitly: ./setup.sh <project>"
  echo
fi

# --- 1. separate Git repository for project memory -------------------------
mkdir -p "$TARGET"
if [ ! -d "$TARGET/.git" ]; then
  git -C "$TARGET" init -q
  [ -f "$TARGET/.gitkeep" ] || touch "$TARGET/.gitkeep"
  if [ -z "$NO_COMMIT" ]; then
    git -C "$TARGET" add .gitkeep
    git -C "$TARGET" -c user.email=setup@local -c user.name=setup \
        commit -qm "init project memory" || say "WARNING: initial commit was not created; memory is connected without a commit"
  fi
  say "initialized memory Git repository at $TARGET"
  say "no remote configured; add a private remote for this project if needed"
fi

# --- 2. symlinks -----------------------------------------------------------
link() {  # link <path> <target>
  local path="$1" target="$2"
  mkdir -p "$(dirname "$path")"
  if [ -L "$path" ]; then
    if [ "$(readlink "$path")" = "$target" ]; then say "already in place: $path"; return; fi
    rm "$path"
  elif [ -e "$path" ]; then
    echo "  ERROR: $path exists and is not a symlink; resolve this manually" >&2
    return 1
  fi
  ln -s "$target" "$path"
  say "created symlink: $path -> $target"
}

[ -n "$STORAGE_ONLY" ] && exit 0

link "$ROOT/.agents/memory" "$STORE/$PROJECT"
link "$ROOT/.claude/skills" "../.agents/skills"

echo
echo "Done."
echo
echo "Verify:"
echo "  ls -l .agents/memory .claude/skills"
