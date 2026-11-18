"""Tests for images/sandbx-adb-relay, run as a real process.

The relay is there because adb shell hangs on krun's TSI sockets when it
reads on one thread and writes on another. That pattern goes through the
relay here; under krun the relay's TCP side is TSI, so it is the real check.
"""

import os
import random
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

RELAY = Path(__file__).resolve().parent.parent / "images" / "sandbx-adb-relay"

# Blocks in recv() on one thread, sends from another 0.3s later: adb shell.
CLIENT = r"""
import socket, sys, threading, time
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sys.argv[1])
got = []
reader = threading.Thread(target=lambda: got.append(s.recv(64)), daemon=True)
reader.start()
time.sleep(0.3)
s.sendall(b"ping")
reader.join(5)
print(got[0].decode() if got else "HUNG")
"""

# Only reads: the relay should hang up rather than leave it waiting.
CLIENT_EOF = r"""
import socket, sys
s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
s.connect(sys.argv[1])
s.settimeout(5)
print("EOF" if s.recv(64) == b"" else "DATA")
"""


def tcp_listener():
    """A listener on an explicit free port. Not port 0: under krun, TSI hands
    back a port from getsockname() that nothing in the guest can connect to."""
    for _ in range(100):
        try:
            return socket.create_server(("127.0.0.1", random.randint(40000, 60000)))
        except OSError:
            continue
    raise RuntimeError("no free port for the upstream stand-in")


class Upstream:
    """TCP stand-in for the host adb server: answers each request once."""

    def __init__(self):
        self.sock = tcp_listener()
        self.target = "127.0.0.1:%d" % self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with conn:
                conn.sendall(b"pong:" + conn.recv(64))

    def close(self):
        self.sock.close()


class TestAdbRelay(unittest.TestCase):

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "adb.sock")
        self.upstream = Upstream()

    def tearDown(self):
        self.upstream.close()
        self.dir.cleanup()

    def relay(self, *command, path=None, target=None, timeout=20):
        return subprocess.run(
            [sys.executable, "-I", str(RELAY), path or self.path,
             target or self.upstream.target, "--", *command],
            capture_output=True, text=True, timeout=timeout)

    def refuses(self, timeout=5.0):
        """True once nothing accepts on the socket any more."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                s.connect(self.path)
            except (ConnectionRefusedError, FileNotFoundError):
                return True
            finally:
                s.close()
            time.sleep(0.1)
        return False

    def test_concurrent_read_and_write_get_through(self):
        r = self.relay(sys.executable, "-I", "-c", CLIENT, self.path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "pong:ping")

    def test_relay_exits_after_the_command(self):
        r = self.relay("true")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.refuses(), "relay outlived its command")

    def test_command_keeps_its_exit_status(self):
        self.assertEqual(self.relay("sh", "-c", "exit 7").returncode, 7)

    def test_command_gets_default_sigpipe_and_sigxfsz(self):
        """Python ignores both; inherited, `yes | head` in the shell breaks."""
        r = self.relay("grep", "SigIgn", "/proc/self/status")
        self.assertEqual(r.returncode, 0, r.stderr)
        ignored = int(r.stdout.split()[1], 16)
        for signum in (13, 25):  # SIGPIPE, SIGXFSZ
            self.assertFalse(ignored & (1 << (signum - 1)), r.stdout)

    def test_command_still_runs_when_the_socket_cannot_be_made(self):
        r = self.relay("echo", "ran", path=os.path.join(self.dir.name, "no", "adb.sock"))
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "ran")
        self.assertIn("adb will not reach the host", r.stderr)

    def test_unreachable_server_closes_the_client(self):
        """adb then reports a protocol error instead of hanging."""
        dead = tcp_listener()
        target = "127.0.0.1:%d" % dead.getsockname()[1]
        dead.close()
        r = self.relay(sys.executable, "-I", "-c", CLIENT_EOF, self.path, target=target)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "EOF")

    def test_bad_usage_is_refused(self):
        for args in ([], [self.path, "127.0.0.1:1", "true"],
                     [self.path, "nohostport", "--", "true"]):
            with self.subTest(args=args):
                r = subprocess.run([sys.executable, "-I", str(RELAY), *args],
                                   capture_output=True, text=True, timeout=10)
                self.assertNotEqual(r.returncode, 0)
                self.assertIn("usage:", r.stderr)


if __name__ == "__main__":
    unittest.main()
