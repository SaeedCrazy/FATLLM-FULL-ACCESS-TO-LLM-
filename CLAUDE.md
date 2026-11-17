# FATLLM: full server access for Claude (and any agent)

This repo installs FATLLM on the owner's servers and lets an AI agent run them through one key.
One server is the **hub** (reachable from the internet, holds the keys and the panel); any number
of other servers **join** it and are reached through it.

The owner works from a phone: reply in **Persian**, short and plain. Never ask them to run
anything on their side except the install one-liner, the join line, or a `fatllm ...` command.

## When the user pastes a key (a line like `FATLLM key: https://<ip>:9001/fat_...`)
1. `./srv key '<the pasted line>'` saves the key in `~/.fatllm-key` (outside the repo), pins the
   hub's self-signed certificate (`~/.fatllm-cert`) and lists the servers. The SessionStart hook
   (`.claude/session-start.sh`) already does this when `FATLLM_KEY` is set or `~/.fatllm-key` exists.
2. Read `./srv guide` once, then work:
   - `./srv 'command'` runs bash as root on the hub; output and `[exit N]`; srv exits with the same code
   - `./srv --on NAME 'command'` runs it on a joined server (`./srv servers` lists them, online or not)
   - `./srv - <<'EOF' ... EOF` for scripts
   - `./srv job <id>` when a command was still running (it keeps running on the server)
   - `./srv get <remote> [local]`, `./srv put <local> <remote> [mode]`, `./srv info` (all take `--on`)
   - srv retries dropped connections with the same job id, so a command never runs twice.
3. Work on your own. The owner granted root and does not want approval prompts for routine
   work. Ask first only before unrecoverable data loss, or before something that could lock
   them out (SSH, firewall, network config: keep their current SSH access working).
4. Never print the full key back, never write it into the repo, never commit it.

## Which server for which job
- Iranian sites (Mixin, Iranian gateways, Iranian APIs) block foreign IPs: run those checks
  `--on` the server in Iran.
- Sites blocked from inside Iran (Telegram, AI APIs, GitHub): run them on the foreign hub/server.
- A joined server that is offline answers 503; it reconnects by itself. Tell the owner to run
  `fatllm status` on that server if it stays offline.
To connect another server, the owner runs `fatllm add-server NAME` on the hub (or the panel's
«افزودن سرور») and pastes the printed line on the other server as root. Never ask for another
server's root password in chat.

If srv cannot reach the hub: a 403 from the proxy means the cloud environment's network policy
blocks the host; tell the user to set Network access to Full (or allow the hub's IP). Otherwise
ask them to run `fatllm status` on the hub and send the output.

## Design (v2)
- **Hub** (`FATLLM_ROLE=hub`): `server/agent.py` serves HTTPS itself (mode `direct`, default) with a
  10-year self-signed certificate for the bare IP (no sslip.io: filtered in Iran), port 9001, else a
  random free one in 9002-9999 (never 80/443). Optional mode `cert`: Caddy + Let's Encrypt on
  `<ip>.sslip.io` or `--domain`, for web connectors (claude.ai/ChatGPT) that need a public cert.
  The Cloudflare tunnel and SSH relays of v1 are gone (unstable; replaced by joined servers).
- **Nodes** (`FATLLM_ROLE=node`): the same agent.py. They dial OUT to the hub and long-poll
  `/node/poll` (results to `/node/result`), so they need no open port. The join line is
  `curl -fsSk --pinnedpubkey 'sha256//PIN' https://hub/join_XXX | bash` (pin = the hub's key, so
  the download cannot be swapped); it installs the node with its own node token and the hub's
  certificate as its CA. Join tokens live 2 h and die on first contact.
- **One op layer** (`run job jobs stat list read write commit info stats audit ping`, plus node-only
  `update set_hub uninstall`): the same code serves local and remote calls. `?on=NAME` / MCP
  `server` pick the target.
- **No double runs:** clients pass `?id=<8-32 hex>`; asking again with the same id returns the same
  job. Jobs write to log files and survive dropped connections and agent restarts (`KillMode=process`).
- **Keys:** `/etc/fatllm/keys.json` (0600, flock, reloaded on change): named keys with expiry, nodes,
  pending joins. A v1 `FATLLM_TOKEN` is migrated as key `main`.
- **Local admin:** the hub writes a root-only `/var/lib/fatllm/cli.token`; `fatllm` (through
  `agent.py admin api ...`) sends it as a Bearer header from 127.0.0.1, so the CLI keeps working
  when the main key is expired or rotated, and no secret appears in a process list.
- Moving the hub (port/IP/certificate change) first tells online nodes the new address and CA
  (`/admin/servers/rehome` → `set_hub`); `fatllm update` pushes the new agent to every online node.

## Files
- `install.sh`: hub installer; never asks anything (re-runs itself with sudo). Options `--port N|random`,
  `--cert`, `--domain`, `--email`, `--expire`, `--new-key`, `--local` (this checkout). Re-running keeps
  keys, port and servers. A node refuses it (`fatllm leave` first).
- `server/agent.py`: hub and node (Python 3.6+, standard library only); docstring lists the HTTP API.
  `agent.py admin ...` = key-store chores for `fatllm` (token, show, keys, add, remove, rotate, expire,
  pin, api, log, leave, node). `JOIN_SCRIPT` is the node installer the hub serves.
- `server/panel.html`: owner's web panel at `<key>/panel` (Persian, RTL, phone-first, Pingo theme from
  the EZ/xui-chain panels, dark/light; one self-contained file, nonce CSP, no external requests).
  Server switcher, dials, terminal (idempotent ids + retry), files, activity, keys, add-server flow.
  Check it at 390px in both themes with Playwright before committing (no sideways scroll, no console errors).
- `server/ui.sh`: terminal look for install.sh and fatllm (Pingo palette: truecolor, 256 or plain;
  ASCII fallback `FATLLM_ASCII=1`). Never wider than the screen: check output at 46 columns
  (`script -qc "stty cols 46; ..."`).
- `server/fatllm`: the owner's CLI (menu with no arguments). Hub: key, status, servers, add-server,
  remove-server, keys, rotate, expire, log, off/on, port, mode, test, update, uninstall. Node: status,
  log, off/on, leave.
- `srv`: client. The agent serves it at `<key>/srv` with the key built in.
- `.claude/settings.json`: allows `./srv`, `srv`, `~/srv` and the `fatllm` MCP tools without prompts,
  and runs `.claude/session-start.sh`.

## Rules
- Text printed on servers (install.sh, fatllm, agent, join script) stays English: phone SSH apps show
  Persian badly.
- The key never reaches logs: the agent does not log request lines; the audit log has no tokens.
- Before committing: `python3 -m unittest discover -s test -v` and
  `shellcheck -S warning install.sh srv server/fatllm server/ui.sh .claude/session-start.sh`
  (`pip install shellcheck-py`).
