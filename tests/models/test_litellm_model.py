from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from minisweagent.exceptions import FormatError
from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig
from minisweagent.models.utils.actions_toolcall import TOOL_DEFINITIONS


class TestLitellmModelConfig:
    def test_default_format_error_template(self):
        assert LitellmModelConfig(model_name="test").format_error_template == "{{ error }}"

    def test_rejects_obsolete_or_unknown_slow_local_settings(self):
        with pytest.raises(ValidationError):
            LitellmModelConfig(model_name="test", retry_attempts=1)
        with pytest.raises(ValidationError):
            LitellmModelConfig(model_name="test", output={"max_chars": 1})

    def test_model_timeout_and_retry_config_cannot_be_overridden_by_model_kwargs(self):
        request_kwargs = LitellmModel(
            model_name="test",
            max_retries=0,
            model_kwargs={"timeout": 1, "max_retries": 3},
        )._request_kwargs()
        assert request_kwargs["max_retries"] == 0
        assert request_kwargs["timeout"].read is None


def _mock_litellm_response(tool_calls):
    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.tool_calls = tool_calls
    mock_response.choices[0].message.model_dump.return_value = {"role": "assistant", "content": None}
    mock_response.model_dump.return_value = {}
    return mock_response


class TestLitellmModel:
    @patch("minisweagent.models.litellm_model.litellm.completion")
    @patch("minisweagent.models.litellm_model.litellm.cost_calculator.completion_cost")
    def test_query_includes_bash_tool(self, mock_cost, mock_completion):
        tool_call = MagicMock()
        tool_call.function.name = "bash"
        tool_call.function.arguments = '{"command": "echo test"}'
        tool_call.id = "call_1"
        mock_completion.return_value = _mock_litellm_response([tool_call])
        mock_cost.return_value = 0.001

        model = LitellmModel(model_name="gpt-4")
        model.query([{"role": "user", "content": "test"}])

        mock_completion.assert_called_once()
        assert mock_completion.call_args.kwargs["tools"] == TOOL_DEFINITIONS
        assert mock_completion.call_args.kwargs["max_retries"] == 0
        timeout = mock_completion.call_args.kwargs["timeout"]
        assert timeout.connect == 30
        assert timeout.read is None
        assert timeout.write is None
        assert timeout.pool is None

    @patch("minisweagent.models.litellm_model.litellm.completion")
    def test_ambiguous_model_failure_is_not_replayed(self, mock_completion):
        mock_completion.side_effect = ConnectionError("response may have been lost")
        with pytest.raises(ConnectionError):
            LitellmModel(model_name="gpt-4").query([{"role": "user", "content": "test"}])
        mock_completion.assert_called_once()

    @patch("minisweagent.models.litellm_model.litellm.completion")
    @patch("minisweagent.models.litellm_model.litellm.cost_calculator.completion_cost")
    def test_tool_output_budget_does_not_truncate_model_content(self, mock_cost, mock_completion):
        tool_call = MagicMock()
        tool_call.function.name = "bash"
        tool_call.function.arguments = '{"command": "true"}'
        tool_call.id = "call_1"
        response = _mock_litellm_response([tool_call])
        response.choices[0].message.model_dump.return_value = {"role": "assistant", "content": "x" * 6001}
        mock_completion.return_value = response
        mock_cost.return_value = 0.001
        assert len(LitellmModel(model_name="gpt-4").query([{"role": "user", "content": "test"}])["content"]) == 6001

    @patch("minisweagent.models.litellm_model.litellm.completion")
    @patch("minisweagent.models.litellm_model.litellm.cost_calculator.completion_cost")
    def test_parse_actions_valid_tool_call(self, mock_cost, mock_completion):
        tool_call = MagicMock()
        tool_call.function.name = "bash"
        tool_call.function.arguments = '{"command": "ls -la"}'
        tool_call.id = "call_abc"
        mock_completion.return_value = _mock_litellm_response([tool_call])
        mock_cost.return_value = 0.001

        model = LitellmModel(model_name="gpt-4")
        result = model.query([{"role": "user", "content": "list files"}])
        assert result["extra"]["actions"] == [{"command": "ls -la", "tool_call_id": "call_abc"}]

    @patch("minisweagent.models.litellm_model.litellm.completion")
    @patch("minisweagent.models.litellm_model.litellm.cost_calculator.completion_cost")
    def test_parse_actions_no_tool_calls_raises(self, mock_cost, mock_completion):
        mock_completion.return_value = _mock_litellm_response(None)
        mock_cost.return_value = 0.001

        model = LitellmModel(model_name="gpt-4")
        with pytest.raises(FormatError):
            model.query([{"role": "user", "content": "test"}])

    @patch("minisweagent.models.litellm_model.litellm.completion")
    @patch("minisweagent.models.litellm_model.litellm.cost_calculator.completion_cost")
    def test_finish_reason_threaded_into_format_error_template(self, mock_cost, mock_completion):
        """The response finish_reason is exposed to format_error_template via template_kwargs, so a
        config can report a max_tokens truncation instead of the misleading "no tool call" error."""
        response = _mock_litellm_response(None)
        response.choices[0].finish_reason = "length"
        mock_completion.return_value = response
        mock_cost.return_value = 0.001

        model = LitellmModel(
            model_name="gpt-4",
            format_error_template="{% if finish_reason == 'length' %}cut off{% else %}{{ error }}{% endif %}",
        )
        with pytest.raises(FormatError) as exc:
            model.query([{"role": "user", "content": "test"}])
        assert exc.value.messages[0]["content"] == "cut off"

    def test_format_observation_messages(self):
        model = LitellmModel(model_name="gpt-4", observation_template="{{ output.output }}")
        message = {"extra": {"actions": [{"command": "echo test", "tool_call_id": "call_1"}]}}
        outputs = [{"output": "test output", "returncode": 0}]
        result = model.format_observation_messages(message, outputs)
        assert len(result) == 1
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "call_1"
        assert result[0]["content"] == "test output"

    def test_tool_output_budget_bounds_observations_and_retains_raw_output(self):
        model = LitellmModel(
            model_name="gpt-4",
            observation_template="{{ output.output }}",
            tool_output={"max_chars": 30, "head_chars": 8, "tail_chars": 12, "error_tail_chars": 20},
        )
        result = model.format_observation_messages(
            {"extra": {"actions": [{"command": "echo test", "tool_call_id": "call_1"}]}},
            [{"output": "start-" + "x" * 50 + "-end", "returncode": 0}],
        )
        assert len(result[0]["content"]) <= 30
        assert result[0]["extra"]["raw_output"].endswith("-end")
        assert result[0]["extra"]["truncated"] is True

    def test_format_observation_messages_no_actions(self):
        model = LitellmModel(model_name="gpt-4")
        result = model.format_observation_messages({"extra": {}}, [])
        assert result == []
