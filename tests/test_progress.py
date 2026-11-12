import asyncio
import io
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from identity.cli import RunConfig, Scenario, run_agents
from identity.progress import ProgressDisplay


class Terminal(io.StringIO):
    def isatty(self):
        return True


class ProgressTests(unittest.TestCase):
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

    def test_phase_without_known_total_has_no_percentage(self):
        stream = Terminal()
        progress = ProgressDisplay(stream)
        progress.interactive = True
        progress.vt = True
        progress.set_stage("Treinando modelo")
        progress._render()
        self.assertIn("Treinando modelo", stream.getvalue())
        self.assertNotIn("%", stream.getvalue())

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
