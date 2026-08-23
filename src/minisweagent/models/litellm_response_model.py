import time
from collections.abc import Callable

import litellm

from minisweagent.exceptions import FormatError
from minisweagent.models import GLOBAL_MODEL_STATS
from minisweagent.models.litellm_model import LitellmModel, LitellmModelConfig
from minisweagent.models.utils.actions_toolcall_response import (
    TOOL_DEFINITIONS_RESPONSE_API,
    finish_reason_from_responses_api,
    format_toolcall_observation_messages,
    parse_toolcall_actions_response,
)


class LitellmResponseModelConfig(LitellmModelConfig):
    pass


class LitellmResponseModel(LitellmModel):
    def __init__(self, *, config_class: Callable = LitellmResponseModelConfig, **kwargs):
        super().__init__(config_class=config_class, **kwargs)

    def _prepare_messages_for_api(self, messages: list[dict]) -> list[dict]:
        """Flatten response objects into their output items for stateless API calls."""
        result = []
        for msg in messages:
            if msg.get("object") == "response":
                for item in msg.get("output", []):
                    result.append({k: v for k, v in item.items() if k != "extra"})
            else:
                result.append({k: v for k, v in msg.items() if k != "extra"})
        return result

    def _query(self, messages: list[dict[str, str]], **kwargs):
        try:
            return litellm.responses(
                model=self.config.model_name,
                input=messages,
                tools=TOOL_DEFINITIONS_RESPONSE_API,
                **self._request_kwargs(**kwargs),
            )
        except litellm.exceptions.AuthenticationError as e:
            e.message += " You can permanently set your API key with `mini-extra config set KEY VALUE`."
            raise e

    def query(self, messages: list[dict[str, str]], **kwargs) -> dict:
        request_started = time.monotonic()
        response = self._query(self._prepare_messages_for_api(messages), **kwargs)
        cost_output = self._calculate_cost(response) | {"request_elapsed_seconds": time.monotonic() - request_started}
        GLOBAL_MODEL_STATS.add(cost_output["cost"])
        try:
            final_text = self._final_text(response)
            actions = [] if final_text else self._parse_actions(response)
        except FormatError as e:
            e.messages[0]["extra"].update(cost_output)
            # hasattr guard: litellm.responses() returns a pydantic object, but tests
            # may inject a plain dict; dict(response) is the correct fallback in that case.
            # Inner try: if serialization itself fails, repr() guarantees the key is always set.
            try:
                e.messages[0]["extra"]["response"] = (
                    response.model_dump(mode="json") if hasattr(response, "model_dump") else dict(response)
                )
            except Exception:
                e.messages[0]["extra"]["response"] = repr(response)
            raise
        message = response.model_dump() if hasattr(response, "model_dump") else dict(response)
        message["extra"] = {
            "actions": actions,
            "is_final": bool(final_text),
            "final_text": final_text,
            **cost_output,
            "timestamp": time.time(),
        }
        return message

    @staticmethod
    def _final_text(response) -> str:
        output = response.get("output", []) if isinstance(response, dict) else getattr(response, "output", [])
        if any((item.get("type") if isinstance(item, dict) else getattr(item, "type", None)) == "function_call" for item in output):
            return ""
        text = []
        for item in output:
            content = item.get("content", []) if isinstance(item, dict) else getattr(item, "content", [])
            if isinstance(content, str):
                text.append(content)
            else:
                text.extend(
                    part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "") for part in content
                )
        return "".join(text).strip()

    def _parse_actions(self, response) -> list[dict]:
        return parse_toolcall_actions_response(
            getattr(response, "output", []),
            format_error_template=self.config.format_error_template,
            template_kwargs={"finish_reason": finish_reason_from_responses_api(response)},
        )

    def format_observation_messages(
        self, message: dict, outputs: list[dict], template_vars: dict | None = None
    ) -> list[dict]:
        """Format execution outputs into tool result messages."""
        actions = message.get("extra", {}).get("actions", [])
        return format_toolcall_observation_messages(
            actions=actions,
            outputs=outputs,
            observation_template=self.config.observation_template,
            template_vars=template_vars,
            multimodal_regex=self.config.multimodal_regex,
            output_config=self.config.tool_output,
        )
