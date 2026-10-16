import hashlib
import ipaddress
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_from_directory, g
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from .store import Store, Problem, ROOT, RULES_HASH, LIMITS, uid, now

PUBLIC_FILES = {'rules.js'}


def text(data, key, minimum=1, maximum=80):
    value = data.get(key)
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        raise Problem(f'Trường {key} cần từ {minimum} đến {maximum} ký tự.')
    return value.strip()


def create_app(directory=None, origin=None, registration_code=None):
    app = Flask(__name__, static_folder=None)
    app.config['MAX_CONTENT_LENGTH'] = 400_000
    store = Store(directory or os.getenv('ARENA_DATA', ROOT / 'arena-data'))
    public_origin = (origin or os.getenv('ARENA_ORIGIN', 'http://localhost:8080')).rstrip('/')
    parsed = urlsplit(public_origin)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path:
        raise RuntimeError('ARENA_ORIGIN must be an origin, e.g. https://arena.example.org')
    local = parsed.hostname in ('localhost', '127.0.0.1')
    if not local and parsed.scheme != 'https':
        raise RuntimeError('Public deployment requires HTTPS through Cloudflare/reverse proxy.')
    code = registration_code if registration_code is not None else os.getenv('ARENA_REGISTRATION_CODE', '')
    if not local and not code:
        raise RuntimeError('Set ARENA_REGISTRATION_CODE before opening public registrations.')
    app.extensions['store'] = store

    @app.before_request
    def before():
        g.user, g.session = None, None
        g.client_ip = request.remote_addr or 'unknown'
        if os.getenv('ARENA_TRUST_CLOUDFLARE') == '1' and g.client_ip in ('127.0.0.1','::1'):
            try:
                g.client_ip = str(ipaddress.ip_address(request.headers.get('CF-Connecting-IP', '')))
            except ValueError:
                pass
        if not request.path.startswith('/api/'):
            return
        if request.method not in ('GET', 'HEAD'):
            if request.headers.get('Origin') not in (None, public_origin):
                raise Problem('Origin không được phép.', 403)
            if not request.is_json:
                raise Problem('API yêu cầu application/json.', 415)
        token = request.cookies.get('ott_session', '')
        if token:
            with store.connect() as db:
                row = db.execute('SELECT s.*,u.username,u.name,u.role FROM sessions s JOIN users u ON u.id=s.user_id '
                                 'WHERE s.hash=? AND s.expires>?',
                                 (hashlib.sha256(token.encode()).hexdigest(), now())).fetchone()
                if row:
                    g.user = {'id': row['user_id'], 'username': row['username'], 'name': row['name'], 'role': row['role']}
                    g.session = dict(row)
        if g.user and request.method not in ('GET', 'HEAD'):
            if not secrets.compare_digest(request.headers.get('X-CSRF-Token', ''), g.session['csrf']):
                raise Problem('Phiên thao tác không hợp lệ. Hãy tải lại trang.', 403)

    @app.after_request
    def after(response):
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        if request.path == '/' or request.path.startswith(('/api/', '/arena')):
            response.headers['Cache-Control'] = 'no-store' if request.path.startswith('/api/') else 'no-cache'
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
        return response

    @app.errorhandler(Problem)
    def problem(error):
        return jsonify(error=str(error)), error.status

    @app.errorhandler(HTTPException)
    def http_error(error):
        return jsonify(error='Yêu cầu không hợp lệ.' if error.code != 404 else 'Không tìm thấy.'), error.code

    @app.errorhandler(sqlite3.IntegrityError)
    def conflict(_error):
        return jsonify(error='Dữ liệu đã tồn tại hoặc vừa thay đổi. Hãy tải lại.'), 409

    def body():
        value = request.get_json()
        if not isinstance(value, dict):
            raise Problem('JSON cần là một object.')
        return value

    def auth(admin=False):
        if not g.user:
            raise Problem('Bạn cần đăng nhập.', 401)
        if admin and g.user['role'] != 'admin':
            raise Problem('Chỉ ban tổ chức được thực hiện.', 403)
        return g.user['id']

    @app.get('/api/health')
    def health():
        with store.connect() as db:
            active = db.execute('SELECT mode FROM workers WHERE seen>?', (now()-40,)).fetchall()
            pending = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        return jsonify(ok=True, rules_hash=RULES_HASH, limits=LIMITS, runner_online=bool(active),
                       runner_modes=sorted({r['mode'] for r in active}), pending=pending,
                       registration_invite=bool(code), server_time=now())

    @app.get('/api/me')
    def me():
        return jsonify(user=g.user, csrf=g.session['csrf'] if g.session else None)

    def session_response(user):
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        with store.connect(True) as db:
            db.execute('DELETE FROM sessions WHERE expires<?', (now(),))
            if g.session:
                db.execute('DELETE FROM sessions WHERE hash=?', (g.session['hash'],))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(), user['id'], csrf, now()+86400))
        response = jsonify(user={k: user[k] for k in ('id','username','name','role')}, csrf=csrf)
        response.set_cookie('ott_session', token, max_age=86400, httponly=True,
                            secure=parsed.scheme == 'https', samesite='Lax', path='/')
        return response

    @app.post('/api/register')
    def register():
        store.rate('register:' + g.client_ip, 20, 3600)
        data = body()
        username, password = text(data, 'username', 3, 32).lower(), text(data, 'password', 12, 128)
        if not re.fullmatch(r'[a-z0-9_]{3,32}', username):
            raise Problem('Tên đăng nhập chỉ gồm chữ thường, số và dấu gạch dưới.')
        if code and not secrets.compare_digest(str(data.get('invite', '')), code):
            raise Problem('Mã tham gia không đúng.', 403)
        user = {'id': uid(), 'username': username, 'name': text(data, 'name', 2, 40), 'role': 'player'}
        password_hash = generate_password_hash(password, method='scrypt')
        with store.connect(True) as db:
            db.execute('INSERT INTO users VALUES(?,?,?,?,?,?)',
                       (user['id'], username, user['name'], password_hash, 'player', now()))
        return session_response(user)

    @app.post('/api/login')
    def login():
        data = body()
        username = text(data, 'username', 3, 32).lower()
        store.rate('login:' + username, 15, 900)
        store.rate('login-ip:' + g.client_ip, 100, 900)
        password = text(data, 'password', 1, 128)
        with store.connect() as db:
            user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if not user or not check_password_hash(user['password'], password):
            raise Problem('Tên đăng nhập hoặc mật khẩu không đúng.', 401)
        return session_response(dict(user))

    @app.post('/api/logout')
    def logout():
        auth()
        with store.connect(True) as db:
            db.execute('DELETE FROM sessions WHERE hash=?', (g.session['hash'],))
        response = jsonify(ok=True)
        response.delete_cookie('ott_session', path='/')
        return response

    @app.get('/api/submissions')
    def submissions():
        user = auth()
        with store.connect() as db:
            rows = db.execute('SELECT id,name,hash,status,log,created FROM submissions WHERE user_id=? '
                              'ORDER BY created DESC LIMIT 100', (user,)).fetchall()
        return jsonify(items=[dict(r) for r in rows])

    @app.get('/api/submissions/<sid>/source')
    def source(sid):
        user = auth()
        with store.connect() as db:
            row = db.execute('SELECT source FROM submissions WHERE id=? AND user_id=?', (sid, user)).fetchone()
        if not row:
            raise Problem('Không tìm thấy bài nộp của bạn.', 404)
        return jsonify(source=row['source'])

    @app.post('/api/submissions')
    def submit():
        user, data = auth(), body()
        store.rate('submit:' + user, 10)
        sid = store.submit(user, text(data, 'name', 2, 50), data.get('source'))
        return jsonify(id=sid), 201

    @app.get('/api/tournaments')
    def tournaments():
        with store.connect() as db:
            rows = db.execute('SELECT t.*,(SELECT COUNT(*) FROM entries e WHERE e.tournament_id=t.id) entrants '
                              'FROM tournaments t ORDER BY created DESC LIMIT 100').fetchall()
        return jsonify(items=[dict(r) for r in rows])

    @app.get('/api/tournaments/<tid>')
    def tournament(tid):
        with store.connect() as db:
            row = db.execute('SELECT * FROM tournaments WHERE id=?', (tid,)).fetchone()
            if not row:
                raise Problem('Không tìm thấy giải.', 404)
            entries = store.standings(db, tid)
        return jsonify(tournament=dict(row), standings=entries)

    @app.post('/api/tournaments/<tid>/join')
    def join(tid):
        store.join(auth(), tid, text(body(), 'submission_id', 24, 24))
        return jsonify(ok=True)

    @app.post('/api/practice')
    def practice():
        user, data = auth(), body()
        store.rate('practice:' + user, 10)
        a, b = text(data, 'p1', 24, 24), text(data, 'p2', 24, 24)
        with store.connect(True) as db:
            for sid in (a, b):
                if not db.execute("SELECT 1 FROM submissions WHERE id=? AND user_id=? AND status='valid'", (sid, user)).fetchone():
                    raise Problem('Chọn hai bot đã vượt kiểm tra của bạn.')
            if db.execute("SELECT 1 FROM matches WHERE requester=? AND status IN ('queued','running')", (user,)).fetchone():
                raise Problem('Bạn còn trận tập đang chờ/chạy.', 409)
            ids = [store.create_match(db, a, b, user), store.create_match(db, b, a, user)]
        return jsonify(ids=ids), 201

    @app.get('/api/ranked')
    def ranked():
        return jsonify(store.ranked_snapshot(g.user['id'] if g.user else None))

    @app.post('/api/ranked/queue')
    def ranked_queue():
        user = auth()
        store.rate('ranked:' + user, 20)
        result = store.join_ranked(user, text(body(), 'submission_id', 24, 24))
        return jsonify(result), 201

    @app.post('/api/ranked/cancel')
    def ranked_cancel():
        return jsonify(cancelled=store.cancel_ranked(auth()))

    @app.get('/api/matches')
    def matches():
        tid = request.args.get('tournament')
        with store.connect() as db:
            rows = db.execute('SELECT m.id,m.status,m.winner,m.reason,m.created,m.pairing_id,'
                              's1.name bot1,s2.name bot2,u1.name player1,u2.name player2,p.round,p.tournament_id '
                              'FROM matches m JOIN submissions s1 ON s1.id=m.p1 JOIN submissions s2 ON s2.id=m.p2 '
                              'JOIN users u1 ON u1.id=s1.user_id JOIN users u2 ON u2.id=s2.user_id '
                              'LEFT JOIN pairings p ON p.id=m.pairing_id '
                              'WHERE (? IS NULL OR p.tournament_id=?) '
                              'ORDER BY CASE m.status WHEN \'running\' THEN 0 WHEN \'queued\' THEN 1 ELSE 2 END,m.created DESC,m.id LIMIT 100',
                              (tid, tid)).fetchall()
        return jsonify(items=[dict(r) for r in rows])

    @app.get('/api/matches/<mid>')
    def match(mid):
        with store.connect() as db:
            row = db.execute('SELECT m.*,s1.name bot1,s2.name bot2,u1.name player1,u2.name player2 '
                             'FROM matches m JOIN submissions s1 ON s1.id=m.p1 JOIN submissions s2 ON s2.id=m.p2 '
                             'JOIN users u1 ON u1.id=s1.user_id JOIN users u2 ON u2.id=s2.user_id WHERE m.id=?', (mid,)).fetchone()
        if not row:
            raise Problem('Không tìm thấy ván.', 404)
        data = dict(row)
        data['replay'] = json.loads(data['replay']) if data['replay'] else None
        data['progress'] = json.loads(data['progress']) if data['progress'] else None
        return jsonify(data)

    @app.post('/api/admin/tournaments')
    def create_tournament():
        actor, data = auth(True), body()
        name, fmt = text(data, 'name', 3, 80), data.get('format')
        rounds, deadline = data.get('rounds', 5), data.get('deadline')
        if fmt not in ('swiss', 'round_robin') or type(rounds) is not int or not 1 <= rounds <= 20:
            raise Problem('Thể thức hoặc số vòng không hợp lệ.')
        if type(deadline) not in (float, int) or not now()+60 <= deadline <= now()+86400*365:
            raise Problem('Hạn đăng ký phải sau ít nhất một phút và trong một năm tới.')
        tid = uid()
        with store.connect(True) as db:
            db.execute('INSERT INTO tournaments(id,name,format,rounds,deadline,rules_hash,config,created) VALUES(?,?,?,?,?,?,?,?)',
                       (tid, name, fmt, rounds, deadline, RULES_HASH, json.dumps(LIMITS), now()))
            store.audit(db, actor, 'tournament.created', {'id': tid})
        return jsonify(id=tid), 201

    @app.post('/api/admin/tournaments/<tid>/freeze')
    def freeze(tid):
        store.freeze(tid, auth(True))
        return jsonify(ok=True)

    @app.post('/api/admin/tournaments/<tid>/next')
    def next_round(tid):
        store.next_round(tid, auth(True))
        return jsonify(ok=True)

    @app.get('/api/admin/jobs')
    def jobs():
        auth(True)
        with store.connect() as db:
            rows = db.execute('SELECT id,kind,ref,status,attempts,error,created FROM jobs ORDER BY created DESC LIMIT 100').fetchall()
            audit = db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 30').fetchall()
        return jsonify(items=[dict(r) for r in rows], audit=[dict(r) for r in audit])

    @app.post('/api/admin/jobs/<jid>/retry')
    def retry(jid):
        actor = auth(True)
        with store.connect(True) as db:
            job = db.execute("SELECT * FROM jobs WHERE id=? AND status='failed'", (jid,)).fetchone()
            if not job:
                raise Problem('Chỉ thử lại job lỗi hạ tầng.', 409)
            db.execute("UPDATE jobs SET status='queued',attempts=0,error='',token=NULL WHERE id=?", (jid,))
            table = 'matches' if job['kind'] == 'game' else 'submissions'
            db.execute(f"UPDATE {table} SET status='queued' WHERE id=?", (job['ref'],))
            store.audit(db, actor, 'job.retried', {'id': jid})
        return jsonify(ok=True)

    @app.get('/')
    def home():
        return send_from_directory(ROOT / 'arena', 'index.html')

    @app.get('/arena')
    @app.get('/arena/')
    def arena():
        return send_from_directory(ROOT / 'arena', 'index.html')

    @app.get('/<path:filename>')
    def files(filename):
        if filename in PUBLIC_FILES:
            return send_from_directory(ROOT, filename)
        if filename in ('arena/arena.js','arena/arena.css','arena/guide.html'):
            return send_from_directory(ROOT, filename)
        if filename in ('sdk/examples/first.py','sdk/examples/hunter.py','sdk/examples/guardian.py'):
            return send_from_directory(ROOT, filename, as_attachment=True)
        raise Problem('Không tìm thấy.', 404)

    return app
