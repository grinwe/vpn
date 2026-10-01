#!/usr/bin/env python
"""RQ queue diagnostics and cleanup.

Run inside the worker container:

    docker compose exec -T worker python /app/scripts/queue_cleanup.py status
    docker compose exec -T worker python /app/scripts/queue_cleanup.py fix

Commands:
    status  — show queue stats, scheduled/failed counts, tick health
    fix     — flush zombie scheduled/failed jobs, reschedule all 8 ticks
"""
import sys

from app.queue import get_queue, TICK_IDS

try:
    from app.queue import schedule_tick
except ImportError:
    schedule_tick = None

from rq.registry import (
    FailedJobRegistry,
    ScheduledJobRegistry,
    StartedJobRegistry,
)
from rq.job import Job
from collections import Counter


def cmd_status():
    q = get_queue()
    if q is None:
        print("ERROR: queue unavailable (REDIS_URL / QUEUE_BACKEND not set?)")
        sys.exit(1)

    print(f"queue: {q.name}")
    print(f"  queued:    {q.count}")

    sr = ScheduledJobRegistry(queue=q)
    fr = FailedJobRegistry(queue=q)
    started = StartedJobRegistry(queue=q)

    sched_ids = sr.get_job_ids()
    print(f"  scheduled: {len(sched_ids)}")
    print(f"  started:   {started.count}")
    print(f"  failed:    {fr.count}")

    # breakdown of scheduled
    if sched_ids:
        funcs = Counter()
        for jid in sched_ids:
            try:
                j = Job.fetch(jid, connection=q.connection)
                funcs[j.func_name or jid] += 1
            except Exception:
                funcs["DEAD/unfetchable"] += 1
        print("\n  scheduled breakdown:")
        for fn, cnt in funcs.most_common():
            print(f"    {fn}: {cnt}")

    # tick health
    print("\n  tick health:")
    for func_name, tick_id in TICK_IDS.items():
        short = tick_id
        try:
            j = Job.fetch(tick_id, connection=q.connection)
            st = j.get_status(refresh=True)
            print(f"    {short:30s} {st}")
        except Exception:
            print(f"    {short:30s} MISSING")

    # workers
    from rq import Worker
    workers = Worker.all(queue=q)
    print(f"\n  workers: {len(workers)}")
    for w in workers:
        print(f"    {w.name}  state={w.get_state()}  heartbeat={w.last_heartbeat}")


def cmd_fix():
    q = get_queue()
    if q is None:
        print("ERROR: queue unavailable")
        sys.exit(1)

    # 1. Clean started registry (zombie reclaim)
    started = StartedJobRegistry(queue=q)
    try:
        started.cleanup()
        print("StartedJobRegistry.cleanup() done")
    except Exception as e:
        print(f"StartedJobRegistry.cleanup() failed: {e}")

    # 2. Flush all scheduled jobs
    sr = ScheduledJobRegistry(queue=q)
    sched_ids = sr.get_job_ids()
    killed_sched = 0
    for jid in sched_ids:
        try:
            j = Job.fetch(jid, connection=q.connection)
            j.delete()
        except Exception:
            sr.remove(jid)
        killed_sched += 1
    print(f"killed {killed_sched} scheduled jobs")

    # 3. Flush failed registry
    fr = FailedJobRegistry(queue=q)
    fids = fr.get_job_ids()
    for jid in fids:
        try:
            Job.fetch(jid, connection=q.connection).delete()
        except Exception:
            fr.remove(jid)
    print(f"killed {len(fids)} failed jobs")

    # 4. Flush non-provisioning from main queue
    job_ids = q.connection.lrange(q.key, 0, -1)
    killed_q = kept_q = 0
    for jid in job_ids:
        jid = jid.decode() if isinstance(jid, bytes) else jid
        try:
            j = Job.fetch(jid, connection=q.connection)
            if "run_provisioning_task" in (j.func_name or ""):
                kept_q += 1
                continue
        except Exception:
            pass
        q.remove(jid)
        try:
            Job.fetch(jid, connection=q.connection).delete()
        except Exception:
            pass
        killed_q += 1
    print(f"queue cleanup: killed={killed_q} kept={kept_q}")

    # 5. Reschedule ticks
    if schedule_tick is None:
        print("WARN: schedule_tick not available, skipping tick reschedule")
    else:
        print("rescheduling ticks:")
        for func_name, tick_id in TICK_IDS.items():
            try:
                r = schedule_tick(func_name, 10, tick_id)
                print(f"  {tick_id}: {r}")
            except Exception as e:
                print(f"  {tick_id}: FAILED ({e})")

    print("\ndone")


if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("status", "fix"):
        print(__doc__)
        sys.exit(1)

    if sys.argv[1] == "status":
        cmd_status()
    elif sys.argv[1] == "fix":
        cmd_fix()
