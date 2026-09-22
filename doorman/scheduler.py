"""The clock that turns a planned temporary person into a Kindoo user.

A scheduled temporary person costs nothing while they wait: the plan lives in
Doorman's own database and no seat is held. This loop is what spends the seat,
a few minutes before their window opens (temps.LEAD), using **that manager's
own token** -- Kindoo attributes the invitation to whoever's token made it, so
a person arranged by one manager must be created by them, not by whoever
happened to be signed in.

Deliberately a plain asyncio task rather than cron or a job runner:

  * There is one always-on process already (systemd). A second scheduler
    process would need its own deployment, its own restart story and its own
    access to the token in the database, for one query a minute.
  * Nothing is held in memory, so nothing has to be rebuilt after a restart.
    Each pass asks the database the same question -- "whose window opens
    soon and has no Kindoo user yet?" -- and the answer survives a reboot,
    a redeploy and a week of downtime. There are no timers to re-arm on
    startup and no queue to drain: a row whose start time passed while the
    app was down is simply due, and goes in on the first pass.

The end of a window is Kindoo's job, not this loop's: temporary users carry a
native ExpiryDate and Kindoo removes them itself. Doorman only notices the
removal afterwards and closes its own record (see the reconcile step in the
web app).
"""
import asyncio
import logging

from . import store, temps

log = logging.getLogger("doorman.scheduler")

#: How often to look. Deliberately short. It is tempting to poll by the hour
#: instead, but the cost of a pass is one indexed read of a local SQLite file
#: -- Kindoo is only called when a row is actually due -- while the cost of an
#: hourly pass is that somebody scheduled for 9am can be left outside until
#: 10. A minute of granularity is nearly free and matches what a manager who
#: typed "9:00" expects.
EVERY = 60

_task = None


def run_due(now=None):
    """Create the Kindoo user for every visit whose window is about to open.

    Returns the number created. Safe to call at any time and from anywhere:
    activation is driven by each row's stored state, so running it twice in a
    row does nothing the second time.
    """
    now = now or temps.now_utc()
    rows = store.due_temp_visits(temps.to_utc_text(now + temps.LEAD),
                                 temps.to_utc_text(now))
    created = 0
    for row in rows:
        owner = row["owner"]
        k = temps.kindoo_for(owner)
        if k is None:
            # Their token is gone, so nothing can be created as them. Said
            # once per row rather than every minute: the row is marked failed
            # and shows up on their page with the reason.
            store.update_temp_visit(owner, row["id"], status="failed",
                                    note="no Kindoo token saved for this manager")
            log.warning("cannot create temp visit for %s: %s has no token",
                        row["email"], owner)
            continue
        problem = temps.activate(k, owner, row)
        if problem:
            log.warning("temp user %s for %s not created: %s",
                        row["email"], owner, problem)
        else:
            created += 1
    return created


async def _loop():
    while True:
        try:
            # SQLite and urllib are both blocking, and a slow Kindoo call would
            # otherwise stall every request being served. A thread keeps the
            # event loop free.
            created = await asyncio.to_thread(run_due)
            if created:
                log.info("scheduler created %d temporary user(s)", created)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad pass must not stop the clock -- the next tick tries again.
            log.exception("scheduler pass failed")
        await asyncio.sleep(EVERY)


def start():
    """Begin ticking. Called once, when the web app starts."""
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())
        log.info("scheduler started (every %ds, %d minutes ahead of start)",
                 EVERY, temps.LEAD.total_seconds() // 60)
    return _task


async def stop():
    """Stop ticking, and wait for a pass in flight to finish unwinding."""
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
    _task = None
