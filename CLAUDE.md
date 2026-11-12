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

## Files
- `install.sh`: one-line installer; never asks anything (re-runs itself with sudo when not root). Agent on 127.0.0.1, HTTPS in front: its own Caddy
  with a Let's Encrypt certificate for `<ip>.sslip.io` (or `--domain`), on 443 or a free
  alternative port; falls back to a Cloudflare quick tunnel when ports 80 and 443 are both busy.
  Re-running keeps the key. `--local` uses the files in this checkout (development only).
- `server/agent.py`: the agent (Python 3.6+, standard library only). HTTP endpoints and an MCP
  server; see its docstring. Token in the first path segment or `Authorization: Bearer`.
  Commands run as jobs with output in a log file, so long commands survive the HTTP call and
  agent restarts (`KillMode=process`).
- `server/fatllm`: the owner's CLI on the server (key, rotate, off/on, status, log, expire, update, uninstall).
- `srv`: client. The agent serves it at `<key>/srv` with the key built in.
- `.claude/settings.json`: allows `./srv`, `srv`, `~/srv` and the `fatllm` MCP tools without prompts,
  and runs `.claude/session-start.sh` (links `srv` into /usr/local/bin, checks the key).

## Rules
- Text printed on the server (install.sh, fatllm, agent) stays English: phone SSH apps show Persian badly.
- The key never reaches logs: the agent does not log request lines, the audit log has commands only.
- Before committing: `python3 -m unittest discover -s test -v` and
  `shellcheck -S warning install.sh srv server/fatllm` (`pip install shellcheck-py`).
