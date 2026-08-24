import json

from minisweagent.models.llama_cpp_model import LlamaCppModel
from minisweagent.models.llama_log import format_llama_server_stats, parse_llama_server_log

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
