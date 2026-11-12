#!/usr/bin/env python3
"""FATLLM agent: full shell access to this server for an AI assistant, over HTTPS.

It listens on 127.0.0.1 only; Caddy (or a Cloudflare quick tunnel) puts HTTPS in
front of it. Every request must carry the secret token, either as the first path
segment (https://host/<token>/run) or as "Authorization: Bearer <token>".
Anything without a valid token gets a plain 404.

Endpoints (after the token):
  GET  /              usage guide for the AI (plain text)
  POST /run           run a bash command as root (body = command)
  GET  /job/<id>      output and status of a command still running
  POST /job/<id>/kill stop it
  GET  /jobs          recent commands
  GET  /file?path=    download a file (or list a directory)
  PUT  /file?path=    upload a file (parents created, atomic)
  GET  /info          server facts (JSON)
  GET  /srv           a small client script with the key built in
  POST /mcp           MCP server (Streamable HTTP, JSON answers)

Python 3.6+, standard library only.
"""
import base64
import errno
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlsplit

VERSION = "1.0.0"

TOKEN = os.environ.get("FATLLM_TOKEN", "").strip()
LISTEN = os.environ.get("FATLLM_LISTEN", "127.0.0.1")
PORT = int(os.environ.get("FATLLM_LOCAL_PORT", "47100") or 47100)
STATE_DIR = os.environ.get("FATLLM_STATE_DIR", "/var/lib/fatllm")
LOG_DIR = os.environ.get("FATLLM_LOG_DIR", "/var/log/fatllm")
PUBLIC_URL = os.environ.get("FATLLM_PUBLIC_URL", "").strip().rstrip("/")
MODE = os.environ.get("FATLLM_MODE", "caddy")
EXPIRES = int(os.environ.get("FATLLM_EXPIRES", "0") or 0)
CLIENT_SCRIPT = os.environ.get(
    "FATLLM_CLIENT", os.path.join(os.path.dirname(os.path.abspath(__file__)), "srv"))
RELAY_DIR = os.environ.get("FATLLM_RELAY_DIR", "/etc/fatllm/relays")
TLS_CERT = os.environ.get("FATLLM_TLS_CERT", "").strip()
TLS_KEY = os.environ.get("FATLLM_TLS_KEY", "").strip()

JOBS_DIR = os.path.join(STATE_DIR, "jobs")
AUDIT_LOG = os.path.join(LOG_DIR, "audit.log")

DEFAULT_WAIT = 50          # seconds a /run call waits before handing back a job id
MAX_WAIT = 600
MAX_OUTPUT = 1000000       # bytes of output returned per answer (head + tail kept)
MAX_COMMAND = 10 * 1024 * 1024
MAX_MCP_BODY = 50 * 1024 * 1024
MAX_RUNNING = 64
KEEP_DONE_JOBS = 6 * 3600  # seconds a finished job's log is kept
FAIL_LIMIT = 30            # bad-token requests per IP per 10 minutes
SHELL = shutil.which("bash") or "/bin/sh"
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
MCP_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]

LOCK = threading.Lock()
JOBS = {}
FAILS = {}


# ----------------------------------------------------------------------------- helpers

def now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def audit(ip, action, detail=""):
    detail = detail.replace("\\", "\\\\").replace("\n", "\\n")
    if len(detail) > 600:
        detail = detail[:600] + "..."
    line = "%s %s %s %s\n" % (now_iso(), ip, action, detail)
    try:
        with LOCK:
            if os.path.exists(AUDIT_LOG) and os.path.getsize(AUDIT_LOG) > 5 * 1024 * 1024:
                os.replace(AUDIT_LOG, AUDIT_LOG + ".1")
            with open(AUDIT_LOG, "a") as f:
                f.write(line)
    except OSError:
        pass


def clamp(value, low, high, default):
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def truthy(value):
    return str(value).lower() in ("1", "true", "yes", "on")


def read_log(path, offset=0, cap=MAX_OUTPUT):
    """Text of a job log from offset, keeping head and tail when it is too long."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return "", 0, False
    offset = max(0, min(offset, size))
    with open(path, "rb") as f:
        f.seek(offset)
        if size - offset <= cap:
            return f.read().decode("utf-8", "replace"), size, False
        head_n = cap // 5
        tail_n = cap - head_n
        head = f.read(head_n)
        f.seek(size - tail_n)
        tail = f.read()
    skipped = size - offset - head_n - tail_n
    text = (head.decode("utf-8", "replace")
            + "\n... [%d bytes skipped; full output on the server: %s] ...\n" % (skipped, path)
            + tail.decode("utf-8", "replace"))
    return text, size, True


def is_binary(sample):
    return b"\0" in sample


def os_name():
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.system()


def meminfo():
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, rest = line.split(":", 1)
                if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree"):
                    out[key] = int(rest.split()[0]) * 1024
    except (OSError, ValueError):
        pass
    return out


def gib(n):
    return "%.1f GiB" % (n / 1073741824.0)


def load_relays():
    """Relays configured by `fatllm relay add`: name -> {socks_port, host, note}."""
    out = {}
    try:
        names = os.listdir(RELAY_DIR)
    except OSError:
        return out
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(RELAY_DIR, name)) as f:
                r = json.load(f)
            port = int(r.get("socks_port") or 0)
            if port and r.get("name"):
                out[r["name"]] = {"socks_port": port, "host": r.get("host", ""), "note": r.get("note", "")}
        except (OSError, ValueError, TypeError):
            continue
    return out


def proxy_env_for(via):
    """Environment that routes a command's network through a named relay (SOCKS5)."""
    if not via:
        return None
    relays = load_relays()
    r = relays.get(via)
    if not r:
        raise ValueError("unknown relay '%s'; configured relays: %s"
                         % (via, ", ".join(sorted(relays)) or "none"))
    socks = "socks5h://127.0.0.1:%d" % r["socks_port"]
    nope = "127.0.0.1,localhost,::1"
    return {"ALL_PROXY": socks, "all_proxy": socks, "HTTPS_PROXY": socks, "https_proxy": socks,
            "HTTP_PROXY": socks, "http_proxy": socks, "NO_PROXY": nope, "no_proxy": nope}


def server_info():
    info = {
        "hostname": socket.gethostname(),
        "os": os_name(),
        "kernel": platform.release(),
        "arch": platform.machine(),
        "cpus": os.cpu_count(),
        "python": platform.python_version(),
        "agent": "fatllm " + VERSION,
        "user": "root" if os.geteuid() == 0 else str(os.geteuid()),
        "time_utc": now_iso(),
        "timezone": time.strftime("%Z"),
        "access_expires": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(EXPIRES)) if EXPIRES else "never",
    }
    try:
        with open("/proc/uptime") as f:
            secs = int(float(f.read().split()[0]))
        info["uptime"] = "%dd %dh %dm" % (secs // 86400, secs % 86400 // 3600, secs % 3600 // 60)
    except (OSError, ValueError, IndexError):
        pass
    try:
        info["load"] = " ".join("%.2f" % x for x in os.getloadavg())
    except OSError:
        pass
    mem = meminfo()
    if "MemTotal" in mem:
        info["memory"] = "%s total, %s available" % (gib(mem["MemTotal"]), gib(mem.get("MemAvailable", 0)))
    try:
        du = shutil.disk_usage("/")
        info["disk_root"] = "%s total, %s free" % (gib(du.total), gib(du.free))
    except OSError:
        pass
    with LOCK:
        info["jobs_running"] = sum(1 for j in JOBS.values() if not j.done.is_set())
    rel = load_relays()
    if rel:
        info["relays"] = {n: "via=%s exits through %s (socks5h://127.0.0.1:%d)"
                          % (n, r["host"] or "another server", r["socks_port"]) for n, r in rel.items()}
    return info


# ----------------------------------------------------------------------------- jobs

class Job(object):
    def __init__(self, cmd, cwd, ip):
        self.id = secrets.token_hex(4)
        self.cmd = cmd
        self.cwd = cwd
        self.ip = ip
        self.log_path = os.path.join(JOBS_DIR, self.id + ".log")
        self.started = time.time()
        self.ended = None
        self.exit_code = None
        self.killed = False
        self.done = threading.Event()
        self.proc = None

    def start(self, stdin_data=None, timeout=0, extra_env=None):
        env = {k: v for k, v in os.environ.items() if not k.startswith("FATLLM_")}
        env.update({
            "PATH": SAFE_PATH,
            "HOME": env.get("HOME") or "/root",
            "TERM": "dumb",
            "DEBIAN_FRONTEND": "noninteractive",
            "NEEDRESTART_MODE": "a",
            "PAGER": "cat",
            "GIT_PAGER": "cat",
            "SYSTEMD_PAGER": "",
            "SYSTEMD_COLORS": "0",
            "LANG": env.get("LANG") or "C.UTF-8",
        })
        if os.geteuid() == 0:
            env["HOME"] = "/root"
        if extra_env:
            env.update(extra_env)
        stdin = subprocess.DEVNULL
        stdin_file = None
        if stdin_data is not None:
            stdin_path = os.path.join(JOBS_DIR, self.id + ".in")
            with open(stdin_path, "wb") as f:
                f.write(stdin_data if isinstance(stdin_data, bytes) else stdin_data.encode("utf-8"))
            stdin_file = open(stdin_path, "rb")
            os.unlink(stdin_path)
            stdin = stdin_file
        log = open(self.log_path, "wb")
        try:
            self.proc = subprocess.Popen(
                [SHELL, "-c", self.cmd], cwd=self.cwd, stdin=stdin, stdout=log,
                stderr=subprocess.STDOUT, env=env, start_new_session=True, close_fds=True)
        finally:
            log.close()
            if stdin_file:
                stdin_file.close()
        threading.Thread(target=self._wait, daemon=True).start()
        if timeout and timeout > 0:
            timer = threading.Timer(timeout, self.kill)
            timer.daemon = True
            timer.start()

    def _wait(self):
        code = self.proc.wait()
        self.exit_code = 128 - code if code < 0 else code
        self.ended = time.time()
        self.done.set()
        audit(self.ip, "done", "job=%s exit=%s %.1fs" % (self.id, self.exit_code, self.ended - self.started))

    def kill(self):
        if self.done.is_set() or not self.proc:
            return False
        self.killed = True
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
            except OSError:
                pass
            if self.done.wait(3):
                break
        return True

    def result(self, offset=0):
        output, size, truncated = read_log(self.log_path, offset)
        return {
            "job": self.id,
            "done": self.done.is_set(),
            "exit_code": self.exit_code,
            "killed": self.killed,
            "seconds": round((self.ended or time.time()) - self.started, 1),
            "output": output,
            "output_bytes": size,
            "next_offset": size,
            "truncated": truncated,
            "command": self.cmd if len(self.cmd) <= 300 else self.cmd[:300] + "...",
        }


def prune_jobs():
    cutoff = time.time() - KEEP_DONE_JOBS
    with LOCK:
        for jid in [j.id for j in JOBS.values() if j.done.is_set() and (j.ended or 0) < cutoff]:
            job = JOBS.pop(jid)
            try:
                os.unlink(job.log_path)
            except OSError:
                pass
        known = set(JOBS)
    try:
        for name in os.listdir(JOBS_DIR):
            path = os.path.join(JOBS_DIR, name)
            if name.split(".")[0] not in known and os.path.getmtime(path) < time.time() - 86400:
                os.unlink(path)
    except OSError:
        pass


def start_job(cmd, cwd, stdin_data, timeout, ip, via=None):
    if not isinstance(cmd, str) or not cmd.strip():
        raise ValueError("empty command")
    cwd = os.path.expanduser(cwd or "~")
    if not os.path.isdir(cwd):
        raise ValueError("cwd does not exist: %s" % cwd)
    proxy_env = proxy_env_for(via)  # raises ValueError for an unknown relay
    prune_jobs()
    with LOCK:
        if sum(1 for j in JOBS.values() if not j.done.is_set()) >= MAX_RUNNING:
            raise ValueError("too many commands running (%d); wait or kill some" % MAX_RUNNING)
    job = Job(cmd, cwd, ip)
    with LOCK:
        JOBS[job.id] = job
    audit(ip, "run", "job=%s cwd=%s%s cmd=%s" % (job.id, cwd, (" via=" + via) if via else "", cmd))
    try:
        job.start(stdin_data, clamp(timeout, 0, 7 * 86400, 0), proxy_env)
    except Exception:
        with LOCK:
            JOBS.pop(job.id, None)
        raise
    return job


def find_job(jid):
    with LOCK:
        return JOBS.get(jid)


def orphan_result(jid, offset=0):
    """A job from before the agent restarted: only its log is left."""
    path = os.path.join(JOBS_DIR, jid + ".log")
    if not os.path.exists(path):
        return None
    output, size, truncated = read_log(path, offset)
    return {"job": jid, "done": None, "exit_code": None, "killed": False, "seconds": None,
            "output": output, "output_bytes": size, "next_offset": size, "truncated": truncated,
            "command": "", "note": "the agent restarted since this job started; its status is unknown"}


def format_result(r, next_hint):
    out = r["output"]
    if out and not out.endswith("\n"):
        out += "\n"
    if r["done"]:
        tail = "[exit %s%s · %ss]" % (r["exit_code"], " (killed)" if r.get("killed") else "", r["seconds"])
    elif r["done"] is None:
        tail = "[status unknown: %s]" % r.get("note", "")
    else:
        tail = "[still running after %ss · job %s · %s]" % (r["seconds"], r["job"], next_hint)
    return out + tail + "\n"


# ----------------------------------------------------------------------------- guide

def guide(base):
    host = socket.gethostname()
    text = """FATLLM: full root shell on the server "{host}" ({os}).
The owner installed this so an AI assistant can run and manage this server for them.
They want you to work on your own: run what the task needs, do not stop to ask for
approval of routine steps. Ask first only before something that destroys data that
cannot be recovered. The key below is a secret: never print it in full, never commit it.

KEY = {base}

EASIEST (a small client with the key built in):
  curl -fsS '{base}/srv' -o ~/srv && chmod +x ~/srv
  ~/srv 'uptime && df -h /'          run a command, prints output + [exit N]
  ~/srv - <<'EOF'                    run a multi-line script
  apt-get update -q
  EOF
  ~/srv job <id>                     a command that was still running
  ~/srv get /etc/nginx/nginx.conf    download   |  ~/srv put ./local /remote/path   upload
  ~/srv info                         server facts

RAW HTTP (always use --data-binary, never -d, so newlines survive):
  curl -sS '{base}/run' --data-binary 'uptime'
  curl -sS '{base}/run?wait=120&cwd=/opt' --data-binary @- <<'EOF'
  ...script...
  EOF
  Answer: stdout+stderr together, last line "[exit N · Ts]" (also header X-Exit-Code).
  Commands run as root with bash -c, stdin=/dev/null, DEBIAN_FRONTEND=noninteractive.
  A call waits up to {wait}s (?wait=0..{maxwait}). If the command is still running you get
  "[still running ... job ID ...]"; it keeps running on the server:
    curl -sS '{base}/job/ID?wait=60'           output so far + status
    curl -sS -X POST '{base}/job/ID/kill'      stop it
  Other options: ?timeout=SECONDS kills it after that; ?json=1 or Content-Type
  application/json ({{"cmd": "...", "cwd": "...", "stdin": "...", "wait": 50}}) answers JSON.

FILES:
  curl -sS '{base}/file?path=/etc/hostname'                    download (a directory lists it)
  curl -sS -T ./local.txt '{base}/file?path=/root/x.txt&mode=600'   upload (parents made, atomic)
  add &append=1 to append instead of replacing.

INFO: curl -sS '{base}/info'
MCP:  {base}/mcp  (Streamable HTTP; tools run_command, job_status, read_file, write_file, server_info)

TIPS: use non-interactive flags (apt-get -y, --yes, --force); output over {maxout} bytes is
cut in the middle (full log path is shown); restarting this agent ("systemctl restart fatllm")
does not stop running commands. Owner commands on the server: fatllm key | rotate | off | log.
""".format(host=host, os=os_name(), base=base, wait=DEFAULT_WAIT, maxwait=MAX_WAIT, maxout=MAX_OUTPUT)
    rel = load_relays()
    if rel:
        lines = "\n".join("  via=%s  ->  exits through %s" % (n, r["host"] or "another server")
                          for n, r in sorted(rel.items()))
        text += ("\nRELAYS (reach sites this server itself can't):\n%s\n"
                 "  route a command through one: srv --via NAME 'curl -s https://site'\n"
                 "  or: curl '%s/run?via=NAME' --data-binary 'curl -s https://site'\n"
                 "  Iranian sites -> use the Iran relay; sites blocked from inside Iran -> run WITHOUT via.\n"
                 % (lines, base))
    return text


MCP_INSTRUCTIONS = (
    "Full root shell on the server %s, granted by its owner so you can run and manage it for them. "
    "Work on your own: run what the task needs without asking for approval of routine steps; ask first "
    "only before destroying data that cannot be recovered. run_command runs bash as root (non-interactive; "
    "use -y flags). Long commands keep running on the server: when a job id comes back, poll it with "
    "job_status.")

TOOLS = [
    {
        "name": "run_command",
        "title": "Run a shell command on the server",
        "description": (
            "Run a bash command (or a multi-line script) as root on the server. Returns stdout+stderr "
            "and a last line [exit N]. Non-interactive: stdin is empty unless given; use -y flags. "
            "If it is still running after wait_seconds you get a job id: it keeps running, check it "
            "with job_status."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "bash command or script"},
                "cwd": {"type": "string", "description": "working directory (default /root)"},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT,
                                 "description": "how long to wait for it to finish (default %d)" % DEFAULT_WAIT},
                "timeout_seconds": {"type": "integer", "minimum": 0,
                                    "description": "kill it after this many seconds (default: never)"},
                "stdin": {"type": "string", "description": "text given to the command on stdin"},
                "via": {"type": "string", "description": "route this command's network through a configured relay by name (e.g. iran) so it exits from another server; relay names are in server_info"},
            },
            "required": ["command"],
        },
        "annotations": {"title": "Run shell command", "readOnlyHint": False,
                        "destructiveHint": True, "openWorldHint": True},
    },
    {
        "name": "job_status",
        "title": "Check or stop a running command",
        "description": "Output so far and status of a command that was still running (job id from run_command). Set kill=true to stop it.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string"},
                "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT,
                                 "description": "wait this long for it to finish (default 30)"},
                "kill": {"type": "boolean", "description": "stop the command"},
            },
            "required": ["job_id"],
        },
        "annotations": {"title": "Job status", "readOnlyHint": False, "destructiveHint": False},
    },
    {
        "name": "read_file",
        "title": "Read a file on the server",
        "description": "Read a text file (or list a directory) on the server.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "absolute path"},
                "offset": {"type": "integer", "minimum": 0, "description": "byte offset to start at"},
                "max_bytes": {"type": "integer", "minimum": 1, "maximum": 5000000,
                              "description": "default 200000"},
                "encoding": {"type": "string", "enum": ["text", "base64"], "description": "default text"},
            },
            "required": ["path"],
        },
        "annotations": {"title": "Read file", "readOnlyHint": True},
    },
    {
        "name": "write_file",
        "title": "Write a file on the server",
        "description": "Create or replace a file (parent folders are created; the write is atomic). An existing file keeps its owner and mode unless mode is given.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "absolute path"},
                "content": {"type": "string"},
                "encoding": {"type": "string", "enum": ["text", "base64"], "description": "default text"},
                "append": {"type": "boolean", "description": "append instead of replacing"},
                "mode": {"type": "string", "description": "octal permissions, e.g. 644 or 600"},
            },
            "required": ["path", "content"],
        },
        "annotations": {"title": "Write file", "readOnlyHint": False, "destructiveHint": True},
    },
    {
        "name": "server_info",
        "title": "Server facts",
        "description": "Hostname, OS, CPU, memory, disk, uptime and load of the server.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": {"title": "Server info", "readOnlyHint": True},
    },
]


class McpError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


# ----------------------------------------------------------------------------- files

def list_dir(path):
    lines = []
    try:
        names = sorted(os.listdir(path))
    except OSError as e:
        return "cannot list %s: %s\n" % (path, e.strerror)
    for name in names:
        full = os.path.join(path, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        kind = "d" if os.path.isdir(full) and not os.path.islink(full) else ("l" if os.path.islink(full) else "-")
        lines.append("%s %s %12d %s %s%s" % (
            kind, oct(st.st_mode & 0o7777)[2:].rjust(4, "0"), st.st_size,
            time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)), name,
            " -> " + os.readlink(full) if kind == "l" else ("/" if kind == "d" else "")))
    return "%s (%d entries)\n%s\n" % (path, len(lines), "\n".join(lines))


def check_path(path):
    if not path or not isinstance(path, str):
        raise ValueError("path is required")
    path = os.path.expanduser(path)
    if not path.startswith("/"):
        raise ValueError("path must be absolute")
    return path


def write_file(path, chunks, mode=None, append=False):
    path = check_path(path)
    if os.path.islink(path):
        path = os.path.realpath(path)
    parent = os.path.dirname(path) or "/"
    os.makedirs(parent, exist_ok=True)
    total = 0
    if append:
        with open(path, "ab") as f:
            for chunk in chunks:
                f.write(chunk)
                total += len(chunk)
        if mode is not None:
            os.chmod(path, mode)
        return path, total
    tmp = os.path.join(parent, ".fatllm-%s.tmp" % secrets.token_hex(4))
    try:
        with open(tmp, "wb") as f:
            for chunk in chunks:
                f.write(chunk)
                total += len(chunk)
        if os.path.exists(path):
            st = os.stat(path)
            os.chmod(tmp, st.st_mode & 0o7777 if mode is None else mode)
            try:
                os.chown(tmp, st.st_uid, st.st_gid)
            except OSError:
                pass
        else:
            os.chmod(tmp, 0o644 if mode is None else mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path, total


def parse_mode(value):
    if value in (None, ""):
        return None
    try:
        mode = int(str(value), 8)
    except ValueError:
        raise ValueError("mode must be octal, like 644")
    if not 0 <= mode <= 0o7777:
        raise ValueError("mode must be octal, like 644")
    return mode


class BodyTooLarge(Exception):
    pass


# ----------------------------------------------------------------------------- HTTP

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fatllm"
    sys_version = ""

    def log_message(self, fmt, *args):  # request lines carry the token: never log them
        pass

    # --- plumbing

    def client_ip(self):
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):
            if MODE == "tunnel" and self.headers.get("Cf-Connecting-Ip"):
                return self.headers.get("Cf-Connecting-Ip").strip()
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[-1].strip()
        return ip

    def send(self, code, body, ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Connection", "close")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self.close_connection = True

    def send_json(self, code, obj, headers=None):
        self.send(code, json.dumps(obj, ensure_ascii=False, indent=1) + "\n",
                  "application/json; charset=utf-8", headers)

    def body_chunks(self, limit=0):
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        total = 0
        if "chunked" in te:
            while True:
                line = self.rfile.readline(65537)
                if not line:
                    raise ValueError("unexpected end of body")
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while True:
                        trailer = self.rfile.readline(65537)
                        if trailer in (b"\r\n", b"\n", b""):
                            break
                    return
                remaining = size
                while remaining:
                    chunk = self.rfile.read(min(remaining, 65536))
                    if not chunk:
                        raise ValueError("unexpected end of body")
                    remaining -= len(chunk)
                    total += len(chunk)
                    if limit and total > limit:
                        raise BodyTooLarge()
                    yield chunk
                self.rfile.readline()
        else:
            n = int(self.headers.get("Content-Length") or 0)
            if limit and n > limit:
                raise BodyTooLarge()
            while n > 0:
                chunk = self.rfile.read(min(n, 65536))
                if not chunk:
                    raise ValueError("unexpected end of body")
                n -= len(chunk)
                yield chunk

    def read_body(self, limit):
        return b"".join(self.body_chunks(limit))

    def authorize(self):
        """(ok, path after the token)."""
        path = urlsplit(self.path).path
        parts = path.split("/", 2)
        first = parts[1] if len(parts) > 1 else ""
        if first and hmac.compare_digest(first.encode("utf-8", "replace"), TOKEN.encode()):
            return True, "/" + (parts[2] if len(parts) > 2 else "")
        auth = self.headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer " and hmac.compare_digest(
                auth[7:].strip().encode("utf-8", "replace"), TOKEN.encode()):
            return True, path
        return False, path

    def base(self):
        if PUBLIC_URL:
            root = PUBLIC_URL
        else:
            host = self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "%s:%d" % (LISTEN, PORT)
            host = host.split(",")[0].strip()
            local = host.startswith("127.") or host.startswith("localhost") or host.startswith("[::1]")
            proto = (self.headers.get("X-Forwarded-Proto") or ("http" if local else "https")).split(",")[0].strip()
            root = "%s://%s" % (proto, host)
        return root + "/" + TOKEN

    # --- dispatch

    def do_GET(self):
        self.route()

    def do_HEAD(self):
        self.route()

    def do_POST(self):
        self.route()

    def do_PUT(self):
        self.route()

    def do_DELETE(self):
        self.route()

    def route(self):
        ip = self.client_ip()
        try:
            if failed_too_often(ip):
                return self.send(429, "too many attempts\n")
            ok, path = self.authorize()
            if not ok or (EXPIRES and time.time() > EXPIRES):
                note_failure(ip)
                return self.send(404, "not found\n")
            query = parse_qs(urlsplit(self.path).query)
            q = {k: v[0] for k, v in query.items()}
            m = self.command
            if path in ("/", "/guide") and m in ("GET", "HEAD"):
                return self.send(200, guide(self.base()))
            if path == "/run" and m == "POST":
                return self.h_run(q, ip)
            if path == "/info" and m in ("GET", "HEAD"):
                return self.send_json(200, server_info())
            if path == "/srv" and m in ("GET", "HEAD"):
                return self.h_client()
            if path == "/file":
                if m in ("GET", "HEAD"):
                    return self.h_get_file(q, ip)
                if m in ("PUT", "POST"):
                    return self.h_put_file(q, ip)
            if path == "/jobs" and m in ("GET", "HEAD"):
                return self.h_jobs()
            jm = re.match(r"^/job/([0-9a-f]{8})(/kill)?/?$", path)
            if jm:
                if jm.group(2) and m in ("POST", "DELETE"):
                    return self.h_kill(jm.group(1), ip)
                if not jm.group(2) and m in ("GET", "HEAD"):
                    return self.h_job(jm.group(1), q)
            if path in ("/mcp", "/mcp/"):
                if m == "POST":
                    return self.h_mcp(ip)
                return self.send(405, "use POST\n", headers={"Allow": "POST"})
            return self.send(404, "unknown endpoint; read the guide: GET %s/\n" % self.base())
        except BodyTooLarge:
            return self.send(413, "body too large\n")
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        except Exception as e:  # keep serving; tell the caller what broke
            try:
                self.send(500, "agent error: %s: %s\n" % (type(e).__name__, e))
            except Exception:
                self.close_connection = True

    # --- endpoints

    def h_run(self, q, ip):
        raw = self.read_body(MAX_COMMAND)
        as_json = "application/json" in (self.headers.get("Content-Type") or "")
        params = {}
        if as_json:
            try:
                params = json.loads(raw.decode("utf-8") or "{}")
                assert isinstance(params, dict)
            except Exception:
                return self.send_json(400, {"error": "body is not a JSON object"})
            cmd = params.get("cmd") or params.get("command") or ""
        else:
            cmd = raw.decode("utf-8", "replace")

        def opt(name, default=None):
            if name in params:
                return params[name]
            return q.get(name, default)

        want_json = as_json or truthy(q.get("json", ""))
        try:
            job = start_job(cmd, opt("cwd"), params.get("stdin") if as_json else None,
                            opt("timeout", 0), ip, opt("via"))
        except ValueError as e:
            if want_json:
                return self.send_json(400, {"error": str(e)})
            return self.send(400, "error: %s\n" % e)
        job.done.wait(clamp(opt("wait", DEFAULT_WAIT), 0, MAX_WAIT, DEFAULT_WAIT))
        return self.reply_job(job.result(), want_json)

    def reply_job(self, r, want_json):
        headers = {"X-Job": r["job"]}
        if r["done"]:
            headers["X-Exit-Code"] = str(r["exit_code"])
        if want_json:
            return self.send_json(200, r, headers)
        hint = "check: curl -sS '%s/job/%s?wait=60'" % (self.base(), r["job"])
        return self.send(200, format_result(r, hint), headers=headers)

    def h_job(self, jid, q):
        offset = clamp(q.get("offset", 0), 0, 1 << 62, 0)
        job = find_job(jid)
        if job:
            job.done.wait(clamp(q.get("wait", 30), 0, MAX_WAIT, 30))
            r = job.result(offset)
        else:
            r = orphan_result(jid, offset)
            if r is None:
                return self.send(404, "no such job (finished jobs are kept %d hours)\n" % (KEEP_DONE_JOBS // 3600))
        return self.reply_job(r, truthy(q.get("json", "")))

    def h_kill(self, jid, ip):
        job = find_job(jid)
        if not job:
            return self.send(404, "no such job\n")
        audit(ip, "kill", "job=%s" % jid)
        job.kill()
        return self.reply_job(job.result(), False)

    def h_jobs(self):
        with LOCK:
            jobs = sorted(JOBS.values(), key=lambda j: j.started, reverse=True)[:50]
        lines = ["%s  %-8s  %7.1fs  %s" % (
            j.id, ("exit %s" % j.exit_code) if j.done.is_set() else "running",
            (j.ended or time.time()) - j.started, j.cmd.replace("\n", " ")[:120]) for j in jobs]
        return self.send(200, "\n".join(lines) + ("\n" if lines else "no jobs\n"))

    def h_client(self):
        try:
            with open(CLIENT_SCRIPT) as f:
                script = f.read()
        except OSError:
            return self.send(404, "client script not installed\n")
        script = script.replace('BAKED_KEY=""', 'BAKED_KEY="%s"' % self.base(), 1)
        return self.send(200, script, "text/x-shellscript; charset=utf-8")

    def h_get_file(self, q, ip):
        try:
            path = check_path(q.get("path"))
        except ValueError as e:
            return self.send(400, "error: %s\n" % e)
        if os.path.isdir(path):
            return self.send(200, list_dir(path))
        try:
            f = open(path, "rb")
        except OSError as e:
            code = 404 if e.errno == errno.ENOENT else 403
            return self.send(code, "cannot read %s: %s\n" % (path, e.strerror))
        audit(ip, "download", path)
        with f:
            size = os.fstat(f.fileno()).st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            if self.command == "HEAD":
                return
            sent = 0
            while sent < size:
                chunk = f.read(min(65536, size - sent))
                if not chunk:
                    break
                self.wfile.write(chunk)
                sent += len(chunk)

    def h_put_file(self, q, ip):
        try:
            mode = parse_mode(q.get("mode"))
            path, n = write_file(q.get("path"), self.body_chunks(), mode, truthy(q.get("append", "")))
        except ValueError as e:
            return self.send(400, "error: %s\n" % e)
        except OSError as e:
            return self.send(403, "cannot write: %s\n" % e)
        audit(ip, "upload", "%s %d bytes" % (path, n))
        return self.send_json(200, {"path": path, "bytes": n})

    # --- MCP (Streamable HTTP, JSON answers only)

    def h_mcp(self, ip):
        raw = self.read_body(MAX_MCP_BODY)
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            return self.send_json(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "Parse error"}})
        batch = isinstance(msg, list)
        replies = []
        for m in (msg if batch else [msg]):
            if not isinstance(m, dict) or "method" not in m:
                continue  # responses or junk: nothing to answer
            reply = self.mcp_one(m, ip)
            if reply is not None:
                replies.append(reply)
        if not replies:
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        return self.send_json(200, replies if batch else replies[0])

    def mcp_one(self, m, ip):
        method = m.get("method")
        params = m.get("params") or {}
        is_request = "id" in m
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                result = {
                    "protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "fatllm", "title": "FATLLM server access", "version": VERSION},
                    "instructions": MCP_INSTRUCTIONS % socket.gethostname(),
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                result = self.mcp_call(params, ip)
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "resources/templates/list":
                result = {"resourceTemplates": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif isinstance(method, str) and method.startswith("notifications/"):
                return None
            else:
                raise McpError(-32601, "Method not found: %s" % method)
        except McpError as e:
            if not is_request:
                return None
            return {"jsonrpc": "2.0", "id": m.get("id"), "error": {"code": e.code, "message": e.message}}
        if not is_request:
            return None
        return {"jsonrpc": "2.0", "id": m.get("id"), "result": result}

    def mcp_call(self, params, ip):
        name = params.get("name")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise McpError(-32602, "arguments must be an object")

        def text(t, error=False):
            return {"content": [{"type": "text", "text": t}], "isError": error}

        try:
            if name == "run_command":
                job = start_job(args.get("command"), args.get("cwd"), args.get("stdin"),
                                args.get("timeout_seconds", 0), ip, args.get("via"))
                job.done.wait(clamp(args.get("wait_seconds", DEFAULT_WAIT), 0, MAX_WAIT, DEFAULT_WAIT))
                return text(format_result(job.result(), 'check it with job_status(job_id="%s")' % job.id))
            if name == "job_status":
                jid = str(args.get("job_id") or "")
                job = find_job(jid)
                if not job:
                    r = orphan_result(jid) if re.match(r"^[0-9a-f]{8}$", jid) else None
                    if r is None:
                        return text("no such job: %s" % jid, True)
                    return text(format_result(r, ""))
                if truthy(args.get("kill", False)):
                    audit(ip, "kill", "job=%s" % jid)
                    job.kill()
                job.done.wait(clamp(args.get("wait_seconds", 30), 0, MAX_WAIT, 30))
                return text(format_result(job.result(), 'check again with job_status(job_id="%s")' % jid))
            if name == "read_file":
                path = check_path(args.get("path"))
                if os.path.isdir(path):
                    return text(list_dir(path))
                offset = clamp(args.get("offset", 0), 0, 1 << 62, 0)
                limit = clamp(args.get("max_bytes", 200000), 1, 5000000, 200000)
                with open(path, "rb") as f:
                    size = os.fstat(f.fileno()).st_size
                    f.seek(offset)
                    data = f.read(limit)
                audit(ip, "read", path)
                more = "" if offset + len(data) >= size else \
                    "\n[showing bytes %d-%d of %d; read more with offset=%d]" % (
                        offset, offset + len(data), size, offset + len(data))
                if args.get("encoding") == "base64":
                    return text(base64.b64encode(data).decode("ascii") + more)
                if is_binary(data[:8192]):
                    return text("binary file (%d bytes); read it with encoding=base64" % size)
                return text(data.decode("utf-8", "replace") + more)
            if name == "write_file":
                content = args.get("content")
                if not isinstance(content, str):
                    raise ValueError("content must be a string")
                data = base64.b64decode(content) if args.get("encoding") == "base64" else content.encode("utf-8")
                path, n = write_file(args.get("path"), [data], parse_mode(args.get("mode")),
                                     truthy(args.get("append", False)))
                audit(ip, "upload", "%s %d bytes" % (path, n))
                return text("wrote %d bytes to %s" % (n, path))
            if name == "server_info":
                return text(json.dumps(server_info(), indent=1, ensure_ascii=False))
        except (ValueError, OSError, TypeError) as e:
            return text("error: %s" % e, True)
        raise McpError(-32602, "Unknown tool: %s" % name)


def failed_too_often(ip):
    with LOCK:
        rec = FAILS.get(ip)
        if not rec:
            return False
        if time.time() - rec[1] > 600:
            del FAILS[ip]
            return False
        return rec[0] >= FAIL_LIMIT


def note_failure(ip):
    with LOCK:
        if len(FAILS) > 10000:
            FAILS.clear()
        rec = FAILS.get(ip)
        if not rec or time.time() - rec[1] > 600:
            FAILS[ip] = [1, time.time()]
        else:
            rec[0] += 1


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main():
    if len(TOKEN) < 32:
        sys.exit("FATLLM_TOKEN is missing or too short; run the installer again.")
    for d in (STATE_DIR, JOBS_DIR, LOG_DIR):
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass
    family = socket.AF_INET6 if ":" in LISTEN else socket.AF_INET
    Server.address_family = family
    httpd = Server((LISTEN, PORT), Handler)
    scheme = "http"
    if TLS_CERT and TLS_KEY:
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except (AttributeError, ValueError):
            pass
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
        scheme = "https"
    print("fatllm %s listening on %s %s:%d (mode %s)" % (VERSION, scheme, LISTEN, PORT, MODE), flush=True)
    signal.signal(signal.SIGTERM, lambda *_: (httpd.server_close(), sys.exit(0)))
    httpd.serve_forever()


if __name__ == "__main__":
    main()
