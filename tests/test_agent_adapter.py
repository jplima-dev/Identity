import asyncio
import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from identity.agent_adapter import (
    load_adapter,
    run_adapter_cases,
    train_adapter,
    write_adapter_report,
)


class AgentAdapterTests(unittest.TestCase):
    def test_training_and_evaluation_cannot_share_the_same_conversation(self):
        case = {"messages": [{"role": "user", "content": "mesma pergunta"}],
                "expected": {"equals": "resposta"}}
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "identity-agent.json").write_text(json.dumps({
                "version": 1, "safe_mode": True, "endpoint": "http://127.0.0.1:8000/test",
                "cases": [{**case, "split": "train"}, {**case, "split": "eval"}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "train e eval"):
                load_adapter(project)

    def test_cli_uses_generic_adapter_for_saved_project_and_rounds(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                payload = '{"response":"olá"}'.encode()
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
                (project / "identity-agent.json").write_text(json.dumps({
                    "version": 1, "safe_mode": True,
                    "endpoint": f"http://127.0.0.1:{server.server_port}/identity-test",
                    "cases": [{"split": "eval", "name": "saudação",
                               "messages": [{"role": "user", "content": "oi"}],
                               "expected": {"equals": "olá"}}],
                }), encoding="utf-8")
                identity = project / "Identity"
                config = identity / "config"
                config.mkdir(parents=True)
                for name in ("agente.tes", "2.ag", "2.rod"):
                    (config / name).touch()
                (identity / "identity.json").write_text('{"base_url":"http://127.0.0.1"}', encoding="utf-8")
                scan = subprocess.run([sys.executable, "-m", "identity", "scam"], cwd=identity,
                                      capture_output=True, text=True, check=False, timeout=15)
                self.assertEqual(scan.returncode, 0, scan.stderr)
                run = subprocess.run([sys.executable, "-m", "identity", project.name], cwd=identity,
                                     capture_output=True, text=True, check=False, timeout=15)
                self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
                run_dir = next((identity / "projects" / project.name / "runs").iterdir())
                report = (run_dir / "relatorio-agente.md").read_text(encoding="utf-8")
                self.assertIn("Rodadas: 2", report)
                self.assertIn("Execuções: 4", report)
                self.assertIn("Estado: não configurado", report)
                summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
                self.assertFalse(summary["failed"])
                self.assertEqual(summary["result"]["requests"], 4)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_conversation_and_candidate_training_are_project_configured(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                response = json.dumps({"response": "Olá, " + data["messages"][-1]["content"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)

            def log_message(self, *_):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as folder:
                project = Path(folder)
                model = project / "model.txt"
                model.write_text("old", encoding="utf-8")
                (project / "trainer.py").write_text(
                    "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('new')\n", encoding="utf-8")
                (project / "evaluate.py").write_text(
                    "import json, pathlib, sys\n"
                    "print(json.dumps({'passed': 2 if pathlib.Path(sys.argv[1]).read_text() == 'new' else 1, 'total': 2}))\n",
                    encoding="utf-8",
                )
                data = {"version": 1, "safe_mode": True,
                        "endpoint": f"http://127.0.0.1:{server.server_port}/identity-test",
                        "cases": [
                            {"split": "train", "messages": [{"role": "user", "content": "Oi"}],
                             "expected": {"contains": "Olá"}},
                            {"split": "eval", "name": "saudação",
                             "messages": [{"role": "user", "content": "mundo"}],
                             "expected": {"equals": "Olá, mundo"}},
                        ],
                        "training": {"model_path": "model.txt", "use_generated": True,
                                     "command": [sys.executable, "trainer.py", "{candidate_model}", "{train_data}"],
                                     "evaluate_command": [sys.executable, "evaluate.py", "{model}", "{eval_data}"]}}
                (project / "identity-agent.json").write_text(json.dumps(data), encoding="utf-8")
                adapter = load_adapter(project)
                output_dir = project / "results"
                result = asyncio.run(run_adapter_cases(2, adapter, output_dir / "run.jsonl"))
                training = train_adapter(project, adapter, output_dir, generated=[
                    {"capability": "saudação", "messages": [{"role": "user", "content": "mundo"}]},
                    {"capability": "saudação", "messages": [{"role": "user", "content": "Boa tarde"}]},
                ])
                report = write_adapter_report(output_dir, result, training)
                self.assertEqual(result["passed"], 2)
                self.assertEqual(training["status"], "publicado")
                self.assertEqual(model.read_text(encoding="utf-8"), "new")
                self.assertEqual((output_dir / "modelo_anterior.txt").read_text(encoding="utf-8"), "old")
                train_lines = [json.loads(line) for line in (output_dir / "conversas_treino.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertEqual(len(train_lines), 2)
                self.assertEqual(train_lines[1]["messages"][0]["content"], "Boa tarde")
                self.assertEqual(len((output_dir / "conversas_avaliacao.jsonl").read_text(encoding="utf-8").splitlines()), 1)
                self.assertIn("Avaliação candidata: 2/2", report.read_text(encoding="utf-8"))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
