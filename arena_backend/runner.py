import hashlib
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from .store import Store, Problem, ROOT, RULES_HASH, LIMITS, uid, now


class BotFault(Exception):
    pass


class InfrastructureError(Exception):
    pass


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


class Executor:
    def __init__(self, mode='docker', image='ottv2-bot:1'):
        self.mode, self.image = mode, image
        self.samples = {hashlib.sha256(p.read_text(encoding='utf-8').encode()).hexdigest() for p in (ROOT / 'sdk/examples').glob('*.py')}
        self.image_id = 'bundled-samples'
        self.scope, self.job = '', None

    def preflight(self):
        if self.mode == 'samples':
            return
        try:
            info = subprocess.run(['docker','info','--format','{{json .Runtimes}}'], capture_output=True,
                                  text=True, timeout=15, check=True)
            if self.mode == 'docker' and 'runsc' not in json.loads(info.stdout):
                raise InfrastructureError('Chưa cài gVisor/runsc. Runner chính thức chưa được khởi động.')
            image = subprocess.run(['docker','image','inspect',self.image,'--format','{{.Id}}'], capture_output=True,
                                   text=True, timeout=15, check=True)
            self.image_id = image.stdout.strip()
        except (OSError, subprocess.SubprocessError, ValueError) as exc:
            raise InfrastructureError('Docker/image chưa sẵn sàng. Xem tài liệu triển khai.') from exc

    def choose(self, source, observation):
        if self.mode == 'samples' and hashlib.sha256(source.encode()).hexdigest() not in self.samples:
            raise InfrastructureError('Chế độ mẫu chỉ chạy nguyên bản bot trong sdk/examples. Cần runner Docker/gVisor để chạy bài nộp khác.')
        name = 'ott-turn-' + uid()
        if self.mode == 'samples':
            command = [sys.executable, '-I', '-B', str(ROOT / 'arena_backend/sandbox/adapter.py')]
        else:
            command = ['docker','run','--rm','-i','--name',name,'--network=none','--read-only',
                       '--cap-drop=ALL','--security-opt=no-new-privileges','--memory=256m','--memory-swap=256m',
                       '--cpus=1','--pids-limit=64','--ulimit','cpu=1:1',
                       '--ulimit','nofile=64:64','--ulimit','core=0:0',
                       '--log-driver=none','--user=65532:65532']
            if self.mode == 'docker':
                command += ['--runtime=runsc']
            if self.job:
                command += ['--label', 'ottv2.store=' + self.scope,
                            '--label', 'ottv2.job=' + self.job['id'], '--label', 'ottv2.token=' + self.job['token']]
            command.append(self.image_id)
        proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        events = queue.Queue(maxsize=32)
        stopped = threading.Event()
        def pump(stream, channel):
            try:
                while not stopped.is_set():
                    chunk = stream.read1(1024)
                    if not chunk:
                        break
                    while not stopped.is_set():
                        try:
                            events.put((channel, chunk), timeout=.1)
                            break
                        except queue.Full:
                            pass
            finally:
                # Never block cleanup trying to report EOF after an output overflow.
                while not stopped.is_set():
                    try:
                        events.put((channel, None), timeout=.1)
                        break
                    except queue.Full:
                        pass
        threads = [threading.Thread(target=pump, args=(stream, channel), daemon=True)
                   for stream, channel in ((proc.stdout,'out'), (proc.stderr,'err'))]
        for thread in threads:
            thread.start()
        out, err, ready, eof = b'', b'', False, 0
        deadline = time.monotonic() + 20
        try:
            while eof < 2:
                if time.monotonic() > deadline:
                    if not ready:
                        raise InfrastructureError('Sandbox không khởi động trong 20 giây.')
                    raise BotFault('Bot vượt giới hạn 3 giây/lượt.')
                try:
                    channel, chunk = events.get(timeout=.05)
                except queue.Empty:
                    continue
                if chunk is None:
                    eof += 1
                    continue
                if channel == 'err':
                    err += chunk
                else:
                    out += chunk
                if len(out) > 8192 or len(err) > 4096:
                    raise BotFault('Bot vượt giới hạn đầu ra/log.')
                if not ready and b'\n' in out:
                    marker, out = out.split(b'\n', 1)
                    if marker.strip() != b'OTT_READY':
                        raise InfrastructureError('Sandbox không đúng giao thức khởi động.')
                    ready = True
                    deadline = time.monotonic() + LIMITS['wall_seconds']
                    proc.stdin.write((json.dumps({'source':source,'observation':observation})+'\n').encode())
                    proc.stdin.close()
            proc.wait(timeout=5)
            if not ready:
                raise InfrastructureError('Không khởi chạy được môi trường bot: ' + err.decode(errors='replace')[-400:])
            if proc.returncode:
                raise BotFault('Bot lỗi thực thi hoặc vượt CPU/RAM: ' + err.decode(errors='replace')[-500:])
            if len(out) > LIMITS['output_bytes']:
                raise BotFault('Kết quả lớn hơn 4 KiB.')
            try:
                return checked_move(json.loads(out), observation['legalMoves'])
            except (ValueError, UnicodeDecodeError) as exc:
                raise BotFault('Bot phải trả JSON hợp lệ; không print ra stdout.') from exc
        finally:
            stopped.set()
            if self.mode != 'samples':
                subprocess.run(['docker','rm','-f',name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=5)
            for thread in threads:
                thread.join(timeout=2)
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                if not stream.closed:
                    stream.close()


def observation(packet, seed):
    state = packet['state']
    return {'protocol_version': 1, 'ruleset_hash': RULES_HASH, 'board': state['board'],
            'mySide': state['turn'], 'turn': state['turn'], 'legalMoves': packet['legalMoves'],
            'ply': state['revision'], 'quietPly': state['quietPly'],
            'repetitions': state['repetitions'], 'seed': seed}


class Worker:
    def __init__(self, store, executor):
        self.store, self.executor, self.id = store, executor, uid()
        self.executor.scope = hashlib.sha256(str(store.path).encode()).hexdigest()[:16]
        self.last_cleanup = 0

    def cleanup_stale_containers(self):
        if self.executor.mode == 'samples' or time.monotonic() - self.last_cleanup < 30:
            return
        self.last_cleanup = time.monotonic()
        listing = subprocess.run(['docker','ps','-aq','--filter','label=ottv2.store='+self.executor.scope],
                                 capture_output=True,text=True,timeout=15,check=True)
        for container in listing.stdout.split():
            inspected = subprocess.run(['docker','inspect',container,'--format','{{json .Config.Labels}}'],
                                       capture_output=True,text=True,timeout=10)
            if inspected.returncode:
                continue  # A healthy turn can finish while this list is being inspected.
            labels = json.loads(inspected.stdout)
            with self.store.connect() as db:
                live = db.execute("SELECT 1 FROM jobs WHERE id=? AND token=? AND status='running' AND expires>?",
                                  (labels.get('ottv2.job'),labels.get('ottv2.token'),now())).fetchone()
            if not live:
                subprocess.run(['docker','rm','-f',container],stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL,timeout=15,check=True)

    def run_once(self):
        self.cleanup_stale_containers()
        job = self.store.claim(self.id, self.executor.mode)
        if not job:
            return False
        self.executor.job = job
        done, lost = threading.Event(), threading.Event()
        def heartbeat():
            while not done.wait(15):
                if not self.store.heartbeat(job, self.id, self.executor.mode):
                    lost.set()
                    return
        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        referee = None
        try:
            referee = Referee()
            if job['kind'] == 'validate':
                with self.store.connect() as db:
                    submission = dict(db.execute('SELECT * FROM submissions WHERE id=?', (job['ref'],)).fetchone())
                source = submission['source']
                if hashlib.sha256(source.encode()).hexdigest() != submission['hash']:
                    raise InfrastructureError('Hash bài nộp không khớp.')
                try:
                    packet = referee.call('new')
                    for turn in range(12):
                        if not packet['legalMoves'] or packet['state']['winner']:
                            break
                        move = self.executor.choose(source, observation(packet, turn))
                        packet = referee.call('move', **move)
                    result = {'valid': True, 'log': 'Vượt kiểm tra giao thức và 12 lượt ở cả hai phe. Không bảo đảm bot luôn hợp lệ ở mọi thế cờ.'}
                except BotFault as exc:
                    result = {'valid': False, 'log': str(exc)}
            else:
                with self.store.connect() as db:
                    match = dict(db.execute('SELECT * FROM matches WHERE id=?', (job['ref'],)).fetchone())
                    submissions = {side: dict(db.execute('SELECT * FROM submissions WHERE id=?', (match[side],)).fetchone())
                                   for side in ('p1','p2')}
                    if match['pairing_id']:
                        tournament = db.execute('SELECT t.config,t.rules_hash FROM tournaments t JOIN pairings p '
                                                'ON p.tournament_id=t.id WHERE p.id=?', (match['pairing_id'],)).fetchone()
                        if tournament['rules_hash'] != RULES_HASH or json.loads(tournament['config']) != LIMITS:
                            raise InfrastructureError('Engine hoặc giới hạn runtime không khớp thể lệ đã khóa.')
                        frozen = db.execute('SELECT e.submission_id,e.frozen_hash FROM entries e JOIN pairings p '
                                            'ON e.tournament_id=p.tournament_id WHERE p.id=?', (match['pairing_id'],)).fetchall()
                        hashes = {e['submission_id']: e['frozen_hash'] for e in frozen}
                        if any(hashes.get(s['id']) != s['hash'] for s in submissions.values()):
                            raise InfrastructureError('Snapshot của giải không khớp bài nộp.')
                if match['rules_hash'] != RULES_HASH:
                    raise InfrastructureError('Phiên bản engine không khớp ván đấu.')
                for s in submissions.values():
                    if hashlib.sha256(s['source'].encode()).hexdigest() != s['hash']:
                        raise InfrastructureError('Hash bài nộp không khớp.')
                packet, moves = referee.call('new'), []
                seed = int(job['ref'][:8], 16)
                winner, reason = None, None
                while not packet['state']['winner']:
                    if lost.is_set():
                        raise InfrastructureError('Quyền xử lý job đã hết hạn.')
                    if not packet['legalMoves']:
                        winner, reason = 'draw', 'no_legal_moves'
                        break
                    side = packet['state']['turn']
                    self.store.progress(job, {'state':packet['state'], 'ply':len(moves)})
                    try:
                        move = self.executor.choose(submissions[side]['source'], observation(packet, seed+len(moves)))
                    except BotFault as exc:
                        winner, reason = ('p2' if side == 'p1' else 'p1'), 'technical:' + str(exc).split(':',1)[0]
                        with self.store.connect(True) as db:
                            self.store.audit(db, 'runner', 'bot.technical_loss',
                                             {'match':job['ref'],'side':side,'diagnostic':str(exc)[:1000]})
                        break
                    packet = referee.call('move', **move)
                    moves.append({**move, 'side':side, 'notation':packet['state']['lastMove']['notation']})
                state = packet['state']
                result = {'winner':winner or state['winner'], 'reason':reason or state['winReason'],
                          'moves':moves, 'final_state':state, 'rules_hash':RULES_HASH,
                          'source_hashes':{side:s['hash'] for side,s in submissions.items()},
                          'seed':seed, 'limits':LIMITS, 'execution_mode':self.executor.mode,
                          'image_id':self.executor.image_id, 'finished':now()}
            if not lost.is_set():
                self.store.finish(job, result)
        except Exception as exc:
            self.store.fail(job, 'Lỗi hạ tầng: ' + str(exc))
        finally:
            done.set()
            thread.join(timeout=2)
            if referee:
                referee.close()
        return True
