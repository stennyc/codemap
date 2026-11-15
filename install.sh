#!/usr/bin/env bash
# install.sh — code_map plugin installer
# Installs code_map into the Hermes Agent plugin directory.
# Supports both Linux and Windows (via git-bash/MSYS or native).
#
# Usage:
#   bash install.sh [--profile <name>] [--all-profiles] [--dry-run]
#
# Platforms:
#   Linux:     $HERMES_HOME/plugins/code_map  (default: ~/.hermes/plugins/code_map)
#   Windows:   C:/Users/<user>/AppData/Local/hermes/plugins/code_map
#              AND  ~/.hermes/plugins/code_map  (dual-install for cross-platform sync)
#
# Profile symlinks:
#   Creates a symlink in each profile's plugins/ directory pointing at the
#   global plugin source. Requires plugins.enabled to contain "code_map" in
#   the profile's config.yaml.
set -euo pipefail

DRY_RUN=false
PROFILE=""
ALL_PROFILES=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)    DRY_RUN=true; shift ;;
    --profile)    PROFILE="$2"; shift 2 ;;
    --all-profiles) ALL_PROFILES=true; shift ;;
    *) echo "unknown arg: $1"; exit 2 ;;
  esac
done

# ── detect paths ──────────────────────────────────────────────────────────────
HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_NAME="code_map"

# Windows: two possible locations (AppData + ~/.hermes)
if command -v cygpath >/dev/null 2>&1; then
  # MSYS/git-bash on Windows
  HERMES_HOME_WIN="$(cygpath -m "$HERMES_HOME")"
  SCRIPT_DIR_WIN="$(cygpath -m "$SCRIPT_DIR")"
  PLUGINS_DIR_WIN="C:/Users/$(whoami)/AppData/Local/hermes/plugins"
else
  HERMES_HOME_WIN=""
  SCRIPT_DIR_WIN=""
  PLUGINS_DIR_WIN=""
fi

install_to() {
  local dest="$1"
  local label="$2"
  if [ "$DRY_RUN" = true ]; then
    echo "  [dry-run] would install code_map to $label: $dest"
    return 0
  fi
  mkdir -p "$dest"
  # Copy all non-hidden files from source to dest
  cp -r "$SCRIPT_DIR/"* "$dest/" 2>/dev/null || true
  # Preserve .git if present (don't overwrite it)
  if [ -d "$SCRIPT_DIR/.git" ] && [ ! -d "$dest/.git" ]; then
    cp -r "$SCRIPT_DIR/.git" "$dest/"
  fi
  echo "  [ok] $label: $dest"
}

# ── step 1: install plugin source ────────────────────────────────────────────
echo "=== code_map installer ==="
echo ""
echo "[1/3] Installing plugin source..."

# Linux: always install to $HERMES_HOME/plugins/
install_to "$HERMES_HOME/plugins/$PLUGIN_NAME" "linux ($HERMES_HOME)"

# Windows: also install to AppData/Local/hermes/plugins/
if [ -n "$PLUGINS_DIR_WIN" ]; then
  win_dest="$PLUGINS_DIR_WIN/$PLUGIN_NAME"
  install_to "$win_dest" "windows (AppData)"
fi

# ── step 2: discover profiles ────────────────────────────────────────────────
echo ""
echo "[2/3] Setting up profile symlinks..."

# Find profiles directory (same dual-path logic)
if [ -n "$PLUGINS_DIR_WIN" ]; then
  # Windows: AppData-based profiles
  PROFILES_DIR="$PLUGINS_DIR_WIN/../profiles"
else
  PROFILES_DIR="$HERMES_HOME/profiles"
fi

if [ ! -d "$PROFILES_DIR" ]; then
  echo "  [warn] profiles dir not found: $PROFILES_DIR"
else
  if [ -n "$PROFILE" ]; then
    PROFILE_LIST=("$PROFILE")
  elif [ "$ALL_PROFILES" = true ]; then
    PROFILE_LIST=($(ls "$PROFILES_DIR"))
  else
    # Default: all non-hidden profiles
    PROFILE_LIST=()
    for d in "$PROFILES_DIR"/*/; do
      [ -d "$d" ] || continue
      name=$(basename "$d")
      [[ "$name" == .* ]] && continue
      PROFILE_LIST+=("$name")
    done
  fi

  for prof in "${PROFILE_LIST[@]}"; do
    prof_plugins="$PROFILES_DIR/$prof/plugins"
    link_target="$prof_plugins/$PLUGIN_NAME"
    src_path="$PLUGINS_DIR_WIN/$PLUGIN_NAME" 2>/dev/null || src_path="$HERMES_HOME/plugins/$PLUGIN_NAME"

    # Check if config.yaml has code_map in plugins.enabled
    cfg="$prof_plugins/../config.yaml"
    has_enabled=false
    if [ -f "$cfg" ] && grep -qE '^\s*-\s*code_map\s*$' "$cfg" 2>/dev/null; then
      has_enabled=true
    fi

    if [ "$has_enabled" = true ]; then
      if [ "$DRY_RUN" = true ]; then
        echo "  [dry-run] would symlink $link_target -> $src_path"
      else
        # Remove existing
        rm -f "$link_target"
        [ -d "$link_target" ] && rm -rf "$link_target"
        # Create symlink (cross-platform)
        if command -v cygpath >/dev/null 2>&1; then
          # Windows: use native path for symlink target
          src_native="$(cygpath -m "$src_path")"
          ln -s "$src_native" "$link_target"
        else
          ln -s "$src_path" "$link_target"
        fi
        echo "  [ok] $prof/plugins/$PLUGIN_NAME -> $src_path"
      fi
    fi
  done
fi

# ── step 3: verify ────────────────────────────────────────────────────────────
echo ""
echo "[3/3] Verification..."
installed_count=0
for check_dir in "$HERMES_HOME/plugins/$PLUGIN_NAME" "${PLUGINS_DIR_WIN:+$PLUGINS_DIR_WIN/$PLUGIN_NAME}"; do
  [ -n "$check_dir" ] && [ -f "$check_dir/__init__.py" ] && installed_count=$((installed_count+1))
done
echo "  installed to $installed_count location(s)"
echo ""
echo "Done. If this is a new install, restart the Hermes gateway for changes to take effect."
