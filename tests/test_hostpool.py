"""Tests for the per-host work pool that parallelizes a refresh.

The point of `hostpool` is a single guarantee: many hosts at once, never one
host twice at once. If that inverts, a refresh quietly starts hammering
individual REP sites -- the one thing this project must not do -- so the
concurrency contract is asserted directly rather than inferred from timings.
"""

from __future__ import annotations

import threading
import time

from energyanalyzer.fetchers import hostpool


def _tracker():
    """Records peak simultaneous workers, overall and per host."""
    lock = threading.Lock()
    live: dict[str, int] = {}
    peak: dict[str, int] = {}
    peak_total = [0]

    def enter(host: str) -> None:
        with lock:
            live[host] = live.get(host, 0) + 1
            peak[host] = max(peak.get(host, 0), live[host])
            peak_total[0] = max(peak_total[0], sum(live.values()))

    def leave(host: str) -> None:
        with lock:
            live[host] -= 1

    return enter, leave, peak, peak_total


def test_one_host_is_never_worked_twice_at_once():
    """The politeness contract. Six jobs on one host must stay strictly serial
    no matter how many workers are offered."""
    enter, leave, peak, _total = _tracker()
    items = [("acme.example", i) for i in range(6)]

    def work(item):
        host, _n = item
        enter(host)
        time.sleep(0.01)
        leave(host)
        return item

    hostpool.run_per_host(items, host_of=lambda it: it[0], work=work, max_workers=4)

    assert peak["acme.example"] == 1


def test_different_hosts_run_concurrently():
    """...and the flip side: separate hosts must actually overlap, otherwise
    this is just a slower serial loop."""
    barrier = threading.Barrier(3, timeout=10)
    items = [(f"h{i}.example", i) for i in range(3)]

    def work(item):
        # Only passes if all three are in flight simultaneously.
        barrier.wait()
        return item

    results = hostpool.run_per_host(
        items, host_of=lambda it: it[0], work=work, max_workers=3
    )

    assert [r.value for r in results] == items


def test_results_come_back_in_input_order():
    """Summaries and manifests are built from this list, so completion order
    must not leak into it -- a fast host finishing first cannot reorder a
    refresh's output."""
    items = [("slow.example", "a"), ("fast.example", "b"), ("slow.example", "c")]

    def work(item):
        host, value = item
        if host == "slow.example":
            time.sleep(0.05)
        return value.upper()

    results = hostpool.run_per_host(
        items, host_of=lambda it: it[0], work=work, max_workers=4
    )

    assert [r.value for r in results] == ["A", "B", "C"]
    assert [r.item for r in results] == items


def test_items_on_one_host_keep_their_relative_order():
    """A host's queue is FIFO: callers rely on it for per-host state such as the
    rate-limit timestamp and the "this host blocked us" breaker."""
    seen: list[int] = []
    items = [("one.example", i) for i in range(5)]

    hostpool.run_per_host(
        items,
        host_of=lambda it: it[0],
        work=lambda it: seen.append(it[1]),
        max_workers=4,
    )

    assert seen == [0, 1, 2, 3, 4]


def test_a_failing_item_is_captured_and_the_rest_still_run():
    """One bad URL among a few hundred must not abort the batch."""
    items = [("a.example", 1), ("a.example", 2), ("b.example", 3)]

    def work(item):
        if item[1] == 1:
            raise ValueError("boom")
        return item[1]

    results = hostpool.run_per_host(
        items, host_of=lambda it: it[0], work=work, max_workers=2
    )

    assert not results[0].ok
    assert isinstance(results[0].error, ValueError)
    assert [r.value for r in results[1:]] == [2, 3]
    assert all(r.ok for r in results[1:])


def test_progress_counts_every_item_exactly_once():
    events: list[tuple[int, int]] = []
    items = [(f"h{i % 3}.example", i) for i in range(9)]

    hostpool.run_per_host(
        items,
        host_of=lambda it: it[0],
        work=lambda it: it,
        max_workers=3,
        on_done=lambda done, total, _item: events.append((done, total)),
    )

    assert sorted(events) == [(i, 9) for i in range(1, 10)]


def test_a_raising_progress_callback_does_not_fail_the_batch():
    """A Streamlit progress bar can blow up mid-refresh (a stale widget, a
    cleared placeholder); losing the download because of it would be absurd."""

    def boom(done, total, item):
        raise RuntimeError("progress bar went away")

    results = hostpool.run_per_host(
        [("a.example", 1)],
        host_of=lambda it: it[0],
        work=lambda it: it[1],
        max_workers=2,
        on_done=boom,
    )

    assert [r.value for r in results] == [1]


def test_empty_input_is_a_no_op():
    assert hostpool.run_per_host([], host_of=lambda it: it, work=lambda it: it) == []


def test_single_worker_stays_on_the_calling_thread():
    """max_workers=1 is the documented escape hatch back to serial behavior;
    it must not spin up a pool (tracebacks and profiling stay intact)."""
    caller = threading.get_ident()
    threads: list[int] = []

    hostpool.run_per_host(
        [("a.example", 1), ("b.example", 2)],
        host_of=lambda it: it[0],
        work=lambda it: threads.append(threading.get_ident()),
        max_workers=1,
    )

    assert threads == [caller, caller]
