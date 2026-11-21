import asyncio
import gzip
import json
import os
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
from identity.saved_projects import load_project, save_project, saved_names
from identity.scanner import discover_api, discover_base_url
from identity.train_yasmin import combine_examples


class CampaignTests(unittest.TestCase):
    def test_old_project_scan_remains_available_when_new_layout_is_created(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            identity = project / "Identity"
            old = identity / project.name / "scan.json"
            old.parent.mkdir(parents=True)
            scan = {"project": str(project), "mode": "api"}
            old.write_text(json.dumps(scan), encoding="utf-8")
            self.assertIn(project.name, saved_names(identity))
            self.assertEqual(load_project(identity, project.name), scan)
            new = save_project(identity, project.name, scan)
            self.assertEqual(new, identity / "projects" / project.name / "scan.json")
            self.assertTrue(old.is_file())
            self.assertEqual(load_project(identity, project.name), scan)

    def test_conversation_examples_replace_only_remaining_base_slots(self):
        base = [("a", "nenhuma"), ("b", "nenhuma"), ("c", "preparar_programacao")]
        extras = [("a", "nenhuma"), ("d", "nenhuma")]
        combined = combine_examples(base, extras)
        self.assertEqual(len(combined), len(base))
        self.assertEqual({text for text, _ in combined}, {"a", "c", "d"})

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
            config = root / "config"
            config.mkdir()
            (config / "100.ag").touch()
            (config / "agente.tes").touch()
            (config / "10.rod").touch()
            self.assertEqual(agent_count(root), 100)
            self.assertEqual(run_mode(root), "agente")
            self.assertEqual(round_count(root), 10)

    def test_inspection_commands_do_not_create_results(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            (project / "service.py").write_text('app = None\n@app.get("/health")\ndef health(): pass\n', encoding="utf-8")
            identity = project / "Identity"
            identity.mkdir()
            config = identity / "config"
            config.mkdir()
            (config / "3.ag").touch()
            (config / "4.rod").touch()
            (config / "api.tes").touch()
            (identity / "identity.json").write_text('{"base_url":"http://127.0.0.1:9999"}', encoding="utf-8")
            for command, expected in (("tes", "api.tes"), ("rod", "4.rod"), ("ag", "3.ag"),
                                      ("plan", "LER   GET /health")):
                result = subprocess.run([sys.executable, "-m", "identity", command], cwd=identity,
                                        text=True, capture_output=True, check=False, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(expected, result.stdout)
            self.assertFalse((identity / "projects" / project.name).exists())
            selected = subprocess.run([sys.executable, "-m", "identity", "plan", "--project", str(project)],
                                      cwd=identity, text=True, capture_output=True, check=False, timeout=15)
            self.assertEqual(selected.returncode, 0, selected.stderr)
            self.assertIn("LER   GET /health", selected.stdout)
            report_dir = identity / project.name
            report_dir.mkdir()
            (report_dir / "relatorio-api.md").write_text("# Relatório\n\nTeste local.", encoding="utf-8")
            shown = subprocess.run([sys.executable, "-m", "identity", "report", project.name],
                                   cwd=identity, text=True, capture_output=True, check=False, timeout=15)
            self.assertEqual(shown.returncode, 0, shown.stderr)
            self.assertIn("Relatório antigo; diagnóstico parcial", shown.stdout)
            self.assertIn("relatorio-api.md", shown.stdout)
            self.assertNotIn("Teste local.", shown.stdout)
            agent_report = report_dir / "relatorio-agente.md"
            agent_report.write_text("# Relatorio de agente\n\nSegundo relatorio.", encoding="utf-8")
            future = (report_dir / "relatorio-api.md").stat().st_mtime + 10
            os.utime(agent_report, (future, future))
            for arguments in (("report", project.name), ("report",)):
                newest = subprocess.run([sys.executable, "-m", "identity", *arguments], cwd=identity,
                                        text=True, capture_output=True, check=False, timeout=15)
                self.assertEqual(newest.returncode, 0, newest.stderr)
                self.assertIn("relatorio-agente.md", newest.stdout)
                self.assertNotIn("Segundo relatorio.", newest.stdout)
            for kind, value, expected in (("ag", "7", "7.ag"), ("rod", "9", "9.rod"), ("tes", "agente", "agente.tes")):
                updated = subprocess.run([sys.executable, "-m", "identity", kind, "set", value], cwd=identity,
                                         text=True, capture_output=True, check=False, timeout=15)
                self.assertEqual(updated.returncode, 0, updated.stderr)
                self.assertTrue((config / expected).is_file())
            self.assertEqual(sorted(path.name for path in config.iterdir()), ["7.ag", "9.rod", "agente.tes"])

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
            with_calls = OfflineCapabilityModel(path, data["rotulos"], ["yasmin me escuta"])
            self.assertEqual(with_calls.predict("yasmin me escuta"), ("CHAMADA_CURTA", 1.0))
            self.assertEqual(with_calls.predict("Yasmin, você me escuta?"), ("CHAMADA_CURTA", 1.0))
            self.assertNotEqual(with_calls.predict("yasmin me lembre de algo")[0], "CHAMADA_CURTA")
            self.assertNotEqual(with_calls.predict("yasmin me escuta e crie um lembrete")[0], "CHAMADA_CURTA")

    def test_api_mode_ignores_rod_and_runs_scanned_route(self):
        hits = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
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
                config = identity / "config"
                config.mkdir()
                (config / "3.ag").touch()
                (config / "api.tes").touch()
                (config / "10.rod").touch()
                (identity / "identity.json").write_text(json.dumps({
                    "base_url": f"http://127.0.0.1:{server.server_port}",
                    "scenarios": [{"path": "/health"}],
                }), encoding="utf-8")
                scan = subprocess.run([sys.executable, "-m", "identity", "scam"], cwd=identity,
                                      text=True, capture_output=True, check=False, timeout=15)
                self.assertEqual(scan.returncode, 0, scan.stderr + scan.stdout)
                self.assertIn(f"identity {project.name}", scan.stdout)
                project_results = identity / "projects" / project.name
                saved_scan = project_results / "scan.json"
                saved_data = json.loads(saved_scan.read_text(encoding="utf-8"))
                saved_data["mode"] = "agente"
                saved_scan.write_text(json.dumps(saved_data), encoding="utf-8")
                self.assertFalse(list((project_results / "runs").glob("*/resultados.jsonl")))
                self.assertEqual(hits, [])
                help_result = subprocess.run([sys.executable, "-m", "identity", "--help"], cwd=identity,
                                             text=True, capture_output=True, check=False, timeout=15)
                self.assertIn(project.name, help_result.stdout)
                self.assertNotIn("{run,", help_result.stdout)
                welcome = subprocess.run([sys.executable, "-m", "identity"], cwd=identity,
                                         text=True, capture_output=True, check=False, timeout=15)
                self.assertEqual(welcome.returncode, 0, welcome.stderr)
                self.assertIn("IDENTITY  /  COMANDOS", welcome.stdout)
                self.assertIn("EXEMPLO  /  API", welcome.stdout)
                self.assertIn("identity scam --project", welcome.stdout)
                self.assertIn("identity report minha-api", welcome.stdout)
                self.assertIn("PROJETOS SALVOS", welcome.stdout)
                (config / "10.rod").unlink()
                missing_rounds = subprocess.run([sys.executable, "-m", "identity", project.name], cwd=project,
                                                text=True, capture_output=True, check=False, timeout=15)
                self.assertEqual(missing_rounds.returncode, 2)
                self.assertIn(".rod", missing_rounds.stderr)
                self.assertEqual(hits, [])
                (config / "10.rod").touch()
                completed = subprocess.run([sys.executable, "-m", "identity", project.name], cwd=project,
                                           text=True, capture_output=True, check=False, timeout=15)
                self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
                self.assertIn("SCANNER  /  API", completed.stdout)
                self.assertIn("RESULTADO  /  API", completed.stdout)
                self.assertEqual(hits, ["/health"] * 3)
                self.assertEqual(json.loads(saved_scan.read_text(encoding="utf-8"))["mode"], "api")
                report = next((project_results / "runs").glob("*/relatorio-api.md")).read_text(encoding="utf-8")
                self.assertIn("Requisições: 3", report)
                (config / "3.ag").unlink()
                (config / "2.ag").touch()
                (config / "10.rod").unlink()
                (config / "5.rod").touch()
                rerun = subprocess.run([sys.executable, "-m", "identity", project.name], cwd=project,
                                       text=True, capture_output=True, check=False, timeout=15)
                self.assertEqual(rerun.returncode, 0, rerun.stderr + rerun.stdout)
                self.assertEqual(hits, ["/health"] * 5)
                newest_report = max((project_results / "runs").glob("*/relatorio-api.md"), key=lambda path: path.stat().st_mtime)
                self.assertIn("Requisições: 2", newest_report.read_text(encoding="utf-8"))
                for arguments in (("report", project.name), ("report",)):
                    shown = subprocess.run([sys.executable, "-m", "identity", *arguments], cwd=identity,
                                           text=True, capture_output=True, check=False, timeout=15)
                    self.assertEqual(shown.returncode, 0, shown.stderr)
                    self.assertIn("Sem falhas detectadas", shown.stdout)
                    self.assertIn("Relatório completo", shown.stdout)
                    self.assertNotIn("# Relatório:", shown.stdout)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
