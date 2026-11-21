import json
import tempfile
import unittest
from pathlib import Path

from identity.report_view import debug_summary


class ReportViewTests(unittest.TestCase):
    def test_llm_block_is_distinct_from_intent_classifier(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            report = run / "relatorio-agente.md"
            report.write_text("# Relatório\n", encoding="utf-8")
            output = run / "resultados.jsonl"
            output.write_text('', encoding="utf-8")
            (run / "summary.json").write_text(json.dumps({
                "mode": "agente", "failed": True,
                "result": {"output": str(output), "requests": 2, "failed": 0, "rounds": [{}]},
                "training": {"training_exit_code": 0, "training_log": "classificador OK"},
                "llm_training": [{"round": 1, "status": "bloqueado", "optimization_steps": 0,
                                  "reason": "Espaço insuficiente"}],
            }), encoding="utf-8")
            rows, problems, _ = debug_summary(report)
            self.assertIn(("Classificador", "OK"), rows)
            self.assertIn(("LLM de conversa", "bloqueado"), rows)
            self.assertIn(("Passos LoRA", "0"), rows)
            self.assertTrue(any("Espaço insuficiente" in problem for problem in problems))

    def test_published_llm_keeps_rejected_candidates_as_information(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            report = run / "relatorio-agente.md"
            report.write_text("# Relatório\n", encoding="utf-8")
            output = run / "resultados.jsonl"
            output.write_text("", encoding="utf-8")
            (run / "summary.json").write_text(json.dumps({
                "mode": "agente", "failed": False,
                "result": {"output": str(output), "requests": 2, "failed": 0, "rounds": [{}, {}]},
                "training": {"training_exit_code": 0},
                "llm_training": [
                    {"round": 1, "status": "rejeitado", "optimization_steps": 16,
                     "reason": "Perda não melhorou"},
                    {"round": 2, "status": "publicado", "optimization_steps": 20},
                ],
            }), encoding="utf-8")
            rows, problems, _ = debug_summary(report)
            self.assertIn(("Estado", "Sem falhas detectadas"), rows)
            self.assertIn(("LLM de conversa", "publicado"), rows)
            self.assertIn(("Rejeitados", "1"), rows)
            self.assertEqual(problems, [])

    def test_incomplete_agent_campaign_is_explicit(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            report = run / "relatorio-agente.md"
            report.write_text("# Relatório\n", encoding="utf-8")
            output = run / "resultados.jsonl"
            output.write_text("", encoding="utf-8")
            (run / "summary.json").write_text(json.dumps({
                "mode": "agente", "failed": True,
                "result": {"output": str(output), "requests": 100, "failed": 0,
                           "rounds_configured": 10, "rounds": [{}, {}]},
                "training": {"training_exit_code": 0},
                "llm_training": [{"round": 2, "status": "publicado", "optimization_steps": 16}],
            }), encoding="utf-8")
            rows, _, _ = debug_summary(report)
            self.assertIn(("Estado", "Campanha incompleta"), rows)
            self.assertIn(("Rodadas", "2/10"), rows)

    def test_api_debug_view_groups_failures_and_keeps_full_file(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            report = run / "relatorio-api.md"
            full = "# Relatório completo\n\nMuitos detalhes de execução.\n"
            report.write_text(full, encoding="utf-8")
            output = run / "resultados.jsonl"
            records = [{"method": "GET", "path": "/users", "status": 500,
                        "passed": False, "skipped": False, "error": "HTTP 500"}] * 3
            output.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            (run / "summary.json").write_text(json.dumps({"mode": "api", "failed": True, "result": {
                "output": str(output), "requests": 3, "failed": 3, "cleanup_failed": 1,
                "tested_routes": 1, "discovered_routes": 2,
                "untested_routes": [{"route": "POST /users", "reason": "Sem payload"}],
            }}), encoding="utf-8")
            rows, problems, _ = debug_summary(report)
            self.assertIn(("Rotas", "1/2 testadas"), rows)
            self.assertIn(("Limpeza pendente", "1"), rows)
            self.assertTrue(any("GET /users" in item and "3x" in item for item in problems))
            self.assertTrue(any("POST /users" in item for item in problems))
            self.assertEqual(report.read_text(encoding="utf-8"), full)

    def test_agent_debug_view_shows_only_last_round_failures(self):
        with tempfile.TemporaryDirectory() as folder:
            run = Path(folder)
            report = run / "relatorio-agente.md"
            report.write_text("# Relatório completo\n", encoding="utf-8")
            output = run / "resultados.jsonl"
            output.write_text("\n".join(json.dumps(item) for item in [
                {"rodada": 1, "passed": False, "case": "antigo", "error": "erro antigo"},
                {"rodada": 2, "passed": False, "case": "atual", "error": "erro atual"},
            ]) + "\n", encoding="utf-8")
            (run / "summary.json").write_text(json.dumps({"mode": "agente", "failed": True,
                "result": {"output": str(output), "requests": 2, "failed": 1, "rounds": [{}, {}]},
                "training": {"status": "rejeitado", "reason": "sem melhora"}}), encoding="utf-8")
            rows, problems, _ = debug_summary(report)
            self.assertIn(("Treino", "rejeitado"), rows)
            self.assertTrue(any("erro atual" in item for item in problems))
            self.assertFalse(any("erro antigo" in item for item in problems))
            self.assertTrue(any("sem melhora" in item for item in problems))


if __name__ == "__main__":
    unittest.main()
