import asyncio
import io
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from identity.cli import RunConfig, Scenario, run_agents
from identity.progress import ProgressDisplay
from identity.terminal_ui import panel


class Terminal(io.StringIO):
    def isatty(self):
        return True


class LegacyConsole(io.StringIO):
    @property
    def encoding(self):
        return "cp1252"

    def write(self, value):
        value.encode(self.encoding)
        return super().write(value)


class ProgressTests(unittest.TestCase):
    def test_panel_does_not_crash_on_legacy_windows_encoding(self):
        stream = LegacyConsole()
        ProgressDisplay(stream).show_panel("DEBUG", [("Perda", "2 → 1")])
        self.assertIn("2 ? 1", stream.getvalue())

    def test_intro_uses_one_green_line_and_skips_captured_output(self):
        stream = Terminal()
        progress = ProgressDisplay(stream)
        progress.vt = True
        progress.show_intro(delay=0, show_tagline=True)
        rendered = stream.getvalue()
        self.assertEqual(rendered.count("\x1b[2K"), len("IDENTITY"))
        self.assertIn("\x1b[1;92m  I D E N T I T Y\x1b[0m", rendered)
        self.assertIn("\x1b[32m  SCAN  /  TEST  /  REPORT\x1b[0m", rendered)
        command_stream = Terminal()
        command_intro = ProgressDisplay(command_stream)
        command_intro.vt = True
        command_intro.show_intro(delay=0)
        self.assertIn("I D E N T I T Y", command_stream.getvalue())
        self.assertNotIn("SCAN  /  TEST  /  REPORT", command_stream.getvalue())
        captured = io.StringIO()
        ProgressDisplay(captured).show_intro(delay=0)
        self.assertEqual(captured.getvalue(), "")

    def test_panels_fit_width_and_do_not_leave_old_bars(self):
        rendered = panel("IDENTITY / SCANNER", [("Projeto", "D:/codes/um-projeto-com-nome-bem-grande")], width=50)
        self.assertTrue(all(len(line) <= 50 for line in rendered.splitlines()))
        self.assertNotIn("+----", rendered)
        narrow = panel("IDENTITY", [("Arquivo", "agente.tes")], width=20)
        self.assertTrue(all(len(line) <= 20 for line in narrow.splitlines()))
        tiny = panel("IDENTITY", [("Arquivo", "agente.tes")], width=4)
        self.assertTrue(all(len(line) <= 4 for line in tiny.splitlines()))
        stream = Terminal()
        progress = ProgressDisplay(stream)
        progress.interactive = True
        progress.vt = True
        progress.set_stage("Escaneando", {"GET": 1})
        progress._render()
        progress.show_panel("SCANNER / API", [("Rotas", "5")])
        progress._render()
        self.assertIn("SCANNER / API", stream.getvalue())
        self.assertIn("Rotas", stream.getvalue())

    def test_bars_follow_completed_results_by_type(self):
        stream = Terminal()
        progress = ProgressDisplay(stream)
        progress.interactive = True
        progress.vt = True
        progress.set_stage("Testando respostas", {"HORA": 2, "conversa": 1})
        progress.advance("HORA")
        progress._render()
        self.assertIn("Total", stream.getvalue())
        self.assertIn("1/3", stream.getvalue())
        self.assertIn("HORA", stream.getvalue())
        self.assertIn("1/2", stream.getvalue())
        self.assertIn("conversa", stream.getvalue())
        self.assertIn("\x1b[31m", stream.getvalue())
        self.assertIn("\x1b[33m", stream.getvalue())
        snapshot = stream.getvalue()
        progress._render()
        self.assertEqual(snapshot, stream.getvalue())
        self.assertNotIn("\x1b[J", snapshot)
        progress.advance("HORA")
        progress.advance("conversa")
        progress._render(final=True)
        self.assertIn("3/3", stream.getvalue())
        self.assertIn("2/2", stream.getvalue())
        self.assertIn("\x1b[32m", stream.getvalue())

    def test_many_capabilities_fit_live_area_and_rounds_stack(self):
        stream = Terminal()
        progress = ProgressDisplay(stream)
        progress.vt = True
        progress.set_stage("Rodada 1", {f"tipo_{index}": 1 for index in range(58)})
        progress.advance("tipo_0")
        progress._render()
        self.assertLessEqual(progress._lines, 8)
        self.assertIn("Outros", stream.getvalue())
        progress.show_round(1, 2, 1, 0, "LLM rejeitado")
        self.assertEqual(progress._lines, 0)
        progress.set_stage("Rodada 2", {"conversa": 1})
        progress.advance("conversa")
        progress._render()
        progress.show_round(2, 2, 0, 1)
        rendered = stream.getvalue()
        self.assertEqual(rendered.count("Rodada 01/02"), 1)
        self.assertEqual(rendered.count("Rodada 02/02"), 1)
        self.assertLess(rendered.index("Rodada 01/02"), rendered.index("Rodada 02/02"))
        self.assertIn("Testes 1/1", rendered)
        self.assertIn("Testes 0/1", rendered)

    def test_phase_without_known_total_has_no_percentage(self):
        stream = Terminal()
        progress = ProgressDisplay(stream)
        progress.interactive = True
        progress.vt = True
        progress.set_stage("Treinando modelo")
        progress._render()
        self.assertIn("Treinando modelo", stream.getvalue())
        self.assertNotIn("%", stream.getvalue())

    def test_error_does_not_appear_as_completed(self):
        stream = Terminal()
        progress = ProgressDisplay(stream)
        progress.vt = True
        progress.finish("Erro")
        self.assertIn("Erro", stream.getvalue())
        self.assertNotIn("Finalizado", stream.getvalue())

    def test_gradient_and_noninteractive_output(self):
        self.assertEqual([ProgressDisplay._color(value) for value in (0, 0.5, 1)], ["31", "33", "32"])
        stream = io.StringIO()
        progress = ProgressDisplay(stream).start()
        progress.set_stage("Testando API", {"GET": 10})
        for _ in range(10):
            progress.advance("GET")
        progress.finish()
        self.assertEqual(stream.getvalue().count("GET"), 0)
        self.assertEqual(stream.getvalue().count("Testando API"), 1)

    def test_fallback_terminal_rewrites_one_line(self):
        stream = Terminal()
        progress = ProgressDisplay(stream)
        progress.interactive = True
        progress.vt = False
        progress.set_stage("Testando API", {"GET": 2})
        progress.advance("GET")
        progress._render()
        progress.advance("GET")
        progress._render(final=True)
        self.assertIn("\r", stream.getvalue())
        self.assertNotIn("\n", stream.getvalue())

    def test_api_bar_advances_with_real_completed_requests(self):
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
            stream = Terminal()
            progress = ProgressDisplay(stream)
            progress.interactive = True
            progress.vt = True
            progress.set_stage("Testando API", {"GET": 3})
            config = RunConfig(
                f"http://127.0.0.1:{server.server_port}",
                (Scenario("GET", "/health", (200,), {}),),
                {}, 1, 2.0, None,
            )
            with tempfile.TemporaryDirectory() as folder:
                result = asyncio.run(run_agents(3, config, Path(folder) / "resultados.jsonl", progress))
            self.assertEqual(result["passed"], 3)
            self.assertEqual(progress.done["GET"], 3)
            progress._render(final=True)
            self.assertIn("GET", stream.getvalue())
            self.assertIn("3/3", stream.getvalue())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
