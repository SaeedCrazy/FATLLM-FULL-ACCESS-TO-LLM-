"""End-to-end tests for FATLLM: the hub, a real node connected to it, and the srv client.

No network, no root needed (a few tests skip without openssl).

    python3 -m unittest discover -s test -v
"""
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENT = os.path.join(ROOT, "server", "agent.py")
SRV = os.path.join(ROOT, "srv")
TOKEN = "fat_" + "t" * 43


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def make_cert(tmp, cn="127.0.0.1"):
    crt, key, cfg = (os.path.join(tmp, n) for n in ("cert.pem", "key.pem", "o.cnf"))
    with open(cfg, "w") as f:
        f.write("[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n[dn]\nCN=%s\n"
                "[v3]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n" % cn)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key, "-out", crt,
                    "-days", "30", "-config", cfg], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return crt, key


def write_keys(path, token=TOKEN):
    with open(path, "w") as f:
        json.dump({"version": 2, "keys": [{"name": "main", "token": token, "created": 1, "expires": 0}],
                   "nodes": [], "joins": []}, f)


class Client(object):
    def __init__(self, base, cafile=None):
        self.base = base
        ctx = None
        if base.startswith("https"):
            ctx = ssl.create_default_context(cafile=cafile) if cafile else ssl._create_unverified_context()
        handlers = [urllib.request.ProxyHandler({})]
        if ctx:
            handlers.append(urllib.request.HTTPSHandler(context=ctx))
        self.opener = urllib.request.build_opener(*handlers)

    def call(self, path, data=None, method=None, headers=None, base=None, timeout=60):
        req = urllib.request.Request((base or self.base) + path, data=data, method=method, headers=headers or {})
        try:
            with self.opener.open(req, timeout=timeout) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def json(self, path, obj=None, method="POST"):
        code, h, body = self.call(path, json.dumps(obj or {}).encode() if obj is not None or method == "POST" else None,
                                  method if obj is not None or method == "POST" else "GET",
                                  {"Content-Type": "application/json"})
        try:
            return code, json.loads(body)
        except ValueError:
            return code, body

    def mcp(self, method, params=None, mid=1):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if mid is not None:
            msg["id"] = mid
        return self.call("/mcp", json.dumps(msg).encode(), "POST", {"Content-Type": "application/json"})


def start_agent(env, cwd=None):
    proc = subprocess.Popen([sys.executable, env.pop("_AGENT", AGENT)], env=env, cwd=cwd,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc


def wait_until(fn, timeout=20, step=0.1):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if fn():
                return True
        except Exception:
            pass
        time.sleep(step)
    return False


# =============================================================================================== hub

class HubTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.port = free_port()
        cls.keys = os.path.join(cls.tmp, "keys.json")
        write_keys(cls.keys)
        cls.env = dict(os.environ, FATLLM_ROLE="hub", FATLLM_LOCAL_PORT=str(cls.port), FATLLM_KEYS=cls.keys,
                       FATLLM_STATE_DIR=os.path.join(cls.tmp, "state"), FATLLM_LOG_DIR=os.path.join(cls.tmp, "log"),
                       FATLLM_CONF=os.path.join(cls.tmp, "fatllm.env"), FATLLM_CLIENT=SRV)
        cls.proc = start_agent(dict(cls.env))
        cls.base = "http://127.0.0.1:%d/%s" % (cls.port, TOKEN)
        cls.c = Client(cls.base)
        assert wait_until(lambda: cls.c.call("/info")[0] == 200), cls.proc.stdout.read()

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def admin(self, *args, env=None):
        p = subprocess.run([sys.executable, AGENT, "admin"] + list(args), env=env or self.env,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return p.returncode, p.stdout.decode(), p.stderr.decode()

    def test_cli_token_and_admin_api(self):
        tok = os.path.join(self.env["FATLLM_STATE_DIR"], "cli.token")
        self.assertEqual(oct(os.stat(tok).st_mode & 0o777), "0o600")
        rc, out, err = self.admin("api", "GET", "/info")
        self.assertEqual(rc, 0, err)
        self.assertIn("hostname", json.loads(out))
        with open(tok) as f:
            cli = f.read().strip()
        code, _, _ = self.c.call("/info", base="http://127.0.0.1:%d" % self.port, headers={"Authorization": "Bearer " + cli})
        self.assertEqual(code, 200)
        self.assertEqual(self.c.call("/info", base="http://127.0.0.1:%d/%s" % (self.port, cli))[0], 404)  # never in a URL
        rc, out, _ = self.admin("api", "POST", "/admin/servers", '{"name": "viacli"}')
        self.assertEqual(rc, 0)
        self.assertIn("join_", json.loads(out)["command"])
        rc, _, err = self.admin("api", "POST", "/admin/servers", '{"name": "bad name"}')
        self.assertNotEqual(rc, 0)
        self.assertIn("letters", err)
        self.assertEqual(self.admin("api", "POST", "/admin/servers/remove", '{"name": "viacli"}')[0], 0)

    def test_admin_cli_keys_and_log(self):
        rc, url, _ = self.admin("add", "clikey", "2h")
        self.assertEqual(rc, 0)
        self.assertRegex(url.strip(), r"/fat_[A-Za-z0-9_-]{30,}$")
        tok = url.strip().rsplit("/", 1)[1]
        self.assertTrue(wait_until(lambda: self.c.call("/info", base="http://127.0.0.1:%d/%s" % (self.port, tok))[0] == 200, 5))
        self.assertIn(tok, self.admin("show", "clikey")[1])
        keys = json.loads(self.admin("keys")[1])
        self.assertTrue(any(k["name"] == "clikey" and k["expires"] > time.time() for k in keys))
        self.assertNotEqual(self.admin("add", "clikey")[0], 0)            # names are unique
        self.assertEqual(self.admin("remove", "clikey")[0], 0)
        self.assertTrue(wait_until(lambda: self.c.call("/info", base="http://127.0.0.1:%d/%s" % (self.port, tok))[0] == 404, 5))
        self.c.call("/run", b"echo for-the-log", "POST")
        rc, out, _ = self.admin("log", "50")
        self.assertEqual(rc, 0)
        line = [l for l in out.splitlines() if "for-the-log" in l][-1]
        self.assertEqual(line.split("\x1f")[2], "run")

    @unittest.skipUnless(hasattr(os, "geteuid") and os.geteuid() == 0, "fatllm needs root")
    def test_fatllm_cli(self):
        tmp = tempfile.mkdtemp()
        try:
            conf = os.path.join(tmp, "fatllm.env")
            with open(conf, "w") as f:
                for k in ("FATLLM_ROLE", "FATLLM_LOCAL_PORT", "FATLLM_KEYS", "FATLLM_STATE_DIR", "FATLLM_LOG_DIR"):
                    f.write("%s=%s\n" % (k, self.env[k]))
                f.write("FATLLM_PUBLIC_URL=http://127.0.0.1:%d\nFATLLM_PORT=%d\nFATLLM_MODE=direct\n" % (self.port, self.port))
            os.makedirs(os.path.join(tmp, "bin"))
            fake = os.path.join(tmp, "bin", "systemctl")      # the agent is "active": it runs in this test
            with open(fake, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(fake, 0o755)
            env = dict(os.environ, FATLLM_CONF=conf, FATLLM_APP=os.path.join(ROOT, "server"),
                       PATH=os.path.join(tmp, "bin") + ":" + os.environ["PATH"])
            fat = os.path.join(ROOT, "server", "fatllm")

            def run(*a):
                p = subprocess.run(["bash", fat] + list(a), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   stdin=subprocess.DEVNULL)
                return p.returncode, p.stdout.decode()
            rc, out = run("key")
            self.assertEqual(rc, 0, out)
            self.assertIn("FATLLM key: http://127.0.0.1:%d/%s" % (self.port, TOKEN), out)
            rc, out = run("status")
            self.assertEqual(rc, 0, out)
            self.assertIn("answers on port", out)
            rc, out = run("add-server", "clinode", "--no-wait")
            self.assertEqual(rc, 0, out)
            self.assertRegex(out, r"curl -fsS http://127\.0\.0\.1:\d+/join_\S+ \| bash")
            rc, out = run("servers")
            self.assertIn("clinode", out)
            rc, out = run("remove-server", "clinode")
            self.assertEqual(rc, 0, out)
            rc, out = run("add-server", "bad name")
            self.assertNotEqual(rc, 0)
            rc, out = run("keys", "add", "clitwo")
            self.assertEqual(rc, 0, out)
            rc, out = run("keys", "remove", "clitwo")
            self.assertEqual(rc, 0, out)
            rc, out = run("expire", "5x")
            self.assertNotEqual(rc, 0)
            self.assertIn("30m, 12h, 7d", out)
            rc, out = run("log", "5")
            self.assertEqual(rc, 0, out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_zz_bad_keys_never_block_a_good_one(self):
        bad = "http://127.0.0.1:%d/fat_%s" % (self.port, "x" * 43)
        codes = {self.c.call("/info", base=bad)[0] for _ in range(35)}
        self.assertIn(429, codes)                        # scanners are slowed down...
        self.assertEqual(self.c.call("/info")[0], 200)   # ...the owner's key still works

    # --- auth
    def test_wrong_key_is_404(self):
        code, _, body = self.c.call("/info", base="http://127.0.0.1:%d/fat_wrong%s" % (self.port, "x" * 30))
        self.assertEqual(code, 404)
        self.assertNotIn(b"hostname", body)
        self.assertEqual(self.c.call("", base="http://127.0.0.1:%d" % self.port)[0], 404)

    def test_bearer(self):
        code, _, body = self.c.call("/info", base="http://127.0.0.1:%d" % self.port,
                                    headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(code, 200)
        self.assertIn("hostname", json.loads(body))

    def test_guide(self):
        code, _, body = self.c.call("/")
        self.assertEqual(code, 200)
        self.assertIn(b"SERVERS", body)
        self.assertIn(self.base.encode(), body)

    # --- run
    def test_run_text(self):
        code, h, body = self.c.call("/run", b"echo hello; echo oops >&2; exit 3", "POST")
        self.assertEqual(code, 200)
        self.assertIn("hello", body.decode())
        self.assertIn("oops", body.decode())
        self.assertIn("[exit 3", body.decode())
        self.assertEqual(h.get("X-Exit-Code"), "3")
        self.assertEqual(h.get("X-Server"), "main")

    def test_run_multiline_env_json(self):
        _, _, body = self.c.call("/run", b"x=1\nif [ $x = 1 ]; then echo yes; fi\necho tok=${FATLLM_KEYS:-none}", "POST")
        self.assertIn("yes", body.decode())
        self.assertIn("tok=none", body.decode())  # nothing of FATLLM's reaches commands
        code, r = self.c.json("/run", {"cmd": "cat; pwd", "stdin": "in-data", "cwd": "/tmp"})
        self.assertEqual(code, 200)
        self.assertTrue(r["done"])
        self.assertIn("in-data", r["output"])
        self.assertIn("/tmp", r["output"])

    def test_run_errors(self):
        self.assertEqual(self.c.call("/run?cwd=/no/such/dir", b"true", "POST")[0], 400)
        self.assertEqual(self.c.call("/run", b"   ", "POST")[0], 400)
        self.assertEqual(self.c.call("/run?id=NOTHEX", b"true", "POST")[0], 400)
        self.assertEqual(self.c.call("/run?on=nope", b"true", "POST")[0], 404)

    def test_idempotent_id(self):
        f = os.path.join(self.tmp, "ran-once")
        cmd = ("echo x >> %s" % f).encode()
        for _ in range(3):  # a client retrying after a dropped connection
            code, h, _ = self.c.call("/run?id=abcdef0123456789", cmd, "POST")
            self.assertEqual(code, 200)
            self.assertEqual(h["X-Job"], "abcdef0123456789")
        with open(f) as fh:
            self.assertEqual(fh.read(), "x\n")

    def test_long_job_poll_kill_timeout(self):
        _, h, body = self.c.call("/run?wait=1", b"echo start; sleep 2; echo end", "POST")
        self.assertIn("still running", body.decode())
        _, h2, body = self.c.call("/job/%s?wait=10" % h["X-Job"])
        self.assertIn("end", body.decode())
        self.assertEqual(h2.get("X-Exit-Code"), "0")
        _, h, _ = self.c.call("/run?wait=0", b"sleep 30", "POST")
        _, _, body = self.c.call("/job/%s/kill" % h["X-Job"], b"", "POST")
        self.assertIn("killed", body.decode())
        _, _, body = self.c.call("/run?timeout=1&wait=10", b"sleep 20", "POST")
        self.assertIn("killed", body.decode())
        _, _, body = self.c.call("/jobs")
        self.assertIn(h["X-Job"], body.decode())

    def test_big_output_cut(self):
        _, _, body = self.c.call("/run", b"head -c 3000000 /dev/zero | tr '\\0' a", "POST")
        self.assertLess(len(body), 1100000)
        self.assertIn(b"bytes skipped", body)

    # --- files
    def test_files(self):
        path = os.path.join(self.tmp, "deep", "d", "f.bin")
        data = bytes(range(256)) * 100
        code, _, body = self.c.call("/file?path=%s&mode=600" % path, data, "PUT")
        self.assertEqual(code, 200, body)
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        self.assertEqual(self.c.call("/file?path=" + path)[2], data)
        self.c.call("/file?path=%s&append=1" % path, b"tail", "PUT")
        self.assertTrue(self.c.call("/file?path=" + path)[2].endswith(b"tail"))
        code, r = self.c.json("/file?json=1&path=" + os.path.dirname(path), method="GET")
        self.assertEqual([e["name"] for e in r["entries"]], ["f.bin"])
        _, h, _ = self.c.call("/file?download=1&path=" + path)
        self.assertIn("filename*=UTF-8''f.bin", h["Content-Disposition"])
        self.assertEqual(self.c.call("/file?path=relative")[0], 400)
        self.assertEqual(self.c.call("/file?path=/no/such/file")[0], 404)

    # --- panel, stats, audit, servers
    def test_panel(self):
        code, h, body = self.c.call("/panel")
        self.assertEqual(code, 200)
        html = body.decode()
        nonce = h["Content-Security-Policy"].split("'nonce-")[1].split("'")[0]
        self.assertIn('nonce="%s"' % nonce, html)
        self.assertNotIn("{{NONCE}}", html)
        self.assertNotIn(TOKEN, html)

    def test_stats_servers_audit(self):
        code, s = self.c.json("/stats", method="GET")
        for k in ("hostname", "cpus", "load", "mem_total", "disk_total", "mode", "expires", "key_name"):
            self.assertIn(k, s)
        code, r = self.c.json("/servers?json=1", method="GET")
        self.assertEqual(r["servers"][0]["name"], "main")
        self.assertIn(b"main", self.c.call("/servers")[2])
        self.c.call("/run", b"echo audited\necho two", "POST")
        time.sleep(0.3)
        _, r = self.c.json("/audit?n=50", method="GET")
        runs = [e for e in r["entries"] if e.get("a") == "run" and "audited" in e.get("cmd", "")]
        self.assertTrue(runs)
        self.assertEqual(runs[0]["key"], "main")
        self.assertIn("audited\necho two", runs[0]["cmd"])
        with open(os.path.join(self.tmp, "log", "audit.log")) as f:
            self.assertNotIn(TOKEN, f.read())

    # --- admin
    def test_keys_admin(self):
        code, r = self.c.json("/admin/keys", {"name": "chatgpt"})
        self.assertEqual(code, 200)
        other = Client(r["key"])
        self.assertEqual(other.call("/info")[0], 200)
        self.assertEqual(self.c.json("/admin/keys", {"name": "chatgpt"})[0], 409)
        self.assertEqual(self.c.json("/admin/keys", {"name": "bad name!"})[0], 400)
        _, r2 = self.c.json("/admin/keys", method="GET")
        self.assertEqual({k["name"] for k in r2["keys"]}, {"main", "chatgpt"})
        _, r3 = self.c.json("/admin/keys/show", {"name": "chatgpt"})
        self.assertEqual(r3["key"], r["key"])
        code, r4 = other.json("/admin/expire", {"in": "2h"})
        self.assertAlmostEqual(r4["expires"], time.time() + 7200, delta=30)
        self.assertEqual(other.json("/admin/expire", {"in": "soon"})[0], 400)
        self.assertEqual(other.json("/admin/keys/remove", {"name": "chatgpt"})[0], 409)  # in use
        self.assertEqual(self.c.json("/admin/keys/remove", {"name": "chatgpt"})[0], 200)
        self.assertEqual(other.call("/info")[0], 404)

    def test_server_join_command(self):
        code, r = self.c.json("/admin/servers", {"name": "ir-1"})
        self.assertEqual(code, 200)
        self.assertIn("| bash", r["command"])
        join = re.search(r"/(join_[A-Za-z0-9_-]+)", r["command"]).group(1)
        code, _, script = self.c.call("", base="http://127.0.0.1:%d/%s" % (self.port, join))
        self.assertEqual(code, 200)
        self.assertEqual(subprocess.run(["bash", "-n"], input=script).returncode, 0)
        self.assertIn(b"FATLLM_ROLE=node", script)
        self.assertNotIn(TOKEN.encode(), script)
        _, s = self.c.json("/servers?json=1", method="GET")
        self.assertTrue(any(x["name"] == "ir-1" and x["joining"] for x in s["servers"]))
        self.assertEqual(self.c.json("/admin/servers", {"name": "main"})[0], 400)
        self.assertEqual(self.c.json("/admin/servers/remove", {"name": "ir-1"})[0], 200)

    def test_off_without_cli(self):
        if shutil.which("fatllm") or os.path.exists("/usr/local/bin/fatllm"):
            self.skipTest("a real fatllm command is installed here")
        self.assertEqual(self.c.json("/admin/off", {})[0], 409)

    # --- MCP
    def test_mcp(self):
        code, _, body = self.c.mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                  "clientInfo": {"name": "t", "version": "1"}})
        self.assertEqual(json.loads(body)["result"]["protocolVersion"], "2025-06-18")
        code, _, body = self.c.mcp("notifications/initialized", mid=None)
        self.assertEqual((code, body), (202, b""))
        _, _, body = self.c.mcp("tools/list", {}, 2)
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        self.assertEqual(names, ["list_servers", "run_command", "job_status", "read_file", "write_file", "server_info"])
        _, _, body = self.c.mcp("tools/call", {"name": "run_command", "arguments": {"command": "echo mcp-ok"}}, 3)
        self.assertIn("mcp-ok", json.loads(body)["result"]["content"][0]["text"])
        path = os.path.join(self.tmp, "mcp.txt")
        self.c.mcp("tools/call", {"name": "write_file", "arguments": {"path": path, "content": "سلام\n"}}, 4)
        _, _, body = self.c.mcp("tools/call", {"name": "read_file", "arguments": {"path": path}}, 5)
        self.assertEqual(json.loads(body)["result"]["content"][0]["text"], "سلام\n")
        _, _, body = self.c.mcp("tools/call", {"name": "run_command", "arguments": {"command": "true", "server": "x"}}, 6)
        self.assertTrue(json.loads(body)["result"]["isError"])
        _, _, body = self.c.mcp("nope", {}, 7)
        self.assertEqual(json.loads(body)["error"]["code"], -32601)

    # --- srv client
    def test_srv(self):
        home = tempfile.mkdtemp()
        env = dict(os.environ, FATLLM_KEY=self.base, HOME=home, NO_PROXY="*", no_proxy="*")
        try:
            p = subprocess.run([SRV, "echo from-srv; exit 4"], env=env, stdout=subprocess.PIPE, timeout=60)
            self.assertIn(b"from-srv", p.stdout)
            self.assertEqual(p.returncode, 4)
            p = subprocess.run([SRV, "-"], input=b"echo one\necho two\n", env=env, stdout=subprocess.PIPE, timeout=60)
            self.assertIn(b"one\ntwo", p.stdout)
            local = os.path.join(home, "up.txt")
            with open(local, "w") as f:
                f.write("with spaces")
            remote = os.path.join(self.tmp, "a dir", "b c.txt")
            self.assertEqual(subprocess.run([SRV, "put", local, remote], env=env, stdout=subprocess.PIPE).returncode, 0)
            p = subprocess.run([SRV, "get", remote], env=env, stdout=subprocess.PIPE)
            self.assertEqual(p.stdout, b"with spaces")
            p = subprocess.run([SRV, "servers"], env=env, stdout=subprocess.PIPE)
            self.assertIn(b"main", p.stdout)
            p = subprocess.run([SRV, "--on", "nope", "true"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(p.returncode, 2)
            for pasted in (self.base + "/mcp", "FATLLM key: %s\n" % self.base, self.base + "/panel"):
                p = subprocess.run([SRV, "key", pasted], env=dict(env, FATLLM_KEY=""), stdout=subprocess.PIPE)
                self.assertEqual(p.returncode, 0)
                with open(os.path.join(home, ".fatllm-key")) as f:
                    self.assertEqual(f.read().strip(), self.base)
            _, _, body = self.c.call("/srv")
            self.assertIn(('BAKED_KEY="%s"' % self.base).encode(), body)
        finally:
            shutil.rmtree(home, ignore_errors=True)


# =============================================================================================== hub + node

@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class NodeTest(unittest.TestCase):
    """A TLS hub (self-signed) and a real node that dials out to it, both on this machine."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.port = free_port()
        cls.crt, cls.keyf = make_cert(cls.tmp)
        cls.keys = os.path.join(cls.tmp, "keys.json")
        write_keys(cls.keys)
        hub_env = dict(os.environ, FATLLM_ROLE="hub", FATLLM_LOCAL_PORT=str(cls.port), FATLLM_KEYS=cls.keys,
                       FATLLM_LISTEN="127.0.0.1", FATLLM_TLS_CERT=cls.crt, FATLLM_TLS_KEY=cls.keyf,
                       FATLLM_MODE="direct", FATLLM_PUBLIC_URL="https://127.0.0.1:%d" % cls.port,
                       FATLLM_STATE_DIR=os.path.join(cls.tmp, "hub-state"),
                       FATLLM_LOG_DIR=os.path.join(cls.tmp, "hub-log"), FATLLM_NODE_ONLINE="4",
                       FATLLM_POLL_WAIT="2")
        cls.hub = start_agent(hub_env)
        cls.base = "https://127.0.0.1:%d/%s" % (cls.port, TOKEN)
        cls.c = Client(cls.base, cafile=cls.crt)
        assert wait_until(lambda: cls.c.call("/info")[0] == 200), cls.hub.stdout.read()
        code, r = cls.c.json("/admin/servers", {"name": "iran"})
        assert code == 200, r
        cls.command = r["command"]
        join = re.search(r"/(join_[A-Za-z0-9_-]+)", r["command"]).group(1)
        _, _, script = cls.c.call("", base="https://127.0.0.1:%d/%s" % (cls.port, join))
        cls.script = script.decode()
        cls.node_token = re.search(r"NODE_TOKEN='(node_[^']+)'", cls.script).group(1)
        # the node runs a copy of the agent, so an update test cannot touch the repository
        cls.node_dir = os.path.join(cls.tmp, "node-app")
        os.makedirs(cls.node_dir)
        shutil.copy(AGENT, os.path.join(cls.node_dir, "agent.py"))
        cls.node_env = dict(os.environ, FATLLM_ROLE="node", FATLLM_NAME="iran",
                            FATLLM_HUB="https://127.0.0.1:%d" % cls.port, FATLLM_NODE_TOKEN=cls.node_token,
                            FATLLM_HUB_CA=cls.crt, FATLLM_STATE_DIR=os.path.join(cls.tmp, "node-state"),
                            FATLLM_LOG_DIR=os.path.join(cls.tmp, "node-log"),
                            FATLLM_CONF=os.path.join(cls.tmp, "node.env"),
                            FATLLM_UI=os.path.join(cls.node_dir, "ui.sh"),
                            FATLLM_CLI_DEST=os.path.join(cls.node_dir, "fatllm"), FATLLM_POLL_WAIT="2",
                            FATLLM_RESTART="true", _AGENT=os.path.join(cls.node_dir, "agent.py"))
        with open(cls.node_env["FATLLM_CONF"], "w") as f:
            f.write("FATLLM_ROLE=node\nFATLLM_HUB=%s\n" % cls.node_env["FATLLM_HUB"])
        cls.node = start_agent(dict(cls.node_env))
        assert wait_until(cls.online, 20), "node did not connect"

    @classmethod
    def online(cls, name="iran"):
        _, r = cls.c.json("/servers?json=1", method="GET")
        return any(s["name"] == name and s["online"] for s in r["servers"])

    @classmethod
    def tearDownClass(cls):
        for p in (cls.node, cls.hub):
            p.terminate()
            p.wait(5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_a_join_script_and_pin(self):
        self.assertIn("--pinnedpubkey 'sha256//", self.command)
        self.assertEqual(subprocess.run(["bash", "-n"], input=self.script.encode()).returncode, 0)
        _, r = self.c.json("/servers?json=1", method="GET")
        node = [s for s in r["servers"] if s["name"] == "iran"][0]
        self.assertTrue(node["online"])
        self.assertFalse(node["joining"])        # the join token is used up on first contact
        self.assertEqual(node["version"], "2.0.0")
        with open(os.path.join(self.tmp, "node-state", "node.json")) as f:
            self.assertTrue(json.load(f)["connected"])

    def test_run_on_node(self):
        code, h, body = self.c.call("/run?on=iran", b"echo on-node; exit 5", "POST")
        self.assertEqual(code, 200, body)
        self.assertIn("on-node", body.decode())
        self.assertIn("iran · exit 5", body.decode())
        self.assertEqual(h["X-Server"], "iran")
        self.assertEqual(h["X-Exit-Code"], "5")
        with open(os.path.join(self.tmp, "node-log", "audit.log")) as f:   # it really ran on the node
            self.assertIn("on-node", f.read())
        _, r = self.c.json("/audit?on=iran", method="GET")
        self.assertTrue(any(e.get("a") == "run" and "on-node" in e.get("cmd", "") for e in r["entries"]))

    def test_node_long_job_kill_and_idempotent_retry(self):
        _, h, body = self.c.call("/run?on=iran&wait=1", b"echo begin; sleep 2; echo finished", "POST")
        self.assertIn("still running", body.decode())
        _, h2, body = self.c.call("/job/%s?on=iran&wait=10" % h["X-Job"])
        self.assertIn("finished", body.decode())
        self.assertEqual(h2["X-Exit-Code"], "0")
        f = os.path.join(self.tmp, "node-once")
        for _ in range(3):
            self.c.call("/run?on=iran&id=0123456789abcdef", ("echo y >> %s" % f).encode(), "POST")
        with open(f) as fh:
            self.assertEqual(fh.read(), "y\n")
        _, h, _ = self.c.call("/run?on=iran&wait=0", b"sleep 30", "POST")
        _, _, body = self.c.call("/job/%s/kill?on=iran" % h["X-Job"], b"", "POST")
        self.assertIn("killed", body.decode())

    def test_node_files_big(self):
        data = os.urandom(9 * 1024 * 1024 + 123)       # more than two hub<->node chunks
        path = os.path.join(self.tmp, "node files", "big.bin")
        code, _, body = self.c.call("/file?on=iran&mode=640&path=" + urllib.request.quote(path), data, "PUT")
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body)["bytes"], len(data))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o640")
        code, h, got = self.c.call("/file?on=iran&path=" + urllib.request.quote(path), timeout=120)
        self.assertEqual(code, 200)
        self.assertEqual(int(h["Content-Length"]), len(data))
        self.assertEqual(got, data)
        _, r = self.c.json("/file?on=iran&json=1&path=" + urllib.request.quote(os.path.dirname(path)), method="GET")
        self.assertEqual([e["name"] for e in r["entries"]], ["big.bin"])
        self.assertFalse([n for n in os.listdir(os.path.dirname(path)) if n.endswith(".part")])

    def test_node_mcp_and_stats(self):
        _, _, body = self.c.mcp("tools/call", {"name": "run_command",
                                               "arguments": {"command": "echo mcp-node", "server": "iran"}}, 9)
        self.assertIn("mcp-node", json.loads(body)["result"]["content"][0]["text"])
        _, _, body = self.c.mcp("tools/call", {"name": "list_servers", "arguments": {}}, 10)
        self.assertIn("iran", json.loads(body)["result"]["content"][0]["text"])
        code, s = self.c.json("/stats?on=iran", method="GET")
        self.assertEqual(code, 200)
        self.assertEqual(s["role"], "node")
        self.assertEqual(s["name"], "iran")

    def test_srv_on_node(self):
        home = tempfile.mkdtemp()
        env = dict(os.environ, HOME=home, NO_PROXY="*", no_proxy="*", FATLLM_KEY="")
        try:
            p = subprocess.run([SRV, "key", self.base], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertIn(b"pinned", p.stdout)
            p = subprocess.run([SRV, "--on", "iran", "echo via-srv; exit 3"], env=env, stdout=subprocess.PIPE)
            self.assertIn(b"via-srv", p.stdout)
            self.assertEqual(p.returncode, 3)
            p = subprocess.run([SRV, "servers"], env=env, stdout=subprocess.PIPE)
            self.assertIn(b"iran", p.stdout)
            self.assertIn(b"online", p.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_x_rehome_sends_certificate(self):
        with open(self.crt) as f:
            pem = f.read()
        url = "https://127.0.0.1:%d" % self.port
        code, r = self.c.json("/admin/servers/rehome", {"url": url, "ca": pem})
        self.assertEqual(code, 200, r)
        self.assertEqual(r["servers"]["iran"], "moved")
        with open(self.node_env["FATLLM_CONF"]) as f:
            conf = f.read()
        self.assertIn("FATLLM_HUB=%s" % url, conf)
        ca = os.path.join(os.path.dirname(self.node_env["FATLLM_CONF"]), "hub.pem")
        self.assertIn("FATLLM_HUB_CA=%s" % ca, conf)
        with open(ca) as f:
            self.assertEqual(f.read(), pem)
        self.assertEqual(oct(os.stat(ca).st_mode & 0o777), "0o600")

    def test_zz_leave(self):
        code, r = self.c.json("/admin/servers", {"name": "leaver"})
        join = re.search(r"/(join_[A-Za-z0-9_-]+)", r["command"]).group(1)
        _, _, script = self.c.call("", base="https://127.0.0.1:%d/%s" % (self.port, join))
        token = re.search(r"NODE_TOKEN='(node_[^']+)'", script.decode()).group(1)
        env = dict(self.node_env, FATLLM_NAME="leaver", FATLLM_NODE_TOKEN=token,
                   FATLLM_STATE_DIR=os.path.join(self.tmp, "leaver-state"), FATLLM_LOG_DIR=os.path.join(self.tmp, "leaver-log"))
        env.pop("_AGENT")
        node = start_agent(dict(env))
        try:
            self.assertTrue(wait_until(lambda: self.online("leaver"), 20))
            p = subprocess.run([sys.executable, AGENT, "admin", "leave"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self.assertEqual(p.returncode, 0, p.stderr)
            _, r = self.c.json("/servers?json=1", method="GET")
            self.assertFalse(any(s["name"] == "leaver" for s in r["servers"]))
        finally:
            node.terminate()
            node.wait(5)

    def test_y_offline_then_reconnect(self):
        self.node.terminate()
        self.node.wait(5)
        self.assertTrue(wait_until(lambda: not self.online(), 10))
        code, _, body = self.c.call("/run?on=iran", b"true", "POST")
        self.assertEqual(code, 503)
        self.assertIn(b"offline", body)
        type(self).node = start_agent(dict(self.node_env))    # it comes back by itself
        self.assertTrue(wait_until(self.online, 20))
        self.assertIn(b"back", self.c.call("/run?on=iran", b"echo back", "POST")[2])

    def test_z_update_and_remove(self):
        code, r = self.c.json("/admin/servers/update", {})
        self.assertEqual(code, 200)
        self.assertEqual(r["servers"]["iran"], "updated")
        with open(os.path.join(self.node_dir, "agent.py"), "rb") as a, open(AGENT, "rb") as b:
            self.assertEqual(a.read(), b.read())
        code, r = self.c.json("/admin/servers/remove", {"name": "iran"})
        self.assertEqual(code, 200)
        self.assertEqual(self.c.call("/run?on=iran", b"true", "POST")[0], 404)

        def removed():
            with open(os.path.join(self.tmp, "node-state", "node.json")) as f:
                return "join again" in json.load(f).get("error", "")
        self.assertTrue(wait_until(removed, 40))


# =============================================================================================== keys store

class StoreTest(unittest.TestCase):
    def test_v1_key_migrates_and_admin_cli(self):
        tmp = tempfile.mkdtemp()
        try:
            keys = os.path.join(tmp, "keys.json")
            env = dict(os.environ, FATLLM_KEYS=keys, FATLLM_TOKEN="fat_" + "v" * 43, FATLLM_EXPIRES="0")
            out = subprocess.run([sys.executable, AGENT, "admin", "token"], env=env, stdout=subprocess.PIPE)
            self.assertEqual(out.stdout.decode().strip(), "fat_" + "v" * 43)   # version 1 key kept
            self.assertEqual(oct(os.stat(keys).st_mode & 0o777), "0o600")
            new = subprocess.run([sys.executable, AGENT, "admin", "rotate"], env=env,
                                 stdout=subprocess.PIPE).stdout.decode().strip()
            self.assertTrue(new.startswith("fat_") and new != "fat_" + "v" * 43)
            exp = subprocess.run([sys.executable, AGENT, "admin", "expire", "1h"], env=env,
                                 stdout=subprocess.PIPE).stdout.decode().strip()
            self.assertAlmostEqual(int(exp), time.time() + 3600, delta=30)
            with open(keys) as f:
                d = json.load(f)
            self.assertEqual(d["keys"][0]["token"], new)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
