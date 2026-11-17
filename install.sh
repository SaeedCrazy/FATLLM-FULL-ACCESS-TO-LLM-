#!/usr/bin/env bash
# FATLLM installer: one command on your server, and you get a key that gives Claude
# full (root) access to it.
#
#   bash <(curl -Ls https://raw.githubusercontent.com/SaeedCrazy/FATLLM-FULL-ACCESS-TO-LLM-/HEAD/install.sh)
#
# Options (after the command: bash <(curl ...) --domain my.example.com):
#   --domain NAME   use your own domain (its DNS A record must point to this server)
#   --port N        HTTPS port (default 9001, or a random free one in 9002-9999; "random" picks one)
#   --direct        serve HTTPS from the agent itself with a self-signed cert on a free port
#                   (stable address, no port 80/443 or Cloudflare needed; works with Claude Code,
#                   not with the claude.ai connector which needs a public certificate)
#   --tunnel        use a Cloudflare quick tunnel instead (no open port; address changes on restart)
#   --email ADDR    e-mail for the Let's Encrypt account (optional)
#   --expire T      the key stops working after T (30m, 12h, 7d)
#   --new-key       make a new key even if one exists
#   --local         use the files next to this script instead of downloading (for development)
# Running it again repairs/updates the install and keeps the key.
# Fully automatic: it never asks anything (only sudo may ask for your password).
set -euo pipefail

RAW="${FATLLM_RAW:-https://raw.githubusercontent.com/SaeedCrazy/FATLLM-FULL-ACCESS-TO-LLM-/HEAD}"
APP=/opt/fatllm
CONF_DIR=/etc/fatllm
CONF=$CONF_DIR/fatllm.env
STATE=/var/lib/fatllm
LOGS=/var/log/fatllm

# Plain output until server/ui.sh (colours, logo, spinners) is loaded below.
say() { printf '> %s\n' "$*"; }
ok() { printf '+ %s\n' "$*"; }
note() { printf '  %s\n' "$*"; }
warn() { printf '! %s\n' "$*" >&2; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
ui_wait() { sleep "$2"; }
ui_box() { printf '%s\n' "$2"; }

# Not root? Run again through sudo, automatically.
if [ "$(id -u)" != 0 ]; then
  command -v sudo >/dev/null 2>&1 || die "run as root: type 'su -', then paste the command again"
  self="${BASH_SOURCE[0]:-}"
  if [ -n "$self" ] && [ -f "$self" ]; then exec sudo bash "$self" "$@"; fi
  dl="$(mktemp -d)"
  curl -fsSL --retry 3 "$RAW/install.sh" -o "$dl/install.sh" || die "download failed: $RAW/install.sh"
  exec sudo bash "$dl/install.sh" "$@"
fi

# ------------------------------------------------------------------ options
DOMAIN="" PORT="" FORCE_TUNNEL=0 FORCE_DIRECT=0 EMAIL="" EXPIRE="" NEW_KEY=0 UPDATE=0 LOCAL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:-}"; shift 2 ;;
    --port) PORT="${2:-}"; shift 2 ;;
    --tunnel) FORCE_TUNNEL=1; shift ;;
    --direct | --selfsigned) FORCE_DIRECT=1; shift ;;
    --email) EMAIL="${2:-}"; shift 2 ;;
    --expire) EXPIRE="${2:-}"; shift 2 ;;
    --new-key) NEW_KEY=1; shift ;;
    --update) UPDATE=1; shift ;;
    --local) LOCAL=1; shift ;;
    -h | --help) sed -n '2,19p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done
[ -z "$PORT" ] || [ "$PORT" = random ] || { [[ "$PORT" =~ ^[0-9]+$ ]] && [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ]; } \
  || die "--port needs a number (1-65535) or 'random'"

# ------------------------------------------------------------------ look
SRC=""
if [ "$LOCAL" = 1 ]; then
  SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  [ -f "$SRC/server/agent.py" ] || die "--local: no server/agent.py next to install.sh"
fi
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fetch() {  # repo path -> local file
  if [ -n "$SRC" ]; then cp "$SRC/$1" "$2"
  else curl -fsSL --retry 3 --max-time 60 "$RAW/$1" -o "$2" || die "download failed: $RAW/$1"; fi
}
if fetch server/ui.sh "$TMP/ui.sh" 2>/dev/null && bash -n "$TMP/ui.sh" 2>/dev/null; then
  # shellcheck source=server/ui.sh
  . "$TMP/ui.sh"
  ui_init
  ui_logo "installer · full server access for AI agents"
fi

# ------------------------------------------------------------------ checks
command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] || die "this server has no systemd; FATLLM needs it"

case "$(uname -m)" in
  x86_64 | amd64) ARCH=amd64 ;;
  aarch64 | arm64) ARCH=arm64 ;;
  armv7* | armv6* | armhf) ARCH=arm ;;
  i386 | i686) ARCH=386 ;;
  *) die "unsupported CPU: $(uname -m)" ;;
esac

pkg_install() {
  if command -v apt-get >/dev/null 2>&1; then
    apt-get update -qq >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null
  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q "$@" >/dev/null
  elif command -v yum >/dev/null 2>&1; then yum install -y -q "$@" >/dev/null
  elif command -v apk >/dev/null 2>&1; then apk add -q "$@" >/dev/null
  elif command -v zypper >/dev/null 2>&1; then zypper -q install -y "$@" >/dev/null
  else return 1; fi
}

NEED=""
command -v curl >/dev/null 2>&1 || NEED="$NEED curl"
command -v python3 >/dev/null 2>&1 || NEED="$NEED python3"
command -v tar >/dev/null 2>&1 || NEED="$NEED tar"
os_pretty="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | tr -d '"')"
ok "System: ${os_pretty:-Linux} · $ARCH · $(hostname 2>/dev/null || echo server)"
if [ -n "$NEED" ]; then
  say "Installing:$NEED"
  # shellcheck disable=SC2086
  pkg_install $NEED || die "could not install:$NEED (install them, then run this again)"
  ok "Installed:$NEED"
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 6))' || die "python3 is older than 3.6"
PY="$(command -v python3)"

# ------------------------------------------------------------------ files
mkdir -p "$APP" "$CONF_DIR" "$STATE/jobs" "$LOGS"
chmod 700 "$CONF_DIR" "$STATE" "$LOGS"
fetch server/agent.py "$TMP/agent.py"
fetch server/fatllm "$TMP/fatllm"
fetch srv "$TMP/srv"
fetch server/panel.html "$TMP/panel.html"
[ -f "$TMP/ui.sh" ] || fetch server/ui.sh "$TMP/ui.sh"
"$PY" -m py_compile "$TMP/agent.py" || die "downloaded agent is broken; try again"
bash -n "$TMP/fatllm" || die "downloaded fatllm script is broken; try again"
install -m 0755 "$TMP/agent.py" "$APP/agent.py"
install -m 0644 "$TMP/srv" "$APP/srv"
install -m 0644 "$TMP/panel.html" "$APP/panel.html"
install -m 0644 "$TMP/ui.sh" "$APP/ui.sh"
install -m 0755 "$TMP/fatllm" /usr/local/bin/fatllm
ok "Files installed ($APP, command: fatllm)"

# ------------------------------------------------------------------ old settings
OLD_TOKEN="" OLD_MODE="" OLD_HOST="" OLD_PORT="" OLD_LOCAL="" OLD_EXPIRES="0" OLD_EMAIL=""
if [ -f "$CONF" ]; then
  OLD_TOKEN="$(sed -n 's/^FATLLM_TOKEN=//p' "$CONF")"
  OLD_MODE="$(sed -n 's/^FATLLM_MODE=//p' "$CONF")"
  OLD_HOST="$(sed -n 's/^FATLLM_HOST=//p' "$CONF")"
  OLD_PORT="$(sed -n 's/^FATLLM_PORT=//p' "$CONF")"
  OLD_LOCAL="$(sed -n 's/^FATLLM_LOCAL_PORT=//p' "$CONF")"
  OLD_EXPIRES="$(sed -n 's/^FATLLM_EXPIRES=//p' "$CONF")"
  OLD_EMAIL="$(sed -n 's/^FATLLM_EMAIL=//p' "$CONF")"
fi
# Stop our own services first so their ports count as free below.
systemctl stop fatllm fatllm-caddy fatllm-tunnel >/dev/null 2>&1 || true

port_free() {  # exit 0 when nothing listens on TCP port $1
  "$PY" - "$1" <<'PY'
import errno, socket, sys
port = int(sys.argv[1])
for fam, addr in ((socket.AF_INET, "0.0.0.0"), (socket.AF_INET6, "::")):
    try:
        s = socket.socket(fam, socket.SOCK_STREAM)
    except OSError:
        continue
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if fam == socket.AF_INET6:
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
        s.bind((addr, port))
    except OSError as e:
        if e.errno == errno.EADDRINUSE:
            sys.exit(1)
    finally:
        s.close()
PY
}

pick_port() {  # 9001, else a random free port in 9002-9999 (away from 80/443 and common panels)
  local p
  if port_free 9001; then echo 9001; return 0; fi
  for _ in $(seq 1 80); do
    p=$(( 9002 + RANDOM % 998 ))
    port_free "$p" && { echo "$p"; return 0; }
  done
  return 1
}

TOKEN="$OLD_TOKEN"
if [ "$NEW_KEY" = 1 ] || [ "${#TOKEN}" -lt 32 ]; then
  TOKEN="$("$PY" -c 'import secrets; print("fat_" + secrets.token_urlsafe(32))')"
  ok "Key: made a new 256-bit key"
else
  ok "Key: kept your existing key"
fi

LOCAL_PORT="$OLD_LOCAL"
if [ -z "$LOCAL_PORT" ] || ! port_free "$LOCAL_PORT"; then
  for _ in $(seq 1 50); do
    LOCAL_PORT=$(( 20000 + RANDOM % 40000 ))
    port_free "$LOCAL_PORT" && break
  done
fi

EXPIRES="${OLD_EXPIRES:-0}"
if [ -n "$EXPIRE" ]; then
  case "$EXPIRE" in
    never | 0) EXPIRES=0 ;;
    *[0-9]m) EXPIRES=$(( $(date +%s) + ${EXPIRE%m} * 60 )) ;;
    *[0-9]h) EXPIRES=$(( $(date +%s) + ${EXPIRE%h} * 3600 )) ;;
    *[0-9]d) EXPIRES=$(( $(date +%s) + ${EXPIRE%d} * 86400 )) ;;
    *) die "--expire takes 30m, 12h, 7d or never" ;;
  esac
fi
[ -n "$EMAIL" ] || EMAIL="$OLD_EMAIL"

# Extra conf fields, set by setup_direct (empty for caddy/tunnel = agent on 127.0.0.1, no TLS).
CONF_LISTEN="" CONF_TLS_CERT="" CONF_TLS_KEY=""

write_conf() {  # mode host port public_url
  umask 077
  cat > "$CONF" <<EOF
# FATLLM settings (root only). The token is the secret part of the key.
FATLLM_TOKEN=$TOKEN
FATLLM_MODE=$1
FATLLM_HOST=$2
FATLLM_PORT=$3
FATLLM_PUBLIC_URL=$4
FATLLM_LOCAL_PORT=$LOCAL_PORT
FATLLM_LISTEN=$CONF_LISTEN
FATLLM_TLS_CERT=$CONF_TLS_CERT
FATLLM_TLS_KEY=$CONF_TLS_KEY
FATLLM_EXPIRES=$EXPIRES
FATLLM_EMAIL=$EMAIL
FATLLM_STATE_DIR=$STATE
FATLLM_LOG_DIR=$LOGS
FATLLM_RELAY_DIR=$CONF_DIR/relays
FATLLM_CLIENT=$APP/srv
EOF
  chmod 600 "$CONF"
  umask 022
}

# ------------------------------------------------------------------ agent
write_conf "${OLD_MODE:-caddy}" "$OLD_HOST" "$OLD_PORT" ""
cat > /etc/systemd/system/fatllm.service <<EOF
[Unit]
Description=FATLLM agent (shell access for an AI assistant)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
EnvironmentFile=$CONF
ExecStart=$PY $APP/agent.py
Restart=always
RestartSec=2
# Restarting the agent must not kill the commands it started.
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable fatllm >/dev/null 2>&1 || die "systemd refused the fatllm service"
systemctl restart fatllm || die "could not start the fatllm service"
local_ok=0
for _ in $(seq 1 20); do
  if curl -fsS --noproxy '*' --max-time 3 "http://127.0.0.1:$LOCAL_PORT/$TOKEN/info" >/dev/null 2>&1; then local_ok=1; break; fi
  sleep 0.5
done
[ "$local_ok" = 1 ] || { journalctl -u fatllm -n 20 --no-pager >&2 || true; die "the agent did not start"; }
ok "Agent running"

# ------------------------------------------------------------------ public HTTPS
open_port() {
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow "$1/tcp" >/dev/null 2>&1 && ok "Firewall (ufw): port $1 open" || true
  fi
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd -q --permanent --add-port="$1/tcp" >/dev/null 2>&1 && firewall-cmd -q --reload >/dev/null 2>&1 \
      && ok "Firewall (firewalld): port $1 open" || true
  fi
}

public_ip() {
  local ip u
  for u in https://api.ipify.org https://ipv4.icanhazip.com https://checkip.amazonaws.com https://ifconfig.me/ip; do
    ip="$(curl -4 -fsS --max-time 6 "$u" 2>/dev/null | tr -d ' \r\n' || true)"
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && { echo "$ip"; return 0; }
  done
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}')"
  [ -n "$ip" ] && echo "$ip"
}

is_private() {
  [[ "$1" =~ ^(10\.|127\.|192\.168\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[01])\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.) ]]
}

get_caddy() {
  if [ -x "$APP/caddy" ] && "$APP/caddy" version >/dev/null 2>&1; then return 0; fi
  if command -v caddy >/dev/null 2>&1; then ln -sf "$(command -v caddy)" "$APP/caddy"; return 0; fi
  say "Downloading Caddy (HTTPS)"
  local carch="$ARCH" extra="" ver
  [ "$ARCH" = arm ] && { carch=arm; extra="&arm=7"; }
  if curl -fsSL --max-time 180 "https://caddyserver.com/api/download?os=linux&arch=$carch$extra" -o "$TMP/caddy" \
      && chmod +x "$TMP/caddy" && "$TMP/caddy" version >/dev/null 2>&1; then
    install -m 0755 "$TMP/caddy" "$APP/caddy"; return 0
  fi
  ver="$(curl -fsSI --max-time 20 https://github.com/caddyserver/caddy/releases/latest 2>/dev/null \
    | tr -d '\r' | awk 'tolower($1)=="location:"{print $2}' | sed 's#.*/tag/v##')"
  [ -n "$ver" ] || return 1
  [ "$ARCH" = arm ] && carch=armv7
  curl -fsSL --max-time 180 "https://github.com/caddyserver/caddy/releases/download/v$ver/caddy_${ver}_linux_${carch}.tar.gz" \
    -o "$TMP/caddy.tgz" || return 1
  tar -xzf "$TMP/caddy.tgz" -C "$TMP" caddy || return 1
  install -m 0755 "$TMP/caddy" "$APP/caddy"
}

get_cloudflared() {
  if [ -x "$APP/cloudflared" ] && "$APP/cloudflared" --version >/dev/null 2>&1; then return 0; fi
  say "Downloading cloudflared (tunnel)"
  curl -fsSL --max-time 180 "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$ARCH" \
    -o "$TMP/cloudflared" || return 1
  chmod +x "$TMP/cloudflared"
  "$TMP/cloudflared" --version >/dev/null 2>&1 || return 1
  install -m 0755 "$TMP/cloudflared" "$APP/cloudflared"
}

setup_caddy() {  # 0 = working HTTPS with a real certificate
  local ip host port http_ok=0 url
  if [ -n "$DOMAIN" ]; then
    host="$DOMAIN"
  elif [ -n "$OLD_HOST" ] && [ "$OLD_MODE" = caddy ] && [ "${OLD_HOST%.sslip.io}" = "$OLD_HOST" ]; then
    host="$OLD_HOST"  # the user's own domain from an earlier --domain
  else
    ip="$(public_ip || true)"
    [ -n "$ip" ] || { warn "could not find this server's public IP"; return 1; }
    is_private "$ip" && { warn "this server has no public IPv4 ($ip)"; return 1; }
    host="${ip//./-}.sslip.io"
  fi
  port_free 80 && http_ok=1
  if [ "$PORT" = random ]; then
    port="$(pick_port)" || { warn "no free port"; return 1; }
  elif [ -n "$PORT" ]; then
    port="$PORT"
    port_free "$port" || { warn "port $port is already in use"; return 1; }
  elif [ -n "$OLD_PORT" ] && [ "$OLD_MODE" = caddy ] && port_free "$OLD_PORT"; then
    port="$OLD_PORT"
  else
    port="$(pick_port)" || { warn "no free port"; return 1; }
  fi
  if [ "$port" != 443 ] && [ "$http_ok" = 0 ]; then
    warn "port 80 is busy, so no public certificate can be issued here"
    return 1
  fi
  get_caddy || { warn "could not download Caddy"; return 1; }

  url="https://$host"; [ "$port" = 443 ] || url="$url:$port"
  say "Trying a real certificate for $url"
  {
    echo "{"
    echo "	admin off"
    echo "	auto_https disable_redirects"
    [ "$http_ok" = 1 ] && echo "	http_port 80"
    [ -n "$EMAIL" ] && echo "	email $EMAIL"
    echo "}"
    echo
    echo "$url {"
    echo "	reverse_proxy 127.0.0.1:$LOCAL_PORT {"
    echo "		flush_interval -1"
    echo "	}"
    echo "}"
  } > "$CONF_DIR/Caddyfile"
  "$APP/caddy" validate --config "$CONF_DIR/Caddyfile" --adapter caddyfile >/dev/null 2>&1 \
    || { warn "Caddy rejected its config"; return 1; }
  cat > /etc/systemd/system/fatllm-caddy.service <<EOF
[Unit]
Description=FATLLM HTTPS (Caddy)
After=network-online.target fatllm.service
Wants=network-online.target

[Service]
Environment=HOME=$STATE XDG_DATA_HOME=$STATE XDG_CONFIG_HOME=$STATE
ExecStart=$APP/caddy run --config $CONF_DIR/Caddyfile --adapter caddyfile
Restart=always
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
  open_port "$port"
  [ "$http_ok" = 1 ] && open_port 80
  systemctl disable --now fatllm-tunnel >/dev/null 2>&1 || true
  systemctl daemon-reload
  systemctl enable fatllm-caddy >/dev/null 2>&1
  systemctl restart fatllm-caddy
  local i
  for i in $(seq 1 60); do
    if curl -fsS --noproxy '*' --max-time 5 --resolve "$host:$port:127.0.0.1" "$url/$TOKEN/info" >/dev/null 2>&1; then
      write_conf caddy "$host" "$port" "$url"
      systemctl restart fatllm
      ok "HTTPS with a real certificate: $url"
      return 0
    fi
    ui_wait "Getting a certificate ($(( i * 3 ))s, up to 3 min)" 3
  done
  warn "No certificate after 3 minutes. Caddy's last words:"
  journalctl -u fatllm-caddy -n 3 --no-pager -o cat 2>/dev/null | cut -c1-$(( ${UI_COLS:-80} - 6 )) \
    | while IFS= read -r l; do note "$l"; done || true
  systemctl disable --now fatllm-caddy >/dev/null 2>&1 || true
  return 1
}

setup_direct() {  # 0 = the agent serves HTTPS itself with a self-signed cert on a stable port
  local ip host pub url crt key san cfg want p
  ip="$(public_ip || true)"
  # The key uses the bare IP unless the owner has a real domain: a self-signed certificate gains
  # nothing from a DNS name, and *.sslip.io is filtered in some countries (Iran), which made the
  # panel unreachable from phones there.
  if [ -n "$DOMAIN" ]; then host="$DOMAIN"
  elif [ -n "$OLD_HOST" ] && [ "$OLD_MODE" = direct ] && [ "${OLD_HOST%.sslip.io}" = "$OLD_HOST" ] \
      && ! [[ "$OLD_HOST" =~ ^[0-9.]+$ ]]; then host="$OLD_HOST"
  elif [ -n "$ip" ] && ! is_private "$ip"; then host="$ip"
  else warn "could not find this server's public IPv4"; return 1; fi
  command -v openssl >/dev/null 2>&1 || pkg_install openssl || { warn "openssl is needed for a self-signed certificate"; return 1; }

  # Port: --port if free; else the previous one (keys stay the same); else 9001 or a random
  # free one in 9002-9999, away from 80/443 so it never clashes with x-ui, nginx or Caddy.
  pub=""
  if [ -n "$PORT" ] && [ "$PORT" != random ]; then
    if port_free "$PORT"; then pub="$PORT"; else warn "port $PORT is already in use; choosing another"; fi
  elif [ -z "$PORT" ] && [ -n "$OLD_PORT" ] && [ "$OLD_MODE" = direct ] && port_free "$OLD_PORT"; then
    pub="$OLD_PORT"
  fi
  [ -n "$pub" ] || pub="$(pick_port)" || { warn "no free port"; return 1; }

  mkdir -p "$STATE/tls"; chmod 700 "$STATE/tls"
  crt="$STATE/tls/cert.pem"; key="$STATE/tls/key.pem"
  if [[ "$host" =~ ^[0-9.]+$ ]]; then san="IP:$host"; want="IP Address:$host"
  else san="DNS:$host"; [ -n "$ip" ] && san="$san,IP:$ip"; want="DNS:$host"; fi
  # Keep a certificate that still covers the address, so clients that pinned it keep working.
  if [ -s "$crt" ] && [ -s "$key" ] && openssl x509 -in "$crt" -noout -checkend 2592000 >/dev/null 2>&1 \
      && openssl x509 -in "$crt" -noout -text 2>/dev/null | grep -qF "$want"; then
    ok "Certificate: kept the existing one for $host"
  else
    cfg="$TMP/openssl.cnf"
    cat > "$cfg" <<CFG
[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = $host
[v3]
subjectAltName = $san
CFG
    openssl req -x509 -newkey rsa:2048 -nodes -keyout "$key" -out "$crt" -days 3650 -config "$cfg" >/dev/null 2>&1 \
      || { warn "openssl could not make a certificate"; return 1; }
    ok "Certificate: self-signed for $host (valid 10 years)"
  fi
  chmod 600 "$key" "$crt"

  url="https://$host"; [ "$pub" = 443 ] || url="$url:$pub"
  systemctl disable --now fatllm-caddy fatllm-tunnel >/dev/null 2>&1 || true
  LOCAL_PORT="$pub"; CONF_LISTEN="0.0.0.0"; CONF_TLS_CERT="$crt"; CONF_TLS_KEY="$key"
  write_conf direct "$host" "$pub" "$url"
  open_port "$pub"
  systemctl restart fatllm
  local up=0
  for _ in $(seq 1 20); do
    if curl -fsS --insecure --noproxy '*' --max-time 5 "https://127.0.0.1:$pub/$TOKEN/info" >/dev/null 2>&1; then up=1; break; fi
    ui_wait "Starting HTTPS on port $pub" 1
  done
  [ "$up" = 1 ] && { ok "HTTPS (self-signed) on $url"; return 0; }
  journalctl -u fatllm -n 15 --no-pager >&2 2>/dev/null || true
  warn "the agent did not come up with TLS"
  return 1
}

setup_tunnel() {
  get_cloudflared || { warn "could not download cloudflared"; return 1; }
  cat > /etc/systemd/system/fatllm-tunnel.service <<EOF
[Unit]
Description=FATLLM tunnel (Cloudflare quick tunnel)
After=network-online.target fatllm.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
ExecStart=/bin/sh -c 'exec $APP/cloudflared tunnel --no-autoupdate --retries 5 --url http://127.0.0.1:$LOCAL_PORT > $STATE/tunnel.log 2>&1'
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
  systemctl disable --now fatllm-caddy >/dev/null 2>&1 || true
  systemctl daemon-reload
  systemctl enable fatllm-tunnel >/dev/null 2>&1
  systemctl restart fatllm-tunnel
  local url=""
  for _ in $(seq 1 45); do
    url="$(grep -aoE 'https://[a-z0-9]+(-[a-z0-9]+)+\.trycloudflare\.com' "$STATE/tunnel.log" 2>/dev/null | tail -1 || true)"
    [ -n "$url" ] && break
    ui_wait "Opening a Cloudflare tunnel" 1
  done
  [ -n "$url" ] || { warn "the tunnel gave no address"; tail -n 8 "$STATE/tunnel.log" >&2 2>/dev/null || true; return 1; }
  write_conf tunnel "" "" ""
  systemctl restart fatllm
  for _ in $(seq 1 30); do
    curl -fsS --max-time 8 "$url/$TOKEN/info" >/dev/null 2>&1 && { ok "Tunnel: $url"; return 0; }
    ui_wait "Waiting for the tunnel to answer" 2
  done
  warn "tunnel address $url does not answer from here yet; it may need a minute"
  return 0
}

if [ "$FORCE_TUNNEL" = 1 ] || { [ "$OLD_MODE" = tunnel ] && [ "$UPDATE" = 1 ] && [ "$FORCE_DIRECT" = 0 ] && [ -z "$DOMAIN$PORT" ]; }; then
  setup_tunnel || die "the tunnel did not start. Check internet access to Cloudflare, or try --direct."
elif [ "$FORCE_DIRECT" = 1 ] || { [ "$OLD_MODE" = direct ] && [ "$UPDATE" = 1 ] && [ "$FORCE_TUNNEL" = 0 ]; }; then
  setup_direct || die "direct HTTPS did not work (see above). Try --domain with a real DNS name, or --tunnel."
elif ! setup_caddy; then
  if [ -n "$DOMAIN" ]; then
    die "HTTPS for --domain $DOMAIN did not work (see above). Fix its DNS/ports, or use --direct or --tunnel."
  fi
  say "Using direct HTTPS instead (self-signed, stable address)"
  setup_direct || {
    warn "Direct HTTPS failed too; trying a Cloudflare tunnel."
    setup_tunnel || die "nothing worked. Free port 80 or 443, or pass --domain, then run this again."
  }
fi

echo
ui_box "$G" "FATLLM is ready"
fatllm key
port_now="$(sed -n 's/^FATLLM_PORT=//p' "$CONF")"
if [ -n "$port_now" ]; then
  note "Can't connect? Open TCP port $port_now in your provider's firewall panel."
  echo
fi
