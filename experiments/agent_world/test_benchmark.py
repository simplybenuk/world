"""Focused experiment checks. Provider calls are mocked in these tests."""

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from .agent import CONTRACT_REVISION, CodexTurnRunner, CrudAdapter, FactAdapter, parse_usage_events, run_agent
from .benchmark import HISTORY_REASON, adapter_for, disposable_pair, expected_pipeline, live_correctness, prepare_history, run_scripted, scripted_pipeline
from .domain import DEFAULT_DEFINITION, load_fixture
from world import WorldDefinition, WorldStore


def tool(operation, arguments):
    return {"kind": "tool", "operation": operation, "arguments_json": json.dumps(arguments),
            "answer_json": "null", "proposals": []}


def answer(value, proposals=None):
    return {"kind": "answer", "operation": "none", "arguments_json": "{}",
            "answer_json": json.dumps(value), "proposals": proposals or []}


class FakeRunner:
    def __init__(self, commands):
        self.commands = iter(commands)
        self.payloads = []

    def generate(self, payload):
        self.payloads.append(json.loads(json.dumps(payload)))
        command = next(self.commands)
        if command is None:
            return {"ok": False, "error": "generation_failed", "usage": None}
        return {"ok": True, "command": command,
                "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20}}


class BenchmarkTests(unittest.TestCase):
    def test_scripted_contracts_produce_equal_fixture_answers_and_show_tradeoffs(self):
        report = run_scripted()
        self.assertTrue(report["all_checks_passed"])
        pipeline = report["scenarios"]["pipeline"]
        self.assertEqual(report["contract_revision"], CONTRACT_REVISION)
        self.assertEqual(pipeline["crud"]["answer"], pipeline["facts"]["answer"])
        records = pipeline["facts"]["answer"]["opportunities"]
        self.assertEqual([record["id"] for record in records], ["northstar-pilot", "northstar-renewal"])
        self.assertEqual([record["amount_minor"] for record in records], [650000, 2400000])
        self.assertEqual(records[1]["next_stages"], ["proposal", "lost"])
        self.assertEqual(records[1]["latest_interaction"]["author_name"], "Maya")
        self.assertEqual(pipeline["crud"]["metrics"]["logical_service_operations"], 6)
        self.assertEqual(pipeline["facts"]["metrics"]["logical_service_operations"], 3)
        for mode in pipeline:
            self.assertEqual(pipeline[mode]["metrics"]["discovery_operations"], 1)
            self.assertGreater(pipeline[mode]["metrics"]["discovery_response_bytes"], 0)
            self.assertGreater(pipeline[mode]["database_size"]["total_bytes"], 0)
        history = report["scenarios"]["history"]
        self.assertEqual(history["crud"]["answer"], history["facts"]["answer"])
        self.assertEqual(history["crud"]["metrics"]["logical_service_operations"], 2)
        self.assertEqual(history["facts"]["metrics"]["logical_service_operations"], 2)
        for mode, run in report["scenarios"]["pipeline_bulk"].items():
            self.assertEqual(run["answer"], pipeline[mode]["answer"])
            self.assertEqual(run["metrics"]["logical_service_operations"], 2)
            self.assertEqual([call["request"]["operation"] for call in run["service_calls"]], ["describe", "list"])

    def test_unrelated_records_do_not_expand_targeted_returned_answers(self):
        report = run_scripted(distractors=25)
        self.assertTrue(report["all_checks_passed"])
        self.assertEqual(report["fixture_records"], 114)
        for mode in ("crud", "facts"):
            run = report["scenarios"]["pipeline"][mode]
            self.assertEqual(len(run["answer"]["opportunities"]), 2)
            self.assertEqual(run["metrics"]["logical_service_operations"], 6 if mode == "crud" else 3)
            bulk = report["scenarios"]["pipeline_bulk"][mode]
            self.assertEqual(bulk["answer"], run["answer"])
            self.assertEqual(bulk["metrics"]["logical_service_operations"], 2)
            self.assertEqual(len(bulk["service_calls"][1]["response"]), 110)
            self.assertGreater(bulk["metrics"]["response_bytes"], run["metrics"]["response_bytes"])

    def test_latest_interaction_uses_same_id_tie_break_in_both_contracts(self):
        document = json.loads(DEFAULT_DEFINITION.read_text())
        duplicate = json.loads(json.dumps(next(item for item in document["seed"] if item["id"] == "renewal-discovery")))
        duplicate["id"] = "aaa-renewal-discovery"
        duplicate["data"]["body"] = "Same date, smaller ID."
        document["seed"].append(duplicate)
        definition = WorldDefinition(document)
        with disposable_pair(definition) as (stores, _):
            answers = [scripted_pipeline(adapter_for(mode, store)) for mode, store in stores.items()]
        self.assertEqual(answers[0], answers[1])
        self.assertEqual(answers[0], expected_pipeline(definition))
        self.assertEqual(answers[0]["opportunities"][1]["latest_interaction"]["id"], "aaa-renewal-discovery")

    def test_batches_distinguish_rollback_from_sequential_partial_success(self):
        report = run_scripted()
        for scenario, code in (("stale_batch", "version_conflict"), ("invalid_transition_batch", "invalid_transition")):
            comparison = report["scenarios"][scenario]
            self.assertTrue(comparison["facts"]["answer"]["unchanged"])
            self.assertEqual(comparison["facts"]["answer"]["error"]["code"], code)
            self.assertFalse(comparison["crud"]["answer"]["unchanged"])
            self.assertTrue(comparison["crud"]["answer"]["first_operation_committed"])
        for run in report["scenarios"]["viewer_permissions"].values():
            self.assertTrue(run["answer"]["private_atlas_hidden"])
            self.assertTrue(run["answer"]["update_denied"])
            self.assertTrue(run["answer"]["atlas_update_denied"])
        for run in report["scenarios"]["member_permissions"].values():
            self.assertTrue(run["answer"]["private_atlas_hidden"])
            self.assertTrue(run["answer"]["northstar_update_permitted"])
            self.assertTrue(run["answer"]["atlas_update_denied"])
        self.assertFalse(report["all_tasks_correct"])
        self.assertFalse(report["scenarios"]["stale_batch"]["crud"]["correct"])
        self.assertTrue(report["scenarios"]["stale_batch"]["crud"]["expected_behavior_observed"])


class AgentBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.definition = load_fixture()
        self.store = WorldStore(Path(self.directory.name) / "records.sqlite3", self.definition)
        self.addCleanup(self.store.close)
        self.adapter = CrudAdapter(self.store, "maya", disposable=True)

    def test_model_receives_discovery_then_only_requested_records(self):
        runner = FakeRunner([tool("query", {"type": "Opportunity", "filters": [
            {"field": "name", "op": "eq", "value": "Northstar renewal"}]}),
            answer({"id": "northstar-renewal", "stage": "qualified", "version": 1})])
        result = run_agent(self.adapter, "Inspect the renewal.", runner=runner)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(runner.payloads[0]["transcript"], [])
        self.assertNotIn("records", runner.payloads[0]["world"])
        self.assertIn("meaning", runner.payloads[0]["world"]["types"]["Opportunity"]["fields"]["amount_minor"])
        returned = runner.payloads[1]["transcript"][0]["response"]
        self.assertEqual([record["id"] for record in returned], ["northstar-renewal"])
        self.assertEqual(result["usage"]["input_tokens"], 200)
        self.assertTrue(result["usage"]["complete"])
        self.assertEqual(result["contract_revision"], CONTRACT_REVISION)
        self.assertEqual(result["contract"], runner.payloads[0]["contract"])
        self.assertEqual(runner.payloads[0]["world"]["record_counts"]["visible_total"], 10)

    def test_reported_contract_is_frozen_at_session_start(self):
        self.adapter.contract = json.loads(json.dumps(self.adapter.contract))

        class MutatingRunner(FakeRunner):
            def generate(inner, payload):
                self.adapter.contract["revision"] = "changed-after-start"
                self.adapter.contract["operations"]["query"]["returns"] = "changed-after-start"
                return super().generate(payload)

        result = run_agent(self.adapter, "Inspect discovery.", runner=MutatingRunner([answer({"ok": True})]))
        self.assertEqual(result["contract_revision"], CONTRACT_REVISION)
        self.assertNotEqual(result["contract"], self.adapter.contract)

    def test_fact_compatibility_reads_are_available_to_the_agent(self):
        with disposable_pair(self.definition) as (stores, _):
            adapter = adapter_for("facts", stores["facts"])
            commands = [tool("list", {"type": "Opportunity"}), tool("query", {"type": "Opportunity", "filters": [
                {"field": "name", "op": "eq", "value": "Northstar renewal"}]}),
                tool("get", {"type": "Opportunity", "id": "northstar-renewal"}), answer({"ok": True})]
            result = run_agent(adapter, "Exercise current-record reads.", runner=FakeRunner(commands))
        self.assertEqual(result["status"], "completed")
        self.assertEqual([call["request"]["operation"] for call in result["service_calls"]], ["describe", "list", "query", "get"])
        self.assertEqual(len(result["transcript"][0]["response"]), 2)
        self.assertEqual(result["transcript"][1]["response"][0]["id"], "northstar-renewal")
        self.assertEqual(result["transcript"][2]["response"]["data"]["stage"], "qualified")

    def test_malformed_request_is_counted_and_error_is_visible(self):
        runner = FakeRunner([tool("get", {"type": "Opportunity"}), answer({"error": "Need an ID."})])
        result = run_agent(self.adapter, "Inspect a record.", runner=runner)
        self.assertEqual(result["metrics"]["logical_service_operations"], 2)
        self.assertEqual(result["transcript"][0]["response"]["error"]["code"], "invalid_arguments")

    def test_model_cannot_execute_an_update_as_a_read_command(self):
        runner = FakeRunner([tool("update", {"type": "Opportunity", "id": "northstar-renewal",
                                                "changes": {"stage": "proposal"}, "expected_version": 1})])
        result = run_agent(self.adapter, "Prepare an update.", runner=runner)
        self.assertEqual(result["status"], "invalid_command")
        self.assertEqual(self.store.get_record("maya", "Opportunity", "northstar-renewal")["data"]["stage"], "qualified")

    def test_failed_provider_attempt_is_preserved_and_not_retried(self):
        runner = FakeRunner([None, answer({"should": "never run"})])
        result = run_agent(self.adapter, "Inspect the pipeline.", runner=runner)
        self.assertEqual(result["status"], "provider_failed")
        self.assertEqual(len(result["turns"]), 1)
        self.assertFalse(result["usage"]["complete"])
        self.assertIsNone(result["usage"]["input_tokens"])

    def test_wrong_final_answer_fails_independent_oracle(self):
        runner = FakeRunner([answer({"opportunities": []})])
        result = run_agent(self.adapter, "Inspect the pipeline.", runner=runner)
        correct, _ = live_correctness(result, "pipeline", self.definition)
        self.assertFalse(correct)
        self.assertNotEqual(result["answer"], expected_pipeline(self.definition))

    def test_history_actor_identity_requires_stored_id(self):
        result = {"status": "completed", "proposals": [], "answer": {
            "id": "northstar-renewal", "then_stage": "qualified", "then_version": 1,
            "now_stage": "proposal", "now_version": 2, "changed_by": "Maya", "reason": HISTORY_REASON}}
        self.assertFalse(live_correctness(result, "history", self.definition)[0])
        result["answer"]["changed_by"] = "maya"
        self.assertTrue(live_correctness(result, "history", self.definition)[0])

    def test_fact_agent_can_read_existing_ledger_history_in_one_task_call(self):
        expected = {"id": "northstar-renewal", "then_stage": "qualified", "then_version": 1,
                    "now_stage": "proposal", "now_version": 2, "changed_by": "maya", "reason": HISTORY_REASON}
        with disposable_pair(self.definition) as (stores, _):
            prepare_history(stores["facts"], "facts")
            adapter = adapter_for("facts", stores["facts"])
            runner = FakeRunner([tool("history", {"type": "Opportunity", "id": "northstar-renewal"}), answer(expected)])
            result = run_agent(adapter, "Read the renewal history.", runner=runner)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(live_correctness(result, "history", self.definition)[0])
        self.assertEqual([call["request"]["operation"] for call in result["service_calls"]], ["describe", "history"])
        events = result["transcript"][0]["response"]
        self.assertEqual(events[0]["after"]["data"]["stage"], "qualified")
        self.assertEqual(events[-1]["after"]["data"]["stage"], "proposal")

    def test_guessed_proposal_without_current_read_fails_oracle(self):
        proposal = {"type": "Opportunity", "id": "northstar-renewal", "changes": [
            {"field": "stage", "value_json": '"proposal"'}], "expected_version": 1, "reason": "Requested proposal."}
        result = run_agent(self.adapter, "Prepare an update.", runner=FakeRunner([
            answer({"id": "northstar-renewal", "stage": "qualified", "version": 1}, [proposal])]))
        self.assertFalse(live_correctness(result, "propose-stage", self.definition)[0])
        read = tool("query", {"type": "Opportunity", "filters": [{"field": "name", "op": "eq", "value": "Northstar renewal"}]})
        result = run_agent(CrudAdapter(self.store, "maya", disposable=True), "Prepare an update.", runner=FakeRunner([
            read, answer({"id": "northstar-renewal", "stage": "qualified", "version": 1}, [proposal])]))
        self.assertTrue(live_correctness(result, "propose-stage", self.definition)[0])

    def test_review_proposal_does_not_write_until_explicit_disposable_apply(self):
        proposal = {"type": "Opportunity", "id": "northstar-renewal", "changes": [
            {"field": "stage", "value_json": '"proposal"'}], "expected_version": 1, "reason": "Requested proposal."}
        runner = FakeRunner([answer({"id": "northstar-renewal", "stage": "qualified", "version": 1}, [proposal])])
        result = run_agent(self.adapter, "Prepare an update.", runner=runner)
        self.assertFalse(result["applied"])
        self.assertEqual(self.store.get_record("maya", "Opportunity", "northstar-renewal")["version"], 1)
        outsider = CrudAdapter(self.store, "maya")
        with self.assertRaises(ValueError):
            outsider.apply(result["proposals"])
        self.adapter.apply(result["proposals"])
        self.assertEqual(self.store.get_record("maya", "Opportunity", "northstar-renewal")["data"]["stage"], "proposal")


class ProviderTests(unittest.TestCase):
    def test_provider_usage_is_reported_without_byte_estimates(self):
        parsed = parse_usage_events('\n'.join([json.dumps({"type": "turn.started"}), json.dumps({
            "type": "turn.completed", "usage": {"input_tokens": 1234, "cached_input_tokens": 100, "output_tokens": 45}})]))
        self.assertEqual(parsed["usage"], {"input_tokens": 1234, "cached_input_tokens": 100, "output_tokens": 45})
        self.assertIsNone(parse_usage_events('{"type":"turn.failed"}')["usage"])

    def test_codex_command_has_no_shell_or_web_and_captures_usage(self):
        captured = []

        class Process:
            returncode = 0

            def communicate(self, _payload=None, timeout=None):
                return json.dumps({"type": "turn.completed", "usage": {
                    "input_tokens": 50, "cached_input_tokens": 0, "output_tokens": 10}}), "model: reported-model\n"

        def popen(command, **kwargs):
            captured.append((command, kwargs))
            Path(command[command.index("--output-last-message") + 1]).write_text(json.dumps(answer({"ok": True})))
            return Process()

        with patch("experiments.agent_world.agent.shutil.which", return_value="/fake/codex"), patch(
                "experiments.agent_world.agent.subprocess.Popen", side_effect=popen):
            result = CodexTurnRunner().generate({"request": "Test."})
        command, kwargs = captured[0]
        self.assertTrue(result["ok"])
        self.assertEqual(result["reported_model"], "reported-model")
        self.assertEqual(result["usage"]["input_tokens"], 50)
        for flag in ("features.shell_tool=false", "features.unified_exec=false", 'web_search="disabled"', "features.apps=false"):
            self.assertIn(flag, command)
        self.assertIn("read-only", command)
        self.assertTrue(kwargs["start_new_session"])

    def test_timeout_kills_entire_provider_process_group(self):
        class Process:
            pid = 12345
            returncode = -9
            calls = 0

            def communicate(self, _payload=None, timeout=None):
                self.calls += 1
                if self.calls == 1:
                    raise subprocess.TimeoutExpired("codex", timeout)
                return "", ""

        with patch("experiments.agent_world.agent.shutil.which", return_value="/fake/codex"), patch(
                "experiments.agent_world.agent.subprocess.Popen", return_value=Process()), patch(
                "experiments.agent_world.agent.os.killpg") as kill:
            result = CodexTurnRunner(timeout=1).generate({"request": "Test."})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "generation_timeout")
        self.assertEqual(kill.call_args.args[0], 12345)


if __name__ == "__main__":
    unittest.main()
