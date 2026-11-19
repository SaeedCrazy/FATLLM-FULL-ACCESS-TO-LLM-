#!/usr/bin/env bash
# FATLLM installer: one command on a server you can reach from the internet (the "hub"); it gives
# you a key that lets an AI agent work on it, and on every other server you connect to it, as root.
#
#   bash <(curl -Ls https://raw.githubusercontent.com/SaeedCrazy/FATLLM-FULL-ACCESS-TO-LLM-/HEAD/install.sh)
#
# Other servers (e.g. one in Iran that can't be reached from outside) are not installed with this:
# run `fatllm add-server NAME` on the hub and paste the line it prints on the other server.
#
# Options (after the command: bash <(curl ...) --port 9123):
#   --port N|random   HTTPS port (default 9001, else a random free one in 9002-9999; never 80/443)
#   --cert            also get a public certificate (Let's Encrypt via Caddy) so web connectors
#                     (claude.ai, ChatGPT) accept it; needs port 80 free. Default: self-signed + IP.
#   --domain NAME     public certificate for your own domain (its DNS must point here); implies --cert
#   --email ADDR      e-mail for the Let's Encrypt account (optional)
#   --expire T        the main key stops working after T (30m, 12h, 7d)
#   --new-key         make a new main key even if one exists
#   --local           use the files next to this script instead of downloading (development)
# Running it again repairs/updates the install and keeps the keys, the port and the servers.
# Fully automatic: it never asks anything (only sudo may ask for your password).
set -euo pipefail

RAW="${FATLLM_RAW:-https://raw.githubusercontent.com/SaeedCrazy/FATLLM-FULL-ACCESS-TO-LLM-/HEAD}"
APP=/opt/fatllm
CONF_DIR=/etc/fatllm
CONF=$CONF_DIR/fatllm.env
KEYS=$CONF_DIR/keys.json
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
G=""

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
PORT="" WANT_CERT=0 DOMAIN="" EMAIL="" EXPIRE="" NEW_KEY=0 LOCAL=0
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="${2:-}"; shift 2 ;;
    --cert) WANT_CERT=1; shift ;;
    --no-cert | --direct) WANT_CERT=0; NO_CERT=1; shift ;;
    --domain) DOMAIN="${2:-}"; WANT_CERT=1; shift 2 ;;
    --email) EMAIL="${2:-}"; shift 2 ;;
    --expire) EXPIRE="${2:-}"; shift 2 ;;
    --new-key) NEW_KEY=1; shift ;;
    --update) shift ;;            # re-running is always an update
    --tunnel) die "the Cloudflare tunnel mode was removed (it was unstable). A server that can't be reached from outside joins a hub instead: run 'fatllm add-server NAME' on the hub." ;;
    --local) LOCAL=1; shift ;;
    -h | --help) sed -n '2,22p' "$0" 2>/dev/null | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "unknown option: $1 (see --help)" ;;
  esac
done
NO_CERT="${NO_CERT:-0}"
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
  else curl -fsSL --retry 3 --max-time 90 "$RAW/$1" -o "$2" || die "download failed: $RAW/$1"; fi
}
if fetch server/ui.sh "$TMP/ui.sh" 2>/dev/null && bash -n "$TMP/ui.sh" 2>/dev/null; then
  # shellcheck source=server/ui.sh
  . "$TMP/ui.sh"
  ui_init
  ui_logo "installer · server access for AI agents"
fi

# ------------------------------------------------------------------ checks
command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] || die "this server has no systemd; FATLLM needs it"
if [ -f "$CONF" ] && grep -q '^FATLLM_ROLE=node' "$CONF"; then
  hub="$(sed -n 's/^FATLLM_HUB=//p' "$CONF")"
  die "this server is connected to the hub $hub. To make it a hub instead: run 'fatllm leave' here, then this again."
fi
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
command -v openssl >/dev/null 2>&1 || NEED="$NEED openssl"
command -v tar >/dev/null 2>&1 || NEED="$NEED tar"
os_pretty="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | tr -d '"' || true)"
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
install -m 0755 "$TMP/fatllm" "$APP/fatllm"          # served to servers that join
install -m 0755 "$TMP/fatllm" /usr/local/bin/fatllm
rm -f "$APP/cloudflared"
ok "Files in $APP, command: fatllm"

# ------------------------------------------------------------------ previous install
# (a fresh server has no settings file: that must give "", not stop the script under set -e)
old() { [ -f "$CONF" ] || return 0; sed -n "s/^$1=//p" "$CONF" | tail -n 1; }
OLD_MODE="$(old FATLLM_MODE)" OLD_HOST="$(old FATLLM_HOST)" OLD_PORT="$(old FATLLM_PORT)"
OLD_LOCAL="$(old FATLLM_LOCAL_PORT)" OLD_URL="$(old FATLLM_PUBLIC_URL)" OLD_EMAIL="$(old FATLLM_EMAIL)"
OLD_TOKEN="$(old FATLLM_TOKEN)" OLD_EXPIRES="$(old FATLLM_EXPIRES)" HUB_NAME="$(old FATLLM_NAME)"
case "$OLD_MODE" in caddy) OLD_MODE=cert ;; tunnel) OLD_MODE=""; OLD_PORT=""; OLD_URL="" ;; esac
HUB_NAME="${HUB_NAME:-main}"
[ -n "$EMAIL" ] || EMAIL="$OLD_EMAIL"
if [ "$WANT_CERT" = 0 ] && [ "$NO_CERT" = 0 ] && [ "$OLD_MODE" = cert ]; then WANT_CERT=1; fi
# a version 1 install kept its relays: they are replaced by joined servers
for u in /etc/systemd/system/fatllm-relay-*.service /etc/systemd/system/fatllm-tunnel.service; do
  [ -e "$u" ] || continue
  systemctl disable --now "$(basename "$u" .service)" >/dev/null 2>&1 || true
  rm -f "$u"
done

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

ours_on() {  # is our own service the one listening on port $1?
  systemctl is-active --quiet fatllm 2>/dev/null || systemctl is-active --quiet fatllm-caddy 2>/dev/null || return 1
  [ "$1" = "$OLD_PORT" ]
}

pick_port() {  # 9001, else a random free port in 9002-9999 (away from 80/443 and common panels)
  local p
  if port_free 9001 || ours_on 9001; then echo 9001; return 0; fi
  for _ in $(seq 1 80); do
    p=$(( 9002 + RANDOM % 998 ))
    port_free "$p" && { echo "$p"; return 0; }
  done
  return 1
}

public_ip() {
  local ip u
  for u in https://api.ipify.org https://ipv4.icanhazip.com https://checkip.amazonaws.com https://ifconfig.me/ip; do
    ip="$(curl -4 -fsS --max-time 6 "$u" 2>/dev/null | tr -d ' \r\n' || true)"
    [[ "$ip" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && { echo "$ip"; return 0; }
  done
  ip="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit }}' || true)"
  [ -n "$ip" ] || return 1
  echo "$ip"
}

is_private() {
  [[ "$1" =~ ^(10\.|127\.|192\.168\.|169\.254\.|172\.(1[6-9]|2[0-9]|3[01])\.|100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.) ]]
}

# ------------------------------------------------------------------ keys
EXPIRE_ARGS=()
[ -n "$EXPIRE" ] && EXPIRE_ARGS=(expire "$EXPIRE" main)
# keys.json (a version 1 key is carried over as "main")
HAD_KEYS=0; [ -s "$KEYS" ] && HAD_KEYS=1
FATLLM_KEYS="$KEYS" FATLLM_TOKEN="$OLD_TOKEN" FATLLM_EXPIRES="${OLD_EXPIRES:-0}" "$PY" "$APP/agent.py" admin token >/dev/null \
  || die "could not create the keys file"
if [ "$NEW_KEY" = 1 ]; then
  FATLLM_KEYS="$KEYS" "$PY" "$APP/agent.py" admin rotate >/dev/null || die "could not make a new key"
  ok "New main key (the old one stopped)"
elif [ "$HAD_KEYS" = 1 ]; then
  ok "Keys kept (your key still works)"
elif [ -n "$OLD_TOKEN" ]; then
  ok "Your version 1 key is kept"
else
  ok "New key made"
fi
if [ "${#EXPIRE_ARGS[@]}" -gt 0 ]; then
  FATLLM_KEYS="$KEYS" "$PY" "$APP/agent.py" admin "${EXPIRE_ARGS[@]}" >/dev/null || die "--expire takes 30m, 12h, 7d or never"
fi

# ------------------------------------------------------------------ port and address
IP="$(public_ip || true)"
if [ -z "$IP" ] || is_private "$IP"; then
  [[ "$OLD_HOST" =~ ^[0-9.]+$ ]] && IP="$OLD_HOST"
fi
if [ "$PORT" = random ]; then
  PUB_PORT="$(pick_port)" || die "no free port"
elif [ -n "$PORT" ]; then
  PUB_PORT="$PORT"
  port_free "$PUB_PORT" || ours_on "$PUB_PORT" || die "port $PUB_PORT is already in use; pick another or use --port random"
elif [ -n "$OLD_PORT" ] && { port_free "$OLD_PORT" || ours_on "$OLD_PORT"; }; then
  PUB_PORT="$OLD_PORT"
else
  PUB_PORT="$(pick_port)" || die "no free port"
fi

# ------------------------------------------------------------------ units
write_conf() {  # mode host public_url listen local_port cert key
  umask 077
  cat > "$CONF" <<EOF
# FATLLM hub settings (root only). The keys are in $KEYS.
FATLLM_ROLE=hub
FATLLM_NAME=$HUB_NAME
FATLLM_MODE=$1
FATLLM_HOST=$2
FATLLM_PORT=$PUB_PORT
FATLLM_PUBLIC_URL=$3
FATLLM_LISTEN=$4
FATLLM_LOCAL_PORT=$5
FATLLM_TLS_CERT=$6
FATLLM_TLS_KEY=$7
FATLLM_EMAIL=$EMAIL
FATLLM_KEYS=$KEYS
FATLLM_STATE_DIR=$STATE
FATLLM_LOG_DIR=$LOGS
EOF
  chmod 600 "$CONF"
  umask 022
}

agent_unit() {
  cat > /etc/systemd/system/fatllm.service <<EOF
[Unit]
Description=FATLLM hub (shell access for AI agents)
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
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
}

open_port() {
  if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    ufw allow "$1/tcp" >/dev/null 2>&1 && ok "Firewall (ufw): port $1 open" || true
  fi
  if command -v firewall-cmd >/dev/null 2>&1 && firewall-cmd --state >/dev/null 2>&1; then
    firewall-cmd -q --permanent --add-port="$1/tcp" >/dev/null 2>&1 && firewall-cmd -q --reload >/dev/null 2>&1 \
      && ok "Firewall (firewalld): port $1 open" || true
  fi
}

# Talk to the running agent the way `fatllm` does (root-only token, never on a command line).
# shellcheck disable=SC1090
agent_api() { ( set -a; . "$CONF"; set +a; "$PY" "$APP/agent.py" admin api "$@" ); }

check_local() {  # the new agent answers
  local i
  for i in $(seq 1 30); do
    if agent_api GET /info >/dev/null 2>&1; then return 0; fi
    ui_wait "Starting the agent ($i)" 1
  done
  return 1
}

# Tell connected servers about a new address/certificate before we move (they reconnect to it).
# Runs while the old agent is still up, with the old settings file.
rehome() {  # url [ca-pem-file]
  local ca="" body out
  [ -f "$CONF" ] && grep -q '^FATLLM_ROLE=hub' "$CONF" || return 0
  "$PY" -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("nodes") else 1)' "$KEYS" 2>/dev/null || return 0
  [ -n "${2:-}" ] && ca="$(cat "$2")"
  body="$("$PY" -c 'import json,sys; print(json.dumps({"url": sys.argv[1], "ca": sys.argv[2]}))' "$1" "$ca")"
  if out="$(agent_api POST /admin/servers/rehome "$body" 2>/dev/null)"; then
    ok "Connected servers moved to $1"
    printf '%s' "$out" | "$PY" -c 'import json,sys
for n, v in json.load(sys.stdin)["servers"].items():
    if v != "moved": print("%s: %s" % (n, v))' 2>/dev/null | while IFS= read -r l; do warn "not moved: $l (join it again: fatllm add-server)"; done || true
  else
    warn "Could not tell the connected servers about the new address; join them again: fatllm add-server NAME"
  fi
}

setup_direct() {  # the agent serves HTTPS itself with a long-lived self-signed cert; key = bare IP
  local host url crt key san want cfg
  [ -n "$IP" ] || die "could not find this server's public IPv4; is it online?"
  host="$IP"
  url="https://$host"; [ "$PUB_PORT" = 443 ] || url="$url:$PUB_PORT"
  mkdir -p "$STATE/tls"; chmod 700 "$STATE/tls"
  crt="$STATE/tls/cert.pem"; key="$STATE/tls/key.pem"
  local before=""
  [ -s "$crt" ] && before="$(openssl x509 -in "$crt" -noout -fingerprint -sha256 2>/dev/null || true)"
  san="IP:$host"; want="IP Address:$host"
  if [ -s "$crt" ] && [ -s "$key" ] && openssl x509 -in "$crt" -noout -checkend 2592000 >/dev/null 2>&1 \
      && openssl x509 -in "$crt" -noout -text 2>/dev/null | grep -qF "$want"; then
    ok "Kept the certificate for $host"
  else
    cfg="$TMP/openssl.cnf"
    printf '[req]\ndistinguished_name = dn\nx509_extensions = v3\nprompt = no\n[dn]\nCN = %s\n[v3]\nsubjectAltName = %s\n' \
      "$host" "$san" > "$cfg"
    openssl req -x509 -newkey rsa:2048 -nodes -keyout "$key" -out "$crt" -days 3650 -config "$cfg" >/dev/null 2>&1 \
      || die "openssl could not make a certificate"
    ok "Self-signed certificate for $host"
  fi
  chmod 600 "$key" "$crt"
  if [ "$url" != "$OLD_URL" ] || [ "$before" != "$(openssl x509 -in "$crt" -noout -fingerprint -sha256 2>/dev/null)" ]; then
    rehome "$url" "$crt"
  fi
  systemctl disable --now fatllm-caddy >/dev/null 2>&1 || true
  rm -f /etc/systemd/system/fatllm-caddy.service
  systemctl stop fatllm >/dev/null 2>&1 || true
  write_conf direct "$host" "$url" 0.0.0.0 "$PUB_PORT" "$crt" "$key"
  agent_unit
  systemctl daemon-reload
  systemctl enable fatllm >/dev/null 2>&1 || die "systemd refused the fatllm service"
  systemctl restart fatllm || die "could not start the fatllm service"
  open_port "$PUB_PORT"
  check_local || { journalctl -u fatllm -n 15 --no-pager >&2 || true; die "the agent did not start"; }
  ok "Serving on $url"
}

get_caddy() {
  if [ -x "$APP/caddy" ] && "$APP/caddy" version >/dev/null 2>&1; then return 0; fi
  if command -v caddy >/dev/null 2>&1; then ln -sf "$(command -v caddy)" "$APP/caddy"; return 0; fi
  say "Downloading Caddy (for the public certificate)"
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

setup_cert() {  # 0 = HTTPS with a public certificate in front of the agent
  local host url http_ok=0 local_port i code
  if [ -n "$DOMAIN" ]; then host="$DOMAIN"
  elif [ -n "$OLD_HOST" ] && [ "$OLD_MODE" = cert ] && [ "${OLD_HOST%.sslip.io}" = "$OLD_HOST" ] \
      && ! [[ "$OLD_HOST" =~ ^[0-9.]+$ ]]; then host="$OLD_HOST"
  elif [ -n "$IP" ] && ! is_private "$IP"; then host="${IP//./-}.sslip.io"
  else warn "could not find this server's public IPv4"; return 1; fi
  if port_free 80; then http_ok=1; fi
  if [ "$PUB_PORT" != 443 ] && [ "$http_ok" = 0 ]; then
    warn "Port 80 is busy: no public certificate"; return 1
  fi
  get_caddy || { warn "could not download Caddy"; return 1; }
  url="https://$host"; [ "$PUB_PORT" = 443 ] || url="$url:$PUB_PORT"
  [ "$url" = "$OLD_URL" ] || rehome "$url"
  local_port="$OLD_LOCAL"
  if [ -z "$local_port" ] || [ "$local_port" = "$PUB_PORT" ] || ! { port_free "$local_port" || [ "$OLD_MODE" = cert ]; }; then
    for _ in $(seq 1 50); do local_port=$(( 20000 + RANDOM % 40000 )); port_free "$local_port" && break; done
  fi
  {
    echo "{"
    echo "	admin off"
    echo "	auto_https disable_redirects"
    [ -n "$EMAIL" ] && echo "	email $EMAIL"
    echo "}"
    echo
    echo "$url {"
    echo "	reverse_proxy 127.0.0.1:$local_port {"
    echo "		flush_interval -1"
    echo "	}"
    echo "}"
  } > "$CONF_DIR/Caddyfile"
  "$APP/caddy" validate --config "$CONF_DIR/Caddyfile" --adapter caddyfile >/dev/null 2>&1 \
    || { warn "Caddy rejected its config"; return 1; }
  cat > /etc/systemd/system/fatllm-caddy.service <<EOF
[Unit]
Description=FATLLM public certificate (Caddy)
After=network-online.target fatllm.service
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Environment=HOME=$STATE XDG_DATA_HOME=$STATE XDG_CONFIG_HOME=$STATE
ExecStart=$APP/caddy run --config $CONF_DIR/Caddyfile --adapter caddyfile
Restart=always
RestartSec=3
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
EOF
  systemctl stop fatllm fatllm-caddy >/dev/null 2>&1 || true
  write_conf cert "$host" "$url" 127.0.0.1 "$local_port" "" ""
  agent_unit
  systemctl daemon-reload
  systemctl enable fatllm fatllm-caddy >/dev/null 2>&1
  systemctl restart fatllm
  check_local || { warn "the agent did not start"; return 1; }
  open_port "$PUB_PORT"; [ "$http_ok" = 1 ] && open_port 80
  systemctl restart fatllm-caddy
  for i in $(seq 1 60); do
    # any answer over a verified TLS connection proves the certificate (no key needed)
    code="$(curl -sS -o /dev/null -w '%{http_code}' --noproxy '*' --max-time 5 \
      --resolve "$host:$PUB_PORT:127.0.0.1" "$url/" 2>/dev/null || true)"
    if [ -n "$code" ] && [ "$code" != 000 ]; then
      ok "HTTPS with a public certificate: $url"
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

if [ "$WANT_CERT" = 1 ]; then
  if ! setup_cert; then
    [ -n "$DOMAIN" ] && die "the certificate for $DOMAIN did not work (see above). Fix its DNS/port 80, or install without --domain."
    say "Using a self-signed certificate instead"
    setup_direct
  fi
else
  setup_direct
fi

# servers already connected get the new version too
if "$PY" -c 'import json,sys; sys.exit(0 if json.load(open(sys.argv[1])).get("nodes") else 1)' "$KEYS" 2>/dev/null; then
  ui_wait "Waiting for the connected servers" 3
  if out="$(agent_api POST /admin/servers/update 2>/dev/null)"; then
    printf '%s' "$out" | "$PY" -c 'import json,sys
for n, v in sorted(json.load(sys.stdin)["servers"].items()): print("%s\t%s" % (n, v))' 2>/dev/null \
      | while IFS=$'\t' read -r n v; do
          if [ "$v" = updated ]; then ok "Server $n updated"; else warn "Server $n: $v (it updates when you run this again)"; fi
        done || true
  fi
fi

echo
ui_box "$G" "FATLLM is ready"
fatllm key
port_now="$(sed -n 's/^FATLLM_PORT=//p' "$CONF")"
note "Not reachable? Open TCP port $port_now in"
note "your provider's firewall panel."
note "Connect another server (e.g. in Iran):"
note "  fatllm add-server iran"
echo
