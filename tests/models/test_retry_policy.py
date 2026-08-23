import logging

import pytest

from minisweagent.models.utils.retry import PreRequestError, make_request_timeout, retry


def test_request_timeout_separates_connection_and_model_read_budgets():
    timeout = make_request_timeout(connect_timeout_seconds=7, model_timeout_seconds=19)
    assert timeout.connect == 7
    assert timeout.read == 19
    assert timeout.write is None
    assert timeout.pool is None


def test_zero_model_timeout_leaves_generation_unbounded():
    timeout = make_request_timeout(connect_timeout_seconds=7, model_timeout_seconds=0)
    assert timeout.connect == 7
    assert timeout.read is None
    assert timeout.write is None
    assert timeout.pool is None


def test_ambiguous_connection_error_is_not_retried_even_when_attempts_are_configured():
    attempts = 0

    def run():
        nonlocal attempts
        for attempt in retry(
            logger=logging.getLogger("test"),
            abort_exceptions=[],
            retry_exceptions=(PreRequestError,),
            max_attempts=3,
        ):
            with attempt:
                attempts += 1
                raise ConnectionError("request may have been sent")

    with pytest.raises(ConnectionError):
        run()
    assert attempts == 1


def test_only_explicit_pre_request_error_is_retryable():
    attempts = 0

    def run():
        nonlocal attempts
        for attempt in retry(
            logger=logging.getLogger("test"),
            abort_exceptions=[],
            retry_exceptions=(PreRequestError,),
            max_attempts=1,
        ):
            with attempt:
                attempts += 1
                raise PreRequestError("connection failed before send")

    with pytest.raises(PreRequestError):
        run()
    assert attempts == 1
