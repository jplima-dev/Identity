import asyncio
import gzip
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from identity.agent_mode import generate_cases, run_agent_cases
from identity.campaign import next_cases
from identity.cli import agent_count, round_count, run_mode
from identity.offline_model import OfflineCapabilityModel
from identity.scanner import discover_api, discover_base_url


class CampaignTests(unittest.TestCase):
    def test_failed_case_stays_and_success_uses_new_case(self):
        pool = ["a", "b", "c", "d"]
        updated, cursor, changed = next_cases(pool, ["a", "b"], [False, True], 2)
        self.assertEqual(updated, ["a", "c"])
        self.assertEqual(cursor, 3)
        self.assertTrue(changed)

    def test_complex_questions_enter_first_round(self):
        cases = generate_cases({"HORA": ["que horas são"], "descrever_projeto": ["descreva o projeto"]})
        self.assertEqual([case["complexidade"] for case in cases[:2]], ["basica", "complexa"])

    def test_open_application_question_is_only_a_local_classification_case(self):
        cases = generate_cases({"abrir_aplicacao_ink": ["abra o Workspace"]})
        self.assertEqual(cases[0]["rotulo"], "abrir_aplicacao_ink")

    def test_mode_agent_count_and_rounds_use_extensions(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "100.ag").touch()
            (root / "agente.tes").touch()
            (root / "10.rod").touch()
            self.assertEqual(agent_count(root), 100)
            self.assertEqual(run_mode(root), "agente")
            self.assertEqual(round_count(root), 10)

    def test_nested_router_and_local_url_are_discovered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "README.md").write_text("http://127.0.0.1:8000/", encoding="utf-8")
            (root / "routes.py").write_text(
                'from fastapi import APIRouter\nrouter = APIRouter(prefix="/torneios/{id}/times")\n'
                '@router.get("/{time_id}")\ndef buscar(): pass\n',
                encoding="utf-8",
            )
            routes = discover_api(root)
            self.assertIn({"method": "GET", "path": "/torneios/{id}/times/{time_id}", "source": "routes.py"}, routes)
            self.assertEqual(discover_base_url(root), "http://127.0.0.1:8000")

    def test_failed_question_is_retried_and_success_is_replaced(self):
        class Model:
            def predict(self, text):
                return ("erro" if text == "erro" else "conversa", 0.9)

        cases = [{"texto": text, "contem": "", "rotulo": "conversa", "complexidade": "basica"} for text in ("erro", "bom", "novo")]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "run.jsonl"
            first = asyncio.run(run_agent_cases(2, cases[:2], Model(), output))
            updated, _, _ = next_cases(cases, cases[:2], first["outcomes"], 2)
            second = asyncio.run(run_agent_cases(2, updated, Model(), output, round_number=2))
            records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
        ordered = sorted(records, key=lambda record: (record["rodada"], record["agent"]))
        self.assertEqual([record["texto"] for record in ordered], ["erro", "bom", "erro", "novo"])
        self.assertEqual([first["failed"], second["failed"]], [1, 1])
        self.assertTrue(all(record["tipo_teste"] == "classificacao_local" for record in records))

    def test_offline_model_reads_weights_without_running_project_code(self):
        data = {"versao": 1, "rotulos": ["CHAMADA_CURTA", "HORA", "abrir_aplicacao_ink"],
                "exemplos": 2, "documentos": {"HORA": 1, "abrir_aplicacao_ink": 1},
                "totais": {"HORA": 1, "abrir_aplicacao_ink": 1},
                "contagens": {"CHAMADA_CURTA": {}, "HORA": {"u:horas": 1},
                              "abrir_aplicacao_ink": {"u:abra": 1}}}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "modelo.gz"
            with gzip.open(path, "wt", encoding="utf-8") as stream:
                json.dump(data, stream)
            model = OfflineCapabilityModel(path, data["rotulos"])
            self.assertEqual(model.predict("abra")[0], "abrir_aplicacao_ink")
            self.assertEqual(model.predict("horas")[0], "HORA")

    def test_api_mode_ignores_rod_and_runs_scanned_route(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                project = Path(folder)
                (project / "service.py").write_text('app = None\n@app.get("/health")\ndef health(): pass\n', encoding="utf-8")
                identity = project / "Identity"
                identity.mkdir()
                (identity / "3.ag").touch()
                (identity / "api.tes").touch()
                (identity / "10.rod").touch()
                (identity / "identity.json").write_text(json.dumps({
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                    "scenarios": [{"path": "/health"}],
                }), encoding="utf-8")
                completed = subprocess.run([sys.executable, "-m", "identity", "scam"], cwd=identity,
                                           text=True, capture_output=True, check=False, timeout=15)
                self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
                report = (identity / project.name / "relatorio.md").read_text(encoding="utf-8")
                self.assertIn("Requisições: 3", report)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
