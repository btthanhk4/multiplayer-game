import concurrent.futures
import hashlib
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

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
            trimmed = {k: v for k, v in packet['state'].items() if k not in ('history', 'repetitions')}
            self.assertEqual(trimmed, replay['final_state'])
            self.assertNotIn('history', replay['final_state'])
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
        maps = [*local.MAP_NAMES, 'random:1f2e3d4c', 'random:0', 'random:ffffffff']
        try:
            for game in range(80):
                name = maps[game % len(maps)]
                packet, state = referee.call('new', map=name), local.new_game(name)
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
                if packet['state']['winReason']:
                    endings.add(packet['state']['winReason'])
        finally:
            referee.close()
        self.assertGreater(moves_checked, 500)
        self.assertIn('goal', endings)
        self.assertTrue(any(e.startswith('eliminate') for e in endings), endings)
        self.assertTrue(any(e.startswith(('material:', 'advance:')) for e in endings), 'some games end by adjudication')

    def load_local(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('local_arena', ROOT / 'sdk/local_arena.py')
        local = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(local)
        return local

    def test_every_map_is_legal_symmetric_safe_and_identical_in_both_engines(self):
        local, referee = self.load_local(), Referee()
        names = [*local.MAP_NAMES, 'random:0', 'random:1', 'random:ffffffff', 'random:deadbeef'] + [f'random:{n * 2654435761 % 2**32:x}' for n in range(1, 120)]
        try:
            for name in names:
                board = local.new_game(name)['board']
                self.assertEqual(referee.call('new', map=name)['state']['board'], board, name)
                mine = {(r, c): p['type'] for r, row in enumerate(board) for c, p in enumerate(row) if p and p['owner'] == 'p1'}
                theirs = {(r, c): p['type'] for r, row in enumerate(board) for c, p in enumerate(row) if p and p['owner'] == 'p2'}
                self.assertEqual(len(mine), 9, name)
                self.assertEqual(sorted(mine.values()), ['paper'] * 3 + ['rock'] * 3 + ['scissors'] * 3, name)
                self.assertEqual({(8 - r, 8 - c): t for (r, c), t in mine.items()}, theirs, f'{name}: Đỏ là bản quay 180° của Xanh')
                self.assertTrue(all(r >= 6 for r, _ in mine), f'{name}: Xanh ở hàng 6–8')
                for goal in local.GOALS.values():
                    self.assertIsNone(board[goal[0]][goal[1]], f'{name}: ô đích để trống')
                gap = min(max(abs(a[0] - b[0]), abs(a[1] - b[1])) for a in mine for b in theirs)
                self.assertGreaterEqual(gap, 4, f'{name}: không có ăn quân ngay từ nước đầu')
                self.assertTrue(local.legal_moves(local.new_game(name)))
            with self.assertRaises(ValueError):
                local.new_game('khong-co')
            self.assertFalse(referee.call.__self__ is None)
            self.assertFalse(referee.call('new', map='standard')['state']['board'] != local.new_game()['board'])
        finally:
            referee.close()
        self.assertEqual(len({str(local.new_game(n)['board']) for n in names[:4]}), 4, 'the named maps differ')
        self.assertGreater(len({str(local.new_game(n)['board']) for n in names[4:]}), 100, 'random maps vary with the seed')

    def test_a_stalled_game_is_decided_by_material_then_advancement(self):
        local, referee = self.load_local(), Referee()

        def board_of(p1, p2):
            board = [[None] * 9 for _ in range(9)]
            for (r, c, t) in p1:
                board[r][c] = {'owner': 'p1', 'type': t}
            for (r, c, t) in p2:
                board[r][c] = {'owner': 'p2', 'type': t}
            return board
        full = [(8, 1, 'rock'), (8, 2, 'paper'), (8, 3, 'scissors')]
        far = [(0, 1, 'rock'), (0, 2, 'paper'), (0, 3, 'scissors')]
        cases = [
            (board_of(full + [(6, 6, 'rock')], far), ('p1', 'material')),                      # one more piece
            (board_of(full, far + [(5, 5, 'paper')]), ('p2', 'material')),
            (board_of([(1, 7, 'rock')], [(6, 2, 'rock')]), ('p1', 'advance')),                  # level on pieces; p1 is 1 from i9, p2 is 2 from a1
            (board_of([(6, 6, 'rock')], [(2, 2, 'rock')]), ('draw', 'level')),                  # 6 from i9 and 6 from a1
        ]
        try:
            for board, expected in cases:
                self.assertEqual(local.adjudicate(board), expected)
                self.assertEqual(local.adjudicated(board, 'quiet'),
                                 ('draw', 'quiet') if expected[0] == 'draw' else (expected[0], f'{expected[1]}:quiet'))
            # The referee answers the move-cap case with the same rule.
            packet = referee.call('new')
            self.assertEqual(packet['state']['board'], local.new_game()['board'])
            verdict = referee.call('adjudicate', cause='ply_limit')['verdict']
            self.assertEqual(verdict, {'winner': 'draw', 'reason': 'ply_limit'}, 'the opening position is level')
        finally:
            referee.close()

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


class ScaleAndFairness(Base):
    def team_with_bot(self, name, source=HUNTER):
        team, user = self.team(name)
        return team, user, self.valid(team, source, name)

    def test_aging_stops_boss_and_practice_games_starving_behind_ranked(self):
        (_, ua, sa), (_, _, sb) = self.team_with_bot("Già A"), self.team_with_bot("Già B")
        with self.store.connect(True) as db:
            old_practice = self.store.create_match(db, sa, sb, ua)           # priority 3
            for _ in range(3):
                self.store.create_match(db, sa, sb, ua, kind='ranked')       # priority 2
        job = self.store.claim('t', 'samples')
        self.assertNotEqual(job['ref'], old_practice, 'fresh ranked games go before a fresh practice game')
        self.store.release(job)
        with self.store.connect(True) as db:
            db.execute("UPDATE jobs SET created=? WHERE ref=?", (now() - 600, old_practice))   # waited 10 minutes
        positions = self.store.queue_positions(self.store.open(), [old_practice])
        self.assertEqual(positions[old_practice], 1)
        self.assertEqual(self.store.claim('t', 'samples')['ref'], old_practice)

    def test_validation_always_beats_aged_games(self):
        (_, ua, sa), (_, _, sb) = self.team_with_bot("Gấp A"), self.team_with_bot("Gấp B")
        with self.store.connect(True) as db:
            game = self.store.create_match(db, sa, sb, ua)
            db.execute("UPDATE jobs SET created=? WHERE ref=?", (now() - 86400, game))
        team, user = self.team('Nộp muộn')
        fresh = self.store.submit(team, user, 'moi', HUNTER + '\n# unique new bot\n')
        self.assertEqual(self.store.claim('t', 'samples')['ref'], fresh)

    def test_back_pressure_refuses_optional_games_but_never_tournaments(self):
        import arena_backend.store as store_module
        (a, ua, sa), (b, ub, sb) = self.team_with_bot('Tải A'), self.team_with_bot('Tải B')
        old_low, old_ranked = store_module.QUEUE_CAP_LOW, store_module.QUEUE_CAP_RANKED
        store_module.QUEUE_CAP_LOW, store_module.QUEUE_CAP_RANKED = 2, 2
        try:
            self.store.practice(a, ua, sa, sa)                               # two games fill the low-priority cap
            with self.assertRaises(Problem) as caught:
                self.store.practice(b, ub, sb, sb)
            self.assertEqual(caught.exception.status, 429)
            self.assertIn('quá tải', str(caught.exception))
            with self.assertRaises(Problem):
                self.store.friendly(b, ub, sb, a)
            with self.store.connect(True) as db:
                for _ in range(2):
                    self.store.create_match(db, sa, sb, ua, kind='ranked')
            with self.assertRaises(Problem) as caught:
                self.store.join_ranked(b, sb)
            self.assertEqual(caught.exception.status, 429)
            with self.store.connect(True) as db:                              # tournaments are exempt
                self.store.create_match(db, sa, sb, ua, kind='tournament')
        finally:
            store_module.QUEUE_CAP_LOW, store_module.QUEUE_CAP_RANKED = old_low, old_ranked

    def test_same_new_bot_from_many_teams_is_graded_once(self):
        source = HUNTER + '\n# everybody pastes this exact file\n'
        teams = [self.team(f'Cùng mã {i}') for i in range(5)]
        ids = [self.store.submit(team, user, 'sao chép', source) for team, user in teams]
        with self.store.connect() as db:
            jobs = db.execute("SELECT COUNT(*) FROM jobs WHERE kind='validate'").fetchone()[0]
            self.assertEqual(jobs, 1, 'one check for five identical submissions')
            statuses = [db.execute('SELECT status FROM submissions WHERE id=?', (i,)).fetchone()[0] for i in ids]
        self.assertEqual(statuses, ['queued'] * 5)
        job = self.store.claim('t', 'samples')
        self.assertEqual(job['ref'], ids[0])
        self.assertTrue(self.store.finish(job, {'valid': True, 'log': 'ổn'}))
        with self.store.connect() as db:
            rows = db.execute('SELECT status,log FROM submissions ORDER BY created,rowid').fetchall()
        self.assertEqual([r['status'] for r in rows], ['valid'] * 5)
        self.assertIn('Dùng chung', rows[4]['log'])
        # Later copies reuse the finished result instead of queueing anything.
        again = self.store.submit(teams[0][0], teams[0][1], 'lại', source, limit=False)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM submissions WHERE id=?', (again,)).fetchone()[0], 'valid')
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE kind='validate'").fetchone()[0], 1)

    def test_followers_share_a_rejection_an_outage_and_the_retry(self):
        source = 'def choose_move(o):\n    return 1 +\n'                      # contains choose_move, so it is accepted for checking
        (t1, u1), (t2, u2) = self.team('Lỗi A'), self.team('Lỗi B')
        first = self.store.submit(t1, u1, 'a', source)
        second = self.store.submit(t2, u2, 'b', source)
        for _ in range(3):                                                   # infrastructure fails three times
            job = self.store.claim('t', 'samples')
            self.store.fail(job, 'docker chết')
        with self.store.connect() as db:
            self.assertEqual({r[0] for r in db.execute('SELECT status FROM submissions')}, {'failed'})
            jid = db.execute("SELECT id FROM jobs WHERE kind='validate'").fetchone()[0]
        self.store.retry(jid, 'admin')
        with self.store.connect() as db:
            self.assertEqual({r[0] for r in db.execute('SELECT status FROM submissions')}, {'queued'})
        job = self.store.claim('t', 'samples')
        self.store.finish(job, {'valid': False, 'log': 'SyntaxError'})
        with self.store.connect() as db:
            rows = {r['id']: r['status'] for r in db.execute('SELECT id,status FROM submissions')}
        self.assertEqual(rows, {first: 'rejected', second: 'rejected'})

    def test_live_view_can_be_switched_off_and_sheds_load(self):
        from arena_backend import runner as runner_module
        team, user = self.team('Tắt xem')
        a, b = self.valid(team, HUNTER, 'a'), self.valid(team, HUNTER, 'b')

        def snapshots():
            with self.store.connect(True) as db:
                self.store.create_match(db, a, b, requester=user)
            seen, real = [], self.store.progress
            self.store.progress = lambda job, state: (seen.append(state), real(job, state))
            old, runner_module.PROGRESS_INTERVAL = runner_module.PROGRESS_INTERVAL, 0
            try:
                Worker(self.store, TrustedExecutor()).run_once()
            finally:
                runner_module.PROGRESS_INTERVAL = old
                self.store.progress = real
            return seen
        self.assertGreater(len(snapshots()), 0)
        self.store.set_settings('admin', {'live_open': False})
        self.assertEqual(snapshots(), [], 'no per-move writes while live viewing is off')
        self.store.set_settings('admin', {'live_open': True})
        old, runner_module.LIVE_SHED_BACKLOG = runner_module.LIVE_SHED_BACKLOG, 0
        try:
            self.assertEqual(snapshots(), [], 'and none while the queue is deeper than the shed threshold')
        finally:
            runner_module.LIVE_SHED_BACKLOG = old
        self.register('liveflag')
        self.assertTrue(self.client.get('/api/health').json['open']['live_open'])

    def test_match_list_orders_running_then_waiting_then_newest_and_searches_by_team(self):
        (_, ua, sa), (_, _, sb), (_, _, sc) = (self.team_with_bot('Sói Bạc'), self.team_with_bot('Cáo Lửa'),
                                               self.team_with_bot('Gấu Nâu'))
        ids = {}
        with self.store.connect(True) as db:
            for label, (p1, p2), status, age in (('old_done', (sa, sb), 'done', 900), ('new_done', (sb, sc), 'done', 100),
                                                 ('cancelled', (sa, sc), 'cancelled', 500), ('queued', (sb, sa), 'queued', 800),
                                                 ('running', (sc, sb), 'running', 700), ('failed', (sc, sa), 'failed', 300)):
                ids[label] = self.store.create_match(db, p1, p2, ua)
                db.execute('UPDATE matches SET status=?,created=? WHERE id=?', (status, now() - age, ids[label]))
        self.register('lister2')
        order = [m['id'] for m in self.client.get('/api/matches').json['items']]
        self.assertEqual(order, [ids[k] for k in ('running', 'queued', 'new_done', 'failed', 'cancelled', 'old_done')])
        self.assertEqual([m['id'] for m in self.client.get('/api/matches?status=done').json['items']],
                         [ids['new_done'], ids['old_done']])
        by_team = {m['id'] for m in self.client.get('/api/matches?q=Gấu').json['items']}
        self.assertEqual(by_team, {ids['new_done'], ids['cancelled'], ids['running'], ids['failed']})
        self.assertEqual({m['id'] for m in self.client.get('/api/matches?q=Sói&status=queued').json['items']}, {ids['queued']})
        self.assertEqual(self.client.get("/api/matches?q=%27%20OR%201%3D1%20--").json['items'], [])
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET role='admin' WHERE username='lister2'")
        listing = self.client.get('/api/admin/matches').json['items']
        self.assertEqual([m['id'] for m in listing][:2], [ids['new_done'], ids['failed']], 'admin list is newest first')
        found = self.client.get(f"/api/admin/matches?q={ids['queued'][:10]}").json['items']
        self.assertEqual([m['id'] for m in found], [ids['queued']])
        self.assertEqual({m['id'] for m in self.client.get('/api/admin/matches?q=Cáo').json['items']},
                         {ids['old_done'], ids['new_done'], ids['queued'], ids['running']})

    def test_health_reports_average_job_times_for_wait_estimates(self):
        team, user, sid = self.team_with_bot('Ước tính')
        with self.store.connect(True) as db:
            self.store.create_match(db, sid, sid, user)
        job = self.store.claim('t', 'samples')
        with self.store.connect(True) as db:
            db.execute('UPDATE jobs SET started=? WHERE id=?', (now() - 4, job['id']))
        self.store.finish(job, {'winner': 'draw', 'reason': 'quiet', 'moves': []})
        average = self.client.get('/api/health').json['avg_seconds']
        self.assertAlmostEqual(average['game'], 4, delta=1.5)


class AdminTournaments(Base):
    def test_edit_name_deadline_rounds_then_only_cosmetic_changes_after_freeze(self):
        tid = self.tournament('swiss', rounds=3)
        self.store.update_tournament(tid, 'admin', name='  Tên   mới  ', description='Thể lệ', rounds=5)
        later = now() + 7200
        self.store.update_tournament(tid, 'admin', deadline=later)
        with self.store.connect() as db:
            t = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
        self.assertEqual((t['name'], t['description'], t['rounds'], t['deadline']), ('Tên mới', 'Thể lệ', 5, later))
        for bad in ({'name': 'ab'}, {'rounds': 0}, {'rounds': 31}, {'rounds': 2.5}, {'deadline': now() + 86400 * 400},
                    {'deadline': 'soon'}, {}):
            with self.assertRaises(Problem, msg=bad):
                self.store.update_tournament(tid, 'admin', **bad)
        with self.assertRaises(Problem) as caught:
            self.store.update_tournament('nope', 'admin', name='Không có')
        self.assertEqual(caught.exception.status, 404)
        self.entrants(tid, 2)
        self.close_deadline(tid)
        self.store.freeze(tid, 'admin')
        self.store.update_tournament(tid, 'admin', name='Đổi tên sau khi khóa')
        with self.assertRaises(Problem) as caught:
            self.store.update_tournament(tid, 'admin', deadline=now() + 600)
        self.assertEqual(caught.exception.status, 409)

    def test_closing_now_makes_the_tournament_freezable(self):
        tid = self.tournament('swiss', rounds=2)
        self.entrants(tid, 3)
        with self.assertRaises(Problem):
            self.store.freeze(tid, 'admin')                    # deadline is still in the future
        self.store.update_tournament(tid, 'admin', deadline=now())
        self.assertEqual(self.store.freeze(tid, 'admin')['accepted'], 3)

    def test_cancel_stops_games_and_blocks_further_rounds(self):
        tid = self.tournament('swiss', rounds=2)
        self.entrants(tid, 4)
        self.close_deadline(tid)
        self.store.freeze(tid, 'admin')
        self.store.next_round(tid, 'admin')
        result = self.store.cancel_tournament(tid, 'admin')
        self.assertEqual(result['games_cancelled'], 4)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM tournaments WHERE id=?', (tid,)).fetchone()[0], 'cancelled')
            self.assertEqual({r[0] for r in db.execute('SELECT status FROM matches')}, {'cancelled'})
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE status='queued' AND kind='game'").fetchone()[0], 0)
        self.assertIsNone(self.store.claim('t', 'samples'))
        for call in (lambda: self.store.cancel_tournament(tid, 'admin'), lambda: self.store.next_round(tid, 'admin')):
            with self.assertRaises(Problem):
                call()

    def test_add_entry_by_code_name_or_id_and_remove_before_freeze(self):
        tid = self.tournament('swiss', rounds=2)
        late, _ = self.team('Đội nộp muộn')
        self.valid(late, name='bản cũ')
        newest = self.valid(late, FIRST, 'bản mới')
        self.close_deadline(tid)                               # after the deadline admins may still add teams
        with self.store.connect() as db:
            code = db.execute('SELECT code FROM teams WHERE id=?', (late,)).fetchone()[0]
        self.assertEqual(self.store.admin_add_entry(tid, code.lower(), 'admin')['team'], 'Đội nộp muộn')
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT submission_id FROM entries WHERE team_id=?', (late,)).fetchone()[0], newest)
        self.assertEqual(self.store.admin_add_entry(tid, 'đội NỘP muộn', 'admin')['team'], 'Đội nộp muộn')   # idempotent
        empty, _ = self.team('Chưa có bot')
        for ref, status in (('', 400), ('khong-co-doi', 404), ('Chưa có bot', 409)):
            with self.assertRaises(Problem) as caught:
                self.store.admin_add_entry(tid, ref, 'admin')
            self.assertEqual(caught.exception.status, status, ref)
        del empty
        with self.assertRaises(Problem) as caught:
            self.store.admin_add_entry(tid, 'b0550000000000000000b055', 'admin')
        self.assertEqual(caught.exception.status, 404)
        self.store.admin_remove_entry(tid, late, 'admin')
        with self.assertRaises(Problem) as caught:
            self.store.admin_remove_entry(tid, late, 'admin')
        self.assertEqual(caught.exception.status, 404)
        self.store.admin_add_entry(tid, code, 'admin')
        rival, _ = self.team('Đội đối thủ')
        self.valid(rival)
        self.store.admin_add_entry(tid, 'Đội đối thủ', 'admin')
        self.store.freeze(tid, 'admin')
        with self.assertRaises(Problem) as caught:
            self.store.admin_remove_entry(tid, rival, 'admin')
        self.assertEqual(caught.exception.status, 409)
        with self.assertRaises(Problem):
            self.store.admin_add_entry(tid, code, 'admin')

    def test_http_endpoints_need_an_admin(self):
        tid = self.tournament('swiss', rounds=2)
        self.register('plain_player')
        for path, body in ((f'/admin/tournaments/{tid}/update', {'name': 'Hack'}), (f'/admin/tournaments/{tid}/cancel', {}),
                           (f'/admin/tournaments/{tid}/entries', {'team': 'x'})):
            self.assertEqual(self.post(path, body).status_code, 403, path)
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET role='admin' WHERE username='plain_player'")
        self.assertEqual(self.post(f'/admin/tournaments/{tid}/update', {'name': 'Giải của BTC'}).status_code, 200)
        team, _ = self.team('Qua HTTP')
        self.valid(team)
        added = self.post(f'/admin/tournaments/{tid}/entries', {'team': 'Qua HTTP'})
        self.assertEqual(added.status_code, 201, added.json)
        self.assertEqual(self.post(f'/admin/tournaments/{tid}/entries/{team}/remove', {}).status_code, 200)
        self.assertEqual(self.post(f'/admin/tournaments/{tid}/cancel', {}).status_code, 200)
        listing = self.client.get('/api/tournaments').json['items']
        self.assertEqual([(t['name'], t['status']) for t in listing], [('Giải của BTC', 'cancelled')])


class VersionedAssets(Base):
    def test_every_page_asks_for_scripts_and_styles_with_the_build_version(self):
        import re
        from arena_backend.app import ASSET_VERSION
        for path in ('/', '/arena/', '/arena/admin', '/arena/guide.html'):
            html = self.client.get(path).get_data(as_text=True)
            assets = re.findall(r'(?:src|href)="(\./[\w.-]+\.(?:js|css)[^"]*)"', html)
            self.assertTrue(assets, path)
            for asset in assets:
                self.assertTrue(asset.endswith(f'?v={ASSET_VERSION}'), (path, asset))
        # Modules import each other with the same stamp, so a stale copy of one can never meet a new copy of another.
        for name in ('arena', 'admin', 'common', 'watch'):
            code = self.client.get(f'/arena/{name}.js?v={ASSET_VERSION}').get_data(as_text=True)
            imports = re.findall(r"from\s+'(\.{1,2}/[\w./-]+\.js[^']*)'", code)
            self.assertTrue(all(i.endswith(f'?v={ASSET_VERSION}') for i in imports), (name, imports))
        self.assertIn(f"../rules.js?v={ASSET_VERSION}", self.client.get('/arena/common.js').get_data(as_text=True))

    def test_only_exact_versioned_urls_are_cached_for_long(self):
        from arena_backend.app import ASSET_VERSION
        stamped = self.client.get(f'/arena/admin.js?v={ASSET_VERSION}')
        self.assertEqual(stamped.headers['Cache-Control'], 'public, max-age=31536000, immutable')
        self.assertEqual(stamped.mimetype, 'text/javascript')
        for url in ('/arena/admin.js', '/arena/admin.js?v=old', '/arena/admin.js?v='):
            self.assertEqual(self.client.get(url).headers['Cache-Control'], 'no-cache', url)
        for page in ('/', '/arena/admin', '/arena/guide.html'):
            self.assertEqual(self.client.get(page).headers['Cache-Control'], 'no-cache', page)
        css = self.client.get(f'/arena/arena.css?v={ASSET_VERSION}')
        self.assertEqual((css.mimetype, css.headers['Cache-Control']), ('text/css', 'public, max-age=31536000, immutable'))
        self.assertEqual(self.client.get(f'/rules.js?v={ASSET_VERSION}').status_code, 200)
        # Revalidation works, so an unstamped request costs a 304 rather than a download.
        tag = self.client.get('/arena/admin.js').headers['ETag']
        self.assertEqual(self.client.get('/arena/admin.js', headers={'If-None-Match': tag}).status_code, 304)
        self.assertEqual(self.client.get('/api/health').headers['Cache-Control'], 'no-store')

    def test_version_changes_when_any_script_changes(self):
        import hashlib
        from arena_backend.app import ASSET_VERSION, VERSIONED_FILES
        from arena_backend.store import ROOT as repo
        def digest(extra=b''):
            return hashlib.sha256(b''.join((repo / n).read_bytes() for n in VERSIONED_FILES) + extra).hexdigest()[:10]
        self.assertEqual(digest(), ASSET_VERSION)
        self.assertNotEqual(digest(b'x'), ASSET_VERSION)

    def test_forms_without_javascript_never_put_a_password_in_the_address(self):
        import re
        for page in ('/', '/arena/admin'):
            html = self.client.get(page).get_data(as_text=True)
            forms = re.findall(r'<form\b[^>]*>', html)
            self.assertTrue(forms)
            for tag in forms:
                self.assertIn('method="post"', tag)
                self.assertIn('action="/arena/enable-javascript"', tag)
            self.assertIn('id="boot-note"', html)
        answer = self.client.post('/arena/enable-javascript', data={'username': 'a', 'password': 'secret-never-echoed'})
        self.assertEqual(answer.status_code, 400)
        text = answer.get_data(as_text=True)
        self.assertIn('chưa được gửi đi', text)
        self.assertNotIn('secret-never-echoed', text)
        self.assertEqual(self.client.get('/arena/enable-javascript').status_code, 404)   # only a POST target


class DeleteUser(Base):
    def make_admin(self, name):
        user = self.user(name)
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET role='admin' WHERE id=?", (user,))
        return user

    def join(self, team_id, name):
        user = self.user(name)
        with self.store.connect() as db:
            code = db.execute('SELECT code FROM teams WHERE id=?', (team_id,)).fetchone()[0]
        self.store.join_team(user, code)
        return user

    def exists(self, table, column, value):
        with self.store.connect() as db:
            return db.execute(f'SELECT 1 FROM {table} WHERE {column}=?', (value,)).fetchone() is not None

    def test_member_leaves_team_keeps_bots_and_loses_sessions(self):
        admin = self.make_admin('boss')
        team, owner = self.team('Đội còn lại')
        member = self.join(team, 'thanh_vien')
        sid = self.valid(team)
        with self.store.connect(True) as db:
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)', ('h' * 64, member, 'csrf', now() + 600))
        result = self.store.delete_user(member, admin)
        self.assertEqual(result, {'ok': True, 'team_kept': True, 'team_deleted': None})
        self.assertFalse(self.exists('users', 'id', member))
        self.assertFalse(self.exists('sessions', 'user_id', member))
        self.assertTrue(self.exists('teams', 'id', team))
        self.assertTrue(self.exists('submissions', 'id', sid))
        self.assertEqual(len(self.store.team_members(self.store.open(), team)), 1)
        with self.store.connect() as db:
            audit = db.execute("SELECT actor,detail FROM audit WHERE action='user.deleted'").fetchone()
        self.assertEqual(audit['actor'], admin)
        self.assertIn('thanh_vien', audit['detail'])
        del owner

    def test_owner_deleted_hands_the_team_to_the_next_member(self):
        admin = self.make_admin('boss')
        team, owner = self.team('Đổi trưởng nhóm')
        member = self.join(team, 'ke_nhiem')
        self.store.delete_user(owner, admin)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT owner FROM teams WHERE id=?', (team,)).fetchone()[0], member)

    def test_last_member_with_a_bot_leaves_a_team_that_can_still_play_but_not_queue(self):
        admin = self.make_admin('boss')
        team, owner = self.team('Chỉ một người')
        sid = self.valid(team)
        self.store.join_ranked(team, sid)
        result = self.store.delete_user(owner, admin)
        self.assertTrue(result['team_kept'])
        self.assertTrue(self.exists('teams', 'id', team), 'results and Elo history stay')
        self.assertFalse(self.exists('ranked_queue', 'team_id', team), 'nobody is left to answer a ranked match')
        with self.store.connect() as db:
            self.assertIsNone(db.execute('SELECT owner FROM teams WHERE id=?', (team,)).fetchone()[0])

    def test_team_without_any_history_disappears_with_its_only_member(self):
        admin = self.make_admin('boss')
        team, owner = self.team('Chưa làm gì')
        self.assertEqual(self.store.delete_user(owner, admin), {'ok': True, 'team_kept': False, 'team_deleted': None})
        self.assertFalse(self.exists('teams', 'id', team))

    def test_refuses_self_unknown_and_the_last_admin(self):
        first = self.make_admin('quan_tri_1')
        for target, status in ((first, 409), ('nope', 404)):
            with self.assertRaises(Problem) as caught:
                self.store.delete_user(target, first)
            self.assertEqual(caught.exception.status, status)
        player, _ = self.team('Người chơi')
        del player
        lone = self.make_admin('quan_tri_2')
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET disabled=1 WHERE id=?", (lone,))   # a disabled admin cannot be relied on
        outsider = self.user('nguoi_ngoai')
        with self.assertRaises(Problem) as caught:
            self.store.delete_user(first, outsider)
        self.assertEqual(caught.exception.status, 409, 'the only usable admin stays')
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET disabled=0 WHERE id=?", (lone,))
        self.store.delete_user(first, lone)                                    # now another admin exists
        self.assertFalse(self.exists('users', 'id', first))

    def test_http_needs_an_admin_and_signs_the_deleted_user_out(self):
        victim_client = self.app.test_client()
        origin = {'Origin': 'http://localhost:8080'}
        registered = victim_client.post('/api/register', json={'username': 'se_bi_xoa', 'name': 'Bị xóa',
                                                               'password': 'test-only-password-123', 'invite': 'test-invite'}, headers=origin)
        victim = registered.json['user']['id']
        self.assertEqual(victim_client.get('/api/me').json['user']['username'], 'se_bi_xoa')
        self.register('quan_tri_web')
        self.assertEqual(self.post(f'/admin/users/{victim}', {'action': 'delete'}).status_code, 403)
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET role='admin' WHERE username='quan_tri_web'")
        self.assertEqual(self.post(f'/admin/users/{victim}', {'action': 'delete'}).status_code, 200)
        self.assertIsNone(victim_client.get('/api/me').json['user'])
        login = victim_client.post('/api/login', json={'username': 'se_bi_xoa', 'password': 'test-only-password-123'}, headers=origin)
        self.assertEqual(login.status_code, 401)
        users = self.client.get('/api/admin/users?q=se_bi_xoa').json['items']
        self.assertEqual(users, [])
        mine = self.client.get('/api/me').json['user']['id']
        self.assertEqual(self.post(f'/admin/users/{mine}', {'action': 'delete'}).status_code, 409)
        self.assertEqual(self.post('/admin/users/missing', {'action': 'delete'}).status_code, 404)
        # The name can be registered again by a new person.
        again = victim_client.post('/api/register', json={'username': 'se_bi_xoa', 'name': 'Người mới', 'password': 'test-only-password-123',
                                                           'invite': 'test-invite'}, headers=origin)
        self.assertEqual(again.status_code, 200, again.json)


class MapsAndAdjudication(Base):
    FIRST_MOVE = "def choose_move(o):\n    m = o['legalMoves'][0]\n    return {'from': m['from'], 'to': m['to']}\n"
    LAST_MOVE = "def choose_move(o):\n    m = o['legalMoves'][-1]\n    return {'from': m['from'], 'to': m['to']}\n"
    # Fails the game (a technical loss) unless it is told the right map and the board really is that map.
    EXPECT = ("EXPECTED = {expected!r}\n"
              "def choose_move(o):\n"
              "    assert o['map'] == EXPECTED or (EXPECTED == 'random' and o['map'].startswith('random:')), o['map']\n"
              "    m = o['legalMoves'][0]\n"
              "    return {{'from': m['from'], 'to': m['to']}}\n")

    def load_local(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location('local_arena', ROOT / 'sdk/local_arena.py')
        local = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(local)
        return local

    def play_one(self, source_a, source_b, map_name):
        team, user = self.team('Bản đồ ' + uid()[:5])
        a, b = self.valid(team, source_a, 'a'), self.valid(team, source_b, 'b')
        with self.store.connect(True) as db:
            mid = self.store.create_match(db, a, b, requester=user, map_name=map_name)
        Worker(self.store, TrustedExecutor()).run_once()
        with self.store.connect() as db:
            row = dict(db.execute('SELECT * FROM matches WHERE id=?', (mid,)).fetchone())
        return row, json.loads(row['replay'])

    def test_bots_are_told_the_map_and_it_is_recorded_in_the_replay(self):
        for name in ('standard', 'wall', 'flank', 'block', 'random:1a2b3c4d'):
            row, replay = self.play_one(self.EXPECT.format(expected=name), self.EXPECT.format(expected=name), name)
            self.assertEqual(row['status'], 'done')
            self.assertFalse(row['reason'].startswith('technical'), (name, row['reason']))
            self.assertEqual((row['map'], replay['map']), (name, name))

    def test_stalled_games_are_decided_the_same_way_by_the_arena_and_the_local_tool(self):
        local = self.load_local()
        first, last = local.Player('first', local.bot_first, None), local.Player('last', lambda o: {
            'from': o['legalMoves'][-1]['from'], 'to': o['legalMoves'][-1]['to']}, None)
        for name in ('standard', 'wall', 'flank', 'block'):
            row, replay = self.play_one(self.FIRST_MOVE, self.LAST_MOVE, name)
            winner, reason, plies, *_ = local.play(first, last, 1, False, name)
            self.assertEqual((row['winner'], row['reason']), (winner, reason), name)
            self.assertEqual(len(replay['moves']), plies, name)
            self.assertFalse(row['reason'].startswith('technical'))

    def test_move_cap_and_blocked_side_are_adjudicated_not_drawn(self):
        referee = Referee()
        try:
            referee.call('new')
            for cause in ('ply_limit', 'no_legal_moves'):
                self.assertEqual(referee.call('adjudicate', cause=cause)['verdict'], {'winner': 'draw', 'reason': cause})
        finally:
            referee.close()
        self.assertEqual(LIMITS['ply_limit'], 'adjudicate')
        self.assertEqual(LIMITS['no_legal_moves'], 'adjudicate')

    def test_map_lists_in_server_pages_and_rules_agree(self):
        import re
        from arena_backend.store import MAP_IDS
        local = self.load_local()
        self.assertEqual(MAP_IDS, {*local.MAP_NAMES, 'random'})
        for page, select in (('arena/index.html', 'practice-map'), ('arena/admin.html', 'map'), ('arena/admin.html', 'edit-map')):
            html = (ROOT / page).read_text(encoding='utf-8')
            tag = re.search(rf'<select[^>]*id="{select}"[^>]*>(.*?)</select>', html, re.S)
            self.assertIsNotNone(tag, (page, select))
            self.assertEqual(set(re.findall(r'value="([^"]+)"', tag.group(1))), MAP_IDS, (page, select))
        common = (ROOT / 'arena/common.js').read_text(encoding='utf-8')
        for name in MAP_IDS:
            self.assertIn(f'{name}:', common, f'label for {name}')

    def test_practice_map_choice_random_gives_both_games_the_same_new_formation(self):
        team, user = self.team('Tập bản đồ')
        a, b = self.valid(team, HUNTER, 'a'), self.valid(team, HUNTER, 'b')
        ids = self.store.practice(team, user, a, b, 'random')
        with self.store.connect() as db:
            maps = [db.execute('SELECT map FROM matches WHERE id=?', (i,)).fetchone()[0] for i in ids]
        self.assertEqual(maps[0], maps[1])
        self.assertRegex(maps[0], r'^random:[0-9a-f]{8}$')
        with self.assertRaises(Problem):
            self.store.practice(team, user, a, b, 'khong-co-ban-do')
        self.client.post('/api/register', json={'username': 'mapper', 'name': 'Mapper', 'password': 'test-only-password-123',
                                                'invite': 'test-invite'}, headers={'Origin': 'http://localhost:8080'})
        health = self.client.get('/api/health').json
        self.assertEqual([m['id'] for m in health['maps']], ['standard', 'wall', 'flank', 'block', 'random'])

    def test_tournament_map_applies_per_round_and_can_change_between_rounds(self):
        tid = self.tournament('swiss', rounds=3)
        self.entrants(tid, 6)
        self.close_deadline(tid)
        self.store.freeze(tid, 'admin')
        with self.assertRaises(Problem):
            self.store.update_tournament(tid, 'admin', map_choice='moon')
        self.store.update_tournament(tid, 'admin', map_choice='wall')
        self.store.next_round(tid, 'admin')
        self.finish_round(tid)
        self.store.update_tournament(tid, 'admin', map_choice='random')
        self.store.next_round(tid, 'admin')
        with self.store.connect() as db:
            rows = db.execute('SELECT p.round,p.id pid,m.map FROM matches m JOIN pairings p ON p.id=m.pairing_id '
                              'ORDER BY p.round,p.slot').fetchall()
        first_round = {r['map'] for r in rows if r['round'] == 1}
        self.assertEqual(first_round, {'wall'}, 'games already made keep their map')
        second = {}
        for r in rows:
            if r['round'] == 2:
                second.setdefault(r['pid'], set()).add(r['map'])
        self.assertTrue(all(len(v) == 1 and next(iter(v)).startswith('random:') for v in second.values()),
                        'both games of a pair share one random formation')
        self.assertGreater(len({next(iter(v)) for v in second.values()}), 1, 'different pairs get different formations')

    def test_admin_creates_a_tournament_with_a_map_over_http(self):
        self.register('map_admin')
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET role='admin' WHERE username='map_admin'")
        ok = self.post('/admin/tournaments', {'name': 'Vòng trong', 'format': 'knockout', 'rounds': 3,
                                              'deadline': now() + 3600, 'map': 'block'})
        self.assertEqual(ok.status_code, 201, ok.json)
        self.assertEqual(self.post('/admin/tournaments', {'name': 'Sai bản đồ', 'format': 'swiss', 'rounds': 3,
                                                          'deadline': now() + 3600, 'map': 'moon'}).status_code, 400)
        listing = self.client.get('/api/tournaments').json['items']
        self.assertEqual([(t['name'], t['map']) for t in listing], [('Vòng trong', 'block')])
        self.assertEqual(self.post(f"/admin/tournaments/{ok.json['id']}/update", {'map': 'flank'}).status_code, 200)
        self.assertEqual(self.client.get('/api/tournaments').json['items'][0]['map'], 'flank')


class DeleteTeam(Base):
    def count(self, sql, *args):
        with self.store.connect() as db:
            return db.execute(sql, args).fetchone()[0]

    def played_team(self):
        """A team with bots, a finished ranked pair against another team, a practice game and a boss run."""
        (a, ua), (b, ub) = self.team('Sẽ bị xóa'), self.team('Ở lại')
        sa, sb = self.valid(a), self.valid(b)
        self.store.join_ranked(a, sa)
        series = self.store.join_ranked(b, sb)
        for _ in range(2):
            job = self.store.claim('t', 'samples')
            self.store.finish(job, {'winner': 'p1', 'reason': 'goal', 'moves': []})
        self.store.practice(a, ua, sa, sa)
        return a, ua, sa, b, ub, sb, series['series_id']

    def test_a_team_that_never_played_a_tournament_disappears_completely(self):
        a, ua, sa, b, ub, sb, series = self.played_team()
        admin = self.user('quan_tri')
        before = self.count('SELECT rating FROM ratings WHERE team_id=?', b)
        self.assertEqual(self.store.delete_team(a, admin), {'mode': 'deleted'})
        self.assertEqual(self.count('SELECT COUNT(*) FROM teams WHERE id=?', a), 0)
        for table, column in (('submissions', 'team_id'), ('ratings', 'team_id'), ('ranked_queue', 'team_id')):
            self.assertEqual(self.count(f'SELECT COUNT(*) FROM {table} WHERE {column}=?', a), 0, table)
        self.assertEqual(self.count('SELECT COUNT(*) FROM ranked_series WHERE id=?', series), 0)
        self.assertEqual(self.count('SELECT COUNT(*) FROM matches'), 0, 'every game of the deleted team is gone')
        self.assertEqual(self.count("SELECT COUNT(*) FROM jobs WHERE kind='game'"), 0)
        self.assertEqual(self.count('SELECT COUNT(*) FROM jobs WHERE ref=?', sa), 0)
        self.assertEqual(self.count('SELECT rating FROM ratings WHERE team_id=?', b), before, 'the opponent keeps its Elo')
        self.assertEqual(self.count('SELECT COUNT(*) FROM submissions WHERE id=?', sb), 1)
        self.assertEqual(self.count('SELECT COUNT(*) FROM users WHERE id=?', ua), 1, 'accounts are not deleted with a team')
        self.assertIsNone(self.count('SELECT team_id FROM users WHERE id=?', ua) or None)
        self.assertEqual(self.count("SELECT COUNT(*) FROM audit WHERE action='team.deleted'"), 1)
        # The same name can be taken again and the player can start a new team.
        self.store.create_team(ua, 'Sẽ bị xóa')

    def test_a_team_with_tournament_history_is_hidden_so_standings_survive(self):
        tid = self.tournament('swiss', rounds=2)
        teams = self.entrants(tid, 3)
        self.close_deadline(tid)
        self.store.freeze(tid, 'admin')
        self.store.next_round(tid, 'admin')
        self.finish_round(tid)
        admin = self.user('quan_tri')
        victim = teams[0]
        with self.store.connect() as db:
            name, code = db.execute('SELECT name,code FROM teams WHERE id=?', (victim,)).fetchone()
        self.assertEqual(self.store.delete_team(victim, admin), {'mode': 'hidden'})
        with self.store.connect() as db:
            row = db.execute('SELECT status,code,owner FROM teams WHERE id=?', (victim,)).fetchone()
        self.assertEqual(row['status'], 'deleted')
        self.assertNotEqual(row['code'], code)
        self.assertIsNone(row['owner'])
        self.assertEqual(len(self.store.standings(self.store.open(), tid)), 3, 'tournament results stay whole')
        self.assertEqual(self.store.create_team(self.user('moi'), name) is not None, True, 'the name is free again')
        newcomer = self.user('vao_muon')
        with self.assertRaises(Problem):
            self.store.join_team(newcomer, code)
        self.register('xem_ho_so')
        self.assertEqual(self.client.get(f'/api/teams/{victim}').status_code, 404)
        self.assertNotIn(victim, [r['team_id'] for r in self.store.ranked_leaderboard()['leaderboard']])

    def test_system_team_unknown_team_and_double_delete_are_refused(self):
        admin = self.user('quan_tri')
        team, _ = self.team('Một lần')
        from arena_backend.store import SYSTEM_TEAM
        with self.store.connect(True) as db:
            db.execute("INSERT INTO teams(id,name,name_key,code,status,created) VALUES(?,?,?,?,'system',?)",
                       (SYSTEM_TEAM, 'Boss', 'boss', 'BOSSBOSS', now()))
        for target, status in ((SYSTEM_TEAM, 409), ('nope', 404)):
            with self.assertRaises(Problem) as caught:
                self.store.delete_team(target, admin)
            self.assertEqual(caught.exception.status, status)
        self.store.delete_team(team, admin)
        with self.assertRaises(Problem) as caught:
            self.store.delete_team(team, admin)
        self.assertEqual(caught.exception.status, 404)

    def test_cleanup_previews_then_removes_only_teams_without_members(self):
        admin = self.user('quan_tri')
        kept, _ = self.team('Còn người')
        orphan_a, owner_a = self.team('Trống A')
        orphan_b, owner_b = self.team('Trống B')
        self.valid(orphan_a)
        for owner in (owner_a, owner_b):
            self.store.leave_team(owner)
        preview = self.store.cleanup_empty_teams(admin)
        self.assertEqual((preview['count'], sorted(preview['names'])), (1, ['Trống A']),
                         'a team with no bots and no history is already removed when its last member leaves')
        self.assertEqual(self.count('SELECT COUNT(*) FROM teams WHERE id=?', orphan_a), 1, 'a preview deletes nothing')
        done = self.store.cleanup_empty_teams(admin, confirm=True)
        self.assertEqual((done['count'], done['deleted'], done['hidden']), (1, 1, 0))
        self.assertEqual(self.count('SELECT COUNT(*) FROM teams WHERE id=?', orphan_a), 0)
        self.assertEqual(self.count('SELECT COUNT(*) FROM teams WHERE id=?', kept), 1)
        self.assertEqual(self.store.cleanup_empty_teams(admin)['count'], 0)

    def test_deleting_the_last_member_can_take_the_team_along(self):
        admin = self.user('quan_tri')
        solo, owner = self.team('Một mình')
        self.valid(solo)
        pair, first = self.team('Hai người')
        second = self.user('ban_cung_doi')
        with self.store.connect() as db:
            code = db.execute('SELECT code FROM teams WHERE id=?', (pair,)).fetchone()[0]
        self.store.join_team(second, code)
        kept = self.store.delete_user(first, admin, delete_team=True)
        self.assertEqual((kept['team_kept'], kept['team_deleted']), (True, None), 'a team that still has a member is never deleted')
        result = self.store.delete_user(owner, admin, delete_team=True)
        self.assertEqual((result['team_kept'], result['team_deleted']), (False, 'deleted'))
        self.assertEqual(self.count('SELECT COUNT(*) FROM teams WHERE id=?', solo), 0)
        self.assertEqual(self.count('SELECT COUNT(*) FROM submissions WHERE team_id=?', solo), 0)

    def test_http_endpoints_and_listings(self):
        a, ua, sa, b, ub, sb, series = self.played_team()
        self.register('quan_tri_web')
        victim = self.client.get('/api/me').json['user']['id']
        self.assertEqual(self.post(f'/admin/teams/{a}', {'action': 'delete'}).status_code, 403)
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET role='admin' WHERE id=?", (victim,))
            db.execute("INSERT INTO teams(id,name,name_key,code,status,created) VALUES('b0550000000000000000b055','Boss','boss','BOSSBOSS','system',?)", (now(),))
        listing = self.client.get('/api/admin/teams').json['items']
        self.assertEqual({t['name'] for t in listing}, {'Sẽ bị xóa', 'Ở lại'}, 'the boss team is not listed')
        self.assertEqual(self.post('/admin/teams-cleanup', {}).json['count'], 0)
        users = self.client.get('/api/admin/users?q=u').json['items']
        self.assertTrue(all('team_size' in u and 'team_id' in u for u in users))
        self.assertEqual(self.post(f'/admin/teams/{a}', {'action': 'delete'}).json, {'mode': 'deleted'})
        self.assertEqual({t['name'] for t in self.client.get('/api/admin/teams').json['items']}, {'Ở lại'})
        self.assertEqual(self.post('/admin/teams/b0550000000000000000b055', {'action': 'delete'}).status_code, 409)
        self.assertEqual(self.post(f'/admin/teams/{a}', {'action': 'delete'}).status_code, 404)


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
