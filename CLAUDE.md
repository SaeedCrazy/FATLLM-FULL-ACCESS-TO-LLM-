# FATLLM: full server access for Claude

This repo installs FATLLM on the owner's server (one command, prints a key) and lets Claude
run that server through the key.

The owner works from a phone: reply in **Persian**, short and plain. Never ask them to run
anything on their side except the install one-liner or a `fatllm ...` command on the server.

## When the user pastes a key (a line like `FATLLM key: https://<host>/fat_...`)
1. `./srv key '<the pasted line>'` saves the key in `~/.fatllm-key` (outside the repo) and prints
   the server's facts. The whole line, or the key with `/mcp` on the end, is fine.
   The SessionStart hook (`.claude/session-start.sh`) already connects when `FATLLM_KEY` is set
   in the environment or `~/.fatllm-key` exists; its note at the top of the session says so.
2. Read `./srv guide` once, then work:
   - `./srv 'command'` (bash as root; output and `[exit N]`; srv exits with the same code)
   - `./srv --via <relay> 'command'` routes that command's network through a relay (see below)
   - `./srv - <<'EOF' ... EOF` for scripts
   - `./srv job <id>` when a command was still running (it keeps running on the server)
   - `./srv get <remote> [local]`, `./srv put <local> <remote> [mode]`, `./srv info`
3. Work on your own. The owner granted root and does not want approval prompts for routine
   work. Ask first only before unrecoverable data loss, or before something that could lock
   them out (SSH, firewall, network config: keep their current SSH access working).
4. Never print the full key back, never write it into the repo, never commit it.

## Relays (reaching sites a server can't reach itself)
A relay sends a command's network out through another server. `srv info` and `./srv guide` list
the configured relays by name. Use `./srv --via <name> 'curl -s https://site'` when the target is
blocked from the FATLLM server itself:
- Iranian sites (Mixin, Iranian gateways) block foreign IPs → use the relay that exits in Iran.
- Sites blocked from inside Iran (Telegram, AI, GitHub) → run WITHOUT `via` if FATLLM is on a
  foreign server, or `via` the foreign relay if FATLLM is on the Iranian server.
The owner adds relays with `fatllm relay add --host <ip> --user root --name <name>` on the server
(it installs an SSH key, used once; the password is never stored). Only the owner does this — do
not ask for another server's root password in chat; point them to that command.

If the key is in the environment as `FATLLM_KEY`, srv uses it; no paste is needed.
If srv cannot reach the server: a 403 from the proxy means the cloud environment's network
policy blocks the host. Tell the user to add the host (`*.sslip.io`, their domain or
`*.trycloudflare.com`) under Allowed domains, or set Network access to Full. Otherwise ask them
to run `fatllm status` on the server and send the output.

## Reachability modes (and the Cloudflare 1033 / changing-address problem)
Three ways the key's address is served, chosen at install and switchable with `fatllm mode <m>`:
- `caddy`: Caddy gets a real Let's Encrypt cert for `<ip>.sslip.io` (or `--domain`) on the FATLLM
  port. Needs port 80 free (or the port to be 443). Works with the claude.ai connector. Stable address.
- `direct`: the agent serves HTTPS itself with a long-lived self-signed cert on a stable port
  (agent env `FATLLM_TLS_CERT`/`FATLLM_TLS_KEY`, binds `0.0.0.0`). No port 80/443 or Cloudflare
  needed; works from Iran. The key uses the bare server IP (`https://<ip>[:port]/fat_...`), not
  `*.sslip.io`, because sslip.io is filtered in Iran; default port 9001.
  Re-running keeps the certificate while it still covers the address, so pinned clients keep working. `srv` trusts it by pinning the cert on `srv key` (TOFU → `~/.fatllm-cert`,
  used as `--cacert`). The claude.ai website connector will NOT trust it (needs a public cert).
- `tunnel`: Cloudflare quick tunnel. Last resort — the address changes on every restart (so a saved
  key goes stale) and can show Cloudflare error 1033 when the tunnel drops.
If a user reports error 1033 or an address that keeps changing, tell them to run `fatllm mode direct`
on the server (stable, Claude-Code-friendly), or `fatllm mode caddy` / `--domain` if they need the
claude.ai connector.

## Port
Default port 9001, else a random free one in 9002-9999 (`pick_port` in install.sh); never 80/443 on its
own, so it never clashes with x-ui, nginx or Caddy. A re-run keeps the previous port. `--port N|random`
or `fatllm port N|random` moves it (the key's address changes; the token stays).

## Files
- `install.sh`: one-line installer; never asks anything (re-runs itself with sudo when not root).
  Picks a mode automatically: caddy if it can get a public cert, else direct self-signed (stable),
  else tunnel. `--direct`/`--tunnel`/`--domain`/`--port` force a choice.
  Re-running keeps the key. `--local` uses the files in this checkout (development only).
- `server/agent.py`: the agent (Python 3.6+, standard library only). HTTP endpoints and an MCP
  server; see its docstring. Token in the first path segment or `Authorization: Bearer`.
  Commands run as jobs with output in a log file, so long commands survive the HTTP call and
  agent restarts (`KillMode=process`).
- `server/panel.html`: the owner's web panel at `<key>/panel` (Persian, RTL, phone-first, dark/light;
  one self-contained file, no external requests; served with a nonce CSP). It learns its key from
  its own address and calls the agent's endpoints: `/stats`, `/audit`, `/jobs?json=1`,
  `/file?json=1` (directory listing), `/file?download=1`, `/run` + `/job/<id>?json=1&offset=`,
  `/admin/expire`, `/admin/rotate` (new token in memory + env file, old key dies at once),
  `/admin/off` (runs `fatllm off` after answering). Check it at 390px in both themes with
  Playwright before committing UI changes (no sideways scroll, no console errors).
- `server/ui.sh`: the terminal look shared by install.sh and fatllm (logo, colours, ✓/!/✗ lines,
  spinners, boxes). Colours only on a real terminal, ASCII fallback (`FATLLM_ASCII=1`), and nothing
  wider than the screen: check new output at 46 columns (`script -qc "stty cols 46; ..."`).
- `server/fatllm`: the owner's CLI on the server (key, rotate, off/on, status, log, expire, mode,
  relay, update, uninstall). `fatllm` with no arguments on a terminal opens a numbered menu.
- `srv`: client. The agent serves it at `<key>/srv` with the key built in.
- `.claude/settings.json`: allows `./srv`, `srv`, `~/srv` and the `fatllm` MCP tools without prompts,
  and runs `.claude/session-start.sh` (links `srv` into /usr/local/bin, checks the key).

## Rules
- Text printed on the server (install.sh, fatllm, agent) stays English: phone SSH apps show Persian badly.
- The key never reaches logs: the agent does not log request lines, the audit log has commands only.
- Before committing: `python3 -m unittest discover -s test -v` and
  `shellcheck -S warning install.sh srv server/fatllm` (`pip install shellcheck-py`).
