import asyncio
import json
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from identity.api_checks import check_response, plan_route_cases
from identity.api_contract import api_runtime, load_api_contract
from identity.api_discovery import discover_contracts
from identity.api_workflow import _cleanup_path, run_api_campaign
from identity.payload_ai import synthesize_body


class ApiContractTests(unittest.TestCase):
    def test_managed_server_starts_and_stops(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            contract = {"start_command": [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
                        "health_path": "/", "ready_timeout_seconds": 10}
            with api_runtime(project, contract, f"http://127.0.0.1:{port}", project / "run") as runtime:
                self.assertFalse(runtime.cleanup_errors)
            self.assertTrue((project / "run" / "servidor.log").is_file())

    def test_cleanup_uses_nested_id_and_rejects_unsafe_values(self):
        response = httpx.Response(201, json={"data": {"id": "abc-123"}})
        self.assertEqual(_cleanup_path("/items/{response.data.id}", response), "/items/abc-123")
        unsafe = httpx.Response(201, json={"data": {"id": "../other"}})
        with self.assertRaisesRegex(ValueError, "identificador seguro"):
            _cleanup_path("/items/{response.data.id}", unsafe)

    def test_custom_openapi_resolves_alternative_request_bodies(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            schema = project / "schema" / "routes.json"
            schema.parent.mkdir()
            schema.write_text(json.dumps({
                "openapi": "3.1.0",
                "paths": {"/items": {"post": {"requestBody": {"content": {
                    "application/json": {"schema": {"oneOf": [
                        {"$ref": "#/components/schemas/Item"}, {"type": "null"}]}}}}}}},
                "components": {"schemas": {"Item": {"type": "object", "required": ["name"],
                    "properties": {"name": {"type": "string"}}}}},
            }), encoding="utf-8")
            route = discover_contracts(project, schema)[0]
            body = synthesize_body(route["body_schema"], "identitycase")
            self.assertIn("identitycase", body["name"])

    def test_upload_form_and_schema_constraints(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = self.rfile.read(int(self.headers["Content-Length"]))
                valid = (b"poem.txt" in data and b"verso de teste" in data
                         and b"categoria" in data and b"poema" in data)
                payload = json.dumps({"name": "poema" if valid else ""}).encode()
                self.send_response(201 if valid else 400)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                project = Path(folder)
                (project / "poem.txt").write_text("verso de teste", encoding="utf-8")
                contracts = [{"method": "POST", "path": "/upload"}]
                cases = plan_route_cases(contracts, {"POST /upload": [{
                    "path": "/upload", "isolated": True, "form": {"categoria": "poema"},
                    "files": {"arquivo": {"path": "poem.txt", "content_type": "text/plain"}},
                    "expected_status": [201], "expected_json": {"name": "poema"}
                }]})
                result = asyncio.run(run_api_campaign(
                    1, f"http://127.0.0.1:{server.server_port}", {}, 2,
                    [], [], project / "resultados.jsonl", direct_cases=cases, fixture_root=project))
                self.assertEqual(result["passed"], 1, result)
                response = httpx.Response(200, json={"name": "x"})
                passed, error, _ = check_response(
                    response, {"response_schema": {"type": "object", "properties": {
                        "name": {"type": "string", "minLength": 3}}}}, {200})
                self.assertFalse(passed)
                self.assertIn("minLength", error)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_teardown_failure_is_reported_after_runtime(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                project = Path(folder)
                contract = {
                    "health_path": "/",
                    "teardown_command": [sys.executable, "-c", "import sys; sys.exit(2)"],
                }
                with api_runtime(project, contract, f"http://127.0.0.1:{server.server_port}",
                                 project / "run") as runtime:
                    pass
                self.assertEqual(len(runtime.cleanup_errors), 1)
                self.assertIn("teardown_command", runtime.cleanup_errors[0])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_uncovered_route_fails_the_campaign_with_a_reason(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                project = Path(folder)
                (project / "app.py").write_text(
                    'from fastapi import FastAPI\napp = FastAPI()\n'
                    '@app.get("/health")\ndef health(): pass\n'
                    '@app.get("/items/{id}")\ndef item(id: int): pass\n',
                    encoding="utf-8")
                identity = project / "Identity"
                settings = identity / "config"
                settings.mkdir(parents=True)
                for name in ("api.tes", "1.ag", "1.rod"):
                    (settings / name).touch()
                (identity / "identity.json").write_text(
                    json.dumps({"base_url": f"http://127.0.0.1:{server.server_port}"}),
                    encoding="utf-8")
                scan = subprocess.run([sys.executable, "-m", "identity", "scam"], cwd=identity,
                                      capture_output=True, text=True, check=False, timeout=20)
                self.assertEqual(scan.returncode, 0, scan.stderr + scan.stdout)
                run = subprocess.run([sys.executable, "-m", "identity", project.name], cwd=identity,
                                     capture_output=True, text=True, check=False, timeout=20)
                self.assertEqual(run.returncode, 1, run.stderr + run.stdout)
                run_dir = next((identity / "projects" / project.name / "runs").iterdir())
                summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["result"]["tested_routes"], 1)
                self.assertIn("GET /items/{id}",
                              [item["route"] for item in summary["result"]["untested_routes"]])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_project_manifest_scans_and_tests_authenticated_routes(self):
        resources = {}

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status, value, *, version=False):
                payload = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                if version:
                    self.send_header("X-Api-Version", "1")
                self.end_headers()
                self.wfile.write(payload)

            def authorized(self):
                return self.headers.get("Authorization") == "Bearer identity-test"

            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path == "/health":
                    self.reply(200, {"ok": True})
                elif not self.authorized():
                    self.reply(401, {})
                elif parsed.path == "/search":
                    term = parse_qs(parsed.query).get("term", [""])[0]
                    self.reply(200 if term else 400, {"term": term}, version=True)
                elif parsed.path.startswith("/items/"):
                    item = resources.get(parsed.path.rsplit("/", 1)[-1])
                    self.reply(200 if item else 404, item or {})
                else:
                    self.reply(404, {})

            def do_POST(self):
                if not self.authorized():
                    self.reply(401, {})
                    return
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                resources["1"] = {"id": "1", **body}
                self.reply(201, resources["1"])

            def do_DELETE(self):
                identifier = self.path.rsplit("/", 1)[-1]
                item = resources.pop(identifier, None)
                self.reply(204 if item else 404, {})

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                project = Path(folder)
                schema = project / "schema" / "service.json"
                schema.parent.mkdir()
                schema.write_text(json.dumps({"openapi": "3.0.0", "paths": {
                    "/search": {"get": {}}, "/items": {"post": {}},
                    "/items/{id}": {"get": {}, "delete": {}}
                }}), encoding="utf-8")
                (project / "auth.py").write_text(
                    'import json\nprint(json.dumps({"headers":{"Authorization":"Bearer identity-test"}}))\n',
                    encoding="utf-8")
                (project / "setup.py").write_text(
                    "from pathlib import Path\nPath('prepared').write_text('yes')\n", encoding="utf-8")
                (project / "teardown.py").write_text(
                    "from pathlib import Path\nPath('prepared').unlink()\nPath('cleaned').write_text('yes')\n",
                    encoding="utf-8")
                manifest = {
                    "version": 1, "base_url": f"http://127.0.0.1:{server.server_port}",
                    "openapi_path": "schema/service.json", "health_path": "/health",
                    "setup_command": [sys.executable, "setup.py"],
                    "teardown_command": [sys.executable, "teardown.py"],
                    "auth_command": [sys.executable, "auth.py"],
                    "api_cases": {
                        "GET /search": [{"path": "/search", "query": {"term": "poem"},
                                         "expected_json": {"term": "poem"},
                                         "expected_headers": {"X-Api-Version": "1"},
                                         "expected_text_contains": "poem"}],
                        "POST /items": [{"path": "/items", "json": {"name": "identity-test"},
                                         "isolated": True, "expected_status": [201],
                                         "cleanup": {"path": "/items/{response.id}",
                                                     "verify_path": "/items/{response.id}"}}],
                    },
                }
                (project / "identity-api.json").write_text(json.dumps(manifest), encoding="utf-8")
                self.assertEqual(load_api_contract(project)["openapi_path"], "schema/service.json")
                identity = project / "Identity"
                settings = identity / "config"
                settings.mkdir(parents=True)
                for name in ("api.tes", "1.ag", "1.rod"):
                    (settings / name).touch()
                (identity / "identity.json").write_text(
                    '{"base_url":"http://127.0.0.1:1"}', encoding="utf-8")
                scan = subprocess.run([sys.executable, "-m", "identity", "scam"], cwd=identity,
                                      capture_output=True, text=True, check=False, timeout=20)
                self.assertEqual(scan.returncode, 0, scan.stderr + scan.stdout)
                run = subprocess.run([sys.executable, "-m", "identity", project.name], cwd=identity,
                                     capture_output=True, text=True, check=False, timeout=30)
                self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
                run_dir = next((identity / "projects" / project.name / "runs").iterdir())
                summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["result"]["tested_routes"], 4, summary)
                self.assertEqual(summary["result"]["cleanup_ok"], 1)
                self.assertEqual(summary["result"]["cleanup_failed"], 0)
                self.assertFalse(resources)
                self.assertFalse((project / "prepared").exists())
                self.assertTrue((project / "cleaned").exists())
                records = (run_dir / "resultados.jsonl").read_text(encoding="utf-8")
                self.assertNotIn("Bearer identity-test", records)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
