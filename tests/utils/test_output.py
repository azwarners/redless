from minisweagent.utils.output import shape_output, shape_outputs_for_turn


def test_shape_output_preserves_short_output_and_metadata():
    result = shape_output({"output": "ok\n", "returncode": 0}, max_chars=10, head_chars=3, tail_chars=3)
    assert result["output"] == "ok\n"
    assert result["truncated"] is False
    assert result["original_chars"] == 3


def test_shape_output_prioritizes_error_tail_and_preserves_raw_source():
    raw = "head-" + ("x" * 20) + "-failure-tail"
    result = shape_output(
        {"output": raw, "returncode": 1, "exception_info": "failed"},
        max_chars=20,
        head_chars=4,
        tail_chars=5,
        error_tail_chars=10,
    )
    assert result["truncated"] is True
    assert result["original_chars"] == len(raw)
    assert "failure-tail"[-10:] in result["output"]
    assert result["exception_info"] == "failed"


def test_turn_budget_keeps_every_observation_and_prioritizes_failures():
    outputs = [
        {"output": "success-" * 100, "returncode": 0},
        {"output": "compiler head\n" + "noise\n" * 100 + "compiler failure tail", "returncode": 1},
        {"output": "success-" * 100, "returncode": 0},
        {"output": "success-" * 100, "returncode": 0},
    ]
    result = shape_outputs_for_turn(
        outputs,
        {
            "max_chars": 6000,
            "head_chars": 1200,
            "tail_chars": 3600,
            "error_tail_chars": 4800,
            "max_turn_chars": 512,
            "minimum_chars_per_observation": 64,
        },
    )
    assert len(result) == 4
    assert all(item["displayed_chars"] > 0 for item in result)
    assert sum(item["displayed_chars"] for item in result) <= 512
    assert "compiler failure tail" in result[1]["output"]
    assert all(item["turn_budget"] == 512 for item in result)


def test_turn_budget_zero_preserves_current_per_observation_behavior():
    output = {"output": "x" * 20, "returncode": 0}
    result = shape_outputs_for_turn(
        [output],
        {"max_chars": 10, "head_chars": 3, "tail_chars": 3, "max_turn_chars": 0},
    )[0]
    assert result["output"] == shape_output(output, max_chars=10, head_chars=3, tail_chars=3)["output"]
    assert result["turn_budget"] == 0


def test_turn_budget_validation_requires_minimum_observation_size():
    import pytest

    with pytest.raises(ValueError, match="too small"):
        shape_outputs_for_turn(
            [{"output": "a"}, {"output": "b"}],
            {"max_chars": 6000, "max_turn_chars": 100, "minimum_chars_per_observation": 64},
        )
