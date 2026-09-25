"""Regression cases from the review of the installed 2.0 runtime."""

import http.client
import io
import json
import socket
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import remctl_mcp as mcp
import remctl_runtime
from helpers import load_module
from test_mcp_server import FakeExecutor, make_server


class RuntimeReviewTests(unittest.TestCase):
    def test_empty_notes_reach_the_cli(self):
        argv = mcp._argv_update_reminder({"reminder_id": 42, "notes": ""})
        self.assertEqual(argv, ["edit", "42", "--notes", "", "--json"])

    def test_partial_import_preserves_created_ids_and_retry_details(self):
        summary = {"status": "partial", "created": 1, "createdIds": [42],
                   "errors": [{"index": 1, "message": "invalid due date"}], "total": 2}
        result = mcp.tool_result_from_command(mcp.TOOLS_BY_NAME["run"],
            mcp.CommandResult([], 1, json.dumps(summary), "imported index=0 id=42\n"))
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["createdIds"], [42])
        self.assertEqual(result["structuredContent"]["errors"], summary["errors"])
        self.assertEqual(json.loads(result["content"][0]["text"]), result["structuredContent"])

    def test_invalid_cancellation_does_not_disconnect_stdio(self):
        executor = mcp.CommandExecutor(["unused"])
        server, _ = make_server(executor)
        messages = [{"jsonrpc": "2.0", "method": "notifications/cancelled",
                     "params": {"requestId": []}},
                    {"jsonrpc": "2.0", "id": 9, "method": "ping"}]
        output = io.BytesIO()
        mcp.serve_stdio(server, io.BytesIO(b"".join(json.dumps(m).encode() + b"\n" for m in messages)), output)
        self.assertEqual(json.loads(output.getvalue())["id"], 9)

    def test_deep_json_and_surrogate_ids_do_not_disconnect_stdio(self):
        server, _ = make_server()
        output = io.BytesIO()
        incoming = b"[" * 2000 + b"0" + b"]" * 2000 + b"\n"
        incoming += json.dumps({"jsonrpc": "2.0", "id": "\ud800", "method": "ping"}).encode() + b"\n"
        mcp.serve_stdio(server, io.BytesIO(incoming), output)
        replies = [json.loads(line) for line in output.getvalue().splitlines()]
        first = replies[0][0] if isinstance(replies[0], list) else replies[0]
        self.assertIn(first["error"]["code"], (mcp.ERR_PARSE, mcp.ERR_INVALID_REQUEST))
        self.assertEqual(replies[1]["id"], "\ud800")

    def test_invalid_unicode_is_rejected_before_spawning_a_command(self):
        server, executor = make_server()
        for args in ({"title": "\ud800"}, {"title": "test", "notes": "\ud800"}):
            result = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                           "params": {"name": "create_reminder", "arguments": args}})
            self.assertEqual(result["result"]["structuredContent"]["error"]["code"], "invalid_argument")
        self.assertEqual(executor.calls, [])

    def test_request_and_cancellation_ids_are_scoped_to_the_session(self):
        server, executor = make_server()
        first, second = mcp.LegacySession(id="first"), mcp.LegacySession(id="second")
        call = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "lists"}}
        server.handle_message(call, first)
        server.handle_message(call, second)
        self.assertNotEqual(executor.calls[0]["key"], executor.calls[1]["key"])
        server.handle_message({"jsonrpc": "2.0", "method": "notifications/cancelled",
                               "params": {"requestId": 1}}, first)
        self.assertEqual(executor.cancelled, [executor.calls[0]["key"]])

    def test_duplicate_inflight_id_does_not_replace_the_running_process(self):
        executor = mcp.CommandExecutor([sys.executable, "-c", "import time; time.sleep(30)"])
        results = []
        worker = threading.Thread(target=lambda: results.append(executor.run("same", [], timeout=5)))
        worker.start()
        try:
            deadline = time.monotonic() + 3
            while "same" not in executor._processes and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertIn("same", executor._processes)
            duplicate = executor.run("same", [], timeout=0.1)
            self.assertEqual(json.loads(duplicate.stderr)["code"], "duplicate_request_id")
        finally:
            executor.cancel("same")
            worker.join(6)
        self.assertFalse(worker.is_alive())
        self.assertTrue(results[0].cancelled)

    def test_cancel_before_a_worker_starts_prevents_the_write(self):
        executor = mcp.CommandExecutor(["unused"])
        ticket = executor.reserve("queued")
        self.assertIsNotNone(ticket)
        self.assertTrue(executor.cancel("queued"))
        with mock.patch.object(mcp.subprocess, "Popen") as spawn:
            result = executor.run("queued", ["delete", "42", "--force"], timeout=1)
        self.assertTrue(result.cancelled)
        spawn.assert_not_called()
        executor.release("queued", ticket)
        self.assertIsNotNone(executor.reserve("queued"))

    def test_client_config_ignores_a_preexisting_temp_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config, victim = root / "client.json", root / "unrelated.txt"
            config.write_text('{"old":true}')
            victim.write_text("keep me")
            (root / "client.json.remctl-tmp").symlink_to(victim)
            mcp._write_json_config(config, {"new": True})
            self.assertEqual(victim.read_text(), "keep me")
            self.assertEqual(json.loads(config.read_text()), {"new": True})

    def test_private_config_write_failure_keeps_the_previous_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.json"
            remctl_runtime.write_private_text_file(path, "old-token")
            with mock.patch.object(remctl_runtime.os, "replace", side_effect=OSError("disk failure")):
                with self.assertRaises(OSError):
                    remctl_runtime.write_private_text_file(path, "new-token")
            self.assertEqual(path.read_text(), "old-token")
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_bundle_creation_ignores_a_preexisting_temp_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output, victim = root / "remctl.mcpb", root / "unrelated.txt"
            victim.write_text("keep me")
            (root / "remctl.mcpb.tmp").symlink_to(victim)
            mcp.build_bundle(Path("/unused/remctl"), "2.0.0", output)
            self.assertEqual(victim.read_text(), "keep me")
            self.assertTrue(output.is_file())

    def test_token_rotation_reports_a_failed_restart_without_printing_the_token(self):
        cli = load_module("remctl_review_cli", "remctl")
        output = io.StringIO()
        with mock.patch.object(mcp, "rotate_http_token", return_value={"token": "do-not-print"}), \
             mock.patch.object(mcp, "http_agent_status", return_value={"loaded": True}), \
             mock.patch.object(mcp, "restart_http_agent", return_value=False), \
             mock.patch("sys.stdout", output):
            with self.assertRaises(SystemExit) as error:
                cli.cmd_mcp(SimpleNamespace(mcp_action="token", rotate=True, json=True))
        self.assertEqual(error.exception.code, 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ok"])
        self.assertIn("old token", result["error"])
        self.assertNotIn("do-not-print", output.getvalue())

    def test_tailscale_install_does_not_claim_an_unhealthy_endpoint_is_ready(self):
        status = {"installed": True, "running": True, "https": True, "hostname": "mac.ts.net", "ips": []}
        with mock.patch.object(mcp, "tailscale_status", return_value=status), \
             mock.patch.object(mcp, "ensure_http_config", return_value={"token": "test", "port": 7362}), \
             mock.patch.object(mcp, "save_http_config"), \
             mock.patch.object(mcp, "install_http_agent", return_value={"ok": True}), \
             mock.patch.object(mcp, "tailscale_serve_enable", return_value={"ok": True}), \
             mock.patch.object(mcp, "http_health", return_value={"ok": False}), \
             mock.patch.object(mcp.time, "sleep"):
            result = mcp.install_tailscale(Path("/unused/remctl"))
        self.assertFalse(result["ok"])
        self.assertNotIn("can now connect", result["note"])


class HTTPReviewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server, cls.executor = make_server(FakeExecutor())
        cls.httpd = mcp.make_http_server(cls.server, mcp.HTTPTransportConfig("test-token"), port=0)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join()

    def raw_status(self, headers):
        # No body is sent: framing and authentication must reject before reading it.
        with socket.create_connection(("127.0.0.1", self.port), timeout=2) as sock:
            sock.sendall((f"POST /mcp HTTP/1.1\r\nHost: localhost\r\n{headers}\r\n\r\n").encode("latin-1"))
            return int(sock.recv(4096).split(b" ")[1])

    def test_unauthenticated_body_is_not_read(self):
        self.assertEqual(self.raw_status("Content-Length: 4000000"), 401)

    def test_invalid_body_framing_is_rejected_before_reading(self):
        for framing in ("Content-Length: -1", "Content-Length: 1\r\nContent-Length: 2",
                        "Transfer-Encoding: chunked", "Content-Length: +2"):
            with self.subTest(framing=framing):
                self.assertEqual(self.raw_status("Authorization: Bearer test-token\r\n" + framing), 400)

    def test_non_ascii_bearer_token_is_rejected(self):
        self.assertEqual(self.raw_status("Authorization: Bearer caf\xe9\r\nContent-Length: 0"), 401)

    def test_malformed_origin_is_rejected(self):
        self.assertEqual(self.raw_status("Authorization: Bearer test-token\r\nOrigin: http://[\r\nContent-Length: 0"), 403)

    def test_persisted_token_rotation_revokes_old_tokens_without_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "mcp-http.json"
            mcp.save_http_config({"token": "old-token"}, config)
            transport = mcp.HTTPTransportConfig("old-token", token_path=config)
            def status(token):
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                try:
                    conn.request("POST", "/mcp", json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
                                 {"Authorization": "Bearer " + token})
                    response = conn.getresponse(); response.read()
                    return response.status
                finally:
                    conn.close()
            with mock.patch.object(self.httpd.RequestHandlerClass, "transport", transport):
                self.assertEqual(status("old-token"), 200)
                mcp.save_http_config({"token": "new-token"}, config)
                self.assertEqual(status("old-token"), 401)
                self.assertEqual(status("new-token"), 200)
                config.unlink()
                self.assertEqual(status("new-token"), 401)

    def test_session_limit_and_expiry(self):
        headers = {"Authorization": "Bearer test-token"}
        with mock.patch.object(mcp, "HTTP_MAX_SESSIONS", 1):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
            try:
                init = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
                conn.request("POST", "/mcp", init, headers)
                response = conn.getresponse()
                session_id = response.getheader("Mcp-Session-Id")
                response.read()
                self.assertEqual(response.status, 200)
                conn.request("POST", "/mcp", init, headers)
                response = conn.getresponse(); response.read()
                self.assertEqual(response.status, 503)
                self.httpd.RequestHandlerClass.sessions[session_id].last_used = 0
                conn.request("POST", "/mcp", init, headers)
                response = conn.getresponse(); response.read()
                self.assertEqual(response.status, 200)
                self.assertNotEqual(response.getheader("Mcp-Session-Id"), session_id)
            finally:
                conn.close()
                self.httpd.RequestHandlerClass.sessions.clear()

    def test_connection_limit_rejects_without_allocating_another_thread(self):
        # Hold every slot without allocating real idle client threads.
        slots = self.httpd._connections
        for _ in range(mcp.HTTP_MAX_CONNECTIONS):
            self.assertTrue(slots.acquire(timeout=2))
        try:
            self.assertEqual(self.raw_status("Content-Length: 0"), 503)
        finally:
            for _ in range(mcp.HTTP_MAX_CONNECTIONS):
                slots.release()


if __name__ == "__main__":
    unittest.main()
