#!/usr/bin/env python3
"""FATLLM agent: full shell access to one or more servers for an AI agent.

Two roles, one file (Python 3.6+, standard library only):

  hub   The server the AI talks to, over HTTPS with a secret key as the first path segment
        (https://host:9001/<key>/run) or "Authorization: Bearer <key>". It runs commands itself
        and forwards them to its nodes.
  node  Any other server. It never listens on the network: it dials OUT to the hub over HTTPS
        and long-polls for work. So it needs no open port and works behind NAT, firewalls and
        networks that block inbound connections (e.g. a server in Iran reached from abroad).

Every operation (run, job, kill, read, write, list, stats...) goes through one op layer, so
"on this server" and "on that node" behave the same. Commands run as jobs with their output in a
log file: they survive dropped connections and agent restarts, and a client that lost its
connection asks for the same job id again instead of starting the command twice.

Hub HTTP API (after the key; add ?on=<server> to target a node):
  GET  /                 usage guide for the AI (plain text)
  GET  /servers          the hub and its nodes, online or not
  POST /run              run a bash command as root (body = command, or JSON)
  GET  /job/<id>         output and status of a running command   POST /job/<id>/kill
  GET  /jobs             recent commands
  GET  /file?path=       download a file / list a directory      PUT /file?path=  upload
  GET  /info  /stats     server facts                            GET /audit  activity log
  GET  /srv              a small client script with the key built in
  GET  /panel            the owner's web panel
  POST /mcp              MCP server (Streamable HTTP, JSON answers)
  /admin/...             keys, expiry, servers, turn off (used by the panel and `fatllm`)
"""
import base64
import errno
import fcntl
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shlex
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, quote, urlsplit

VERSION = "2.0.0"

ENV = os.environ.get
ROLE = ENV("FATLLM_ROLE", "hub").strip() or "hub"
NAME = (ENV("FATLLM_NAME", "") or ("main" if ROLE == "hub" else socket.gethostname())).strip()
LISTEN = ENV("FATLLM_LISTEN", "") or "127.0.0.1"
PORT = int(ENV("FATLLM_LOCAL_PORT", "") or 47100)
MODE = ENV("FATLLM_MODE", "direct").strip() or "direct"
PUBLIC_URL = ENV("FATLLM_PUBLIC_URL", "").strip().rstrip("/")
TLS_CERT = ENV("FATLLM_TLS_CERT", "").strip()
TLS_KEY = ENV("FATLLM_TLS_KEY", "").strip()
STATE_DIR = ENV("FATLLM_STATE_DIR", "/var/lib/fatllm")
LOG_DIR = ENV("FATLLM_LOG_DIR", "/var/log/fatllm")
CONF_PATH = ENV("FATLLM_CONF", "/etc/fatllm/fatllm.env")
KEYS_FILE = ENV("FATLLM_KEYS", "/etc/fatllm/keys.json")
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CLIENT_SCRIPT = ENV("FATLLM_CLIENT", os.path.join(APP_DIR, "srv"))
PANEL_FILE = ENV("FATLLM_PANEL", os.path.join(APP_DIR, "panel.html"))
UI_FILE = ENV("FATLLM_UI", os.path.join(APP_DIR, "ui.sh"))
CLI_FILE = ENV("FATLLM_CLI", os.path.join(APP_DIR, "fatllm"))
LEGACY_TOKEN = ENV("FATLLM_TOKEN", "").strip()
LEGACY_EXPIRES = int(ENV("FATLLM_EXPIRES", "0") or 0)
# node side
HUB_URL = ENV("FATLLM_HUB", "").strip().rstrip("/")
NODE_TOKEN = ENV("FATLLM_NODE_TOKEN", "").strip()
HUB_CA = ENV("FATLLM_HUB_CA", "").strip()
RESTART = ENV("FATLLM_RESTART", "systemctl restart fatllm")   # how a node restarts itself (tests: true)

JOBS_DIR = os.path.join(STATE_DIR, "jobs")
SPOOL_DIR = os.path.join(STATE_DIR, "spool")
AUDIT_LOG = os.path.join(LOG_DIR, "audit.log")
CLI_TOKEN_FILE = os.path.join(STATE_DIR, "cli.token")   # lets `fatllm` on this server talk to the agent

DEFAULT_WAIT = 50          # seconds /run waits before handing back a job id
MAX_WAIT = 600
MAX_OUTPUT = 1000000       # bytes of output per answer (head + tail kept)
MAX_COMMAND = 10 * 1024 * 1024
MAX_JSON = 64 * 1024 * 1024
CHUNK = 4 * 1024 * 1024    # file bytes per hub<->node message
MAX_RUNNING = 64
KEEP_DONE_JOBS = 6 * 3600
FAIL_LIMIT = 30            # bad-key requests per IP per 10 minutes
NODE_ONLINE = int(ENV("FATLLM_NODE_ONLINE", "") or 45)  # online if heard from in the last N seconds
POLL_WAIT = int(ENV("FATLLM_POLL_WAIT", "") or 25)      # how long one long-poll is held open
JOIN_TTL = 2 * 3600
SHELL = shutil.which("bash") or "/bin/sh"
SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/snap/bin"
MCP_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
JOB_ID = re.compile(r"^[0-9a-f]{8,32}$")
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,31}$")

LOCK = threading.Lock()
JOBS = {}
FAILS = {}
LAST_USED = {}


class OpError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


# ============================================================================= helpers

def now():
    return int(time.time())


def iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t if t is not None else time.time()))


def clamp(value, low, high, default):
    try:
        value = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, value))


def truthy(value):
    return str(value).lower() in ("1", "true", "yes", "on")


def b64e(data):
    return base64.b64encode(data).decode("ascii")


def b64d(text):
    return base64.b64decode(text if isinstance(text, bytes) else text.encode("ascii"))


def same(a, b):
    return hmac.compare_digest(str(a).encode("utf-8", "replace"), str(b).encode("utf-8", "replace"))


def new_token(prefix):
    return prefix + secrets.token_urlsafe(32)


def ensure_dirs(*dirs):
    for d in dirs:
        os.makedirs(d, exist_ok=True)
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass


def audit(entry):
    """One JSON line per action. Never the key itself."""
    entry = dict(entry)
    entry.setdefault("t", iso())
    for k in ("cmd", "path"):
        if isinstance(entry.get(k), str) and len(entry[k]) > 2000:
            entry[k] = entry[k][:2000] + "..."
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        with LOCK:
            if os.path.exists(AUDIT_LOG) and os.path.getsize(AUDIT_LOG) > 5 * 1024 * 1024:
                os.replace(AUDIT_LOG, AUDIT_LOG + ".1")
            with open(AUDIT_LOG, "a", encoding="utf-8") as f:
                f.write(line)
    except OSError:
        pass


def read_audit(n=300, on=None):
    lines = []
    for path in (AUDIT_LOG + ".1", AUDIT_LOG):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines.extend(f.read().splitlines())
        except OSError:
            pass
    out = []
    for line in reversed(lines):
        if not line.strip():
            continue
        if line.startswith("{"):
            try:
                e = json.loads(line)
            except ValueError:
                continue
        else:  # a version 1 line: "time ip action detail"
            p = line.split(" ", 3)
            if len(p) < 3:
                continue
            e = {"t": p[0], "ip": p[1], "a": p[2], "detail": p[3] if len(p) > 3 else ""}
        if on and e.get("on", NAME) != on:
            continue
        out.append(e)
        if len(out) >= n:
            break
    return out


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


def uptime_secs():
    try:
        with open("/proc/uptime") as f:
            return int(float(f.read().split()[0]))
    except (OSError, ValueError, IndexError):
        return None


def brief():
    mem = meminfo()
    return {"hostname": socket.gethostname(), "os": os_name(), "kernel": platform.release(),
            "arch": platform.machine(), "cpus": os.cpu_count() or 1, "version": VERSION,
            "mem_total": mem.get("MemTotal"), "uptime_s": uptime_secs()}


def stats():
    mem = meminfo()
    try:
        du = shutil.disk_usage("/")
        disk_total, disk_free = du.total, du.free
    except OSError:
        disk_total = disk_free = None
    try:
        load = [round(x, 2) for x in os.getloadavg()]
    except OSError:
        load = []
    with LOCK:
        running = sum(1 for j in JOBS.values() if not j.done.is_set())
    s = brief()
    s.update({"load": load, "mem_available": mem.get("MemAvailable"), "swap_total": mem.get("SwapTotal"),
              "swap_free": mem.get("SwapFree"), "disk_total": disk_total, "disk_free": disk_free,
              "jobs_running": running, "time": now(), "role": ROLE, "name": NAME})
    return s


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
    return (head.decode("utf-8", "replace")
            + "\n... [%d bytes skipped; full output on the server: %s] ...\n" % (skipped, path)
            + tail.decode("utf-8", "replace")), size, True


def persist_conf(key, value, path=None):
    """Set KEY=value in the env file the service reads (atomic). False when there is none."""
    path = path or CONF_PATH
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return False
    out, found = [], False
    for line in lines:
        if line.startswith(key + "="):
            out.append("%s=%s" % (key, value))
            found = True
        else:
            out.append(line)
    if not found:
        out.append("%s=%s" % (key, value))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("\n".join(out) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


def detached(script):
    """Run a shell snippet after we answered (it may restart or remove this agent)."""
    subprocess.Popen(["/bin/sh", "-c", script], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


_CLI = []


def cli_token(create=False):
    """The secret `fatllm` on this server uses (root-only file; accepted only from 127.0.0.1)."""
    if _CLI:
        return _CLI[0]
    try:
        with open(CLI_TOKEN_FILE) as f:
            tok = f.read().strip()
    except OSError:
        tok = ""
    if len(tok) < 32 and create:
        tok = new_token("cli_")
        fd = os.open(CLI_TOKEN_FILE + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(tok + "\n")
        os.replace(CLI_TOKEN_FILE + ".tmp", CLI_TOKEN_FILE)
    if len(tok) >= 32:
        _CLI.append(tok)
        return tok
    return ""


_PIN = {}


def cert_pin(path):
    """sha256 pin of a certificate's public key, as curl's --pinnedpubkey wants it."""
    if not path or not os.path.exists(path):
        return ""
    if path in _PIN:
        return _PIN[path]
    try:
        pub = subprocess.run(["openssl", "x509", "-in", path, "-pubkey", "-noout"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        der = subprocess.run(["openssl", "pkey", "-pubin", "-outform", "der"], input=pub,
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True).stdout
        pin = "sha256//" + base64.b64encode(hashlib.sha256(der).digest()).decode("ascii")
    except (OSError, subprocess.CalledProcessError):
        pin = ""
    _PIN[path] = pin
    return pin


# ============================================================================= key store (hub)

class Store(object):
    """keys.json on the hub: agent keys, nodes and pending joins. Shared with `agent.py admin`
    through a file lock; the agent re-reads it whenever it changes on disk."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self._data = None
        self._mtime = None

    def _fresh(self):
        d = {"version": 2, "keys": [], "nodes": [], "joins": []}
        if len(LEGACY_TOKEN) >= 32:   # an install from version 1: keep its key working
            d["keys"].append({"name": "main", "token": LEGACY_TOKEN, "created": now(),
                              "expires": LEGACY_EXPIRES})
        else:
            d["keys"].append({"name": "main", "token": new_token("fat_"), "created": now(), "expires": 0})
        return d

    def _read(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            try:
                os.replace(self.path, "%s.broken-%d" % (self.path, now()))
            except OSError:
                pass
            return None
        if not isinstance(d, dict):
            return None
        for k in ("keys", "nodes", "joins"):
            d.setdefault(k, [])
        return d

    def _write(self, d):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = "%s.tmp-%d" % (self.path, os.getpid())
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def _stamp(self):
        try:
            return os.stat(self.path).st_mtime_ns
        except OSError:
            return None

    def data(self):
        with self.lock:
            stamp = self._stamp()
            if self._data is not None and stamp is not None and stamp == self._mtime:
                return self._data
            d = self._read() if stamp is not None else None
            if d is None or not d["keys"]:
                self.modify(lambda dd: None)   # creates the file with a key
            else:
                self._data, self._mtime = d, stamp
            return self._data

    def modify(self, fn):
        with self.lock:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            with open(self.path + ".lock", "a") as lockf:
                fcntl.flock(lockf, fcntl.LOCK_EX)
                try:
                    d = self._read()
                    if d is None or not d["keys"]:
                        fresh = self._fresh()
                        d = d or fresh
                        d["keys"] = d["keys"] or fresh["keys"]
                    result = fn(d)
                    self._write(d)
                    self._data = d
                    self._mtime = self._stamp()
                    return result
                finally:
                    fcntl.flock(lockf, fcntl.LOCK_UN)

    # --- lookups
    def principal(self, token):
        """("key"|"node"|"join", record) for a token, or (None, None)."""
        if not token or len(token) < 20:
            return None, None
        d = self.data()
        t = now()
        for k in d["keys"]:
            if same(k.get("token", ""), token):
                if k.get("expires") and t > k["expires"]:
                    return None, None
                return "key", k
        for n in d["nodes"]:
            if same(n.get("token", ""), token):
                return "node", n
        for j in d["joins"]:
            if same(j.get("token", ""), token) and j.get("expires", 0) > t:
                return "join", j
        return None, None

    def node(self, name):
        for n in self.data()["nodes"]:
            if n["name"] == name:
                return n
        return None

    def key(self, name):
        for k in self.data()["keys"]:
            if k["name"] == name:
                return k
        return None


STORE = Store(KEYS_FILE)


# ============================================================================= jobs

class Job(object):
    def __init__(self, cmd, cwd, jid, who):
        self.id = jid or secrets.token_hex(4)
        self.cmd = cmd
        self.cwd = cwd
        self.who = who
        self.log_path = os.path.join(JOBS_DIR, self.id + ".log")
        self.started = time.time()
        self.ended = None
        self.exit_code = None
        self.killed = False
        self.done = threading.Event()
        self.proc = None

    def start(self, stdin_data=None, timeout=0):
        env = {k: v for k, v in os.environ.items() if not k.startswith("FATLLM_")}
        env.update({"PATH": SAFE_PATH, "HOME": "/root" if os.geteuid() == 0 else env.get("HOME", "/"),
                    "TERM": "dumb", "DEBIAN_FRONTEND": "noninteractive", "NEEDRESTART_MODE": "a",
                    "PAGER": "cat", "GIT_PAGER": "cat", "SYSTEMD_PAGER": "", "SYSTEMD_COLORS": "0",
                    "LANG": env.get("LANG") or "C.UTF-8"})
        stdin, stdin_file = subprocess.DEVNULL, None
        if stdin_data is not None:
            spath = os.path.join(JOBS_DIR, self.id + ".in")
            with open(spath, "wb") as f:
                f.write(stdin_data if isinstance(stdin_data, bytes) else str(stdin_data).encode("utf-8"))
            stdin_file = open(spath, "rb")
            os.unlink(spath)
            stdin = stdin_file
        log = open(self.log_path, "wb")
        try:
            self.proc = subprocess.Popen([SHELL, "-c", self.cmd], cwd=self.cwd, stdin=stdin, stdout=log,
                                         stderr=subprocess.STDOUT, env=env, start_new_session=True,
                                         close_fds=True)
        finally:
            log.close()
            if stdin_file:
                stdin_file.close()
        threading.Thread(target=self._wait, daemon=True).start()
        if timeout and timeout > 0:
            t = threading.Timer(timeout, self.kill)
            t.daemon = True
            t.start()

    def _wait(self):
        code = self.proc.wait()
        self.exit_code = 128 - code if code < 0 else code
        self.ended = time.time()
        self.done.set()
        audit({"a": "done", "on": NAME, "job": self.id, "exit": self.exit_code,
               "secs": round(self.ended - self.started, 1)})

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
        return {"job": self.id, "done": self.done.is_set(), "exit_code": self.exit_code,
                "killed": self.killed, "seconds": round((self.ended or time.time()) - self.started, 1),
                "output": output, "output_bytes": size, "next_offset": size, "truncated": truncated,
                "command": self.cmd if len(self.cmd) <= 300 else self.cmd[:300] + "...", "server": NAME}


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


def orphan_result(jid, offset=0):
    """A job from before the agent restarted: only its log is left."""
    path = os.path.join(JOBS_DIR, jid + ".log")
    if not os.path.exists(path):
        return None
    output, size, truncated = read_log(path, offset)
    return {"job": jid, "done": None, "exit_code": None, "killed": False, "seconds": None,
            "output": output, "output_bytes": size, "next_offset": size, "truncated": truncated,
            "command": "", "server": NAME,
            "note": "the agent restarted since this job started; its exit status is unknown"}


def format_result(r, next_hint):
    out = r.get("output") or ""
    if out and not out.endswith("\n"):
        out += "\n"
    server = r.get("server")
    where = "%s · " % server if server and not (ROLE == "hub" and server == NAME) else ""
    if r.get("done"):
        tail = "[%sexit %s%s · %ss]" % (where, r["exit_code"], " (killed)" if r.get("killed") else "", r["seconds"])
    elif r.get("done") is None:
        tail = "[%sstatus unknown: %s]" % (where, r.get("note", ""))
    else:
        tail = "[%sstill running after %ss · job %s · %s]" % (where, r["seconds"], r["job"], next_hint)
    return out + tail + "\n"


# ============================================================================= files

def check_path(path):
    if not path or not isinstance(path, str):
        raise OpError(400, "path is required")
    path = os.path.expanduser(path)
    if not path.startswith("/"):
        raise OpError(400, "path must be absolute")
    return path


def parse_mode(value):
    if value in (None, ""):
        return None
    try:
        mode = int(str(value), 8)
    except ValueError:
        raise OpError(400, "mode must be octal, like 644")
    if not 0 <= mode <= 0o7777:
        raise OpError(400, "mode must be octal, like 644")
    return mode


def list_dir_json(path, limit=3000):
    try:
        names = os.listdir(path)
    except OSError as e:
        raise OpError(403 if e.errno == errno.EACCES else 404, "cannot list %s: %s" % (path, e.strerror))
    entries = []
    for name in names[:limit]:
        full = os.path.join(path, name)
        try:
            st = os.lstat(full)
        except OSError:
            continue
        islink = os.path.islink(full)
        entries.append({"name": name, "dir": os.path.isdir(full), "link": os.readlink(full) if islink else None,
                        "size": st.st_size, "mode": oct(st.st_mode & 0o7777)[2:].rjust(4, "0"),
                        "mtime": int(st.st_mtime)})
    entries.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return {"path": path, "parent": os.path.dirname(path.rstrip("/")) or "/", "entries": entries,
            "truncated": len(names) > limit, "server": NAME}


def list_dir_text(d):
    lines = []
    for e in d["entries"]:
        kind = "l" if e["link"] else ("d" if e["dir"] else "-")
        lines.append("%s %s %12d %s %s%s" % (
            kind, e["mode"], e["size"], time.strftime("%Y-%m-%d %H:%M", time.localtime(e["mtime"])),
            e["name"], " -> " + e["link"] if e["link"] else ("/" if e["dir"] else "")))
    return "%s (%d entries)\n%s\n" % (d["path"], len(lines), "\n".join(lines))


def finish_write(tmp, path, mode):
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
        finish_write(tmp, path, mode)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return path, total


def part_path(path):
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path))[:80]
    return os.path.join(os.path.dirname(path) or "/", ".fatllm-%s.part" % base)


# ============================================================================= ops (local)

def op_run(a, ctx):
    cmd = a.get("cmd") or a.get("command") or ""
    if not isinstance(cmd, str) or not cmd.strip():
        raise OpError(400, "empty command")
    cwd = os.path.expanduser(a.get("cwd") or "~")
    if not os.path.isdir(cwd):
        raise OpError(400, "cwd does not exist: %s" % cwd)
    job = None
    jid = a.get("id") or None
    if jid is not None:
        jid = str(jid).lower()
        if not JOB_ID.match(jid):
            raise OpError(400, "id must be 8-32 lowercase hex characters")
        with LOCK:
            job = JOBS.get(jid)
        if job is None and os.path.exists(os.path.join(JOBS_DIR, jid + ".log")):
            return orphan_result(jid)          # a retry after the agent restarted
    if job is None:
        prune_jobs()
        with LOCK:
            if sum(1 for j in JOBS.values() if not j.done.is_set()) >= MAX_RUNNING:
                raise OpError(429, "too many commands running (%d); wait or kill some" % MAX_RUNNING)
            job = Job(cmd, cwd, jid, ctx.get("who", ""))
            JOBS[job.id] = job
        audit({"a": "run", "on": NAME, "job": job.id, "ip": ctx.get("ip"), "key": ctx.get("who"),
               "cwd": cwd, "cmd": cmd})
        try:
            job.start(a.get("stdin"), clamp(a.get("timeout", 0), 0, 7 * 86400, 0))
        except Exception as e:
            with LOCK:
                JOBS.pop(job.id, None)
            raise OpError(500, "could not start the command: %s" % e)
    job.done.wait(clamp(a.get("wait", DEFAULT_WAIT), 0, MAX_WAIT, DEFAULT_WAIT))
    return job.result(0)


def op_job(a, ctx):
    jid = str(a.get("id") or "").lower()
    if not JOB_ID.match(jid):
        raise OpError(400, "bad job id")
    offset = clamp(a.get("offset", 0), 0, 1 << 62, 0)
    with LOCK:
        job = JOBS.get(jid)
    if job:
        if truthy(a.get("kill", "")):
            audit({"a": "kill", "on": NAME, "job": jid, "ip": ctx.get("ip"), "key": ctx.get("who")})
            job.kill()
        job.done.wait(clamp(a.get("wait", 30), 0, MAX_WAIT, 30))
        return job.result(offset)
    r = orphan_result(jid, offset)
    if r is None:
        raise OpError(404, "no such job (finished jobs are kept %d hours)" % (KEEP_DONE_JOBS // 3600))
    return r


def op_jobs(a, ctx):
    with LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: j.started, reverse=True)[:50]
    return {"server": NAME, "jobs": [{
        "id": j.id, "done": j.done.is_set(), "exit_code": j.exit_code, "killed": j.killed,
        "started": int(j.started), "seconds": round((j.ended or time.time()) - j.started, 1),
        "command": j.cmd if len(j.cmd) <= 300 else j.cmd[:300] + "..."} for j in jobs]}


def op_stat(a, ctx):
    path = check_path(a.get("path"))
    if os.path.isdir(path):
        return {"type": "dir", "path": path}
    try:
        st = os.stat(path)
    except OSError as e:
        raise OpError(404 if e.errno == errno.ENOENT else 403, "cannot read %s: %s" % (path, e.strerror))
    return {"type": "file", "path": path, "size": st.st_size}


def op_list(a, ctx):
    return list_dir_json(check_path(a.get("path")))


def op_read(a, ctx):
    path = check_path(a.get("path"))
    offset = clamp(a.get("offset", 0), 0, 1 << 62, 0)
    length = clamp(a.get("length", CHUNK), 0, CHUNK, CHUNK)
    try:
        with open(path, "rb") as f:
            size = os.fstat(f.fileno()).st_size
            f.seek(offset)
            data = f.read(length)
    except OSError as e:
        raise OpError(404 if e.errno == errno.ENOENT else 403, "cannot read %s: %s" % (path, e.strerror))
    if truthy(a.get("log", "1")) and offset == 0:
        audit({"a": "read", "on": NAME, "path": path, "ip": ctx.get("ip"), "key": ctx.get("who")})
    return {"path": path, "size": size, "offset": offset, "data": b64e(data),
            "eof": offset + len(data) >= size}


def op_write(a, ctx):
    """Write bytes. part=True collects a big upload next to the target first (see op_commit)."""
    path = check_path(a.get("path"))
    data = b64d(a.get("data") or "")
    mode = parse_mode(a.get("mode"))
    try:
        if truthy(a.get("part", "")):
            target = part_path(path)
            os.makedirs(os.path.dirname(target) or "/", exist_ok=True)
            with open(target, "ab" if truthy(a.get("append", "")) else "wb") as f:
                f.write(data)
            return {"path": path, "part": True, "bytes": len(data)}
        path, n = write_file(path, [data], mode, truthy(a.get("append", "")))
    except OSError as e:
        raise OpError(403, "cannot write %s: %s" % (path, e.strerror or e))
    audit({"a": "write", "on": NAME, "path": path, "bytes": n, "ip": ctx.get("ip"), "key": ctx.get("who")})
    return {"path": path, "bytes": n}


def op_commit(a, ctx):
    path = check_path(a.get("path"))
    if os.path.islink(path):
        path = os.path.realpath(path)
    tmp = part_path(path)
    if not os.path.exists(tmp):
        raise OpError(400, "nothing uploaded for %s" % path)
    n = os.path.getsize(tmp)
    try:
        finish_write(tmp, path, parse_mode(a.get("mode")))
    except OSError as e:
        raise OpError(403, "cannot write %s: %s" % (path, e.strerror or e))
    audit({"a": "write", "on": NAME, "path": path, "bytes": n, "ip": ctx.get("ip"), "key": ctx.get("who")})
    return {"path": path, "bytes": n}


def op_info(a, ctx):
    s = stats()
    s["timezone"] = time.strftime("%Z")
    s["time_utc"] = iso()
    return s


def op_stats(a, ctx):
    return stats()


def op_audit(a, ctx):
    return {"entries": read_audit(clamp(a.get("n", 300), 1, 3000, 300))}


def op_ping(a, ctx):
    return {"pong": True, "name": NAME, "version": VERSION}


def op_update(a, ctx):
    """Replace this node's program with the hub's and restart (node only)."""
    files = a.get("files") or {}
    code = b64d(files.get("agent.py", ""))
    if not code:
        raise OpError(400, "no agent code")
    try:
        compile(code, "agent.py", "exec")
    except SyntaxError as e:
        raise OpError(400, "the new agent does not compile: %s" % e)
    targets = {"agent.py": os.path.abspath(__file__), "ui.sh": UI_FILE,
               "fatllm": ENV("FATLLM_CLI_DEST", "/usr/local/bin/fatllm")}
    for name, dest in targets.items():
        if name not in files:
            continue
        tmp = dest + ".new"
        with open(tmp, "wb") as f:
            f.write(b64d(files[name]))
        os.chmod(tmp, 0o755 if name in ("agent.py", "fatllm") else 0o644)
        os.replace(tmp, dest)
    audit({"a": "update", "on": NAME, "key": ctx.get("who")})
    detached("sleep 1; " + RESTART)
    return {"updated": True, "from": VERSION}


def op_set_hub(a, ctx):
    """The hub moved (new port, address or certificate): remember it and reconnect (node only)."""
    url = str(a.get("url") or "").rstrip("/")
    if not re.match(r"^https?://[^/\s]+$", url):
        raise OpError(400, "bad hub address")
    ca = str(a.get("ca") or "")
    if ca:
        if "BEGIN CERTIFICATE" not in ca:
            raise OpError(400, "bad certificate")
        dest = os.path.join(os.path.dirname(CONF_PATH), "hub.pem")
        with open(dest + ".tmp", "w") as f:
            f.write(ca)
        os.chmod(dest + ".tmp", 0o600)
        os.replace(dest + ".tmp", dest)
        persist_conf("FATLLM_HUB_CA", dest)
    if not persist_conf("FATLLM_HUB", url):
        raise OpError(500, "no settings file on this server")
    audit({"a": "hub-moved", "on": NAME, "detail": url})
    detached("sleep 2; " + RESTART)
    return {"hub": url}


def op_uninstall(a, ctx):
    audit({"a": "uninstall", "on": NAME, "key": ctx.get("who")})
    detached("sleep 2; systemctl disable --now fatllm; rm -f /etc/systemd/system/fatllm.service; "
             "systemctl daemon-reload; rm -rf /opt/fatllm /etc/fatllm /var/lib/fatllm /var/log/fatllm "
             "/usr/local/bin/fatllm")
    return {"removed": True}


OPS = {"run": op_run, "job": op_job, "jobs": op_jobs, "stat": op_stat, "list": op_list, "read": op_read,
       "write": op_write, "commit": op_commit, "info": op_info, "stats": op_stats, "audit": op_audit,
       "ping": op_ping, "update": op_update, "set_hub": op_set_hub, "uninstall": op_uninstall}
NODE_ONLY_OPS = ("update", "set_hub", "uninstall")


def local_op(op, args, ctx):
    fn = OPS.get(op)
    if fn is None:
        raise OpError(400, "unknown operation %s" % op)
    if op in NODE_ONLY_OPS and ROLE != "node":
        raise OpError(400, "%s is only for nodes" % op)
    return fn(args or {}, ctx or {})


# ============================================================================= hub <-> node link

class NodeLink(object):
    def __init__(self, name):
        self.name = name
        self.cond = threading.Condition()
        self.queue = deque()
        self.pending = {}
        self.last_seen = 0
        self.polling = 0
        self.info = {}

    def online(self):
        # last_seen moves only when we actually hear from the node (a poll arrives, a result
        # arrives), never because the hub itself is still holding an old poll open.
        return time.time() - self.last_seen < NODE_ONLINE


LINKS = {}
LINKS_LOCK = threading.Lock()
REMOTE_DONE = deque(maxlen=2000)
SEEN_RUNS = deque(maxlen=2000)
OP_TIMEOUTS = {"run": 30, "job": 30, "read": 180, "write": 180, "commit": 60, "update": 60}


def link_for(name):
    with LINKS_LOCK:
        lk = LINKS.get(name)
        if lk is None:
            lk = LINKS[name] = NodeLink(name)
        return lk


def remote_call(name, op, args, ctx):
    if STORE.node(name) is None:
        raise OpError(404, "no server named %s" % name)
    lk = link_for(name)
    if not lk.online():
        ago = "never connected" if not lk.last_seen else "last seen %ds ago" % (time.time() - lk.last_seen)
        raise OpError(503, "server %s is offline (%s); it reconnects by itself when it can reach the hub"
                      % (name, ago))
    timeout = OP_TIMEOUTS.get(op, 45)
    if op in ("run", "job"):
        timeout += clamp(args.get("wait", DEFAULT_WAIT if op == "run" else 30), 0, MAX_WAIT, DEFAULT_WAIT)
    rid = secrets.token_hex(8)
    item = {"rid": rid, "op": op, "args": args,
            "ctx": {"ip": ctx.get("ip"), "who": "hub:%s" % ctx.get("who", "")}}
    ev, box = threading.Event(), {}
    with lk.cond:
        lk.pending[rid] = (ev, box)
        lk.queue.append(item)
        lk.cond.notify_all()
    deadline = time.time() + timeout
    while not ev.wait(1.0):
        if time.time() > deadline or not lk.online():
            break
    if not ev.is_set():
        with lk.cond:
            lk.pending.pop(rid, None)
            try:
                lk.queue.remove(item)
                taken = False
            except ValueError:
                taken = True
        if not lk.online():
            raise OpError(503, "server %s went offline%s; it reconnects by itself (ask again with the "
                               "same job id)" % (name, " while working on this" if taken else ""))
        raise OpError(504, "server %s did not answer within %ds (a command keeps running there; "
                           "ask for its job again)" % (name, timeout))
    if not box.get("ok"):
        raise OpError(int(box.get("code") or 500), box.get("error") or "the server reported an error")
    return box.get("data")


def call(server, op, args, ctx):
    """Run an op on this hub or on one of its nodes; results say which server answered."""
    if not server or server == NAME:
        return local_op(op, args, ctx)
    if op == "run":
        # The hub names the job, so a retry after a dropped connection finds the same one and the
        # activity log shows each command once.
        cmd = args.get("cmd") or ""
        if not isinstance(cmd, str) or not cmd.strip():
            raise OpError(400, "empty command")
        args = dict(args)
        args["id"] = str(args.get("id") or secrets.token_hex(4)).lower()
        if not JOB_ID.match(args["id"]):
            raise OpError(400, "id must be 8-32 lowercase hex characters")
        if (server, args["id"]) not in SEEN_RUNS:
            SEEN_RUNS.append((server, args["id"]))
            audit({"a": "run", "on": server, "job": args["id"], "ip": ctx.get("ip"), "key": ctx.get("who"),
                   "cwd": args.get("cwd") or "~", "cmd": cmd})
    data = remote_call(server, op, args, ctx)
    if isinstance(data, dict):
        data["server"] = server
        if op in ("run", "job") and data.get("done") and (server, data.get("job")) not in REMOTE_DONE:
            REMOTE_DONE.append((server, data.get("job")))
            audit({"a": "done", "on": server, "job": data.get("job"), "exit": data.get("exit_code"),
                   "secs": data.get("seconds")})
    return data


def servers():
    out = [dict(brief(), name=NAME, role="hub", online=True, self=True)]
    d = STORE.data()
    joining = {j["name"] for j in d["joins"] if j.get("expires", 0) > now()}
    for n in d["nodes"]:
        lk = link_for(n["name"])
        e = {"name": n["name"], "role": "node", "online": lk.online(), "last_seen": int(lk.last_seen) or None,
             "created": n.get("created"), "joining": n["name"] in joining and not lk.last_seen}
        e.update({k: v for k, v in (lk.info or {}).items() if k in (
            "hostname", "os", "kernel", "arch", "cpus", "version", "mem_total", "uptime_s")})
        out.append(e)
    for j in d["joins"]:
        if j.get("expires", 0) > now() and not any(s["name"] == j["name"] for s in out):
            out.append({"name": j["name"], "role": "node", "online": False, "joining": True})
    return out


# ============================================================================= guide and MCP

def guide(base):
    lines = []
    for s in servers():
        state = "this hub" if s.get("self") else ("online" if s["online"] else "OFFLINE")
        lines.append("  %-12s %-8s %s" % (s["name"], state, s.get("os") or ""))
    return """FATLLM: full root shell on the owner's servers. The owner installed this so an AI agent can
run and manage these servers for them. Work on your own: run what the task needs and do not stop
to ask for approval of routine steps. Ask first only before destroying data that cannot be
recovered, or before changing SSH/firewall/network settings in a way that could lock the owner
out. The key below is a secret: never print it in full, never commit it.

KEY = {base}

SERVERS (pick one with ?on=NAME, srv --on NAME, or the MCP "server" argument; default: {hub})
{servers}

EASIEST: a small client with the key built in
  curl -fsSk '{base}/srv' -o ~/srv && chmod +x ~/srv && ~/srv servers
  ~/srv 'uptime && df -h /'              run on the hub ({hub})
  ~/srv --on NAME 'curl -s https://x'    run on another server
  ~/srv - <<'EOF'                        run a multi-line script
  apt-get update -q
  EOF
  ~/srv servers | info | jobs | job ID | kill ID | get PATH [LOCAL] | put LOCAL REMOTE
  srv retries by itself when the connection drops; a command is never started twice.

RAW HTTP (always --data-binary, never -d, so newlines survive; -k: self-signed certificate)
  curl -sk '{base}/run?on=NAME&id=RANDOMHEX' --data-binary 'uptime'
  Answer: stdout+stderr, last line "[exit N · Ts]" (also header X-Exit-Code).
  Commands run as root with bash -c, stdin=/dev/null, DEBIAN_FRONTEND=noninteractive.
  A call waits up to {wait}s (?wait=0..{maxwait}). Still running -> "[still running ... job ID]":
    curl -sk '{base}/job/ID?on=NAME&wait=60'          output so far + status
    curl -sk -X POST '{base}/job/ID/kill?on=NAME'      stop it
  Pass your own id (8-32 hex) with ?id= so that a retry after a dropped connection returns the
  same job instead of running the command again. ?timeout=SECONDS kills it after that.
  JSON: Content-Type application/json, {{"cmd": "...", "on": "NAME", "cwd": "...", "stdin": "..."}}.
FILES: curl -sk '{base}/file?on=NAME&path=/etc/hostname'   |   curl -sk -T ./f '{base}/file?on=NAME&path=/root/f'
MCP:  {base}/mcp  (tools list_servers, run_command, job_status, read_file, write_file, server_info)

TIPS: use non-interactive flags (apt-get -y); output over {maxout} bytes is cut in the middle (the
full log path is shown); commands keep running if the agent restarts. Owner commands on the hub:
fatllm, fatllm key, fatllm add-server NAME, fatllm rotate, fatllm off.
""".format(base=base, hub=NAME, servers="\n".join(lines), wait=DEFAULT_WAIT, maxwait=MAX_WAIT,
           maxout=MAX_OUTPUT)


MCP_INSTRUCTIONS = (
    "Full root shell on the owner's servers, granted so you can run and manage them for the owner. "
    "Work on your own without asking for approval of routine steps; ask first only before destroying "
    "data that cannot be recovered or before changing SSH/firewall/network settings that could lock the "
    "owner out. list_servers shows the servers; every tool takes an optional 'server' (default: the hub). "
    "Long commands keep running: when a job id comes back, poll it with job_status.")

SERVER_ARG = {"type": "string", "description": "which server (see list_servers); default: the hub"}
TOOLS = [
    {"name": "list_servers", "title": "List servers",
     "description": "The hub and the servers connected to it, with online status and OS.",
     "inputSchema": {"type": "object", "properties": {}},
     "annotations": {"title": "List servers", "readOnlyHint": True}},
    {"name": "run_command", "title": "Run a shell command",
     "description": ("Run a bash command or script as root on a server. Returns stdout+stderr and a last "
                     "line [exit N]. Non-interactive (use -y flags). If it is still running after "
                     "wait_seconds you get a job id: it keeps running, check it with job_status."),
     "inputSchema": {"type": "object", "properties": {
         "command": {"type": "string", "description": "bash command or script"},
         "server": SERVER_ARG,
         "cwd": {"type": "string", "description": "working directory (default /root)"},
         "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT,
                          "description": "how long to wait for it (default %d)" % DEFAULT_WAIT},
         "timeout_seconds": {"type": "integer", "minimum": 0, "description": "kill it after this long"},
         "stdin": {"type": "string", "description": "text given on stdin"}},
         "required": ["command"]},
     "annotations": {"title": "Run shell command", "readOnlyHint": False, "destructiveHint": True,
                     "openWorldHint": True}},
    {"name": "job_status", "title": "Check or stop a running command",
     "description": "Output so far and status of a command that was still running. kill=true stops it.",
     "inputSchema": {"type": "object", "properties": {
         "job_id": {"type": "string"}, "server": SERVER_ARG,
         "wait_seconds": {"type": "integer", "minimum": 0, "maximum": MAX_WAIT},
         "kill": {"type": "boolean"}}, "required": ["job_id"]},
     "annotations": {"title": "Job status", "readOnlyHint": False, "destructiveHint": False}},
    {"name": "read_file", "title": "Read a file",
     "description": "Read a text file (or list a directory) on a server.",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "absolute path"}, "server": SERVER_ARG,
         "offset": {"type": "integer", "minimum": 0},
         "max_bytes": {"type": "integer", "minimum": 1, "maximum": CHUNK},
         "encoding": {"type": "string", "enum": ["text", "base64"]}}, "required": ["path"]},
     "annotations": {"title": "Read file", "readOnlyHint": True}},
    {"name": "write_file", "title": "Write a file",
     "description": "Create or replace a file (parents created, atomic; an existing file keeps its owner and mode).",
     "inputSchema": {"type": "object", "properties": {
         "path": {"type": "string", "description": "absolute path"}, "content": {"type": "string"},
         "server": SERVER_ARG, "encoding": {"type": "string", "enum": ["text", "base64"]},
         "append": {"type": "boolean"}, "mode": {"type": "string", "description": "octal, e.g. 644"}},
         "required": ["path", "content"]},
     "annotations": {"title": "Write file", "readOnlyHint": False, "destructiveHint": True}},
    {"name": "server_info", "title": "Server facts",
     "description": "Hostname, OS, CPU, memory, disk, uptime and load of a server.",
     "inputSchema": {"type": "object", "properties": {"server": SERVER_ARG}},
     "annotations": {"title": "Server info", "readOnlyHint": True}},
]


class McpError(Exception):
    def __init__(self, code, message):
        Exception.__init__(self, message)
        self.code = code
        self.message = message


def mcp_call(params, ctx):
    name = params.get("name")
    args = params.get("arguments") or {}
    if not isinstance(args, dict):
        raise McpError(-32602, "arguments must be an object")

    def text(t, error=False):
        return {"content": [{"type": "text", "text": t}], "isError": error}

    server = args.get("server") or None
    try:
        if server and server != NAME and STORE.node(server) is None:
            raise OpError(404, "no server named %s (see list_servers)" % server)
        if name == "list_servers":
            return text(json.dumps(servers(), indent=1, ensure_ascii=False))
        if name == "run_command":
            r = call(server, "run", {"cmd": args.get("command"), "cwd": args.get("cwd"),
                                     "stdin": args.get("stdin"), "timeout": args.get("timeout_seconds", 0),
                                     "wait": args.get("wait_seconds", DEFAULT_WAIT)}, ctx)
            return text(format_result(r, 'check it with job_status(job_id="%s"%s)' % (
                r["job"], ', server="%s"' % server if server else "")))
        if name == "job_status":
            r = call(server, "job", {"id": args.get("job_id"), "wait": args.get("wait_seconds", 30),
                                     "kill": args.get("kill", False)}, ctx)
            return text(format_result(r, 'check again with job_status(job_id="%s")' % r["job"]))
        if name == "read_file":
            path = args.get("path")
            st = call(server, "stat", {"path": path}, ctx)
            if st["type"] == "dir":
                return text(list_dir_text(call(server, "list", {"path": path}, ctx)))
            offset = clamp(args.get("offset", 0), 0, 1 << 62, 0)
            r = call(server, "read", {"path": path, "offset": offset,
                                      "length": clamp(args.get("max_bytes", 200000), 1, CHUNK, 200000)}, ctx)
            data = b64d(r["data"])
            more = "" if r["eof"] else "\n[showing bytes %d-%d of %d; read more with offset=%d]" % (
                offset, offset + len(data), r["size"], offset + len(data))
            if args.get("encoding") == "base64":
                return text(b64e(data) + more)
            if b"\0" in data[:8192]:
                return text("binary file (%d bytes); read it with encoding=base64" % r["size"])
            return text(data.decode("utf-8", "replace") + more)
        if name == "write_file":
            content = args.get("content")
            if not isinstance(content, str):
                raise OpError(400, "content must be a string")
            data = b64d(content) if args.get("encoding") == "base64" else content.encode("utf-8")
            r = call(server, "write", {"path": args.get("path"), "data": b64e(data), "mode": args.get("mode"),
                                       "append": args.get("append", False)}, ctx)
            return text("wrote %d bytes to %s on %s" % (r["bytes"], r["path"], server or NAME))
        if name == "server_info":
            return text(json.dumps(call(server, "info", {}, ctx), indent=1, ensure_ascii=False))
    except OpError as e:
        return text("error: %s" % e.message, True)
    except (ValueError, TypeError) as e:
        return text("error: %s" % e, True)
    raise McpError(-32602, "Unknown tool: %s" % name)


def mcp_one(m, ctx):
    method = m.get("method")
    params = m.get("params") or {}
    is_request = "id" in m
    try:
        if method == "initialize":
            asked = params.get("protocolVersion")
            result = {"protocolVersion": asked if asked in MCP_VERSIONS else MCP_VERSIONS[0],
                      "capabilities": {"tools": {"listChanged": False}},
                      "serverInfo": {"name": "fatllm", "title": "FATLLM server access", "version": VERSION},
                      "instructions": MCP_INSTRUCTIONS}
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "tools/call":
            result = mcp_call(params, ctx)
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


# ============================================================================= join (node install)

JOIN_SCRIPT = r'''#!/usr/bin/env bash
# FATLLM: join this server to the hub @@HUB@@ as "@@NAME@@".
# It connects OUT to the hub, so this server needs no open port.
main() {
set -euo pipefail
HUB='@@HUB@@' JOIN='@@JOIN@@' NAME='@@NAME@@' NODE_TOKEN='@@NODE_TOKEN@@' PIN='@@PIN@@'
if [ -t 1 ]; then G=$'\033[38;5;86m' C=$'\033[38;5;117m' Y=$'\033[38;5;221m' RD=$'\033[38;5;210m' B=$'\033[1m' D=$'\033[2m' R=$'\033[0m'
else G="" C="" Y="" RD="" B="" D="" R=""; fi
ok() { printf '  %s✓%s %s\n' "$G" "$R" "$*"; }
say() { printf '  %s›%s %s\n' "$C" "$R" "$*"; }
warn() { printf '  %s! %s%s\n' "$Y" "$*" "$R" >&2; }
die() { printf '\n  %s✗ %s%s\n\n' "$RD" "$*" "$R" >&2; exit 1; }
printf '\n  %sFATLLM%s  %sjoin "%s" to %s%s\n\n' "$B$C" "$R" "$D" "$NAME" "$HUB" "$R"
[ "$(id -u)" = 0 ] || die "run as root: type 'sudo -i', then paste the command again"
command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] || die "this server has no systemd"
if ! command -v python3 >/dev/null 2>&1; then
  say "Installing python3"
  if command -v apt-get >/dev/null 2>&1; then apt-get update -qq </dev/null >/dev/null 2>&1 || true
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 </dev/null >/dev/null
  elif command -v dnf >/dev/null 2>&1; then dnf install -y -q python3 </dev/null >/dev/null
  elif command -v yum >/dev/null 2>&1; then yum install -y -q python3 </dev/null >/dev/null
  elif command -v apk >/dev/null 2>&1; then apk add -q python3 </dev/null >/dev/null
  else die "install python3, then paste the command again"; fi
fi
python3 -c 'import sys; sys.exit(sys.version_info < (3, 6))' || die "python3 is older than 3.6"
CURL=(curl -fsS --retry 3 --max-time 120)
[ -n "$PIN" ] && CURL+=(-k --pinnedpubkey "$PIN")
fetch() { "${CURL[@]}" "$HUB/$JOIN/$1" -o "$2" </dev/null || die "could not download $1 from the hub"; }
if [ -f /etc/fatllm/fatllm.env ] && grep -q '^FATLLM_ROLE=hub' /etc/fatllm/fatllm.env; then
  die "this server is a hub itself; to join another hub, run 'fatllm uninstall' here first"
fi
if [ -f /etc/fatllm/fatllm.env ] && ! grep -q '^FATLLM_ROLE=node' /etc/fatllm/fatllm.env; then
  bak="/etc/fatllm.bak-$(date +%s)"
  systemctl disable --now fatllm fatllm-caddy fatllm-tunnel >/dev/null 2>&1 </dev/null || true
  for u in fatllm-caddy fatllm-tunnel; do rm -f "/etc/systemd/system/$u.service"; done
  cp -a /etc/fatllm "$bak"
  warn "An old FATLLM (version 1) was replaced."
  warn "Backup: $bak"
fi
systemctl stop fatllm >/dev/null 2>&1 </dev/null || true
install -d -m 755 /opt/fatllm
install -d -m 700 /etc/fatllm /var/lib/fatllm /var/log/fatllm
fetch agent.py /opt/fatllm/agent.py.new
python3 -m py_compile /opt/fatllm/agent.py.new || die "the downloaded agent is broken; try again"
mv /opt/fatllm/agent.py.new /opt/fatllm/agent.py; chmod 755 /opt/fatllm/agent.py
fetch ui.sh /opt/fatllm/ui.sh
fetch fatllm /usr/local/bin/fatllm.new && chmod 755 /usr/local/bin/fatllm.new && mv /usr/local/bin/fatllm.new /usr/local/bin/fatllm
CA=""
if [ -n "$PIN" ]; then fetch hub.pem /etc/fatllm/hub.pem; CA=/etc/fatllm/hub.pem; fi
umask 077
cat > /etc/fatllm/fatllm.env <<EOF
# FATLLM node settings (root only)
FATLLM_ROLE=node
FATLLM_NAME=$NAME
FATLLM_HUB=$HUB
FATLLM_NODE_TOKEN=$NODE_TOKEN
FATLLM_HUB_CA=$CA
FATLLM_STATE_DIR=/var/lib/fatllm
FATLLM_LOG_DIR=/var/log/fatllm
EOF
umask 022
cat > /etc/systemd/system/fatllm.service <<EOF
[Unit]
Description=FATLLM node (connects out to its hub)
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
EnvironmentFile=/etc/fatllm/fatllm.env
ExecStart=$(command -v python3) /opt/fatllm/agent.py
Restart=always
RestartSec=3
KillMode=process

[Install]
WantedBy=multi-user.target
EOF
rm -f /var/lib/fatllm/node.json
systemctl daemon-reload </dev/null
systemctl enable fatllm >/dev/null 2>&1 </dev/null
systemctl restart fatllm </dev/null
for _ in $(seq 1 40); do
  if grep -q '"connected": true' /var/lib/fatllm/node.json 2>/dev/null; then
    printf '\n'; ok "Connected to the hub as \"$NAME\"."; ok "Your AI can use it now: --on $NAME"
    printf '    %sManage it here: fatllm%s\n\n' "$D" "$R"; return 0
  fi
  sleep 0.5
done
warn "Installed; not connected yet. It keeps trying."
warn "This server must reach $HUB"
warn "Check with: fatllm status"
}
main "$@"
'''


def join_script(join, hub_url):
    """The installer a new server downloads with its join token. Re-issues the node's token."""
    node_token = new_token("node_")

    def upd(d):
        d["nodes"] = [n for n in d["nodes"] if n["name"] != join["name"]]
        d["nodes"].append({"name": join["name"], "token": node_token, "created": now()})
    STORE.modify(upd)
    with LINKS_LOCK:
        LINKS.pop(join["name"], None)
    pin = cert_pin(TLS_CERT) if MODE == "direct" and TLS_CERT else ""
    s = JOIN_SCRIPT
    for k, v in (("@@HUB@@", hub_url), ("@@JOIN@@", join["token"]), ("@@NAME@@", join["name"]),
                 ("@@NODE_TOKEN@@", node_token), ("@@PIN@@", pin)):
        s = s.replace(k, v)
    return s


def join_command(join_token, hub_url):
    pin = cert_pin(TLS_CERT) if MODE == "direct" and TLS_CERT else ""
    if pin:
        return "curl -fsSk --pinnedpubkey '%s' %s/%s | bash" % (pin, hub_url, join_token)
    return "curl -fsS %s/%s | bash" % (hub_url, join_token)


def parse_duration(value):
    """'30m' / '12h' / '7d' -> epoch seconds from now; 'never' -> 0."""
    v = str(value or "").strip().lower()
    if v in ("never", "0", ""):
        return 0
    m = re.match(r"^(\d+)\s*([mhd])$", v)
    if not m:
        raise OpError(400, "use 30m, 12h, 7d or never")
    secs = int(m.group(1)) * {"m": 60, "h": 3600, "d": 86400}[m.group(2)]
    if secs <= 0 or secs > 3650 * 86400:
        raise OpError(400, "duration out of range")
    return now() + secs


# ============================================================================= HTTP

class BodyTooLarge(Exception):
    pass


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


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "fatllm"
    sys_version = ""
    timeout = 120  # per socket read/write, so a stalled client cannot hold a thread for ever

    def log_message(self, fmt, *args):  # request lines carry the key: never log them
        pass

    # --- plumbing
    def client_ip(self):
        ip = self.client_address[0]
        if ip in ("127.0.0.1", "::1"):
            xff = self.headers.get("X-Forwarded-For")
            if xff:
                return xff.split(",")[-1].strip()
        return ip

    def drain(self):
        """Read what is left of the request body. Closing a socket with unread data makes TCP send
        a reset, and the client would see 'connection reset' instead of our answer."""
        if getattr(self, "_consumed", True):
            return
        self._consumed = True
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if 0 < n <= 4 * 1024 * 1024:
                self.rfile.read(n)
        except (OSError, ValueError):
            pass

    def send(self, code, body, ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.drain()
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
        self.send(code, json.dumps(obj, ensure_ascii=False, indent=1) + "\n", "application/json; charset=utf-8",
                  headers)

    def fail(self, e):
        if self.want_json:
            return self.send_json(e.code, {"error": e.message})
        return self.send(e.code, "error: %s\n" % e.message)

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
                    self._consumed = True
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
            self._consumed = True

    def read_body(self, limit):
        return b"".join(self.body_chunks(limit))

    def read_json(self, limit=1 << 20):
        raw = self.read_body(limit)
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except ValueError:
            raise OpError(400, "body is not JSON")
        if not isinstance(data, dict):
            raise OpError(400, "body must be a JSON object")
        return data

    def authorize(self):
        """(kind, record, token, path after the token)."""
        path = urlsplit(self.path).path
        parts = path.split("/", 2)
        first = parts[1] if len(parts) > 1 else ""
        kind, rec = STORE.principal(first)
        if kind:
            return kind, rec, first, "/" + (parts[2] if len(parts) > 2 else "")
        auth = self.headers.get("Authorization") or ""
        if auth[:7].lower() == "bearer ":
            tok = auth[7:].strip()
            kind, rec = STORE.principal(tok)
            if kind == "key":
                return kind, rec, tok, path
            cli = cli_token()
            local = (self.client_address[0] in ("127.0.0.1", "::1", "::ffff:127.0.0.1")
                     and not self.headers.get("X-Forwarded-For"))     # not through Caddy: really on this server
            if cli and local and same(tok, cli):
                return "key", {"name": "@server", "token": tok, "expires": 0}, tok, path
        return None, None, None, path

    def root(self):
        if PUBLIC_URL:
            return PUBLIC_URL
        host = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "%s:%d" % (LISTEN, PORT))
        host = host.split(",")[0].strip()
        local = host.startswith("127.") or host.startswith("localhost") or host.startswith("[::1]")
        default = "https" if (TLS_CERT or not local) else "http"
        proto = (self.headers.get("X-Forwarded-Proto") or default).split(",")[0].strip()
        return "%s://%s" % (proto, host)

    def base(self):
        return self.root() + "/" + self.token

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
        self.want_json = False
        self._consumed = False
        try:
            kind, rec, self.token, path = self.authorize()
            if not kind:
                # Guessing a 256-bit key is hopeless anyway; this only quiets scanners. A valid key
                # is never refused, even from an address that scanners share.
                if failed_too_often(ip):
                    return self.send(429, "too many attempts\n")
                note_failure(ip)
                return self.send(404, "not found\n")
            q = {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()}
            self.want_json = truthy(q.get("json", "")) or "application/json" in (self.headers.get("Content-Type") or "")
            if kind == "node":
                return self.route_node(rec, path)
            if kind == "join":
                return self.route_join(rec, path)
            LAST_USED[rec["name"]] = now()
            self.ctx = {"ip": ip, "who": rec["name"]}
            return self.route_key(rec, path, q)
        except OpError as e:
            return self.fail(e)
        except BodyTooLarge:
            return self.send(413, "body too large\n")
        except (BrokenPipeError, ConnectionResetError, socket.timeout):
            self.close_connection = True
        except Exception as e:  # keep serving; tell the caller what broke
            try:
                self.send(500, "agent error: %s: %s\n" % (type(e).__name__, e))
            except Exception:
                self.close_connection = True

    def target(self, q, body=None):
        on = (body or {}).get("on") or (body or {}).get("server") or q.get("on") or ""
        if on and on != NAME and STORE.node(on) is None:
            raise OpError(404, "no server named %s (GET /servers lists them)" % on)
        return on or None

    def route_key(self, key, path, q):
        m = self.command
        get = m in ("GET", "HEAD")
        if path in ("/", "/guide") and get:
            return self.send(200, guide(self.base()))
        if path in ("/panel", "/panel/") and get:
            return self.h_panel()
        if path == "/srv" and get:
            return self.h_client()
        if path == "/servers" and get:
            ss = servers()
            if self.want_json:
                return self.send_json(200, {"hub": NAME, "servers": ss})
            rows = []
            for s in ss:
                if s.get("self"):
                    state = "hub"
                elif s.get("online"):
                    state = "online"
                elif s.get("joining"):
                    state = "joining"
                else:
                    state = "OFFLINE"
                rows.append("%-14s %-8s %s" % (s["name"], state, s.get("os") or ""))
            return self.send(200, "%-14s %-8s %s\n%s\n" % ("NAME", "STATE", "OS", "\n".join(rows)))
        if path == "/run" and m == "POST":
            return self.h_run(q)
        jm = re.match(r"^/job/([0-9a-f]{8,32})(/kill)?/?$", path)
        if jm:
            if jm.group(2) and m in ("POST", "DELETE"):
                r = call(self.target(q), "job", {"id": jm.group(1), "kill": True, "wait": 5}, self.ctx)
                return self.reply_job(r, self.want_json)
            if not jm.group(2) and get:
                r = call(self.target(q), "job", {"id": jm.group(1), "offset": q.get("offset", 0),
                                                 "wait": q.get("wait", 30)}, self.ctx)
                return self.reply_job(r, self.want_json)
        if path == "/jobs" and get:
            r = call(self.target(q), "jobs", {}, self.ctx)
            if self.want_json:
                return self.send_json(200, r)
            lines = ["%s  %-8s  %7.1fs  %s" % (j["id"], ("exit %s" % j["exit_code"]) if j["done"] else "running",
                                               j["seconds"], j["command"].replace("\n", " ")[:120])
                     for j in r["jobs"]]
            return self.send(200, "\n".join(lines) + ("\n" if lines else "no jobs\n"))
        if path == "/file":
            if get:
                return self.h_get_file(q)
            if m in ("PUT", "POST"):
                return self.h_put_file(q)
        if path == "/info" and get:
            return self.send_json(200, call(self.target(q), "info", {}, self.ctx))
        if path == "/stats" and get:
            s = call(self.target(q), "stats", {}, self.ctx)
            s.update({"mode": MODE, "expires": key.get("expires", 0), "key_name": key["name"], "port": PORT,
                      "hub": NAME, "fatllm_cli": bool(shutil.which("fatllm") or os.path.exists(
                          "/usr/local/bin/fatllm"))})
            return self.send_json(200, s)
        if path == "/audit" and get:
            return self.send_json(200, {"entries": read_audit(clamp(q.get("n", 300), 1, 3000, 300),
                                                              q.get("on") or None)})
        if path in ("/mcp", "/mcp/"):
            if m == "POST":
                return self.h_mcp()
            return self.send(405, "use POST\n", headers={"Allow": "POST"})
        if path == "/admin/keys" and get:
            return self.send_json(200, {"keys": self.keys_view(key)})
        if path.startswith("/admin/") and m == "POST":
            return self.route_admin(key, path)
        return self.send(404, "unknown endpoint; read the guide: GET %s/\n" % self.base())

    # --- run and jobs
    def h_run(self, q):
        raw = self.read_body(MAX_COMMAND)
        params = {}
        if "application/json" in (self.headers.get("Content-Type") or ""):
            try:
                params = json.loads(raw.decode("utf-8") or "{}")
            except ValueError:
                raise OpError(400, "body is not a JSON object")
            if not isinstance(params, dict):
                raise OpError(400, "body is not a JSON object")
            cmd = params.get("cmd") or params.get("command") or ""
        else:
            cmd = raw.decode("utf-8", "replace")

        def opt(name, default=None):
            return params[name] if name in params else q.get(name, default)

        r = call(self.target(q, params), "run", {
            "cmd": cmd, "cwd": opt("cwd"), "stdin": params.get("stdin"), "timeout": opt("timeout", 0),
            "wait": opt("wait", DEFAULT_WAIT), "id": opt("id")}, self.ctx)
        return self.reply_job(r, self.want_json)

    def reply_job(self, r, want_json):
        headers = {"X-Job": r["job"], "X-Server": r.get("server") or NAME}
        if r.get("done"):
            headers["X-Exit-Code"] = str(r["exit_code"])
        if want_json:
            return self.send_json(200, r, headers)
        on = r.get("server") if r.get("server") not in (None, NAME) else ""
        hint = "check: curl -sk '%s/job/%s?wait=60%s'" % (self.base(), r["job"], "&on=" + on if on else "")
        return self.send(200, format_result(r, hint), headers=headers)

    # --- files
    def h_get_file(self, q):
        on = self.target(q)
        path = check_path(q.get("path"))
        st = call(on, "stat", {"path": path}, self.ctx)
        if st["type"] == "dir":
            d = call(on, "list", {"path": path}, self.ctx)
            return self.send_json(200, d) if self.want_json else self.send(200, list_dir_text(d))
        first = call(on, "read", {"path": path, "offset": 0, "length": CHUNK}, self.ctx)
        size = first["size"]
        self.drain()
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if truthy(q.get("download", "")):
            self.send_header("Content-Disposition",
                             "attachment; filename*=UTF-8''%s" % quote(os.path.basename(path) or "file"))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        sent, chunk = 0, first
        while True:
            data = b64d(chunk["data"])[:max(0, size - sent)]
            self.wfile.write(data)
            sent += len(data)
            if sent >= size or not data or chunk.get("eof"):
                break
            chunk = call(on, "read", {"path": path, "offset": sent, "length": CHUNK, "log": "0"}, self.ctx)

    def h_put_file(self, q):
        on = self.target(q)
        path = check_path(q.get("path"))
        mode = q.get("mode")
        append = truthy(q.get("append", ""))
        if not on or on == NAME:   # here: stream straight to disk
            path, n = write_file(path, self.body_chunks(), parse_mode(mode), append)
            audit({"a": "write", "on": NAME, "path": path, "bytes": n, "ip": self.ctx["ip"], "key": self.ctx["who"]})
            return self.send_json(200, {"path": path, "bytes": n, "server": NAME})
        ensure_dirs(SPOOL_DIR)  # another server: spool here, then send in chunks
        spool = os.path.join(SPOOL_DIR, secrets.token_hex(8))
        try:
            with open(spool, "wb") as f:
                for chunk in self.body_chunks():
                    f.write(chunk)
            size = os.path.getsize(spool)
            with open(spool, "rb") as f:
                if append:
                    while True:
                        data = f.read(CHUNK)
                        if not data:
                            break
                        call(on, "write", {"path": path, "data": b64e(data), "append": True, "mode": mode}, self.ctx)
                    return self.send_json(200, {"path": path, "bytes": size, "server": on})
                first = True
                while True:
                    data = f.read(CHUNK)
                    if not data and not first:
                        break
                    call(on, "write", {"path": path, "data": b64e(data), "part": True, "append": not first}, self.ctx)
                    first = False
                    if not data:
                        break
            r = call(on, "commit", {"path": path, "mode": mode}, self.ctx)
            return self.send_json(200, {"path": r["path"], "bytes": r["bytes"], "server": on})
        finally:
            try:
                os.unlink(spool)
            except OSError:
                pass

    # --- panel, client, MCP
    def h_panel(self):
        try:
            with open(PANEL_FILE, encoding="utf-8") as f:
                html = f.read()
        except OSError:
            return self.send(404, "panel not installed; run: fatllm update\n")
        nonce = secrets.token_urlsafe(16)
        html = html.replace("{{NONCE}}", nonce)
        csp = ("default-src 'none'; script-src 'nonce-%s'; style-src 'unsafe-inline'; connect-src 'self'; "
               "img-src 'self' data:; font-src 'self' data:; base-uri 'none'; form-action 'none'; "
               "frame-ancestors 'none'" % nonce)
        return self.send(200, html, "text/html; charset=utf-8", {"Content-Security-Policy": csp,
                                                                 "X-Frame-Options": "DENY"})

    def h_client(self):
        try:
            with open(CLIENT_SCRIPT) as f:
                script = f.read()
        except OSError:
            return self.send(404, "client script not installed\n")
        script = script.replace('BAKED_KEY=""', 'BAKED_KEY="%s"' % self.base(), 1)
        return self.send(200, script, "text/x-shellscript; charset=utf-8")

    def h_mcp(self):
        raw = self.read_body(MAX_JSON)
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:
            return self.send_json(400, {"jsonrpc": "2.0", "id": None,
                                        "error": {"code": -32700, "message": "Parse error"}})
        batch = isinstance(msg, list)
        replies = []
        for m in (msg if batch else [msg]):
            if isinstance(m, dict) and "method" in m:
                r = mcp_one(m, self.ctx)
                if r is not None:
                    replies.append(r)
        if not replies:
            self.drain()
            self.send_response(202)
            self.send_header("Content-Length", "0")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            return
        return self.send_json(200, replies if batch else replies[0])

    # --- admin (panel and the fatllm command)
    def keys_view(self, current):
        return [{"name": k["name"], "created": k.get("created"), "expires": k.get("expires", 0),
                 "last_used": LAST_USED.get(k["name"]), "current": k["name"] == current["name"]}
                for k in STORE.data()["keys"]]

    def route_admin(self, key, path):
        body = self.read_json()
        ip, who = self.ctx["ip"], self.ctx["who"]

        def set_key(name, field, value):
            def upd(d):
                for k in d["keys"]:
                    if k["name"] == name:
                        k[field] = value
                        if field == "token":
                            k["created"] = now()
                        return True
                return False
            if not STORE.modify(upd):
                raise OpError(404, "no key named %s" % name)

        if path == "/admin/expire":
            name = body.get("name") or key["name"]
            exp = parse_duration(body.get("in"))
            set_key(name, "expires", exp)
            audit({"a": "expire", "key": who, "ip": ip, "name": name, "until": exp})
            return self.send_json(200, {"name": name, "expires": exp})
        if path == "/admin/rotate":
            name = body.get("name") or key["name"]
            token = new_token("fat_")
            set_key(name, "token", token)
            audit({"a": "rotate", "key": who, "ip": ip, "name": name})
            return self.send_json(200, {"name": name, "key": self.root() + "/" + token})
        if path == "/admin/keys":
            name = str(body.get("name") or "").strip()
            if not NAME_RE.match(name):
                raise OpError(400, "a key name uses letters, digits, - and _ (up to 32)")
            token = new_token("fat_")

            def upd(d):
                if any(k["name"] == name for k in d["keys"]):
                    return False
                d["keys"].append({"name": name, "token": token, "created": now(), "expires": 0})
                return True
            if not STORE.modify(upd):
                raise OpError(409, "a key named %s already exists" % name)
            audit({"a": "key-add", "key": who, "ip": ip, "name": name})
            return self.send_json(200, {"name": name, "key": self.root() + "/" + token})
        if path == "/admin/keys/show":
            k = STORE.key(str(body.get("name") or ""))
            if not k:
                raise OpError(404, "no such key")
            return self.send_json(200, {"name": k["name"], "key": self.root() + "/" + k["token"]})
        if path == "/admin/keys/remove":
            name = str(body.get("name") or "")
            if name == key["name"] and not truthy(body.get("force", "")):
                raise OpError(409, "this is the key you are using; pass force to remove it anyway")

            def upd(d):
                left = [k for k in d["keys"] if k["name"] != name]
                if len(left) == len(d["keys"]):
                    return "missing"
                if not left:
                    return "last"
                d["keys"] = left
                return "ok"
            res = STORE.modify(upd)
            if res == "missing":
                raise OpError(404, "no key named %s" % name)
            if res == "last":
                raise OpError(409, "the last key cannot be removed")
            audit({"a": "key-remove", "key": who, "ip": ip, "name": name})
            return self.send_json(200, {"removed": name})
        if path == "/admin/off":
            fat = shutil.which("fatllm") or "/usr/local/bin/fatllm"
            if not os.path.exists(fat):
                raise OpError(409, "the fatllm command is not installed here")
            audit({"a": "off", "key": who, "ip": ip})
            self.send_json(200, {"ok": True})
            detached("sleep 2; exec %s off" % shlex.quote(fat))
            return
        if path == "/admin/servers":
            name = str(body.get("name") or "").strip()
            if not NAME_RE.match(name) or name == NAME:
                raise OpError(400, "a server name uses letters, digits, - and _ (up to 32), and is not '%s'" % NAME)
            token = new_token("join_")

            def upd(d):
                d["joins"] = [j for j in d["joins"] if j["name"] != name and j.get("expires", 0) > now()]
                d["joins"].append({"name": name, "token": token, "expires": now() + JOIN_TTL})
            STORE.modify(upd)
            audit({"a": "server-add", "key": who, "ip": ip, "name": name})
            return self.send_json(200, {"name": name, "command": join_command(token, self.root()),
                                        "expires": now() + JOIN_TTL})
        if path == "/admin/servers/remove":
            name = str(body.get("name") or "")
            result = {"removed": name}
            if truthy(body.get("uninstall", "")) and STORE.node(name) and link_for(name).online():
                try:
                    call(name, "uninstall", {}, self.ctx)
                    result["uninstalled"] = True
                except OpError as e:
                    result["uninstall_error"] = e.message

            def upd(d):
                before = len(d["nodes"]) + len(d["joins"])
                d["nodes"] = [n for n in d["nodes"] if n["name"] != name]
                d["joins"] = [j for j in d["joins"] if j["name"] != name]
                return before != len(d["nodes"]) + len(d["joins"])
            if not STORE.modify(upd):
                raise OpError(404, "no server named %s" % name)
            with LINKS_LOCK:
                LINKS.pop(name, None)
            audit({"a": "server-remove", "key": who, "ip": ip, "name": name})
            return self.send_json(200, result)
        if path == "/admin/servers/update":
            files = {}
            for fname, p in (("agent.py", os.path.abspath(__file__)), ("ui.sh", UI_FILE), ("fatllm", CLI_FILE)):
                try:
                    with open(p, "rb") as f:
                        files[fname] = b64e(f.read())
                except OSError:
                    pass
            out = {}
            for n in STORE.data()["nodes"]:
                if not link_for(n["name"]).online():
                    out[n["name"]] = "offline"
                    continue
                try:
                    call(n["name"], "update", {"files": files}, self.ctx)
                    out[n["name"]] = "updated"
                except OpError as e:
                    out[n["name"]] = "error: %s" % e.message
            return self.send_json(200, {"version": VERSION, "servers": out})
        if path == "/admin/servers/rehome":
            url = str(body.get("url") or "").rstrip("/")
            out = {}
            for n in STORE.data()["nodes"]:
                if not link_for(n["name"]).online():
                    out[n["name"]] = "offline"
                    continue
                try:
                    call(n["name"], "set_hub", {"url": url, "ca": str(body.get("ca") or "")}, self.ctx)
                    out[n["name"]] = "moved"
                except OpError as e:
                    out[n["name"]] = "error: %s" % e.message
            return self.send_json(200, {"url": url, "servers": out})
        raise OpError(404, "unknown admin action")

    # --- nodes talking to the hub
    def route_node(self, node, path):
        name = node["name"]
        if path == "/node/poll" and self.command == "POST":
            body = self.read_json(1 << 20)
            lk = link_for(name)
            first_contact = not lk.last_seen
            with lk.cond:
                lk.polling += 1
                lk.last_seen = time.time()
                if isinstance(body.get("info"), dict):
                    lk.info = body["info"]
            items = []
            try:
                deadline = time.time() + clamp(body.get("wait", POLL_WAIT), 0, 30, POLL_WAIT)
                with lk.cond:
                    while not lk.queue:
                        left = deadline - time.time()
                        if left <= 0:
                            break
                        lk.cond.wait(left)
                    while lk.queue and len(items) < 16:
                        items.append(lk.queue.popleft())
            finally:
                with lk.cond:
                    lk.polling -= 1
            if first_contact:
                STORE.modify(lambda d: d.update(joins=[j for j in d["joins"] if j["name"] != name]))
                audit({"a": "server-online", "on": name, "ip": self.client_ip()})
            try:
                return self.send_json(200, {"requests": items, "hub": NAME, "hub_version": VERSION})
            except Exception:
                with lk.cond:   # the node went away: keep its work for the next poll
                    for it in reversed(items):
                        lk.queue.appendleft(it)
                    lk.cond.notify_all()
                raise
        if path == "/node/result" and self.command == "POST":
            body = self.read_json(MAX_JSON)
            lk = link_for(name)
            lk.last_seen = time.time()
            for r in body.get("results") or []:
                with lk.cond:
                    p = lk.pending.pop(r.get("rid"), None)
                if p:
                    p[1].update(r)
                    p[0].set()
            return self.send_json(200, {"ok": True})
        if path == "/node/leave" and self.command == "POST":
            self.read_json(1 << 16)
            STORE.modify(lambda d: d.update(nodes=[n for n in d["nodes"] if n["name"] != name]))
            with LINKS_LOCK:
                LINKS.pop(name, None)
            audit({"a": "server-left", "on": name, "ip": self.client_ip()})
            return self.send_json(200, {"left": name})
        return self.send(404, "not found\n")

    # --- a new server downloading its installer
    def route_join(self, join, path):
        if self.command not in ("GET", "HEAD"):
            return self.send(405, "use GET\n")
        if path in ("/", ""):
            audit({"a": "join-download", "name": join["name"], "ip": self.client_ip()})
            return self.send(200, join_script(join, self.root()), "text/x-shellscript; charset=utf-8")
        files = {"/agent.py": os.path.abspath(__file__), "/ui.sh": UI_FILE, "/fatllm": CLI_FILE,
                 "/hub.pem": TLS_CERT}
        p = files.get(path)
        if not p:
            return self.send(404, "not found\n")
        try:
            with open(p, "rb") as f:
                return self.send(200, f.read(), "application/octet-stream")
        except OSError:
            return self.send(404, "not found\n")


class Server(ThreadingMixIn, HTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    ssl_ctx = None

    def finish_request(self, request, client_address):
        # The TLS handshake happens here, in the worker thread: a slow or broken client never
        # holds up the accept loop.
        if self.ssl_ctx is None:
            return self.RequestHandlerClass(request, client_address, self)
        try:
            request.settimeout(30)
            request = self.ssl_ctx.wrap_socket(request, server_side=True)
        except (ssl.SSLError, OSError, ValueError):
            return
        try:
            self.RequestHandlerClass(request, client_address, self)
        finally:
            try:
                request.close()
            except OSError:
                pass


def hub_main():
    ensure_dirs(STATE_DIR, JOBS_DIR, SPOOL_DIR, LOG_DIR)
    STORE.data()  # create keys.json (or migrate a version 1 key) before the first request
    cli_token(create=True)
    Server.address_family = socket.AF_INET6 if ":" in LISTEN else socket.AF_INET
    httpd = Server((LISTEN, PORT), Handler)
    scheme = "http"
    if TLS_CERT and TLS_KEY:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(TLS_CERT, TLS_KEY)
        try:
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        except (AttributeError, ValueError):
            pass
        httpd.ssl_ctx = ctx
        scheme = "https"
    print("fatllm %s hub '%s' listening on %s %s:%d (mode %s)" % (VERSION, NAME, scheme, LISTEN, PORT, MODE),
          flush=True)
    signal.signal(signal.SIGTERM, lambda *_: (httpd.server_close(), sys.exit(0)))
    httpd.serve_forever()


# ============================================================================= node main loop

def node_status(**kw):
    kw.setdefault("time", now())
    kw.setdefault("hub", HUB_URL)
    kw.setdefault("name", NAME)
    try:
        tmp = os.path.join(STATE_DIR, "node.json.tmp")
        with open(tmp, "w") as f:
            json.dump(kw, f)
        os.replace(tmp, os.path.join(STATE_DIR, "node.json"))
    except OSError:
        pass


def node_main():
    if not HUB_URL or len(NODE_TOKEN) < 20:
        sys.exit("FATLLM_HUB and FATLLM_NODE_TOKEN are needed; join again from the hub.")
    ensure_dirs(STATE_DIR, JOBS_DIR, LOG_DIR)
    sctx = ssl.create_default_context()
    if HUB_CA and os.path.exists(HUB_CA):
        sctx.load_verify_locations(cafile=HUB_CA)
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=sctx),
                                         urllib.request.ProxyHandler({}))
    base = HUB_URL + "/" + NODE_TOKEN
    workers = threading.BoundedSemaphore(32)
    print("fatllm %s node '%s' -> %s" % (VERSION, NAME, HUB_URL), flush=True)

    def post(path, obj, timeout):
        req = urllib.request.Request(base + path, data=json.dumps(obj).encode("utf-8"), method="POST",
                                     headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8") or "{}")

    def handle(item):
        try:
            try:
                data = local_op(item.get("op"), item.get("args") or {}, item.get("ctx") or {})
                res = {"rid": item.get("rid"), "ok": True, "data": data}
            except OpError as e:
                res = {"rid": item.get("rid"), "ok": False, "code": e.code, "error": e.message}
            except Exception as e:
                res = {"rid": item.get("rid"), "ok": False, "code": 500, "error": "%s: %s" % (type(e).__name__, e)}
            for attempt in range(5):
                try:
                    post("/node/result", {"results": [res]}, 120)
                    break
                except Exception:
                    time.sleep(1 + attempt * 2)
        finally:
            workers.release()

    backoff = 1
    info_at = 0
    connected = False
    while True:
        try:
            body = {"wait": POLL_WAIT if connected else 0}   # the first poll answers at once
            if time.time() - info_at > 300 or not connected:
                body["info"] = brief()
                info_at = time.time()
            r = post("/node/poll", body, POLL_WAIT + 20)
            if not connected:
                print("connected to the hub", flush=True)
            connected = True
            backoff = 1
            node_status(connected=True, last_ok=now(), hub_name=r.get("hub"))
            for item in r.get("requests") or []:
                workers.acquire()
                threading.Thread(target=handle, args=(item,), daemon=True).start()
        except urllib.error.HTTPError as e:
            connected = False
            msg = ("the hub does not know this server any more (removed?); join again" if e.code == 404
                   else "the hub answered %s" % e.code)
            node_status(connected=False, error=msg)
            time.sleep(60 if e.code == 404 else min(backoff, 30))
            backoff = min(backoff * 2, 30)
        except Exception as e:
            connected = False
            node_status(connected=False, error="cannot reach the hub: %s" % (getattr(e, "reason", None) or e))
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)


# ============================================================================= admin CLI (used by `fatllm`)

def local_api(method, path, body=None, timeout=60):
    """Call this server's own agent as `fatllm` (the root-only cli token, never in argv or URLs)."""
    tok = cli_token()
    if not tok:
        raise OpError(503, "the agent has not started yet (no %s)" % CLI_TOKEN_FILE)
    tls = bool(TLS_CERT and TLS_KEY)
    url = "%s://127.0.0.1:%d%s" % ("https" if tls else "http", PORT, path)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE        # our own loopback address
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), urllib.request.ProxyHandler({}))
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        with opener.open(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            msg = json.loads(raw).get("error") or raw
        except ValueError:
            msg = raw.strip().replace("error: ", "", 1)
        raise OpError(e.code, msg)
    except (urllib.error.URLError, OSError) as e:
        raise OpError(503, "the agent does not answer on port %d (%s)" % (PORT, getattr(e, "reason", e)))
    try:
        return json.loads(raw)
    except ValueError:
        return {"text": raw}


def key_url(token):
    return (PUBLIC_URL or "https://<this-server>") + "/" + token


def admin(argv):
    """python3 agent.py admin <command>: chores for the `fatllm` command. Prints plain text/JSON."""
    cmd = argv[0] if argv else "help"
    a = argv[1:]

    def set_field(name, field, value):
        def upd(dd):
            for k in dd["keys"]:
                if k["name"] == name:
                    k[field] = value
                    if field == "token":
                        k["created"] = now()
                    return True
            return False
        if not STORE.modify(upd):
            raise OpError(404, "no key named %s (fatllm keys lists them)" % name)

    def main_name():
        d = STORE.data()
        return "main" if any(k["name"] == "main" for k in d["keys"]) else d["keys"][0]["name"]

    if cmd in ("init", "token"):            # the token of a key (creates keys.json when needed)
        name = a[0] if a else main_name()
        k = STORE.key(name)
        if not k:
            raise OpError(404, "no key named %s" % name)
        print(k["token"])
    elif cmd == "show":                     # the full key line of a key
        name = a[0] if a else main_name()
        k = STORE.key(name)
        if not k:
            raise OpError(404, "no key named %s (fatllm keys lists them)" % name)
        print(key_url(k["token"]))
    elif cmd == "keys":
        print(json.dumps([{"name": k["name"], "created": k.get("created"), "expires": k.get("expires", 0)}
                          for k in STORE.data()["keys"]]))
    elif cmd == "add":
        name = a[0] if a else ""
        if not NAME_RE.match(name):
            raise OpError(400, "a key name uses letters, digits, - and _ (up to 32)")
        exp = parse_duration(a[1]) if len(a) > 1 else 0
        token = new_token("fat_")

        def upd(d):
            if any(k["name"] == name for k in d["keys"]):
                return False
            d["keys"].append({"name": name, "token": token, "created": now(), "expires": exp})
            return True
        if not STORE.modify(upd):
            raise OpError(409, "a key named %s already exists" % name)
        print(key_url(token))
    elif cmd == "remove":
        name = a[0] if a else ""

        def upd(d):
            left = [k for k in d["keys"] if k["name"] != name]
            if len(left) == len(d["keys"]):
                return "missing"
            if not left:
                return "last"
            d["keys"] = left
            return "ok"
        res = STORE.modify(upd)
        if res == "missing":
            raise OpError(404, "no key named %s" % name)
        if res == "last":
            raise OpError(409, "the last key cannot be removed (rotate it instead)")
        print("removed")
    elif cmd == "rotate":
        name = a[0] if a else main_name()
        token = new_token("fat_")
        set_field(name, "token", token)
        print(token)
    elif cmd == "expire":
        if not a:
            raise OpError(400, "usage: expire 30m|12h|7d|never [name]")
        exp = parse_duration(a[0])
        set_field(a[1] if len(a) > 1 else main_name(), "expires", exp)
        print(exp)
    elif cmd == "pin":
        print(cert_pin(a[0] if a else TLS_CERT))
    elif cmd == "api":                      # api METHOD /path [json]
        if len(a) < 2:
            raise OpError(400, "usage: api GET|POST /path [json]")
        body = json.loads(a[2]) if len(a) > 2 else ({} if a[0] == "POST" else None)
        print(json.dumps(local_api(a[0], a[1], body, timeout=150)))
    elif cmd == "log":                      # log [N]: tab-separated lines for the terminal
        n = int(a[0]) if a and a[0].isdigit() else 40
        for e in reversed(read_audit(n, a[1] if len(a) > 1 else None)):
            t = str(e.get("t", ""))
            act = e.get("a", "?")
            on = e.get("on") or e.get("name") or ""
            if act == "run":
                det = e.get("cmd", "")
            elif act == "done":
                det = "exit %s %ss" % (e.get("exit"), e.get("secs"))
            elif act in ("read", "write"):
                det = e.get("path", "")
            else:
                det = e.get("detail") or e.get("key") or ""
            det = str(det).replace("\x1f", " ").replace("\r", "").replace("\n", " \u23ce ")
            print("\x1f".join((t[5:10], t[11:16], act, on, det)))   # unit separator: fields may be empty
    elif cmd == "leave":                    # node: tell the hub to forget this server
        if ROLE != "node" or not HUB_URL or not NODE_TOKEN:
            raise OpError(400, "this server is not connected to a hub")
        sctx = ssl.create_default_context()
        if HUB_CA and os.path.exists(HUB_CA):
            sctx.load_verify_locations(cafile=HUB_CA)
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=sctx),
                                             urllib.request.ProxyHandler({}))
        req = urllib.request.Request(HUB_URL + "/" + NODE_TOKEN + "/node/leave", data=b"{}", method="POST",
                                     headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=20) as r:
                r.read()
        except Exception as e:
            raise OpError(503, "the hub did not answer (%s)" % (getattr(e, "reason", None) or e))
        print("left")
    elif cmd == "node":                     # this node's link state
        try:
            with open(os.path.join(STATE_DIR, "node.json")) as f:
                print(f.read().strip())
        except OSError:
            print("{}")
    else:
        sys.exit("admin commands: token [name], show [name], keys, add NAME [T], remove NAME, rotate [name], "
                 "expire T [name], pin [cert], api METHOD /path [json], log [N] [server], node")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "admin":
        try:
            return admin(sys.argv[2:])
        except OpError as e:
            sys.exit(e.message)
        except BrokenPipeError:
            sys.exit(0)
    if ROLE == "node":
        return node_main()
    return hub_main()


if __name__ == "__main__":
    main()
