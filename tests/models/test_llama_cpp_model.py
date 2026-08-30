import json

import pytest
import requests

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import FormatError, ModelStreamError
from minisweagent.models.llama_cpp_model import LlamaCppModel
from minisweagent.models.llama_log import format_llama_server_stats, parse_llama_server_log
from minisweagent.models.utils.tool_protocol import NemotronToolProtocol

LOG = """
35.00.706.626 I slot operator(): id  0 | task 0 | new prompt, n_ctx_slot = 131072, n_keep = 0, task.n_tokens = 429
42.34.228.520 I slot print_timing: id  0 | task 0 | prompt eval time = 87441.29 ms / 429 tokens (203.83 ms per token, 4.91 tokens per second)
42.34.228.526 I slot print_timing: id  0 | task 0 | eval time = 366080.58 ms / 259 tokens (1418.92 ms per token, 0.70 tokens per second)
42.34.228.527 I slot print_timing: id  0 | task 0 | total time = 453521.86 ms / 688 tokens
42.34.228.535 I slot print_timing: id  0 | task 0 | graphs reused = 253
"""


class Response:
    def __init__(self, chunks):
        self.chunks = chunks

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=True):
        return [f"data: {json.dumps(chunk)}" for chunk in self.chunks] + ["data: [DONE]"]


class JsonResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=True):
        return [json.dumps(self.body)]

    def json(self):
        return self.body


class SseMetadataResponse(Response):
    def iter_lines(self, decode_unicode=True):
        return ["event: message", ": keep-alive", *super().iter_lines(decode_unicode)]


class RawResponse:
    def __init__(self, lines, error=None):
        self.lines = lines
        self.error = error

    def raise_for_status(self):
        return None

    def iter_lines(self, decode_unicode=True):
        yield from self.lines
        if self.error:
            raise self.error


def test_llama_log_parser_reports_context_and_rates():
    stats = parse_llama_server_log(LOG)
    assert stats is not None
    assert stats.context_tokens == 429
    assert stats.context_limit == 131072
    assert stats.prompt_tokens == 429
    assert stats.prompt_tokens_per_second == 4.91
    assert stats.generation_tokens == 259
    assert stats.generation_tokens_per_second == 0.70
    assert stats.total_tokens == 688
    assert stats.graphs_reused == 253
    assert "context=429/131072" in format_llama_server_stats(stats)
    assert "generation=0.70 tok/s" in format_llama_server_stats(stats)


def test_llama_cpp_reconstructs_fragmented_tool_call(monkeypatch, tmp_path, capsys):
    log_path = tmp_path / "llama.log"
    log_path.write_text(LOG)
    chunks = [
        {"choices": [{"delta": {"content": "thinking "}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "ba"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"name": "sh", "arguments": '{"command":"echo '}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": 'ok"}'}}]}}]},
    ]
    seen = {}

    def post(url, **kwargs):
        seen.update(url=url, kwargs=kwargs)
        return Response(chunks)

    monkeypatch.setattr("minisweagent.models.llama_cpp_model.requests.post", post)
    model = LlamaCppModel(
        model_name="local",
        model_kwargs={"api_base": "http://server:8080/v1"},
        llama_log_path=log_path,
    )
    result = model.query([{"role": "user", "content": "task"}])
    assert result["extra"]["actions"] == [{"command": "echo ok", "tool_call_id": "call-1"}]
    assert seen["url"] == "http://server:8080/v1/chat/completions"
    assert seen["kwargs"]["json"]["stream"] is True
    assert seen["kwargs"]["json"]["tools"]
    assert {tool["function"]["name"] for tool in seen["kwargs"]["json"]["tools"]} == {
        "bash", "read_text", "replace_text", "create_text"
    }
    assert "llama.cpp: context=429/131072" in capsys.readouterr().err
    assert result["content"] == "thinking "
    assert "llama.cpp" not in result["content"]


def test_llama_cpp_reconstructs_final_text_without_tool_call(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"content": "final"}}]},
        {"choices": [{"delta": {"content": " answer"}}]},
    ]
    monkeypatch.setattr("minisweagent.models.llama_cpp_model.requests.post", lambda *a, **k: Response(chunks))
    model = LlamaCppModel(model_name="local", model_kwargs={"api_base": "http://server/v1"}, response_streaming="off")
    result = model.query([{"role": "user", "content": "task"}])
    assert result["extra"]["is_final"] is True
    assert result["extra"]["final_text"] == "final answer"


def test_llama_cpp_displays_reasoning_deltas_without_adding_them_to_content(monkeypatch, capsys):
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "Think first. "}}]},
        {"choices": [{"delta": {"content": "final"}}]},
    ]
    monkeypatch.setattr("minisweagent.models.llama_cpp_model.requests.post", lambda *a, **k: Response(chunks))
    model = LlamaCppModel(model_name="local", model_kwargs={"api_base": "http://server/v1"})
    result = model.query([{"role": "user", "content": "task"}])
    stderr = capsys.readouterr().err
    assert "Model reasoning: Think first." in stderr
    assert "Model draft: final" in stderr
    assert result["content"] == "final"


def test_llama_cpp_tool_result_is_sent_back_with_openai_chat_shape(monkeypatch):
    calls = []
    responses = [
        Response([
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {"name": "bash", "arguments": '{"command":"pwd"}'}}]}, "finish_reason": "tool_calls"}]}
        ]),
        Response([{"choices": [{"delta": {"content": "The workspace is clean."}, "finish_reason": "stop"}]}]),
    ]

    def post(url, **kwargs):
        calls.append(kwargs["json"])
        return responses.pop(0)

    monkeypatch.setattr("minisweagent.models.llama_cpp_model.requests.post", post)
    model = LlamaCppModel(model_name="local", model_kwargs={"api_base": "http://server/v1"}, response_streaming="off")
    first = model.query([{"role": "user", "content": "inspect the repo"}])
    observation = model.format_observation_messages(
        first, [{"output": "/workspace\n", "returncode": 0, "exception_info": None}]
    )
    second = model.query([
        {"role": "user", "content": "inspect the repo"},
        first,
        *observation,
    ])

    assert first["extra"]["actions"] == [{"command": "pwd", "tool_call_id": "call-1"}]
    assert observation[0]["role"] == "tool"
    assert observation[0]["tool_call_id"] == "call-1"
    assert observation[0]["content"] == "<returncode>0</returncode>\n<output>\n/workspace\n</output>"
    assert calls[1]["messages"][1]["tool_calls"][0]["id"] == "call-1"
    assert calls[1]["messages"][2] == {"role": "tool", "tool_call_id": "call-1", "content": observation[0]["content"]}
    assert second["extra"]["is_final"] is True
    assert second["extra"]["final_text"] == "The workspace is clean."


def test_llama_cpp_nonstream_chat_response_is_supported(monkeypatch):
    response = JsonResponse({"choices": [{"message": {"role": "assistant", "content": "done"}, "finish_reason": "stop"}]})
    monkeypatch.setattr("minisweagent.models.llama_cpp_model.requests.post", lambda *a, **k: response)
    result = LlamaCppModel(model_name="local", response_streaming="off").query([])
    assert result["extra"]["is_final"] is True
    assert result["content"] == "done"


def test_llama_cpp_tool_finish_without_a_tool_call_is_a_format_error(monkeypatch):
    monkeypatch.setattr(
        "minisweagent.models.llama_cpp_model.requests.post",
        lambda *a, **k: Response([{
            "choices": [{
                "delta": {"tool_calls": [{"index": 0, "id": "call-bad", "function": {"name": "bash", "arguments": "not json"}}]},
                "finish_reason": "tool_calls",
            }]
        }]),
    )
    with pytest.raises(FormatError) as exc_info:
        LlamaCppModel(model_name="local", response_streaming="off").query([])
    assert "Error parsing tool call arguments" in exc_info.value.messages[0]["content"]


def test_llama_cpp_nemotron_protocol_round_trip_and_multiple_calls(monkeypatch):
    requests = []
    responses = [
        Response([{"choices": [{"delta": {"content": '<TOOLCALL>[read_text(path="README.md", start_line=1, end_line=2), bash(command="pwd", timeout_seconds=3)]</TOOLCALL>'}}]}]),
        Response([{"choices": [{"delta": {"content": '<TOOLCALL>[read_text(path="src/app.py", start_line=4, end_line=4)]</TOOLCALL>'}}]}]),
        Response([{"choices": [{"delta": {"content": "The repository was inspected."}}]}]),
    ]

    def post(url, **kwargs):
        requests.append(kwargs["json"])
        return responses.pop(0)

    monkeypatch.setattr("minisweagent.models.llama_cpp_model.requests.post", post)
    model = LlamaCppModel(model_name="nemotron", tool_protocol="nemotron", response_streaming="off")
    first = model.query([{"role": "user", "content": "Inspect the repository."}])
    first_observation = model.format_observation_messages(
        first,
        [{"output": "line one\nline two", "returncode": 0, "exception_info": None}, {"output": "/repo", "returncode": 0, "exception_info": None}],
    )
    second = model.query([{"role": "user", "content": "Inspect the repository."}, first, *first_observation])
    second_observation = model.format_observation_messages(
        second, [{"output": "code", "returncode": 0, "exception_info": None}]
    )
    final = model.query([{"role": "user", "content": "Inspect the repository."}, first, *first_observation, second, *second_observation])

    assert first["extra"]["actions"] == [
        {"tool": "read_text", "path": "README.md", "start_line": 1, "end_line": 2, "tool_call_id": "nemotron-1"},
        {"command": "pwd", "timeout_seconds": 3, "tool_call_id": "nemotron-2"},
    ]
    prompt = next(message["content"] for message in requests[0]["messages"] if message["role"] == "user")
    assert "<AVAILABLE_TOOLS>" in prompt
    assert "read_text" in prompt
    assert prompt.index("<AVAILABLE_TOOLS>") < prompt.index("Inspect the repository.")
    assert "tools" not in requests[0]
    assert first_observation[0]["role"] == "user"
    assert "Tool result (nemotron-1)" in requests[1]["messages"][-2]["content"]
    assert second["extra"]["actions"][0]["path"] == "src/app.py"
    assert final["extra"]["is_final"] is True
    assert final["extra"]["final_text"] == "The repository was inspected."


def test_nemotron_tool_action_is_executed_before_prose_submission(monkeypatch):
    requests = []
    responses = [
        Response([{"choices": [{"delta": {"content": '<TOOLCALL>[bash(command="grep -r \'interactive\' .")]</TOOLCALL>'}}]}]),
        Response([{"choices": [{"delta": {"content": "The repository inspection is complete."}}]}]),
    ]

    def post(url, **kwargs):
        requests.append(kwargs["json"])
        return responses.pop(0)

    monkeypatch.setattr("minisweagent.models.llama_cpp_model.requests.post", post)
    model = LlamaCppModel(model_name="nemotron", tool_protocol="nemotron", response_streaming="draft")
    agent = DefaultAgent(
        model=model,
        env=LocalEnvironment(),
        system_template="You are a repository agent.",
        instance_template="{{task}}",
        step_limit=2,
    )

    info = agent.run("Inspect the repository.")

    assert agent.messages[2]["extra"]["actions"]
    assert agent.messages[2]["extra"]["is_final"] is False
    assert agent.n_tool_calls == 1
    assert agent.n_calls == 2
    assert any(message.get("role") == "user" and "<returncode>" in message.get("content", "") for message in agent.messages)
    assert info["submission"] == "The repository inspection is complete."
    assert len(requests) == 2


def test_llama_cpp_nemotron_malformed_toolcall_is_not_final(monkeypatch):
    monkeypatch.setattr(
        "minisweagent.models.llama_cpp_model.requests.post",
        lambda *args, **kwargs: Response([{"choices": [{"delta": {"content": "<TOOLCALL>[bash(command=oops)</TOOLCALL>"}}]}]),
    )
    with pytest.raises(FormatError) as exc_info:
        LlamaCppModel(model_name="nemotron", tool_protocol="nemotron", response_streaming="off").query([])
    error = exc_info.value.messages[0]["content"]
    assert "FORMAT ERROR" in error
    assert "Nemotron" in error
    assert "Available tools:" in error
    assert "- bash" in error
    assert "Nemotron is not a tool" not in error


def test_llama_cpp_accepts_sse_metadata_before_content(monkeypatch):
    monkeypatch.setattr(
        "minisweagent.models.llama_cpp_model.requests.post",
        lambda *args, **kwargs: SseMetadataResponse([
            {"choices": [{"delta": {"content": "done"}}]},
        ]),
    )
    assert LlamaCppModel(model_name="local", response_streaming="off").query([])["content"] == "done"


def test_llama_cpp_reassembles_evidently_fragmented_sse_record(monkeypatch):
    monkeypatch.setattr(
        "minisweagent.models.llama_cpp_model.requests.post",
        lambda *args, **kwargs: RawResponse([
            'data: {"choices":[{"delta":{"content":"part',
            'data: ial"}}]}',
            "data: [DONE]",
        ]),
    )
    assert LlamaCppModel(model_name="local", response_streaming="off").query([])["content"] == "partial"


def test_llama_cpp_rejects_malformed_sse_json_with_diagnostics(monkeypatch):
    monkeypatch.setattr(
        "minisweagent.models.llama_cpp_model.requests.post",
        lambda *args, **kwargs: RawResponse(["data: {not json}"]),
    )
    with pytest.raises(ModelStreamError, match="Malformed JSON") as exc_info:
        LlamaCppModel(model_name="local", response_streaming="off").query([])
    assert exc_info.value.diagnostics["line"] == 1
    assert exc_info.value.diagnostics["payload_fragment"] == "{not json}"
    assert "Expecting property name" in exc_info.value.diagnostics["parser_error"]


def test_llama_cpp_reports_truncated_final_sse_record(monkeypatch):
    monkeypatch.setattr(
        "minisweagent.models.llama_cpp_model.requests.post",
        lambda *args, **kwargs: RawResponse(['data: {"choices":[{"delta":{"content":"partial']),
    )
    with pytest.raises(ModelStreamError, match="stream ended") as exc_info:
        LlamaCppModel(model_name="local", response_streaming="off").query([])
    assert exc_info.value.diagnostics["payload_fragment"].endswith('"partial')


def test_llama_cpp_reports_disconnected_stream_with_diagnostics(monkeypatch):
    error = requests.exceptions.ChunkedEncodingError("connection reset")
    monkeypatch.setattr(
        "minisweagent.models.llama_cpp_model.requests.post",
        lambda *args, **kwargs: RawResponse([], error=error),
    )
    with pytest.raises(ModelStreamError, match="stream disconnected") as exc_info:
        LlamaCppModel(model_name="local", response_streaming="off").query([])
    assert exc_info.value.diagnostics["parser_error"] == "connection reset"


def test_model_stream_error_is_saved_in_trajectory(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "minisweagent.models.llama_cpp_model.requests.post",
        lambda *args, **kwargs: RawResponse(["data: {not json}"]),
    )
    output_path = tmp_path / "stream-error.json"
    agent = DefaultAgent(
        model=LlamaCppModel(model_name="local", response_streaming="off"),
        env=LocalEnvironment(),
        system_template="System",
        instance_template="{{task}}",
        output_path=output_path,
    )

    with pytest.raises(ModelStreamError):
        agent.run("Inspect the stream")

    trajectory = json.loads(output_path.read_text())
    extra = trajectory["messages"][-1]["extra"]
    assert extra["exit_status"] == "ModelStreamError"
    assert extra["model_stream"]["payload_fragment"] == "{not json}"


def test_nemotron_empty_and_unknown_calls_have_specific_recovery_feedback():
    protocol = NemotronToolProtocol()
    for output, expected in [
        ("<TOOLCALL>[]</TOOLCALL>", "tool-call list is empty"),
        ("<TOOLCALL>[Nemotron(command=\"pwd\") ]</TOOLCALL>", "Unknown tool 'Nemotron'"),
        ("before <TOOLCALL>[bash(command=\"pwd\") ]</TOOLCALL>", "only the tool-call block"),
    ]:
        with pytest.raises(FormatError) as exc_info:
            protocol.parse_response(output, [], format_error_template="{{ error }}", finish_reason="stop")
        message = exc_info.value.messages[0]["content"]
        assert expected in message
        assert "Available tools:" in message
        assert "- read_text" in message


def test_nemotron_recovers_from_empty_call_then_completes_repository_sequence(monkeypatch):
    responses = [
        Response([{"choices": [{"delta": {"content": "<TOOLCALL>[]</TOOLCALL>"}}]}]),
        Response([{"choices": [{"delta": {"content": '<TOOLCALL>[bash(command="git ls-files | head -200")]</TOOLCALL>'}}]}]),
        Response([{"choices": [{"delta": {"content": '<TOOLCALL>[read_text(path="README.md", start_line=1, end_line=20)]</TOOLCALL>'}}]}]),
        Response([{"choices": [{"delta": {"content": "The blueprint is ready."}}]}]),
    ]
    monkeypatch.setattr("minisweagent.models.llama_cpp_model.requests.post", lambda *args, **kwargs: responses.pop(0))
    model = LlamaCppModel(model_name="nemotron", tool_protocol="nemotron", response_streaming="off")
    task = {"role": "user", "content": "Inspect the repository and create a blueprint."}
    with pytest.raises(FormatError) as exc_info:
        model.query([task])
    recovery = exc_info.value.messages[0]
    discovery = model.query([task, recovery])
    discovery_observation = model.format_observation_messages(
        discovery, [{"output": "README.md\nsrc/", "returncode": 0, "exception_info": None}]
    )
    read = model.query([task, recovery, discovery, *discovery_observation])
    read_observation = model.format_observation_messages(
        read, [{"output": "# Project", "returncode": 0, "exception_info": None}]
    )
    final = model.query([task, recovery, discovery, *discovery_observation, read, *read_observation])

    assert recovery["extra"]["interrupt_type"] == "FormatError"
    assert discovery["extra"]["actions"][0]["command"] == "git ls-files | head -200"
    assert read["extra"]["actions"][0]["path"] == "README.md"
    assert final["extra"]["final_text"] == "The blueprint is ready."
