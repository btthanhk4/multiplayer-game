#!/usr/bin/env python3
"""Measure how fast the grader validates bots and plays ranked games, on a throwaway database.

On the server (inside a one-off container that can reach Docker, never against live data):
  docker run --rm --network none --group-add "$(stat -c %g /var/run/docker.sock)" \\
    -v /var/run/docker.sock:/var/run/docker.sock -w /app --entrypoint python ottv2-arena-api:local \\
    scripts/arena-bench.py --mode docker --slots 8 --teams 60
"""
import argparse
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from arena_backend.runner import Executor, Worker  # noqa: E402
from arena_backend.store import Store, now, uid  # noqa: E402

HUNTER = (ROOT / 'sdk/examples/hunter.py').read_text(encoding='utf-8')


def percentile(values, share):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * share))] if values else 0


def durations(store, kind):
    with store.connect() as db:
        rows = db.execute("SELECT finished-started d FROM jobs WHERE kind=? AND status='done' AND started IS NOT NULL",
                          (kind,)).fetchall()
    return [row['d'] for row in rows]


def wait_for(store, sql, label, timeout):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        with store.connect() as db:
            left = db.execute(sql).fetchone()[0]
        print(f'\r  {label}: {left} left   ', end='', flush=True)
        if not left:
            print()
            return time.monotonic() - started
        time.sleep(1)
    print()
    raise SystemExit(f'Timed out waiting for {label}')


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--mode', default='docker', choices=['docker', 'samples'])
    parser.add_argument('--slots', type=int, default=6)
    parser.add_argument('--teams', type=int, default=60)
    parser.add_argument('--image', default='ottv2-bot:1')
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args()
    if args.teams % 2:
        parser.error('--teams must be even')

    data = tempfile.mkdtemp(prefix='arena-bench-')
    store = Store(data)
    teams = []
    with store.connect(True) as db:
        for i in range(args.teams):
            user = uid()
            db.execute("INSERT INTO users(id,username,name,password,role,created) VALUES(?,?,?,'x','player',?)",
                       (user, f'bench{i}', f'Bench {i}', now()))
            teams.append(store.insert_team(db, f'Bench team {i}', user))
    executor = Executor(args.mode, args.image)
    executor.preflight()
    worker = Worker(store, executor, args.slots)
    thread = threading.Thread(target=worker.run_forever, daemon=True)
    thread.start()
    print(f'{args.mode} · {args.slots} slots · {args.teams} teams · data {data}')
    try:
        # 1. Everyone submits a different bot at once (the real-world deadline rush).
        started = time.monotonic()
        subs = []
        for i, team in enumerate(teams):
            source = HUNTER if args.mode == 'samples' else HUNTER + f'\n# unique bot {i}\n'
            subs.append(store.submit(team, None, f'bot {i}', source, limit=False))
        submitted = time.monotonic() - started
        took = wait_for(store, "SELECT COUNT(*) FROM submissions WHERE status IN ('queued','running')", 'validating', args.timeout)
        valid = durations(store, 'validate')
        with store.connect() as db:
            ok = db.execute("SELECT COUNT(*) FROM submissions WHERE status='valid'").fetchone()[0]
        print(f'validation: {ok}/{args.teams} valid; queued {args.teams} in {submitted:.2f}s; all graded in {took:.0f}s '
              f'= {args.teams / took:.2f}/s  (job p50 {percentile(valid, .5):.1f}s p95 {percentile(valid, .95):.1f}s)')

        # 2. Everyone asks for a ranked match at once.
        started = time.monotonic()
        for team, sid in zip(teams, subs):
            store.join_ranked(team, sid)
        matched = time.monotonic() - started
        with store.connect() as db:
            series = db.execute('SELECT COUNT(*) FROM ranked_series').fetchone()[0]
        took = wait_for(store, "SELECT COUNT(*) FROM matches WHERE status IN ('queued','running')", 'playing', args.timeout)
        games = durations(store, 'game')
        print(f'ranked: {series} pairs formed in {matched:.2f}s; {len(games)} games played in {took:.0f}s '
              f'= {len(games) / took:.2f} games/s  (job p50 {percentile(games, .5):.1f}s p95 {percentile(games, .95):.1f}s)')
    finally:
        worker.stop.set()
        thread.join(timeout=60)


if __name__ == '__main__':
    main()
