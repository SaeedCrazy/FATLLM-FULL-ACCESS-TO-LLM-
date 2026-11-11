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
        env = dict(os.environ, FATLLM_TOKEN=TOKEN, FATLLM_LOCAL_PORT=str(cls.port),
                   FATLLM_STATE_DIR=os.path.join(cls.tmp, "state"),
                   FATLLM_LOG_DIR=os.path.join(cls.tmp, "log"),
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
        p = subprocess.run([srv, "key", self.base + "/mcp"], env=dict(env, FATLLM_KEY=""),
                           stdout=subprocess.PIPE, timeout=60)
        self.assertEqual(p.returncode, 0)
        with open(os.path.join(self.tmp, ".fatllm-key")) as f:
            self.assertEqual(f.read().strip(), self.base)

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

    def test_audit_log_has_no_token(self):
        self.call("/run", b"echo audited", "POST")
        time.sleep(0.3)
        with open(os.path.join(self.tmp, "log", "audit.log")) as f:
            log = f.read()
        self.assertIn("echo audited", log)
        self.assertNotIn(TOKEN, log)


if __name__ == "__main__":
    unittest.main()
