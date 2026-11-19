"""Runs the real install.sh, with every system path moved into a temporary folder and a stand-in
systemctl that starts the units as plain background processes. Catches what the agent tests
cannot see: a fresh server (no settings file yet), a re-run, and an upgrade from version 1.

    python3 -m unittest discover -s test -v
"""
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import unittest
import urllib.request
import ssl

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

FAKE_SYSTEMCTL = r'''#!/bin/bash
# stand-in for systemctl: runs ExecStart of a unit in the background, tracks it by pid file
RUN="@RUN@"; UNITS="@UNITS@"
q=0; now=0; args=()
for a in "$@"; do case "$a" in --quiet|-q) q=1 ;; --now) now=1 ;; --no-pager) ;; *) args+=("$a") ;; esac; done
cmd="${args[0]:-}"; units=("${args[@]:1}")
alive() { [ -f "$RUN/$1.pid" ] && kill -0 "$(cat "$RUN/$1.pid")" 2>/dev/null; }
start() { local f="$UNITS/$1.service"; [ -f "$f" ] || return 1; alive "$1" && return 0
  local envf exe; envf="$(sed -n 's/^EnvironmentFile=//p' "$f")"; exe="$(sed -n 's/^ExecStart=//p' "$f")"
  ( if [ -n "$envf" ]; then set -a; . "$envf"; set +a; fi; exec $exe ) >>"$RUN/$1.log" 2>&1 & echo $! > "$RUN/$1.pid"; }
stop() { if alive "$1"; then kill "$(cat "$RUN/$1.pid")"; sleep 0.3; fi; rm -f "$RUN/$1.pid"; }
case "$cmd" in
  is-active) for u in "${units[@]}"; do if alive "${u%.service}"; then [ $q = 1 ] || echo active; else [ $q = 1 ] || echo inactive; exit 3; fi; done ;;
  start) for u in "${units[@]}"; do start "${u%.service}"; done ;;
  restart) for u in "${units[@]}"; do stop "${u%.service}"; start "${u%.service}" || exit 5; done ;;
  stop) for u in "${units[@]}"; do stop "${u%.service}"; done ;;
  enable) if [ $now = 1 ]; then for u in "${units[@]}"; do start "${u%.service}"; done; fi ;;
  disable) if [ $now = 1 ]; then for u in "${units[@]}"; do stop "${u%.service}"; done; fi ;;
esac
exit 0
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@unittest.skipUnless(shutil.which("openssl") and shutil.which("curl"), "needs openssl and curl")
class InstallTest(unittest.TestCase):
    def setUp(self):
        self.sb = tempfile.mkdtemp()
        for d in ("bin", "run", "etc/systemd/system", "usr/local/bin", "systemd-run"):
            os.makedirs(os.path.join(self.sb, d))
        with open(os.path.join(ROOT, "install.sh")) as f:
            script = f.read()
        sb = self.sb
        for path in ("/opt/fatllm", "/etc/fatllm", "/var/lib/fatllm", "/var/log/fatllm", "/etc/systemd/system",
                     "/usr/local/bin/fatllm"):
            script = script.replace(path, sb + path)
        script = script.replace("/run/systemd/system", sb + "/systemd-run")
        # this machine stands in for the server: its "public" address is 127.0.0.1, and it is root
        script = script.replace('IP="$(public_ip || true)"', "IP=127.0.0.1")
        script = script.replace('if [ -z "$IP" ] || is_private "$IP"; then', 'if [ -z "$IP" ]; then')
        script = script.replace('if [ "$(id -u)" != 0 ]; then', "if false; then")
        script = re.sub(r"(?m)^fatllm key$", 'FATLLM_CONF="$CONF" FATLLM_APP="$APP" %s/usr/local/bin/fatllm key' % sb,
                        script)
        left = re.findall(r"(?<!%s)(?:/opt/fatllm|/etc/fatllm|/var/lib/fatllm|/var/log/fatllm|/etc/systemd/system|"
                          r"/usr/local/bin/fatllm|/run/systemd/system)" % re.escape(sb), script)
        self.assertEqual(left, [], "the installer copy still names a system path")
        self.installer = os.path.join(sb, "install.sh")
        with open(self.installer, "w") as f:
            f.write(script)
        fake = os.path.join(sb, "bin", "systemctl")
        with open(fake, "w") as f:
            f.write(FAKE_SYSTEMCTL.replace("@RUN@", sb + "/run").replace("@UNITS@", sb + "/etc/systemd/system"))
        os.chmod(fake, 0o755)
        with open(os.path.join(sb, "bin", "journalctl"), "w") as f:
            f.write("#!/bin/sh\ntail -n 20 %s/run/fatllm.log\n" % sb)
        os.chmod(os.path.join(sb, "bin", "journalctl"), 0o755)
        self.env = dict(os.environ, PATH=sb + "/bin:" + os.environ["PATH"], FATLLM_RAW="file://" + ROOT,
                        NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost", TERM="dumb")
        self.conf = os.path.join(sb, "etc/fatllm/fatllm.env")

    def tearDown(self):
        subprocess.run([os.path.join(self.sb, "bin", "systemctl"), "stop", "fatllm", "fatllm-caddy"], env=self.env)
        shutil.rmtree(self.sb, ignore_errors=True)

    def install(self, *args):
        p = subprocess.run(["bash", self.installer] + list(args), env=self.env, stdin=subprocess.DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=240)
        return p.returncode, p.stdout.decode()

    def key_works(self, url):
        ctx = ssl._create_unverified_context()
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=ctx))
        try:
            with opener.open(url + "/info", timeout=10) as r:
                return r.status == 200 and "hostname" in json.loads(r.read())
        except Exception:
            return False

    def key_line(self, out):
        m = re.search(r"FATLLM key: (https://\S+/fat_[A-Za-z0-9_-]+)", out)
        self.assertTrue(m, out)
        return m.group(1)

    def test_fresh_then_rerun(self):
        port = free_port()
        rc, out = self.install("--port", str(port))
        self.assertEqual(rc, 0, out)
        self.assertIn("New key made", out)
        key = self.key_line(out)
        self.assertTrue(key.startswith("https://127.0.0.1:%d/" % port))
        self.assertTrue(self.key_works(key))
        self.assertEqual(oct(os.stat(self.conf).st_mode & 0o777), "0o600")
        rc, out = self.install()                      # running it again is an update
        self.assertEqual(rc, 0, out)
        self.assertIn("Keys kept", out)
        self.assertIn("Kept the certificate", out)
        self.assertEqual(self.key_line(out), key)
        self.assertTrue(self.key_works(key))

    def test_upgrade_from_version_1(self):
        port = free_port()
        token = "fat_" + secrets.token_urlsafe(32)
        os.makedirs(os.path.dirname(self.conf))
        with open(self.conf, "w") as f:
            f.write("FATLLM_TOKEN=%s\nFATLLM_EXPIRES=0\nFATLLM_MODE=direct\nFATLLM_HOST=127.0.0.1\nFATLLM_PORT=%d\n"
                    "FATLLM_PUBLIC_URL=https://127.0.0.1:%d\nFATLLM_LOCAL_PORT=%d\n" % (token, port, port, port))
        rc, out = self.install()
        self.assertEqual(rc, 0, out)
        self.assertIn("version 1 key is kept", out)
        self.assertTrue(self.key_works("https://127.0.0.1:%d/%s" % (port, token)))
        with open(self.conf) as f:
            self.assertNotIn("FATLLM_TOKEN", f.read())   # the key lives in keys.json now


if __name__ == "__main__":
    unittest.main()
