# -*- coding: utf-8 -*-
"""Run independent scrape passes concurrently.

Each pass is a self-contained query (a day x language slice), so two passes can
run at the same time on two different egress IPs: the collector picks the next
proxy entry per collector, which roughly halves the per-IP request rate and
keeps Meta from soft-blocking.

The caller owns ``absorb(result)`` (it folds a ScrapeResult into the job
outcome). ``on_pass_done(idx, keep)`` is called after every finished pass so the
caller can persist a resumable checkpoint. Returning ``False`` from ``absorb``
(or raising in a runner) aborts the remaining passes.
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed


def run_passes(
    passes,
    *,
    runner,
    absorb,
    start_pass: int = 1,
    done=None,
    workers: int = 2,
    should_stop=None,
    progress=None,
    on_pass_done=None,
):
    """Run passes[start_pass-1:] (minus ``done``) with ``workers`` threads.

    Returns ``{"aborted": bool, "ran": [idx, ...], "done": [idx, ...]}``.
    """
    done_set = set(done or ())
    log = progress or (lambda _m: None)
    stop = should_stop or (lambda: False)

    abort = threading.Event()
    lock = threading.Lock()
    ran = []
    finished = sorted(done_set)

    todo = [(idx, p) for idx, p in enumerate(passes, 1)
            if idx >= start_pass and idx not in done_set]
    if not todo:
        return {"aborted": False, "ran": [], "done": finished}

    def _one(idx, p):
        if abort.is_set() or stop():
            return idx, None, None
        try:
            return idx, runner(idx, p), None
        except Exception as exc:  # noqa: BLE001
            return idx, None, exc

    with ThreadPoolExecutor(max_workers=max(1, int(workers))) as pool:
        futures = [pool.submit(_one, idx, p) for idx, p in todo]
        for fut in as_completed(futures):
            idx, result, exc = fut.result()
            with lock:
                ran.append(idx)
                keep = False
                if exc is not None:
                    log("pass %s failed: %s" % (idx, exc))
                elif result is not None:
                    keep = bool(absorb(result))
                if on_pass_done:
                    try:
                        on_pass_done(idx, keep)
                    except Exception as err:  # noqa: BLE001
                        log("pass %s checkpoint failed: %s" % (idx, err))
                finished = sorted(set(finished) | {idx})
                if exc is not None or (result is not None and not keep) or stop():
                    abort.set()

    return {"aborted": abort.is_set(), "ran": ran, "done": finished}
