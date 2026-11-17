"""End-to-end tests for server/agent.py and the srv client (no network, no root needed).

    python3 -m unittest discover -s test -v
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOKEN = "fat_" + "t" * 43


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class AgentTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.port = free_port()
        cls.relay_dir = os.path.join(cls.tmp, "relays")
        os.makedirs(cls.relay_dir)
        with open(os.path.join(cls.relay_dir, "iran.json"), "w") as f:
            json.dump({"name": "iran", "host": "203.0.113.9", "user": "root",
                       "port": 22, "socks_port": 9999, "note": "test relay"}, f)
        cls.conf = os.path.join(cls.tmp, "fatllm.env")
        with open(cls.conf, "w") as f:
            f.write("FATLLM_TOKEN=%s\nFATLLM_EXPIRES=0\n" % TOKEN)
        env = dict(os.environ, FATLLM_TOKEN=TOKEN, FATLLM_LOCAL_PORT=str(cls.port),
                   FATLLM_STATE_DIR=os.path.join(cls.tmp, "state"),
                   FATLLM_LOG_DIR=os.path.join(cls.tmp, "log"),
                   FATLLM_RELAY_DIR=cls.relay_dir,
                   FATLLM_CONF=cls.conf,
                   FATLLM_CLIENT=os.path.join(ROOT, "srv"))
        cls.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server", "agent.py")],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        cls.base = "http://127.0.0.1:%d/%s" % (cls.port, TOKEN)
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.base + "/info", timeout=1)
                break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def call(self, path, data=None, method=None, headers=None, base=None):
        req = urllib.request.Request((base or self.base) + path, data=data, method=method,
                                     headers=headers or {})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=30) as r:
                return r.status, dict(r.headers), r.read()
        except urllib.error.HTTPError as e:
            return e.code, dict(e.headers), e.read()

    def mcp(self, method, params=None, mid=1):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        if mid is not None:
            msg["id"] = mid
        return self.call("/mcp", json.dumps(msg).encode(), "POST",
                         {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"})

    # --- auth

    def test_wrong_token_is_404(self):
        code, _, body = self.call("/info", base="http://127.0.0.1:%d/fat_wrong" % self.port)
        self.assertEqual(code, 404)
        self.assertNotIn(b"hostname", body)
        code, _, _ = self.call("", base="http://127.0.0.1:%d" % self.port)
        self.assertEqual(code, 404)

    def test_bearer_header(self):
        code, _, body = self.call("/info", base="http://127.0.0.1:%d" % self.port,
                                  headers={"Authorization": "Bearer " + TOKEN})
        self.assertEqual(code, 200)
        self.assertIn("hostname", json.loads(body))

    def test_guide(self):
        code, _, body = self.call("/")
        self.assertEqual(code, 200)
        self.assertIn(b"FATLLM", body)
        self.assertIn(self.base.encode(), body)

    # --- run

    def test_run_text(self):
        code, headers, body = self.call("/run", b"echo hello; echo oops >&2; exit 3", "POST")
        self.assertEqual(code, 200)
        text = body.decode()
        self.assertIn("hello", text)
        self.assertIn("oops", text)
        self.assertIn("[exit 3", text)
        self.assertEqual(headers.get("X-Exit-Code"), "3")

    def test_run_multiline_and_env(self):
        script = b"x=1\nif [ $x = 1 ]; then echo yes; fi\necho \"tok=${FATLLM_TOKEN:-none}\"\n"
        _, _, body = self.call("/run", script, "POST")
        self.assertIn("yes", body.decode())
        self.assertIn("tok=none", body.decode())  # the token never reaches commands

    def test_run_json(self):
        code, _, body = self.call("/run", json.dumps({"cmd": "cat; pwd", "stdin": "in-data", "cwd": "/tmp"}).encode(),
                                  "POST", {"Content-Type": "application/json"})
        r = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(r["done"])
        self.assertEqual(r["exit_code"], 0)
        self.assertIn("in-data", r["output"])
        self.assertIn("/tmp", r["output"])

    def test_run_bad_cwd(self):
        code, _, body = self.call("/run?cwd=/no/such/dir", b"true", "POST")
        self.assertEqual(code, 400)

    def test_long_job_and_poll(self):
        _, headers, body = self.call("/run?wait=1", b"echo start; sleep 2; echo end", "POST")
        self.assertIn("still running", body.decode())
        jid = headers["X-Job"]
        _, headers, body = self.call("/job/%s?wait=10" % jid)
        self.assertIn("end", body.decode())
        self.assertEqual(headers.get("X-Exit-Code"), "0")

    def test_kill(self):
        _, headers, _ = self.call("/run?wait=0", b"sleep 30", "POST")
        jid = headers["X-Job"]
        _, _, body = self.call("/job/%s/kill" % jid, b"", "POST")
        self.assertIn("killed", body.decode())
        _, _, body = self.call("/jobs")
        self.assertIn(jid, body.decode())

    def test_timeout_kills(self):
        _, _, body = self.call("/run?timeout=1&wait=10", b"sleep 20", "POST")
        self.assertIn("killed", body.decode())

    def test_big_output_is_cut(self):
        _, _, body = self.call("/run", b"head -c 3000000 /dev/zero | tr '\\0' a", "POST")
        self.assertLess(len(body), 1100000)
        self.assertIn(b"bytes skipped", body)

    # --- files

    def test_file_roundtrip(self):
        path = os.path.join(self.tmp, "deep", "dir", "f.bin")
        data = bytes(range(256)) * 100
        code, _, body = self.call("/file?path=" + path + "&mode=600", data, "PUT")
        self.assertEqual(code, 200, body)
        self.assertEqual(json.loads(body)["bytes"], len(data))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o600")
        code, _, body = self.call("/file?path=" + path)
        self.assertEqual(body, data)
        self.call("/file?path=%s&append=1" % path, b"tail", "PUT")
        _, _, body = self.call("/file?path=" + path)
        self.assertTrue(body.endswith(b"tail"))
        _, _, body = self.call("/file?path=" + os.path.dirname(path))
        self.assertIn(b"f.bin", body)

    def test_file_errors(self):
        code, _, _ = self.call("/file?path=relative.txt")
        self.assertEqual(code, 400)
        code, _, _ = self.call("/file?path=/no/such/file")
        self.assertEqual(code, 404)

    # --- client script

    def test_client_script_has_key(self):
        _, _, body = self.call("/srv")
        self.assertIn(('BAKED_KEY="%s"' % self.base).encode(), body)

    def test_srv_client(self):
        env = dict(os.environ, FATLLM_KEY=self.base, HOME=self.tmp, NO_PROXY="*", no_proxy="*")
        srv = os.path.join(ROOT, "srv")
        p = subprocess.run([srv, "echo from-srv; exit 4"], env=env, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, timeout=60)
        self.assertIn(b"from-srv", p.stdout)
        self.assertEqual(p.returncode, 4)
        p = subprocess.run([srv, "-"], input=b"echo one\necho two\n", env=env,
                           stdout=subprocess.PIPE, timeout=60)
        self.assertIn(b"one\ntwo", p.stdout)
        local = os.path.join(self.tmp, "up.txt")
        with open(local, "w") as f:
            f.write("uploaded with spaces")
        remote = os.path.join(self.tmp, "a dir", "b c.txt")
        p = subprocess.run([srv, "put", local, remote], env=env, stdout=subprocess.PIPE, timeout=60)
        self.assertEqual(p.returncode, 0, p.stdout)
        p = subprocess.run([srv, "get", remote], env=env, stdout=subprocess.PIPE, timeout=60)
        self.assertEqual(p.stdout, b"uploaded with spaces")
        for pasted in (self.base + "/mcp", "FATLLM key: %s\n" % self.base):
            p = subprocess.run([srv, "key", pasted], env=dict(env, FATLLM_KEY=""),
                               stdout=subprocess.PIPE, timeout=60)
            self.assertEqual(p.returncode, 0)
            with open(os.path.join(self.tmp, ".fatllm-key")) as f:
                self.assertEqual(f.read().strip(), self.base)

    def test_session_hook(self):
        hook = os.path.join(ROOT, ".claude", "session-start.sh")
        env = dict(os.environ, HOME=self.tmp, CLAUDE_PROJECT_DIR=ROOT, NO_PROXY="*", no_proxy="*")
        p = subprocess.run(["bash", hook], env=dict(env, FATLLM_KEY=self.base),
                           stdout=subprocess.PIPE, timeout=60)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"connected", p.stdout)
        self.assertIn(b"hostname", p.stdout)
        p = subprocess.run(["bash", hook], env=dict(env, FATLLM_KEY=self.base.replace("fat_", "fat_x")),
                           stdout=subprocess.PIPE, timeout=60)
        self.assertEqual(p.returncode, 0)
        self.assertIn(b"did not answer", p.stdout)

    # --- MCP

    def test_mcp_flow(self):
        code, _, body = self.mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                "clientInfo": {"name": "t", "version": "1"}})
        r = json.loads(body)
        self.assertEqual(code, 200)
        self.assertEqual(r["result"]["protocolVersion"], "2025-06-18")
        self.assertIn("tools", r["result"]["capabilities"])
        code, _, body = self.mcp("notifications/initialized", mid=None)
        self.assertEqual(code, 202)
        self.assertEqual(body, b"")
        _, _, body = self.mcp("tools/list", {}, 2)
        names = [t["name"] for t in json.loads(body)["result"]["tools"]]
        self.assertEqual(names, ["run_command", "job_status", "read_file", "write_file", "server_info"])
        _, _, body = self.mcp("tools/call", {"name": "run_command", "arguments": {"command": "echo mcp-ok"}}, 3)
        res = json.loads(body)["result"]
        self.assertFalse(res["isError"])
        self.assertIn("mcp-ok", res["content"][0]["text"])
        self.assertIn("[exit 0", res["content"][0]["text"])

    def test_mcp_job_and_files(self):
        _, _, body = self.mcp("tools/call", {"name": "run_command",
                                             "arguments": {"command": "sleep 2; echo late", "wait_seconds": 0}}, 4)
        text = json.loads(body)["result"]["content"][0]["text"]
        self.assertIn("job_status", text)
        jid = text.split("job ")[1].split(" ")[0]
        _, _, body = self.mcp("tools/call", {"name": "job_status", "arguments": {"job_id": jid, "wait_seconds": 10}}, 5)
        self.assertIn("late", json.loads(body)["result"]["content"][0]["text"])
        path = os.path.join(self.tmp, "mcp.txt")
        _, _, body = self.mcp("tools/call", {"name": "write_file", "arguments": {"path": path, "content": "سلام\n"}}, 6)
        self.assertFalse(json.loads(body)["result"]["isError"])
        _, _, body = self.mcp("tools/call", {"name": "read_file", "arguments": {"path": path}}, 7)
        self.assertEqual(json.loads(body)["result"]["content"][0]["text"], "سلام\n")
        _, _, body = self.mcp("tools/call", {"name": "read_file", "arguments": {"path": "/no/such"}}, 8)
        self.assertTrue(json.loads(body)["result"]["isError"])

    def test_mcp_errors_and_batch(self):
        _, _, body = self.mcp("no/such/method", {}, 9)
        self.assertEqual(json.loads(body)["error"]["code"], -32601)
        _, _, body = self.mcp("tools/call", {"name": "nope", "arguments": {}}, 10)
        self.assertEqual(json.loads(body)["error"]["code"], -32602)
        batch = [{"jsonrpc": "2.0", "id": 1, "method": "ping"},
                 {"jsonrpc": "2.0", "method": "notifications/initialized"}]
        _, _, body = self.call("/mcp", json.dumps(batch).encode(), "POST", {"Content-Type": "application/json"})
        self.assertEqual(json.loads(body), [{"jsonrpc": "2.0", "id": 1, "result": {}}])
        code, _, _ = self.call("/mcp")
        self.assertEqual(code, 405)

    # --- relays

    def test_relay_via_injects_proxy(self):
        _, _, body = self.call("/run?via=iran", b'echo "$ALL_PROXY|$https_proxy|$NO_PROXY|$HTTP_PROXY"', "POST")
        t = body.decode()
        self.assertIn("socks5h://127.0.0.1:9999", t)
        self.assertIn("127.0.0.1,localhost", t)

    def test_relay_unknown_is_400(self):
        code, _, body = self.call("/run?via=nope", b"true", "POST")
        self.assertEqual(code, 400)
        self.assertIn("unknown relay", body.decode())

    def test_relay_in_guide_and_info(self):
        _, _, body = self.call("/")
        self.assertIn("via=iran", body.decode())
        _, _, body = self.call("/info")
        self.assertIn("iran", json.loads(body).get("relays", {}))

    def test_relay_mcp_via(self):
        _, _, body = self.mcp("tools/call", {"name": "run_command",
                                             "arguments": {"command": 'echo "$ALL_PROXY"', "via": "iran"}}, 30)
        self.assertIn("socks5h://127.0.0.1:9999", json.loads(body)["result"]["content"][0]["text"])
        _, _, body = self.mcp("tools/call", {"name": "run_command",
                                             "arguments": {"command": "true", "via": "nope"}}, 31)
        self.assertTrue(json.loads(body)["result"]["isError"])

    def test_relay_srv_via(self):
        env = dict(os.environ, FATLLM_KEY=self.base, HOME=self.tmp, NO_PROXY="*", no_proxy="*")
        p = subprocess.run([os.path.join(ROOT, "srv"), "--via", "iran", 'echo "$ALL_PROXY"'],
                           env=env, stdout=subprocess.PIPE, timeout=60)
        self.assertIn(b"socks5h://127.0.0.1:9999", p.stdout)

    # --- web panel and its endpoints

    def test_panel_html_and_csp(self):
        code, headers, body = self.call("/panel")
        self.assertEqual(code, 200)
        self.assertIn("text/html", headers["Content-Type"])
        html = body.decode()
        self.assertNotIn("{{NONCE}}", html)
        csp = headers["Content-Security-Policy"]
        nonce = csp.split("'nonce-")[1].split("'")[0]
        self.assertIn('nonce="%s"' % nonce, html)
        self.assertIn("frame-ancestors 'none'", csp)
        self.assertNotIn(TOKEN, html)  # the page learns its key from its own address
        code, _, _ = self.call("/panel", base="http://127.0.0.1:%d/fat_wrong" % self.port)
        self.assertEqual(code, 404)

    def test_stats(self):
        _, _, body = self.call("/stats")
        s = json.loads(body)
        for k in ("hostname", "cpus", "load", "mem_total", "disk_total", "mode", "expires", "relays"):
            self.assertIn(k, s)
        self.assertEqual(s["relays"][0]["name"], "iran")
        self.assertFalse(s["relays"][0]["up"])  # nothing listens on the test socks port

    def test_audit_endpoint(self):
        self.call("/run", b"echo audit-endpoint\necho two", "POST")
        time.sleep(0.3)
        _, _, body = self.call("/audit?n=50")
        entries = json.loads(body)["entries"]
        runs = [e for e in entries if e["action"] == "run" and "audit-endpoint" in e["detail"]]
        self.assertTrue(runs)
        self.assertIn("audit-endpoint\necho two", runs[0]["detail"])  # newlines restored
        self.assertNotIn(TOKEN, body.decode())

    def test_jobs_json(self):
        self.call("/run", b"echo jobs-json", "POST")
        _, _, body = self.call("/jobs?json=1")
        jobs = json.loads(body)["jobs"]
        self.assertTrue(any(j["command"] == "echo jobs-json" and j["done"] for j in jobs))

    def test_dir_listing_json_and_download_name(self):
        d = os.path.join(self.tmp, "listing")
        os.makedirs(os.path.join(d, "sub"), exist_ok=True)
        with open(os.path.join(d, "a b.txt"), "w") as f:
            f.write("x")
        _, _, body = self.call("/file?json=1&path=" + d)
        r = json.loads(body)
        self.assertEqual([e["name"] for e in r["entries"]], ["sub", "a b.txt"])  # folders first
        self.assertTrue(r["entries"][0]["dir"])
        _, headers, _ = self.call("/file?download=1&path=" + os.path.join(d, "a%20b.txt"))
        self.assertIn("filename*=UTF-8''a%20b.txt", headers["Content-Disposition"])

    def test_admin_expire(self):
        code, _, body = self.call("/admin/expire", b'{"in": "2h"}', "POST", {"Content-Type": "application/json"})
        r = json.loads(body)
        self.assertEqual(code, 200)
        self.assertTrue(r["saved"])
        self.assertAlmostEqual(r["expires"], time.time() + 7200, delta=30)
        with open(self.conf) as f:
            self.assertIn("FATLLM_EXPIRES=%d" % r["expires"], f.read())
        code, _, _ = self.call("/info")
        self.assertEqual(code, 200)  # still valid
        code, _, body = self.call("/admin/expire", b'{"in": "soon"}', "POST", {"Content-Type": "application/json"})
        self.assertEqual(code, 400)
        _, _, body = self.call("/admin/expire", b'{"in": "never"}', "POST", {"Content-Type": "application/json"})
        self.assertEqual(json.loads(body)["expires"], 0)

    def test_admin_off_without_cli(self):
        if shutil.which("fatllm") or os.path.exists("/usr/local/bin/fatllm"):
            self.skipTest("a real fatllm command is installed here")
        code, _, _ = self.call("/admin/off", b"", "POST")
        self.assertEqual(code, 409)

    def test_audit_log_has_no_token(self):
        self.call("/run", b"echo audited", "POST")
        time.sleep(0.3)
        with open(os.path.join(self.tmp, "log", "audit.log")) as f:
            log = f.read()
        self.assertIn("echo audited", log)
        self.assertNotIn(TOKEN, log)


class RotateTest(unittest.TestCase):
    """A new key from the panel: the old one stops at once, the new one is saved to the env file."""

    def test_rotate(self):
        tmp = tempfile.mkdtemp()
        port = free_port()
        conf = os.path.join(tmp, "fatllm.env")
        with open(conf, "w") as f:
            f.write("FATLLM_TOKEN=%s\nFATLLM_MODE=caddy\n" % TOKEN)
        env = dict(os.environ, FATLLM_TOKEN=TOKEN, FATLLM_LOCAL_PORT=str(port), FATLLM_CONF=conf,
                   FATLLM_STATE_DIR=os.path.join(tmp, "s"), FATLLM_LOG_DIR=os.path.join(tmp, "l"))
        proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server", "agent.py")],
                                env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def get(url, data=None):
            try:
                with opener.open(urllib.request.Request(url, data=data, method="POST" if data is not None else "GET"), timeout=10) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()
        old = "http://127.0.0.1:%d/%s" % (port, TOKEN)
        try:
            for _ in range(50):
                try:
                    if get(old + "/info")[0] == 200:
                        break
                except Exception:
                    time.sleep(0.1)
            code, body = get(old + "/admin/rotate", b"")
            self.assertEqual(code, 200)
            r = json.loads(body)
            new_key = r["key"]
            self.assertTrue(r["saved"])
            self.assertNotEqual(new_key, old)
            self.assertEqual(get(old + "/info")[0], 404)
            self.assertEqual(get(new_key + "/info")[0], 200)
            new_token = new_key.rsplit("/", 1)[1]
            with open(conf) as f:
                saved = f.read()
            self.assertIn("FATLLM_TOKEN=%s\n" % new_token, saved)
            self.assertIn("FATLLM_MODE=caddy", saved)  # other settings kept
            self.assertEqual(oct(os.stat(conf).st_mode & 0o777), "0o600")
        finally:
            proc.terminate()
            proc.wait(5)
            shutil.rmtree(tmp, ignore_errors=True)


class ReverseRelayTest(unittest.TestCase):
    """`fatllm relay reverse add` wiring (no live SSH): restricted key, json, and the paste block."""

    def test_reverse_relay_setup(self):
        if os.geteuid() != 0 or not shutil.which("ssh-keygen"):
            self.skipTest("needs root and ssh-keygen")
        tmp = tempfile.mkdtemp()
        conf = os.path.join(tmp, "fatllm.env")
        with open(conf, "w") as f:
            f.write("FATLLM_TOKEN=%s\nFATLLM_MODE=direct\nFATLLM_HOST=203.0.113.10\nFATLLM_PORT=9001\n"
                    "FATLLM_PUBLIC_URL=https://203.0.113.10:9001\nFATLLM_EXPIRES=0\n" % TOKEN)
        ak = os.path.join(tmp, "authorized_keys")
        env = dict(os.environ, FATLLM_CONF=conf, FATLLM_RELAY_DIR=os.path.join(tmp, "relays"),
                   FATLLM_AUTHKEYS=ak, FATLLM_AUDIT_LOG=os.path.join(tmp, "a.log"), TERM="dumb")
        fatllm = os.path.join(ROOT, "server", "fatllm")
        try:
            p = subprocess.run(["bash", fatllm, "relay", "reverse", "add", "--name", "iran", "--from", "37.32.6.236"],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout)
            out = p.stdout.decode()
            # restricted key: forwarding only, never a shell
            with open(ak) as f:
                line = f.read().strip()
            self.assertTrue(line.startswith('restrict,port-forwarding,from="37.32.6.236" ssh-ed25519 '))
            self.assertTrue(line.endswith("fatllm-reverse-iran"))
            with open(os.path.join(tmp, "relays", "iran.json")) as f:
                j = json.load(f)
            self.assertTrue(j["reverse"])
            self.assertEqual(j["user"], "root")
            # the paste block for the other server is valid bash and dials back with a reverse SOCKS
            block = out[out.index("# ===== FATLLM reverse"):out.index("# ===== end") + 10]
            chk = subprocess.run(["bash", "-n"], input=block.encode(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            self.assertEqual(chk.returncode, 0, chk.stdout)
            self.assertIn("-R 127.0.0.1:%d" % j["socks_port"], block)
            self.assertIn("root@203.0.113.10", block)   # the configured host, dialed from the other side
            self.assertIn("-----BEGIN OPENSSH PRIVATE KEY-----", block)
            self.assertNotIn(TOKEN, out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class DirectTLSTest(unittest.TestCase):
    """The agent serving HTTPS itself with a self-signed cert, and srv pinning it (direct mode)."""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("openssl"):
            raise unittest.SkipTest("openssl not available")
        cls.tmp = tempfile.mkdtemp()
        cls.port = free_port()
        crt = os.path.join(cls.tmp, "cert.pem")
        key = os.path.join(cls.tmp, "key.pem")
        cfg = os.path.join(cls.tmp, "openssl.cnf")
        with open(cfg, "w") as f:
            f.write("[req]\ndistinguished_name=dn\nx509_extensions=v3\nprompt=no\n"
                    "[dn]\nCN=localhost\n[v3]\nsubjectAltName=DNS:localhost,IP:127.0.0.1\n")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", key, "-out", crt, "-days", "3650", "-config", cfg],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        env = dict(os.environ, FATLLM_TOKEN=TOKEN, FATLLM_LOCAL_PORT=str(cls.port),
                   FATLLM_LISTEN="127.0.0.1", FATLLM_TLS_CERT=crt, FATLLM_TLS_KEY=key,
                   FATLLM_STATE_DIR=os.path.join(cls.tmp, "state"),
                   FATLLM_LOG_DIR=os.path.join(cls.tmp, "log"),
                   FATLLM_CLIENT=os.path.join(ROOT, "srv"))
        cls.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "server", "agent.py")],
                                    env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        cls.base = "https://localhost:%d/%s" % (cls.port, TOKEN)
        ctx = __import__("ssl").SSLContext(__import__("ssl").PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = __import__("ssl").CERT_NONE
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.base + "/info", timeout=1, context=ctx)
                break
            except urllib.error.HTTPError:
                break
            except Exception:
                time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(5)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_srv_pins_selfsigned_and_runs(self):
        home = tempfile.mkdtemp()
        try:
            env = dict(os.environ, HOME=home, NO_PROXY="*", no_proxy="*", FATLLM_KEY="")
            srv = os.path.join(ROOT, "srv")
            p = subprocess.run([srv, "key", self.base], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertIn(b"pinned", p.stdout)
            self.assertTrue(os.path.exists(os.path.join(home, ".fatllm-cert")))
            p = subprocess.run([srv, "echo tls-ok; exit 7"], env=env, stdout=subprocess.PIPE, timeout=60)
            self.assertIn(b"tls-ok", p.stdout)
            self.assertEqual(p.returncode, 7)
        finally:
            shutil.rmtree(home, ignore_errors=True)

    def test_srv_pins_selfsigned_on_bare_ip(self):
        # direct mode keys use the server's IP (no DNS name needed): https://<ip>:<port>/fat_...
        home = tempfile.mkdtemp()
        try:
            env = dict(os.environ, HOME=home, NO_PROXY="*", no_proxy="*", FATLLM_KEY="")
            srv = os.path.join(ROOT, "srv")
            ip_key = "https://127.0.0.1:%d/%s" % (self.port, TOKEN)
            p = subprocess.run([srv, "key", "FATLLM key: " + ip_key], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
            self.assertEqual(p.returncode, 0, p.stdout)
            self.assertIn(b"pinned", p.stdout)
            p = subprocess.run([srv, "echo ip-ok"], env=env, stdout=subprocess.PIPE, timeout=60)
            self.assertIn(b"ip-ok", p.stdout)
        finally:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
