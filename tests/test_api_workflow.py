import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from identity.agent_mode import generate_cases, write_training_examples
from identity.api_discovery import discover_contracts
from identity.api_workflow import plan_resources, run_api_campaign
from identity.scanner import discover_agent


class ApiWorkflowTests(unittest.TestCase):
    def test_nested_resource_is_deleted_before_its_parent(self):
        resources = {"parents": {}, "children": {}}
        deleted = []

        class Handler(BaseHTTPRequestHandler):
            def _reply(self, status, value):
                payload = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/parents":
                    resources["parents"]["1"] = {"id": "1", **body}
                    self._reply(201, resources["parents"]["1"])
                elif self.path == "/parents/1/children":
                    resources["children"]["2"] = {"id": "2", **body}
                    self._reply(201, resources["children"]["2"])
                else:
                    self._reply(404, {})

            def do_PUT(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self._reply(200, {})

            def do_GET(self):
                self._reply(200, [])

            def do_DELETE(self):
                if self.path == "/parents/1/children/2":
                    resources["children"].pop("2")
                    deleted.append("child")
                elif self.path == "/parents/1":
                    resources["parents"].pop("1")
                    deleted.append("parent")
                else:
                    self._reply(404, {})
                    return
                self._reply(204, {})

            def log_message(self, *_):
                return

        schema = {"type": "object", "required": ["name"], "properties": {"name": {"type": "string"}}}
        contracts = [{"method": method, "path": path, "body_schema": schema if method in {"POST", "PUT"} else None}
                     for method, path in (("POST", "/parents"), ("PUT", "/parents/{parent_id}"),
                                          ("GET", "/parents"), ("DELETE", "/parents/{parent_id}"),
                                          ("POST", "/parents/{parent_id}/children"),
                                          ("PUT", "/parents/{parent_id}/children/{child_id}"),
                                          ("GET", "/parents/{parent_id}/children"),
                                          ("DELETE", "/parents/{parent_id}/children/{child_id}"))]
        flows, skipped, _ = plan_resources(contracts)
        self.assertEqual(len(flows), 2, skipped)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                nested = next(flow for flow in flows if flow.parent)
                result = asyncio.run(run_api_campaign(1, f"http://127.0.0.1:{server.server_port}", {},
                                                      2, [nested], [], Path(folder) / "run.jsonl"))
            self.assertEqual(result["cleanup_ok"], 2)
            self.assertEqual(deleted, ["child", "parent"])
            self.assertFalse(resources["parents"])
            self.assertFalse(resources["children"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_each_agent_deletes_its_own_resource_after_update_failure(self):
        records = {}
        deleted = []
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def _json(self, status, value):
                payload = json.dumps(value).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with lock:
                    identifier = str(len(records) + 1)
                    records[identifier] = {"id": identifier, **body}
                self._json(201, records[identifier])

            def do_PUT(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                self._json(500, {"error": "falha simulada"})

            def do_GET(self):
                if self.path == "/users":
                    self._json(200, list(records.values()))
                else:
                    identifier = self.path.rsplit("/", 1)[-1]
                    self._json(200 if identifier in records else 404, records.get(identifier, {}))

            def do_DELETE(self):
                identifier = self.path.rsplit("/", 1)[-1]
                with lock:
                    item = records.pop(identifier, None)
                    if item is not None:
                        deleted.append(identifier)
                self._json(200 if item is not None else 404, {})

            def log_message(self, *_):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                (root / "app.py").write_text(
                    "from fastapi import FastAPI\nfrom pydantic import BaseModel\n"
                    "app = FastAPI()\n"
                    "class UserCreate(BaseModel):\n    name: str\n    email: str\n"
                    "class UserUpdate(BaseModel):\n    name: str\n"
                    "@app.post('/users')\ndef create(body: UserCreate): pass\n"
                    "@app.put('/users/{user_id}')\ndef update(user_id: int, body: UserUpdate): pass\n"
                    "@app.get('/users/{user_id}')\ndef read(user_id: int): pass\n"
                    "@app.get('/users')\ndef list_users(): pass\n"
                    "@app.delete('/users/{user_id}')\ndef delete(user_id: int): pass\n",
                    encoding="utf-8",
                )
                contracts = discover_contracts(root)
                flows, skipped, standalone = plan_resources(contracts)
                self.assertEqual(len(flows), 1, skipped)
                self.assertFalse(standalone)
                output = root / "run.jsonl"
                result = asyncio.run(run_api_campaign(3, f"http://127.0.0.1:{server.server_port}", {},
                                                      2, flows, standalone, output))
                events = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
                identity = root / "Identity"
                identity.mkdir()
                config = identity / "config"
                config.mkdir()
                (config / "2.ag").touch()
                (config / "api.tes").touch()
                (config / "10.rod").touch()
                (identity / "identity.json").write_text(json.dumps({"base_url": f"http://127.0.0.1:{server.server_port}"}),
                                                        encoding="utf-8")
                scan = subprocess.run([sys.executable, "-m", "identity", "scam"], cwd=identity,
                                      text=True, capture_output=True, check=False, timeout=15)
                self.assertEqual(scan.returncode, 0, scan.stderr + scan.stdout)
                completed = subprocess.run([sys.executable, "-m", "identity", root.name], cwd=identity,
                                           text=True, capture_output=True, check=False, timeout=15)
                self.assertEqual(completed.returncode, 1, completed.stderr + completed.stdout)
                report = (identity / root.name / "relatorio-api.md").read_text(encoding="utf-8")
                self.assertIn("Recursos excluídos ao final: 2", report)
            self.assertEqual(result["cleanup_ok"], 3)
            self.assertEqual(result["cleanup_failed"], 0)
            self.assertEqual(len(deleted), 5)
            self.assertFalse(records)
            self.assertEqual(sum(event["phase"] == "update" and event["status"] == 500 for event in events), 3)
            self.assertEqual(sum(event["phase"] == "cleanup" for event in events), 3)
            self.assertTrue(all(event["path"].startswith("/users/") for event in events if event["phase"] == "cleanup"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_openapi_payload_and_unmatched_write_are_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "openapi.json").write_text(json.dumps({
                "openapi": "3.1.0", "paths": {
                    "/items": {"post": {"requestBody": {"content": {"application/json": {"schema": {
                        "$ref": "#/components/schemas/Item"}}}}}, "get": {}},
                    "/items/{id}": {"put": {"requestBody": {"content": {"application/json": {"schema": {
                        "$ref": "#/components/schemas/Item"}}}}}, "delete": {}},
                    "/danger": {"delete": {}}},
                "components": {"schemas": {"Item": {"type": "object", "required": ["name"],
                                                      "properties": {"name": {"type": "string"}}}}},
            }), encoding="utf-8")
            flows, skipped, _ = plan_resources(discover_contracts(root))
            self.assertEqual(len(flows), 1)
            self.assertIn("DELETE /danger", [item["route"] for item in skipped])

    def test_router_mount_prefix_and_flask_methods_are_discovered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "users.py").write_text("from fastapi import APIRouter\nrouter = APIRouter(prefix='/users')\n"
                                           "@router.get('/')\ndef listing(): pass\n", encoding="utf-8")
            (root / "main.py").write_text("from fastapi import FastAPI\nfrom users import router as users_router\n"
                                          "app = FastAPI()\napp.include_router(users_router, prefix='/api/v1')\n",
                                          encoding="utf-8")
            (root / "flask_api.py").write_text("from flask import Flask\napp = Flask(__name__)\n"
                                               "@app.route('/posts', methods=['GET', 'POST'])\n"
                                               "def posts(): pass\n", encoding="utf-8")
            routes = {(route["method"], route["path"]) for route in discover_contracts(root)}
            self.assertIn(("GET", "/api/v1/users/"), routes)
            self.assertIn(("GET", "/posts"), routes)
            self.assertIn(("POST", "/posts"), routes)

    def test_agent_inventory_includes_registered_tools_and_held_out_questions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            corpus = root / "Agent" / "agente" / "treino_capacidades.py"
            corpus.parent.mkdir(parents=True)
            corpus.write_text("PEDIDOS: dict[str, tuple[str, ...]] = {'conversa': ('oi',), 'abrir_app': ('abra app',)}\n"
                              "AVALIACAO = (('vamos conversar', 'conversa'),)\n", encoding="utf-8")
            (corpus.parent / "tools.py").write_text('registro.registrar(Ferramenta("abrir_app", "Abre aplicativo"))\n'
                                                     'registro.registrar(Ferramenta("extra", "Outro recurso"))\n', encoding="utf-8")
            bridge = root / "Agent" / "servicos" / "comandos_workspace.py"
            bridge.parent.mkdir(parents=True)
            bridge.write_text('PORTA = 1234\nself.path != "/command"\n', encoding="utf-8")
            discovery = discover_agent(root)
            self.assertEqual(discovery["uncovered_tools"], ["extra"])
            cases = generate_cases(discovery["examples"], discovery["evaluation"])
            self.assertGreater(len(cases), 2)
            self.assertIn("vamos conversar", [case["texto"] for case in cases])
            output = root / "train.jsonl"
            write_training_examples(cases, output)
            self.assertNotIn("vamos conversar", output.read_text(encoding="utf-8"))
