import concurrent.futures
import hashlib
import io
import json
import sqlite3
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from arena_backend import importer
from arena_backend.app import create_app
from arena_backend.store import (Store, ROOT, RULES_HASH, LIMITS, SCHEMA_VERSION, SYSTEM_TEAM, Problem, uid, now,
                                 swiss_pairs, bracket_order)
from arena_backend.runner import Worker, Executor, Referee, checked_move, BotFault, InfrastructureError

HUNTER = (ROOT/'sdk/examples/hunter.py').read_text(encoding='utf-8')
FIRST = (ROOT/'sdk/examples/first.py').read_text(encoding='utf-8')


class TrustedExecutor(Executor):
    """Runs any source on the host. Tests only: never reachable from the CLI."""
    def __init__(self):
        super().__init__('samples')

    def allowed(self, source):
        return True


class Base(unittest.TestCase):
    bosses = False

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(self.tmp.name, origin='http://localhost:8080', registration_code='test-invite',
                              bosses=self.bosses)
        self.app.testing = True
        self.store = self.app.extensions['store']
        self.client = self.app.test_client()
        self.csrf = ''

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def post(self, path, data, client=None, csrf=None):
        return (client or self.client).post('/api'+path, json=data,
                    headers={'Origin': 'http://localhost:8080', 'X-CSRF-Token': self.csrf if csrf is None else csrf})

    def register(self, username='alice', client=None):
        result = self.post('/register', {'username': username, 'name': username.title(),
                                         'password': 'test-only-password-123', 'invite': 'test-invite'}, client)
        self.assertEqual(result.status_code, 200, result.json)
        self.csrf = result.json['csrf']
        return result.json['user']['id']

    def user(self, name):
        user = uid()
        with self.store.connect(True) as db:
            db.execute("INSERT INTO users(id,username,name,password,role,created) VALUES(?,?,?,'unused','player',?)",
                       (user, name, name, now()))
        return user

    def team(self, name):
        user = self.user('u' + uid()[:12])
        return self.store.create_team(user, name), user

    def valid(self, team, source=HUNTER, name='bot'):
        sid = self.store.submit(team, None, name, source, limit=False)
        with self.store.connect(True) as db:
            db.execute("UPDATE submissions SET status='valid' WHERE id=?", (sid,))
            db.execute("UPDATE jobs SET status='done' WHERE ref=?", (sid,))
        return sid

    def tournament(self, fmt='swiss', rounds=2, auto=False):
        tid = uid()
        with self.store.connect(True) as db:
            db.execute('INSERT INTO tournaments(id,name,format,rounds,deadline,rules_hash,config,created,auto_advance) '
                       'VALUES(?,?,?,?,?,?,?,?,?)',
                       (tid, 'Test tournament', fmt, rounds, now()+500, RULES_HASH, json.dumps(LIMITS), now(), int(auto)))
        return tid

    def close_deadline(self, tid):
        with self.store.connect(True) as db:
            db.execute('UPDATE tournaments SET deadline=? WHERE id=?', (now()-1, tid))

    def entrants(self, tid, count, prefix='team'):
        teams = []
        for i in range(count):
            team, _ = self.team(f'{prefix} {i}')
            self.store.join(team, tid, self.valid(team))
            teams.append(team)
        return teams

    def finish_round(self, tid, winner_for=None):
        """Finish every queued tournament game; winner_for(match) returns 'p1'|'p2'|'draw'."""
        while True:
            job = self.store.claim('test', 'samples')
            if not job:
                return
            with self.store.connect() as db:
                match = db.execute('SELECT * FROM matches WHERE id=?', (job['ref'],)).fetchone()
            result = winner_for(match) if winner_for else 'draw'
            self.assertTrue(self.store.finish(job, {'winner': result, 'reason': 'goal'}))


class AccountsAndTeams(Base):
    def test_auth_csrf_privacy_and_static_allowlist(self):
        self.assertEqual(self.post('/register', {'username': 'alice', 'name': 'Alice', 'password': 'test-only-password-123',
                                                 'invite': 'bad'}).status_code, 403)
        self.register()
        cookie = self.client.get_cookie('ott_session')
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site, 'Lax')
        self.assertEqual(self.post('/submissions', {'name': 'bot', 'source': FIRST}).status_code, 409)
        self.assertEqual(self.post('/team', {'name': 'Đội Alice'}).status_code, 201)
        self.assertEqual(self.post('/submissions', {'name': 'bot', 'source': FIRST}, csrf='wrong').status_code, 403)
        self.assertEqual(self.client.post('/api/submissions', json={'name': 'bot', 'source': FIRST},
                         headers={'Origin': 'https://evil.example', 'X-CSRF-Token': self.csrf}).status_code, 403)
        self.assertEqual(self.post('/admin/tournaments', {}).status_code, 403)
        self.assertEqual(self.client.get('/api/admin/overview').status_code, 403)
        sid = self.post('/submissions', {'name': 'test', 'source': FIRST}).json['id']
        other = self.app.test_client()
        self.assertEqual(other.get('/api/submissions/'+sid+'/source').status_code, 401)
        alice_csrf = self.csrf
        self.register('mallory', other)
        self.csrf = alice_csrf
        self.assertEqual(other.get('/api/submissions/'+sid+'/source').status_code, 404)
        for path in ('/.env', '/arena-data/arena.sqlite3', '/arena_backend/app.py', '/.git/config',
                     '/arena/../../arena_backend/app.py', '/index.html', '/main.js'):
            self.assertEqual(self.client.get(path).status_code, 404, path)
        for path in ('/', '/arena/', '/arena/admin', '/arena/arena.js', '/arena/admin.js', '/arena/common.js',
                     '/rules.js'):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)
            response.close()
        self.post('/logout', {})
        self.assertIsNone(self.client.get('/api/me').json['user'])
        self.assertEqual(self.post('/login', {'username': 'alice', 'password': 'incorrect'}).status_code, 401)
        self.assertEqual(self.post('/login', {'username': 'alice', 'password': 'test-only-password-123'}).status_code, 200)

    def test_guests_only_reach_health_me_and_sign_in(self):
        for path in ('/tournaments', '/ranked', '/feed', '/campaign', '/matches', '/teams/x', '/submissions'):
            self.assertEqual(self.client.get('/api' + path).status_code, 401, path)
        self.assertEqual(self.client.get('/api/health').status_code, 200)
        self.assertEqual(self.client.get('/api/me').json['user'], None)
        self.register('visitor')
        self.assertEqual(self.client.get('/api/tournaments').status_code, 200)

    def test_campus_nat_allows_many_registrations_from_one_ip(self):
        for i in range(60):
            client = self.app.test_client()
            self.register(f'student{i}', client)

    def test_team_join_limits_owner_transfer_and_kick(self):
        owner, second = self.user('owner'), self.user('second')
        team = self.store.create_team(owner, 'Bàn Phím Gỗ')
        with self.assertRaises(Problem):
            self.store.create_team(second, 'bàn  phím gỗ')
        with self.store.connect() as db:
            code = db.execute('SELECT code FROM teams WHERE id=?', (team,)).fetchone()[0]
        with self.assertRaises(Problem):
            self.store.join_team(second, 'WRONG')
        self.store.join_team(second, code.lower())
        extra = [self.user(f'extra{i}') for i in range(3)]
        self.store.join_team(extra[0], code)
        self.store.join_team(extra[1], code)
        with self.assertRaises(Problem):
            self.store.join_team(extra[2], code)
        with self.assertRaises(Problem):
            self.store.kick(second, extra[0])
        self.store.kick(owner, extra[0])
        self.store.leave_team(owner)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT owner FROM teams WHERE id=?', (team,)).fetchone()[0], second)
        self.store.set_team_status(team, 'admin', 'disqualified', 'Gian lận')
        with self.assertRaises(Problem):
            self.store.join_team(extra[2], code)

    def test_empty_team_without_history_is_removed(self):
        user = self.user('solo')
        team = self.store.create_team(user, 'Tạm thời')
        self.store.leave_team(user)
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM teams WHERE id=?', (team,)).fetchone())

    def test_read_only_mode_blocks_players_but_not_admins(self):
        user = self.register()
        self.post('/team', {'name': 'Đội A'})
        self.store.set_settings('admin', {'read_only': True})
        self.app.extensions['cache'].clear()
        response = self.post('/submissions', {'name': 'bot', 'source': FIRST})
        self.assertEqual(response.status_code, 503)
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET role='admin' WHERE id=?", (user,))
        self.assertEqual(self.post('/admin/settings', {'read_only': False}).status_code, 200)
        self.assertEqual(self.post('/submissions', {'name': 'bot', 'source': FIRST}).status_code, 201)

    def test_admin_reset_password_and_disable(self):
        admin = self.register('organiser')
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET role='admin' WHERE id=?", (admin,))
        player = self.user('player')
        reset = self.post(f'/admin/users/{player}', {'action': 'reset_password'}).json
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT must_change FROM users WHERE id=?', (player,)).fetchone()[0], 1)
        other = self.app.test_client()
        login = other.post('/api/login', json={'username': 'player', 'password': reset['password']},
                           headers={'Origin': 'http://localhost:8080'})
        self.assertEqual(login.status_code, 200)
        self.post(f'/admin/users/{player}', {'action': 'disable'})
        self.assertIsNone(other.get('/api/me').json['user'])
        self.assertEqual(self.post(f'/admin/users/{admin}', {'action': 'disable'}).status_code, 409)


class Submissions(Base):
    def test_pending_limit_validation_cache_and_source_checks(self):
        team, user = self.team('Đội nộp')
        with self.assertRaises(Problem):
            self.store.submit(team, user, 'bot', 'print(1)')
        with self.assertRaises(Problem):
            self.store.submit(team, user, 'bot', 'def choose_move(o):\x00')
        for _ in range(3):
            self.store.submit(team, user, 'bot', FIRST + f'\n# {uid()}')
        with self.assertRaises(Problem) as caught:
            self.store.submit(team, user, 'bot', FIRST)
        self.assertEqual(caught.exception.status, 429)
        worker = Worker(self.store, Executor('samples'))
        other, other_user = self.team('Đội khác')
        first = self.store.submit(other, other_user, 'mẫu', HUNTER)
        while worker.run_once():
            pass
        copy = self.store.submit(other, other_user, 'mẫu 2', HUNTER)
        with self.store.connect() as db:
            rows = {r['id']: r for r in db.execute('SELECT * FROM submissions')}
            self.assertEqual(rows[first]['status'], 'valid')
            self.assertEqual(rows[copy]['status'], 'valid')
            self.assertIn('Dùng lại', rows[copy]['log'])
            self.assertIsNone(db.execute("SELECT 1 FROM jobs WHERE ref=?", (copy,)).fetchone())

    def test_queue_priority_validation_first_then_tournament_then_practice(self):
        team, user = self.team('Ưu tiên')
        a, b = self.valid(team), self.valid(team, FIRST)
        practice = self.store.practice(team, user, a, b)
        tid = self.tournament(rounds=1)
        self.entrants(tid, 2)
        self.close_deadline(tid)
        self.store.freeze(tid, 'admin')
        self.store.next_round(tid, 'admin')
        fresh = self.store.submit(team, user, 'mới', FIRST + '\n#x')
        order = []
        while True:
            job = self.store.claim('test', 'samples')
            if not job:
                break
            order.append(job['kind'] if job['kind'] == 'validate' else job['priority'])
            self.store.finish(job, {'valid': True, 'log': ''} if job['kind'] == 'validate'
                              else {'winner': 'draw', 'reason': 'quiet'})
        self.assertEqual(order, ['validate', 1, 1, 3, 3])
        self.assertTrue(practice and fresh)


class Runner(Base):
    def game(self, a_source, b_source):
        team, user = self.team('Thử nghiệm ' + uid()[:6])
        a, b = self.valid(team, a_source, 'a'), self.valid(team, b_source, 'b')
        with self.store.connect(True) as db:
            mid = self.store.create_match(db, a, b, requester=user)
        Worker(self.store, TrustedExecutor()).run_once()
        with self.store.connect() as db:
            return dict(db.execute('SELECT * FROM matches WHERE id=?', (mid,)).fetchone())

    def test_samples_validation_game_and_full_replay(self):
        team, user = self.team('Mẫu')
        sid = self.store.submit(team, user, 'hunter', HUNTER)
        worker = Worker(self.store, Executor('samples'))
        self.assertTrue(worker.run_once())
        with self.store.connect(True) as db:
            self.assertEqual(db.execute('SELECT status FROM submissions WHERE id=?', (sid,)).fetchone()[0], 'valid')
            mid = self.store.create_match(db, sid, sid, requester=user)
        self.assertTrue(worker.run_once())
        with self.store.connect() as db:
            match = db.execute('SELECT * FROM matches WHERE id=?', (mid,)).fetchone()
            self.assertEqual(match['status'], 'done', dict(match))
            replay = json.loads(match['replay'])
        self.assertIsNotNone(match['p1_left'])
        referee = Referee()
        try:
            packet = referee.call('new')
            for move in replay['moves']:
                packet = referee.call('move', **{k: move[k] for k in ('from', 'to')})
            self.assertEqual(packet['state'], replay['final_state'])
            self.assertLessEqual(len(replay['moves']), LIMITS['max_plies'])
        finally:
            referee.close()

    def test_persistent_bot_keeps_state_and_print_does_not_break_protocol(self):
        counting = ("calls = 0\n"
                    "def choose_move(o):\n"
                    "    global calls\n"
                    "    calls += 1\n"
                    "    print('debug', calls)\n"
                    "    assert calls == o['ply'] // 2 + 1, calls\n"
                    "    m = o['legalMoves'][0]\n"
                    "    return {'from': m['from'], 'to': m['to']}\n")
        match = self.game(counting, counting)
        self.assertEqual(match['status'], 'done')
        self.assertFalse(match['reason'].startswith('technical'), match['reason'])

    def test_bot_exception_and_bad_move_are_technical_losses(self):
        crash = "def choose_move(o):\n    raise ValueError('boom')\n"
        match = self.game(crash, FIRST)
        self.assertEqual((match['winner'], match['reason']), ('p2', 'technical:Bot lỗi thực thi'))
        self.assertNotIn('boom', match['replay'])
        illegal = "def choose_move(o):\n    return {'from': [0, 0], 'to': [9, 9]}\n"
        match = self.game(FIRST, illegal)
        self.assertEqual(match['winner'], 'p1')
        broken = "def choose_move(o)\n    pass\n"
        match = self.game(broken, broken)
        self.assertEqual((match['winner'], match['reason']), ('draw', 'technical:both'))

    def test_turn_timeout_is_a_technical_loss(self):
        slow = "import time\ndef choose_move(o):\n    time.sleep(10)\n"
        started = time.monotonic()
        match = self.game(FIRST, slow)
        self.assertEqual(match['winner'], 'p1')
        self.assertIn('giây/lượt', self.audit_text())
        self.assertLess(time.monotonic() - started, 9)

    def audit_text(self):
        with self.store.connect() as db:
            return ' '.join(r[0] for r in db.execute('SELECT detail FROM audit'))

    def test_validation_rejects_load_errors_with_team_visible_log(self):
        team, user = self.team('Lỗi nạp')
        sid = self.store.submit(team, user, 'bad', 'import does_not_exist\ndef choose_move(o): pass\n')
        Worker(self.store, TrustedExecutor()).run_once()
        with self.store.connect() as db:
            row = db.execute('SELECT status,log FROM submissions WHERE id=?', (sid,)).fetchone()
        self.assertEqual(row['status'], 'rejected')
        self.assertIn('ModuleNotFoundError', row['log'])

    def test_sample_mode_rejects_modified_code_without_retry(self):
        team, user = self.team('Sửa mẫu')
        sid = self.store.submit(team, user, 'custom', FIRST + '\n# changed')
        Worker(self.store, Executor('samples')).run_once()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM submissions WHERE id=?', (sid,)).fetchone()[0], 'failed')
        with self.assertRaises(BotFault):
            checked_move({'from': [True, 0], 'to': [1, 0]}, [])
        with self.assertRaises(BotFault):
            checked_move({'from': [0, 0], 'to': [1, 0], 'extra': 1}, [])

    def test_infrastructure_failures_retry_three_times_then_wait_for_admin(self):
        class BrokenExecutor(Executor):
            def spawn(self, source, seed, labels=None):
                raise InfrastructureError('Docker unavailable')
        team, user = self.team('Hạ tầng')
        sid = self.valid(team)
        with self.store.connect(True) as db:
            mid = self.store.create_match(db, sid, sid, user)
        worker = Worker(self.store, BrokenExecutor('samples'))
        for _ in range(3):
            self.assertTrue(worker.run_once())
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM matches WHERE id=?', (mid,)).fetchone()[0], 'failed')
            job = db.execute("SELECT id,attempts FROM jobs WHERE ref=?", (mid,)).fetchone()
        self.assertEqual(job['attempts'], 3)
        self.assertEqual(self.store.retry_failed('admin'), 1)

    def test_lease_fencing_duplicate_results_and_concurrent_claim(self):
        team, user = self.team('Lease')
        sid = self.store.submit(team, user, 'bot', FIRST + '\n#lease')
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            jobs = list(pool.map(lambda _: self.store.claim(uid(), 'samples'), range(4)))
        self.assertEqual(sum(j is not None for j in jobs), 1)
        old = next(j for j in jobs if j)
        with self.store.connect(True) as db:
            db.execute('UPDATE jobs SET expires=?', (now()-10,))
        new = self.store.claim('second', 'samples')
        self.assertNotEqual(old['token'], new['token'])
        self.assertFalse(self.store.finish(old, {'valid': False, 'log': 'stale'}))
        self.assertTrue(self.store.finish(new, {'valid': True, 'log': 'ok'}))
        self.assertFalse(self.store.finish(new, {'valid': False, 'log': 'duplicate'}))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM submissions WHERE id=?', (sid,)).fetchone()[0], 'valid')

    def test_parallel_slots_grade_several_games(self):
        team, user = self.team('Song song')
        sid = self.valid(team)
        with self.store.connect(True) as db:
            mids = [self.store.create_match(db, sid, sid, user) for _ in range(4)]
        worker = Worker(self.store, Executor('samples'), slots=3)
        import threading
        thread = threading.Thread(target=worker.run_forever)
        thread.start()
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            with self.store.connect() as db:
                done = db.execute("SELECT COUNT(*) FROM matches WHERE status='done'").fetchone()[0]
            if done == 4:
                break
            time.sleep(.3)
        worker.stop.set()
        thread.join(timeout=60)
        self.assertEqual(done, 4, mids)


class Tournaments(Base):
    def test_snapshot_deadline_ownership_and_pairing(self):
        (a, _), (b, _) = self.team('Alice'), self.team('Bob')
        sa, sb = self.valid(a), self.valid(b)
        tid = self.tournament(rounds=1)
        with self.assertRaises(Problem):
            self.store.join(a, tid, sb)
        self.store.join(a, tid, sa)
        self.store.join(b, tid, sb)
        with self.assertRaises(Problem):
            self.store.freeze(tid, 'admin')
        self.close_deadline(tid)
        with self.assertRaises(Problem):
            self.store.join(a, tid, sa)
        self.store.freeze(tid, 'admin')
        with self.assertRaises(Problem):
            self.store.join(a, tid, sa)
        with self.store.connect(True) as db:
            db.execute('UPDATE tournaments SET config=? WHERE id=?', (json.dumps({**LIMITS, 'turn_seconds': 9}), tid))
        with self.assertRaises(Problem):
            self.store.next_round(tid, 'admin')
        with self.store.connect(True) as db:
            db.execute('UPDATE tournaments SET config=? WHERE id=?', (json.dumps(LIMITS), tid))
        self.store.next_round(tid, 'admin')
        with self.store.connect() as db:
            games = db.execute('SELECT * FROM matches ORDER BY id').fetchall()
            self.assertEqual({(g['p1'], g['p2']) for g in games}, {(sa, sb), (sb, sa)})
        with self.assertRaises(Problem):
            self.store.next_round(tid, 'admin')
        self.finish_round(tid, lambda m: 'p1')
        self.assertEqual(self.store.next_round(tid, 'admin'), {'completed': True})
        with self.store.connect() as db:
            scores = self.store.standings(db, tid)
        self.assertEqual([s['points'] for s in scores], [.5, .5])
        self.assertEqual([s['rank'] for s in scores], [1, 1])

    def test_freeze_excludes_invalid_bots_and_waits_for_pending_checks(self):
        tid = self.tournament(rounds=1)
        good = self.entrants(tid, 3)
        bad, bad_user = self.team('Lỗi')
        broken = self.store.submit(bad, bad_user, 'x', FIRST + '\n#bad', limit=False)
        self.store.join(bad, tid, broken)
        self.close_deadline(tid)
        with self.assertRaises(Problem) as caught:
            self.store.freeze(tid, 'admin')
        self.assertIn('đang chờ kiểm tra', str(caught.exception))
        with self.store.connect(True) as db:
            db.execute("UPDATE submissions SET status='rejected' WHERE id=?", (broken,))
        self.assertEqual(self.store.freeze(tid, 'admin'), {'accepted': 3, 'excluded': 1})
        with self.store.connect() as db:
            rows = self.store.standings(db, tid)
        self.assertEqual(rows[-1]['tier'], 'Bot không hợp lệ')
        self.assertEqual({r['team_id'] for r in rows if r['rank']}, set(good))

    def test_round_robin_has_every_pair_once_and_balanced_byes(self):
        tid = self.tournament('round_robin')
        self.entrants(tid, 5)
        self.close_deadline(tid)
        self.store.freeze(tid, 'admin')
        for _ in range(5):
            self.store.next_round(tid, 'admin')
            self.finish_round(tid)
        with self.store.connect() as db:
            pairs = db.execute('SELECT a,b FROM pairings WHERE tournament_id=?', (tid,)).fetchall()
            real = [frozenset(p) for p in pairs if p['b']]
            self.assertEqual(len(real), 10)
            self.assertEqual(len(set(real)), 10)
            self.assertEqual(len({p['a'] for p in pairs if p['b'] is None}), 5)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM matches').fetchone()[0], 20)

    def test_auto_advance_runs_swiss_to_completion(self):
        tid = self.tournament(rounds=3, auto=True)
        self.entrants(tid, 6)
        self.close_deadline(tid)
        self.store.freeze(tid, 'admin')
        self.store.next_round(tid, 'admin')
        for _ in range(3):
            self.finish_round(tid, lambda m: 'p1' if m['p1'] < m['p2'] else 'draw')
        with self.store.connect() as db:
            t = db.execute('SELECT status,round FROM tournaments WHERE id=?', (tid,)).fetchone()
            rows = self.store.standings(db, tid)
        self.assertEqual(tuple(t), ('completed', 3))
        self.assertEqual(rows[0]['tier'], 'Xuất sắc')
        self.assertTrue(all(r['rank'] for r in rows))

    def test_knockout_bracket_byes_tiebreaks_and_champion(self):
        self.assertEqual(bracket_order(8), [1, 8, 4, 5, 2, 7, 3, 6])
        tid = self.tournament('knockout')
        teams = self.entrants(tid, 5, 'ko')
        self.close_deadline(tid)
        self.store.freeze(tid, 'admin')
        with self.store.connect() as db:
            seeds = {r['team_id']: r['seed'] for r in db.execute('SELECT * FROM entries WHERE tournament_id=?', (tid,))}
            self.assertEqual(db.execute('SELECT rounds FROM tournaments WHERE id=?', (tid,)).fetchone()[0], 3)
        self.store.next_round(tid, 'admin')
        with self.store.connect() as db:
            byes = [p['a'] for p in db.execute('SELECT * FROM pairings WHERE tournament_id=? AND b IS NULL', (tid,))]
        self.assertEqual(sorted(seeds[t] for t in byes), [1, 2, 3])
        # Draw every game: equal score and equal material, so the higher seed advances.
        for _ in range(3):
            self.finish_round(tid)
            self.store.next_round(tid, 'admin')
        with self.store.connect() as db:
            rows = self.store.standings(db, tid)
            status = db.execute('SELECT status FROM tournaments WHERE id=?', (tid,)).fetchone()[0]
        self.assertEqual(status, 'completed')
        champion = next(t for t in teams if seeds[t] == 1)
        self.assertEqual((rows[0]['team_id'], rows[0]['rank']), (champion, 1))
        self.assertEqual(rows[1]['rank'], 2)

    def test_knockout_seeded_from_swiss_top_teams(self):
        tid = self.tournament(rounds=1)
        self.entrants(tid, 6, 'swiss')
        self.close_deadline(tid)
        self.store.freeze(tid, 'admin')
        self.store.next_round(tid, 'admin')
        self.finish_round(tid, lambda m: 'p1')
        self.store.next_round(tid, 'admin')
        ko = self.store.create_tournament('admin', 'Chung kết', 'knockout', seed_from=tid, seed_count=4)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM tournaments WHERE id=?', (ko,)).fetchone()[0], 'frozen')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM entries WHERE tournament_id=?', (ko,)).fetchone()[0], 4)
        self.store.next_round(ko, 'admin')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM matches m JOIN pairings p ON p.id=m.pairing_id '
                                        'WHERE p.tournament_id=?', (ko,)).fetchone()[0], 4)

    def test_disqualification_forfeits_open_pairings_and_auto_advances(self):
        tid = self.tournament(rounds=2, auto=True)
        teams = self.entrants(tid, 4)
        self.close_deadline(tid)
        self.store.freeze(tid, 'admin')
        self.store.next_round(tid, 'admin')
        cheat = teams[0]
        self.store.set_team_status(cheat, 'admin', 'disqualified', 'Chép mã')
        with self.store.connect() as db:
            cancelled = db.execute("SELECT COUNT(*) FROM matches WHERE status='cancelled'").fetchone()[0]
        self.assertEqual(cancelled, 2)
        self.finish_round(tid)
        with self.store.connect() as db:
            t = db.execute('SELECT round FROM tournaments WHERE id=?', (tid,)).fetchone()[0]
            second = db.execute('SELECT a,b FROM pairings WHERE tournament_id=? AND round=2', (tid,)).fetchall()
            rows = self.store.standings(db, tid)
        self.assertEqual(t, 2)
        self.assertTrue(all(cheat not in (p['a'], p['b']) for p in second))
        self.assertEqual(rows[-1]['tier'], 'Truất quyền')

    def test_1000_team_swiss_round_is_fast(self):
        tid = self.tournament(rounds=9)
        digest = hashlib.sha256(FIRST.encode()).hexdigest()
        with self.store.connect(True) as db:
            for i in range(1000):
                team, sid = uid(), uid()
                db.execute('INSERT INTO teams(id,name,name_key,code,created) VALUES(?,?,?,?,?)',
                           (team, f'Team {i}', f'team {i}', f'C{i:07d}', now()))
                db.execute("INSERT INTO submissions(id,team_id,name,source,hash,status,created) "
                           "VALUES(?,?,?,?,?,'valid',?)", (sid, team, 'Bot', FIRST, digest, now()))
                db.execute('INSERT INTO entries(tournament_id,team_id,submission_id) VALUES(?,?,?)', (tid, team, sid))
        self.close_deadline(tid)
        self.store.freeze(tid, 'admin')
        started = time.monotonic()
        self.store.next_round(tid, 'admin')
        self.finish_round(tid, lambda m: 'p1')
        self.store.next_round(tid, 'admin')
        elapsed = time.monotonic() - started
        with self.store.connect() as db:
            pairs = db.execute('SELECT a,b FROM pairings WHERE tournament_id=? AND round=2', (tid,)).fetchall()
            begin = time.monotonic()
            self.store.standings(db, tid)
            standings_time = time.monotonic() - begin
        self.assertEqual(len(pairs), 500)
        self.assertEqual(len({team for pair in pairs for team in pair}), 1000)
        self.assertLess(standings_time, 3)
        self.assertLess(elapsed, 120)

    def test_swiss_pairing_swaps_to_avoid_rematch_without_large_score_gap(self):
        standings = [{'team_id': str(i), 'points': 4-i//2} for i in range(6)]
        history = {frozenset(('0', '1'))}
        pairs = swiss_pairs(standings, history)
        self.assertEqual({team for pair in pairs for team in pair}, {str(i) for i in range(6)})
        self.assertTrue(all(frozenset(pair) not in history for pair in pairs))
        self.assertLessEqual(sum(abs(standings[int(a)]['points']-standings[int(b)]['points']) for a, b in pairs), 2)

    def test_http_tournament_flow_export_and_personal_entry(self):
        admin = self.register('organiser')
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET role='admin' WHERE id=?", (admin,))
        self.post('/team', {'name': 'BTC'})
        response = self.post('/admin/tournaments', {'name': 'Giải tân sinh viên', 'format': 'swiss', 'rounds': 3,
                                                    'deadline': now()+120, 'auto_advance': True})
        self.assertEqual(response.status_code, 201, response.json)
        tid = response.json['id']
        self.client.get(f'/api/tournaments/{tid}')
        sid = self.post('/submissions', {'name': 'mẫu', 'source': HUNTER}).json['id']
        self.assertEqual(self.post(f'/tournaments/{tid}/join', {'submission_id': sid}).status_code, 200)
        detail = self.client.get(f'/api/tournaments/{tid}').json
        self.assertEqual(detail['mine']['submission_id'], sid)
        export = self.client.get(f'/api/admin/tournaments/{tid}/export')
        self.assertEqual(export.status_code, 200)
        self.assertIn('Xếp loại', export.get_data(as_text=True))
        self.assertEqual(self.client.get('/api/admin/overview').json['teams'], 1)


class Ranked(Base):
    def test_ranked_queue_swaps_colors_and_settles_once(self):
        (a, _), (b, _) = self.team('Alice'), self.team('Bob')
        sa, sb = self.valid(a), self.valid(b)
        self.assertEqual(self.store.join_ranked(a, sa)['status'], 'queued')
        with self.assertRaises(Problem):
            self.store.join_ranked(a, sa)
        result = self.store.join_ranked(b, sb)
        self.assertEqual(result['status'], 'matched')
        with self.assertRaises(Problem):
            self.store.join_ranked(a, sa)
        for _ in range(2):
            job = self.store.claim('test', 'samples')
            with self.store.connect() as db:
                p1 = db.execute('SELECT p1 FROM matches WHERE id=?', (job['ref'],)).fetchone()[0]
            self.assertTrue(self.store.finish(job, {'winner': 'p1' if p1 == sa else 'p2', 'reason': 'goal'}))
        with self.store.connect() as db:
            series = db.execute('SELECT * FROM ranked_series WHERE id=?', (result['series_id'],)).fetchone()
            self.assertEqual((series['status'], series['a_points'], series['rating_change']), ('done', 2, 16))
            self.assertEqual(tuple(db.execute('SELECT rating,played,wins FROM ratings WHERE team_id=?', (a,)).fetchone()),
                             (1016, 1, 1))
        mine = self.store.ranked_mine(a)
        self.assertEqual((mine['rating'], mine['tier'], mine['position']), (1016, 'Bạc', 1))

    def test_ranked_cancel_split_results_and_repeat_pair(self):
        (a, _), (b, _) = self.team('Alice'), self.team('Bob')
        sa, sb = self.valid(a), self.valid(b)
        self.store.join_ranked(a, sa)
        self.assertTrue(self.store.cancel_ranked(a))
        self.assertFalse(self.store.cancel_ranked(a))
        changes = []
        for _ in range(2):
            self.store.join_ranked(a, sa)
            series = self.store.join_ranked(b, sb)
            for _ in range(2):
                job = self.store.claim('test', 'samples')
                with self.store.connect() as db:
                    p1 = db.execute('SELECT p1 FROM matches WHERE id=?', (job['ref'],)).fetchone()[0]
                self.store.finish(job, {'winner': 'p1' if p1 == sa else 'p2', 'reason': 'goal'})
            with self.store.connect() as db:
                changes.append(db.execute('SELECT rating_change FROM ranked_series WHERE id=?',
                                          (series['series_id'],)).fetchone()[0])
        self.assertEqual(changes[0], 16)
        self.assertLess(changes[1], changes[0])

    def test_ranked_closed_by_organiser(self):
        team, _ = self.team('Đóng')
        self.store.set_settings('admin', {'ranked_open': False})
        with self.assertRaises(Problem):
            self.store.join_ranked(team, self.valid(team))


class GoogleFormImport(Base):
    def csv(self, rows, headers=('Dấu thời gian', 'Tên đội', 'Thành viên', 'Dán mã Python')):
        out = io.StringIO()
        import csv
        writer = csv.writer(out)
        writer.writerow(headers)
        writer.writerows(rows)
        return out.getvalue().encode('utf-8-sig')

    def test_pasted_code_dedupes_latest_and_cleans_copy_paste(self):
        existing, _ = self.team('Đội Có Sẵn')
        data = self.csv([
            ['1', 'Đội Mới', 'An, Bình', FIRST],
            ['2', 'Đội Có Sẵn', 'Chi', 'print("old")'],
            ['3', 'đội có sẵn', 'Chi', '```python\n' + FIRST.replace('    ', '\xa0   ') + '\n```'],
            ['4', 'Không mã', 'Dũng', 'xin chào'],
            ['5', '', '', FIRST],
        ])
        planned = importer.plan(self.store, data, create_teams=True)
        status = [row['status'] for row in planned['rows']]
        self.assertEqual(status, ['ready', 'superseded', 'ready', 'error', 'error'])
        self.assertEqual(planned['mapping']['source'], 'Dán mã Python')
        self.assertNotIn('\xa0', planned['rows'][2]['source'])
        credentials = importer.apply(self.store, planned, 'admin', create_accounts=True)
        self.assertEqual(len(credentials), 1)
        self.assertEqual(credentials[0]['team'], 'Đội Mới')
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM submissions WHERE origin='form'").fetchone()[0], 2)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM submissions WHERE team_id=?', (existing,)).fetchone()[0], 1)
            user = db.execute('SELECT * FROM users WHERE username=?', (credentials[0]['username'],)).fetchone()
            self.assertEqual(user['must_change'], 1)
        again = importer.plan(self.store, data, create_teams=True)
        self.assertEqual([row['status'] for row in again['rows']][:3], ['unchanged', 'superseded', 'unchanged'])

    def test_zip_upload_matches_team_code_and_rejects_bad_files(self):
        (a, _), (b, _), (c, _) = self.team('Alpha'), self.team('Beta'), self.team('Gamma')
        with self.store.connect() as db:
            codes = {t: db.execute('SELECT code FROM teams WHERE id=?', (t,)).fetchone()[0] for t in (a, b, c)}
        bundle = io.BytesIO()
        with zipfile.ZipFile(bundle, 'w') as archive:
            archive.writestr(f'Nộp bài/{codes[a]} - Nguyễn Văn A.py', FIRST.encode())
            archive.writestr('../../etc/' + codes[b] + '.py', '<!DOCTYPE html><html>Drive</html>')
            archive.writestr(f'{codes[c]}.py', ('# ' + 'x'*70000 + '\n' + FIRST).encode())
            archive.writestr('README.md', 'ignored')
        data = self.csv([['1', codes[t], 'https://drive.google.com/open?id=x'] for t in (a, b, c)],
                        headers=('Timestamp', 'Mã đội', 'Tải file bot lên'))
        files, _ = importer.Files.from_zip(bundle.getvalue())
        planned = importer.plan(self.store, data, files)
        rows = {row['code']: row for row in planned['rows']}
        self.assertEqual(rows[codes[a]]['status'], 'ready')
        self.assertIn('HTML', rows[codes[b]]['message'])
        self.assertIn('64 KiB', rows[codes[c]]['message'])
        with self.assertRaises(Problem):
            importer.plan(self.store, data)

    def test_http_import_dry_run_apply_and_one_time_credentials(self):
        admin = self.register('organiser')
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET role='admin' WHERE id=?", (admin,))
        tid = self.post('/admin/tournaments', {'name': 'Giải Form', 'format': 'swiss', 'rounds': 3,
                                               'deadline': now()+600}).json['id']
        data = self.csv([[str(i), f'Đội Form {i}', 'SV', FIRST] for i in range(30)])
        headers = {'Origin': 'http://localhost:8080', 'X-CSRF-Token': self.csrf}
        form = lambda extra: {'csv': (io.BytesIO(data), 'responses.csv'), 'create_teams': '1',
                              'create_accounts': '1', 'tournament': tid, **extra}
        dry = self.client.post('/api/admin/import', data=form({}), headers=headers, content_type='multipart/form-data')
        self.assertEqual(dry.status_code, 200, dry.json)
        self.assertEqual(dry.json['summary']['ready'], 30)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM teams').fetchone()[0], 0)
        applied = self.client.post('/api/admin/import', data=form({'apply': '1'}), headers=headers,
                                   content_type='multipart/form-data')
        self.assertEqual(applied.status_code, 202, applied.json)
        iid = applied.json['id']
        for _ in range(200):
            status = self.client.get(f'/api/admin/imports/{iid}').json
            if status['status'] != 'running':
                break
            time.sleep(.1)
        self.assertEqual(status['status'], 'done', status)
        self.assertEqual(status['summary']['imported'], 30)
        first = self.client.get(f'/api/admin/imports/{iid}/credentials')
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.get_data(as_text=True).count('\n'), 31)
        self.assertEqual(self.client.get(f'/api/admin/imports/{iid}/credentials').status_code, 410)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM entries WHERE tournament_id=?', (tid,)).fetchone()[0], 30)


class Campaign(Base):
    bosses = True

    def ready_bosses(self):
        with self.store.connect(True) as db:
            db.execute("UPDATE submissions SET status='valid' WHERE team_id=?", (SYSTEM_TEAM,))
            db.execute("UPDATE jobs SET status='done' WHERE kind='validate'")

    def play(self, sid, results):
        """Finish queued games; results[i] is the team's result in game i: 'win' | 'draw' | 'loss'."""
        for result in results:
            job = self.store.claim('test', 'samples')
            with self.store.connect() as db:
                p1 = db.execute('SELECT p1 FROM matches WHERE id=?', (job['ref'],)).fetchone()[0]
            ours = 'p1' if p1 == sid else 'p2'
            winner = 'draw' if result == 'draw' else ours if result == 'win' else ('p2' if ours == 'p1' else 'p1')
            self.assertTrue(self.store.finish(job, {'winner': winner, 'reason': 'goal'}))

    def test_bosses_are_registered_once_validated_by_runner_and_kept_private(self):
        with self.store.connect() as db:
            bosses = db.execute('SELECT b.level,s.status,s.origin FROM bosses b JOIN submissions s '
                                'ON s.id=b.submission_id ORDER BY b.level').fetchall()
            code = db.execute('SELECT code FROM teams WHERE id=?', (SYSTEM_TEAM,)).fetchone()[0]
        self.assertEqual([b['level'] for b in bosses], [1, 2, 3, 4, 5])
        self.assertEqual({b['origin'] for b in bosses}, {'boss'})
        create_app(self.tmp.name, origin='http://localhost:8080', registration_code='test-invite').extensions['store'].close()
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM submissions WHERE team_id=?', (SYSTEM_TEAM,)).fetchone()[0], 5)
        worker = Worker(self.store, Executor('samples'))
        while worker.run_once():
            pass
        with self.store.connect() as db:
            statuses = [r[0] for r in db.execute('SELECT s.status FROM bosses b JOIN submissions s ON s.id=b.submission_id')]
            boss_sid = db.execute('SELECT submission_id FROM bosses WHERE level=5').fetchone()[0]
        self.assertEqual(statuses, ['valid'] * 5)
        user = self.user('joiner')
        with self.assertRaises(Problem) as caught:
            self.store.join_team(user, code)
        self.assertEqual(caught.exception.status, 404)
        self.register('peeker')
        self.post('/team', {'name': 'Tò mò'})
        self.assertEqual(self.client.get(f'/api/submissions/{boss_sid}/source').status_code, 404)
        profile = self.client.get(f'/api/teams/{SYSTEM_TEAM}').json
        self.assertTrue(profile['system'])
        self.assertEqual(profile['badges'], [])
        data = self.client.get('/api/campaign').json
        self.assertEqual([b['status'] for b in data['bosses']], ['valid'] * 5)

    def test_levels_unlock_in_order_with_stars_hall_and_feed(self):
        self.ready_bosses()
        team, user = self.team('Leo tháp')
        sid = self.valid(team)
        with self.assertRaises(Problem) as caught:
            self.store.challenge(team, user, 2, sid)
        self.assertEqual(caught.exception.status, 403)
        with self.assertRaises(Problem):
            self.store.challenge(team, user, 9, sid)
        with self.assertRaises(Problem):
            self.store.challenge(team, user, '1', sid)
        self.store.challenge(team, user, 1, sid)
        with self.assertRaises(Problem) as caught:
            self.store.challenge(team, user, 1, sid)
        self.assertEqual(caught.exception.status, 409)
        self.play(sid, ['win', 'win'])
        mine = self.store.campaign_mine(team)
        self.assertEqual((mine['cleared'], mine['stars'], mine['runs'][0]['stars']), (1, 3, 3))
        self.store.challenge(team, user, 2, sid)
        self.play(sid, ['win', 'draw'])
        self.store.challenge(team, user, 3, sid)
        self.play(sid, ['win', 'loss'])
        mine = self.store.campaign_mine(team)
        self.assertEqual((mine['cleared'], mine['stars']), (2, 5))
        with self.assertRaises(Problem):
            self.store.challenge(team, user, 4, sid)
        hall = self.store.campaign_hall()
        self.assertEqual([(h['name'], h['cleared'], h['stars']) for h in hall['hall']], [('Leo tháp', 2, 5)])
        events = [e for e in self.store.feed()['events'] if e['kind'] == 'boss']
        self.assertEqual([(e['level'], e['stars']) for e in events], [(2, 2), (1, 3)])
        self.assertEqual(self.store.feed()['stats']['boss_clears'], 2)
        badges = {b['key']: b['earned'] for b in self.store.team_badges(team)}
        self.assertTrue(badges['boss1'])
        self.assertFalse(badges['boss_final'])
        self.store.set_settings('admin', {'campaign_open': False})
        with self.assertRaises(Problem) as caught:
            self.store.challenge(team, user, 3, sid)
        self.assertEqual(caught.exception.status, 403)

    def test_cancelled_game_cancels_run_without_progress(self):
        self.ready_bosses()
        team, user = self.team('Bị hủy')
        sid = self.valid(team)
        run = self.store.challenge(team, user, 1, sid)
        with self.store.connect(True) as db:
            self.store.cancel_match(db, run['matches'][0])
        self.play(sid, ['win'])
        mine = self.store.campaign_mine(team)
        self.assertEqual((mine['runs'][0]['status'], mine['cleared']), ('cancelled', 0))
        self.store.challenge(team, user, 1, sid)

    def test_boss_not_ready_and_http_challenge(self):
        self.register('climber')
        self.post('/team', {'name': 'Leo HTTP'})
        team = self.client.get('/api/me').json['team']['id']
        sid = self.valid(team)
        result = self.post('/campaign/challenge', {'level': 1, 'submission_id': sid})
        self.assertEqual(result.status_code, 409, result.json)
        self.ready_bosses()
        result = self.post('/campaign/challenge', {'level': 1, 'submission_id': sid})
        self.assertEqual(result.status_code, 201, result.json)
        mine = self.client.get('/api/campaign').json['mine']
        self.assertEqual(mine['runs'][0]['status'], 'running')
        self.assertEqual(len([p for p in mine['runs'][0]['positions'] if p]), 2)
        matches = self.client.get('/api/matches?mine=1').json['items']
        self.assertEqual({m['kind'] for m in matches}, {'campaign'})
        self.assertEqual({m['team2_id'] for m in matches} | {m['team1_id'] for m in matches}, {team, SYSTEM_TEAM})


class SignInCornerCases(Base):
    PASSWORD = 'test-only-password-123'

    def attempt(self, path, data, origin='http://localhost:8080', client=None, **extra):
        return (client or self.app.test_client()).post('/api' + path, json=data, headers={'Origin': origin, **extra})

    def signup(self, **over):
        data = {'username': 'newbie', 'name': 'Tân sinh viên', 'password': self.PASSWORD, 'invite': 'test-invite', **over}
        return self.attempt('/register', data)

    def test_origin_accepts_configured_or_same_host_but_not_foreign_sites(self):
        self.assertEqual(self.signup(username='cfg').status_code, 200)
        # Reached through another address (LAN IP, second domain): same host as the request.
        lan = self.attempt('/register', {'username': 'lan', 'name': 'Qua LAN', 'password': self.PASSWORD,
                                         'invite': 'test-invite'}, origin='http://192.168.10.185:8091',
                           client=self.app.test_client(), Host='192.168.10.185:8091')
        self.assertEqual(lan.status_code, 200, lan.json)
        evil = self.attempt('/register', {'username': 'evil', 'name': 'Giả mạo', 'password': self.PASSWORD,
                                          'invite': 'test-invite'}, origin='https://evil.example',
                            client=self.app.test_client())
        self.assertEqual(evil.status_code, 403)
        self.assertIn('Địa chỉ truy cập', evil.json['error'])
        opaque = self.attempt('/register', {'username': 'nul', 'name': 'Opaque', 'password': self.PASSWORD},
                              origin='null', client=self.app.test_client())
        self.assertEqual(opaque.status_code, 403)

    def test_cookie_is_secure_only_when_browser_used_https(self):
        http = self.attempt('/register', {'username': 'plain', 'name': 'Http', 'password': self.PASSWORD,
                                          'invite': 'test-invite'}, origin='http://localhost:8080')
        self.assertNotIn('Secure', http.headers['Set-Cookie'])
        secure = self.attempt('/login', {'username': 'plain', 'password': self.PASSWORD},
                              origin='https://arena.test', client=self.app.test_client(), Host='arena.test')
        self.assertIn('Secure', secure.headers['Set-Cookie'])
        self.assertIn('HttpOnly', secure.headers['Set-Cookie'])

    def test_username_rules_case_reserved_and_duplicates(self):
        self.assertEqual(self.signup(username='  MixedCase_01 ').status_code, 200)
        self.assertEqual(self.signup(username='mixedcase_01').status_code, 409)
        self.assertEqual(self.signup(username='MIXEDCASE_01').status_code, 409)
        for bad in ('ab', 'a' * 33, 'có dấu', 'space in', 'semi;colon', '../etc', '<b>x</b>', 'user-name', ''):
            self.assertEqual(self.signup(username=bad).status_code, 400, bad)
        for reserved in ('admin', 'Root', 'SYSTEM'):
            self.assertEqual(self.signup(username=reserved).status_code, 409, reserved)

    def test_password_rules(self):
        for bad, expect in (('short-1', 400), (' leading-space-pass', 400), ('trailing-space-pass ', 400),
                            ('a' * 129, 400), ('aaaaaaaaaaaaaaaa', 400), ('xx-newbie-xx-yy', 400), ('', 400)):
            self.assertEqual(self.signup(password=bad).status_code, expect, repr(bad))
        ok = self.signup(password='đây là mật khẩu dài có dấu')
        self.assertEqual(ok.status_code, 200, ok.json)
        self.assertEqual(self.attempt('/login', {'username': 'newbie', 'password': 'đây là mật khẩu dài có dấu'},
                                      client=self.app.test_client()).status_code, 200)

    def test_name_student_id_and_wrong_types(self):
        browser = self.app.test_client()
        messy = '  Nguyễn ​Văn   A\n '
        r = self.attempt('/register', {'username': 'clean', 'name': messy, 'password': self.PASSWORD,
                                       'invite': 'test-invite'}, client=browser)
        self.assertEqual(r.status_code, 200, r.json)
        self.assertEqual(browser.get('/api/me').json['user']['name'], 'Nguyễn Văn A')
        for bad in ('x', ' ', '\u200b\u200b', 'a' * 41, 123, None, ['a'], {'a': 1}):
            self.assertEqual(self.signup(username=f'u{abs(hash(str(bad))) % 10**8}', name=bad).status_code, 400, repr(bad))
        for bad in ('has space', 'ab<script>', 'x' * 21, '!!'):
            self.assertEqual(self.signup(username='mssv_bad', student_id=bad).status_code, 400, bad)
        good = self.signup(username='mssv_ok', student_id='b21dcat001')
        self.assertEqual(good.status_code, 200, good.json)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT student_id FROM users WHERE username='mssv_ok'").fetchone()[0], 'B21DCAT001')
        for field, value in (('username', 5), ('password', 5), ('username', None), ('password', ['x'])):
            body = {'username': 'typed', 'name': 'Kiểu dữ liệu', 'password': self.PASSWORD, 'invite': 'test-invite'}
            body[field] = value
            self.assertEqual(self.attempt('/register', body).status_code, 400, (field, value))
        self.assertEqual(self.attempt('/register', {'username': 'x'}).status_code, 400)
        self.assertEqual(self.client.post('/api/register', data='not json', headers={'Origin': 'http://localhost:8080'}).status_code, 415)
        self.assertEqual(self.client.post('/api/register', json=['list'], headers={'Origin': 'http://localhost:8080'}).status_code, 400)

    def test_invite_code_required_trimmed_and_checked_in_constant_time_form(self):
        self.assertEqual(self.signup(username='noinvite', invite='').status_code, 403)
        self.assertEqual(self.signup(username='wrong', invite='nope').status_code, 403)
        self.assertEqual(self.signup(username='missing', invite=None).status_code, 403)
        self.assertEqual(self.signup(username='spaced', invite='  test-invite ').status_code, 200)

    def test_login_messages_do_not_reveal_which_part_was_wrong(self):
        self.signup(username='member')
        wrong_password = self.attempt('/login', {'username': 'member', 'password': 'wrong-password-1'})
        unknown = self.attempt('/login', {'username': 'ghost_user', 'password': 'wrong-password-1'})
        self.assertEqual((wrong_password.status_code, unknown.status_code), (401, 401))
        self.assertEqual(wrong_password.json, unknown.json)
        self.assertEqual(self.attempt('/login', {'username': '  MEMBER ', 'password': self.PASSWORD}).status_code, 200)
        for body in ({}, {'username': 'member'}, {'password': 'x'}, {'username': 7, 'password': 'x'},
                     {'username': 'member', 'password': ['x']}, {'username': '', 'password': ''}):
            self.assertEqual(self.attempt('/login', body).status_code, 400, body)
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET disabled=1 WHERE username='member'")
        locked = self.attempt('/login', {'username': 'member', 'password': self.PASSWORD})
        self.assertEqual(locked.status_code, 403)
        self.assertIn('khóa', locked.json['error'])
        # A disabled account must not keep working through an old session.
        self.assertIsNone(self.client.get('/api/me').json['user'])

    def test_login_is_rate_limited_per_username_but_other_accounts_still_work(self):
        self.signup(username='target')
        self.signup(username='bystander')
        codes = [self.attempt('/login', {'username': 'target', 'password': 'wrong-password-1'}).status_code
                 for _ in range(22)]
        self.assertEqual(codes[:20], [401] * 20)
        self.assertEqual(codes[20:], [429, 429])
        self.assertEqual(self.attempt('/login', {'username': 'bystander', 'password': self.PASSWORD}).status_code, 200)

    def test_registration_closed_logout_and_stale_cookie(self):
        self.store.set_settings('admin', {'registration_open': False})
        self.app.extensions['cache'].clear()
        closed = self.signup(username='toolate')
        self.assertEqual(closed.status_code, 403)
        self.assertIn('đóng đăng ký', closed.json['error'])
        self.store.set_settings('admin', {'registration_open': True})
        self.app.extensions['cache'].clear()
        browser = self.app.test_client()
        opened = self.attempt('/register', {'username': 'open_again', 'name': 'Mở lại', 'password': self.PASSWORD,
                                            'invite': 'test-invite'}, client=browser)
        self.assertEqual(opened.status_code, 200)
        csrf = browser.get('/api/me').json['csrf']
        self.assertEqual(self.post('/logout', {}, client=browser, csrf=csrf).status_code, 200)
        self.assertIsNone(browser.get('/api/me').json['user'])
        browser.set_cookie('ott_session', 'forged-or-expired-token')
        self.assertEqual(browser.get('/api/me').json['user'], None)
        self.assertEqual(browser.get('/api/tournaments').status_code, 401)

    def test_password_change_rules_and_other_sessions_end(self):
        browser, other = self.app.test_client(), self.app.test_client()
        self.attempt('/register', {'username': 'changer', 'name': 'Đổi mật khẩu', 'password': self.PASSWORD,
                                   'invite': 'test-invite'}, client=browser)
        csrf = browser.get('/api/me').json['csrf']
        self.attempt('/login', {'username': 'changer', 'password': self.PASSWORD}, client=other)
        weak = self.post('/me/password', {'current': self.PASSWORD, 'password': 'aaaaaaaaaaaaaaaa'}, client=browser, csrf=csrf)
        self.assertEqual(weak.status_code, 400)
        wrong = self.post('/me/password', {'current': 'not-my-password', 'password': 'brand-new-password-9'}, client=browser, csrf=csrf)
        self.assertEqual(wrong.status_code, 403)
        ok = self.post('/me/password', {'current': self.PASSWORD, 'password': 'brand-new-password-9'}, client=browser, csrf=csrf)
        self.assertEqual(ok.status_code, 200)
        self.assertIsNone(other.get('/api/me').json['user'])
        self.assertEqual(browser.get('/api/me').json['user']['username'], 'changer')
        self.assertEqual(self.attempt('/login', {'username': 'changer', 'password': 'brand-new-password-9'},
                                      client=self.app.test_client()).status_code, 200)

    def test_concurrent_registration_of_one_username_creates_exactly_one_account(self):
        def go(i):
            client = self.app.test_client()
            return client.post('/api/register', json={'username': 'race', 'name': f'Người {i}', 'password': self.PASSWORD,
                                                      'invite': 'test-invite'},
                               headers={'Origin': 'http://localhost:8080'}).status_code
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            codes = list(pool.map(go, range(8)))
        self.assertEqual(sorted(codes), [200] + [409] * 7, codes)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM users WHERE username='race'").fetchone()[0], 1)


class LocalArena(unittest.TestCase):
    """The offline tool students use must agree with the real referee move by move."""

    def test_rules_copy_matches_referee_on_random_games(self):
        import importlib.util
        import random
        spec = importlib.util.spec_from_file_location('local_arena', ROOT / 'sdk/local_arena.py')
        local = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(local)
        referee, rng, moves_checked, endings = Referee(), random.Random(7), 0, set()
        try:
            for game in range(60):
                packet, state = referee.call('new'), local.new_game()
                for _ in range(400):
                    theirs = packet['legalMoves']
                    self.assertEqual(local.legal_moves(state), theirs, (game, state['revision']))
                    if packet['state']['winner'] or not theirs:
                        break
                    captures = [m for m in theirs if m['capture']]
                    # Bias towards captures and goals so every kind of ending is exercised.
                    pool = captures if captures and rng.random() < .8 else theirs
                    goal = [m for m in theirs if m['to'] == list(local.GOALS[packet['state']['turn']])]
                    move = goal[0] if goal else rng.choice(pool)
                    packet = referee.call('move', **{'from': move['from'], 'to': move['to']})
                    state, error = local.apply_move(state, state['turn'], move['from'], move['to'])
                    self.assertIsNone(error)
                    expected = packet['state']
                    for key in ('board', 'turn', 'winner', 'winReason', 'revision', 'quietPly', 'repetitions'):
                        self.assertEqual(state[key], expected[key], (game, state['revision'], key))
                    self.assertEqual(state['lastMove']['notation'], expected['lastMove']['notation'])
                    moves_checked += 1
                endings.add(packet['state']['winReason'])
        finally:
            referee.close()
        self.assertGreater(moves_checked, 500)
        self.assertTrue({'goal', 'quiet'} <= endings and any(e.startswith('eliminate') for e in endings), endings)

    def test_illegal_moves_are_rejected_like_the_referee(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('local_arena', ROOT / 'sdk/local_arena.py')
        local = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(local)
        state = local.new_game()
        for bad in (([0, 0], [1, 1]), ([7, 0], [7, 0]), ([7, 0], [3, 3]), ([0, 8], [0, 7]), ([9, 9], [8, 8]),
                    ([7, 0], [6.0, 0]), ('ab', [1, 1]), ([7, 0], None)):
            self.assertIsNotNone(local.apply_move(state, 'p1', *bad)[1], bad)
        self.assertIsNotNone(local.apply_move(state, 'p2', [1, 8], [2, 8])[1])


class LiveSpectating(Base):
    MOVES = [[6, 1, 5, 1], [2, 7, 3, 7], [5, 1, 4, 1]]

    def running_match(self, name='Trực tiếp'):
        (a, ua), (b, _) = self.team(name + ' A'), self.team(name + ' B')
        sa, sb = self.valid(a), self.valid(b)
        with self.store.connect(True) as db:
            mid = self.store.create_match(db, sa, sb, ua)
        job = self.store.claim('test', 'samples')
        self.assertEqual(job['ref'], mid)
        return mid, job, a, b

    def watcher(self, name='viewer'):
        client = self.app.test_client()
        client.post('/api/register', json={'username': name, 'name': 'Khán giả', 'password': 'test-only-password-123',
                                           'invite': 'test-invite'}, headers={'Origin': 'http://localhost:8080'})
        return client

    def test_live_poll_returns_only_new_moves_and_detects_restarts(self):
        mid, job, _, _ = self.running_match()
        viewer = self.watcher()
        self.assertEqual(self.app.test_client().get(f'/api/matches/{mid}/live').status_code, 401)
        first = viewer.get(f'/api/matches/{mid}/live').json
        self.assertEqual((first['status'], first['moves'], first['ply'], first['reset']), ('running', [], 0, False))
        self.store.progress(job, {'ply': 3, 'thinking': 'p2', 'moves': self.MOVES, 'left': {'p1': 59.2, 'p2': 58.9}})
        self.app.extensions['cache'].clear()
        live = viewer.get(f'/api/matches/{mid}/live?since=0').json
        self.assertEqual((live['moves'], live['ply'], live['thinking'], live['left']['p2']), (self.MOVES, 3, 'p2', 58.9))
        self.assertEqual(viewer.get(f'/api/matches/{mid}/live?since=2').json['moves'], [self.MOVES[2]])
        self.assertEqual(viewer.get(f'/api/matches/{mid}/live?since=3').json['moves'], [])
        for junk in ('abc', '-5', ''):
            self.assertEqual(viewer.get(f'/api/matches/{mid}/live?since={junk}').status_code, 200)
        # The grader restarted the game (retry): fewer moves than the viewer already has => full resync.
        self.store.progress(job, {'ply': 1, 'thinking': 'p2', 'moves': self.MOVES[:1], 'left': {'p1': 60, 'p2': 60}})
        self.app.extensions['cache'].clear()
        restart = viewer.get(f'/api/matches/{mid}/live?since=3').json
        self.assertEqual((restart['reset'], restart['moves']), (True, self.MOVES[:1]))
        self.assertEqual(viewer.get('/api/matches/nope/live').status_code, 404)

    def test_live_status_queue_position_done_and_cancel(self):
        (a, ua), (b, _) = self.team('Chờ A'), self.team('Chờ B')
        sa, sb = self.valid(a), self.valid(b)
        with self.store.connect(True) as db:
            ahead = self.store.create_match(db, sa, sb, ua)
            waiting = self.store.create_match(db, sb, sa, ua)
        viewer = self.watcher('queuer')
        queued = viewer.get(f'/api/matches/{waiting}/live').json
        self.assertEqual((queued['status'], queued['position']), ('queued', 2))
        job = self.store.claim('test', 'samples')
        self.assertEqual(job['ref'], ahead)
        self.assertTrue(self.store.finish(job, {'winner': 'p1', 'reason': 'goal', 'moves': [{'from': [6, 1], 'to': [5, 1]}]}))
        self.app.extensions['cache'].clear()
        done = viewer.get(f'/api/matches/{ahead}/live').json
        self.assertEqual((done['status'], done['winner'], done['moves']), ('done', 'p1', []))
        with self.store.connect(True) as db:
            self.store.cancel_match(db, waiting)
        self.app.extensions['cache'].clear()
        self.assertEqual(viewer.get(f'/api/matches/{waiting}/live').json['status'], 'cancelled')

    def test_viewer_count_and_team_live_list(self):
        mid, job, a, b = self.running_match('Đếm')
        first, second = self.watcher('w_one'), self.watcher('w_two')
        self.assertEqual(first.get(f'/api/matches/{mid}/live').json['viewers'], 1)
        self.assertEqual(second.get(f'/api/matches/{mid}/live').json['viewers'], 2)
        self.assertEqual(second.get(f'/api/matches/{mid}').json['viewers'], 2)
        self.assertEqual(first.get(f'/api/matches/{mid}/live').json['viewers'], 2)
        # A signed-in player sees the games of their own team, running first.
        owner = self.app.test_client()
        owner.post('/api/register', json={'username': 'owner_x', 'name': 'Chủ đội', 'password': 'test-only-password-123',
                                          'invite': 'test-invite'}, headers={'Origin': 'http://localhost:8080'})
        csrf = owner.get('/api/me').json['csrf']
        self.assertEqual(owner.get('/api/me/live').json['items'], [])
        with self.store.connect(True) as db:
            user = db.execute("SELECT id FROM users WHERE username='owner_x'").fetchone()[0]
            db.execute('UPDATE users SET team_id=? WHERE id=?', (a, user))
        self.app.extensions['cache'].clear()
        items = owner.get('/api/me/live').json['items']
        self.assertEqual([(m['id'], m['status']) for m in items], [(mid, 'running')])
        self.assertEqual({items[0]['team1_id'], items[0]['team2_id']}, {a, b})
        del csrf
        self.assertEqual(self.app.test_client().get('/api/me/live').status_code, 401)

    def test_match_detail_links_the_other_game_of_each_pair(self):
        (a, _), (b, _) = self.team('Cặp A'), self.team('Cặp B')
        sa, sb = self.valid(a), self.valid(b)
        self.store.join_ranked(a, sa)
        series = self.store.join_ranked(b, sb)
        viewer = self.watcher('pairs')
        first, second = series['matches']
        one, two = viewer.get(f'/api/matches/{first}').json, viewer.get(f'/api/matches/{second}').json
        self.assertEqual((one['sibling'], one['game_number'], two['sibling'], two['game_number']), (second, 1, first, 2))
        with self.store.connect(True) as db:
            lone = self.store.create_match(db, sa, sb, None)
        self.assertEqual(viewer.get(f'/api/matches/{lone}').json['sibling'], None)
        requester = self.user('req_user')
        with self.store.connect(True) as db:
            pair = self.store.create_match(db, sa, sb, requester=requester)
            other = self.store.create_match(db, sb, sa, requester=requester)
        self.app.extensions['cache'].clear()
        self.assertEqual(viewer.get(f'/api/matches/{pair}').json['sibling'], other)

    def test_match_list_filters_by_kind_and_team_name(self):
        (a, ua), (b, _) = self.team('Sói Xám'), self.team('Cáo Đỏ')
        sa, sb = self.valid(a), self.valid(b)
        with self.store.connect(True) as db:
            practice = self.store.create_match(db, sa, sb, ua)
            friendly = self.store.create_match(db, sb, sa, ua, kind='friendly')
        viewer = self.watcher('lister')
        ids = lambda query: {m['id'] for m in viewer.get('/api/matches' + query).json['items']}
        self.assertEqual(ids(''), {practice, friendly})
        self.assertEqual(ids('?kind=friendly'), {friendly})
        self.assertEqual(ids('?q=S%C3%B3i'), {practice, friendly})
        self.assertEqual(ids('?q=Cáo&kind=practice'), {practice})
        self.assertEqual(ids('?q=%25%27%3B--'), set())
        self.assertEqual(ids('?kind=bogus'), {practice, friendly})

    def test_runner_publishes_compact_progress_while_a_game_runs(self):
        from arena_backend import runner as runner_module
        team, user = self.team('Tiến độ')
        a, b = self.valid(team, HUNTER, 'a'), self.valid(team, HUNTER, 'b')
        with self.store.connect(True) as db:
            mid = self.store.create_match(db, a, b, requester=user)
        snapshots, real = [], self.store.progress
        self.store.progress = lambda job, state: (snapshots.append(state), real(job, state))
        old, runner_module.PROGRESS_INTERVAL = runner_module.PROGRESS_INTERVAL, 0
        try:
            Worker(self.store, TrustedExecutor()).run_once()
        finally:
            runner_module.PROGRESS_INTERVAL = old
        self.assertGreaterEqual(len(snapshots), 2)
        plies = [snap['ply'] for snap in snapshots]
        self.assertEqual(plies, sorted(plies))
        last = snapshots[-1]
        self.assertEqual(len(last['moves']), last['ply'])
        self.assertTrue(all(len(m) == 4 and all(type(x) is int for x in m) for m in last['moves']))
        self.assertIn(last['thinking'], ('p1', 'p2'))
        self.assertTrue(0 < last['left']['p1'] <= 60)
        with self.store.connect() as db:
            row = db.execute('SELECT status,progress FROM matches WHERE id=?', (mid,)).fetchone()
        self.assertEqual((row['status'], row['progress']), ('done', None))


class AdminMatches(Base):
    def admin_client(self):
        self.register('boss_admin')
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET role='admin' WHERE username='boss_admin'")

    def test_list_filter_and_cancel_only_standalone_games(self):
        (a, ua), (b, _) = self.team('Alpha'), self.team('Beta')
        sa, sb = self.valid(a), self.valid(b)
        with self.store.connect(True) as db:
            practice = self.store.create_match(db, sa, sb, ua)
        tid = self.tournament()
        with self.store.connect(True) as db:
            pid = uid()
            db.execute('INSERT INTO pairings(id,tournament_id,round,slot,a,b) VALUES(?,?,1,0,?,?)', (pid, tid, a, b))
            ranked_game = self.store.create_match(db, sa, sb, pairing=pid, kind='tournament')
        self.assertEqual(self.client.get('/api/admin/matches').status_code, 401)
        self.admin_client()
        items = self.client.get('/api/admin/matches').json['items']
        self.assertEqual({m['id'] for m in items}, {practice, ranked_game})
        self.assertEqual([m['id'] for m in self.client.get('/api/admin/matches?kind=practice').json['items']], [practice])
        self.assertEqual(len(self.client.get('/api/admin/matches?q=Alpha&status=queued').json['items']), 2)
        self.assertEqual(self.post(f'/admin/matches/{ranked_game}', {'action': 'cancel'}).status_code, 409)
        self.assertEqual(self.post(f'/admin/matches/{practice}', {'action': 'cancel'}).status_code, 200)
        self.assertEqual(self.post(f'/admin/matches/{practice}', {'action': 'cancel'}).status_code, 409)
        self.assertEqual(self.post('/admin/matches/nope', {'action': 'cancel'}).status_code, 404)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM matches WHERE id=?', (practice,)).fetchone()[0], 'cancelled')

    def test_clear_queue_cancels_waiting_games_and_their_series(self):
        (a, _), (b, _) = self.team('Một'), self.team('Hai')
        sa, sb = self.valid(a), self.valid(b)
        self.store.join_ranked(a, sa)
        series = self.store.join_ranked(b, sb)['series_id']
        self.admin_client()
        self.assertEqual(self.post('/admin/matches-cancel-queued', {'kind': 'tournament'}).status_code, 400)
        self.assertEqual(self.post('/admin/matches-cancel-queued', {'kind': 'ranked'}).json['cancelled'], 2)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM ranked_series WHERE id=?', (series,)).fetchone()[0], 'cancelled')


class OpenRegistration(unittest.TestCase):
    def test_public_origin_needs_invite_or_explicit_open_registration(self):
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RuntimeError):
                create_app(directory, origin='https://arena.example.org', registration_code='', bosses=False)
            with mock.patch.dict(os.environ, {'ARENA_OPEN_REGISTRATION': '1'}):
                app = create_app(directory, origin='https://arena.example.org', registration_code='', bosses=False)
                client = app.test_client()
                self.assertFalse(client.get('/api/health').json['registration_invite'])
                result = client.post('/api/register', json={'username': 'newbie', 'name': 'Tân sinh viên',
                                                            'password': 'test-only-password-123'},
                                     headers={'Origin': 'https://arena.example.org'})
                self.assertEqual(result.status_code, 200, result.json)
            app.extensions['store'].close()


class Social(Base):
    def series(self, a, sa, b, sb, a_wins, final_state=None, moves=30):
        self.store.join_ranked(a, sa)
        self.store.join_ranked(b, sb)
        for _ in range(2):
            job = self.store.claim('test', 'samples')
            with self.store.connect() as db:
                p1 = db.execute('SELECT p1 FROM matches WHERE id=?', (job['ref'],)).fetchone()[0]
            a_side = 'p1' if p1 == sa else 'p2'
            winner = a_side if a_wins else ('p2' if a_side == 'p1' else 'p1')
            result = {'winner': winner, 'reason': 'goal', 'moves': [{}] * moves}
            if final_state:
                result['final_state'] = final_state
            self.store.finish(job, result)

    def test_friendly_uses_rivals_latest_bot_one_at_a_time(self):
        (a, ua), (b, ub), (c, _) = self.team('Thách'), self.team('Nhận'), self.team('Trống')
        sa = self.valid(a)
        self.valid(b, name='cũ')
        latest = self.valid(b, FIRST, name='mới')
        ids = self.store.friendly(a, ua, sa, b)
        with self.store.connect() as db:
            rows = db.execute('SELECT kind,p1,p2 FROM matches WHERE id IN (?,?) ORDER BY created,rowid', ids).fetchall()
        self.assertEqual([tuple(r) for r in rows], [('friendly', sa, latest), ('friendly', latest, sa)])
        with self.assertRaises(Problem) as caught:
            self.store.friendly(a, ua, sa, b)
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(len(self.store.friendly(b, ub, latest, a)), 2)
        for opponent in (a, c, 'f' * 24):
            with self.assertRaises(Problem):
                self.store.friendly(a, ua, sa, opponent)
        self.store.set_settings('admin', {'practice_open': False})
        with self.assertRaises(Problem) as caught:
            self.store.friendly(b, ub, latest, c)
        self.assertEqual(caught.exception.status, 403)

    def test_badges_rating_history_upset_streak_and_feed(self):
        (a, _), (b, _) = self.team('Tân binh'), self.team('Lão làng')
        sa, sb = self.valid(a), self.valid(b)
        with self.store.connect(True) as db:
            db.execute('INSERT INTO ratings(team_id,rating,played) VALUES(?,1200,5)', (b,))
        full = {'board': [[{'owner': 'p1' if i < 9 else 'p2', 'type': 'rock'} if i < 18 else None
                           for i in range(r * 9, r * 9 + 9)] for r in range(9)]}
        self.series(a, sa, b, sb, True, full)
        badges = {x['key']: x['earned'] for x in self.store.team_badges(a)}
        for key in ('first_bot', 'first_win', 'goal', 'blitz', 'flawless', 'upset'):
            self.assertTrue(badges[key], key)
        for key in ('streak', 'gold', 'wipe', 'squad', 'boss1'):
            self.assertFalse(badges[key], key)
        event = self.store.feed()['events'][0]
        self.assertEqual((event['winner'], event['points'], sorted(event['tags'])), (a, 2, ['sweep', 'upset']))
        self.series(a, sa, b, sb, True, moves=80)
        self.series(a, sa, b, sb, True, moves=80)
        badges = {x['key']: x['earned'] for x in self.store.team_badges(a)}
        self.assertTrue(badges['streak'])
        profile = self.store.team_profile(a)
        ratings = [point['rating'] for point in profile['history']]
        self.assertEqual(ratings[0], 1000)
        self.assertEqual(ratings[-1], profile['rating'])
        self.assertEqual(len(ratings), 4)
        self.assertTrue(all(x < y for x, y in zip(ratings, ratings[1:])))
        # Repeat pairings within 24 hours move Elo less each time: +24, +11, +7.
        self.assertEqual((profile['peak'], profile['position'], profile['record']['wins']), (1042, 2, 3))
        self.assertFalse({x['key']: x['earned'] for x in profile['badges']}['gold'])
        loser = self.store.team_profile(b)
        self.assertEqual(loser['peak'], 1200)
        self.assertEqual(loser['record']['losses'], 3)

    def test_profile_feed_and_badge_endpoints(self):
        self.register('owner')
        self.post('/team', {'name': 'Hồ sơ'})
        team = self.client.get('/api/me').json['team']['id']
        self.valid(team)
        badges = self.client.get('/api/me/badges').json['items']
        self.assertTrue(next(b for b in badges if b['key'] == 'first_bot')['earned'])
        profile = self.client.get(f'/api/teams/{team}').json
        self.assertEqual((profile['name'], profile['members'], profile['rating'], profile['bot']), ('Hồ sơ', 1, 1000, 'bot'))
        self.assertNotIn('code', profile)
        self.assertEqual(self.client.get('/api/teams/nope').status_code, 404)
        feed = self.client.get('/api/feed').json
        self.assertEqual(feed['stats']['teams_ready'], 1)
        other, _ = self.team('Đối thủ')
        self.valid(other)
        result = self.post('/friendly', {'team_id': other, 'submission_id': self.valid(team, FIRST, 'b2')})
        self.assertEqual(result.status_code, 201, result.json)
        self.store.set_team_status(other, 'admin', 'disqualified')
        self.assertEqual(self.client.get(f'/api/teams/{other}').status_code, 404)


class Migration(unittest.TestCase):
    V1 = '''
    CREATE TABLE users (id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
        password TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'player', created REAL NOT NULL);
    CREATE TABLE sessions (hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), csrf TEXT NOT NULL, expires REAL NOT NULL);
    CREATE TABLE submissions (id TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
        source TEXT NOT NULL, hash TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued', log TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
    CREATE TABLE tournaments (id TEXT PRIMARY KEY, name TEXT NOT NULL, format TEXT NOT NULL, rounds INTEGER NOT NULL,
        round INTEGER NOT NULL DEFAULT 0, deadline REAL NOT NULL, status TEXT NOT NULL DEFAULT 'open',
        rules_hash TEXT NOT NULL, config TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE entries (tournament_id TEXT NOT NULL REFERENCES tournaments(id), user_id TEXT NOT NULL REFERENCES users(id),
        submission_id TEXT NOT NULL REFERENCES submissions(id), frozen_hash TEXT, PRIMARY KEY(tournament_id, user_id));
    CREATE TABLE pairings (id TEXT PRIMARY KEY, tournament_id TEXT NOT NULL REFERENCES tournaments(id),
        round INTEGER NOT NULL, a TEXT NOT NULL REFERENCES users(id), b TEXT REFERENCES users(id));
    CREATE TABLE matches (id TEXT PRIMARY KEY, pairing_id TEXT REFERENCES pairings(id),
        p1 TEXT NOT NULL REFERENCES submissions(id), p2 TEXT NOT NULL REFERENCES submissions(id),
        requester TEXT REFERENCES users(id), status TEXT NOT NULL DEFAULT 'queued', winner TEXT, reason TEXT,
        replay TEXT, progress TEXT, rules_hash TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, ref TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
        token TEXT, expires REAL, attempts INTEGER NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL);
    CREATE UNIQUE INDEX job_ref ON jobs(kind,ref);
    CREATE INDEX job_queue ON jobs(status,created);
    CREATE INDEX submission_owner ON submissions(user_id,created);
    CREATE TABLE workers (id TEXT PRIMARY KEY, seen REAL NOT NULL, mode TEXT NOT NULL);
    CREATE TABLE audit (id INTEGER PRIMARY KEY, actor TEXT, action TEXT NOT NULL, detail TEXT NOT NULL, created REAL NOT NULL);
    CREATE TABLE rate_limits (key TEXT PRIMARY KEY, start REAL NOT NULL, count INTEGER NOT NULL);
    CREATE TABLE ratings (user_id TEXT PRIMARY KEY REFERENCES users(id), rating INTEGER NOT NULL DEFAULT 1000,
        played INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0, draws INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0);
    CREATE INDEX ratings_order ON ratings(rating DESC,played DESC);
    CREATE TABLE ranked_queue (user_id TEXT PRIMARY KEY REFERENCES users(id), submission_id TEXT NOT NULL REFERENCES submissions(id), joined REAL NOT NULL);
    CREATE TABLE ranked_series (id TEXT PRIMARY KEY, a TEXT NOT NULL REFERENCES users(id), b TEXT NOT NULL REFERENCES users(id),
        a_submission TEXT NOT NULL REFERENCES submissions(id), b_submission TEXT NOT NULL REFERENCES submissions(id),
        first_match TEXT REFERENCES matches(id), second_match TEXT REFERENCES matches(id),
        status TEXT NOT NULL DEFAULT 'running', a_points REAL, rating_change INTEGER, created REAL NOT NULL, finished REAL);
    '''

    def test_v1_database_moves_to_teams_without_losing_results(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'arena.sqlite3'
            with sqlite3.connect(path) as db:
                db.executescript(self.V1)
                db.executemany('INSERT INTO users VALUES(?,?,?,?,?,?)',
                               [('u1', 'alice', 'Alice', 'x', 'player', 1), ('u2', 'bob', 'Alice', 'x', 'player', 2),
                                ('adm', 'admin', 'BTC', 'x', 'admin', 0)])
                db.executemany('INSERT INTO submissions VALUES(?,?,?,?,?,?,?,?)',
                               [('s1', 'u1', 'a', FIRST, 'h', 'valid', '', 1), ('s2', 'u2', 'b', FIRST, 'h', 'valid', '', 2)])
                db.execute("INSERT INTO tournaments VALUES('t1','T','swiss',1,1,0,'running',?,?,0)",
                           (RULES_HASH, json.dumps(LIMITS)))
                db.executemany('INSERT INTO entries VALUES(?,?,?,?)', [('t1', 'u1', 's1', 'h'), ('t1', 'u2', 's2', 'h')])
                db.execute("INSERT INTO pairings VALUES('p1','t1',1,'u1','u2')")
                db.execute("INSERT INTO matches VALUES('m1','p1','s1','s2',NULL,'done','p1','goal',?,NULL,?,1)",
                           (json.dumps({'final_state': {'board': [[{'owner': 'p1', 'type': 'rock'}]]}}), RULES_HASH))
                db.execute("INSERT INTO matches VALUES('m2','p1','s2','s1',NULL,'queued',NULL,NULL,NULL,NULL,?,2)", (RULES_HASH,))
                db.execute("INSERT INTO jobs(id,kind,ref,status,created) VALUES('j1','game','m2','queued',1)")
                db.execute("INSERT INTO ratings VALUES('u1',1016,1,1,0,0)")
            store = Store(directory)
            with store.connect() as db:
                teams = {r['id']: r for r in db.execute('SELECT * FROM teams')}
                self.assertEqual(len(teams), 2)
                self.assertEqual(sorted(t['name'] for t in teams.values()), ['Alice', 'Alice (2)'])
                alice = db.execute("SELECT team_id FROM users WHERE id='u1'").fetchone()[0]
                self.assertIsNone(db.execute("SELECT team_id FROM users WHERE id='adm'").fetchone()[0])
                self.assertEqual(db.execute("SELECT team_id FROM submissions WHERE id='s1'").fetchone()[0], alice)
                self.assertEqual(db.execute("SELECT rating FROM ratings WHERE team_id=?", (alice,)).fetchone()[0], 1016)
                self.assertEqual(db.execute("SELECT kind,p1_left FROM matches WHERE id='m1'").fetchone()[:], ('tournament', 1))
                self.assertEqual(db.execute("SELECT priority FROM jobs WHERE id='j1'").fetchone()[0], 1)
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
                self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
                self.assertEqual(len(store.standings(db, 't1')), 2)
            store.close()
            Store(directory).close()  # idempotent

    def test_v2_database_gains_new_columns_and_keeps_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            team = store.create_team(Base.user(type('T', (), {'store': store})(), 'old'), 'Cũ')
            store.close()
            with sqlite3.connect(Path(directory)/'arena.sqlite3') as db:
                db.execute('ALTER TABLE matches DROP COLUMN plies')
                db.execute('ALTER TABLE ranked_series DROP COLUMN a_rating')
                db.execute('PRAGMA user_version=2')
            store = Store(directory)
            with store.connect() as db:
                self.assertIn('plies', store.columns(db, 'matches'))
                self.assertIn('a_rating', store.columns(db, 'ranked_series'))
                self.assertEqual(db.execute('SELECT name FROM teams WHERE id=?', (team,)).fetchone()[0], 'Cũ')
                self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            store.close()


if __name__ == '__main__':
    unittest.main()
