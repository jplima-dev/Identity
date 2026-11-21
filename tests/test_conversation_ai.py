import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from identity.conversation_ai import (
    discover_features,
    generate_conversations,
    save_conversations,
)


class ConversationAITests(unittest.TestCase):
    def test_scam_can_save_generic_agent_without_adapter(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "other_agent"
            project.mkdir()
            (project / "chat.py").write_text(
                "INTENTS = {'consultar_pedidos': ['Quais são os meus pedidos?']}\n"
                "def responder(messages):\n"
                "    raise RuntimeError('Nao executar durante o scan')\n",
                encoding="utf-8",
            )
            identity = project / "Identity"
            config = identity / "config"
            config.mkdir(parents=True)
            for name in ("agente.tes", "1.ag", "1.rod"):
                (config / name).touch()
            (identity / "identity.json").write_text('{"base_url":"http://127.0.0.1"}', encoding="utf-8")
            scan = subprocess.run(
                [sys.executable, "-m", "identity", "scam"], cwd=identity,
                capture_output=True, text=True, check=False, timeout=15,
            )
            self.assertEqual(scan.returncode, 0, scan.stdout + scan.stderr)
            data = identity / "projects" / project.name / "conversas" / "dados_gerados.jsonl"
            self.assertTrue(data.is_file())
            self.assertIn("consultar_pedidos", data.read_text(encoding="utf-8"))
            run = subprocess.run(
                [sys.executable, "-m", "identity", project.name], cwd=identity,
                capture_output=True, text=True, check=False, timeout=15,
            )
            self.assertEqual(run.returncode, 2)
            self.assertIn("identity-agent.json", run.stderr)

    def test_discovers_generic_python_features_without_executing_project_code(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "agent.py").write_text(
                "PEDIDOS = {'agenda': ('Mostre meus compromissos', 'Quais eventos tenho?')}\n"
                "@tool\n"
                "def pesquisar(texto):\n"
                "    '''Pesquisa documentos locais.'''\n"
                "    raise RuntimeError('O scanner executou uma ferramenta')\n"
                "def responder(messages):\n"
                "    raise RuntimeError('O scanner executou o agente')\n",
                encoding="utf-8",
            )
            inventory = discover_features(project)
            self.assertEqual({item["name"] for item in inventory["capabilities"]}, {"agenda", "pesquisar"})
            self.assertIn("responder", {item["symbol"] for item in inventory["mechanisms"]})
            records = generate_conversations(inventory)
            self.assertTrue(any(item["variant"] == "multiturno" for item in records))
            self.assertTrue(all(item["reviewed"] is False for item in records))
            self.assertTrue(all(item["messages"][-1]["role"] == "user" for item in records))
            scan_file, data_file = save_conversations(project / "Identity" / "projects" / "example", inventory, records)
            self.assertEqual(scan_file.parent.name, "conversas")
            self.assertEqual(data_file.parent, scan_file.parent)
            self.assertEqual(len(data_file.read_text(encoding="utf-8").splitlines()), len(records))

    def test_scam_saves_generated_data_inside_saved_project_folder(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                body = b'{"response":"Consulta de agenda", "capability":"agenda"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self._scan_and_run_project(server.server_port)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def _scan_and_run_project(self, port):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "example"
            project.mkdir()
            (project / "agent.py").write_text(
                "CAPABILITIES = {'agenda': ['Mostre minha agenda']}\n"
                "def chat(messages):\n"
                "    raise RuntimeError('Nao execute o chat durante o scan')\n",
                encoding="utf-8",
            )
            (project / "identity-agent.json").write_text(json.dumps({
                "version": 1, "safe_mode": True,
                "endpoint": f"http://127.0.0.1:{port}/identity-test",
                "capability_field": "capability",
            }), encoding="utf-8")
            identity = project / "Identity"
            config = identity / "config"
            config.mkdir(parents=True)
            for name in ("agente.tes", "2.ag", "1.rod"):
                (config / name).touch()
            (identity / "identity.json").write_text('{"base_url":"http://127.0.0.1"}', encoding="utf-8")
            scan = subprocess.run(
                [sys.executable, "-m", "identity", "scam"], cwd=identity,
                capture_output=True, text=True, check=False, timeout=15,
            )
            self.assertEqual(scan.returncode, 0, scan.stdout + scan.stderr)
            saved = identity / "projects" / project.name / "conversas"
            self.assertTrue((saved / "funcionalidades.json").is_file())
            self.assertTrue((saved / "dados_gerados.jsonl").is_file())
            inventory = json.loads((saved / "funcionalidades.json").read_text(encoding="utf-8"))
            self.assertEqual(inventory["capabilities"][0]["name"], "agenda")
            records = [json.loads(line) for line in (saved / "dados_gerados.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertGreaterEqual(len(records), 3)
            self.assertFalse((identity / "projects" / project.name / "runs").exists())
            run = subprocess.run(
                [sys.executable, "-m", "identity", project.name], cwd=identity,
                capture_output=True, text=True, check=False, timeout=15,
            )
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
            run_dir = next((identity / "projects" / project.name / "runs").iterdir())
            results = [json.loads(line) for line in (run_dir / "resultados.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(results), 2)
            self.assertTrue(all(item["passed"] and item["origin"] == "generated" for item in results))


if __name__ == "__main__":
    unittest.main()
