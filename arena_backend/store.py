import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES_HASH = hashlib.sha256((ROOT / 'rules.js').read_bytes()).hexdigest()
LIMITS = {'cpu_seconds': 1, 'wall_seconds': 3, 'memory_mib': 256,
          'source_bytes': 65536, 'output_bytes': 4096, 'protocol': 1,
          'no_legal_moves': 'draw'}


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def uid():
    return secrets.token_hex(12)


def now():
    return time.time()


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'arena.sqlite3'
        with self.connect() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'player', created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions (
                hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
                csrf TEXT NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS submissions (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
                source TEXT NOT NULL, hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
                log TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS tournaments (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, format TEXT NOT NULL,
                rounds INTEGER NOT NULL, round INTEGER NOT NULL DEFAULT 0,
                deadline REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                rules_hash TEXT NOT NULL, config TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS entries (
                tournament_id TEXT NOT NULL REFERENCES tournaments(id),
                user_id TEXT NOT NULL REFERENCES users(id),
                submission_id TEXT NOT NULL REFERENCES submissions(id), frozen_hash TEXT,
                PRIMARY KEY(tournament_id, user_id));
            CREATE TABLE IF NOT EXISTS pairings (
                id TEXT PRIMARY KEY, tournament_id TEXT NOT NULL REFERENCES tournaments(id),
                round INTEGER NOT NULL, a TEXT NOT NULL REFERENCES users(id), b TEXT REFERENCES users(id));
            CREATE TABLE IF NOT EXISTS matches (
                id TEXT PRIMARY KEY, pairing_id TEXT REFERENCES pairings(id),
                p1 TEXT NOT NULL REFERENCES submissions(id), p2 TEXT NOT NULL REFERENCES submissions(id),
                requester TEXT REFERENCES users(id), status TEXT NOT NULL DEFAULT 'queued',
                winner TEXT, reason TEXT, replay TEXT, progress TEXT,
                rules_hash TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'queued', token TEXT, expires REAL,
                attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
            CREATE UNIQUE INDEX IF NOT EXISTS job_ref ON jobs(kind,ref);
            CREATE INDEX IF NOT EXISTS job_queue ON jobs(status,created);
            CREATE INDEX IF NOT EXISTS submission_owner ON submissions(user_id,created);
            CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, seen REAL NOT NULL, mode TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, actor TEXT, action TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, start REAL NOT NULL, count INTEGER NOT NULL);
            ''')

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def audit(db, actor, action, detail):
        db.execute('INSERT INTO audit(actor,action,detail,created) VALUES(?,?,?,?)',
                   (actor, action, json.dumps(detail), now()))

    def rate(self, key, maximum, window=3600):
        with self.connect(True) as db:
            db.execute('DELETE FROM rate_limits WHERE start<?', (now() - 86400,))
            row = db.execute('SELECT * FROM rate_limits WHERE key=?', (key,)).fetchone()
            if not row or row['start'] + window < now():
                db.execute('INSERT OR REPLACE INTO rate_limits VALUES(?,?,1)', (key, now()))
            elif row['count'] >= maximum:
                raise Problem('Bạn thao tác quá nhanh. Hãy thử lại sau.', 429)
            else:
                db.execute('UPDATE rate_limits SET count=count+1 WHERE key=?', (key,))

    @staticmethod
    def job(db, kind, ref):
        db.execute('INSERT INTO jobs(id,kind,ref,created) VALUES(?,?,?,?)', (uid(), kind, ref, now()))

    def submit(self, user, name, source):
        if not isinstance(source, str) or not 1 <= len(source.encode('utf-8')) <= LIMITS['source_bytes']:
            raise Problem('Mã Python phải có nội dung và không vượt quá 64 KiB.')
        if '\x00' in source:
            raise Problem('File Python chứa ký tự không hợp lệ.')
        source = source.replace('\r\n', '\n')
        sid = uid()
        with self.connect(True) as db:
            db.execute('INSERT INTO submissions(id,user_id,name,source,hash,created) VALUES(?,?,?,?,?,?)',
                       (sid, user, name, source, hashlib.sha256(source.encode()).hexdigest(), now()))
            self.job(db, 'validate', sid)
            self.audit(db, user, 'submission.created', {'id': sid})
        return sid

    def join(self, user, tid, sid):
        with self.connect(True) as db:
            t = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
            s = db.execute('SELECT * FROM submissions WHERE id=? AND user_id=?', (sid, user)).fetchone()
            if not t or t['status'] != 'open' or now() >= t['deadline']:
                raise Problem('Giải đã đóng đăng ký/chọn phiên bản.', 409)
            if not s or s['status'] != 'valid':
                raise Problem('Chỉ được chọn bot của bạn đã vượt kiểm tra.')
            count = db.execute('SELECT COUNT(*) FROM entries WHERE tournament_id=?', (tid,)).fetchone()[0]
            exists = db.execute('SELECT 1 FROM entries WHERE tournament_id=? AND user_id=?', (tid, user)).fetchone()
            if count >= (32 if t['format'] == 'round_robin' else 500) and not exists:
                raise Problem('Giải đã đủ số lượng đăng ký.', 409)
            db.execute('INSERT INTO entries VALUES(?,?,?,NULL) ON CONFLICT(tournament_id,user_id) '
                       'DO UPDATE SET submission_id=excluded.submission_id', (tid, user, sid))
            self.audit(db, user, 'entry.selected', {'tournament': tid, 'submission': sid})

    def freeze(self, tid, actor):
        with self.connect(True) as db:
            t = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
            if not t or t['status'] != 'open':
                raise Problem('Giải không ở trạng thái nhận bài.', 409)
            if now() < t['deadline']:
                raise Problem('Chưa tới hạn đóng bài; không khóa sớm so với lịch công bố.', 409)
            entries = db.execute('SELECT e.*,s.status,s.hash FROM entries e JOIN submissions s '
                                 'ON s.id=e.submission_id WHERE e.tournament_id=?', (tid,)).fetchall()
            if len(entries) < 2 or any(e['status'] != 'valid' for e in entries):
                raise Problem('Cần ít nhất hai bot đã vượt kiểm tra.')
            for e in entries:
                db.execute('UPDATE entries SET frozen_hash=? WHERE tournament_id=? AND user_id=?',
                           (e['hash'], tid, e['user_id']))
            rounds = t['rounds'] if t['format'] == 'swiss' else len(entries) - 1 + len(entries) % 2
            db.execute("UPDATE tournaments SET status='frozen',rounds=? WHERE id=?", (rounds, tid))
            self.audit(db, actor, 'tournament.frozen', {'id': tid, 'count': len(entries)})

    @staticmethod
    def standings(db, tid):
        rows = db.execute('SELECT e.user_id,u.name,s.name bot,e.submission_id FROM entries e '
                          'JOIN users u ON u.id=e.user_id JOIN submissions s ON s.id=e.submission_id '
                          'WHERE e.tournament_id=? ORDER BY e.user_id', (tid,)).fetchall()
        stats = {r['user_id']: {**dict(r), 'points': 0, 'game_points': 0, 'wins': 0,
                              'draws': 0, 'losses': 0, 'byes': 0, 'buchholz': 0} for r in rows}
        opponents = {r['user_id']: [] for r in rows}
        for pair in db.execute('SELECT * FROM pairings WHERE tournament_id=?', (tid,)):
            a, b = pair['a'], pair['b']
            if b is None:
                stats[a]['points'] += 1
                stats[a]['byes'] += 1
                continue
            games = db.execute('SELECT m.*,s.user_id owner1 FROM matches m JOIN submissions s ON s.id=m.p1 '
                               'WHERE pairing_id=?', (pair['id'],)).fetchall()
            if len(games) != 2 or any(g['status'] != 'done' for g in games):
                continue
            scores = {a: 0, b: 0}
            for g in games:
                if g['winner'] == 'draw':
                    scores[a] += .5
                    scores[b] += .5
                else:
                    winning_user = g['owner1'] if g['winner'] == 'p1' else (b if g['owner1'] == a else a)
                    scores[winning_user] += 1
            for player, other in ((a, b), (b, a)):
                score = 1 if scores[player] > scores[other] else .5 if scores[player] == scores[other] else 0
                stats[player]['points'] += score
                stats[player]['game_points'] += scores[player]
                stats[player]['wins' if score == 1 else 'draws' if score == .5 else 'losses'] += 1
                opponents[player].append(other)
        for player, others in opponents.items():
            stats[player]['buchholz'] = sum(stats[o]['points'] for o in others)
        result = sorted(stats.values(), key=lambda s: (-s['points'], -s['buchholz'], -s['game_points'], s['user_id']))
        previous, rank = None, 0
        for index, row in enumerate(result):
            key = (row['points'], row['buchholz'], row['game_points'])
            if key != previous:
                rank = index + 1
            row['rank'], previous = rank, key
        return result

    def next_round(self, tid, actor):
        with self.connect(True) as db:
            t = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
            if not t or t['status'] not in ('frozen', 'running'):
                raise Problem('Cần khóa danh sách trước khi mở vòng.', 409)
            if t['rules_hash'] != RULES_HASH:
                raise Problem('Engine đã thay đổi so với giải; cần dùng đúng phiên bản.', 409)
            if json.loads(t['config']) != LIMITS:
                raise Problem('Giới hạn runtime đã thay đổi so với thể lệ của giải.', 409)
            unfinished = db.execute("SELECT 1 FROM matches m JOIN pairings p ON p.id=m.pairing_id "
                                    "WHERE p.tournament_id=? AND m.status!='done' LIMIT 1", (tid,)).fetchone()
            if unfinished:
                raise Problem('Vòng trước chưa hoàn thành. Kiểm tra hàng đợi hoặc job lỗi.', 409)
            if t['round'] >= t['rounds']:
                db.execute("UPDATE tournaments SET status='completed' WHERE id=?", (tid,))
                self.audit(db, actor, 'tournament.completed', {'id': tid})
                return
            entries = db.execute('SELECT * FROM entries WHERE tournament_id=? ORDER BY user_id', (tid,)).fetchall()
            sids = {e['user_id']: e['submission_id'] for e in entries}
            if t['format'] == 'round_robin':
                ring = list(sids)
                if len(ring) % 2:
                    ring.append(None)
                for _ in range(t['round']):
                    ring = [ring[0], ring[-1], *ring[1:-1]]
                pairs = [(ring[i], ring[-i-1]) for i in range(len(ring)//2)]
            else:
                standings = self.standings(db, tid)
                waiting = [s['user_id'] for s in standings]
                pairs = []
                if len(waiting) % 2:
                    bye = min(standings, key=lambda s: (s['byes'], s['points'], -standings.index(s)))['user_id']
                    waiting.remove(bye)
                    pairs.append((bye, None))
                history = {frozenset((p['a'], p['b'])) for p in db.execute(
                    'SELECT a,b FROM pairings WHERE tournament_id=?', (tid,))}
                # Deterministic Swiss-style pairing: closest score first, avoid rematches where possible.
                while waiting:
                    a = waiting.pop(0)
                    i = next((i for i, b in enumerate(waiting) if frozenset((a, b)) not in history), 0)
                    pairs.append((a, waiting.pop(i)))
            for a, b in pairs:
                if a is None:
                    a, b = b, a
                pid = uid()
                db.execute('INSERT INTO pairings VALUES(?,?,?,?,?)', (pid, tid, t['round'] + 1, a, b))
                if b:
                    for first, second in ((a, b), (b, a)):
                        self.create_match(db, sids[first], sids[second], pairing=pid)
            db.execute("UPDATE tournaments SET status='running',round=round+1 WHERE id=?", (tid,))
            self.audit(db, actor, 'round.created', {'id': tid, 'round': t['round'] + 1})

    @classmethod
    def create_match(cls, db, p1, p2, requester=None, pairing=None):
        mid = uid()
        db.execute('INSERT INTO matches(id,pairing_id,p1,p2,requester,rules_hash,created) VALUES(?,?,?,?,?,?,?)',
                   (mid, pairing, p1, p2, requester, RULES_HASH, now()))
        cls.job(db, 'game', mid)
        return mid

    def claim(self, worker, mode):
        with self.connect(True) as db:
            db.execute('INSERT OR REPLACE INTO workers VALUES(?,?,?)', (worker, now(), mode))
            expired = db.execute("SELECT * FROM jobs WHERE status='running' AND expires<?", (now(),)).fetchall()
            for job in expired:
                status = 'failed' if job['attempts'] >= 3 else 'queued'
                db.execute('UPDATE jobs SET status=?,token=NULL,error=? WHERE id=?',
                           (status, 'Runner mất kết nối; đã thu hồi lượt xử lý.', job['id']))
                if job['kind'] == 'game':
                    db.execute('UPDATE matches SET status=?,progress=NULL WHERE id=?', (status, job['ref']))
                else:
                    db.execute('UPDATE submissions SET status=?,log=? WHERE id=?', (status, 'Chờ runner khôi phục.', job['ref']))
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if not row:
                return None
            token = uid()
            db.execute("UPDATE jobs SET status='running',token=?,expires=?,attempts=attempts+1 WHERE id=?",
                       (token, now() + 90, row['id']))
            table = 'matches' if row['kind'] == 'game' else 'submissions'
            db.execute(f"UPDATE {table} SET status='running' WHERE id=?", (row['ref'],))
            return {**dict(row), 'token': token, 'attempts': row['attempts'] + 1}

    @staticmethod
    def owns(db, job):
        return db.execute("SELECT 1 FROM jobs WHERE id=? AND token=? AND status='running' AND expires>?",
                          (job['id'], job['token'], now())).fetchone() is not None

    def heartbeat(self, job, worker, mode):
        with self.connect(True) as db:
            if not self.owns(db, job):
                return False
            db.execute('UPDATE jobs SET expires=? WHERE id=?', (now() + 90, job['id']))
            db.execute('INSERT OR REPLACE INTO workers VALUES(?,?,?)', (worker, now(), mode))
            return True

    def progress(self, job, state):
        with self.connect(True) as db:
            if not self.owns(db, job):
                raise Problem('Job đã được thu hồi.', 409)
            db.execute('UPDATE matches SET progress=? WHERE id=?', (json.dumps(state), job['ref']))

    def finish(self, job, result):
        with self.connect(True) as db:
            if not self.owns(db, job):
                return False
            if job['kind'] == 'validate':
                db.execute('UPDATE submissions SET status=?,log=? WHERE id=?',
                           ('valid' if result['valid'] else 'rejected', result['log'], job['ref']))
            else:
                db.execute("UPDATE matches SET status='done',winner=?,reason=?,replay=?,progress=NULL WHERE id=?",
                           (result['winner'], result['reason'], json.dumps(result, ensure_ascii=False), job['ref']))
            db.execute("UPDATE jobs SET status='done',token=NULL,expires=NULL,error='' WHERE id=?", (job['id'],))
            self.audit(db, 'runner', 'job.completed', {'id': job['id'], 'attempt': job['attempts']})
            return True

    def fail(self, job, message):
        with self.connect(True) as db:
            if not self.owns(db, job):
                return
            # Explicit admin retry after infrastructure failure; never convert this into a bot loss.
            db.execute("UPDATE jobs SET status='failed',error=?,token=NULL WHERE id=?", (message[:1000], job['id']))
            if job['kind'] == 'game':
                db.execute("UPDATE matches SET status='failed',progress=NULL WHERE id=?", (job['ref'],))
            else:
                db.execute("UPDATE submissions SET status='failed',log=? WHERE id=?", (message[:1000], job['ref']))

    def backup(self, target):
        with self.connect() as source, sqlite3.connect(target) as dest:
            source.backup(dest)
