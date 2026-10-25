"""Load test for a staging Arena. Never point this at the live event database.

Simulates N students: register, form teams of three, submit a bot, then keep a
dashboard tab open (the same requests arena.js makes) for --seconds.

  python scripts/arena-load.py --base http://localhost:8080 --users 1000 --invite CODE
"""
import argparse
import http.client
import json
import random
import statistics
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlsplit

parser = argparse.ArgumentParser()
parser.add_argument('--base', default='http://localhost:8080')
parser.add_argument('--users', type=int, default=1000)
parser.add_argument('--concurrency', type=int, default=100)
parser.add_argument('--seconds', type=int, default=120)
parser.add_argument('--interval', type=float, default=15, help='Dashboard refresh period per user')
parser.add_argument('--invite', default='')
parser.add_argument('--unique-bots', action='store_true', help='Make every bot distinct so each one is validated')
args = parser.parse_args()
SOURCE = (Path(__file__).resolve().parents[1] / 'sdk/examples/hunter.py').read_text(encoding='utf-8')
latencies, statuses, lock = [], Counter(), threading.Lock()
run = f'{random.randrange(36**4):04x}'


class Client:
    """One browser: a session cookie and a keep-alive connection reused within each refresh."""
    def __init__(self):
        self.cookie, self.csrf, self.connection = '', '', None
        parts = urlsplit(args.base)
        self.factory = (http.client.HTTPSConnection if parts.scheme == 'https' else http.client.HTTPConnection, parts.netloc)

    def close(self):
        if self.connection:
            self.connection.close()
            self.connection = None

    def call(self, path, data=None, label=None):
        headers = {'Origin': args.base, 'Cookie': self.cookie}
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers.update({'Content-Type': 'application/json', 'X-CSRF-Token': self.csrf})
        started = time.perf_counter()
        try:
            if not self.connection:
                self.connection = self.factory[0](self.factory[1], timeout=60)
            self.connection.request('POST' if body is not None else 'GET', '/api' + path, body, headers)
            response = self.connection.getresponse()
            raw = response.read()
            status = response.status
            cookie = response.getheader('Set-Cookie') or ''
            if cookie.startswith('ott_session='):
                self.cookie = cookie.split(';', 1)[0]
            payload = json.loads(raw or b'{}') if status < 400 else {}
        except (OSError, http.client.HTTPException) as error:
            self.close()
            status, payload = f'network:{getattr(error, "winerror", None) or getattr(error, "errno", None) or type(error).__name__}', {}
        with lock:
            latencies.append((label or path.split('?')[0], time.perf_counter() - started))
            statuses[(label or path.split('?')[0], status)] += 1
        return status, payload


def student(index):
    client = Client()
    status, payload = client.call('/register', {'username': f'load_{run}_{index}', 'name': f'Tải {index}',
                                                'password': 'load-test-password-123', 'invite': args.invite})
    if status != 200:
        return None
    client.csrf = payload['csrf']
    client.close()
    return client


def team_up(batch):
    number, clients = batch
    leader, *mates = clients
    status, _ = leader.call('/team', {'name': f'Load {run} {number}'})
    if status != 201:
        return
    code = leader.call('/me')[1]['team']['code']
    for mate in mates:
        mate.call('/team/join', {'code': code})
    source = SOURCE + (f'\n# {run}-{id(leader)}' if args.unique_bots else '')
    leader.call('/submissions', {'name': 'Tải', 'source': source})
    for client in clients:
        client.close()


def browse(client, stop):
    time.sleep(random.random() * args.interval)
    while not stop.is_set():
        for path in ('/health', '/tournaments', '/submissions', '/matches?mine=1', '/ranked'):
            client.call(path)
        client.close()
        stop.wait(args.interval * (0.75 + random.random() * 0.5))


def report(title):
    print(f'\n== {title}')
    groups = {}
    for label, seconds in latencies:
        groups.setdefault(label, []).append(seconds)
    for label, values in sorted(groups.items()):
        values.sort()
        p95 = values[int(len(values) * .95) - 1] if len(values) > 1 else values[0]
        codes = {status: n for (name, status), n in statuses.items() if name == label}
        print(f'{label:<20} n={len(values):<6} p50={statistics.median(values)*1000:7.0f}ms '
              f'p95={p95*1000:7.0f}ms max={values[-1]*1000:7.0f}ms {codes}')
    latencies.clear()
    statuses.clear()


started = time.monotonic()
with ThreadPoolExecutor(args.concurrency) as pool:
    clients = [c for c in pool.map(student, range(args.users)) if c]
print(f'Registered {len(clients)}/{args.users} in {time.monotonic() - started:.1f}s')
report('registration')
with ThreadPoolExecutor(args.concurrency) as pool:
    list(pool.map(team_up, enumerate(clients[i:i+3] for i in range(0, len(clients), 3))))
report('teams and submissions')
stop = threading.Event()
threads = [threading.Thread(target=browse, args=(c, stop), daemon=True) for c in clients]
for thread in threads:
    thread.start()
time.sleep(args.seconds)
stop.set()
for thread in threads:
    thread.join(timeout=60)
report(f'{len(clients)} open dashboards for {args.seconds}s')
