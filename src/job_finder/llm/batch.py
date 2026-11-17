"""Scoring through the Message Batches API, at half the price. Used by scheduled runs.

One batch carries the chosen jobs of every profile, each request the same one a direct
call sends (see `scorer.request_params`). The run waits for the batch up to a limit;
past it, the batch is canceled and what did not come back is returned to be scored
directly. Nothing is stored about the batch besides its id in the log: if the app
restarts mid-wait, the jobs stay pending and the next run scores them again.
"""

import logging
import time
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import anthropic
from sqlalchemy import Engine

from job_finder.domain import FitResult
from job_finder.llm.client import (
    CostCapReached,
    InvalidResponse,
    LLMClient,
    is_fatal,
    json_format,
)
from job_finder.llm.scorer import OPERATION, Item, Prepared, fail, request_params, store
from job_finder.pipeline.run import Progress

log = logging.getLogger(__name__)

POLL_SECONDS = 30.0
# After a cancel, how long to wait for the batch to end before giving up on it.
CANCEL_WAIT_SECONDS = 300.0


def custom_id(match_id: int) -> str:
    return f"match-{match_id}"


def score_batch(
    engine: Engine,
    client: LLMClient,
    prepared: Sequence[Prepared],
    *,
    model: str,
    language: str,
    max_wait_s: float,
    run_id: int | None = None,
    progress: Progress | None = None,
    poll_s: float = POLL_SECONDS,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict[int, list[Item]]:
    """Scores the items of every profile in one batch, updating each summary.

    Returns, by profile id, the items to score directly: those the batch did not
    answer (errored, expired, canceled, missing) or all of them if it could not be
    created. A refusal or invalid output uses an attempt, as with a direct call. The
    cost cap is checked before submitting; account errors (key, balance) are raised.
    """
    pending = {custom_id(item.match_id): (p, item) for p in prepared for item in p.items}
    if not pending:
        return {}
    try:
        client.check_cap()
    except CostCapReached as e:
        for p in prepared:
            if p.items:
                p.summary.interrupted = str(e)
        return {}

    fmt = json_format(FitResult)
    requests = []
    for cid, (p, item) in pending.items():
        params = request_params(p.profile, item.job, language=language, examples=item.examples)
        params["output_config"] = {**params["output_config"], "format": fmt}
        requests.append({"custom_id": cid, "params": {"model": model, **params}})

    batches = client.anthropic.messages.batches
    try:
        batch = batches.create(requests=requests)
    except anthropic.APIError as e:
        if is_fatal(e):
            raise
        log.warning("could not create the scoring batch; scoring directly: %s", e)
        return _by_profile(pending.values())
    log.info("scoring batch %s: %d requests", batch.id, len(requests))
    if progress is not None:
        progress("progress.batch", {"n": len(requests)})

    if _wait(batches, batch.id, max_wait_s, poll_s, sleep, clock):
        try:
            for result in batches.results(batch.id):
                r = result.result
                if r.type != "succeeded" or result.custom_id not in pending:
                    if r.type != "succeeded":
                        log.info("batch %s: %s %s", batch.id, result.custom_id, r.type)
                    continue
                p, item = pending.pop(result.custom_id)
                try:
                    fit = client.from_batch(
                        FitResult,
                        r.message,
                        operation=OPERATION,
                        model=model,
                        profile_id=p.profile_id,
                        run_id=run_id,
                    )
                except InvalidResponse as e:
                    fail(engine, p.summary, item, e)
                    continue
                store(engine, item.match_id, fit, model)
                p.summary.scored += 1
        except anthropic.APIError as e:
            if is_fatal(e):
                raise
            log.warning("could not read the results of batch %s: %s", batch.id, e)
    if pending:
        log.info("batch %s: %d jobs left to score directly", batch.id, len(pending))
    return _by_profile(pending.values())


def _wait(
    batches: Any,
    batch_id: str,
    max_wait_s: float,
    poll_s: float,
    sleep: Callable[[float], None],
    clock: Callable[[], float],
) -> bool:
    """Polls until the batch ends; cancels it past `max_wait_s`. False if it never
    ended, even after the cancel."""
    deadline = clock() + max_wait_s
    canceled_at: float | None = None
    while True:
        try:
            if batches.retrieve(batch_id).processing_status == "ended":
                return True
        except anthropic.APIError as e:
            if is_fatal(e):
                raise
            log.warning("could not check batch %s: %s", batch_id, e)
        now = clock()
        if canceled_at is None and now >= deadline:
            log.warning("batch %s not done after %.0f s; canceling", batch_id, max_wait_s)
            canceled_at = now
            try:
                batches.cancel(batch_id)
            except anthropic.APIError as e:  # it may have just ended; the poll tells
                if is_fatal(e):
                    raise
                log.warning("could not cancel batch %s: %s", batch_id, e)
        elif canceled_at is not None and now >= canceled_at + CANCEL_WAIT_SECONDS:
            log.warning("batch %s did not end after the cancel; giving up on it", batch_id)
            return False
        sleep(poll_s)


def _by_profile(entries: Iterable[tuple[Prepared, Item]]) -> dict[int, list[Item]]:
    out: dict[int, list[Item]] = {}
    for p, item in entries:
        out.setdefault(p.profile_id, []).append(item)
    return out
