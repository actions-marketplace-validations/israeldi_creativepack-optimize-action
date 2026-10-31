"""Real local HTTP protocol checks using synthetic/example data; no gateway calls."""
import base64
import contextlib
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import importlib.util
import io
import json
from pathlib import Path
import random
import ssl
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("creativepack_action", ROOT / "run.py")
action = importlib.util.module_from_spec(spec)
spec.loader.exec_module(action)
ORIGINAL = (ROOT / "tests/fixtures/sample-creative.zip").read_bytes()
CANONICAL = json.loads((ROOT / "tests/fixtures/optimize-response.json").read_text())


def synthetic_archive():
    data = io.BytesIO()
    with zipfile.ZipFile(data, "w", compression=zipfile.ZIP_STORED) as z:
        z.writestr("index.html", b"<html><script src='ad.js'></script></html>")
        z.writestr("ad.js", random.Random(42).randbytes(2200))
    return data.getvalue()


def response_for(original, output=None, budget=1024):
    output = original if output is None else output
    report = copy.deepcopy(CANONICAL["report"])
    report.update(status="within_budget" if len(output) <= budget else "over_budget",
                  budget_bytes=budget, input_bytes=len(original), output_bytes=len(output),
                  saved_bytes=len(original)-len(output), input_sha256=hashlib.sha256(original).hexdigest(),
                  output_sha256=hashlib.sha256(output).hexdigest(), selection="original")
    report["files"] = []
    with zipfile.ZipFile(io.BytesIO(original)) as before, zipfile.ZipFile(io.BytesIO(output)) as after:
        for name in before.namelist():
            old, new = before.read(name), after.read(name)
            report["files"].append(dict(path=name, is_directory=False, kind="non_image",
                input_bytes=len(old), output_bytes=len(new), input_sha256=hashlib.sha256(old).hexdigest(),
                output_sha256=hashlib.sha256(new).hexdigest()))
    return {"archive_base64": base64.b64encode(output).decode(), "report": report}


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_POST(self):
        request = self.rfile.read(int(self.headers["Content-Length"]))
        self.server.calls.append((self.path, dict(self.headers), request))
        if self.server.hook:
            self.server.hook()
        self.send_response(self.server.status)
        self.send_header("Content-Type", self.server.content_type)
        for key, value in self.server.extra_headers.items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(self.server.body)
        except (BrokenPipeError, ConnectionResetError):
            pass


class ActionHTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "input.zip").write_bytes(ORIGINAL)
        self.key = "synthetic-key-used-only-by-loopback-tests"
        self.env = {"GITHUB_WORKSPACE": str(self.root), "GITHUB_OUTPUT": str(self.root / "outputs"),
                    "CREATIVEPACK_RAPIDAPI_KEY": self.key, "CREATIVEPACK_INPUT": "input.zip",
                    "CREATIVEPACK_BUDGET_BYTES": "4096", "CREATIVEPACK_MODE": "lossless"}
        self.server.calls = []
        self.server.status = 200
        self.server.content_type = "application/json"
        self.server.extra_headers = {}
        self.server.body = json.dumps(CANONICAL).encode()
        self.server.hook = None
        self.logical_hosts = []

    def connect(self, host, *, timeout, context):
        self.logical_hosts.append(host)
        self.assertIsInstance(context, ssl.SSLContext)
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        return http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)

    def perform(self, **changes):
        env = dict(self.env, **changes)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = action.execute(env, connection_factory=self.connect)
        logs = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(self.key, logs)
        return code, logs

    def assert_no_result(self):
        self.assertFalse((self.root / "creativepack-optimized.zip").exists())
        self.assertFalse((self.root / "creativepack-report.json").exists())

    def test_canonical_http_request_and_verified_outputs(self):
        code, logs = self.perform()
        self.assertEqual(code, 0, logs)
        self.assertEqual(self.logical_hosts, ["creativepack.p.rapidapi.com"])
        self.assertEqual(len(self.server.calls), 1)
        path, headers, data = self.server.calls[0]
        self.assertEqual(path, "/v1/optimize")
        self.assertEqual(headers["X-RapidAPI-Host"], "creativepack.p.rapidapi.com")
        self.assertEqual(headers["X-RapidAPI-Key"], self.key)
        payload = json.loads(data)
        self.assertEqual(set(payload), {"archive_base64", "budget_bytes", "mode"})
        self.assertEqual(base64.b64decode(payload["archive_base64"]), ORIGINAL)
        self.assertEqual((payload["budget_bytes"], payload["mode"]), (4096, "lossless"))
        output = (self.root / "creativepack-optimized.zip").read_bytes()
        self.assertEqual(hashlib.sha256(output).hexdigest(), "e0a8bc33e56131b903d5c37a5f23912ebc674f093af9de4534562d7186759ebf")
        self.assertEqual((self.root / "input.zip").read_bytes(), ORIGINAL)
        self.assertIn("status=within_budget\n", (self.root / "outputs").read_text())
        self.assertEqual((self.root / "creativepack-report.json").stat().st_mode & 0o777, 0o600)

    def set_over_budget(self):
        original = synthetic_archive()
        (self.root / "input.zip").write_bytes(original)
        self.server.body = json.dumps(response_for(original)).encode()
        self.env["CREATIVEPACK_BUDGET_BYTES"] = "1024"

    def test_over_budget_fails_after_saving_report_and_zip(self):
        self.set_over_budget()
        code, logs = self.perform()
        self.assertEqual(code, 2, logs)
        self.assertTrue((self.root / "creativepack-optimized.zip").exists())
        self.assertIn("status=over_budget\n", (self.root / "outputs").read_text())
        self.assertEqual(len(self.server.calls), 1)

    def test_over_budget_can_be_report_only(self):
        self.set_over_budget()
        code, logs = self.perform(CREATIVEPACK_FAIL_ON_BUDGET="false")
        self.assertEqual(code, 0, logs)

    def test_balanced_option_is_sent_explicitly(self):
        response = copy.deepcopy(CANONICAL)
        response["report"]["requested_mode"] = "balanced"
        self.server.body = json.dumps(response).encode()
        code, logs = self.perform(CREATIVEPACK_MODE="balanced")
        self.assertEqual(code, 0, logs)
        self.assertEqual(json.loads(self.server.calls[0][2])["mode"], "balanced")

    def test_http_errors_never_reflect_secret_or_retry(self):
        for status in (401, 413, 422, 429, 500, 503):
            with self.subTest(status=status):
                self.server.calls = []
                self.server.status = status
                self.server.body = (self.key + " private upstream body").encode()
                code, logs = self.perform()
                self.assertEqual(code, 1)
                self.assertIn(f"HTTP {status}", logs)
                self.assertNotIn("private upstream body", logs)
                self.assertEqual(len(self.server.calls), 1)
                self.assert_no_result()

    def test_redirect_is_not_followed(self):
        self.server.status = 302
        self.server.extra_headers = {"Location": "https://example.invalid/steal"}
        code, logs = self.perform()
        self.assertEqual(code, 1, logs)
        self.assertEqual(len(self.server.calls), 1)
        self.assert_no_result()

    def test_non_json_success_is_rejected(self):
        self.server.content_type = "text/html"
        self.server.body = b"<html>gateway error</html>"
        self.assertEqual(self.perform()[0], 1)
        self.assert_no_result()

    def test_malformed_json_success_is_rejected(self):
        self.server.body = b"<html>gateway error</html>"
        self.assertEqual(self.perform()[0], 1)
        self.assert_no_result()

    def test_duplicate_json_fields_are_rejected(self):
        self.server.body = b'{"archive_base64":"first","archive_base64":"second","report":{}}'
        self.assertEqual(self.perform()[0], 1)
        self.assert_no_result()

    def test_nonstandard_json_is_rejected(self):
        self.server.body = b'{"archive_base64":"","report":{"extra":NaN}}'
        self.assertEqual(self.perform()[0], 1)
        self.assert_no_result()

    def test_content_encoding_is_rejected(self):
        self.server.extra_headers = {"Content-Encoding": "gzip"}
        self.assertEqual(self.perform()[0], 1)
        self.assert_no_result()

    def test_advertised_oversized_response_is_rejected_before_read(self):
        self.server.extra_headers = {"Content-Length": str(action.MAX_RESPONSE + 1)}
        self.server.body = b"x"
        self.assertEqual(self.perform()[0], 1)
        self.assert_no_result()

    def test_unframed_response_read_is_bounded(self):
        self.server.body = b"x" * 129
        with patch.object(action, "MAX_RESPONSE", 128):
            self.assertEqual(self.perform()[0], 1)
        self.assert_no_result()

    def test_wrong_result_digest_is_rejected(self):
        value = copy.deepcopy(CANONICAL)
        value["report"]["output_sha256"] = "0" * 64
        self.server.body = json.dumps(value).encode()
        self.assertEqual(self.perform()[0], 1)
        self.assert_no_result()

    def test_nonimage_mutation_is_rejected_even_if_claimed_as_image(self):
        original = synthetic_archive()
        stream = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(original)) as src, zipfile.ZipFile(stream, "w") as dst:
            for name in src.namelist():
                dst.writestr(name, b"bad" if name == "ad.js" else src.read(name))
        value = response_for(original, stream.getvalue(), 4096)
        value["report"]["files"][1]["kind"] = "image"
        (self.root / "input.zip").write_bytes(original)
        self.server.body = json.dumps(value).encode()
        self.assertEqual(self.perform()[0], 1)
        self.assert_no_result()

    def test_reflected_key_in_success_report_is_not_saved(self):
        value = copy.deepcopy(CANONICAL)
        value["report"]["warnings"] = ["reflected " + self.key]
        self.server.body = json.dumps(value).encode()
        self.assertEqual(self.perform()[0], 1)
        self.assert_no_result()

    def test_same_input_and_output_is_rejected_without_request(self):
        self.assertEqual(self.perform(CREATIVEPACK_OUTPUT="input.zip")[0], 1)
        self.assertEqual(self.server.calls, [])
        self.assertEqual((self.root / "input.zip").read_bytes(), ORIGINAL)

    def test_existing_output_is_preserved_without_request(self):
        target = self.root / "creativepack-optimized.zip"
        target.write_bytes(b"preserve this")
        self.assertEqual(self.perform()[0], 1)
        self.assertEqual(self.server.calls, [])
        self.assertEqual(target.read_bytes(), b"preserve this")

    def test_same_report_and_archive_is_rejected(self):
        self.assertEqual(self.perform(CREATIVEPACK_REPORT="creativepack-optimized.zip")[0], 1)
        self.assertEqual(self.server.calls, [])

    def test_outside_workspace_path_is_rejected(self):
        self.assertEqual(self.perform(CREATIVEPACK_OUTPUT="../outside.zip")[0], 1)
        self.assertEqual(self.server.calls, [])

    def test_symlink_input_is_rejected(self):
        (self.root / "link.zip").symlink_to(self.root / "input.zip")
        self.assertEqual(self.perform(CREATIVEPACK_INPUT="link.zip")[0], 1)
        self.assertEqual(self.server.calls, [])

    def test_invalid_zip_is_rejected_without_consuming_request(self):
        (self.root / "input.zip").write_bytes(b"not a zip")
        self.assertEqual(self.perform()[0], 1)
        self.assertEqual(self.server.calls, [])

    def test_invalid_inputs_are_rejected_without_request(self):
        for changes in ({"CREATIVEPACK_RAPIDAPI_KEY": "bad\nkey"}, {"CREATIVEPACK_BUDGET_BYTES": "1000"},
                        {"CREATIVEPACK_MODE": "fast"}, {"CREATIVEPACK_FAIL_ON_BUDGET": "yes"},
                        {"CREATIVEPACK_REPORT": "injected\noutput=x"}):
            with self.subTest(changes=list(changes)):
                self.assertEqual(self.perform(**changes)[0], 1)
                self.assertEqual(self.server.calls, [])

    def test_late_report_collision_rolls_back_only_new_archive(self):
        target = self.root / "creativepack-report.json"
        self.server.hook = lambda: target.write_bytes(b"concurrent file")
        self.assertEqual(self.perform()[0], 1)
        self.assertEqual(target.read_bytes(), b"concurrent file")
        self.assertFalse((self.root / "creativepack-optimized.zip").exists())

    def test_step_output_write_error_keeps_verified_files(self):
        self.assertEqual(self.perform(GITHUB_OUTPUT=str(self.root))[0], 1)
        self.assertTrue((self.root / "creativepack-optimized.zip").exists())
        self.assertTrue((self.root / "creativepack-report.json").exists())


if __name__ == "__main__":
    unittest.main()
