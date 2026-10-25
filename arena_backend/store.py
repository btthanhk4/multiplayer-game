import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import threading
import time
import unicodedata
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RULES_HASH = hashlib.sha256((ROOT / 'rules.js').read_bytes()).hexdigest()
# Protocol 2: one sandbox per bot per game. turn_seconds is wall time per move,
# game_seconds is each bot's thinking budget for the whole game.
LIMITS = {'protocol': 2, 'turn_seconds': 3, 'game_seconds': 60, 'init_seconds': 5,
          'memory_mib': 256, 'source_bytes': 65536, 'output_bytes': 4096,
          'max_plies': 400, 'no_legal_moves': 'adjudicate', 'ply_limit': 'adjudicate'}
ENGINE_KEY = hashlib.sha256((RULES_HASH + json.dumps(LIMITS, sort_keys=True)).encode()).hexdigest()
# Starting formations ("bản đồ"). Rules, goals and board size never change; ids match rules.js. `random` gives every
# pair its own symmetric formation (both games of the pair share it).
MAPS = [
    ('standard', 'Chuẩn', 'Cách xếp quân gốc.'),
    ('wall', 'Bức tường', 'Hai hàng liền nhau dọc đáy bàn cờ.'),
    ('flank', 'Hai cánh', 'Quân chia thành hai cụm ở hai bên.'),
    ('block', 'Khối vuông', 'Ba hàng ba quân xếp thành một khối.'),
    ('random', 'Ngẫu nhiên', 'Mỗi cặp đấu một cách xếp khác nhau, đối xứng hai bên.'),
]
MAP_IDS = {item[0] for item in MAPS}
SCHEMA_VERSION = 3
PRIORITY = {'validate': 0, 'tournament': 1, 'ranked': 2, 'campaign': 3, 'friendly': 3, 'practice': 3}
TEAM_SIZE = int(os.getenv('ARENA_TEAM_SIZE', '4'))
PENDING_PER_TEAM = int(os.getenv('ARENA_PENDING_PER_TEAM', '3'))
CAPACITY = {'round_robin': 32, 'swiss': 2000, 'knockout': 1024}
# Back-pressure: refuse new optional games when this many are already waiting, so one stampede of
# practice/boss/ranked requests cannot bury the grader for hours. Tournament games are never refused.
QUEUE_CAP_LOW = int(os.getenv('ARENA_QUEUE_CAP_LOW', '2000'))        # practice, friendly, campaign
QUEUE_CAP_RANKED = int(os.getenv('ARENA_QUEUE_CAP_RANKED', '4000'))  # ranked games
# A waiting job moves up one priority level per this many seconds, at most AGING_LEVELS levels, so a
# steady stream of higher-priority work cannot starve boss/practice games forever. Validation always goes first.
AGING_SECONDS, AGING_LEVELS = 300.0, 1.5
DEFAULT_SETTINGS = {'registration_open': '1', 'submissions_open': '1', 'ranked_open': '1',
                    'practice_open': '1', 'campaign_open': '1', 'live_open': '1', 'read_only': '0', 'announcement': ''}
# Share of ranked teams (cumulative) that reach each classification.
TIERS = [(0.10, 'Xuất sắc'), (0.30, 'Giỏi'), (0.60, 'Khá'), (1.0, 'Đạt')]
ELO_TIERS = [(1150, 'Kim cương'), (1075, 'Vàng'), (1000, 'Bạc'), (0, 'Đồng')]
UNFINISHED = "('queued','running','failed')"
# Campaign ladder: built-in bots of rising strength. A level is cleared by taking
# at least 1.5 of the 2 colour-swapped games; 2/2 earns three stars.
SYSTEM_TEAM = 'b0550000000000000000b055'
BOSSES = [
    (1, 'Tân binh', 'sdk/examples/first.py', 'Luôn đi nước hợp lệ đầu tiên. Ải khởi động để kiểm tra bot chạy đúng.'),
    (2, 'Thợ săn', 'sdk/examples/hunter.py', 'Lao thẳng về ô đích, thấy quân ăn được là ăn.'),
    (3, 'Người gác', 'sdk/examples/guardian.py', 'Né ô bị khắc chế và giữ chặt loại quân cuối cùng.'),
    (4, 'Kỵ sĩ', 'sdk/bosses/knight.py', 'Tính trước nước đáp trả tốt nhất của bạn trước khi đi.'),
    (5, 'Đại kiện tướng', 'sdk/bosses/grandmaster.py', 'Tìm kiếm alpha-beta, xét hết chuỗi ăn quân. Trùm cuối.'),
]
CLEAR_POINTS = 1.5
BADGES = [
    ('first_bot', '🐣', 'Khai bút', 'Có bot đầu tiên vượt kiểm tra'),
    ('inventor', '🧪', 'Nhà phát minh', 'Nộp 5 phiên bản bot'),
    ('squad', '👥', 'Đủ đội hình', f'Đội có đủ {TEAM_SIZE} thành viên'),
    ('first_win', '⚔️', 'Trận thắng đầu', 'Thắng một ván ngoài đấu tập'),
    ('goal', '🎯', 'Về đích', 'Thắng bằng cách đưa quân vào ô đích'),
    ('wipe', '💥', 'Càn quét', 'Thắng bằng cách ăn hết một loại quân'),
    ('blitz', '⚡', 'Chớp nhoáng', 'Thắng trong tối đa 40 nửa lượt'),
    ('flawless', '🛡️', 'Toàn vẹn', 'Thắng mà không mất quân nào'),
    ('streak', '🔥', 'Chuỗi 3', 'Thắng 3 cặp xếp hạng liên tiếp'),
    ('upset', '😮', 'Lật kèo', 'Thắng cặp xếp hạng trước đội hơn mình 100 Elo'),
    ('gold', '🥇', 'Bậc Vàng', 'Chạm 1075 Elo'),
    ('diamond', '💎', 'Kim cương', 'Chạm 1150 Elo'),
    ('boss1', '🗡️', 'Vượt ải', 'Hạ boss đầu tiên của chiến dịch'),
    ('boss_final', '👑', 'Hạ trùm cuối', f'Hạ {BOSSES[-1][1]}'),
    ('perfect', '🌟', 'Hoàn hảo', 'Ba sao ở mọi ải chiến dịch'),
    ('podium', '🏆', 'Bục vinh quang', 'Vào top 3 một giải đã hoàn tất'),
    ('excellent', '🎓', 'Xuất sắc', 'Xếp loại Xuất sắc ở một giải đã hoàn tất'),
]
# Columns added after a table first shipped; CREATE TABLE IF NOT EXISTS cannot add them.
ADDED_COLUMNS = {'matches': ['plies INTEGER', "map TEXT NOT NULL DEFAULT 'standard'"],
                 'ranked_series': ['a_rating INTEGER', 'b_rating INTEGER'],
                 'tournaments': ["map TEXT NOT NULL DEFAULT 'standard'", 'final_map TEXT']}

SCHEMA = [
    '''CREATE TABLE IF NOT EXISTS teams (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, name_key TEXT NOT NULL UNIQUE, code TEXT NOT NULL UNIQUE,
        owner TEXT, status TEXT NOT NULL DEFAULT 'active', note TEXT NOT NULL DEFAULT '', created REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'player', created REAL NOT NULL,
        team_id TEXT REFERENCES teams(id), student_id TEXT, disabled INTEGER NOT NULL DEFAULT 0,
        must_change INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS sessions (
        hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
        csrf TEXT NOT NULL, expires REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS submissions (
        id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES teams(id), author TEXT, name TEXT NOT NULL,
        source TEXT NOT NULL, hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
        log TEXT NOT NULL DEFAULT '', created REAL NOT NULL, origin TEXT NOT NULL DEFAULT 'web', checked TEXT)''',
    '''CREATE TABLE IF NOT EXISTS tournaments (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, format TEXT NOT NULL,
        rounds INTEGER NOT NULL, round INTEGER NOT NULL DEFAULT 0,
        deadline REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open',
        rules_hash TEXT NOT NULL, config TEXT NOT NULL, created REAL NOT NULL,
        auto_advance INTEGER NOT NULL DEFAULT 0, seed_from TEXT, seed_count INTEGER,
        description TEXT NOT NULL DEFAULT '', map TEXT NOT NULL DEFAULT 'standard', final_map TEXT)''',
    '''CREATE TABLE IF NOT EXISTS entries (
        tournament_id TEXT NOT NULL REFERENCES tournaments(id),
        team_id TEXT NOT NULL REFERENCES teams(id),
        submission_id TEXT NOT NULL REFERENCES submissions(id), frozen_hash TEXT,
        seed INTEGER, status TEXT NOT NULL DEFAULT 'active',
        PRIMARY KEY(tournament_id, team_id))''',
    '''CREATE TABLE IF NOT EXISTS pairings (
        id TEXT PRIMARY KEY, tournament_id TEXT NOT NULL REFERENCES tournaments(id),
        round INTEGER NOT NULL, slot INTEGER NOT NULL DEFAULT 0,
        a TEXT NOT NULL REFERENCES teams(id), b TEXT REFERENCES teams(id), forfeit TEXT)''',
    '''CREATE TABLE IF NOT EXISTS matches (
        id TEXT PRIMARY KEY, pairing_id TEXT REFERENCES pairings(id),
        p1 TEXT NOT NULL REFERENCES submissions(id), p2 TEXT NOT NULL REFERENCES submissions(id),
        requester TEXT, kind TEXT NOT NULL DEFAULT 'practice', status TEXT NOT NULL DEFAULT 'queued',
        winner TEXT, reason TEXT, replay TEXT, progress TEXT,
        rules_hash TEXT NOT NULL, created REAL NOT NULL, p1_left INTEGER, p2_left INTEGER, plies INTEGER,
        map TEXT NOT NULL DEFAULT 'standard')''',
    '''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'queued', token TEXT, expires REAL,
        attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
        priority INTEGER NOT NULL DEFAULT 2, started REAL, finished REAL, worker TEXT)''',
    'CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, seen REAL NOT NULL, mode TEXT NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS audit (
        id INTEGER PRIMARY KEY, actor TEXT, action TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL)''',
    'CREATE TABLE IF NOT EXISTS rate_limits (key TEXT PRIMARY KEY, start REAL NOT NULL, count INTEGER NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS ratings (
        team_id TEXT PRIMARY KEY REFERENCES teams(id), rating INTEGER NOT NULL DEFAULT 1000,
        played INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0,
        draws INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE IF NOT EXISTS ranked_queue (
        team_id TEXT PRIMARY KEY REFERENCES teams(id),
        submission_id TEXT NOT NULL REFERENCES submissions(id), joined REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS ranked_series (
        id TEXT PRIMARY KEY, a TEXT NOT NULL REFERENCES teams(id), b TEXT NOT NULL REFERENCES teams(id),
        a_submission TEXT NOT NULL REFERENCES submissions(id),
        b_submission TEXT NOT NULL REFERENCES submissions(id),
        first_match TEXT REFERENCES matches(id), second_match TEXT REFERENCES matches(id),
        status TEXT NOT NULL DEFAULT 'running', a_points REAL, rating_change INTEGER,
        created REAL NOT NULL, finished REAL, a_rating INTEGER, b_rating INTEGER)''',
    'CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)',
    '''CREATE TABLE IF NOT EXISTS bosses (
        level INTEGER PRIMARY KEY, name TEXT NOT NULL, blurb TEXT NOT NULL,
        submission_id TEXT NOT NULL REFERENCES submissions(id))''',
    '''CREATE TABLE IF NOT EXISTS campaign_runs (
        id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES teams(id), level INTEGER NOT NULL,
        submission_id TEXT NOT NULL REFERENCES submissions(id), boss_submission TEXT NOT NULL REFERENCES submissions(id),
        first_match TEXT REFERENCES matches(id), second_match TEXT REFERENCES matches(id),
        status TEXT NOT NULL DEFAULT 'running', points REAL, created REAL NOT NULL, finished REAL)''',
    'CREATE INDEX IF NOT EXISTS v3_campaign_team ON campaign_runs(team_id,level)',
    'CREATE INDEX IF NOT EXISTS v3_campaign_done ON campaign_runs(status,finished)',
    'CREATE INDEX IF NOT EXISTS v3_series_done ON ranked_series(status,finished)',
    'CREATE INDEX IF NOT EXISTS v3_audit_action ON audit(action,created)',
    'CREATE UNIQUE INDEX IF NOT EXISTS v2_job_ref ON jobs(kind,ref)',
    'CREATE INDEX IF NOT EXISTS v2_job_queue ON jobs(status,priority,created)',
    'CREATE INDEX IF NOT EXISTS v2_sub_team ON submissions(team_id,created)',
    'CREATE INDEX IF NOT EXISTS v2_sub_hash ON submissions(hash)',
    'CREATE INDEX IF NOT EXISTS v2_sub_status ON submissions(status)',
    'CREATE INDEX IF NOT EXISTS v2_users_team ON users(team_id)',
    'CREATE INDEX IF NOT EXISTS v2_matches_status ON matches(status,created)',
    'CREATE INDEX IF NOT EXISTS v3_matches_created ON matches(created)',
    'CREATE INDEX IF NOT EXISTS v2_matches_pairing ON matches(pairing_id)',
    'CREATE INDEX IF NOT EXISTS v2_matches_p1 ON matches(p1)',
    'CREATE INDEX IF NOT EXISTS v2_matches_p2 ON matches(p2)',
    'CREATE INDEX IF NOT EXISTS v2_pairings_round ON pairings(tournament_id,round,slot)',
    'CREATE INDEX IF NOT EXISTS v2_ratings_order ON ratings(rating DESC,played DESC)',
    'CREATE INDEX IF NOT EXISTS v2_series_a ON ranked_series(a,created)',
    'CREATE INDEX IF NOT EXISTS v2_series_b ON ranked_series(b,created)',
]


class Problem(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def uid():
    return secrets.token_hex(12)


def now():
    return time.time()


# Zero-width characters, BOM and soft hyphen vanish; non-breaking and wide spaces become ordinary spaces.
INVISIBLE_IN_CODE = {**dict.fromkeys(map(ord, '\u200b\u200c\u200d\u2060\ufeff\u00ad'), None),
                     **dict.fromkeys(map(ord, '\u00a0\u2007\u202f\u3000'), ' ')}


def name_key(text):
    return unicodedata.normalize('NFC', ' '.join(str(text).split())).casefold()


def slug(text):
    value = unicodedata.normalize('NFD', str(text)).replace('đ', 'd').replace('Đ', 'D')
    value = ''.join(c for c in value if unicodedata.category(c) != 'Mn').lower()
    return re.sub(r'[^a-z0-9]+', '', value)


def team_code():
    return ''.join(secrets.choice('ABCDEFGHJKLMNPQRSTUVWXYZ23456789') for _ in range(8))


def resolve_map(choice, seed=None):
    """Concrete map id for one pair of games: `random` becomes `random:<seed>` so both games share a formation."""
    choice = choice or 'standard'
    if choice not in MAP_IDS:
        raise Problem('Bản đồ không tồn tại.')
    return f'random:{(seed or uid())[:8]}' if choice == 'random' else choice


def elo_tier(rating):
    return next(label for floor, label in ELO_TIERS if rating >= floor)


def pair_without_rematches(teams, points, history, budget=60000):
    """Exact search for a pairing with no repeat opponent, closest scores first; None when there is none (or the
    search budget runs out). Used for small fields, where the greedy method can leave avoidable rematches."""
    order = {team: index for index, team in enumerate(teams)}
    nodes = 0

    def cost(a, b):
        return round(abs(points[a] - points[b]) * 1000) + abs(order[a] - order[b])

    def search(free):
        nonlocal nodes
        if not free:
            return []
        nodes += 1
        if nodes > budget:
            return None
        first, rest = free[0], free[1:]
        for other in sorted((t for t in rest if frozenset((first, t)) not in history), key=lambda t: cost(first, t)):
            result = search([t for t in rest if t != other])
            if result is not None:
                return [(first, other)] + result
            if nodes > budget:
                return None
        return None
    return search(list(teams))


def swiss_pairs(standings, history, window=12):
    """Pair nearby scores, then remove repeat opponents with local pair swaps.

    Swaps are limited to a window of neighbouring pairs so a 1,000-team round
    stays linear instead of quadratic while the write lock is held. Fields of up to 40 teams are first tried with an
    exact search, which finds a rematch-free pairing whenever one exists."""
    teams = [row['team_id'] for row in standings]
    points = {row['team_id']: row['points'] for row in standings}
    if len(teams) <= 40:
        exact = pair_without_rematches(teams, points, history)
        if exact is not None:
            return exact
    positions = {team: index for index, team in enumerate(teams)}
    pairs = [(teams[i], teams[i+1]) for i in range(0, len(teams) - 1, 2)]

    def cost(a, b):
        return (1000000 if frozenset((a, b)) in history else 0) + \
               round(abs(points[a]-points[b])*1000) + abs(positions[a]-positions[b])

    for _ in range(4):
        changed = False
        for i in range(len(pairs)):
            for j in range(i+1, min(len(pairs), i+1+window)):
                a, b = pairs[i]
                c, d = pairs[j]
                before = cost(a, b) + cost(c, d)
                alternatives = [((a, c), (b, d)), ((a, d), (b, c))]
                best = min(alternatives, key=lambda candidate: sum(cost(*pair) for pair in candidate))
                if sum(cost(*pair) for pair in best) < before:
                    pairs[i], pairs[j] = best
                    changed = True
        if not changed:
            break
    return pairs


def bracket_order(size):
    """Seed numbers in bracket position order, e.g. 8 -> 1,8,4,5,2,7,3,6."""
    order = [1]
    while len(order) < size:
        total = len(order) * 2 + 1
        order = [seed for top in order for seed in (top, total - top)]
    return order


def pair_outcome(pair, games, needs_decider=False):
    """Result of a pair: two colour-swapped games, plus (knockout only) a third deciding game when they end level."""
    a, b = pair['a'], pair['b']
    if b is None:
        return {'complete': True, 'bye': True}
    if pair['forfeit']:
        return {'complete': True, 'forfeit': pair['forfeit']}
    if len(games) < 2 or any(g['status'] != 'done' for g in games):
        return {'complete': False}
    scores, material = {a: 0.0, b: 0.0}, {a: 0, b: 0}
    for g in games[:2]:
        first = g['owner1']
        second = b if first == a else a
        if g['winner'] == 'draw':
            scores[a] += .5
            scores[b] += .5
        else:
            scores[first if g['winner'] == 'p1' else second] += 1
        material[first] += g['p1_left'] or 0
        material[second] += g['p2_left'] or 0
    decider = None
    if needs_decider and scores[a] == scores[b]:
        if len(games) < 3:
            return {'complete': False, 'needs_decider': True}
        extra = games[2]
        if extra['winner'] != 'draw':
            first = extra['owner1']
            decider = first if extra['winner'] == 'p1' else (b if first == a else a)
    return {'complete': True, 'scores': scores, 'material': material, 'decider': decider}


def knockout_winner(pair, outcome, seeds):
    if not outcome['complete']:
        return None
    a, b = pair['a'], pair['b']
    if outcome.get('bye'):
        return a
    if outcome.get('forfeit'):
        return b if outcome['forfeit'] == a else a
    scores, material = outcome['scores'], outcome['material']
    if scores[a] != scores[b]:
        return a if scores[a] > scores[b] else b
    if outcome.get('decider'):
        return outcome['decider']
    if material[a] != material[b]:
        return a if material[a] > material[b] else b
    return min((a, b), key=lambda team: (seeds.get(team) or math.inf, team))


def pieces_left(state):
    counts = {'p1': 0, 'p2': 0}
    for line in state['board']:
        for piece in line:
            if piece:
                counts[piece['owner']] += 1
    return counts


class Store:
    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'arena.sqlite3'
        self.local = threading.local()
        # Final placements of completed tournaments never change; computing standings
        # for 1,000 teams on every badge lookup would not scale.
        self.placements, self.placement_lock = {}, threading.Lock()
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA foreign_keys=OFF')
            db.execute('BEGIN IMMEDIATE')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            legacy = 'user_id' in self.columns(db, 'submissions')
            if legacy:
                self._migrate_v1(db)
            for statement in SCHEMA:
                db.execute(statement)
            for table, definitions in ADDED_COLUMNS.items():
                existing = self.columns(db, table)
                for definition in definitions:
                    if definition.split()[0] not in existing:
                        db.execute(f'ALTER TABLE {table} ADD COLUMN {definition}')
            for key, value in DEFAULT_SETTINGS.items():
                db.execute('INSERT OR IGNORE INTO settings VALUES(?,?)', (key, value))
            if version < SCHEMA_VERSION:
                db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
            db.execute('COMMIT')
        except Exception:
            if db.in_transaction:
                db.execute('ROLLBACK')
            raise
        finally:
            db.close()

    @staticmethod
    def columns(db, table):
        return {row[1] for row in db.execute(f'PRAGMA table_info({table})')}

    def _migrate_v1(self, db):
        """Move a V1 database (one competitor per user) to teams, keeping every result."""
        for table in ('submissions', 'entries', 'pairings', 'matches', 'ratings', 'ranked_queue',
                      'ranked_series', 'workers'):
            if self.columns(db, table):
                db.execute(f'ALTER TABLE {table} RENAME TO _v1_{table}')
        additions = {
            'users': ['team_id TEXT REFERENCES teams(id)', 'student_id TEXT',
                      'disabled INTEGER NOT NULL DEFAULT 0', 'must_change INTEGER NOT NULL DEFAULT 0'],
            'jobs': ['priority INTEGER NOT NULL DEFAULT 2', 'started REAL', 'finished REAL', 'worker TEXT'],
            'tournaments': ['auto_advance INTEGER NOT NULL DEFAULT 0', 'seed_from TEXT', 'seed_count INTEGER',
                            "description TEXT NOT NULL DEFAULT ''"],
        }
        for table, definitions in additions.items():
            existing = self.columns(db, table)
            for definition in definitions:
                if definition.split()[0] not in existing:
                    db.execute(f'ALTER TABLE {table} ADD COLUMN {definition}')
        for statement in SCHEMA:
            db.execute(statement)
        used = set()
        users = db.execute("SELECT id,name,role FROM users ORDER BY created,id").fetchall()
        competitors = {row[0] for row in db.execute('SELECT user_id FROM _v1_submissions')}
        for user_id, name, role in users:
            if role != 'player' and user_id not in competitors:
                continue
            label, suffix = name, 2
            while name_key(label) in used:
                label, suffix = f'{name} ({suffix})', suffix + 1
            used.add(name_key(label))
            tid = uid()
            db.execute('INSERT INTO teams(id,name,name_key,code,owner,created) VALUES(?,?,?,?,?,?)',
                       (tid, label, name_key(label), team_code(), user_id, now()))
            db.execute('UPDATE users SET team_id=? WHERE id=?', (tid, user_id))
        team = '(SELECT team_id FROM users WHERE id={})'
        db.execute('INSERT INTO submissions(id,team_id,author,name,source,hash,status,log,created,origin) '
                   f"SELECT id,{team.format('user_id')},user_id,name,source,hash,status,log,created,'web' "
                   'FROM _v1_submissions')
        db.execute('INSERT INTO entries(tournament_id,team_id,submission_id,frozen_hash,status) '
                   f"SELECT tournament_id,{team.format('user_id')},submission_id,frozen_hash,'active' FROM _v1_entries")
        db.execute('INSERT INTO pairings(id,tournament_id,round,slot,a,b) '
                   f"SELECT id,tournament_id,round,rowid,{team.format('a')},"
                   f"CASE WHEN b IS NULL THEN NULL ELSE {team.format('b')} END FROM _v1_pairings")
        db.execute('INSERT INTO matches(id,pairing_id,p1,p2,requester,kind,status,winner,reason,replay,progress,'
                   'rules_hash,created) SELECT id,pairing_id,p1,p2,requester,'
                   "CASE WHEN pairing_id IS NOT NULL THEN 'tournament' "
                   "WHEN id IN (SELECT first_match FROM _v1_ranked_series UNION SELECT second_match FROM _v1_ranked_series) "
                   "THEN 'ranked' ELSE 'practice' END,status,winner,reason,replay,progress,rules_hash,created FROM _v1_matches")
        db.execute('INSERT INTO ratings SELECT ' + team.format('user_id') +
                   ',rating,played,wins,draws,losses FROM _v1_ratings')
        db.execute('INSERT INTO ranked_queue SELECT ' + team.format('user_id') +
                   ',submission_id,joined FROM _v1_ranked_queue')
        db.execute('INSERT INTO ranked_series(id,a,b,a_submission,b_submission,first_match,second_match,status,'
                   'a_points,rating_change,created,finished) '
                   f"SELECT id,{team.format('a')},{team.format('b')},a_submission,"
                   'b_submission,first_match,second_match,status,a_points,rating_change,created,finished '
                   'FROM _v1_ranked_series')
        for row in db.execute('SELECT id,replay FROM matches WHERE replay IS NOT NULL').fetchall():
            state = json.loads(row[1]).get('final_state')
            if state:
                left = pieces_left(state)
                db.execute('UPDATE matches SET p1_left=?,p2_left=? WHERE id=?', (left['p1'], left['p2'], row[0]))
        db.execute("UPDATE jobs SET priority=0 WHERE kind='validate'")
        db.execute("UPDATE jobs SET priority=COALESCE((SELECT CASE m.kind WHEN 'tournament' THEN 1 WHEN 'ranked' "
                   "THEN 2 ELSE 3 END FROM matches m WHERE m.id=jobs.ref),3) WHERE kind='game'")
        for table in ('ranked_series', 'ranked_queue', 'ratings', 'matches', 'pairings', 'entries',
                      'submissions', 'workers'):
            db.execute(f'DROP TABLE IF EXISTS _v1_{table}')
        db.execute('DROP INDEX IF EXISTS job_ref')
        db.execute('DROP INDEX IF EXISTS job_queue')
        problems = db.execute('PRAGMA foreign_key_check').fetchall()
        if problems:
            raise RuntimeError(f'Migration left {len(problems)} broken references; database unchanged.')
        db.execute('INSERT INTO audit(actor,action,detail,created) VALUES(?,?,?,?)',
                   ('migration', 'schema.v2', json.dumps({'teams': len(used)}), now()))

    def open(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=30000')
        # Safe with WAL: a power cut can lose the last commits but never corrupts the file.
        db.execute('PRAGMA synchronous=NORMAL')
        return db

    @contextmanager
    def connect(self, write=False):
        # Reuse one connection per thread; reopening (and checkpointing the WAL) on
        # every request dominated the cost of short transactions.
        pooled = getattr(self.local, 'db', None)
        nested = getattr(self.local, 'busy', False)
        if pooled is None or nested:
            db = self.open()
            if not nested:
                self.local.db = pooled = db
        else:
            db = pooled
        if db is pooled:
            self.local.busy = True
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            if db is pooled:
                db.close()
                self.local.db = None
            raise
        finally:
            if db is pooled:
                self.local.busy = False
            else:
                db.close()

    def close(self):
        db = getattr(self.local, 'db', None)
        if db is not None:
            db.close()
            self.local.db = None

    @staticmethod
    def audit(db, actor, action, detail):
        db.execute('INSERT INTO audit(actor,action,detail,created) VALUES(?,?,?,?)',
                   (actor, action, json.dumps(detail, ensure_ascii=False), now()))

    def rate(self, key, maximum, window=3600):
        with self.connect(True) as db:
            db.execute('DELETE FROM rate_limits WHERE start<?', (now() - 86400,))
            row = db.execute('SELECT * FROM rate_limits WHERE key=?', (key,)).fetchone()
            if not row or row['start'] + window < now():
                db.execute('INSERT OR REPLACE INTO rate_limits VALUES(?,?,1)', (key, now()))
            elif row['count'] >= maximum:
                raise Problem('Bạn thao tác quá nhanh. Hãy thử lại sau ít phút.', 429)
            else:
                db.execute('UPDATE rate_limits SET count=count+1 WHERE key=?', (key,))

    # ---- settings -------------------------------------------------------

    def settings(self, db=None):
        if db is None:
            with self.connect() as db:
                return self.settings(db)
        return {row['key']: row['value'] for row in db.execute('SELECT key,value FROM settings')}

    def set_settings(self, actor, values):
        with self.connect(True) as db:
            for key, value in values.items():
                if key not in DEFAULT_SETTINGS:
                    raise Problem(f'Không có cài đặt {key}.')
                if key == 'announcement':
                    value = str(value).strip()[:500]
                elif value not in (True, False, '0', '1', 0, 1):
                    raise Problem(f'Cài đặt {key} chỉ nhận bật/tắt.')
                else:
                    value = '1' if value in (True, '1', 1) else '0'
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, value))
            self.audit(db, actor, 'settings.updated', values)

    def require_open(self, db, key, message):
        if self.settings(db).get(key) != '1':
            raise Problem(message, 403)

    # ---- users and teams ------------------------------------------------

    @staticmethod
    def team_of(db, user):
        row = db.execute('SELECT t.* FROM users u JOIN teams t ON t.id=u.team_id WHERE u.id=?', (user,)).fetchone()
        return dict(row) if row else None

    def require_team(self, db, user):
        team = self.team_of(db, user)
        if not team:
            raise Problem('Bạn cần tạo đội hoặc vào đội bằng mã trước.', 409)
        if team['status'] != 'active':
            raise Problem('Đội đã bị truất quyền thi đấu. Liên hệ ban tổ chức.', 403)
        return team

    @staticmethod
    def team_members(db, team_id):
        return [dict(row) for row in db.execute(
            'SELECT id,username,name,student_id FROM users WHERE team_id=? ORDER BY created,id', (team_id,))]

    @staticmethod
    def insert_team(db, name, owner=None):
        label = ' '.join(str(name).split())
        if not 2 <= len(label) <= 40:
            raise Problem('Tên đội cần 2–40 ký tự.')
        if db.execute('SELECT 1 FROM teams WHERE name_key=?', (name_key(label),)).fetchone():
            raise Problem('Tên đội đã có người dùng. Hãy chọn tên khác.', 409)
        tid = uid()
        for _ in range(10):
            code = team_code()
            if not db.execute('SELECT 1 FROM teams WHERE code=?', (code,)).fetchone():
                break
        db.execute('INSERT INTO teams(id,name,name_key,code,owner,created) VALUES(?,?,?,?,?,?)',
                   (tid, label, name_key(label), code, owner, now()))
        return tid

    def create_team(self, user, name):
        with self.connect(True) as db:
            if self.team_of(db, user):
                raise Problem('Bạn đã ở trong một đội. Rời đội trước khi tạo đội mới.', 409)
            tid = self.insert_team(db, name, user)
            db.execute('UPDATE users SET team_id=? WHERE id=?', (tid, user))
            self.audit(db, user, 'team.created', {'team': tid})
            return tid

    def join_team(self, user, code):
        code = str(code or '').strip().upper()
        with self.connect(True) as db:
            if self.team_of(db, user):
                raise Problem('Bạn đã ở trong một đội. Rời đội trước khi vào đội khác.', 409)
            team = db.execute('SELECT * FROM teams WHERE code=?', (code,)).fetchone()
            if not team or team['status'] == 'system':
                raise Problem('Mã đội không đúng.', 404)
            if team['status'] != 'active':
                raise Problem('Đội này đã bị truất quyền thi đấu.', 403)
            count = db.execute('SELECT COUNT(*) FROM users WHERE team_id=?', (team['id'],)).fetchone()[0]
            if count >= TEAM_SIZE:
                raise Problem(f'Đội đã đủ {TEAM_SIZE} thành viên.', 409)
            db.execute('UPDATE users SET team_id=? WHERE id=?', (team['id'], user))
            if not team['owner'] or not count:
                db.execute('UPDATE teams SET owner=? WHERE id=?', (user, team['id']))
            self.audit(db, user, 'team.joined', {'team': team['id']})
            return team['id']

    def leave_team(self, user, actor=None):
        with self.connect(True) as db:
            return self._leave(db, user, actor or user)

    def _leave(self, db, user, actor):
        team = self.team_of(db, user)
        if not team:
            return False
        db.execute('UPDATE users SET team_id=NULL WHERE id=?', (user,))
        rest = db.execute('SELECT id FROM users WHERE team_id=? ORDER BY created,id', (team['id'],)).fetchall()
        if team['owner'] == user:
            db.execute('UPDATE teams SET owner=? WHERE id=?', (rest[0]['id'] if rest else None, team['id']))
        used = db.execute('SELECT 1 FROM submissions WHERE team_id=? UNION SELECT 1 FROM entries WHERE team_id=? '
                          'UNION SELECT 1 FROM ratings WHERE team_id=?', (team['id'],)*3).fetchone()
        if not rest:
            db.execute('DELETE FROM ranked_queue WHERE team_id=?', (team['id'],))   # nobody left to answer a match
            if not used:
                db.execute('DELETE FROM teams WHERE id=?', (team['id'],))
        self.audit(db, actor, 'team.left', {'team': team['id'], 'user': user})
        return True

    def _remove_team(self, db, team_id, actor):
        """Delete a team. A team that never took part in a tournament disappears completely, with its bots, its games and
        its ranked/boss history. A team that did is only hidden and renamed, so other teams' tournament results stay intact."""
        team = db.execute('SELECT * FROM teams WHERE id=?', (team_id,)).fetchone()
        db.execute('UPDATE users SET team_id=NULL WHERE team_id=?', (team_id,))
        db.execute('DELETE FROM ranked_queue WHERE team_id=?', (team_id,))
        in_tournament = db.execute('SELECT 1 FROM entries WHERE team_id=? UNION SELECT 1 FROM pairings WHERE a=? OR b=?',
                                   (team_id,) * 3).fetchone()
        if in_tournament:
            db.execute("UPDATE teams SET status='deleted',owner=NULL,name_key=?,code=? WHERE id=?",
                       (f"{team['name_key']}:deleted:{team_id}", f'DEL{team_id[:10].upper()}', team_id))
            mode = 'hidden'
        else:
            mine = 'SELECT id FROM submissions WHERE team_id=?'
            db.execute('DELETE FROM campaign_runs WHERE team_id=?', (team_id,))
            db.execute('DELETE FROM ranked_series WHERE a=? OR b=?', (team_id, team_id))
            db.execute(f"DELETE FROM jobs WHERE kind='game' AND ref IN (SELECT id FROM matches WHERE p1 IN ({mine}) "
                       f"OR p2 IN ({mine}))", (team_id, team_id))
            db.execute(f'DELETE FROM matches WHERE p1 IN ({mine}) OR p2 IN ({mine})', (team_id, team_id))
            db.execute(f"DELETE FROM jobs WHERE kind='validate' AND ref IN ({mine})", (team_id,))
            db.execute('DELETE FROM ratings WHERE team_id=?', (team_id,))
            db.execute('DELETE FROM submissions WHERE team_id=?', (team_id,))
            db.execute('DELETE FROM teams WHERE id=?', (team_id,))
            mode = 'deleted'
        self.audit(db, actor, 'team.deleted', {'team': team_id, 'name': team['name'], 'mode': mode})
        return mode

    def delete_team(self, team_id, actor):
        with self.connect(True) as db:
            team = db.execute('SELECT status FROM teams WHERE id=?', (team_id,)).fetchone()
            if not team or team['status'] == 'deleted':
                raise Problem('Không tìm thấy đội.', 404)
            if team['status'] == 'system':
                raise Problem('Đội boss do hệ thống quản lý.', 409)
            return {'mode': self._remove_team(db, team_id, actor)}

    def empty_teams(self, db):
        return db.execute("SELECT t.id,t.name FROM teams t WHERE t.status NOT IN ('system','deleted') AND NOT EXISTS "
                          '(SELECT 1 FROM users u WHERE u.team_id=t.id) ORDER BY t.created').fetchall()

    def cleanup_empty_teams(self, actor, confirm=False):
        """Preview (or, with confirm, delete) every team that has no members left."""
        with self.connect(True) as db:
            teams = self.empty_teams(db)
            if not confirm:
                return {'count': len(teams), 'names': [t['name'] for t in teams[:20]]}
            modes = [self._remove_team(db, t['id'], actor) for t in teams]
            return {'count': len(teams), 'deleted': modes.count('deleted'), 'hidden': modes.count('hidden')}

    def delete_user(self, user, actor, delete_team=False):
        """Remove an account for good. Bots, results and Elo stay with the team unless `delete_team` asks to remove
        the team too once its last member is gone."""
        with self.connect(True) as db:
            row = db.execute('SELECT * FROM users WHERE id=?', (user,)).fetchone()
            if not row:
                raise Problem('Không tìm thấy tài khoản.', 404)
            if user == actor:
                raise Problem('Không tự xóa tài khoản của chính mình.', 409)
            if row['role'] == 'admin' and not db.execute(
                    "SELECT 1 FROM users WHERE role='admin' AND disabled=0 AND id!=?", (user,)).fetchone():
                raise Problem('Không xóa được quản trị viên cuối cùng.', 409)
            team = self.team_of(db, user)
            self._leave(db, user, actor)
            db.execute('DELETE FROM sessions WHERE user_id=?', (user,))
            db.execute('DELETE FROM users WHERE id=?', (user,))
            self.audit(db, actor, 'user.deleted', {'user': user, 'username': row['username'], 'name': row['name'],
                                                   'role': row['role'], 'team': team['id'] if team else None})
            team_mode = None
            if delete_team and team and not db.execute('SELECT 1 FROM users WHERE team_id=?', (team['id'],)).fetchone() \
                    and db.execute('SELECT 1 FROM teams WHERE id=?', (team['id'],)).fetchone():
                team_mode = self._remove_team(db, team['id'], actor)
            kept = bool(team and db.execute("SELECT 1 FROM teams WHERE id=? AND status!='deleted'", (team['id'],)).fetchone())
            return {'ok': True, 'team_kept': kept, 'team_deleted': team_mode}

    def kick(self, owner, member):
        with self.connect() as db:
            team = self.team_of(db, owner)
            target = self.team_of(db, member)
        if not team or team['owner'] != owner:
            raise Problem('Chỉ trưởng nhóm được mời thành viên ra khỏi đội.', 403)
        if member == owner or not target or target['id'] != team['id']:
            raise Problem('Thành viên không thuộc đội của bạn.')
        return self.leave_team(member, owner)

    def rename_team(self, actor, team_id, name, admin=False):
        with self.connect(True) as db:
            team = db.execute('SELECT * FROM teams WHERE id=?', (team_id,)).fetchone()
            if not team or (not admin and team['owner'] != actor):
                raise Problem('Chỉ trưởng nhóm được đổi tên đội.', 403)
            label = ' '.join(str(name).split())
            if not 2 <= len(label) <= 40:
                raise Problem('Tên đội cần 2–40 ký tự.')
            clash = db.execute('SELECT id FROM teams WHERE name_key=?', (name_key(label),)).fetchone()
            if clash and clash['id'] != team_id:
                raise Problem('Tên đội đã có người dùng.', 409)
            db.execute('UPDATE teams SET name=?,name_key=? WHERE id=?', (label, name_key(label), team_id))
            self.audit(db, actor, 'team.renamed', {'team': team_id, 'name': label})

    def new_team_code(self, actor, team_id, admin=False):
        with self.connect(True) as db:
            team = db.execute('SELECT * FROM teams WHERE id=?', (team_id,)).fetchone()
            if not team or (not admin and team['owner'] != actor):
                raise Problem('Chỉ trưởng nhóm được đổi mã đội.', 403)
            code = team_code()
            db.execute('UPDATE teams SET code=? WHERE id=?', (code, team_id))
            self.audit(db, actor, 'team.code_changed', {'team': team_id})
            return code

    # ---- submissions ----------------------------------------------------

    @staticmethod
    def job(db, kind, ref, priority):
        db.execute('INSERT INTO jobs(id,kind,ref,priority,created) VALUES(?,?,?,?,?)',
                   (uid(), kind, ref, priority, now()))

    @staticmethod
    def clean_source(source):
        if not isinstance(source, str) or not 1 <= len(source.encode('utf-8')) <= LIMITS['source_bytes']:
            raise Problem('Mã Python phải có nội dung và không vượt quá 64 KiB.')
        if '\x00' in source:
            raise Problem('File Python chứa ký tự không hợp lệ.')
        source = source.replace('\r\n', '\n').replace('\r', '\n')
        # Code copied from web pages, chats and Word often carries characters Python rejects but nobody can see.
        source = source.translate(INVISIBLE_IN_CODE)
        if 'choose_move' not in source:
            raise Problem('Không thấy hàm choose_move(observation) trong mã.')
        return source

    def submit(self, team, author, name, source, origin='web', db=None, limit=True):
        source = self.clean_source(source)
        if db is None:
            with self.connect(True) as db:
                return self.submit(team, author, name, source, origin, db, limit)
        if limit:
            self.require_open(db, 'submissions_open', 'Ban tổ chức đang tạm đóng nộp bài.')
            pending = db.execute("SELECT COUNT(*) FROM submissions WHERE team_id=? AND status IN ('queued','running')",
                                 (team,)).fetchone()[0]
            if pending >= PENDING_PER_TEAM:
                raise Problem(f'Đội đang có {pending} bài chờ kiểm tra. Đợi kết quả rồi nộp tiếp.', 429)
        digest = hashlib.sha256(source.encode()).hexdigest()
        sid = uid()
        # Hundreds of freshmen submit the unchanged sample bots; reuse a finished check.
        known = db.execute("SELECT status,log FROM submissions WHERE hash=? AND checked=? "
                           "AND status IN ('valid','rejected') ORDER BY created DESC,rowid DESC LIMIT 1",
                           (digest, ENGINE_KEY)).fetchone()
        if known:
            note = ' (Dùng lại kết quả của bản có cùng mã.)'
            log = known['log'] if known['log'].endswith(note) else known['log'] + note
            db.execute('INSERT INTO submissions(id,team_id,author,name,source,hash,status,log,created,origin,checked) '
                       'VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                       (sid, team, author, name, source, digest, known['status'], log, now(), origin, ENGINE_KEY))
        else:
            # Hundreds of teams submit the same sample at the same minute: grade it once and let the
            # others wait for that result instead of queueing hundreds of identical checks.
            inflight = db.execute("SELECT 1 FROM submissions s JOIN jobs j ON j.kind='validate' AND j.ref=s.id "
                                  "WHERE s.hash=? AND j.status IN ('queued','running') LIMIT 1", (digest,)).fetchone()
            note = 'Đang dùng chung lượt kiểm tra của bản có cùng mã (đang chờ máy chấm).' if inflight else ''
            db.execute('INSERT INTO submissions(id,team_id,author,name,source,hash,log,created,origin) '
                       'VALUES(?,?,?,?,?,?,?,?,?)', (sid, team, author, name, source, digest, note, now(), origin))
            if not inflight:
                self.job(db, 'validate', sid, PRIORITY['validate'])
        self.audit(db, author or 'system', 'submission.created', {'id': sid, 'team': team, 'origin': origin})
        return sid

    # Validation first; everything else by priority, moving up a level for every AGING_SECONDS waited.
    EFFECTIVE = (f"CASE WHEN priority=0 THEN -10.0 ELSE priority - MIN({AGING_LEVELS}, (? - created)/{AGING_SECONDS}) END")

    @classmethod
    def queue_positions(cls, db, refs):
        positions, moment = {}, now()
        for ref in refs:
            job = db.execute(f"SELECT created,rowid rid,{cls.EFFECTIVE} eff FROM jobs WHERE ref=? AND status='queued'",
                             (moment, ref)).fetchone()
            if job:
                # Same order as claim(): effective priority, then creation time, then insertion order.
                positions[ref] = 1 + db.execute(
                    f"SELECT COUNT(*) FROM jobs WHERE status='queued' AND ({cls.EFFECTIVE}<? OR ({cls.EFFECTIVE}=? AND "
                    f"(created<? OR (created=? AND rowid<?))))",
                    (moment, job['eff'], moment, job['eff'], job['created'], job['created'], job['rid'])).fetchone()[0]
        return positions

    @staticmethod
    def check_capacity(db, kind):
        low = kind in ('practice', 'friendly', 'campaign')
        limit, level = (QUEUE_CAP_LOW, 3) if low else (QUEUE_CAP_RANKED, 2)
        waiting = db.execute("SELECT COUNT(*) FROM jobs WHERE kind='game' AND status='queued' AND priority=?",
                             (level,)).fetchone()[0]
        if waiting >= limit:
            raise Problem(f'Máy chấm đang quá tải ({waiting} ván đang chờ). Hãy thử lại sau ít phút.', 429)

    # ---- matches and ranked ladder ---------------------------------------

    @classmethod
    def create_match(cls, db, p1, p2, requester=None, pairing=None, kind='practice', map_name='standard'):
        mid = uid()
        db.execute('INSERT INTO matches(id,pairing_id,p1,p2,requester,kind,rules_hash,created,map) VALUES(?,?,?,?,?,?,?,?,?)',
                   (mid, pairing, p1, p2, requester, kind, RULES_HASH, now(), map_name))
        cls.job(db, 'game', mid, PRIORITY[kind])
        return mid

    def practice(self, team, actor, a, b, map_choice='standard'):
        name = resolve_map(map_choice)
        with self.connect(True) as db:
            self.require_open(db, 'practice_open', 'Đấu tập đang tạm đóng để ưu tiên giải đấu.')
            self.check_capacity(db, 'practice')
            for sid in (a, b):
                if not db.execute("SELECT 1 FROM submissions WHERE id=? AND team_id=? AND status='valid'",
                                  (sid, team)).fetchone():
                    raise Problem('Chọn hai bot đã vượt kiểm tra của đội bạn.')
            if db.execute("SELECT 1 FROM matches m JOIN submissions s ON s.id=m.p1 WHERE m.kind='practice' "
                          "AND s.team_id=? AND m.status IN ('queued','running')", (team,)).fetchone():
                raise Problem('Đội còn trận tập đang chờ/chạy.', 409)
            return [self.create_match(db, a, b, actor, map_name=name), self.create_match(db, b, a, actor, map_name=name)]

    def ranked_leaderboard(self, limit=50):
        with self.connect() as db:
            waiting = db.execute('SELECT COUNT(*) FROM ranked_queue').fetchone()[0]
            rows = db.execute('SELECT t.id team_id,t.name,r.rating,r.played,r.wins,r.draws,r.losses '
                              "FROM ratings r JOIN teams t ON t.id=r.team_id WHERE r.played>0 AND t.status='active' "
                              'ORDER BY r.rating DESC,r.played DESC,t.id LIMIT ?', (limit,)).fetchall()
        return {'waiting': waiting,
                'leaderboard': [{**dict(row), 'tier': elo_tier(row['rating'])} for row in rows]}

    def ranked_mine(self, team):
        with self.connect() as db:
            rating = db.execute('SELECT * FROM ratings WHERE team_id=?', (team,)).fetchone()
            queued = db.execute('SELECT q.submission_id,s.name bot,q.joined FROM ranked_queue q '
                                'JOIN submissions s ON s.id=q.submission_id WHERE q.team_id=?', (team,)).fetchone()
            value = rating['rating'] if rating else 1000
            position = None
            if rating and rating['played']:
                position = 1 + db.execute('SELECT COUNT(*) FROM ratings WHERE played>0 AND rating>?',
                                          (value,)).fetchone()[0]
            rows = db.execute('SELECT rs.*,ta.name a_name,tb.name b_name,sa.name a_bot,sb.name b_bot, '
                              'm1.status first_status,m2.status second_status '
                              'FROM ranked_series rs JOIN teams ta ON ta.id=rs.a JOIN teams tb ON tb.id=rs.b '
                              'JOIN submissions sa ON sa.id=rs.a_submission JOIN submissions sb ON sb.id=rs.b_submission '
                              'LEFT JOIN matches m1 ON m1.id=rs.first_match LEFT JOIN matches m2 ON m2.id=rs.second_match '
                              'WHERE rs.a=? OR rs.b=? ORDER BY rs.created DESC LIMIT 10', (team, team)).fetchall()
            series = [dict(row) for row in rows]
            pending = [m for s in series if s['status'] == 'running' for m in (s['first_match'], s['second_match'])]
            positions = self.queue_positions(db, pending)
        for item in series:
            item['positions'] = [positions.get(item['first_match']), positions.get(item['second_match'])]
        return {'rating': value, 'tier': elo_tier(value), 'played': rating['played'] if rating else 0,
                'position': position, 'queued': dict(queued) if queued else None, 'series': series}

    def join_ranked(self, team, sid, actor=None):
        with self.connect(True) as db:
            self.require_open(db, 'ranked_open', 'Đấu xếp hạng đang tạm đóng để ưu tiên giải đấu.')
            self.check_capacity(db, 'ranked')
            if not db.execute("SELECT 1 FROM submissions WHERE id=? AND team_id=? AND status='valid'",
                              (sid, team)).fetchone():
                raise Problem('Chỉ được ghép trận với bot hợp lệ của đội bạn.')
            if db.execute('SELECT 1 FROM ranked_queue WHERE team_id=?', (team,)).fetchone():
                raise Problem('Đội đã ở hàng chờ. Hủy tìm trận trước khi chọn bot khác.', 409)
            if db.execute("SELECT 1 FROM ranked_series WHERE status='running' AND (a=? OR b=?)", (team, team)).fetchone():
                raise Problem('Đội đang có cặp trận xếp hạng chưa hoàn tất.', 409)
            db.execute('INSERT OR IGNORE INTO ratings(team_id) VALUES(?)', (team,))
            rating = db.execute('SELECT rating FROM ratings WHERE team_id=?', (team,)).fetchone()[0]
            opponent = db.execute(
                'SELECT q.team_id,q.submission_id FROM ranked_queue q JOIN ratings r ON r.team_id=q.team_id '
                "JOIN teams t ON t.id=q.team_id WHERE q.team_id!=? AND t.status='active' AND NOT EXISTS "
                "(SELECT 1 FROM ranked_series rs WHERE rs.status='running' AND (rs.a=q.team_id OR rs.b=q.team_id)) "
                "ORDER BY (SELECT COUNT(*) FROM ranked_series recent WHERE recent.status='done' "
                'AND recent.finished>? AND ((recent.a=q.team_id AND recent.b=?) '
                'OR (recent.b=q.team_id AND recent.a=?))) ASC, '
                'ABS(r.rating-?) ASC,q.joined ASC,q.team_id ASC LIMIT 1',
                (team, now()-86400, team, team, rating)).fetchone()
            if not opponent:
                db.execute('INSERT INTO ranked_queue VALUES(?,?,?)', (team, sid, now()))
                self.audit(db, actor or team, 'ranked.queued', {'team': team, 'submission': sid})
                return {'status': 'queued'}
            db.execute('DELETE FROM ranked_queue WHERE team_id=?', (opponent['team_id'],))
            series_id = uid()
            db.execute('INSERT INTO ranked_series(id,a,b,a_submission,b_submission,created) VALUES(?,?,?,?,?,?)',
                       (series_id, opponent['team_id'], team, opponent['submission_id'], sid, now()))
            first = self.create_match(db, opponent['submission_id'], sid, kind='ranked')
            second = self.create_match(db, sid, opponent['submission_id'], kind='ranked')
            db.execute('UPDATE ranked_series SET first_match=?,second_match=? WHERE id=?', (first, second, series_id))
            self.audit(db, actor or team, 'ranked.matched', {'series': series_id, 'opponent': opponent['team_id']})
            return {'status': 'matched', 'series_id': series_id, 'matches': [first, second]}

    def cancel_ranked(self, team, actor=None):
        with self.connect(True) as db:
            removed = db.execute('DELETE FROM ranked_queue WHERE team_id=?', (team,)).rowcount
            if removed:
                self.audit(db, actor or team, 'ranked.cancelled', {'team': team})
            return bool(removed)

    @staticmethod
    def settle_ranked(db, match_id):
        series = db.execute("SELECT * FROM ranked_series WHERE status='running' "
                            'AND (first_match=? OR second_match=?)', (match_id, match_id)).fetchone()
        if not series:
            return
        games = db.execute('SELECT id,p1,winner,status FROM matches WHERE id IN (?,?)',
                           (series['first_match'], series['second_match'])).fetchall()
        if len(games) != 2 or any(game['status'] not in ('done', 'cancelled') for game in games):
            return
        if any(game['status'] == 'cancelled' for game in games):
            db.execute("UPDATE ranked_series SET status='cancelled',finished=? WHERE id=?", (now(), series['id']))
            return
        a_points = sum(.5 if game['winner'] == 'draw' else
                       1 if (game['winner'] == 'p1') == (game['p1'] == series['a_submission']) else 0
                       for game in games)
        ra = db.execute('SELECT rating FROM ratings WHERE team_id=?', (series['a'],)).fetchone()[0]
        rb = db.execute('SELECT rating FROM ratings WHERE team_id=?', (series['b'],)).fetchone()[0]
        expected = 1/(1+10**((rb-ra)/400))
        recent = db.execute("SELECT COUNT(*) FROM ranked_series WHERE status='done' AND finished>? "
                            'AND ((a=? AND b=?) OR (a=? AND b=?))',
                            (now()-86400, series['a'], series['b'], series['b'], series['a'])).fetchone()[0]
        change = round(max(8, 32//(recent+1))*(a_points/2-expected))
        result = 'draw' if a_points == 1 else 'a' if a_points > 1 else 'b'
        for team, delta, won in ((series['a'], change, result == 'a'), (series['b'], -change, result == 'b')):
            db.execute('UPDATE ratings SET rating=rating+?,played=played+1,wins=wins+?,draws=draws+?,losses=losses+? '
                       'WHERE team_id=?', (delta, int(won), int(result == 'draw'),
                                           int(result != 'draw' and not won), team))
        db.execute("UPDATE ranked_series SET status='done',a_points=?,rating_change=?,finished=?,a_rating=?,b_rating=? "
                   'WHERE id=?', (a_points, change, now(), ra, rb, series['id']))
        Store.audit(db, 'runner', 'ranked.settled', {'series': series['id'], 'a_points': a_points, 'change': change})

    # ---- campaign and friendlies ------------------------------------------

    def ensure_bosses(self):
        """Register the built-in ladder bots once, and again whenever a boss file changes."""
        with self.connect(True) as db:
            if not db.execute('SELECT 1 FROM teams WHERE id=?', (SYSTEM_TEAM,)).fetchone():
                label = 'Boss · Ban tổ chức'
                db.execute("INSERT INTO teams(id,name,name_key,code,owner,status,created) VALUES(?,?,?,?,NULL,'system',?)",
                           (SYSTEM_TEAM, label, name_key(label), team_code(), now()))
            for level, name, path, blurb in BOSSES:
                source = self.clean_source((ROOT / path).read_text(encoding='utf-8'))
                digest = hashlib.sha256(source.encode()).hexdigest()
                row = db.execute('SELECT s.hash FROM bosses b JOIN submissions s ON s.id=b.submission_id '
                                 'WHERE b.level=?', (level,)).fetchone()
                if row and row['hash'] == digest:
                    continue
                sid = self.submit(SYSTEM_TEAM, None, f'Boss {level} · {name}', source, 'boss', db, limit=False)
                db.execute('INSERT INTO bosses VALUES(?,?,?,?) ON CONFLICT(level) DO UPDATE SET '
                           'name=excluded.name,blurb=excluded.blurb,submission_id=excluded.submission_id',
                           (level, name, blurb, sid))

    @staticmethod
    def bosses(db):
        return [dict(row) for row in db.execute(
            'SELECT b.level,b.name,b.blurb,s.status FROM bosses b JOIN submissions s ON s.id=b.submission_id '
            'ORDER BY b.level')]

    @staticmethod
    def stars(points):
        if points is None or points < CLEAR_POINTS:
            return 0
        return 3 if points >= 2 else 2

    @classmethod
    def campaign_progress(cls, db, team):
        best = {row['level']: row['best'] for row in db.execute(
            "SELECT level,MAX(points) best FROM campaign_runs WHERE team_id=? AND status='done' GROUP BY level", (team,))}
        cleared = 0
        while (best.get(cleared + 1) or 0) >= CLEAR_POINTS:
            cleared += 1
        return {'best': best, 'cleared': cleared, 'stars': sum(cls.stars(points) for points in best.values())}

    def campaign_mine(self, team):
        with self.connect() as db:
            progress = self.campaign_progress(db, team)
            runs = [dict(row) for row in db.execute(
                'SELECT r.id,r.level,r.status,r.points,r.created,r.finished,r.first_match,r.second_match,s.name bot,'
                'b.name boss,m1.status first_status,m2.status second_status FROM campaign_runs r '
                'JOIN submissions s ON s.id=r.submission_id LEFT JOIN bosses b ON b.level=r.level '
                'LEFT JOIN matches m1 ON m1.id=r.first_match LEFT JOIN matches m2 ON m2.id=r.second_match '
                'WHERE r.team_id=? ORDER BY r.created DESC LIMIT 10', (team,))]
            pending = [m for r in runs if r['status'] == 'running' for m in (r['first_match'], r['second_match'])]
            positions = self.queue_positions(db, pending)
        for run in runs:
            run['stars'] = self.stars(run['points'])
            run['positions'] = [positions.get(run['first_match']), positions.get(run['second_match'])]
        return {**progress, 'runs': runs}

    def campaign_hall(self, limit=20):
        with self.connect() as db:
            rows = db.execute(
                'SELECT r.team_id,t.name,r.level,MAX(r.points) best,MIN(CASE WHEN r.points>=? THEN r.finished END) '
                "cleared_at FROM campaign_runs r JOIN teams t ON t.id=r.team_id WHERE r.status='done' "
                "AND t.status='active' GROUP BY r.team_id,r.level", (CLEAR_POINTS,)).fetchall()
            bosses = self.bosses(db)
        teams = {}
        for row in rows:
            item = teams.setdefault(row['team_id'], {'name': row['name'], 'best': {}, 'cleared_at': {}})
            item['best'][row['level']] = row['best']
            item['cleared_at'][row['level']] = row['cleared_at']
        hall = []
        for team, item in teams.items():
            cleared = 0
            while (item['best'].get(cleared + 1) or 0) >= CLEAR_POINTS:
                cleared += 1
            if cleared:
                hall.append({'team_id': team, 'name': item['name'], 'cleared': cleared,
                             'stars': sum(self.stars(v) for v in item['best'].values()),
                             'reached_at': item['cleared_at'][cleared]})
        hall.sort(key=lambda h: (-h['cleared'], -h['stars'], h['reached_at'], h['team_id']))
        return {'bosses': bosses, 'hall': hall[:limit], 'climbers': len(hall),
                'finishers': sum(h['cleared'] == len(bosses) for h in hall)}

    def challenge(self, team, actor, level, sid):
        with self.connect(True) as db:
            self.require_open(db, 'campaign_open', 'Chiến dịch đang tạm đóng để ưu tiên giải đấu.')
            self.check_capacity(db, 'campaign')
            boss = None
            if type(level) is int:
                boss = db.execute('SELECT b.*,s.status FROM bosses b JOIN submissions s ON s.id=b.submission_id '
                                  'WHERE b.level=?', (level,)).fetchone()
            if not boss:
                raise Problem('Không có ải này.', 404)
            if boss['status'] != 'valid':
                raise Problem('Boss của ải này đang được máy chấm kiểm tra. Thử lại sau ít phút.', 409)
            if not db.execute("SELECT 1 FROM submissions WHERE id=? AND team_id=? AND status='valid'",
                              (sid, team)).fetchone():
                raise Problem('Chọn bot đã vượt kiểm tra của đội bạn.')
            if level > self.campaign_progress(db, team)['cleared'] + 1:
                raise Problem(f'Hạ boss ải {level - 1} trước để mở ải này.', 403)
            if db.execute("SELECT 1 FROM campaign_runs WHERE team_id=? AND status='running'", (team,)).fetchone():
                raise Problem('Đội đang có một lượt thách đấu boss chưa xong.', 409)
            run = uid()
            first = self.create_match(db, sid, boss['submission_id'], actor, kind='campaign')
            second = self.create_match(db, boss['submission_id'], sid, actor, kind='campaign')
            db.execute('INSERT INTO campaign_runs(id,team_id,level,submission_id,boss_submission,first_match,'
                       'second_match,created) VALUES(?,?,?,?,?,?,?,?)',
                       (run, team, level, sid, boss['submission_id'], first, second, now()))
            self.audit(db, actor, 'campaign.challenge', {'team': team, 'level': level, 'run': run})
            return {'id': run, 'matches': [first, second]}

    @staticmethod
    def settle_campaign(db, match_id):
        run = db.execute("SELECT * FROM campaign_runs WHERE status='running' AND (first_match=? OR second_match=?)",
                         (match_id, match_id)).fetchone()
        if not run:
            return
        games = db.execute('SELECT p1,winner,status FROM matches WHERE id IN (?,?)',
                           (run['first_match'], run['second_match'])).fetchall()
        if len(games) != 2 or any(game['status'] not in ('done', 'cancelled') for game in games):
            return
        if any(game['status'] == 'cancelled' for game in games):
            db.execute("UPDATE campaign_runs SET status='cancelled',finished=? WHERE id=?", (now(), run['id']))
            return
        points = sum(.5 if game['winner'] == 'draw' else
                     1 if (game['winner'] == 'p1') == (game['p1'] == run['submission_id']) else 0 for game in games)
        db.execute("UPDATE campaign_runs SET status='done',points=?,finished=? WHERE id=?", (points, now(), run['id']))

    def friendly(self, team, actor, sid, opponent):
        with self.connect(True) as db:
            self.require_open(db, 'practice_open', 'Giao hữu đang tạm đóng để ưu tiên giải đấu.')
            self.check_capacity(db, 'friendly')
            if opponent == team:
                raise Problem('Hãy chọn một đội khác. Muốn so hai bot của đội, dùng Đấu tập.')
            if not db.execute("SELECT 1 FROM submissions WHERE id=? AND team_id=? AND status='valid'",
                              (sid, team)).fetchone():
                raise Problem('Chọn bot đã vượt kiểm tra của đội bạn.')
            other = db.execute("SELECT * FROM teams WHERE id=? AND status='active'", (opponent,)).fetchone()
            if not other:
                raise Problem('Không tìm thấy đội đối thủ.', 404)
            rival = db.execute("SELECT id FROM submissions WHERE team_id=? AND status='valid' "
                               'ORDER BY created DESC,rowid DESC LIMIT 1', (opponent,)).fetchone()
            if not rival:
                raise Problem(f'Đội {other["name"]} chưa có bot hợp lệ để giao hữu.', 409)
            if db.execute("SELECT 1 FROM matches m WHERE m.kind='friendly' AND m.status IN ('queued','running') "
                          'AND m.p1 IN (SELECT id FROM submissions WHERE team_id=?) AND m.requester IN '
                          '(SELECT id FROM users WHERE team_id=?)', (team, team)).fetchone():
                raise Problem('Đội còn trận giao hữu đang chờ/chạy.', 409)
            ids = [self.create_match(db, sid, rival['id'], actor, kind='friendly'),
                   self.create_match(db, rival['id'], sid, actor, kind='friendly')]
            self.audit(db, actor, 'friendly.created', {'team': team, 'opponent': opponent, 'matches': ids})
            return ids

    # ---- badges, profiles and the activity feed -------------------------

    def placement_table(self, db, tid):
        with self.placement_lock:
            if tid in self.placements:
                return self.placements[tid]
        value = {row['team_id']: (row['rank'], row['tier']) for row in self.standings(db, tid) if row.get('rank')}
        with self.placement_lock:
            self.placements[tid] = value
        return value

    @staticmethod
    def rating_history(db, team):
        """Rating after each finished ranked series, oldest first, rebuilt back from the current rating."""
        current = db.execute('SELECT rating FROM ratings WHERE team_id=?', (team,)).fetchone()
        rows = db.execute("SELECT a,b,a_points,rating_change,a_rating,b_rating,finished FROM ranked_series "
                          "WHERE status='done' AND (a=? OR b=?) ORDER BY finished DESC,id DESC", (team, team)).fetchall()
        rating, points = current['rating'] if current else 1000, []
        for row in rows:
            delta = row['rating_change'] if row['a'] == team else -row['rating_change']
            points.append({'time': row['finished'], 'rating': rating, 'delta': delta})
            rating -= delta
        points.append({'time': None, 'rating': rating, 'delta': 0})
        return points[::-1], [dict(row) for row in rows[::-1]]

    def achievements(self, db, team):
        count = lambda sql, *args: db.execute(sql, args).fetchone()[0]
        mine = '(SELECT id FROM submissions WHERE team_id=?)'
        wins = db.execute(
            "SELECT COUNT(*) n,MAX(m.reason='goal') goal,MAX(m.reason LIKE 'eliminate:%') wipe,"
            'MAX(m.plies<=40) blitz,MAX(CASE WHEN s1.team_id=? THEN m.p1_left ELSE m.p2_left END>=9) flawless '
            'FROM matches m JOIN submissions s1 ON s1.id=m.p1 JOIN submissions s2 ON s2.id=m.p2 '
            "WHERE m.status='done' AND m.kind!='practice' AND m.reason NOT LIKE 'technical:%' "
            f'AND (m.p1 IN {mine} OR m.p2 IN {mine}) '
            "AND ((m.winner='p1' AND s1.team_id=?) OR (m.winner='p2' AND s2.team_id=?))",
            (team, team, team, team, team)).fetchone()
        history, series = self.rating_history(db, team)
        streak = best_streak = 0
        upset = False
        for row in series:
            a_side = row['a'] == team
            points = row['a_points'] if a_side else 2 - row['a_points']
            streak = streak + 1 if points > 1 else 0
            best_streak = max(best_streak, streak)
            if points > 1 and row['a_rating'] is not None:
                own, rival = (row['a_rating'], row['b_rating']) if a_side else (row['b_rating'], row['a_rating'])
                upset = upset or rival - own >= 100
        peak = max(point['rating'] for point in history)
        campaign = self.campaign_progress(db, team)
        placements = []
        for row in db.execute("SELECT t.id FROM entries e JOIN tournaments t ON t.id=e.tournament_id "
                              "WHERE e.team_id=? AND t.status='completed'", (team,)).fetchall():
            placement = self.placement_table(db, row['id']).get(team)
            if placement:
                placements.append(placement)
        earned = {
            'first_bot': count("SELECT COUNT(*) FROM submissions WHERE team_id=? AND status='valid'", team) > 0,
            'inventor': count('SELECT COUNT(*) FROM submissions WHERE team_id=?', team) >= 5,
            'squad': count('SELECT COUNT(*) FROM users WHERE team_id=?', team) >= TEAM_SIZE,
            'first_win': wins['n'] > 0, 'goal': bool(wins['goal']), 'wipe': bool(wins['wipe']),
            'blitz': bool(wins['blitz']), 'flawless': bool(wins['flawless']),
            'streak': best_streak >= 3, 'upset': upset, 'gold': peak >= 1075, 'diamond': peak >= 1150,
            'boss1': campaign['cleared'] >= 1, 'boss_final': campaign['cleared'] >= len(BOSSES),
            'perfect': campaign['stars'] >= 3 * len(BOSSES),
            'podium': any(rank <= 3 for rank, _ in placements),
            'excellent': any(tier == 'Xuất sắc' for _, tier in placements),
        }
        return [{'key': key, 'icon': icon, 'title': title, 'detail': detail, 'earned': earned[key]}
                for key, icon, title, detail in BADGES]

    def team_badges(self, team):
        with self.connect() as db:
            return self.achievements(db, team)

    def team_profile(self, team):
        with self.connect() as db:
            row = db.execute('SELECT id,name,status,created FROM teams WHERE id=?', (team,)).fetchone()
            if not row or row['status'] in ('disqualified', 'deleted'):
                raise Problem('Không tìm thấy đội.', 404)
            system = row['status'] == 'system'
            rating = db.execute('SELECT * FROM ratings WHERE team_id=?', (team,)).fetchone()
            value = rating['rating'] if rating else 1000
            history, _ = self.rating_history(db, team)
            position = None
            if rating and rating['played']:
                position = 1 + db.execute('SELECT COUNT(*) FROM ratings WHERE played>0 AND rating>?',
                                          (value,)).fetchone()[0]
            bot = db.execute("SELECT name FROM submissions WHERE team_id=? AND status='valid' "
                             'ORDER BY created DESC,rowid DESC LIMIT 1', (team,)).fetchone()
            campaign = self.campaign_progress(db, team)
            return {
                **dict(row), 'system': system,
                'members': db.execute('SELECT COUNT(*) FROM users WHERE team_id=?', (team,)).fetchone()[0],
                'rating': value, 'tier': elo_tier(value), 'position': position,
                'record': {k: rating[k] if rating else 0 for k in ('played', 'wins', 'draws', 'losses')},
                'peak': max(point['rating'] for point in history), 'history': history[-40:],
                'bot': bot['name'] if bot else None,
                'campaign': {'cleared': campaign['cleared'], 'stars': campaign['stars'], 'levels': len(BOSSES)},
                'badges': [] if system else self.achievements(db, team),
            }

    def feed(self, limit=30):
        with self.connect() as db:
            events = []
            for row in db.execute(
                    'SELECT rs.finished,rs.a,rs.b,ta.name a_name,tb.name b_name,rs.a_points,rs.rating_change,'
                    'rs.a_rating,rs.b_rating,rs.first_match FROM ranked_series rs JOIN teams ta ON ta.id=rs.a '
                    "JOIN teams tb ON tb.id=rs.b WHERE rs.status='done' AND rs.a_points!=1 AND ta.status!='deleted' AND tb.status!='deleted' "
                    'ORDER BY rs.finished DESC LIMIT ?', (limit,)):
                winner, loser = ('a', 'b') if row['a_points'] > 1 else ('b', 'a')
                points = row['a_points'] if winner == 'a' else 2 - row['a_points']
                tags = ['sweep'] if points == 2 else []
                if row['a_rating'] is not None and row[f'{loser}_rating'] - row[f'{winner}_rating'] >= 100:
                    tags.append('upset')
                events.append({'time': row['finished'], 'kind': 'ranked', 'winner': row[winner],
                               'winner_name': row[f'{winner}_name'], 'loser': row[loser],
                               'loser_name': row[f'{loser}_name'], 'points': points,
                               'change': abs(row['rating_change']), 'match': row['first_match'], 'tags': tags})
            for row in db.execute(
                    'SELECT r.finished,r.team_id,t.name,r.level,r.points,b.name boss,r.first_match FROM campaign_runs r '
                    'JOIN teams t ON t.id=r.team_id LEFT JOIN bosses b ON b.level=r.level '
                    "WHERE r.status='done' AND r.points>=? ORDER BY r.finished DESC LIMIT ?", (CLEAR_POINTS, limit)):
                events.append({'time': row['finished'], 'kind': 'boss', 'winner': row['team_id'],
                               'winner_name': row['name'], 'level': row['level'], 'boss': row['boss'],
                               'stars': self.stars(row['points']), 'match': row['first_match'],
                               'tags': ['final'] if row['level'] == len(BOSSES) else []})
            for row in db.execute("SELECT detail,created FROM audit WHERE action='tournament.completed' "
                                  'ORDER BY created DESC LIMIT 5').fetchall():
                tid = json.loads(row['detail']).get('id')
                tournament = db.execute('SELECT name FROM tournaments WHERE id=?', (tid,)).fetchone()
                champions = sorted(team for team, (rank, _) in self.placement_table(db, tid).items() if rank == 1)
                if tournament and champions:
                    names = [r['name'] for r in db.execute(
                        f'SELECT name FROM teams WHERE id IN ({",".join("?" * len(champions))}) ORDER BY name',
                        champions)]
                    events.append({'time': row['created'], 'kind': 'champion', 'tournament': tid,
                                   'tournament_name': tournament['name'], 'winner': champions[0],
                                   'winner_name': ' & '.join(names), 'tags': ['champion']})
            stats = {
                'teams_ready': db.execute("SELECT COUNT(DISTINCT team_id) FROM submissions WHERE status='valid' "
                                          'AND team_id!=?', (SYSTEM_TEAM,)).fetchone()[0],
                'games_today': db.execute("SELECT COUNT(*) FROM matches WHERE status='done' AND created>?",
                                          (now() - 86400,)).fetchone()[0],
                'live': db.execute("SELECT COUNT(*) FROM matches WHERE status='running'").fetchone()[0],
                'boss_clears': db.execute("SELECT COUNT(*) FROM campaign_runs WHERE status='done' AND points>=?",
                                          (CLEAR_POINTS,)).fetchone()[0],
            }
        events.sort(key=lambda event: event['time'], reverse=True)
        return {'events': events[:limit], 'stats': stats}

    # ---- tournaments ----------------------------------------------------

    def create_tournament(self, actor, name, fmt, rounds=5, deadline=None, auto_advance=False,
                          description='', seed_from=None, seed_count=None, map_choice='standard', final_map=None):
        if fmt not in ('swiss', 'round_robin', 'knockout'):
            raise Problem('Thể thức không hợp lệ.')
        map_choice = resolve_map(map_choice, 'x') if map_choice != 'random' else 'random'
        final_map = (final_map if final_map == 'random' else resolve_map(final_map, 'x')) if final_map else None
        if type(rounds) is not int or not 1 <= rounds <= 30:
            raise Problem('Số vòng Swiss cần từ 1 đến 30.')
        tid = uid()
        with self.connect(True) as db:
            if seed_from:
                source = db.execute('SELECT * FROM tournaments WHERE id=?', (seed_from,)).fetchone()
                if not source or source['status'] != 'completed':
                    raise Problem('Chỉ lấy hạt giống từ giải đã hoàn tất.', 409)
                if type(seed_count) is not int or not 2 <= seed_count <= CAPACITY[fmt]:
                    raise Problem(f'Số đội đi tiếp cần từ 2 đến {CAPACITY[fmt]}.')
                ranked = [row for row in self.standings(db, seed_from) if row.get('rank')][:seed_count]
                if len(ranked) < 2:
                    raise Problem('Giải nguồn chưa có đủ hai đội được xếp hạng.', 409)
                db.execute('INSERT INTO tournaments(id,name,format,rounds,deadline,status,rules_hash,config,created,'
                           'auto_advance,seed_from,seed_count,description,map,final_map) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           (tid, name, fmt, rounds, now(), 'open', RULES_HASH, json.dumps(LIMITS), now(),
                            int(bool(auto_advance)), seed_from, seed_count, description, map_choice, final_map))
                for seed, row in enumerate(ranked, 1):
                    db.execute('INSERT INTO entries(tournament_id,team_id,submission_id,seed) VALUES(?,?,?,?)',
                               (tid, row['team_id'], row['submission_id'], seed))
                self.audit(db, actor, 'tournament.created', {'id': tid, 'seed_from': seed_from, 'count': len(ranked)})
            else:
                if type(deadline) not in (float, int) or not now()+60 <= deadline <= now()+86400*365:
                    raise Problem('Hạn đăng ký phải sau ít nhất một phút và trong một năm tới.')
                db.execute('INSERT INTO tournaments(id,name,format,rounds,deadline,rules_hash,config,created,'
                           'auto_advance,description,map,final_map) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                           (tid, name, fmt, rounds, deadline, RULES_HASH, json.dumps(LIMITS), now(),
                            int(bool(auto_advance)), description, map_choice, final_map))
                self.audit(db, actor, 'tournament.created', {'id': tid})
        if seed_from:
            self.freeze(tid, actor)
        return tid

    def update_tournament(self, tid, actor, name=None, description=None, deadline=None, rounds=None, map_choice=None,
                          final_map=False):
        """Rename, re-describe, move the deadline or change the Swiss round count (open tournaments only).
        The map may also change between rounds: it applies to rounds opened afterwards, never to games already made."""
        with self.connect(True) as db:
            t = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
            if not t:
                raise Problem('Không tìm thấy giải.', 404)
            changes = {}
            if name is not None:
                label = ' '.join(str(name).split())
                if not 3 <= len(label) <= 80:
                    raise Problem('Tên giải cần 3–80 ký tự.')
                changes['name'] = label
            if description is not None:
                changes['description'] = str(description).strip()[:500]
            if map_choice is not None:
                if t['status'] not in ('open', 'frozen', 'running'):
                    raise Problem('Giải đã kết thúc, không đổi bản đồ được.', 409)
                changes['map'] = map_choice if map_choice == 'random' else resolve_map(map_choice, 'x')
            if final_map is not False:        # None or '' clears the separate final-round map
                if t['status'] not in ('open', 'frozen', 'running'):
                    raise Problem('Giải đã kết thúc, không đổi bản đồ được.', 409)
                changes['final_map'] = (final_map if final_map == 'random' else resolve_map(final_map, 'x')) if final_map else None
            if deadline is not None or rounds is not None:
                if t['status'] != 'open':
                    raise Problem('Chỉ đổi hạn hoặc số vòng khi giải còn nhận đăng ký.', 409)
                if deadline is not None:
                    if type(deadline) not in (int, float) or not now() - 86400 <= deadline <= now() + 86400 * 365:
                        raise Problem('Hạn đăng ký phải nằm trong một năm tới.')
                    changes['deadline'] = float(deadline)
                if rounds is not None:
                    if type(rounds) is not int or not 1 <= rounds <= 30:
                        raise Problem('Số vòng Swiss cần từ 1 đến 30.')
                    changes['rounds'] = rounds
            if not changes:
                raise Problem('Không có thay đổi nào.')
            db.execute(f"UPDATE tournaments SET {','.join(f'{k}=?' for k in changes)} WHERE id=?", (*changes.values(), tid))
            self.audit(db, actor, 'tournament.updated', {'id': tid, **changes})

    def cancel_tournament(self, tid, actor):
        with self.connect(True) as db:
            t = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
            if not t:
                raise Problem('Không tìm thấy giải.', 404)
            if t['status'] in ('completed', 'cancelled'):
                raise Problem('Giải đã kết thúc hoặc đã hủy.', 409)
            games = db.execute(f"SELECT m.id FROM matches m JOIN pairings p ON p.id=m.pairing_id WHERE p.tournament_id=? "
                               f"AND m.status IN {UNFINISHED}", (tid,)).fetchall()
            for game in games:
                self.cancel_match(db, game['id'])
            db.execute("UPDATE tournaments SET status='cancelled',auto_advance=0 WHERE id=?", (tid,))
            self.audit(db, actor, 'tournament.cancelled', {'id': tid, 'games_cancelled': len(games)})
            return {'games_cancelled': len(games)}

    def admin_add_entry(self, tid, reference, actor):
        """Enter a team by id, join code or exact name using its newest usable bot (also after the deadline)."""
        reference = str(reference or '').strip()
        if not reference:
            raise Problem('Nhập mã đội hoặc tên đội.')
        with self.connect(True) as db:
            team = db.execute('SELECT * FROM teams WHERE id=? OR code=? OR name_key=?',
                              (reference, reference.upper(), name_key(reference))).fetchone()
            if not team or team['status'] in ('system', 'deleted'):
                raise Problem('Không tìm thấy đội.', 404)
            sub = db.execute("SELECT id FROM submissions WHERE team_id=? AND status IN ('valid','queued','running') "
                             "ORDER BY (status='valid') DESC,created DESC,rowid DESC LIMIT 1", (team['id'],)).fetchone()
            if not sub:
                raise Problem(f"Đội {team['name']} chưa có bot hợp lệ hoặc đang chờ kiểm tra.", 409)
            self.join(team['id'], tid, sub['id'], actor, db, admin=True)
            return {'team': team['name']}

    def admin_remove_entry(self, tid, team, actor):
        with self.connect(True) as db:
            t = db.execute('SELECT status FROM tournaments WHERE id=?', (tid,)).fetchone()
            if not t:
                raise Problem('Không tìm thấy giải.', 404)
            if t['status'] != 'open':
                raise Problem('Giải đã khóa danh sách. Dùng “Truất quyền” để loại đội.', 409)
            if not db.execute('DELETE FROM entries WHERE tournament_id=? AND team_id=?', (tid, team)).rowcount:
                raise Problem('Đội không có trong giải.', 404)
            self.audit(db, actor, 'entry.removed', {'tournament': tid, 'team': team})

    def set_auto_advance(self, tid, actor, value):
        with self.connect(True) as db:
            if not db.execute('UPDATE tournaments SET auto_advance=? WHERE id=?', (int(bool(value)), tid)).rowcount:
                raise Problem('Không tìm thấy giải.', 404)
            self.audit(db, actor, 'tournament.auto_advance', {'id': tid, 'value': bool(value)})
        if value:
            with self.connect(True) as db:
                self.maybe_advance(db, tid)

    def join(self, team, tid, sid, actor=None, db=None, admin=False):
        if db is None:
            with self.connect(True) as db:
                return self.join(team, tid, sid, actor, db, admin)
        t = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
        s = db.execute('SELECT * FROM submissions WHERE id=? AND team_id=?', (sid, team)).fetchone()
        # Organisers may still add entries after the deadline until the list is frozen.
        if not t or t['status'] != 'open' or (now() >= t['deadline'] and not admin) or t['seed_from']:
            raise Problem('Giải đã đóng đăng ký/chọn phiên bản.', 409)
        if not s or s['status'] not in ('queued', 'running', 'valid'):
            raise Problem('Chọn bot của đội bạn đã hợp lệ hoặc đang chờ kiểm tra.')
        if db.execute("SELECT status FROM teams WHERE id=?", (team,)).fetchone()[0] != 'active':
            raise Problem('Đội đã bị truất quyền thi đấu.', 403)
        count = db.execute('SELECT COUNT(*) FROM entries WHERE tournament_id=?', (tid,)).fetchone()[0]
        exists = db.execute('SELECT 1 FROM entries WHERE tournament_id=? AND team_id=?', (tid, team)).fetchone()
        if count >= CAPACITY[t['format']] and not exists:
            raise Problem('Giải đã đủ số lượng đăng ký.', 409)
        db.execute('INSERT INTO entries(tournament_id,team_id,submission_id) VALUES(?,?,?) '
                   'ON CONFLICT(tournament_id,team_id) DO UPDATE SET submission_id=excluded.submission_id',
                   (tid, team, sid))
        self.audit(db, actor or team, 'entry.selected', {'tournament': tid, 'team': team, 'submission': sid})

    def withdraw(self, team, tid, actor=None):
        with self.connect(True) as db:
            t = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
            if not t or t['status'] != 'open' or now() >= t['deadline']:
                raise Problem('Chỉ rút đăng ký khi giải còn mở.', 409)
            removed = db.execute('DELETE FROM entries WHERE tournament_id=? AND team_id=?', (tid, team)).rowcount
            if removed:
                self.audit(db, actor or team, 'entry.withdrawn', {'tournament': tid, 'team': team})
            return bool(removed)

    def freeze(self, tid, actor, force=False):
        with self.connect(True) as db:
            t = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
            if not t or t['status'] != 'open':
                raise Problem('Giải không ở trạng thái nhận bài.', 409)
            if now() < t['deadline']:
                raise Problem('Chưa tới hạn đóng bài; không khóa sớm so với lịch công bố.', 409)
            entries = db.execute('SELECT e.*,s.status bot_status,s.hash,tm.status team_status FROM entries e '
                                 'JOIN submissions s ON s.id=e.submission_id JOIN teams tm ON tm.id=e.team_id '
                                 'WHERE e.tournament_id=?', (tid,)).fetchall()
            pending = [e for e in entries if e['bot_status'] in ('queued', 'running')]
            if pending and not force:
                raise Problem(f'Còn {len(pending)} bot đăng ký đang chờ kiểm tra. Đợi runner xử lý xong, '
                              'hoặc khóa bắt buộc để loại các bot chưa có kết quả.', 409)
            accepted = [e for e in entries if e['bot_status'] == 'valid' and e['team_status'] == 'active']
            if len(accepted) < 2:
                raise Problem('Cần ít nhất hai bot đã vượt kiểm tra.')
            ratings = {r['team_id']: r['rating'] for r in db.execute('SELECT team_id,rating FROM ratings')}

            def order(entry):
                tiebreak = hashlib.sha256((tid + entry['team_id']).encode()).hexdigest()
                return (entry['seed'] or math.inf, -ratings.get(entry['team_id'], 1000), tiebreak)
            accepted.sort(key=order)
            for seed, e in enumerate(accepted, 1):
                db.execute("UPDATE entries SET frozen_hash=?,seed=?,status='active' WHERE tournament_id=? AND team_id=?",
                           (e['hash'], seed, tid, e['team_id']))
            chosen = {e['team_id'] for e in accepted}
            excluded = [e for e in entries if e['team_id'] not in chosen]
            for e in excluded:
                db.execute("UPDATE entries SET status='invalid',seed=NULL WHERE tournament_id=? AND team_id=?",
                           (tid, e['team_id']))
            n = len(accepted)
            rounds = {'swiss': min(t['rounds'], n - 1 + n % 2), 'round_robin': n - 1 + n % 2,
                      'knockout': max(1, math.ceil(math.log2(n)))}[t['format']]
            db.execute("UPDATE tournaments SET status='frozen',rounds=? WHERE id=?", (rounds, tid))
            self.audit(db, actor, 'tournament.frozen', {'id': tid, 'count': n, 'excluded': len(excluded)})
            return {'accepted': n, 'excluded': len(excluded)}

    @staticmethod
    def tournament_games(db, tid):
        games = defaultdict(list)
        for g in db.execute('SELECT m.pairing_id,m.status,m.winner,m.p1_left,m.p2_left,s.team_id owner1 '
                            'FROM matches m JOIN pairings p ON p.id=m.pairing_id JOIN submissions s ON s.id=m.p1 '
                            'WHERE p.tournament_id=? ORDER BY m.created,m.rowid', (tid,)):
            games[g['pairing_id']].append(g)
        return games

    @classmethod
    def standings(cls, db, tid):
        t = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
        if not t:
            return []
        rows = db.execute('SELECT e.team_id,e.submission_id,e.seed,e.status entry_status,tm.name,'
                          's.name bot,s.status bot_status FROM entries e JOIN teams tm ON tm.id=e.team_id '
                          'JOIN submissions s ON s.id=e.submission_id WHERE e.tournament_id=?', (tid,)).fetchall()
        stats = {r['team_id']: {**dict(r), 'points': 0, 'game_points': 0, 'wins': 0, 'draws': 0, 'losses': 0,
                                'byes': 0, 'buchholz': 0, 'reached': 0, 'eliminated': None} for r in rows}
        opponents = {team: [] for team in stats}
        pairs = db.execute('SELECT * FROM pairings WHERE tournament_id=? ORDER BY round,slot', (tid,)).fetchall()
        games = cls.tournament_games(db, tid)
        seeds = {team: row['seed'] for team, row in stats.items()}
        for pair in pairs:
            a, b = pair['a'], pair['b']
            outcome = pair_outcome(pair, games[pair['id']], t['format'] == 'knockout')
            for team in (a, b):
                if team in stats:
                    stats[team]['reached'] = max(stats[team]['reached'], pair['round'])
            if not outcome['complete']:
                continue
            if t['format'] == 'knockout':
                winner = knockout_winner(pair, outcome, seeds)
                for team in (a, b):
                    if team and team != winner and team in stats:
                        stats[team]['eliminated'] = pair['round']
            if outcome.get('bye'):
                stats[a]['points'] += 1
                stats[a]['byes'] += 1
                continue
            if outcome.get('forfeit'):
                loser = outcome['forfeit']
                winner = b if loser == a else a
                stats[winner]['points'] += 1
                stats[winner]['wins'] += 1
                stats[loser]['losses'] += 1
                continue
            scores = outcome['scores']
            for player, other in ((a, b), (b, a)):
                score = 1 if scores[player] > scores[other] else .5 if scores[player] == scores[other] else 0
                stats[player]['points'] += score
                stats[player]['game_points'] += scores[player]
                stats[player]['wins' if score == 1 else 'draws' if score == .5 else 'losses'] += 1
                opponents[player].append(other)
        for player, others in opponents.items():
            stats[player]['buchholz'] = sum(stats[o]['points'] for o in others)
        ranked = [s for s in stats.values() if s['entry_status'] == 'active']
        others = [s for s in stats.values() if s['entry_status'] != 'active']
        if t['format'] == 'knockout':
            def key(s):
                alive = s['eliminated'] is None
                return (0 if alive else 1, -(s['eliminated'] or 0), -s['reached'], s['seed'] or math.inf)
            group = lambda s: (s['eliminated'] is None, s['eliminated'], s['reached'] if s['eliminated'] is None else 0)
        else:
            def key(s):
                return (-s['points'], -s['buchholz'], -s['game_points'], s['seed'] or math.inf, s['team_id'])
            group = lambda s: (s['points'], s['buchholz'], s['game_points'])
        ranked.sort(key=key)
        previous, rank = None, 0
        for index, row in enumerate(ranked):
            if group(row) != previous:
                rank = index + 1
            row['rank'], previous = rank, group(row)
            share = (rank - 1) / len(ranked)
            played = row['wins'] + row['draws'] + row['losses'] + row['byes']
            row['tier'] = 'Chưa đạt' if played and row['points'] == 0 and row['game_points'] == 0 \
                else next(label for limit, label in TIERS if share < limit)
        labels = {'disqualified': 'Truất quyền', 'invalid': 'Bot không hợp lệ'}
        for row in others:
            row['rank'] = None
            row['tier'] = labels.get(row['entry_status'], 'Chưa xếp loại')
        others.sort(key=lambda s: (s['entry_status'], s['name']))
        return ranked + others

    @staticmethod
    def unfinished(db, tid):
        return db.execute(f'SELECT COUNT(*) FROM matches m JOIN pairings p ON p.id=m.pairing_id '
                          f'WHERE p.tournament_id=? AND m.status IN {UNFINISHED}', (tid,)).fetchone()[0]

    def next_round(self, tid, actor):
        with self.connect(True) as db:
            t = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
            if not t:
                raise Problem('Không tìm thấy giải.', 404)
            return self._next_round(db, t, actor)

    def _next_round(self, db, t, actor):
        tid = t['id']
        if t['status'] not in ('frozen', 'running'):
            raise Problem('Cần khóa danh sách trước khi mở vòng.', 409)
        if t['rules_hash'] != RULES_HASH:
            raise Problem('Engine đã thay đổi so với giải; cần dùng đúng phiên bản.', 409)
        if json.loads(t['config']) != LIMITS:
            raise Problem('Giới hạn runtime đã thay đổi so với thể lệ của giải.', 409)
        pending = self.unfinished(db, tid)
        if pending:
            raise Problem(f'Vòng trước còn {pending} ván chưa xong. Kiểm tra hàng đợi hoặc job lỗi.', 409)
        entries = db.execute('SELECT * FROM entries WHERE tournament_id=? AND seed IS NOT NULL ORDER BY seed',
                             (tid,)).fetchall()
        active = [e['team_id'] for e in entries if e['status'] == 'active']
        sids = {e['team_id']: e['submission_id'] for e in entries}
        next_number = t['round'] + 1
        pairs = []
        if t['format'] == 'knockout':
            if t['round'] >= 1:
                made = self._create_deciders(db, t, sids)
                if made:      # level pairs play one more game before the bracket can move on
                    return {'round': t['round'], 'deciders': made}
            size = 2 ** t['rounds']
            if t['round'] == 0:
                order = bracket_order(size)
                by_seed = {e['seed']: e['team_id'] for e in entries}
                slots = [by_seed.get(seed) for seed in order]
                winners = [team if team in active else None for team in slots]
            else:
                previous = db.execute('SELECT * FROM pairings WHERE tournament_id=? AND round=?',
                                      (tid, t['round'])).fetchall()
                games = self.tournament_games(db, tid)
                seeds = {e['team_id']: e['seed'] for e in entries}
                winners = [None] * (size // 2 ** (t['round'] - 1) // 2)
                for pair in previous:
                    winner = knockout_winner(pair, pair_outcome(pair, games[pair['id']], True), seeds)
                    winners[pair['slot']] = winner if winner in active else None
            if t['round'] >= t['rounds'] or sum(w is not None for w in winners) <= 1:
                return self._complete(db, tid, actor)
            for slot in range(len(winners) // 2):
                a, b = winners[2*slot], winners[2*slot+1]
                if a or b:
                    pairs.append((slot, a or b, b if a else None, None))
        else:
            if t['round'] >= t['rounds'] or len(active) < 2:
                return self._complete(db, tid, actor)
            if t['format'] == 'round_robin':
                ring = [e['team_id'] for e in entries]
                if len(ring) % 2:
                    ring.append(None)
                for _ in range(t['round']):
                    ring = [ring[0], ring[-1], *ring[1:-1]]
                for slot in range(len(ring) // 2):
                    a, b = ring[slot], ring[-slot-1]
                    if a is None or b is None:
                        team = a or b
                        if team in active:
                            pairs.append((slot, team, None, None))
                        continue
                    dropped = [team for team in (a, b) if team not in active]
                    if len(dropped) == 2:
                        continue
                    pairs.append((slot, a, b, dropped[0] if dropped else None))
            else:
                standings = [s for s in self.standings(db, tid) if s['team_id'] in active]
                waiting = [s['team_id'] for s in standings]
                if len(waiting) % 2:
                    bye = min(reversed(standings), key=lambda s: (s['byes'], s['points']))['team_id']
                    waiting.remove(bye)
                    pairs.append((len(waiting) // 2, bye, None, None))
                history = {frozenset((p['a'], p['b'])) for p in db.execute(
                    'SELECT a,b FROM pairings WHERE tournament_id=? AND b IS NOT NULL', (tid,))}
                chosen = swiss_pairs([s for s in standings if s['team_id'] in waiting], history)
                pairs.extend((slot, a, b, None) for slot, (a, b) in enumerate(chosen))
        for slot, a, b, forfeit in pairs:
            pid = uid()
            db.execute('INSERT INTO pairings(id,tournament_id,round,slot,a,b,forfeit) VALUES(?,?,?,?,?,?,?)',
                       (pid, tid, next_number, slot, a, b, forfeit))
            if b and not forfeit:
                for first, second in ((a, b), (b, a)):
                    self.create_match(db, sids[first], sids[second], pairing=pid, kind='tournament',
                                      map_name=resolve_map(self.round_map_choice(t, next_number), pid))
        db.execute("UPDATE tournaments SET status='running',round=? WHERE id=?", (next_number, tid))
        self.audit(db, actor, 'round.created', {'id': tid, 'round': next_number, 'pairs': len(pairs)})
        if all(b is None or forfeit for _, _, b, forfeit in pairs):
            # Nothing to play (only byes or forfeits); keep moving when automatic.
            self.maybe_advance(db, tid)
        return {'round': next_number, 'pairs': len(pairs)}

    @staticmethod
    def round_map_choice(t, number):
        """The map setting for a round: the final round may have its own map."""
        final = t['final_map']
        return final if final and number >= t['rounds'] else t['map']

    def _create_deciders(self, db, t, sids):
        """One extra game, colours drawn by the pair's id, for every pair of the current knockout round that ended level
        after two games. A new random formation is used when the map is random."""
        games = self.tournament_games(db, t['id'])
        made = 0
        for pair in db.execute('SELECT * FROM pairings WHERE tournament_id=? AND round=? ORDER BY slot',
                               (t['id'], t['round'])).fetchall():
            if not pair['b'] or pair['forfeit']:
                continue
            if not pair_outcome(pair, games[pair['id']], True).get('needs_decider'):
                continue
            first, second = (pair['a'], pair['b']) if int(pair['id'][-4:], 16) % 2 == 0 else (pair['b'], pair['a'])
            self.create_match(db, sids[first], sids[second], pairing=pair['id'], kind='tournament',
                              map_name=resolve_map(self.round_map_choice(t, t['round']), pair['id'][8:16]))
            made += 1
        if made:
            self.audit(db, 'system', 'round.deciders', {'id': t['id'], 'round': t['round'], 'games': made})
        return made

    def _complete(self, db, tid, actor):
        db.execute("UPDATE tournaments SET status='completed' WHERE id=?", (tid,))
        self.audit(db, actor, 'tournament.completed', {'id': tid})
        return {'completed': True}

    def maybe_advance(self, db, tid):
        t = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
        if not t or not t['auto_advance'] or t['status'] not in ('frozen', 'running') or self.unfinished(db, tid):
            return
        # A failure here must never roll back the game result that triggered it.
        db.execute('SAVEPOINT advance')
        try:
            self._next_round(db, t, 'auto')
        except Exception as exc:
            db.execute('ROLLBACK TO advance')
            self.audit(db, 'auto', 'round.auto_failed', {'id': tid, 'error': str(exc)[:500]})
        db.execute('RELEASE advance')

    def disqualify(self, tid, team, actor, reason=''):
        with self.connect(True) as db:
            entry = db.execute('SELECT * FROM entries WHERE tournament_id=? AND team_id=?', (tid, team)).fetchone()
            if not entry or entry['status'] != 'active':
                raise Problem('Đội không còn thi đấu trong giải này.', 409)
            db.execute("UPDATE entries SET status='disqualified' WHERE tournament_id=? AND team_id=?", (tid, team))
            current = db.execute(f'SELECT DISTINCT p.id FROM pairings p JOIN matches m ON m.pairing_id=p.id '
                                 f'WHERE p.tournament_id=? AND (p.a=? OR p.b=?) AND m.status IN {UNFINISHED}',
                                 (tid, team, team)).fetchall()
            for pair in current:
                db.execute('UPDATE pairings SET forfeit=? WHERE id=?', (team, pair['id']))
                for match in db.execute(f'SELECT id FROM matches WHERE pairing_id=? AND status IN {UNFINISHED}',
                                        (pair['id'],)).fetchall():
                    self.cancel_match(db, match['id'])
            self.audit(db, actor, 'entry.disqualified', {'tournament': tid, 'team': team, 'reason': reason[:300]})
            with self.placement_lock:
                self.placements.pop(tid, None)
            self.maybe_advance(db, tid)

    @staticmethod
    def cancel_match(db, mid):
        db.execute("UPDATE matches SET status='cancelled',progress=NULL WHERE id=?", (mid,))
        db.execute("UPDATE jobs SET status='cancelled',token=NULL,finished=? WHERE kind='game' AND ref=?", (now(), mid))
        Store.settle_ranked(db, mid)
        Store.settle_campaign(db, mid)

    def admin_cancel_match(self, mid, actor):
        with self.connect(True) as db:
            match = db.execute('SELECT status,pairing_id FROM matches WHERE id=?', (mid,)).fetchone()
            if not match:
                raise Problem('Không tìm thấy ván.', 404)
            if match['pairing_id']:
                raise Problem('Ván của giải đấu không hủy lẻ được. Dùng Truất quyền ở tab Giải đấu.', 409)
            if match['status'] not in ('queued', 'running', 'failed'):
                raise Problem('Chỉ hủy được ván đang chờ, đang chạy hoặc lỗi.', 409)
            self.cancel_match(db, mid)
            self.audit(db, actor, 'match.cancelled', {'id': mid})
            return {'ok': True}

    def admin_cancel_queued(self, actor, kind):
        """Clear a backlog of waiting non-tournament games, e.g. when the grader is overloaded."""
        if kind not in ('practice', 'friendly', 'campaign', 'ranked'):
            raise Problem('Chọn loại ván: practice, friendly, campaign hoặc ranked.')
        with self.connect(True) as db:
            ids = [r['id'] for r in db.execute("SELECT id FROM matches WHERE kind=? AND status='queued'", (kind,))]
            for mid in ids:
                self.cancel_match(db, mid)
            self.audit(db, actor, 'match.cancelled_queue', {'kind': kind, 'count': len(ids)})
            return len(ids)

    def set_team_status(self, team, actor, status, note=''):
        if status not in ('active', 'disqualified'):
            raise Problem('Trạng thái đội không hợp lệ.')
        with self.connect(True) as db:
            if not db.execute('UPDATE teams SET status=?,note=? WHERE id=?', (status, note[:300], team)).rowcount:
                raise Problem('Không tìm thấy đội.', 404)
            if status == 'disqualified':
                db.execute('DELETE FROM ranked_queue WHERE team_id=?', (team,))
            self.audit(db, actor, 'team.status', {'team': team, 'status': status, 'note': note[:300]})
        if status == 'disqualified':
            with self.connect() as db:
                running = [r['tournament_id'] for r in db.execute(
                    "SELECT e.tournament_id FROM entries e JOIN tournaments t ON t.id=e.tournament_id "
                    "WHERE e.team_id=? AND e.status='active' AND t.status IN ('frozen','running')", (team,))]
            for tid in running:
                self.disqualify(tid, team, actor, note)

    # ---- job queue ------------------------------------------------------

    def claim(self, worker, mode):
        with self.connect(True) as db:
            db.execute('INSERT OR REPLACE INTO workers VALUES(?,?,?)', (worker, now(), mode))
            expired = db.execute("SELECT * FROM jobs WHERE status='running' AND expires<?", (now(),)).fetchall()
            for job in expired:
                status = 'failed' if job['attempts'] >= 3 else 'queued'
                db.execute('UPDATE jobs SET status=?,token=NULL,error=? WHERE id=?',
                           (status, 'Runner mất kết nối; đã thu hồi lượt xử lý.', job['id']))
                self.mark(db, job, status, 'Chờ runner khôi phục.')
            row = db.execute(f"SELECT * FROM jobs WHERE status='queued' ORDER BY {self.EFFECTIVE},created,rowid LIMIT 1",
                             (now(),)).fetchone()
            if not row:
                return None
            token = uid()
            db.execute("UPDATE jobs SET status='running',token=?,expires=?,attempts=attempts+1,started=?,worker=? "
                       'WHERE id=?', (token, now() + 90, now(), worker, row['id']))
            self.mark(db, row, 'running')
            return {**dict(row), 'token': token, 'attempts': row['attempts'] + 1}

    @staticmethod
    def mark(db, job, status, log=None):
        if job['kind'] == 'game':
            db.execute('UPDATE matches SET status=?,progress=NULL WHERE id=?', (status, job['ref']))
        elif log is not None:
            db.execute('UPDATE submissions SET status=?,log=? WHERE id=?', (status, log, job['ref']))
        else:
            db.execute('UPDATE submissions SET status=? WHERE id=?', (status, job['ref']))
        if job['kind'] == 'validate' and status in ('failed', 'queued'):
            # Teams sharing this check follow the leader through an outage and its retry.
            before, after = ('queued', 'failed') if status == 'failed' else ('failed', 'queued')
            db.execute("UPDATE submissions SET status=?,log=? WHERE status=? AND NOT EXISTS (SELECT 1 FROM jobs j "
                       "WHERE j.kind='validate' AND j.ref=submissions.id) AND hash=(SELECT hash FROM submissions "
                       "WHERE id=?)", (after, (log or '')[:1000] if status == 'failed' else '', before, job['ref']))

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

    def match_live(self, mid):
        """Everything the live view needs from one row, parsed once."""
        with self.connect() as db:
            row = db.execute('SELECT status,progress,winner,reason FROM matches WHERE id=?', (mid,)).fetchone()
            if not row:
                raise Problem('Không tìm thấy ván.', 404)
            position = self.queue_positions(db, [mid]).get(mid) if row['status'] == 'queued' else None
        progress = json.loads(row['progress']) if row['progress'] else {}
        return {'status': row['status'], 'moves': progress.get('moves') or [], 'thinking': progress.get('thinking'),
                'left': progress.get('left'), 'position': position, 'winner': row['winner'], 'reason': row['reason']}

    def timing(self):
        """Average seconds per validation and per game over the last hour (None until there is data)."""
        with self.connect() as db:
            rows = db.execute("SELECT kind,AVG(finished-started) a FROM (SELECT kind,started,finished FROM jobs WHERE "
                              "status='done' AND started IS NOT NULL AND finished>? ORDER BY finished DESC LIMIT 400) "
                              "GROUP BY kind", (now() - 3600,)).fetchall()
        return {row['kind']: row['a'] for row in rows}

    def live_matches(self, team):
        """Games of this team that are running or waiting, newest first."""
        with self.connect() as db:
            rows = db.execute(
                'SELECT m.id,m.status,m.kind,m.created,t1.name team1,t2.name team2,t1.id team1_id,t2.id team2_id '
                'FROM matches m JOIN submissions s1 ON s1.id=m.p1 JOIN submissions s2 ON s2.id=m.p2 '
                'JOIN teams t1 ON t1.id=s1.team_id JOIN teams t2 ON t2.id=s2.team_id '
                "WHERE m.status IN ('running','queued') AND (s1.team_id=? OR s2.team_id=?) "
                "ORDER BY CASE m.status WHEN 'running' THEN 0 ELSE 1 END,m.created DESC LIMIT 6", (team, team)).fetchall()
            positions = self.queue_positions(db, [r['id'] for r in rows if r['status'] == 'queued'])
        return [{**dict(r), 'position': positions.get(r['id'])} for r in rows]

    def sibling(self, db, mid, kind, pairing, created, requester, p1, p2):
        """The other game of the same two-game pair, and whether this is game 1 or 2."""
        other = None
        if pairing:
            games = [r['id'] for r in db.execute('SELECT id FROM matches WHERE pairing_id=? ORDER BY created,rowid', (pairing,))]
            if mid not in games or len(games) < 2:
                return None, None
            at = games.index(mid)
            return games[at - 1 if at else 1], at + 1       # a third game is the knockout decider
        elif kind == 'ranked':
            row = db.execute('SELECT first_match,second_match FROM ranked_series WHERE first_match=? OR second_match=?',
                             (mid, mid)).fetchone()
            other = row and db.execute('SELECT id,created FROM matches WHERE id=?',
                                       (row['second_match'] if row['first_match'] == mid else row['first_match'],)).fetchone()
        elif kind == 'campaign':
            row = db.execute('SELECT first_match,second_match FROM campaign_runs WHERE first_match=? OR second_match=?',
                             (mid, mid)).fetchone()
            other = row and db.execute('SELECT id,created FROM matches WHERE id=?',
                                       (row['second_match'] if row['first_match'] == mid else row['first_match'],)).fetchone()
        elif requester:
            other = db.execute('SELECT id,created FROM matches WHERE kind=? AND requester=? AND p1=? AND p2=? '
                               'AND ABS(created-?)<10 AND id!=?', (kind, requester, p2, p1, created, mid)).fetchone()
        if not other:
            return None, None
        order = lambda row_id: db.execute('SELECT created,rowid FROM matches WHERE id=?', (row_id,)).fetchone()
        return other['id'], 1 if tuple(order(mid)) <= tuple(order(other['id'])) else 2

    def finish(self, job, result):
        with self.connect(True) as db:
            if not self.owns(db, job):
                return False
            if job['kind'] == 'validate':
                verdict = 'valid' if result['valid'] else 'rejected'
                db.execute('UPDATE submissions SET status=?,log=?,checked=? WHERE id=?',
                           (verdict, result['log'], ENGINE_KEY, job['ref']))
                db.execute("UPDATE submissions SET status=?,log=?,checked=? WHERE status IN ('queued','failed') "
                           "AND id!=? AND hash=(SELECT hash FROM submissions WHERE id=?) AND NOT EXISTS (SELECT 1 FROM "
                           "jobs j WHERE j.kind='validate' AND j.ref=submissions.id)",
                           (verdict, result['log'] + ' (Dùng chung kết quả của bản có cùng mã.)', ENGINE_KEY, job['ref'], job['ref']))
            else:
                left = pieces_left(result['final_state']) if result.get('final_state') else {'p1': None, 'p2': None}
                plies = len(result['moves']) if 'moves' in result else None
                db.execute("UPDATE matches SET status='done',winner=?,reason=?,replay=?,progress=NULL,"
                           'p1_left=?,p2_left=?,plies=? WHERE id=?',
                           (result['winner'], result['reason'], json.dumps(result, ensure_ascii=False),
                            left['p1'], left['p2'], plies, job['ref']))
            db.execute("UPDATE jobs SET status='done',token=NULL,expires=NULL,error='',finished=? WHERE id=?",
                       (now(), job['id']))
            if job['kind'] == 'game':
                self.settle_ranked(db, job['ref'])
                self.settle_campaign(db, job['ref'])
                pairing = db.execute('SELECT p.tournament_id FROM matches m JOIN pairings p ON p.id=m.pairing_id '
                                     'WHERE m.id=?', (job['ref'],)).fetchone()
                if pairing:
                    self.maybe_advance(db, pairing['tournament_id'])
            return True

    def fail(self, job, message, retry=True):
        with self.connect(True) as db:
            if not self.owns(db, job):
                return
            # Transient infrastructure errors are retried automatically, never turned into a bot loss.
            status = 'queued' if retry and job['attempts'] < 3 else 'failed'
            db.execute('UPDATE jobs SET status=?,error=?,token=NULL,expires=NULL WHERE id=?',
                       (status, message[:1000], job['id']))
            self.mark(db, job, status, None if status == 'queued' else message[:1000])

    def release(self, job):
        """Give a job back immediately when a runner shuts down cleanly."""
        with self.connect(True) as db:
            if not self.owns(db, job):
                return
            db.execute("UPDATE jobs SET status='queued',token=NULL,expires=NULL,attempts=MAX(attempts-1,0) WHERE id=?",
                       (job['id'],))
            self.mark(db, job, 'queued')

    def retry(self, jid, actor):
        with self.connect(True) as db:
            job = db.execute("SELECT * FROM jobs WHERE id=? AND status='failed'", (jid,)).fetchone()
            if not job:
                raise Problem('Chỉ thử lại job lỗi hạ tầng.', 409)
            db.execute("UPDATE jobs SET status='queued',attempts=0,error='',token=NULL WHERE id=?", (jid,))
            self.mark(db, job, 'queued')
            self.audit(db, actor, 'job.retried', {'id': jid})

    def retry_failed(self, actor):
        with self.connect(True) as db:
            jobs = db.execute("SELECT * FROM jobs WHERE status='failed'").fetchall()
            for job in jobs:
                db.execute("UPDATE jobs SET status='queued',attempts=0,error='',token=NULL WHERE id=?", (job['id'],))
                self.mark(db, job, 'queued')
            self.audit(db, actor, 'job.retried_all', {'count': len(jobs)})
            return len(jobs)

    def overview(self):
        with self.connect() as db:
            count = lambda sql, *args: db.execute(sql, args).fetchone()[0]
            jobs = defaultdict(dict)
            for row in db.execute('SELECT kind,status,COUNT(*) n FROM jobs GROUP BY kind,status'):
                jobs[row['kind']][row['status']] = row['n']
            games = defaultdict(dict)
            for row in db.execute("SELECT m.kind,m.status,COUNT(*) n FROM matches m GROUP BY m.kind,m.status"):
                games[row['kind']][row['status']] = row['n']
            submissions = {row['status']: row['n'] for row in
                           db.execute('SELECT status,COUNT(*) n FROM submissions GROUP BY status')}
            slots = db.execute('SELECT mode,COUNT(*) n FROM workers WHERE seen>? GROUP BY mode', (now()-40,)).fetchall()
            recent = db.execute("SELECT kind,finished-started d FROM jobs WHERE status='done' AND started IS NOT NULL "
                                'AND finished>? ORDER BY finished DESC LIMIT 400', (now()-3600,)).fetchall()
            durations = defaultdict(list)
            for row in recent:
                durations[row['kind']].append(row['d'])
            active_slots = sum(row['n'] for row in slots)
            queued = count("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')")
            average = {kind: sum(values)/len(values) for kind, values in durations.items()}
            backlog = sum(average.get(row['kind'], 20) for row in
                          db.execute("SELECT kind FROM jobs WHERE status IN ('queued','running')"))
            return {
                'users': count("SELECT COUNT(*) FROM users"),
                'players_without_team': count("SELECT COUNT(*) FROM users WHERE team_id IS NULL AND role='player'"),
                'teams': count("SELECT COUNT(*) FROM teams WHERE status!='system'"),
                'teams_with_valid_bot': count("SELECT COUNT(DISTINCT team_id) FROM submissions WHERE status='valid' "
                                              'AND team_id!=?', SYSTEM_TEAM),
                'sessions': count('SELECT COUNT(*) FROM sessions WHERE expires>?', now()),
                'submissions': submissions, 'jobs': jobs, 'games': games,
                'workers': {row['mode']: row['n'] for row in slots}, 'active_slots': active_slots,
                'finished_last_hour': len(recent), 'average_seconds': average, 'queued': queued,
                'eta_seconds': backlog / active_slots if active_slots else None,
                'ranked_waiting': count('SELECT COUNT(*) FROM ranked_queue'),
                'bosses': self.bosses(db),
                'campaign_running': count("SELECT COUNT(*) FROM campaign_runs WHERE status='running'"),
                'database_bytes': sum(path.stat().st_size for path in self.directory.glob('arena.sqlite3*')),
            }

    def backup(self, target):
        with self.connect() as source, sqlite3.connect(target) as dest:
            source.backup(dest)
