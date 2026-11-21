import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

from identity.api_checks import check_response, plan_route_cases
from identity.api_workflow import build_api_plan, plan_resources, run_api_campaign
from identity.payload_ai import synthesize_body


class ApiChecksTests(unittest.TestCase):
    def test_post_and_delete_are_tested_and_cleaned_without_full_crud(self):
        records = {}

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                identifier = str(len(records) + 1)
                records[identifier] = data
                body = json.dumps({"id": identifier, **data}).encode()
                self.send_response(201)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_DELETE(self):
                identifier = self.path.rsplit("/", 1)[-1]
                existed = records.pop(identifier, None) is not None
                self.send_response(204 if existed else 404)
                self.end_headers()

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            schema = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
            contracts = [{"method": "POST", "path": "/notes", "body_schema": schema},
                         {"method": "DELETE", "path": "/notes/{id}", "body_schema": None}]
            flows, skipped, standalone = plan_resources(contracts)
            self.assertEqual(len(flows), 1, skipped)
            self.assertIsNone(flows[0].update)
            plan = build_api_plan(contracts, flows, standalone, [], skipped)
            self.assertEqual(plan["planned"], 2)
            self.assertEqual(plan["blocked"], 0)
            create_test = next(route["tests"][0] for route in plan["routes"] if route["method"] == "POST")
            self.assertEqual(create_test["cleanup"], {"method": "DELETE", "path": "/notes/{id}"})
            self.assertIn("identitypreview", create_test["body_example"]["name"])
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "results.jsonl"
                result = asyncio.run(run_api_campaign(2, f"http://127.0.0.1:{server.server_port}", {},
                                                      2, flows, standalone, output, max_concurrency=1))
                events = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(result["cleanup_ok"], 2)
            self.assertEqual(result["cleanup_failed"], 0)
            self.assertFalse(records)
            self.assertEqual({item["method"] for item in events}, {"POST", "DELETE"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_local_payload_planner_uses_field_meanings_and_constraints(self):
        schema = {"type": "object", "required": ["descricao", "email", "quantidade"],
                  "properties": {"descricao": {"type": "string", "minLength": 8},
                                 "email": {"type": "string", "format": "email"},
                                 "quantidade": {"type": "integer", "minimum": 3},
                                 "ativo": {"type": "boolean"}}}
        body = synthesize_body(schema, "agente123")
        self.assertIn("agente123", body["descricao"])
        self.assertEqual(body["email"], "identity+agente123@example.com")
        self.assertGreaterEqual(body["quantidade"], 3)
        self.assertNotIn("ativo", body)
        descriptions = {"type": "object", "required": ["descricao"],
                        "properties": {"descricao": {"type": "string"}}}
        routes = [{"method": method, "path": path,
                   "body_schema": descriptions if method in {"POST", "PUT"} else None}
                  for method, path in (("POST", "/notas"), ("PUT", "/notas/{id}"),
                                       ("GET", "/notas"), ("DELETE", "/notas/{id}"))]
        flows, skipped, _ = plan_resources(routes)
        self.assertEqual(len(flows), 1, skipped)

    def test_explicit_write_requires_disposable_environment(self):
        route = [{"method": "POST", "path": "/jobs"}]
        with self.assertRaisesRegex(ValueError, "isolated"):
            plan_route_cases(route, {"POST /jobs": [{"json": {"name": "test"}}]})

    def test_crud_detects_success_status_without_update_or_deletion(self):
        record = {}
        created = []

        class Handler(BaseHTTPRequestHandler):
            def reply(self, status, value):
                payload = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                created.append(True)
                record.update(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.reply(201, {"id": "1", **record})

            def do_PUT(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self.reply(200, {})

            def do_GET(self):
                self.reply(200, [record] if self.path == "/items" else record)

            def do_DELETE(self):
                self.reply(204, {})

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            schema = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
            contracts = [{"method": method, "path": path,
                          "body_schema": schema if method in {"POST", "PUT"} else None}
                         for method, path in (("POST", "/items"), ("PUT", "/items/{id}"),
                                              ("GET", "/items/{id}"), ("GET", "/items"),
                                              ("DELETE", "/items/{id}"))]
            flows, _, standalone = plan_resources(contracts)
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "run.jsonl"
                result = asyncio.run(run_api_campaign(3, f"http://127.0.0.1:{server.server_port}", {},
                                                      2, flows, standalone, output, max_concurrency=1))
                records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(result["failed"], 2)
            self.assertEqual(result["cleanup_failed"], 1)
            self.assertEqual(len(created), 1)
            self.assertGreaterEqual(result["skipped"], 2)
            self.assertTrue(any(item["phase"] == "invalid_update" and not item["passed"] for item in records))
            self.assertTrue(any("continua disponível" in item["error"] for item in records if item["error"]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_contract_rejects_wrong_json_even_with_http_200(self):
        route = {"expected_status": [200], "response_schema": {"type": "object", "required": ["id"],
                 "properties": {"id": {"type": "integer"}}}}
        passed, error, _ = check_response(httpx.Response(200, json={"id": "wrong"}), route, {200})
        self.assertFalse(passed)
        self.assertIn("resposta.id", error)

    def test_discovered_parameter_route_runs_explicit_case(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = json.dumps({"id": "wrong"}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            contracts = [{"method": "GET", "path": "/items/{id}", "expected_status": [200],
                          "response_schema": {"type": "object", "required": ["id"],
                                              "properties": {"id": {"type": "integer"}}}}]
            cases = plan_route_cases(contracts, {"GET /items/{id}": [{"path": "/items/1"}]})
            with tempfile.TemporaryDirectory() as folder:
                output = Path(folder) / "results.jsonl"
                result = asyncio.run(run_api_campaign(2, f"http://127.0.0.1:{server.server_port}", {},
                                                      2, [], [], output, direct_cases=cases))
                record = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(result["failed"], 2)
            self.assertEqual(record["route"], "/items/{id}")
            self.assertEqual(record["path"], "/items/1")
            self.assertIn("resposta.id", record["error"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_cli_reports_route_coverage_and_schema_failure(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                payload = b'{"id":"wrong"}'
                self.send_response(200)
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
                (project / "openapi.json").write_text(json.dumps({"paths": {"/items/{id}": {"get": {
                    "responses": {"200": {"content": {"application/json": {"schema": {
                        "type": "object", "required": ["id"], "properties": {"id": {"type": "integer"}}
                    }}}}}
                }}}}), encoding="utf-8")
                identity = project / "Identity"
                config = identity / "config"
                config.mkdir(parents=True)
                for name in ("api.tes", "2.ag", "1.rod"):
                    (config / name).touch()
                (identity / "identity.json").write_text(json.dumps({
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                    "api_cases": {"GET /items/{id}": [{"path": "/items/1"}]},
                }), encoding="utf-8")
                scan = subprocess.run([sys.executable, "-m", "identity", "scam"], cwd=identity,
                                      capture_output=True, text=True, check=False, timeout=15)
                self.assertEqual(scan.returncode, 0, scan.stderr)
                plan = json.loads((identity / "projects" / project.name / "testes-api.json").read_text(encoding="utf-8"))
                self.assertEqual(plan["planned"], 1)
                self.assertEqual(plan["routes"][0]["status"], "planejada")
                run = subprocess.run([sys.executable, "-m", "identity", project.name], cwd=identity,
                                     capture_output=True, text=True, check=False, timeout=15)
                self.assertEqual(run.returncode, 1, run.stderr + run.stdout)
                run_dir = next((identity / "projects" / project.name / "runs").iterdir())
                report = (run_dir / "relatorio-api.md").read_text(encoding="utf-8")
                self.assertIn("Rotas executadas: 1/1", report)
                self.assertIn("resposta.id deveria ser inteiro", report)
                summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
                self.assertTrue(summary["failed"])
                self.assertEqual(summary["result"]["tested_routes"], 1)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
