import csv
import hashlib
import io
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_from_directory, g
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from . import importer
from .store import Store, Problem, ROOT, RULES_HASH, LIMITS, TEAM_SIZE, SYSTEM_TEAM, uid, now

PUBLIC_FILES = {'rules.js', 'arena/arena.js', 'arena/admin.js', 'arena/common.js', 'arena/arena.css',
                'arena/guide.html'}
SAMPLES = {'sdk/examples/first.py', 'sdk/examples/hunter.py', 'sdk/examples/guardian.py'}
SESSION_SECONDS = int(os.getenv('ARENA_SESSION_DAYS', '7')) * 86400
IMPORT_BYTES = 60_000_000


def env_rate(name, default):
    return int(os.getenv(f'ARENA_RATE_{name}', str(default)))


# A whole campus often shares one public IP, so per-IP limits are generous and the
# per-account limits do the real work.
RATES = {
    'register_ip': (env_rate('REGISTER_IP', 3000), 3600),
    'login_ip': (env_rate('LOGIN_IP', 10000), 900),
    'login_user': (env_rate('LOGIN_USER', 20), 900),
    'submit_team': (env_rate('SUBMIT_TEAM', 20), 3600),
    'practice_team': (env_rate('PRACTICE_TEAM', 10), 3600),
    'ranked_team': (env_rate('RANKED_TEAM', 30), 3600),
    'campaign_team': (env_rate('CAMPAIGN_TEAM', 30), 3600),
    'team_user': (env_rate('TEAM_USER', 20), 3600),
}


def text(data, key, minimum=1, maximum=80):
    value = data.get(key)
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise Problem(f'Trường {key} cần từ {minimum} đến {maximum} ký tự.')
    return value.strip()


class TTLCache:
    def __init__(self):
        self.items, self.lock = {}, threading.Lock()

    def get(self, key, ttl, build):
        with self.lock:
            hit = self.items.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
        value = build()
        with self.lock:
            if len(self.items) > 2000:
                self.items.clear()
            self.items[key] = (time.monotonic() + ttl, value)
        return value

    def clear(self):
        with self.lock:
            self.items.clear()


def create_app(directory=None, origin=None, registration_code=None, bosses=True):
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 400_000
    app.json.ensure_ascii = False
    store = Store(directory or os.getenv('ARENA_DATA', ROOT / 'arena-data'))
    public_origin = (origin or os.getenv('ARENA_ORIGIN', 'http://localhost:8080')).rstrip('/')
    parsed = urlsplit(public_origin)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path:
        raise RuntimeError('ARENA_ORIGIN must be an origin, e.g. https://arena.example.org')
    local = parsed.hostname in ('localhost', '127.0.0.1')
    if not local and parsed.scheme != 'https':
        raise RuntimeError('Public deployment requires HTTPS through Cloudflare/reverse proxy.')
    code = registration_code if registration_code is not None else os.getenv('ARENA_REGISTRATION_CODE', '')
    if not local and not code and os.getenv('ARENA_OPEN_REGISTRATION') != '1':
        raise RuntimeError('Set ARENA_REGISTRATION_CODE, or ARENA_OPEN_REGISTRATION=1 to let anyone register.')
    app.extensions['store'] = store
    if bosses:
        store.ensure_bosses()
    cache = TTLCache()
    app.extensions['cache'] = cache
    credentials = {}

    def settings():
        return cache.get('settings', 2, store.settings)

    @app.before_request
    def before():
        g.user, g.session = None, None
        g.client_ip = request.remote_addr or 'unknown'
        if os.getenv('ARENA_TRUST_CLOUDFLARE') == '1' and g.client_ip in ('127.0.0.1', '::1'):
            try:
                g.client_ip = str(ipaddress.ip_address(request.headers.get('CF-Connecting-IP', '')))
            except ValueError:
                pass
        if not request.path.startswith('/api/'):
            return
        if request.method not in ('GET', 'HEAD'):
            if request.headers.get('Origin') not in (None, public_origin):
                raise Problem('Origin không được phép.', 403)
            if request.path != '/api/admin/import' and not request.is_json:
                raise Problem('API yêu cầu application/json.', 415)
        token = request.cookies.get('ott_session', '')
        if token:
            with store.connect() as db:
                row = db.execute('SELECT s.*,u.username,u.name,u.role,u.team_id,u.must_change,u.disabled '
                                 'FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.hash=? AND s.expires>?',
                                 (hashlib.sha256(token.encode()).hexdigest(), now())).fetchone()
            if row and not row['disabled']:
                g.user = {'id': row['user_id'], 'username': row['username'], 'name': row['name'],
                          'role': row['role'], 'team_id': row['team_id'], 'must_change': bool(row['must_change'])}
                g.session = dict(row)
        # Everything except the sign-in screen's own needs requires an account.
        if not g.user and request.method in ('GET', 'HEAD') and request.path not in ('/api/health', '/api/me'):
            raise Problem('Bạn cần đăng nhập để xem nội dung này.', 401)
        if g.user and request.method not in ('GET', 'HEAD'):
            if not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), g.session['csrf']):
                raise Problem('Phiên thao tác không hợp lệ. Hãy tải lại trang.', 403)
        if request.method not in ('GET', 'HEAD') and settings().get('read_only') == '1' and \
                request.path not in ('/api/login', '/api/logout') and not (g.user and g.user['role'] == 'admin'):
            raise Problem('Hệ thống đang tạm ở chế độ chỉ xem để bảo trì. Dữ liệu của bạn vẫn an toàn.', 503)

    @app.after_request
    def after(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        if request.path.startswith('/api/') and request.method not in ('GET', 'HEAD') and response.status_code < 400:
            cache.clear()
        if request.path == '/' or request.path.startswith(('/api/', '/arena', '/rules.js')):
            response.headers['Cache-Control'] = 'no-store' if request.path.startswith('/api/') else 'no-cache'
            response.headers['Content-Security-Policy'] = (
                "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
                "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; "
                "object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        return response

    @app.errorhandler(Problem)
    def problem(error):
        return jsonify(error=str(error)), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        messages = {404: 'Không tìm thấy.', 413: 'Dữ liệu gửi lên quá lớn.', 405: 'Phương thức không hỗ trợ.'}
        return jsonify(error=messages.get(error.code, 'Yêu cầu không hợp lệ.')), error.code

    @app.errorhandler(sqlite3.IntegrityError)
    def conflict(_error):
        return jsonify(error='Dữ liệu đã tồn tại hoặc vừa thay đổi. Hãy tải lại.'), 409

    @app.errorhandler(sqlite3.OperationalError)
    def busy(error):
        app.logger.warning('SQLite busy: %s', error)
        return jsonify(error='Máy chủ đang bận. Hãy thử lại sau vài giây.'), 503

    def body():
        value = request.get_json(silent=True)
        if not isinstance(value, dict):
            raise Problem('JSON cần là một object.')
        return value

    def auth(admin=False):
        if not g.user:
            raise Problem('Bạn cần đăng nhập.', 401)
        if admin and g.user['role'] != 'admin':
            raise Problem('Chỉ ban tổ chức được thực hiện.', 403)
        return g.user['id']

    def team_required():
        user = auth()
        with store.connect() as db:
            team = store.require_team(db, user)
        return user, team['id']

    def rate(name, key):
        maximum, window = RATES[name]
        store.rate(f'{name}:{key}', maximum, window)

    # ---- public ---------------------------------------------------------

    def health_data():
        with store.connect() as db:
            active = db.execute('SELECT mode FROM workers WHERE seen>?', (now()-40,)).fetchall()
            pending = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        values = settings()
        return {'ok': True, 'rules_hash': RULES_HASH, 'limits': LIMITS, 'runner_online': bool(active),
                'runner_slots': len(active), 'runner_modes': sorted({r['mode'] for r in active}),
                'pending': pending, 'registration_invite': bool(code), 'team_size': TEAM_SIZE,
                'announcement': values.get('announcement', ''),
                'open': {key: values.get(key) == '1' for key in
                         ('registration_open', 'submissions_open', 'ranked_open', 'practice_open', 'campaign_open',
                          'read_only')}}

    @app.get('/api/health')
    def health():
        return jsonify({**cache.get('health', 2, health_data), 'server_time': now()})

    @app.get('/api/me')
    def me():
        if not g.user:
            return jsonify(user=None, csrf=None, team=None)
        with store.connect() as db:
            team = store.team_of(db, g.user['id'])
            if team:
                team = {k: team[k] for k in ('id', 'name', 'code', 'owner', 'status', 'note')}
                team['members'] = store.team_members(db, team['id'])
                team['entries'] = [dict(r) for r in db.execute(
                    'SELECT e.tournament_id,e.submission_id,e.status,s.name bot,s.status bot_status FROM entries e '
                    'JOIN submissions s ON s.id=e.submission_id WHERE e.team_id=?', (team['id'],))]
        user = {k: g.user[k] for k in ('id', 'username', 'name', 'role', 'must_change')}
        return jsonify(user=user, csrf=g.session['csrf'], team=team)

    def session_response(user):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with store.connect(True) as db:
            db.execute('DELETE FROM sessions WHERE expires<?', (now(),))
            if g.session:
                db.execute('DELETE FROM sessions WHERE hash=?', (g.session['hash'],))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), user['id'], csrf, now()+SESSION_SECONDS))
        response = jsonify(user={k: user[k] for k in ('id', 'username', 'name', 'role')}, csrf=csrf)
        response.set_cookie('ott_session', token, max_age=SESSION_SECONDS, httponly=True,
                            secure=parsed.scheme == 'https', samesite='Lax', path='/')
        return response

    @app.post('/api/register')
    def register():
        data = body()
        if settings().get('registration_open') != '1':
            raise Problem('Ban tổ chức đang tạm đóng đăng ký tài khoản.', 403)
        rate('register_ip', g.client_ip)
        username, password = text(data, 'username', 3, 32).lower(), text(data, 'password', 12, 128)
        if not re.fullmatch(r'[a-z0-9_]{3,32}', username):
            raise Problem('Tên đăng nhập chỉ gồm chữ thường, số và dấu gạch dưới.')
        if password != data['password']:
            raise Problem('Mật khẩu không được có khoảng trắng ở đầu/cuối.')
        if code and not secrets.compare_digest(str(data.get('invite', '')).strip(), code):
            raise Problem('Mã tham gia không đúng.', 403)
        student_id = str(data.get('student_id') or '').strip()[:20] or None
        user = {'id': uid(), 'username': username, 'name': text(data, 'name', 2, 40), 'role': 'player'}
        with store.connect() as db:
            if db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                raise Problem('Tên đăng nhập đã có người dùng.', 409)
        password_hash = generate_password_hash(password, method='scrypt')
        with store.connect(True) as db:
            db.execute('INSERT INTO users(id,username,name,password,role,created,student_id) VALUES(?,?,?,?,?,?,?)',
                       (user['id'], username, user['name'], password_hash, 'player', now(), student_id))
            store.audit(db, user['id'], 'user.registered', {'username': username})
        return session_response(user)

    @app.post('/api/login')
    def login():
        data = body()
        username = text(data, 'username', 3, 32).lower()
        rate('login_user', username)
        rate('login_ip', g.client_ip)
        text(data, 'password', 1, 128)
        with store.connect() as db:
            user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if not user or not check_password_hash(user['password'], data['password']):
            raise Problem('Tên đăng nhập hoặc mật khẩu không đúng.', 401)
        if user['disabled']:
            raise Problem('Tài khoản đã bị khóa. Liên hệ ban tổ chức.', 403)
        return session_response(dict(user))

    @app.post('/api/logout')
    def logout():
        auth()
        with store.connect(True) as db:
            db.execute('DELETE FROM sessions WHERE hash=?', (g.session['hash'],))
        response = jsonify(ok=True)
        response.delete_cookie('ott_session', path='/')
        return response

    @app.post('/api/me/password')
    def change_password():
        user, data = auth(), body()
        rate('login_user', g.user['username'])
        new = text(data, 'password', 12, 128)
        if new != data['password']:
            raise Problem('Mật khẩu không được có khoảng trắng ở đầu/cuối.')
        with store.connect() as db:
            row = db.execute('SELECT password FROM users WHERE id=?', (user,)).fetchone()
        if not check_password_hash(row['password'], str(data.get('current', ''))):
            raise Problem('Mật khẩu hiện tại không đúng.', 403)
        digest = generate_password_hash(new, method='scrypt')
        with store.connect(True) as db:
            db.execute('UPDATE users SET password=?,must_change=0 WHERE id=?', (digest, user))
            db.execute('DELETE FROM sessions WHERE user_id=? AND hash!=?', (user, g.session['hash']))
            store.audit(db, user, 'user.password_changed', {})
        return jsonify(ok=True)

    # ---- teams ----------------------------------------------------------

    @app.post('/api/team')
    def create_team():
        user = auth()
        rate('team_user', user)
        return jsonify(id=store.create_team(user, text(body(), 'name', 2, 40))), 201

    @app.post('/api/team/join')
    def join_team():
        user = auth()
        rate('team_user', user)
        return jsonify(id=store.join_team(user, text(body(), 'code', 4, 12)))

    @app.post('/api/team/leave')
    def leave_team():
        return jsonify(left=store.leave_team(auth()))

    @app.post('/api/team/rename')
    def rename_team():
        user, team = team_required()
        rate('team_user', user)
        store.rename_team(user, team, text(body(), 'name', 2, 40))
        return jsonify(ok=True)

    @app.post('/api/team/code')
    def team_code():
        user, team = team_required()
        rate('team_user', user)
        return jsonify(code=store.new_team_code(user, team))

    @app.post('/api/team/kick')
    def kick():
        user = auth()
        store.kick(user, text(body(), 'user_id', 24, 24))
        return jsonify(ok=True)

    # ---- submissions ----------------------------------------------------

    @app.get('/api/submissions')
    def submissions():
        auth()
        if not g.user['team_id']:
            return jsonify(items=[])
        with store.connect() as db:
            rows = db.execute('SELECT s.id,s.name,s.hash,s.status,s.log,s.created,s.origin,u.name author '
                              'FROM submissions s LEFT JOIN users u ON u.id=s.author WHERE s.team_id=? '
                              'ORDER BY s.created DESC LIMIT 100', (g.user['team_id'],)).fetchall()
            items = [dict(r) for r in rows]
            positions = store.queue_positions(db, [i['id'] for i in items if i['status'] == 'queued'])
        for item in items:
            item['position'] = positions.get(item['id'])
        return jsonify(items=items)

    @app.get('/api/submissions/<sid>/source')
    def source(sid):
        auth()
        with store.connect() as db:
            row = db.execute('SELECT source FROM submissions WHERE id=? AND team_id=?',
                             (sid, g.user['team_id'])).fetchone()
        if not row:
            raise Problem('Không tìm thấy bài nộp của đội bạn.', 404)
        return jsonify(source=row['source'])

    @app.post('/api/submissions')
    def submit():
        (user, team), data = team_required(), body()
        rate('submit_team', team)
        sid = store.submit(team, user, text(data, 'name', 2, 50), data.get('source'))
        return jsonify(id=sid), 201

    @app.post('/api/practice')
    def practice():
        (user, team), data = team_required(), body()
        rate('practice_team', team)
        ids = store.practice(team, user, text(data, 'p1', 24, 24), text(data, 'p2', 24, 24))
        return jsonify(ids=ids), 201

    # ---- ranked ---------------------------------------------------------

    @app.get('/api/ranked')
    def ranked():
        data = dict(cache.get('ranked', 3, store.ranked_leaderboard))
        data['mine'] = store.ranked_mine(g.user['team_id']) if g.user and g.user['team_id'] else None
        return jsonify(data)

    @app.post('/api/ranked/queue')
    def ranked_queue():
        user, team = team_required()
        rate('ranked_team', team)
        return jsonify(store.join_ranked(team, text(body(), 'submission_id', 24, 24), user)), 201

    @app.post('/api/ranked/cancel')
    def ranked_cancel():
        user, team = team_required()
        return jsonify(cancelled=store.cancel_ranked(team, user))

    # ---- campaign, friendlies, profiles, feed -----------------------------

    @app.get('/api/campaign')
    def campaign():
        data = dict(cache.get('campaign', 5, store.campaign_hall))
        data['mine'] = store.campaign_mine(g.user['team_id']) if g.user and g.user['team_id'] else None
        return jsonify(data)

    @app.post('/api/campaign/challenge')
    def campaign_challenge():
        (user, team), data = team_required(), body()
        rate('campaign_team', team)
        return jsonify(store.challenge(team, user, data.get('level'), text(data, 'submission_id', 24, 24))), 201

    @app.post('/api/friendly')
    def friendly():
        (user, team), data = team_required(), body()
        rate('practice_team', team)
        ids = store.friendly(team, user, text(data, 'submission_id', 24, 24), text(data, 'team_id', 24, 24))
        return jsonify(ids=ids), 201

    @app.get('/api/teams/<tid>')
    def team_profile(tid):
        return jsonify(cache.get(f'team:{tid}', 5, lambda: store.team_profile(tid)))

    @app.get('/api/me/badges')
    def my_badges():
        auth()
        if not g.user['team_id']:
            return jsonify(items=[])
        team = g.user['team_id']
        return jsonify(items=cache.get(f'badges:{team}', 5, lambda: store.team_badges(team)))

    @app.get('/api/feed')
    def feed():
        return jsonify(cache.get('feed', 5, store.feed))

    # ---- tournaments ----------------------------------------------------

    def tournament_list():
        with store.connect() as db:
            rows = db.execute('SELECT t.id,t.name,t.format,t.rounds,t.round,t.deadline,t.status,t.created,'
                              't.auto_advance,t.description,t.seed_from,'
                              '(SELECT COUNT(*) FROM entries e WHERE e.tournament_id=t.id) entrants '
                              'FROM tournaments t ORDER BY t.created DESC LIMIT 100').fetchall()
        return [dict(r) for r in rows]

    @app.get('/api/tournaments')
    def tournaments():
        return jsonify(items=cache.get('tournaments', 3, tournament_list))

    def tournament_data(tid):
        with store.connect() as db:
            row = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
            if not row:
                raise Problem('Không tìm thấy giải.', 404)
            standings = store.standings(db, tid)
            unfinished = store.unfinished(db, tid)
        data = dict(row)
        data['config'] = json.loads(data['config'])
        return {'tournament': data, 'standings': standings, 'unfinished': unfinished}

    @app.get('/api/tournaments/<tid>')
    def tournament(tid):
        data = dict(cache.get(f'tournament:{tid}', 3, lambda: tournament_data(tid)))
        team = g.user['team_id'] if g.user else None
        data['mine'] = next((s for s in data['standings'] if s['team_id'] == team), None)
        return jsonify(data)

    def round_data(tid, number):
        with store.connect() as db:
            pairs = db.execute('SELECT p.*,ta.name a_name,tb.name b_name FROM pairings p JOIN teams ta ON ta.id=p.a '
                               'LEFT JOIN teams tb ON tb.id=p.b WHERE p.tournament_id=? AND p.round=? '
                               'ORDER BY p.slot', (tid, number)).fetchall()
            games = {}
            for m in db.execute('SELECT m.id,m.pairing_id,m.status,m.winner,m.reason,s.team_id p1_team FROM matches m '
                                'JOIN pairings p ON p.id=m.pairing_id JOIN submissions s ON s.id=m.p1 '
                                'WHERE p.tournament_id=? AND p.round=? ORDER BY m.created,m.id', (tid, number)):
                games.setdefault(m['pairing_id'], []).append(dict(m))
        return [{**dict(p), 'games': games.get(p['id'], [])} for p in pairs]

    @app.get('/api/tournaments/<tid>/rounds/<int:number>')
    def tournament_round(tid, number):
        return jsonify(items=cache.get(f'round:{tid}:{number}', 3, lambda: round_data(tid, number)))

    @app.post('/api/tournaments/<tid>/join')
    def join(tid):
        user, team = team_required()
        store.join(team, tid, text(body(), 'submission_id', 24, 24), user)
        return jsonify(ok=True)

    @app.post('/api/tournaments/<tid>/withdraw')
    def withdraw(tid):
        user, team = team_required()
        return jsonify(withdrawn=store.withdraw(team, tid, user))

    # ---- matches --------------------------------------------------------

    MATCH_SELECT = ('SELECT m.id,m.status,m.winner,m.reason,m.created,m.pairing_id,m.kind,m.plies,'
                    's1.name bot1,s2.name bot2,t1.name team1,t2.name team2,t1.id team1_id,t2.id team2_id,'
                    'p.round,p.tournament_id '
                    'FROM matches m JOIN submissions s1 ON s1.id=m.p1 JOIN submissions s2 ON s2.id=m.p2 '
                    'JOIN teams t1 ON t1.id=s1.team_id JOIN teams t2 ON t2.id=s2.team_id '
                    'LEFT JOIN pairings p ON p.id=m.pairing_id ')
    ORDER = ("ORDER BY CASE m.status WHEN 'running' THEN 0 WHEN 'queued' THEN 1 ELSE 2 END,"
             "m.created DESC,m.id LIMIT 100")

    def match_list(status, tid):
        where, args = [], []
        if status in ('queued', 'running', 'done', 'failed', 'cancelled'):
            where.append('m.status=?')
            args.append(status)
        if tid:
            where.append('p.tournament_id=?')
            args.append(tid)
        with store.connect() as db:
            rows = db.execute(MATCH_SELECT + ('WHERE ' + ' AND '.join(where) + ' ' if where else '') + ORDER,
                              args).fetchall()
        return [dict(r) for r in rows]

    @app.get('/api/matches')
    def matches():
        status, tid = request.args.get('status'), request.args.get('tournament')
        if request.args.get('mine') == '1':
            auth()
            with store.connect() as db:
                rows = db.execute(MATCH_SELECT + 'WHERE m.p1 IN (SELECT id FROM submissions WHERE team_id=?) '
                                  'OR m.p2 IN (SELECT id FROM submissions WHERE team_id=?) ' + ORDER,
                                  (g.user['team_id'], g.user['team_id'])).fetchall()
            return jsonify(items=[dict(r) for r in rows])
        return jsonify(items=cache.get(f'matches:{status}:{tid}', 3, lambda: match_list(status, tid)))

    def match_data(mid):
        with store.connect() as db:
            row = db.execute('SELECT m.*,s1.name bot1,s2.name bot2,t1.name team1,t2.name team2,'
                             't1.id team1_id,t2.id team2_id '
                             'FROM matches m JOIN submissions s1 ON s1.id=m.p1 JOIN submissions s2 ON s2.id=m.p2 '
                             'JOIN teams t1 ON t1.id=s1.team_id JOIN teams t2 ON t2.id=s2.team_id WHERE m.id=?',
                             (mid,)).fetchone()
            if not row:
                raise Problem('Không tìm thấy ván.', 404)
            position = store.queue_positions(db, [mid]).get(mid)
        data = dict(row)
        data['replay'] = json.loads(data['replay']) if data['replay'] else None
        data['progress'] = json.loads(data['progress']) if data['progress'] else None
        data['position'] = position
        for key in ('p1', 'p2', 'requester'):
            data.pop(key, None)
        return data

    @app.get('/api/matches/<mid>')
    def match(mid):
        return jsonify(cache.get(f'match:{mid}', 2, lambda: match_data(mid)))

    # ---- admin ----------------------------------------------------------

    @app.get('/api/admin/overview')
    def overview():
        auth(True)
        return jsonify(store.overview())

    @app.get('/api/admin/settings')
    def get_settings():
        auth(True)
        return jsonify(store.settings())

    @app.post('/api/admin/settings')
    def put_settings():
        store.set_settings(auth(True), body())
        return jsonify(store.settings())

    @app.post('/api/admin/tournaments')
    def create_tournament():
        actor, data = auth(True), body()
        tid = store.create_tournament(
            actor, text(data, 'name', 3, 80), data.get('format'), data.get('rounds', 5), data.get('deadline'),
            bool(data.get('auto_advance')), str(data.get('description') or '')[:500],
            data.get('seed_from') or None, data.get('seed_count'))
        return jsonify(id=tid), 201

    @app.post('/api/admin/tournaments/<tid>/freeze')
    def freeze(tid):
        return jsonify(store.freeze(tid, auth(True), bool(body().get('force'))))

    @app.post('/api/admin/tournaments/<tid>/next')
    def next_round(tid):
        return jsonify(store.next_round(tid, auth(True)))

    @app.post('/api/admin/tournaments/<tid>/auto')
    def auto_advance(tid):
        store.set_auto_advance(tid, auth(True), bool(body().get('value')))
        return jsonify(ok=True)

    @app.post('/api/admin/tournaments/<tid>/entries/<team>/disqualify')
    def disqualify(tid, team):
        store.disqualify(tid, team, auth(True), str(body().get('reason') or ''))
        return jsonify(ok=True)

    @app.get('/api/admin/tournaments/<tid>/export')
    def export(tid):
        auth(True)
        with store.connect() as db:
            t = db.execute('SELECT name FROM tournaments WHERE id=?', (tid,)).fetchone()
            if not t:
                raise Problem('Không tìm thấy giải.', 404)
            rows = store.standings(db, tid)
            members = {}
            for m in db.execute('SELECT team_id,name,student_id FROM users WHERE team_id IN '
                                '(SELECT team_id FROM entries WHERE tournament_id=?)', (tid,)):
                members.setdefault(m['team_id'], []).append(m['name'] + (f" ({m['student_id']})" if m['student_id'] else ''))
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['Hạng', 'Xếp loại', 'Đội', 'Thành viên', 'Bot', 'Điểm cặp', 'Buchholz', 'Điểm ván',
                         'Thắng', 'Hòa', 'Thua', 'BYE', 'Hạt giống', 'Trạng thái'])
        for s in rows:
            writer.writerow([s['rank'] or '', s['tier'], s['name'], '; '.join(members.get(s['team_id'], [])), s['bot'],
                             s['points'], s['buchholz'], s['game_points'], s['wins'], s['draws'], s['losses'],
                             s['byes'], s['seed'] or '', s['entry_status']])
        filename = re.sub(r'[^A-Za-z0-9_-]+', '-', t['name']).strip('-') or 'ket-qua'
        return Response('﻿' + out.getvalue(), mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename="{filename}.csv"'})

    def page():
        try:
            return max(0, int(request.args.get('page', 0)))
        except ValueError:
            return 0

    @app.get('/api/admin/teams')
    def admin_teams():
        auth(True)
        q = f"%{request.args.get('q', '').strip()}%"
        with store.connect() as db:
            rows = db.execute(
                'SELECT t.*,(SELECT COUNT(*) FROM users u WHERE u.team_id=t.id) members,'
                '(SELECT COUNT(*) FROM submissions s WHERE s.team_id=t.id) bots,'
                "(SELECT COUNT(*) FROM submissions s WHERE s.team_id=t.id AND s.status='valid') valid_bots,"
                'r.rating FROM teams t LEFT JOIN ratings r ON r.team_id=t.id '
                'WHERE t.name LIKE ? OR t.code LIKE ? OR t.id IN (SELECT team_id FROM users WHERE username LIKE ? '
                'OR name LIKE ? OR student_id LIKE ?) ORDER BY t.created DESC LIMIT 50 OFFSET ?',
                (q, q, q, q, q, page()*50)).fetchall()
            items = [dict(r) for r in rows]
            for item in items:
                item['member_list'] = store.team_members(db, item['id'])
        return jsonify(items=items)

    @app.post('/api/admin/teams/<team>')
    def admin_team(team):
        actor, data = auth(True), body()
        action = data.get('action')
        if team == SYSTEM_TEAM:
            raise Problem('Đội boss do hệ thống quản lý.', 409)
        if action == 'rename':
            store.rename_team(actor, team, text(data, 'name', 2, 40), admin=True)
        elif action in ('disqualify', 'reinstate'):
            store.set_team_status(team, actor, 'disqualified' if action == 'disqualify' else 'active',
                                  str(data.get('note') or ''))
        elif action == 'new_code':
            return jsonify(code=store.new_team_code(actor, team, admin=True))
        else:
            raise Problem('Thao tác không hợp lệ.')
        return jsonify(ok=True)

    @app.get('/api/admin/users')
    def admin_users():
        auth(True)
        q = f"%{request.args.get('q', '').strip()}%"
        with store.connect() as db:
            rows = db.execute('SELECT u.id,u.username,u.name,u.role,u.created,u.student_id,u.disabled,u.must_change,'
                              't.name team FROM users u LEFT JOIN teams t ON t.id=u.team_id WHERE u.username LIKE ? '
                              'OR u.name LIKE ? OR u.student_id LIKE ? OR t.name LIKE ? '
                              'ORDER BY u.created DESC LIMIT 50 OFFSET ?', (q, q, q, q, page()*50)).fetchall()
        return jsonify(items=[dict(r) for r in rows])

    @app.post('/api/admin/users/<user>')
    def admin_user(user):
        actor, data = auth(True), body()
        action = data.get('action')
        if action == 'reset_password':
            secret = importer.password()
            digest = generate_password_hash(secret, method='scrypt')
        with store.connect(True) as db:
            target = db.execute('SELECT * FROM users WHERE id=?', (user,)).fetchone()
            if not target:
                raise Problem('Không tìm thấy tài khoản.', 404)
            if user == actor and action in ('disable', 'remove_admin'):
                raise Problem('Không tự khóa hoặc tự bỏ quyền của chính mình.', 409)
            result = {'ok': True}
            if action == 'reset_password':
                db.execute('UPDATE users SET password=?,must_change=1 WHERE id=?', (digest, user))
                db.execute('DELETE FROM sessions WHERE user_id=?', (user,))
                result['password'] = secret
            elif action in ('disable', 'enable'):
                db.execute('UPDATE users SET disabled=? WHERE id=?', (int(action == 'disable'), user))
                if action == 'disable':
                    db.execute('DELETE FROM sessions WHERE user_id=?', (user,))
            elif action in ('make_admin', 'remove_admin'):
                db.execute('UPDATE users SET role=? WHERE id=?', ('admin' if action == 'make_admin' else 'player', user))
            elif action != 'remove_from_team':
                raise Problem('Thao tác không hợp lệ.')
            store.audit(db, actor, 'admin.user', {'user': user, 'action': action})
        if action == 'remove_from_team':
            store.leave_team(user, actor)
        return jsonify(result)

    @app.get('/api/admin/matches')
    def admin_matches():
        auth(True)
        where, args = [], []
        for column, key in (('m.status', 'status'), ('m.kind', 'kind')):
            if request.args.get(key):
                where.append(f'{column}=?')
                args.append(request.args[key])
        q = request.args.get('q', '').strip()
        if q:
            where.append('(t1.name LIKE ? OR t2.name LIKE ? OR m.id LIKE ?)')
            args += [f'%{q}%'] * 3
        with store.connect() as db:
            rows = db.execute(
                'SELECT m.id,m.status,m.kind,m.winner,m.reason,m.plies,m.created,m.pairing_id,s1.name bot1,s2.name bot2,'
                't1.name team1,t2.name team2,j.attempts,j.error FROM matches m '
                'JOIN submissions s1 ON s1.id=m.p1 JOIN submissions s2 ON s2.id=m.p2 '
                'JOIN teams t1 ON t1.id=s1.team_id JOIN teams t2 ON t2.id=s2.team_id '
                "LEFT JOIN jobs j ON j.kind='game' AND j.ref=m.id "
                + ('WHERE ' + ' AND '.join(where) + ' ' if where else '')
                + 'ORDER BY m.created DESC LIMIT 50 OFFSET ?', (*args, page()*50)).fetchall()
        return jsonify(items=[dict(r) for r in rows])

    @app.post('/api/admin/matches/<mid>')
    def admin_match(mid):
        actor = auth(True)
        if body().get('action') != 'cancel':
            raise Problem('Thao tác không hợp lệ.')
        return jsonify(store.admin_cancel_match(mid, actor))

    @app.post('/api/admin/matches-cancel-queued')
    def admin_cancel_queued():
        actor, data = auth(True), body()
        return jsonify(cancelled=store.admin_cancel_queued(actor, str(data.get('kind') or '')))

    @app.get('/api/admin/submissions')
    def admin_submissions():
        auth(True)
        q, status = f"%{request.args.get('q', '').strip()}%", request.args.get('status') or None
        with store.connect() as db:
            rows = db.execute('SELECT s.id,s.name,s.status,s.log,s.created,s.origin,s.hash,t.name team '
                              'FROM submissions s JOIN teams t ON t.id=s.team_id '
                              'WHERE (? IS NULL OR s.status=?) AND (t.name LIKE ? OR s.name LIKE ? OR s.hash LIKE ?) '
                              'ORDER BY s.created DESC LIMIT 50 OFFSET ?',
                              (status, status, q, q, q, page()*50)).fetchall()
            duplicates = {r['hash']: r['n'] for r in db.execute(
                'SELECT hash,COUNT(DISTINCT team_id) n FROM submissions WHERE team_id!=? GROUP BY hash HAVING n>1',
                (SYSTEM_TEAM,))}
        items = [{**dict(r), 'shared_by': duplicates.get(r['hash'], 1)} for r in rows]
        return jsonify(items=items)

    @app.get('/api/admin/submissions/<sid>/source')
    def admin_source(sid):
        actor = auth(True)
        with store.connect(True) as db:
            row = db.execute('SELECT source FROM submissions WHERE id=?', (sid,)).fetchone()
            if not row:
                raise Problem('Không tìm thấy bài nộp.', 404)
            store.audit(db, actor, 'admin.source_viewed', {'submission': sid})
        return jsonify(source=row['source'])

    @app.get('/api/admin/jobs')
    def jobs():
        auth(True)
        status = request.args.get('status') or None
        with store.connect() as db:
            rows = db.execute('SELECT id,kind,ref,status,attempts,error,created,priority,started,finished,worker '
                              'FROM jobs WHERE (? IS NULL OR status=?) ORDER BY '
                              "CASE status WHEN 'failed' THEN 0 WHEN 'running' THEN 1 WHEN 'queued' THEN 2 ELSE 3 END,"
                              'priority,created DESC LIMIT 100', (status, status)).fetchall()
        return jsonify(items=[dict(r) for r in rows])

    @app.post('/api/admin/jobs/<jid>/retry')
    def retry(jid):
        store.retry(jid, auth(True))
        return jsonify(ok=True)

    @app.post('/api/admin/jobs/retry-failed')
    def retry_failed():
        return jsonify(count=store.retry_failed(auth(True)))

    @app.get('/api/admin/audit')
    def audit():
        auth(True)
        with store.connect() as db:
            rows = db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 200').fetchall()
        return jsonify(items=[dict(r) for r in rows])

    # ---- Google Form import --------------------------------------------

    def import_inputs():
        request.max_content_length = IMPORT_BYTES
        upload, bundle = request.files.get('csv'), request.files.get('files')
        csv_data = upload.read() if upload else None
        files = None
        if bundle and bundle.filename:
            files, bundled_csv = importer.Files.from_zip(bundle.read())
            csv_data = csv_data or bundled_csv
        if not csv_data:
            raise Problem('Chọn file CSV tải từ Google Forms (hoặc ZIP có kèm CSV).')
        overrides = {role: request.form.get(f'col_{role}', '').strip()
                     for role in ('team', 'team_code', 'source', 'file', 'bot_name', 'members', 'email')}
        options = {'create_teams': request.form.get('create_teams') == '1',
                   'create_accounts': request.form.get('create_accounts') == '1',
                   'tournament': request.form.get('tournament') or None}
        return csv_data, files, overrides, options

    @app.post('/api/admin/import')
    def import_form():
        actor = auth(True)
        csv_data, files, overrides, options = import_inputs()
        planned = importer.plan(store, csv_data, files, overrides, options['create_teams'])
        if request.form.get('apply') != '1':
            return jsonify(mapping=planned['mapping'], summary=planned['summary'], rows=importer.public_rows(planned))
        iid = uid()
        with store.connect(True) as db:
            db.execute("INSERT INTO imports(id,actor,status,summary,created) VALUES(?,?,'running',?,?)",
                       (iid, actor, json.dumps(planned['summary']), now()))

        def work():
            try:
                found = importer.apply(store, planned, actor, options['tournament'], options['create_accounts'])
                if found:
                    credentials[iid] = (importer.credentials_csv(found), time.monotonic() + 3600)
                summary = {**planned['summary'], 'imported': sum(r['status'] == 'imported' for r in planned['rows']),
                           'accounts': len(found)}
                status = 'done'
            except Exception as exc:  # noqa: BLE001 - report every failure to the organiser
                summary, status = {**planned['summary'], 'error': str(exc)[:500]}, 'failed'
            with store.connect(True) as db:
                db.execute('UPDATE imports SET status=?,summary=?,report=?,finished=? WHERE id=?',
                           (status, json.dumps(summary, ensure_ascii=False),
                            json.dumps(importer.public_rows(planned), ensure_ascii=False), now(), iid))
            cache.clear()
        threading.Thread(target=work, daemon=True).start()
        return jsonify(id=iid, summary=planned['summary']), 202

    @app.get('/api/admin/imports')
    def imports():
        auth(True)
        with store.connect() as db:
            rows = db.execute('SELECT id,actor,status,summary,created,finished FROM imports '
                              'ORDER BY created DESC LIMIT 20').fetchall()
        return jsonify(items=[{**dict(r), 'summary': json.loads(r['summary']),
                               'credentials': r['id'] in credentials} for r in rows])

    @app.get('/api/admin/imports/<iid>')
    def import_status(iid):
        auth(True)
        with store.connect() as db:
            row = db.execute('SELECT * FROM imports WHERE id=?', (iid,)).fetchone()
        if not row:
            raise Problem('Không tìm thấy lượt nhập.', 404)
        return jsonify({**dict(row), 'summary': json.loads(row['summary']), 'report': json.loads(row['report']),
                        'credentials': iid in credentials})

    @app.get('/api/admin/imports/<iid>/credentials')
    def import_credentials(iid):
        actor = auth(True)
        found = credentials.pop(iid, None)
        if not found or found[1] < time.monotonic():
            raise Problem('Thông tin đăng nhập chỉ tải được một lần trong vòng 1 giờ sau khi nhập.', 410)
        with store.connect(True) as db:
            store.audit(db, actor, 'import.credentials_downloaded', {'import': iid})
        return Response(found[0], mimetype='text/csv',
                        headers={'Content-Disposition': f'attachment; filename="tai-khoan-doi-{iid[:6]}.csv"'})

    # ---- pages ----------------------------------------------------------

    @app.get('/')
    def home():
        return send_from_directory(ROOT / 'arena', 'index.html')

    @app.get('/arena')
    @app.get('/arena/')
    def arena():
        return send_from_directory(ROOT / 'arena', 'index.html')

    @app.get('/arena/admin')
    @app.get('/arena/admin.html')
    def admin_page():
        return send_from_directory(ROOT / 'arena', 'admin.html')

    @app.get('/<path:filename>')
    def files(filename):
        if filename in PUBLIC_FILES:
            return send_from_directory(ROOT, filename)
        if filename in SAMPLES:
            return send_from_directory(ROOT, filename, as_attachment=True)
        raise Problem('Không tìm thấy.', 404)

    return app
