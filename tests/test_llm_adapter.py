import json
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from identity.agent_adapter import load_adapter
from identity.llm_adapter import train_llm_adapter_round
from identity.training_lock import training_lock


def _project(folder: str, *, candidate: str = "next", smoke_ok: bool = True) -> tuple[Path, dict]:
    project = Path(folder)
    (project / "model.txt").write_text("base", encoding="utf-8")
    (project / "dados.jsonl").write_text(
        json.dumps({"reviewed": False, "messages": [
            {"role": "user", "content": "inventado"}, {"role": "assistant", "content": "incorreto"}]}) + "\n" +
        json.dumps({"reviewed": True, "messages": [
            {"role": "user", "content": "Escreva um poema curto sobre chuva"},
            {"role": "assistant", "content": "A chuva risca o vidro em silêncio."}]}) + "\n",
        encoding="utf-8",
    )
    (project / "train.py").write_text(
        "import json, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(sys.argv[2], encoding='utf-8')\n"
        "print(json.dumps({'optimization_steps': 3}))\n", encoding="utf-8")
    (project / "eval.py").write_text(
        "import json, pathlib, sys\n"
        "value = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        "print(json.dumps({'loss': {'base': 0.8, 'next': 0.4, 'bad': 0.9}[value]}))\n",
        encoding="utf-8")
    (project / "smoke.py").write_text(
        "import json, pathlib, sys\n"
        "value = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')\n"
        f"print(json.dumps({{'ok': {smoke_ok!r} and value == 'next'}}))\n", encoding="utf-8")
    config = {
        "version": 1, "safe_mode": True, "endpoint": "http://127.0.0.1:8000/identity-test",
        "cases": [{"split": "eval",
                   "messages": [{"role": "user", "content": "Escreva um poema sobre o sol"}],
                   "expected": {"contains": "sol"}}],
        "training": {"kind": "llm", "model_path": "model.txt", "dataset_path": "dados.jsonl",
                     "metric": "loss",
                     "command": [sys.executable, "train.py", "{candidate_model}", candidate, "{train_data}"],
                     "evaluate_command": [sys.executable, "eval.py", "{model}", "{eval_data}"],
                     "smoke_command": [sys.executable, "smoke.py", "{model}"]},
    }
    (project / "identity-agent.json").write_text(json.dumps(config), encoding="utf-8")
    return project, load_adapter(project)


class GenericLlmTrainingTests(unittest.TestCase):
    def test_publishes_real_candidate_only_after_improvement_and_smoke(self):
        with tempfile.TemporaryDirectory() as folder:
            project, adapter = _project(folder)
            result = train_llm_adapter_round(project, adapter, project / "run", 1)
            self.assertEqual(result["status"], "publicado", result)
            self.assertEqual(result["optimization_steps"], 3)
            self.assertEqual(result["pairs_reviewed"], 1)
            self.assertEqual(result["metric_before"], 0.8)
            self.assertEqual(result["metric_candidate"], 0.4)
            self.assertEqual((project / "model.txt").read_text(encoding="utf-8"), "next")
            self.assertEqual(Path(result["backup"]).read_text(encoding="utf-8"), "base")
            lines = (project / "run" / "llm-rodada-1" / "conversas_treino.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertNotIn("inventado", lines[0])

    def test_rejects_worse_candidate_without_changing_active_model(self):
        with tempfile.TemporaryDirectory() as folder:
            project, adapter = _project(folder, candidate="bad")
            result = train_llm_adapter_round(project, adapter, project / "run", 1)
            self.assertEqual(result["status"], "rejeitado", result)
            self.assertEqual(result["optimization_steps"], 3)
            self.assertEqual((project / "model.txt").read_text(encoding="utf-8"), "base")

    def test_restores_active_model_when_smoke_fails(self):
        with tempfile.TemporaryDirectory() as folder:
            project, adapter = _project(folder, smoke_ok=False)
            result = train_llm_adapter_round(project, adapter, project / "run", 1)
            self.assertEqual(result["status"], "falhou", result)
            self.assertIn("Ativação revertida", result["reason"])
            self.assertEqual((project / "model.txt").read_text(encoding="utf-8"), "base")

    def test_blocks_parallel_training_of_the_same_model(self):
        with tempfile.TemporaryDirectory() as folder:
            project, adapter = _project(folder)
            with training_lock(project / ".identity-training.lock") as acquired:
                self.assertTrue(acquired)
                result = train_llm_adapter_round(project, adapter, project / "run", 1)
            self.assertEqual(result["status"], "bloqueado")
            self.assertEqual(result["optimization_steps"], 0)
            self.assertEqual((project / "model.txt").read_text(encoding="utf-8"), "base")

    def test_checks_disk_space_before_activation(self):
        with tempfile.TemporaryDirectory() as folder:
            project, adapter = _project(folder)
            with patch("identity.llm_adapter.shutil.disk_usage", return_value=SimpleNamespace(free=1)):
                result = train_llm_adapter_round(project, adapter, project / "run", 1)
            self.assertEqual(result["status"], "bloqueado", result)
            self.assertEqual(result["optimization_steps"], 3)
            self.assertEqual((project / "model.txt").read_text(encoding="utf-8"), "base")

    def test_publishes_directory_weights(self):
        with tempfile.TemporaryDirectory() as folder:
            project, _ = _project(folder)
            (project / "model.txt").unlink()
            model = project / "models" / "active"
            model.mkdir(parents=True)
            (model / "weights.txt").write_text("base", encoding="utf-8")
            (project / "train_dir.py").write_text(
                "import json, pathlib, sys\n"
                "target = pathlib.Path(sys.argv[1])\n"
                "target.mkdir(parents=True)\n"
                "(target / 'weights.txt').write_text('next')\n"
                "print(json.dumps({'optimization_steps': 2}))\n", encoding="utf-8")
            (project / "eval_dir.py").write_text(
                "import json, pathlib, sys\n"
                "value = (pathlib.Path(sys.argv[1]) / 'weights.txt').read_text()\n"
                "print(json.dumps({'loss': 0.3 if value == 'next' else 0.8}))\n",
                encoding="utf-8")
            (project / "smoke_dir.py").write_text(
                "import json, pathlib, sys\n"
                "value = (pathlib.Path(sys.argv[1]) / 'weights.txt').read_text()\n"
                "print(json.dumps({'ok': value == 'next'}))\n", encoding="utf-8")
            config = json.loads((project / "identity-agent.json").read_text(encoding="utf-8"))
            training = config["training"]
            training["model_path"] = "models/active"
            training["command"] = [sys.executable, "train_dir.py", "{candidate_model}", "{train_data}"]
            training["evaluate_command"] = [sys.executable, "eval_dir.py", "{model}", "{eval_data}"]
            training["smoke_command"] = [sys.executable, "smoke_dir.py", "{model}"]
            (project / "identity-agent.json").write_text(json.dumps(config), encoding="utf-8")
            result = train_llm_adapter_round(project, load_adapter(project), project / "run", 1)
            self.assertEqual(result["status"], "publicado", result)
            self.assertEqual((model / "weights.txt").read_text(encoding="utf-8"), "next")
            self.assertEqual((Path(result["backup"]) / "weights.txt").read_text(encoding="utf-8"), "base")

    def test_scan_and_saved_project_train_generic_llm(self):
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                payload = json.dumps({"response": "Um verso sobre o sol"}).encode()
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
            with tempfile.TemporaryDirectory() as folder:
                project, _ = _project(folder)
                config = json.loads((project / "identity-agent.json").read_text(encoding="utf-8"))
                config["endpoint"] = f"http://127.0.0.1:{server.server_port}/identity-test"
                (project / "identity-agent.json").write_text(json.dumps(config), encoding="utf-8")
                identity = project / "Identity"
                settings = identity / "config"
                settings.mkdir(parents=True)
                for name in ("agente.tes", "1.ag", "2.rod"):
                    (settings / name).touch()
                (identity / "identity.json").write_text(
                    '{"base_url":"http://127.0.0.1"}', encoding="utf-8")
                scan = subprocess.run([sys.executable, "-m", "identity", "scam"],
                                      cwd=identity, capture_output=True, text=True, timeout=20, check=False)
                self.assertEqual(scan.returncode, 0, scan.stderr + scan.stdout)
                saved = json.loads((identity / "projects" / project.name / "scan.json").read_text(encoding="utf-8"))
                self.assertEqual(saved["training_kind"], "llm")
                run = subprocess.run([sys.executable, "-m", "identity", project.name],
                                     cwd=identity, capture_output=True, text=True, timeout=30, check=False)
                self.assertEqual(run.returncode, 0, run.stderr + run.stdout)
                run_dir = next((identity / "projects" / project.name / "runs").iterdir())
                summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
                self.assertEqual(summary["training"]["status"], "publicado")
                self.assertEqual([item["status"] for item in summary["training"]["rounds"]],
                                 ["publicado", "rejeitado"])
                self.assertEqual([item["optimization_steps"] for item in summary["training"]["rounds"]], [3, 3])
                self.assertEqual((project / "model.txt").read_text(encoding="utf-8"), "next")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_requires_reviewed_ground_truth_and_separate_eval(self):
        with tempfile.TemporaryDirectory() as folder:
            project, adapter = _project(folder)
            (project / "dados.jsonl").write_text(
                json.dumps({"reviewed": True, "messages": [
                    {"role": "user", "content": "Escreva um poema sobre o sol"},
                    {"role": "assistant", "content": "O sol nasce."}]}) + "\n", encoding="utf-8")
            result = train_llm_adapter_round(project, adapter, project / "run", 1)
            self.assertEqual(result["optimization_steps"], 0)
            self.assertIn("compartilham", result["reason"])
            self.assertEqual((project / "model.txt").read_text(encoding="utf-8"), "base")


if __name__ == "__main__":
    unittest.main()
