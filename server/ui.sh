# shellcheck shell=bash
# shellcheck disable=SC2034  # the colour variables are used by the scripts that source this file
# FATLLM terminal look, shared by install.sh and fatllm.
# English only (phone SSH apps show Persian badly) and never wider than the terminal
# (a phone in portrait is about 40-60 columns). Colours and Unicode only on a real terminal.

ui_init() {
  UI_COLS=80
  if [ -t 1 ]; then UI_COLS="$(tput cols 2>/dev/null || echo 80)"; fi
  case "$UI_COLS" in '' | *[!0-9]*) UI_COLS=80 ;; esac
  [ "$UI_COLS" -ge 24 ] || UI_COLS=24
  UI_W=$(( UI_COLS > 62 ? 62 : UI_COLS ))
  UI_TTY=0
  R="" B="" D="" G="" C="" Y="" RD="" M="" L1="" L2="" L3="" L4="" L5="" L6=""
  if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != dumb ]; then
    UI_TTY=1
    R=$'\033[0m' B=$'\033[1m' D=$'\033[2m'
    local n; n="$(tput colors 2>/dev/null || echo 8)"
    case "$n" in '' | *[!0-9]*) n=8 ;; esac
    if [ "$n" -ge 256 ]; then
      G=$'\033[38;5;79m' C=$'\033[38;5;117m' Y=$'\033[38;5;221m' RD=$'\033[38;5;210m' M=$'\033[38;5;183m'
      L1=$'\033[38;5;45m' L2=$'\033[38;5;44m' L3=$'\033[38;5;43m' L4=$'\033[38;5;42m' L5=$'\033[38;5;78m' L6=$'\033[38;5;114m'
    else
      G=$'\033[32m' C=$'\033[36m' Y=$'\033[33m' RD=$'\033[31m' M=$'\033[35m'
      L1=$C L2=$C L3=$C L4=$G L5=$G L6=$G
    fi
  fi
  if [ "${FATLLM_ASCII:-}" = 1 ] || [ "${TERM:-}" = linux ]; then
    UI_U=0 S_OK="+" S_BAD="x" S_WARN="!" S_STEP=">" S_DOT="*" S_ODOT="o" S_H="-" S_TL="+" S_TR="+" S_BL="+" S_BR="+" S_V="|"
    S_ELL="..." S_NL=" / "
  else
    UI_U=1 S_OK="✓" S_BAD="✗" S_WARN="!" S_STEP="›" S_DOT="●" S_ODOT="○" S_H="─" S_TL="╭" S_TR="╮" S_BL="╰" S_BR="╯" S_V="│"
    S_ELL="…" S_NL=" ⏎ "
  fi
  UI_SPIN_I=0
}

ui_rep() { local s="" i; for ((i = 0; i < $2; i++)); do s+="$1"; done; printf '%s' "$s"; }

ui_logo() {  # ui_logo [tagline]
  local tag="${1:-full server access for AI agents}"
  echo
  if [ "$UI_U" = 1 ] && [ "$UI_COLS" -ge 30 ]; then
    printf '  %s█▀▀ %s▄▀▄ %s▀█▀ %s█   %s█   %s█▄ ▄█%s\n' "$L1" "$L2" "$L3" "$L4" "$L5" "$L6" "$R"
    printf '  %s█▀  %s█▀█ %s █  %s█   %s█   %s█ ▀ █%s\n' "$L1" "$L2" "$L3" "$L4" "$L5" "$L6" "$R"
    printf '  %s▀   %s▀ ▀ %s ▀  %s▀▀▀ %s▀▀▀ %s▀   ▀%s\n' "$L1" "$L2" "$L3" "$L4" "$L5" "$L6" "$R"
  else
    printf '  %s%sF A T L L M%s\n' "$B" "$C" "$R"
  fi
  printf '  %s%s%s\n\n' "$D" "$tag" "$R"
}

ui_rule() { printf '  %s%s%s\n' "$D" "$(ui_rep "$S_H" $(( UI_W - 4 )))" "$R"; }
ui_head() { printf '\n  %s%s%s%s  %s%s%s\n' "$B" "$C" "$1" "$R" "$D" "${2:-}" "$R"; }

say() { ui_spin_end; printf '  %s%s%s %s\n' "$C" "$S_STEP" "$R" "$*"; }
ok() { ui_spin_end; printf '  %s%s%s %s\n' "$G" "$S_OK" "$R" "$*"; }
note() { printf '    %s%s%s\n' "$D" "$*" "$R"; }
warn() { ui_spin_end; printf '  %s%s %s%s\n' "$Y" "$S_WARN" "$*" "$R" >&2; }
die() { ui_spin_end; printf '\n  %s%s%s %s%s\n\n' "$RD" "$B" "$S_BAD" "$*" "$R" >&2; exit 1; }

ui_kv() { printf '  %s%-9s%s %s\n' "$D" "$1" "$R" "$2"; }
ui_cmd() { printf '  %s%-17s%s %s%s%s\n' "$C" "$1" "$R" "$D" "$2" "$R"; }
ui_dot() {  # ui_dot good|bad|off "label" "value"
  local col="$G" sym="$S_DOT"
  case "$1" in bad) col="$RD" ;; off) col="$D" sym="$S_ODOT" ;; warn) col="$Y" ;; esac
  printf '  %s%s%s %-10s %s\n' "$col" "$sym" "$R" "$2" "$3"
}

ui_box() {  # ui_box COLOUR "short ASCII text" -> a rounded one-line box
  local col="$1" text="$2" n
  n=$(( ${#text} + 4 ))
  if [ "$n" -gt $(( UI_COLS - 4 )) ]; then printf '  %s%s %s%s\n' "$col" "$S_OK" "$text" "$R"; return; fi
  printf '  %s%s%s%s%s\n' "$col" "$S_TL" "$(ui_rep "$S_H" "$n")" "$S_TR" "$R"
  printf '  %s%s%s  %s%s%s  %s%s%s\n' "$col" "$S_V" "$R" "$B" "$text" "$R" "$col" "$S_V" "$R"
  printf '  %s%s%s%s%s\n' "$col" "$S_BL" "$(ui_rep "$S_H" "$n")" "$S_BR" "$R"
}

ui_spin() {  # one spinner frame on the current line (call it in a loop)
  [ "$UI_TTY" = 1 ] || return 0
  local f
  if [ "$UI_U" = 1 ]; then f=(⠋ ⠙ ⠹ ⠸ ⠼ ⠴ ⠦ ⠧ ⠇ ⠏); else f=('|' '/' '-' '\'); fi
  printf '\r\033[K  %s%s%s %s' "$C" "${f[UI_SPIN_I % ${#f[@]}]}" "$R" "$1"
  UI_SPIN_I=$(( UI_SPIN_I + 1 ))
  UI_SPINNING=1
}
ui_spin_end() { if [ "${UI_SPINNING:-0}" = 1 ]; then printf '\r\033[K'; UI_SPINNING=0; fi; return 0; }
ui_wait() {  # ui_wait "label" SECONDS: spin (or just sleep when not on a terminal)
  local i
  if [ "$UI_TTY" = 1 ]; then for ((i = 0; i < $2 * 5; i++)); do ui_spin "$1"; sleep 0.2; done
  else sleep "$2"; fi
}
