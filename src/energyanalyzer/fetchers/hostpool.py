"""Run per-host work concurrently without ever hitting one host in parallel.

A refresh spends nearly all of its wall clock waiting on other people's servers.
In the 2026-07-26 run the 92 discovered EFLs took 3m02s, and roughly 2s of each
of those was :func:`rep_discovery._respect_rate_limit` sleeping -- not transfer
time. Thirteen hosts were made to queue behind one another for no reason.

The rule encoded here is the one politeness actually cares about: **serialize
per host, parallelize across hosts**. Every host gets its own FIFO queue, run
start to finish by a single worker, so an individual REP sees exactly the request
pattern it sees today -- one request at a time, the same throttle in between.
What changes is only that Gexa's queue no longer waits for Green Mountain's.

Results come back in the caller's input order regardless of completion order, so
summaries and manifests stay byte-for-byte deterministic.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional, Sequence, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Concurrent hosts. Deliberately small: each worker may drive a headless
# Chromium (HTML EFL viewers, per-REP renders) and this runs on a WSL box with
# roughly 7 GB to spend, so the ceiling is memory, not politeness -- the per-host
# queues already guarantee no site sees more than one request at a time.
DEFAULT_MAX_WORKERS = 4


class HostResult:
    """Outcome of one item: exactly one of ``value`` / ``error`` is set."""

    __slots__ = ("item", "value", "error")

    def __init__(self, item, value=None, error: Optional[BaseException] = None) -> None:
        self.item = item
        self.value = value
        self.error = error

    @property
    def ok(self) -> bool:
        return self.error is None


def run_per_host(
    items: Sequence[T],
    host_of: Callable[[T], str],
    work: Callable[[T], object],
    max_workers: int = DEFAULT_MAX_WORKERS,
    on_done: Optional[Callable[[int, int, T], None]] = None,
) -> list[HostResult]:
    """Apply ``work`` to every item, one host at a time but many hosts at once.

    ``host_of(item)`` buckets the work; items sharing a bucket run in their
    original relative order on a single thread, so a caller's per-host state (a
    rate-limit timestamp, a "this host blocked us" breaker) behaves exactly as it
    does in a serial loop and needs no locking of its own.

    ``on_done(done, total, item)`` is invoked once per item as it finishes, under
    a lock, for progress reporting. Because hosts finish out of order, ``done``
    counts completions rather than position.

    An exception from ``work`` is captured on that item's :class:`HostResult` and
    does not disturb the rest of its queue or any other host -- these are network
    calls against a few hundred third-party servers, and one bad URL must never
    abort the batch.

    Returns one :class:`HostResult` per input item, in input order.
    """
    total = len(items)
    results: list[Optional[HostResult]] = [None] * total
    if total == 0:
        return []

    # Bucket by host, remembering each item's slot so the output can be
    # reassembled in input order.
    queues: dict[str, list[int]] = {}
    for index, item in enumerate(items):
        queues.setdefault(host_of(item), []).append(index)

    progress_lock = threading.Lock()
    done = 0

    def _run_queue(indices: list[int]) -> None:
        nonlocal done
        for index in indices:
            item = items[index]
            try:
                results[index] = HostResult(item, value=work(item))
            except BaseException as exc:  # noqa: BLE001 -- recorded, never fatal
                results[index] = HostResult(item, error=exc)
            if on_done is not None:
                with progress_lock:
                    done += 1
                    try:
                        on_done(done, total, item)
                    except Exception:  # noqa: BLE001 -- a progress bar must not fail a refresh
                        logger.debug("progress callback raised", exc_info=True)

    workers = max(1, min(max_workers, len(queues)))
    if workers == 1:
        # One host (or concurrency disabled): stay on the calling thread so the
        # serial path keeps its exact behaviour, tracebacks included.
        for indices in queues.values():
            _run_queue(indices)
    else:
        logger.info(
            "running %d item(s) across %d host(s), %d at a time",
            total, len(queues), workers,
        )
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(_run_queue, list(queues.values())))

    return [r for r in results if r is not None]
