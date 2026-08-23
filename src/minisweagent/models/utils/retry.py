"""Retry utility for model queries."""

import logging
import os

import httpx
from tenacity import (
    Retrying,
    before_sleep_log,
    retry_if_exception_type,
    retry_if_not_exception_type,
    stop_after_attempt,
    wait_exponential,
)


class PreRequestError(ConnectionError):
    """An error proven to occur before request bytes were sent."""


def make_request_timeout(connect_timeout_seconds: int, model_timeout_seconds: int) -> httpx.Timeout:
    """Build a timeout with a connection-only deadline and an optional read deadline.

    ``model_timeout_seconds=0`` deliberately becomes ``read=None``.  Never pass a
    literal zero to httpx: it means an immediate timeout rather than an unlimited
    model prefill/generation budget.
    """
    return httpx.Timeout(
        timeout=None,
        connect=connect_timeout_seconds,
        read=model_timeout_seconds or None,
        write=None,
        pool=None,
    )


def retry(
    *,
    logger: logging.Logger,
    abort_exceptions: list[type[Exception]],
    retry_exceptions: tuple[type[Exception], ...] | None = None,
    max_attempts: int | None = None,
) -> Retrying:
    """Thin wrapper around tenacity.Retrying to make use of global config etc.

    Args:
        logger: Logger to use for reporting retries
        abort_exceptions: Exceptions to abort on.

    Returns:
        A tenacity.Retrying object.
    """
    retry_policy = (
        retry_if_exception_type(retry_exceptions)
        if retry_exceptions
        else retry_if_not_exception_type(tuple(abort_exceptions))
    )
    return Retrying(
        reraise=True,
        stop=stop_after_attempt(max_attempts or int(os.getenv("MSWEA_MODEL_RETRY_STOP_AFTER_ATTEMPT", "10"))),
        wait=wait_exponential(multiplier=1, min=4, max=60),
        before_sleep=before_sleep_log(logger, logging.WARNING),
        retry=retry_policy,
    )
