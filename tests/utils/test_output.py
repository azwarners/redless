from minisweagent.utils.output import shape_output


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
