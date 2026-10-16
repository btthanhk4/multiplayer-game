import concurrent.futures
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from arena_backend.app import create_app
from arena_backend.store import Store, ROOT, RULES_HASH, LIMITS, Problem, uid, now, swiss_pairs
from arena_backend.runner import Worker, Executor, Referee, observation, checked_move, BotFault, InfrastructureError


class ArenaTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = create_app(self.tmp.name, origin='http://localhost:8080', registration_code='test-invite')
        self.app.testing = True
        self.store = self.app.extensions['store']
        self.client = self.app.test_client()
        self.csrf = ''

    def tearDown(self):
        self.tmp.cleanup()

    def post(self, path, data, client=None, csrf=None):
        return (client or self.client).post('/api'+path, json=data,
                    headers={'Origin':'http://localhost:8080','X-CSRF-Token':self.csrf if csrf is None else csrf})

    def register(self, username='alice'):
        result = self.post('/register', {'username':username, 'name':username.title(),
                                        'password':'test-only-password-123', 'invite':'test-invite'})
        self.assertEqual(result.status_code, 200, result.json)
        self.csrf = result.json['csrf']
        return result.json['user']['id']

    def admin(self, user):
        with self.store.connect(True) as db:
            db.execute("UPDATE users SET role='admin' WHERE id=?", (user,))

    def user(self, name):
        user = uid()
        with self.store.connect(True) as db:
            db.execute('INSERT INTO users VALUES(?,?,?,?,?,?)', (user,name,name,'unused','player',now()))
        return user

    def valid(self, user, name='bot'):
        sid = self.store.submit(user,name,(ROOT/'sdk/examples/hunter.py').read_text(encoding='utf-8'))
        with self.store.connect(True) as db:
            db.execute("UPDATE submissions SET status='valid' WHERE id=?", (sid,))
            db.execute("UPDATE jobs SET status='done' WHERE ref=?", (sid,))
        return sid

    def tournament(self, fmt='swiss', rounds=2):
        tid=uid()
        with self.store.connect(True) as db:
            db.execute('INSERT INTO tournaments(id,name,format,rounds,deadline,rules_hash,config,created) VALUES(?,?,?,?,?,?,?,?)',
                       (tid,'Test tournament',fmt,rounds,now()+500,RULES_HASH,json.dumps(LIMITS),now()))
        return tid

    def close_deadline(self, tid):
        with self.store.connect(True) as db:
            db.execute('UPDATE tournaments SET deadline=? WHERE id=?', (now()-1,tid))

    def test_auth_csrf_privacy_and_static_allowlist(self):
        self.assertEqual(self.post('/register',{'username':'alice','name':'Alice','password':'test-only-password-123','invite':'bad'}).status_code,403)
        user=self.register()
        cookie=self.client.get_cookie('ott_session')
        self.assertTrue(cookie.http_only)
        self.assertEqual(cookie.same_site, 'Lax')
        self.assertEqual(self.post('/submissions',{'name':'bot','source':'x'},csrf='wrong').status_code,403)
        self.assertEqual(self.client.post('/api/submissions',json={'name':'bot','source':'x'},headers={'Origin':'https://evil.example','X-CSRF-Token':self.csrf}).status_code,403)
        self.assertEqual(self.post('/admin/tournaments',{}).status_code,403)
        sid=self.post('/submissions',{'name':'test','source':'def choose_move(o): return {}'}).json['id']
        other=self.app.test_client()
        self.assertEqual(other.get('/api/submissions/'+sid+'/source').status_code,401)
        for path in ('/.env','/arena-data/arena.sqlite3','/arena_backend/app.py','/.git/config','/arena/../../arena_backend/app.py'):
            self.assertEqual(self.client.get(path).status_code,404,path)
        response=self.client.get('/arena/')
        self.assertEqual(response.status_code,200)
        response.close()
        response=self.client.get('/')
        self.assertEqual(response.status_code,200)
        self.assertIn(b'id="ranked"',response.data)
        response.close()
        self.assertEqual(self.client.get('/index.html').status_code,404)
        self.assertEqual(self.client.get('/main.js').status_code,404)
        self.post('/logout',{})
        self.assertIsNone(self.client.get('/api/me').json['user'])
        self.assertEqual(self.post('/login',{'username':'alice','password':'incorrect'}).status_code,401)
        self.assertEqual(self.post('/login',{'username':'alice','password':'test-only-password-123'}).status_code,200)

    def test_snapshot_deadline_ownership_and_pairing(self):
        a,b=self.user('alice'),self.user('bob')
        sa,sb=self.valid(a),self.valid(b)
        tid=self.tournament(rounds=1)
        with self.assertRaises(Problem): self.store.join(a,tid,sb)
        self.store.join(a,tid,sa); self.store.join(b,tid,sb)
        with self.assertRaises(Problem): self.store.freeze(tid,a)
        self.close_deadline(tid)
        with self.assertRaises(Problem): self.store.join(a,tid,sa)
        self.store.freeze(tid,a)
        with self.assertRaises(Problem): self.store.join(a,tid,sa)
        with self.store.connect(True) as db:
            db.execute('UPDATE tournaments SET config=? WHERE id=?',(json.dumps({**LIMITS,'cpu_seconds':2}),tid))
        with self.assertRaises(Problem): self.store.next_round(tid,a)
        with self.store.connect(True) as db:
            db.execute('UPDATE tournaments SET config=? WHERE id=?',(json.dumps(LIMITS),tid))
        self.store.next_round(tid,a)
        with self.store.connect() as db:
            games=db.execute('SELECT * FROM matches ORDER BY id').fetchall()
            self.assertEqual(len(games),2)
            self.assertEqual({(g['p1'],g['p2']) for g in games},{(sa,sb),(sb,sa)})
        with self.assertRaises(Problem): self.store.next_round(tid,a)
        with self.store.connect(True) as db:
            db.execute("UPDATE matches SET status='done',winner='p1'")
        self.store.next_round(tid,a)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM tournaments WHERE id=?',(tid,)).fetchone()[0],'completed')
            scores=self.store.standings(db,tid)
            self.assertEqual([s['points'] for s in scores],[.5,.5])
            self.assertEqual([s['rank'] for s in scores],[1,1])

    def test_round_robin_has_every_pair_once_and_balanced_byes(self):
        tid=self.tournament('round_robin')
        for i in range(5):
            user=self.user('user'+str(i)); self.store.join(user,tid,self.valid(user))
        self.close_deadline(tid); self.store.freeze(tid,'admin')
        for _ in range(5):
            self.store.next_round(tid,'admin')
            with self.store.connect(True) as db: db.execute("UPDATE matches SET status='done',winner='draw'")
        with self.store.connect() as db:
            pairs=db.execute('SELECT a,b FROM pairings WHERE tournament_id=?',(tid,)).fetchall()
            real=[frozenset(p) for p in pairs if p['b']]
            self.assertEqual(len(real),10); self.assertEqual(len(set(real)),10)
            self.assertEqual(len({p['a'] for p in pairs if p['b'] is None}),5)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM matches').fetchone()[0],20)

    def test_lease_fencing_duplicate_results_and_concurrent_claim(self):
        user=self.user('alice'); sid=self.store.submit(user,'bot','def choose_move(o): return {}')
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            jobs=list(pool.map(lambda _:self.store.claim(uid(),'samples'),range(2)))
        self.assertEqual(sum(j is not None for j in jobs),1)
        old=next(j for j in jobs if j)
        with self.store.connect(True) as db: db.execute('UPDATE jobs SET expires=?',(now()-10,))
        new=self.store.claim('second','samples')
        self.assertNotEqual(old['token'],new['token'])
        self.assertFalse(self.store.finish(old,{'valid':False,'log':'stale'}))
        self.assertTrue(self.store.finish(new,{'valid':True,'log':'ok'}))
        self.assertFalse(self.store.finish(new,{'valid':False,'log':'duplicate'}))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT status FROM submissions WHERE id=?',(sid,)).fetchone()[0],'valid')

    def test_samples_validation_game_and_full_replay(self):
        user=self.user('alice')
        source=(ROOT/'sdk/examples/hunter.py').read_text(encoding='utf-8')
        sid=self.store.submit(user,'hunter',source)
        worker=Worker(self.store,Executor('samples'))
        self.assertTrue(worker.run_once())
        with self.store.connect(True) as db:
            self.assertEqual(db.execute('SELECT status FROM submissions WHERE id=?',(sid,)).fetchone()[0],'valid')
            mid=self.store.create_match(db,sid,sid,requester=user)
        self.assertTrue(worker.run_once())
        with self.store.connect() as db:
            match=db.execute('SELECT * FROM matches WHERE id=?',(mid,)).fetchone()
            self.assertEqual(match['status'],'done',dict(match))
            replay=json.loads(match['replay'])
        referee=Referee()
        try:
            packet=referee.call('new')
            for move in replay['moves']:
                packet=referee.call('move',**{k:move[k] for k in ('from','to')})
            self.assertEqual(packet['state'],replay['final_state'])
            self.assertEqual(len(replay['moves']),packet['state']['revision'])
            self.assertEqual(packet['state']['winner'],replay['winner'])
        finally: referee.close()

    def test_sample_mode_rejects_modified_code_and_strict_moves(self):
        ex=Executor('samples')
        with self.assertRaises(InfrastructureError): ex.choose('import os\nos.remove("secret")',{})
        with self.assertRaises(BotFault): checked_move({'from':[True,0],'to':[1,0]},[])
        with self.assertRaises(BotFault): checked_move({'from':[0,0],'to':[1,0],'extra':1},[])

    def test_backup_is_consistent(self):
        self.register()
        backup=Path(self.tmp.name)/'copy.sqlite3'
        self.store.backup(backup)
        import sqlite3
        with sqlite3.connect(backup) as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            self.assertEqual(db.execute('SELECT COUNT(*) FROM users').fetchone()[0],1)

    def test_public_origin_requires_invite_and_https(self):
        with self.assertRaises(RuntimeError): create_app(self.tmp.name,origin='http://arena.example.org',registration_code='x')
        with self.assertRaises(RuntimeError): create_app(self.tmp.name,origin='https://arena.example.org',registration_code='')

    def test_500_entries_make_250_pairs_and_500_games(self):
        tid=self.tournament(rounds=9)
        source=(ROOT/'sdk/examples/first.py').read_text(encoding='utf-8')
        digest=hashlib.sha256(source.encode()).hexdigest()
        with self.store.connect(True) as db:
            for i in range(500):
                user,sid=uid(),uid()
                db.execute('INSERT INTO users VALUES(?,?,?,?,?,?)',(user,'user'+str(i),'Player '+str(i),'unused','player',now()))
                db.execute("INSERT INTO submissions(id,user_id,name,source,hash,status,created) VALUES(?,?,?,?,?,'valid',?)",
                           (sid,user,'Bot',source,digest,now()))
                db.execute('INSERT INTO entries VALUES(?,?,?,NULL)',(tid,user,sid))
        self.close_deadline(tid); self.store.freeze(tid,'admin'); self.store.next_round(tid,'admin')
        with self.store.connect() as db:
            pairs=db.execute('SELECT a,b FROM pairings WHERE tournament_id=?',(tid,)).fetchall()
            self.assertEqual(len(pairs),250)
            self.assertEqual(len({user for pair in pairs for user in pair}),500)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM matches').fetchone()[0],500)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM jobs WHERE kind='game'").fetchone()[0],500)

    def test_bot_fault_forfeits_but_infrastructure_failure_does_not(self):
        class FailingExecutor(Executor):
            def choose(self, source, obs):
                raise BotFault('Bot lỗi thực thi hoặc vượt CPU/RAM: private diagnostic')
        user=self.user('alice'); sid=self.valid(user)
        with self.store.connect(True) as db: mid=self.store.create_match(db,sid,sid,user)
        Worker(self.store,FailingExecutor('samples')).run_once()
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM matches WHERE id=?',(mid,)).fetchone()
            self.assertEqual(row['winner'],'p2')
            self.assertEqual(row['status'],'done')
            self.assertNotIn('private diagnostic',row['replay'])
        class BrokenExecutor(Executor):
            def choose(self, source, obs): raise InfrastructureError('Docker unavailable')
        with self.store.connect(True) as db: mid=self.store.create_match(db,sid,sid,user)
        Worker(self.store,BrokenExecutor('samples')).run_once()
        with self.store.connect() as db:
            row=db.execute('SELECT * FROM matches WHERE id=?',(mid,)).fetchone()
            self.assertEqual(row['status'],'failed')
            self.assertIsNone(row['winner'])

    def test_ranked_queue_swaps_colors_and_settles_once(self):
        a,b=self.user('alice'),self.user('bob')
        sa,sb=self.valid(a),self.valid(b)
        self.assertEqual(self.store.join_ranked(a,sa)['status'],'queued')
        with self.assertRaises(Problem): self.store.join_ranked(a,sa)
        result=self.store.join_ranked(b,sb)
        self.assertEqual(result['status'],'matched')
        self.assertEqual(len(result['matches']),2)
        with self.assertRaises(Problem): self.store.join_ranked(a,sa)
        with self.store.connect() as db:
            games=db.execute('SELECT p1,p2 FROM matches WHERE id IN (?,?) ORDER BY created,id',
                             result['matches']).fetchall()
            self.assertEqual({(row['p1'],row['p2']) for row in games},{(sa,sb),(sb,sa)})
        first=self.store.claim('test','samples')
        with self.store.connect() as db:
            first_p1=db.execute('SELECT p1 FROM matches WHERE id=?',(first['ref'],)).fetchone()[0]
        self.assertTrue(self.store.finish(first,{'winner':'p1' if first_p1==sa else 'p2','reason':'goal'}))
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT rating FROM ratings WHERE user_id=?',(a,)).fetchone()[0],1000)
        second=self.store.claim('test','samples')
        with self.store.connect() as db:
            second_p1=db.execute('SELECT p1 FROM matches WHERE id=?',(second['ref'],)).fetchone()[0]
        self.assertTrue(self.store.finish(second,{'winner':'p1' if second_p1==sa else 'p2','reason':'goal'}))
        self.assertFalse(self.store.finish(second,{'winner':'p1','reason':'goal'}))
        with self.store.connect() as db:
            series=db.execute('SELECT * FROM ranked_series WHERE id=?',(result['series_id'],)).fetchone()
            self.assertEqual((series['status'],series['a_points'],series['rating_change']),('done',2,16))
            self.assertEqual(db.execute('SELECT rating,played,wins FROM ratings WHERE user_id=?',(a,)).fetchone()[:],(1016,1,1))
            self.assertEqual(db.execute('SELECT rating,played,losses FROM ratings WHERE user_id=?',(b,)).fetchone()[:],(984,1,1))

    def test_ranked_cancel_and_split_results_draw(self):
        a,b=self.user('alice'),self.user('bob')
        sa,sb=self.valid(a),self.valid(b)
        self.store.join_ranked(a,sa)
        self.assertTrue(self.store.cancel_ranked(a))
        self.assertFalse(self.store.cancel_ranked(a))
        self.store.join_ranked(a,sa)
        series=self.store.join_ranked(b,sb)
        self.assertFalse(self.store.cancel_ranked(a))
        first=self.store.claim('test','samples')
        self.store.finish(first,{'winner':'p1','reason':'goal'})
        second=self.store.claim('test','samples')
        self.store.finish(second,{'winner':'p1','reason':'goal'})
        with self.store.connect() as db:
            row=db.execute('SELECT a_points,rating_change FROM ranked_series WHERE id=?',(series['series_id'],)).fetchone()
            self.assertEqual(tuple(row),(1,0))
            self.assertEqual([r['rating'] for r in db.execute('SELECT rating FROM ratings ORDER BY user_id')],[1000,1000])

    def test_ranked_repeat_pair_has_smaller_rating_change(self):
        a,b=self.user('alice'),self.user('bob')
        sa,sb=self.valid(a),self.valid(b)
        changes=[]
        for _ in range(2):
            self.store.join_ranked(a,sa)
            series=self.store.join_ranked(b,sb)
            for turn in range(2):
                job=self.store.claim('test','samples')
                with self.store.connect() as db:
                    p1=db.execute('SELECT p1 FROM matches WHERE id=?',(job['ref'],)).fetchone()[0]
                self.store.finish(job,{'winner':'p1' if p1==sa else 'p2','reason':'goal'})
            with self.store.connect() as db:
                changes.append(db.execute('SELECT rating_change FROM ranked_series WHERE id=?',
                                          (series['series_id'],)).fetchone()[0])
        self.assertEqual(changes[0],16)
        self.assertLess(changes[1],changes[0])

    def test_ranked_pair_runs_both_games_and_replays(self):
        a,b=self.user('alice'),self.user('bob')
        sa,sb=self.valid(a),self.valid(b)
        self.store.join_ranked(a,sa)
        series=self.store.join_ranked(b,sb)
        worker=Worker(self.store,Executor('samples'))
        self.assertTrue(worker.run_once())
        self.assertTrue(worker.run_once())
        with self.store.connect() as db:
            row=db.execute('SELECT status,a_points,rating_change FROM ranked_series WHERE id=?',
                           (series['series_id'],)).fetchone()
            self.assertEqual(row['status'],'done')
            self.assertEqual(db.execute('SELECT SUM(played) FROM ratings').fetchone()[0],2)
            games=db.execute('SELECT status,replay FROM matches WHERE id IN (?,?)',series['matches']).fetchall()
            self.assertTrue(all(game['status']=='done' and json.loads(game['replay'])['moves'] for game in games))

    def test_swiss_pairing_swaps_to_avoid_rematch_without_large_score_gap(self):
        standings=[{'user_id':str(i),'points':4-i//2} for i in range(6)]
        history={frozenset(('0','1'))}
        pairs=swiss_pairs(standings,history)
        self.assertEqual({user for pair in pairs for user in pair},{str(i) for i in range(6)})
        self.assertTrue(all(frozenset(pair) not in history for pair in pairs))
        self.assertLessEqual(sum(abs(standings[int(a)]['points']-standings[int(b)]['points']) for a,b in pairs),2)


if __name__=='__main__': unittest.main()
