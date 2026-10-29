import hashlib
import json
import queue
import random
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .store import Problem, ROOT, RULES_HASH, RULES_COMPATIBLE, LIMITS, uid, now

STARTUP_SECONDS = 20
STDERR_LIMIT = 1_048_576


class BotFault(Exception):
    pass


class InfrastructureError(Exception):
    def __init__(self, message, retry=True):
        super().__init__(message)
        self.retry = retry


class Stopped(Exception):
    """The runner is shutting down; the job goes back to the queue."""


def checked_move(move, legal):
    if not isinstance(move, dict) or set(move) != {'from', 'to'}:
        raise BotFault('Bot phải trả object chỉ có from và to.')
    for key in ('from', 'to'):
        if not isinstance(move[key], list) or len(move[key]) != 2 or any(type(v) is not int for v in move[key]):
            raise BotFault('Tọa độ phải là mảng hai số nguyên.')
    if not any(move['from'] == m['from'] and move['to'] == m['to'] for m in legal):
        raise BotFault('Bot trả nước đi không hợp lệ.')
    return move


class Referee:
    def __init__(self):
        self.process = subprocess.Popen(['node', str(ROOT / 'arena_backend/referee.mjs')],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                        text=True, encoding='utf-8')
        self.lines = queue.Queue()

        def reader():
            for line in self.process.stdout:
                self.lines.put(line)
            self.lines.put(None)
        self.thread = threading.Thread(target=reader, daemon=True)
        self.thread.start()

    def call(self, op, **kwargs):
        try:
            self.process.stdin.write(json.dumps({'op': op, **kwargs}) + '\n')
            self.process.stdin.flush()
            line = self.lines.get(timeout=10)
            if line is None:
                raise InfrastructureError('Trọng tài Node.js đã dừng.')
            result = json.loads(line)
        except (OSError, queue.Empty, ValueError) as exc:
            raise InfrastructureError('Không nhận được phản hồi của trọng tài.') from exc
        if not result['ok']:
            raise InfrastructureError('Trọng tài từ chối nước đi đã kiểm tra.')
        return result

    def close(self):
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait(timeout=5)
        self.thread.join(timeout=2)
        self.process.stdin.close()
        self.process.stdout.close()


class BotProcess:
    """One sandboxed Python process that serves every move of one bot in one game."""

    def __init__(self, executor, source, seed, labels=None):
        self.executor, self.name = executor, 'ott-bot-' + uid()
        self.lines = queue.Queue()
        self.stderr, self.stderr_total, self.closed = bytearray(), 0, False
        self.proc = subprocess.Popen(executor.command(self.name, labels or {}), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.threads = [threading.Thread(target=self.pump_out, daemon=True),
                        threading.Thread(target=self.pump_err, daemon=True)]
        for thread in self.threads:
            thread.start()
        try:
            self.start(source, seed)
        except BaseException:
            self.close()
            raise

    def pump_out(self):
        buffer = b''
        try:
            while True:
                chunk = self.proc.stdout.read1(4096)
                if not chunk:
                    break
                buffer += chunk
                while b'\n' in buffer:
                    line, buffer = buffer.split(b'\n', 1)
                    self.lines.put(line)
                if len(buffer) > 4 * LIMITS['output_bytes']:
                    self.lines.put(b'\x00overflow')
                    break
        except (OSError, ValueError):
            pass
        finally:
            self.lines.put(None)

    def pump_err(self):
        try:
            while True:
                chunk = self.proc.stderr.read1(4096)
                if not chunk:
                    break
                self.stderr_total += len(chunk)
                self.stderr = (self.stderr + chunk)[-4096:]
        except (OSError, ValueError):
            pass

    def tail(self, size=500):
        return self.stderr.decode(errors='replace')[-size:].strip()

    def read(self, timeout):
        try:
            return self.lines.get(timeout=max(timeout, 0.001))
        except queue.Empty:
            raise TimeoutError from None

    def write(self, message):
        try:
            self.proc.stdin.write((json.dumps(message, separators=(',', ':')) + '\n').encode())
            self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            return False

    def died(self, prefix):
        time.sleep(.05)  # let stderr arrive so the team sees its own traceback
        detail = self.tail()
        return BotFault(f'{prefix}: {detail}' if detail else prefix)

    def start(self, source, seed):
        try:
            line = self.read(STARTUP_SECONDS)
        except TimeoutError:
            raise InfrastructureError(f'Sandbox không khởi động trong {STARTUP_SECONDS} giây.') from None
        if line is None:
            raise InfrastructureError('Không khởi chạy được môi trường bot: ' + self.tail(400))
        if line.strip() != b'OTT_READY':
            raise InfrastructureError('Sandbox không đúng giao thức khởi động.')
        if not self.write({'source': source, 'seed': seed}):
            raise self.died('Bot dừng khi đang nạp mã')
        try:
            reply = self.read(LIMITS['init_seconds'])
        except TimeoutError:
            raise BotFault(f'Bot nạp mã quá {LIMITS["init_seconds"]} giây.') from None
        message = self.decode(reply, 'Bot dừng khi đang nạp mã')
        if message.get('ready') is not True:
            raise BotFault(str(message.get('error') or 'Bot không xác nhận sẵn sàng.'))

    def decode(self, line, prefix):
        if line is None:
            raise self.died(prefix)
        if line == b'\x00overflow' or len(line) > LIMITS['output_bytes']:
            raise BotFault('Kết quả lớn hơn 4 KiB.')
        try:
            message = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            raise BotFault('Bot phải trả JSON hợp lệ.') from None
        if not isinstance(message, dict):
            raise BotFault('Bot phải trả JSON hợp lệ.')
        if 'error' in message:
            raise BotFault(str(message['error'])[:600])
        return message

    def move(self, observation, budget):
        limit = min(LIMITS['turn_seconds'], budget)
        started = time.monotonic()
        if not self.write(observation):
            raise self.died('Bot lỗi thực thi hoặc vượt CPU/RAM')
        try:
            line = self.read(limit)
        except TimeoutError:
            if budget < LIMITS['turn_seconds']:
                raise BotFault(f'Bot hết quỹ {LIMITS["game_seconds"]} giây suy nghĩ của ván.') from None
            raise BotFault(f'Bot vượt giới hạn {LIMITS["turn_seconds"]} giây/lượt.') from None
        elapsed = time.monotonic() - started
        if self.stderr_total > STDERR_LIMIT:
            raise BotFault('Bot ghi log quá 1 MiB.')
        message = self.decode(line, 'Bot lỗi thực thi hoặc vượt CPU/RAM')
        return checked_move(message.get('move'), observation['legalMoves']), elapsed

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.executor.mode != 'samples':
            subprocess.run(['docker', 'rm', '-f', self.name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15)
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        for thread in self.threads:
            thread.join(timeout=2)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                stream.close()
            except OSError:
                pass


class Executor:
    def __init__(self, mode='docker', image='ottv2-bot:1'):
        self.mode, self.image = mode, image
        self.samples = {hashlib.sha256(p.read_text(encoding='utf-8').encode()).hexdigest()
                        for folder in ('sdk/examples', 'sdk/bosses') for p in (ROOT / folder).glob('*.py')}
        self.image_id = 'bundled-samples'

    def preflight(self):
        if self.mode == 'samples':
            return
        try:
            info = subprocess.run(['docker', 'info', '--format', '{{json .Runtimes}}'], capture_output=True,
                                  text=True, timeout=15, check=True)
            if self.mode == 'docker' and 'runsc' not in json.loads(info.stdout):
                raise InfrastructureError('Chưa cài gVisor/runsc. Runner chính thức chưa được khởi động.')
            image = subprocess.run(['docker', 'image', 'inspect', self.image, '--format', '{{.Id}}'],
                                   capture_output=True, text=True, timeout=15, check=True)
            self.image_id = image.stdout.strip()
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise InfrastructureError('Docker/image chưa sẵn sàng. Xem tài liệu triển khai.') from exc

    def allowed(self, source):
        return self.mode != 'samples' or hashlib.sha256(source.encode()).hexdigest() in self.samples

    def command(self, name, labels):
        if self.mode == 'samples':
            return [sys.executable, '-I', '-B', str(ROOT / 'arena_backend/sandbox/adapter.py')]
        cpu = LIMITS['game_seconds'] + LIMITS['init_seconds'] + 15
        command = ['docker', 'run', '--rm', '-i', '--name', name, '--network=none', '--read-only',
                   '--cap-drop=ALL', '--security-opt=no-new-privileges', '--memory=256m', '--memory-swap=256m',
                   '--cpus=1', '--pids-limit=64', '--ulimit', f'cpu={cpu}:{cpu}',
                   '--ulimit', 'nofile=64:64', '--ulimit', 'core=0:0',
                   '--log-driver=none', '--user=65532:65532']
        if self.mode == 'docker':
            command += ['--runtime=runsc']
        for key, value in labels.items():
            command += ['--label', f'ottv2.{key}={value}']
        return command + [self.image_id]

    def spawn(self, source, seed, labels=None):
        if not self.allowed(source):
            raise InfrastructureError('Chế độ mẫu chỉ chạy nguyên bản bot trong sdk/examples và boss trong sdk/bosses. '
                                      'Cần runner Docker/gVisor để chạy bài nộp khác.', retry=False)
        return BotProcess(self, source, seed, labels)

    def choose(self, source, observation):
        """Single move in a fresh sandbox (used by the sandbox smoke test)."""
        bot = self.spawn(source, observation.get('seed', 0))
        try:
            return bot.move(observation, LIMITS['game_seconds'])[0]
        finally:
            bot.close()


def observation(packet, seed, time_left=None, map_name='standard'):
    state = packet['state']
    size = len(state['board'])
    # Obstacles are listed in `obstacles` and show as None in `board`, so a bot that only reads `legalMoves` never notices them.
    obstacles = [[r, c] for r, line in enumerate(state['board']) for c, cell in enumerate(line) if cell and cell['type'] == 'wall']
    board = [[None if cell and cell['type'] == 'wall' else cell for cell in line] for line in state['board']]
    goals = {'p1': [0, size - 1], 'p2': [size - 1, 0]}
    return {'protocol_version': LIMITS['protocol'], 'ruleset_hash': RULES_HASH, 'map': map_name, 'size': size,
            'obstacles': obstacles, 'board': board, 'goal': goals[state['turn']],
            'opponentGoal': goals['p2' if state['turn'] == 'p1' else 'p1'],
            'mySide': state['turn'], 'turn': state['turn'], 'legalMoves': packet['legalMoves'],
            'ply': state['revision'], 'quietPly': state['quietPly'],
            'repetitions': state['repetitions'], 'seed': seed,
            'timeLeft': round(LIMITS['game_seconds'] if time_left is None else time_left, 3),
            'turnLimit': LIMITS['turn_seconds'], 'maxPly': LIMITS['max_plies']}


# Live viewers see a new batch of moves about this often; a 400-move game is ~6 KB per write.
PROGRESS_INTERVAL = 1.0
# Live viewing is a luxury: stop publishing move-by-move progress when the queue is this deep.
LIVE_SHED_BACKLOG = 150


class Worker:
    def __init__(self, store, executor, slots=1):
        self.store, self.executor, self.id, self.slots = store, executor, uid(), max(1, slots)
        self.scope = hashlib.sha256(str(store.path).encode()).hexdigest()[:16]
        self.stop = threading.Event()
        self.cleanup_lock = threading.Lock()
        self.last_cleanup = 0

    def cleanup_stale_containers(self):
        if self.executor.mode == 'samples' or time.monotonic() - self.last_cleanup < 30:
            return
        if not self.cleanup_lock.acquire(blocking=False):
            return
        try:
            self.last_cleanup = time.monotonic()
            listing = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=ottv2.store='+self.scope],
                                     capture_output=True, text=True, timeout=15, check=True)
            for container in listing.stdout.split():
                inspected = subprocess.run(['docker', 'inspect', container, '--format', '{{json .Config.Labels}}'],
                                           capture_output=True, text=True, timeout=10)
                if inspected.returncode:
                    continue  # A healthy game can finish while this list is being inspected.
                labels = json.loads(inspected.stdout)
                with self.store.connect() as db:
                    live = db.execute("SELECT 1 FROM jobs WHERE id=? AND token=? AND status='running' AND expires>?",
                                      (labels.get('ottv2.job'), labels.get('ottv2.token'), now())).fetchone()
                if not live:
                    subprocess.run(['docker', 'rm', '-f', container], stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=15)
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            print('Container cleanup skipped:', exc, flush=True)
        finally:
            self.cleanup_lock.release()

    def check(self, lost):
        if lost.is_set():
            raise InfrastructureError('Quyền xử lý job đã hết hạn.')
        if self.stop.is_set():
            raise Stopped()

    def spawn_all(self, sources, seed, labels):
        """Start every side's sandbox in parallel; returns {side: BotProcess | BotFault}."""
        with ThreadPoolExecutor(len(sources)) as pool:
            futures = {side: pool.submit(self.executor.spawn, source, seed, labels)
                       for side, source in sources.items()}
        bots, problem = {}, None
        for side, future in futures.items():
            try:
                bots[side] = future.result()
            except BotFault as exc:
                bots[side] = exc
            except Exception as exc:  # noqa: BLE001 - surface the first infrastructure error after cleanup
                problem = problem or exc
        if problem:
            for bot in bots.values():
                if isinstance(bot, BotProcess):
                    bot.close()
            raise problem
        return bots

    def run_once(self, slot=0):
        self.cleanup_stale_containers()
        worker = f'{self.id}:{slot}'
        job = self.store.claim(worker, self.executor.mode)
        if not job:
            return False
        done, lost = threading.Event(), threading.Event()

        def heartbeat():
            while not done.wait(15):
                if not self.store.heartbeat(job, worker, self.executor.mode):
                    lost.set()
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        referee = None
        labels = {'store': self.scope, 'job': job['id'], 'token': job['token']}
        try:
            referee = Referee()
            result = (self.validate if job['kind'] == 'validate' else self.game)(job, referee, labels, lost)
            if not lost.is_set():
                self.store.finish(job, result)
        except Stopped:
            self.store.release(job)
        except Problem:
            pass  # The job was cancelled or reassigned while running; its owner records the result.
        except InfrastructureError as exc:
            self.store.fail(job, 'Lỗi hạ tầng: ' + str(exc), exc.retry)
        except Exception as exc:  # noqa: BLE001 - never turn an unexpected runner error into a bot loss
            self.store.fail(job, f'Lỗi hạ tầng: {type(exc).__name__}: {exc}')
        finally:
            done.set()
            thread.join(timeout=2)
            if referee:
                referee.close()
        return True

    def validate(self, job, referee, labels, lost):
        with self.store.connect() as db:
            submission = dict(db.execute('SELECT * FROM submissions WHERE id=?', (job['ref'],)).fetchone())
        source = submission['source']
        if hashlib.sha256(source.encode()).hexdigest() != submission['hash']:
            raise InfrastructureError('Hash bài nộp không khớp.', retry=False)
        seed = int(job['ref'][:8], 16)
        bots = self.spawn_all({'p1': source, 'p2': source}, seed, labels)
        try:
            for bot in bots.values():
                if isinstance(bot, BotFault):
                    return {'valid': False, 'log': str(bot)}
            packet, budgets = referee.call('new'), {'p1': LIMITS['game_seconds'], 'p2': LIMITS['game_seconds']}
            for turn in range(12):
                self.check(lost)
                if not packet['legalMoves'] or packet['state']['winner']:
                    break
                side = packet['state']['turn']
                move, used = bots[side].move(observation(packet, seed + turn, budgets[side]), budgets[side])
                budgets[side] -= used
                packet = referee.call('move', **move)
            return {'valid': True, 'log': 'Vượt kiểm tra giao thức và 12 nửa lượt ở cả hai phe. '
                                          'Không bảo đảm bot luôn hợp lệ ở mọi thế cờ.'}
        except BotFault as exc:
            return {'valid': False, 'log': str(exc)}
        finally:
            for bot in bots.values():
                if isinstance(bot, BotProcess):
                    bot.close()

    def game(self, job, referee, labels, lost):
        with self.store.connect() as db:
            match = dict(db.execute('SELECT * FROM matches WHERE id=?', (job['ref'],)).fetchone())
            submissions = {side: dict(db.execute('SELECT * FROM submissions WHERE id=?', (match[side],)).fetchone())
                           for side in ('p1', 'p2')}
            if match['pairing_id']:
                tournament = db.execute('SELECT t.config,t.rules_hash FROM tournaments t JOIN pairings p '
                                        'ON p.tournament_id=t.id WHERE p.id=?', (match['pairing_id'],)).fetchone()
                if tournament['rules_hash'] not in RULES_COMPATIBLE or json.loads(tournament['config']) != LIMITS:
                    raise InfrastructureError('Engine hoặc giới hạn runtime không khớp thể lệ đã khóa.', retry=False)
                frozen = db.execute('SELECT e.submission_id,e.frozen_hash FROM entries e JOIN pairings p '
                                    'ON e.tournament_id=p.tournament_id WHERE p.id=?', (match['pairing_id'],)).fetchall()
                hashes = {e['submission_id']: e['frozen_hash'] for e in frozen}
                if any(hashes.get(s['id']) != s['hash'] for s in submissions.values()):
                    raise InfrastructureError('Snapshot của giải không khớp bài nộp.', retry=False)
        if match['rules_hash'] not in RULES_COMPATIBLE:
            raise InfrastructureError('Phiên bản engine không khớp ván đấu.', retry=False)
        for s in submissions.values():
            if hashlib.sha256(s['source'].encode()).hexdigest() != s['hash']:
                raise InfrastructureError('Hash bài nộp không khớp.', retry=False)
        seed = int(job['ref'][:8], 16)
        map_name = match.get('map') or 'standard'
        packet, moves = referee.call('new', map=map_name), []
        winner, reason = None, None
        budgets = {'p1': LIMITS['game_seconds'], 'p2': LIMITS['game_seconds']}
        bots = self.spawn_all({side: s['source'] for side, s in submissions.items()}, seed, labels)
        try:
            faults = {side: bot for side, bot in bots.items() if isinstance(bot, BotFault)}
            if faults:
                winner = 'draw' if len(faults) == 2 else ('p2' if 'p1' in faults else 'p1')
                reason = 'technical:both' if len(faults) == 2 else 'technical:' + str(next(iter(faults.values()))).split(':', 1)[0]
                self.audit_faults(job, faults)
            last_progress = 0
            with self.store.connect() as db:
                live_on = self.store.settings(db).get('live_open') == '1' and db.execute(
                    "SELECT COUNT(*) FROM jobs WHERE status='queued'").fetchone()[0] < LIVE_SHED_BACKLOG
            while not winner and not packet['state']['winner']:
                self.check(lost)
                for cause, stalled in (('no_legal_moves', not packet['legalMoves']),
                                       ('ply_limit', len(moves) >= LIMITS['max_plies'])):
                    if stalled:
                        # A game the arena cannot continue is decided like any stalled game: material, then advancement.
                        verdict = referee.call('adjudicate', cause=cause)['verdict']
                        winner, reason = verdict['winner'], verdict['reason']
                        break
                if winner:
                    break
                side = packet['state']['turn']
                if live_on and time.monotonic() - last_progress > PROGRESS_INTERVAL:
                    self.publish(job, moves, side, budgets)
                    last_progress = time.monotonic()
                try:
                    move, used = bots[side].move(observation(packet, seed + len(moves), budgets[side], map_name), budgets[side])
                except BotFault as exc:
                    winner, reason = ('p2' if side == 'p1' else 'p1'), 'technical:' + str(exc).split(':', 1)[0]
                    self.audit_faults(job, {side: exc})
                    break
                budgets[side] -= used
                packet = referee.call('move', **move)
                moves.append({**move, 'side': side, 'notation': packet['state']['lastMove']['notation']})
        finally:
            for bot in bots.values():
                if isinstance(bot, BotProcess):
                    bot.close()
        state = packet['state']
        # The move list rebuilds the game; keep only what a result page needs (history and repetition
        # tables are most of the size of a stored replay).
        final = {k: v for k, v in state.items() if k not in ('history', 'repetitions')}
        return {'winner': winner or state['winner'], 'reason': reason or state['winReason'],
                'moves': moves, 'final_state': final, 'rules_hash': RULES_HASH, 'map': map_name,
                'source_hashes': {side: s['hash'] for side, s in submissions.items()},
                'seed': seed, 'limits': LIMITS, 'execution_mode': self.executor.mode,
                'time_used': {side: round(LIMITS['game_seconds'] - left, 3) for side, left in budgets.items()},
                'image_id': self.executor.image_id, 'finished': now()}

    def publish(self, job, moves, side, budgets):
        """Live view: compact moves so far, whose turn it is, and each side's remaining clock."""
        self.store.progress(job, {'ply': len(moves), 'thinking': side,
                                  'moves': [[m['from'][0], m['from'][1], m['to'][0], m['to'][1]] for m in moves],
                                  'left': {k: round(v, 1) for k, v in budgets.items()}})

    def audit_faults(self, job, faults):
        with self.store.connect(True) as db:
            for side, exc in faults.items():
                self.store.audit(db, 'runner', 'bot.technical_loss',
                                 {'match': job['ref'], 'side': side, 'diagnostic': str(exc)[:1000]})

    def loop(self, slot):
        while not self.stop.is_set():
            try:
                busy = self.run_once(slot)
            except Exception as exc:  # noqa: BLE001 - e.g. database busy; keep the slot alive
                print(f'Runner slot {slot} error:', exc, flush=True)
                busy = False
            if not busy:
                self.stop.wait(1.5 + random.random())

    def run_forever(self):
        threads = [threading.Thread(target=self.loop, args=(slot,), daemon=True) for slot in range(self.slots)]
        for thread in threads:
            thread.start()
        try:
            while any(thread.is_alive() for thread in threads):
                if self.stop.wait(1):
                    break
        finally:
            self.stop.set()
            for thread in threads:
                thread.join(timeout=30)
