import json
import subprocess
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import patch

from identity.conversation_ai import save_conversations
from identity.llm_conversations import add_round_pairs
from identity.llm_training import (
    _base_python,
    _check_environment,
    _ollama_active_error,
    _run,
    _training_lock,
    preflight_space,
    train_llm_round,
)


class LLMTrainingTests(unittest.TestCase):
    def test_training_python_uses_base_interpreter_and_rejects_mixed_stdlib(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "yasmin"
            app = project / ".venv" / "Scripts" / "python.exe"
            app.parent.mkdir(parents=True)
            app.touch()
            base = Path(folder) / "pythoncore-3.13" / "python.exe"
            base.parent.mkdir()
            base.touch()
            output = Path(folder) / "logs"
            output.mkdir()
            with patch("identity.llm_training._run", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps({"base": str(base), "version": [3, 13]}), "")):
                selected, version, error = _base_python(project, output)
            self.assertIsNone(error)
            self.assertEqual(selected, base)
            self.assertEqual(version, (3, 13))

            venv_python = Path(folder) / "train-env" / "Scripts" / "python.exe"
            stdlib = Path(folder) / "pythoncore-3.14" / "Lib" / "venv" / "__init__.py"
            stdlib.parent.mkdir(parents=True)
            stdlib.touch()
            payload = {"version": [3, 13], "prefix": str(venv_python.parent.parent),
                       "base_prefix": str(stdlib.parents[2]), "stdlib": str(stdlib)}
            with patch("identity.llm_training._run", return_value=subprocess.CompletedProcess(
                    [], 0, json.dumps(payload), "")):
                mismatch = _check_environment(venv_python, (3, 13), project, output)
            self.assertIn("incompatível", mismatch)

    def test_reviewed_pairs_are_verified_distinct_and_survive_rescan(self):
        with tempfile.TemporaryDirectory() as folder:
            project_folder = Path(folder) / "projects" / "yasmin"
            inventory = {"capabilities": [], "mechanisms": []}
            original = {"capability": "conversa", "reviewed": False,
                        "messages": [{"role": "user", "content": "pergunta antiga"}]}
            _, path = save_conversations(project_folder, inventory, [original])
            before = path.read_bytes()
            first = add_round_pairs(path, 1)
            self.assertEqual(first["added"], 40)
            self.assertTrue(path.read_bytes().startswith(before))
            second = add_round_pairs(path, 2)
            self.assertEqual(second["added"], 10)
            next_campaign = add_round_pairs(path, 1)
            self.assertEqual(next_campaign["added"], 10)
            save_conversations(project_folder, inventory, [original])
            records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            reviewed = [item for item in records if item.get("reviewed") is True]
            self.assertEqual(len(reviewed), 60)
            self.assertEqual(len({item["messages"][0]["content"] for item in reviewed}), 60)
            self.assertTrue(all(item["evidence"]["kind"] == "deterministic_python" for item in reviewed))
            self.assertTrue(all("\ufffd" not in item["messages"][1]["content"] for item in reviewed))

    def test_timeout_keeps_partial_training_output(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "training.log"
            expired = subprocess.TimeoutExpired(["python"], 1,
                                                output=b'{"passos":16}\n', stderr=b'processing')
            with patch("identity.llm_training.subprocess.run", side_effect=expired):
                result = _run(["python"], Path(folder), log, 1)
            self.assertEqual(result.returncode, 124)
            self.assertIn('"passos":16', result.stdout)
            self.assertIn("Tempo limite", result.stderr)
            self.assertIn('"passos":16', log.read_text(encoding="utf-8"))

    def test_ollama_check_catches_server_with_old_model_directory(self):
        with patch("identity.llm_training.http.client.HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.read.return_value = b'{"models":[{"name":"yasmin-treino:old"}]}'
            self.assertIsNone(_ollama_active_error(Path("D:/codes/yasmin"), "yasmin-treino:old"))
            error = _ollama_active_error(Path("D:/codes/yasmin"), "yasmin-treino:new")
            self.assertIn("OLLAMA_MODELS", error)

    def test_training_lock_rejects_second_handle(self):
        with tempfile.TemporaryDirectory() as folder:
            lock = Path(folder) / "train.lock"
            with _training_lock(lock) as first, _training_lock(lock) as second:
                self.assertTrue(first)
                self.assertFalse(second)
            with _training_lock(lock) as acquired_again:
                self.assertTrue(acquired_again)

    def test_concurrent_training_is_blocked_before_loading_model(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "yasmin"
            conversations = Path(folder) / "dados.jsonl"
            conversations.write_text("", encoding="utf-8")
            output = Path(folder) / "run"
            with patch("identity.llm_training._training_lock", return_value=nullcontext(False)), \
                    patch("identity.llm_training.prepare_environment") as prepare:
                result = train_llm_round(project, conversations, output, 1)
            prepare.assert_not_called()
            self.assertEqual(result["status"], "bloqueado")
            self.assertIn("Outro treino", result["reason"])
            self.assertEqual(result["optimization_steps"], 0)

    def test_disk_block_prevents_installation_or_weight_change(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "yasmin"
            active = project / "Agent" / "modelos" / "llm_ativa.json"
            active.parent.mkdir(parents=True)
            active.write_text('{"modelo":"yasmin-treino:anterior"}', encoding="utf-8")
            conversations = Path(folder) / "dados.jsonl"
            conversations.write_text("", encoding="utf-8")
            output = Path(folder) / "run"
            with patch("identity.llm_training.preflight_space", return_value={
                "ok": False, "volumes": [{"volume": "E:", "free_bytes": 1,
                                           "required_bytes": 2, "ok": False, "items": ["pesos"]}]}), \
                    patch("identity.llm_training.prepare_environment") as prepare:
                result = train_llm_round(project, conversations, output, 1)
            prepare.assert_not_called()
            self.assertEqual(result["status"], "bloqueado")
            self.assertEqual(result["optimization_steps"], 0)
            self.assertEqual(json.loads(active.read_text(encoding="utf-8"))["modelo"], "yasmin-treino:anterior")
            self.assertTrue((output / "llm-rodada-1.json").is_file())

    def test_space_budget_counts_merged_weights_and_ollama_import(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "yasmin"
            with patch("identity.llm_training.shutil.disk_usage") as usage:
                usage.return_value.free = 250
                result = preflight_space(project, project / "venv", project / "cache",
                                         project / "ollama", model_bytes=100)
            self.assertFalse(result["ok"])
            self.assertGreater(result["volumes"][0]["required_bytes"], 200)

    def test_published_status_requires_steps_losses_adapter_and_active_pointer(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "yasmin"
            agent = project / "Agent"
            agent.mkdir(parents=True)
            active = agent / "modelos" / "llm_ativa.json"
            adapter = agent / "modelos" / "treino_llm" / "rodada" / "adaptador"
            adapter.mkdir(parents=True)
            (adapter / "adapter_model.safetensors").write_bytes(b"teste")
            data = Path(folder) / "dados.jsonl"
            data.write_text("", encoding="utf-8")

            def run(command, cwd, log, timeout, extra_env=None):
                if "--dry-run" in command:
                    stdout = '{"treino_revisado":32,"avaliacao_reservada":8,"novos_desde_publicacao":32}\n'
                else:
                    self.assertEqual(command[-2:], ["--max-passos", "40"])
                    self.assertIsNotNone(extra_env)
                    self.assertIn("IDENTITY_GGUF_CONVERTER", extra_env)
                    active.write_text(json.dumps({"modelo": "yasmin-treino:novo", "adaptador": str(adapter)}),
                                      encoding="utf-8")
                    stdout = ('{"passos":40,"perda_anterior":0.0045,"perda_candidata":0.0044,"melhorou":true}\n'
                              'Pesos LoRA publicados e ativados: yasmin-treino:novo\n')
                return subprocess.CompletedProcess(command, 0, stdout, "")

            with patch("identity.llm_training.preflight_space", return_value={"ok": True, "volumes": []}), \
                    patch("identity.llm_training.prepare_environment", return_value=(Path("python"), None)), \
                    patch("identity.llm_training._run", side_effect=run):
                result = train_llm_round(project, data, Path(folder) / "out", 1)
            self.assertEqual(result["status"], "publicado")
            self.assertEqual(result["optimization_steps"], 40)
            self.assertEqual(result["optimization_steps_target"], 40)
            self.assertEqual(result["loss_before"], 0.0045)
            self.assertEqual(result["loss_candidate"], 0.0044)
            self.assertEqual(result["adapter"], str(adapter / "adapter_model.safetensors"))
            self.assertEqual(result["model_published"], "yasmin-treino:novo")

    def test_timeout_recovers_candidate_metrics_without_activating_model(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "yasmin"
            active = project / "Agent" / "modelos" / "llm_ativa.json"
            active.parent.mkdir(parents=True)
            original = b'{"modelo":"yasmin-treino:old"}'
            active.write_bytes(original)
            data = Path(folder) / "dados.jsonl"
            data.write_text("", encoding="utf-8")

            def run(command, cwd, log, timeout, extra_env=None):
                if "--dry-run" in command:
                    return subprocess.CompletedProcess(command, 0,
                        '{"treino_revisado":30,"avaliacao_reservada":5,"novos_desde_publicacao":8}\n', "")
                candidate = project / "Agent" / "modelos" / "treino_llm" / "round"                             / "adaptador" / "adapter_model.safetensors"
                candidate.parent.mkdir(parents=True)
                candidate.write_bytes(b"weights")
                (candidate.parent.parent / "metricas.json").write_text(json.dumps({
                    "passos": 16, "treino": 30, "avaliacao": 5,
                    "perda_anterior": 2.0, "perda_candidata": 1.5, "melhorou": True,
                }), encoding="utf-8")
                return subprocess.CompletedProcess(command, 124, "", "Tempo limite excedido")

            with patch("identity.llm_training.preflight_space", return_value={"ok": True, "volumes": []}), \
                    patch("identity.llm_training.prepare_environment", return_value=(Path("python"), None)), \
                    patch("identity.llm_training._ollama_active_error", return_value=None), \
                    patch("identity.llm_training._run", side_effect=run):
                result = train_llm_round(project, data, Path(folder) / "out", 1)
            self.assertEqual(result["status"], "falhou")
            self.assertEqual(result["optimization_steps"], 16)
            self.assertEqual(result["loss_candidate"], 1.5)
            self.assertIn("publicação excedeu", result["reason"])
            self.assertEqual(active.read_bytes(), original)

    def test_failed_publication_restores_previous_active_pointer(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder) / "yasmin"
            agent = project / "Agent"
            agent.mkdir(parents=True)
            active = agent / "modelos" / "llm_ativa.json"
            active.parent.mkdir(parents=True)
            original = b'{"modelo":"yasmin-treino:anterior"}'
            active.write_bytes(original)
            data = Path(folder) / "dados.jsonl"
            data.write_text("", encoding="utf-8")

            def run(command, cwd, log, timeout, extra_env=None):
                if "--dry-run" in command:
                    return subprocess.CompletedProcess(command, 0,
                        '{"treino_revisado":40,"avaliacao_reservada":10,"novos_desde_publicacao":8}\n', "")
                self.assertEqual(command[-2:], ["--max-passos", "16"])
                active.write_text('{"modelo":"yasmin-treino:falhou"}', encoding="utf-8")
                return subprocess.CompletedProcess(command, 1,
                    '{"passos":10,"perda_anterior":2.0,"perda_candidata":2.1,"melhorou":false}\n',
                    "Publicação falhou")

            with patch("identity.llm_training.preflight_space", return_value={"ok": True, "volumes": []}), \
                    patch("identity.llm_training.prepare_environment", return_value=(Path("python"), None)), \
                    patch("identity.llm_training._ollama_active_error", return_value=None), \
                    patch("identity.llm_training._run", side_effect=run):
                result = train_llm_round(project, data, Path(folder) / "out", 1)
            self.assertEqual(result["status"], "rejeitado")
            self.assertIn("Perda reservada não melhorou", result["reason"])
            self.assertEqual(result["optimization_steps_target"], 16)
            self.assertEqual(active.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
