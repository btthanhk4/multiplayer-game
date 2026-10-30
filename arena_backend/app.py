import concurrent.futures
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
import unicodedata
from urllib.parse import urlsplit

from flask import Flask, Response, jsonify, request, send_from_directory, g
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash

from .mailer import mailer_from_env
from .store import Store, Problem, ROOT, RULES_HASH, LIMITS, TEAM_SIZE, SYSTEM_TEAM, MAPS, GEOMETRY_MAPS, uid, now

PUBLIC_FILES = {'rules.js', 'arena/arena.js', 'arena/admin.js', 'arena/common.js', 'arena/watch.js', 'arena/arena.css',
                'arena/theme.js', 'arena/explain.js', 'arena/builder.js', 'arena/guide.html'}
# Every script and stylesheet is requested as <name>?v=<hash of all of them>. A browser or CDN that still holds
# an older copy can then never pair it with a newer page: a new deploy changes every URL.
VERSIONED_FILES = ('arena/arena.js', 'arena/admin.js', 'arena/common.js', 'arena/watch.js', 'arena/arena.css', 'arena/theme.js', 'arena/explain.js', 'arena/builder.js', 'rules.js')
ASSET_VERSION = hashlib.sha256(b''.join((ROOT / name).read_bytes() for name in VERSIONED_FILES)).hexdigest()[:10]
JS_IMPORT = re.compile(r"(from\s+')(\.{1,2}/[\w./-]+\.js)(')")
HTML_ASSET = re.compile(r'((?:src|href)=")(\./[\w.-]+\.(?:js|css))(")')
NO_SCRIPT_PAGE = (
    '<!doctype html><html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
    '<title>Cần bật JavaScript</title><link rel="stylesheet" href="/arena/arena.css"></head><body><main class="plain-page">'
    '<p class="eyebrow">TRANG CHƯA TẢI ĐƯỢC</p><h1>Trang chưa chạy được JavaScript.</h1>'
    '<p>Thông tin bạn vừa nhập <b>chưa được gửi đi</b>. Nguyên nhân thường gặp: tiện ích trình duyệt chặn script, hoặc bản trang cũ còn lưu trong trình duyệt.</p>'
    '<p>Hãy mở lại trang, nhấn <b>Ctrl + F5</b> (hoặc Cmd + Shift + R), kiểm tra JavaScript đang bật, rồi nhập lại.</p>'
    '<p><a class="button primary" href="/">Quay lại trang chính</a></p></main></body></html>')

SAMPLES = {'sdk/examples/first.py', 'sdk/examples/hunter.py', 'sdk/examples/guardian.py',
           'sdk/examples/pathfinder.py', 'sdk/examples/custom.py', 'sdk/examples/template.py', 'sdk/local_arena.py'}
SESSION_SECONDS = int(os.getenv('ARENA_SESSION_DAYS', '7')) * 86400


def env_rate(name, default):
    return int(os.getenv(f'ARENA_RATE_{name}', str(default)))


# A whole campus often shares one public IP, so per-IP limits are generous and the
# per-account limits do the real work.
RATES = {
    'register_ip': (env_rate('REGISTER_IP', 3000), 3600),
    'login_ip': (env_rate('LOGIN_IP', 10000), 900),
    # Guessing one account's password from one address stops after 20 tries. The looser per-account cap still
    # bounds guessing spread over many addresses, while a prankster on another network can no longer lock a
    # student out with 20 wrong tries of their username.
    'login_user': (env_rate('LOGIN_USER', 20), 900),
    'login_account': (env_rate('LOGIN_ACCOUNT', 100), 900),
    'submit_team': (env_rate('SUBMIT_TEAM', 20), 3600),
    'practice_team': (env_rate('PRACTICE_TEAM', 10), 3600),
    'ranked_team': (env_rate('RANKED_TEAM', 30), 3600),
    'campaign_team': (env_rate('CAMPAIGN_TEAM', 30), 3600),
    'team_user': (env_rate('TEAM_USER', 20), 3600),
    'tournament_code': (env_rate('TOURNAMENT_CODE', 20), 900),
    # Verification emails: per address, per visitor, and for the whole server per day (stay under the SMTP
    # provider's daily quota, e.g. 2,000 for Google Workspace, 500 for a personal Gmail).
    'email_address': (env_rate('EMAIL_ADDRESS', 5), 3600),
    'email_ip': (env_rate('EMAIL_IP', 300), 3600),
    'email_day': (env_rate('EMAIL_DAY', 1800), 86400),
    'reset_login': (env_rate('RESET_LOGIN', 10), 3600),
}
CODE_SECONDS, RESEND_SECONDS, CODE_ATTEMPTS = 600, 60, 5
EMAIL = re.compile(r"[a-z0-9_%+'-]+(\.[a-z0-9_%+'-]+)*@[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


def mask_email(email):
    name, domain = email.split('@', 1)
    return f'{name[:2]}{"•" * max(3, len(name) - 2)}@{domain}'


def code_digest(cid, code):
    return hashlib.sha256(f'{cid}:{code}'.encode()).hexdigest()


def sheet_cell(value):
    """Names are typed by players: a team called '=HYPERLINK(...)' must open in Excel as text, not as a formula."""
    value = str(value)
    return "'" + value if value[:1] in ('=', '+', '-', '@', '\t', '\r') else value


def trusted_proxies():
    """Addresses allowed to tell us the visitor's real IP in CF-Connecting-IP (the host running cloudflared)."""
    networks = [ipaddress.ip_network(part.strip(), strict=False)
                for part in os.getenv('ARENA_TRUSTED_PROXIES', '').split(',') if part.strip()]
    if os.getenv('ARENA_TRUST_CLOUDFLARE') == '1':
        networks += [ipaddress.ip_network('127.0.0.1/32'), ipaddress.ip_network('::1/128')]
    return networks


def temp_password():
    """12 characters without look-alikes (0/o, 1/l/i), for admin-issued temporary passwords."""
    return ''.join(secrets.choice('abcdefghjkmnpqrstuvwxyz23456789') for _ in range(12))


def text(data, key, minimum=1, maximum=80):
    value = data.get(key)
    if not isinstance(value, str) or not minimum <= len(value.strip()) <= maximum:
        label = FIELD_LABELS.get(key, key)
        raise Problem(f'{label} cần từ {minimum} đến {maximum} ký tự.' if minimum != maximum
                      else f'{label} không hợp lệ.')
    return value.strip()


FIELD_LABELS = {'username': 'Tên đăng nhập', 'password': 'Mật khẩu', 'name': 'Họ và tên', 'invite': 'Mã tham gia',
                'current': 'Mật khẩu hiện tại', 'login': 'Tên đăng nhập hoặc email'}
RESERVED_NAMES = {'admin', 'administrator', 'root', 'system', 'ottv2', 'btc', 'support', 'boss', 'moderator'}
INVISIBLE = dict.fromkeys(map(ord, '\u200b\u200c\u200d\u2060\ufeff\u00ad'), None)
# Hashed once so unknown usernames cost the same time as wrong passwords.
DUMMY_HASH = generate_password_hash('not-a-real-password', method='scrypt')


def new_password(data, username):
    password = text(data, 'password', 12, 128)
    if password != data['password']:
        raise Problem('Mật khẩu không được có khoảng trắng ở đầu hoặc cuối.')
    if len(set(password)) < 4 or username in password.lower():
        raise Problem('Mật khẩu quá dễ đoán: cần ít nhất 4 ký tự khác nhau và không chứa tên đăng nhập.')
    return password


def clean_name(value, label='Họ và tên'):
    if not isinstance(value, str):
        raise Problem(f'{label} không hợp lệ.')
    value = unicodedata.normalize('NFC', value).translate(INVISIBLE)
    value = ' '.join(''.join(c for c in value if c.isprintable()).split())
    if not 2 <= len(value) <= 40:
        raise Problem(f'{label} cần từ 2 đến 40 ký tự.')
    return value


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


def create_app(directory=None, origin=None, registration_code=None, bosses=True, mailer=None):
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
    proxies = trusted_proxies()
    mail = mailer if mailer is not None else mailer_from_env(local)
    email_domains = [d.strip().lower().lstrip('@') for d in os.getenv('ARENA_EMAIL_DOMAINS', '').split(',') if d.strip()]

    def from_proxy(address):
        try:
            return any(ipaddress.ip_address(address) in network for network in proxies)
        except ValueError:
            return False

    def settings():
        return cache.get('settings', 2, store.settings)

    @app.before_request
    def before():
        g.user, g.session = None, None
        g.client_ip = request.remote_addr or 'unknown'
        if proxies and from_proxy(g.client_ip):
            try:
                g.client_ip = str(ipaddress.ip_address(request.headers.get('CF-Connecting-IP', '')))
            except ValueError:
                pass
        if not request.path.startswith('/api/'):
            return
        if request.method not in ('GET', 'HEAD'):
            sent = request.headers.get('Origin')
            if sent not in (None, 'null', public_origin) and urlsplit(sent).netloc != request.host:
                raise Problem('Địa chỉ truy cập không khớp với máy chủ. Hãy mở lại bằng đúng đường link được cấp.', 403)
            if sent == 'null':
                raise Problem('Trình duyệt không gửi nguồn gốc yêu cầu. Hãy mở trang trực tiếp bằng link được cấp.', 403)
            if not request.is_json:
                raise Problem('API yêu cầu application/json.', 415)
        token = request.cookies.get('ott_session', '')
        if token:
            with store.connect() as db:
                row = db.execute('SELECT s.*,u.username,u.name,u.role,u.team_id,u.must_change,u.disabled,u.email '
                                 'FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.hash=? AND s.expires>?',
                                 (hashlib.sha256(token.encode()).hexdigest(), now())).fetchone()
            if row and not row['disabled']:
                g.user = {'id': row['user_id'], 'username': row['username'], 'name': row['name'],
                          'role': row['role'], 'team_id': row['team_id'], 'must_change': bool(row['must_change']),
                          'email': row['email']}
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
            if 'Cache-Control' not in response.headers:
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

    def rate(name, key, message=None, status=429):
        maximum, window = RATES[name]
        try:
            store.rate(f'{name}:{key}', maximum, window)
        except Problem:
            if not message:
                raise
            raise Problem(message, status)

    # ---- email codes ----------------------------------------------------

    def email_required():
        return bool(mail) and settings().get('email_required') == '1'

    def need_mail():
        if not mail:
            raise Problem('Máy chủ chưa bật gửi email. Liên hệ ban tổ chức.', 404)

    def clean_email(value):
        email = value.strip().lower() if isinstance(value, str) else ''
        if not email:
            raise Problem('Hãy nhập email.')
        if len(email) > 254 or not EMAIL.fullmatch(email):
            raise Problem('Email không hợp lệ. Ví dụ đúng: ten.ban@gmail.com')
        domain = email.rsplit('@', 1)[1]
        if email_domains and not any(domain == d or domain.endswith('.' + d) for d in email_domains):
            raise Problem(f'Chỉ nhận email {" hoặc ".join("@" + d for d in email_domains)}.')
        return email

    def email_free(email, user=None):
        with store.connect() as db:
            owner = db.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
        if owner and owner['id'] != user:
            raise Problem('Email này đã gắn với một tài khoản khác. Hãy đăng nhập, hoặc dùng "Quên mật khẩu".', 409)

    def code_message(purpose, code, username=None):
        what = {'register': 'xác minh email để tạo tài khoản', 'verify': 'gắn email này vào tài khoản của bạn',
                'reset': f'đặt lại mật khẩu cho tài khoản @{username}'}[purpose]
        subject = f'{code} là mã {"đặt lại mật khẩu" if purpose == "reset" else "xác minh"} OTTv2 Arena'
        body = (f'Xin chào,\n\nMã để {what} trên OTTv2 Arena là:\n\n    {code}\n\n'
                f'Mã dùng được trong {CODE_SECONDS // 60} phút và chỉ một lần. Đừng gửi mã này cho ai, kể cả người '
                f'xưng là ban tổ chức.\nNếu bạn không yêu cầu mã, hãy bỏ qua email này.\n\n{public_origin}\n')
        return subject, body

    def issue_code(purpose, email, user=None, username=None, wait=True):
        """Store a fresh 6-digit code for (purpose, email) and email it. Returns 'sent' or 'queued'."""
        with store.connect() as db:
            last = db.execute('SELECT created FROM email_codes WHERE purpose=? AND email=? ORDER BY created DESC LIMIT 1',
                              (purpose, email)).fetchone()
        if last and now() - last['created'] < RESEND_SECONDS:
            wait_for = int(RESEND_SECONDS - (now() - last['created'])) + 1
            raise Problem(f'Mã vừa được gửi. Kiểm tra hộp thư (cả mục Spam/Quảng cáo) hoặc đợi {wait_for} giây rồi gửi lại.', 429)
        rate('email_address', email, 'Địa chỉ này đã nhận nhiều mã trong một giờ. Hãy dùng mã mới nhất hoặc thử lại sau.')
        rate('email_ip', g.client_ip)
        rate('email_day', 'all', 'Hôm nay máy chủ đã gửi tối đa số email cho phép. Hãy báo ban tổ chức.', 503)
        code, cid = f'{secrets.randbelow(10 ** 6):06d}', uid()
        with store.connect(True) as db:
            db.execute('DELETE FROM email_codes WHERE expires<?', (now(),))
            db.execute('DELETE FROM email_codes WHERE purpose=? AND email=?', (purpose, email))
            db.execute('INSERT INTO email_codes(id,purpose,email,user_id,digest,expires,created) VALUES(?,?,?,?,?,?,?)',
                       (cid, purpose, email, user, code_digest(cid, code), now() + CODE_SECONDS, now()))
        future = mail.send(email, *code_message(purpose, code, username))
        if not wait:
            return 'queued'
        try:
            future.result(timeout=8)
            return 'sent'
        except concurrent.futures.TimeoutError:
            return 'queued'
        except Exception:
            with store.connect(True) as db:
                db.execute('DELETE FROM email_codes WHERE id=?', (cid,))
            raise Problem('Chưa gửi được email tới địa chỉ này. Kiểm tra lại địa chỉ, hoặc thử lại sau ít phút.', 502)

    def check_code(purpose, email, code, user=None):
        """Use up the code sent for (purpose, email); wrong guesses count, five end the code."""
        code = str(code or '').strip()
        if not re.fullmatch(r'\d{6}', code):
            raise Problem('Nhập mã xác minh gồm 6 chữ số trong email.')
        failure = None
        with store.connect(True) as db:
            row = db.execute('SELECT * FROM email_codes WHERE purpose=? AND email=? AND expires>? '
                             'ORDER BY created DESC LIMIT 1', (purpose, email, now())).fetchone()
            if not row or row['user_id'] != user:
                failure = 'Mã xác minh đã hết hạn hoặc chưa được gửi. Bấm "Gửi mã" để nhận mã mới.'
            elif row['attempts'] >= CODE_ATTEMPTS:
                failure = 'Nhập sai quá nhiều lần. Hãy bấm "Gửi mã" để nhận mã mới.'
            elif not secrets.compare_digest(row['digest'], code_digest(row['id'], code)):
                db.execute('UPDATE email_codes SET attempts=attempts+1 WHERE id=?', (row['id'],))
                left = CODE_ATTEMPTS - row['attempts'] - 1
                failure = f'Mã xác minh không đúng. Còn {left} lần thử.' if left else \
                    'Nhập sai quá nhiều lần. Hãy bấm "Gửi mã" để nhận mã mới.'
            else:
                db.execute('DELETE FROM email_codes WHERE id=?', (row['id'],))
        if failure:
            raise Problem(failure, 400)

    # ---- public ---------------------------------------------------------

    def health_data():
        with store.connect() as db:
            active = db.execute('SELECT mode FROM workers WHERE seen>?', (now()-40,)).fetchall()
            pending = db.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]
        values = settings()
        return {'ok': True, 'rules_hash': RULES_HASH, 'limits': LIMITS, 'runner_online': bool(active),
                'runner_slots': len(active), 'runner_modes': sorted({r['mode'] for r in active}),
                'pending': pending, 'registration_invite': bool(code), 'team_size': TEAM_SIZE,
                'email': {'enabled': bool(mail), 'required': email_required(), 'domains': email_domains},
                'announcement': values.get('announcement', ''),
                'open': {key: values.get(key) == '1' for key in
                         ('registration_open', 'submissions_open', 'ranked_open', 'practice_open', 'campaign_open',
                          'live_open', 'read_only')},
                'avg_seconds': {k: round(v, 1) for k, v in store.timing().items()},
                'maps': [{'id': i, 'name': n, 'blurb': b, 'geometry': i in GEOMETRY_MAPS} for i, n, b in MAPS]}

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
        user = {k: g.user[k] for k in ('id', 'username', 'name', 'role', 'must_change', 'email')}
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
        # A Secure cookie is silently dropped over plain http (e.g. the LAN address), so only
        # set it when the browser actually used https.
        scheme = urlsplit(request.headers.get('Origin') or '').scheme or request.headers.get('X-Forwarded-Proto') \
            or request.scheme
        response.set_cookie('ott_session', token, max_age=SESSION_SECONDS, httponly=True,
                            secure=scheme == 'https', samesite='Lax', path='/')
        return response

    @app.post('/api/register')
    def register():
        data = body()
        if settings().get('registration_open') != '1':
            raise Problem('Ban tổ chức đang tạm đóng đăng ký tài khoản.', 403)
        rate('register_ip', g.client_ip)
        username = text(data, 'username', 3, 32).lower()
        if not re.fullmatch(r'[a-z0-9_]{3,32}', username):
            raise Problem('Tên đăng nhập chỉ gồm chữ thường không dấu, số và dấu gạch dưới (3–32 ký tự).')
        if username in RESERVED_NAMES:
            raise Problem('Tên đăng nhập này được dành riêng. Hãy chọn tên khác.', 409)
        password = new_password(data, username)
        if code and not secrets.compare_digest(str(data.get('invite') or '').strip(), code):
            raise Problem('Mã tham gia không đúng. Hỏi ban tổ chức để lấy mã.', 403)
        student_id = str(data.get('student_id') or '').strip().upper()
        if student_id and not re.fullmatch(r'[A-Z0-9][A-Z0-9._-]{0,19}', student_id):
            raise Problem('Mã số sinh viên chỉ gồm chữ, số và dấu - _ . (tối đa 20 ký tự).')
        user = {'id': uid(), 'username': username, 'name': clean_name(data.get('name')), 'role': 'player'}
        with store.connect() as db:
            if db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                raise Problem('Tên đăng nhập đã có người dùng.', 409)
        # With email switched on, an address is optional unless the organisers require it; one given is always verified.
        email = None
        if mail and (data.get('email') or email_required()):
            email = clean_email(data.get('email'))
            email_free(email)
            check_code('register', email, data.get('email_code'))
        password_hash = generate_password_hash(password, method='scrypt')
        try:
            with store.connect(True) as db:
                db.execute('INSERT INTO users(id,username,name,password,role,created,student_id,email,email_verified) '
                           'VALUES(?,?,?,?,?,?,?,?,?)', (user['id'], username, user['name'], password_hash, 'player', now(),
                                                         student_id or None, email, now() if email else None))
                store.audit(db, user['id'], 'user.registered', {'username': username, 'email': bool(email)})
        except sqlite3.IntegrityError:
            raise Problem('Tên đăng nhập hoặc email vừa được người khác dùng. Hãy chọn cái khác.', 409)
        return session_response(user)

    @app.post('/api/register/email-code')
    def register_email_code():
        data = body()
        need_mail()
        if settings().get('registration_open') != '1':
            raise Problem('Ban tổ chức đang tạm đóng đăng ký tài khoản.', 403)
        email = clean_email(data.get('email'))
        email_free(email)
        status = issue_code('register', email)
        return jsonify(ok=True, email=mask_email(email), status=status, expires_in=CODE_SECONDS, resend_in=RESEND_SECONDS)

    def account_for(login):
        with store.connect() as db:
            return db.execute('SELECT * FROM users WHERE username=? OR email=?', (login, login)).fetchone()

    @app.post('/api/password/forgot')
    def forgot_password():
        data = body()
        need_mail()
        login = text(data, 'login', 3, 254).lower()
        rate('login_ip', g.client_ip)
        rate('reset_login', login)
        user = account_for(login)
        # The answer is the same whether or not the account exists or has an email, so it cannot be used to probe.
        if user and user['email'] and not user['disabled']:
            try:
                issue_code('reset', user['email'], user['id'], user['username'], wait=False)
            except Problem:
                pass
        return jsonify(ok=True, resend_in=RESEND_SECONDS, expires_in=CODE_SECONDS)

    @app.post('/api/password/reset')
    def reset_password():
        data = body()
        need_mail()
        login = text(data, 'login', 3, 254).lower()
        rate('login_user', f'{login}@{g.client_ip}')
        rate('login_account', login)
        rate('login_ip', g.client_ip)
        user = account_for(login)
        if not user or not user['email'] or user['disabled']:
            raise Problem('Mã xác minh đã hết hạn hoặc chưa được gửi. Bấm "Gửi mã" để nhận mã mới.', 400)
        password = new_password(data, user['username'])
        check_code('reset', user['email'], data.get('code'), user['id'])
        digest = generate_password_hash(password, method='scrypt')
        with store.connect(True) as db:
            db.execute('UPDATE users SET password=?,must_change=0 WHERE id=?', (digest, user['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?', (user['id'],))
            store.audit(db, user['id'], 'user.password_reset_by_email', {})
        g.session = None
        return session_response(dict(user))

    @app.post('/api/login')
    def login():
        data = body()
        username = text(data, 'username', 3, 32).lower()
        rate('login_user', f'{username}@{g.client_ip}')
        rate('login_account', username)
        rate('login_ip', g.client_ip)
        text(data, 'password', 1, 128)
        with store.connect() as db:
            user = db.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if not check_password_hash(user['password'] if user else DUMMY_HASH, data['password']) or not user:
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
        rate('login_user', f"{g.user['username']}@{g.client_ip}")
        new = new_password(data, g.user['username'])
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

    @app.post('/api/me/email/code')
    def my_email_code():
        user, data = auth(), body()
        need_mail()
        email = clean_email(data.get('email'))
        if email == g.user['email']:
            raise Problem('Email này đã gắn với tài khoản của bạn.', 409)
        email_free(email, user)
        status = issue_code('verify', email, user)
        return jsonify(ok=True, email=mask_email(email), status=status, expires_in=CODE_SECONDS, resend_in=RESEND_SECONDS)

    @app.post('/api/me/email')
    def my_email():
        user, data = auth(), body()
        need_mail()
        email = clean_email(data.get('email'))
        email_free(email, user)
        check_code('verify', email, data.get('code'), user)
        try:
            with store.connect(True) as db:
                db.execute('UPDATE users SET email=?,email_verified=? WHERE id=?', (email, now(), user))
                store.audit(db, user, 'user.email_verified', {})
        except sqlite3.IntegrityError:
            raise Problem('Email này vừa được gắn với một tài khoản khác.', 409)
        return jsonify(ok=True, email=email)

    # ---- teams ----------------------------------------------------------

    @app.post('/api/team')
    def create_team():
        user = auth()
        rate('team_user', user)
        return jsonify(id=store.create_team(user, text(body(), 'name', 2, 40))), 201

    @app.post('/api/team/solo')
    def solo_team():
        user = auth()
        rate('team_user', user)
        return jsonify(id=store.create_solo_team(user)), 201

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
        ids = store.practice(team, user, text(data, 'p1', 24, 24), text(data, 'p2', 24, 24), data.get('map') or 'standard')
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
                              't.auto_advance,t.description,t.seed_from,t.map,t.final_map,(t.join_code IS NOT NULL) locked,'
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
        data['locked'] = data.pop('join_code') is not None      # the cache is shared: the code itself never goes in
        data['config'] = json.loads(data['config'])
        return {'tournament': data, 'standings': standings, 'unfinished': unfinished}

    @app.get('/api/tournaments/<tid>')
    def tournament(tid):
        data = dict(cache.get(f'tournament:{tid}', 3, lambda: tournament_data(tid)))
        team = g.user['team_id'] if g.user else None
        if g.user and g.user['role'] == 'admin':     # organisers read the code from the same call
            with store.connect() as db:
                code = db.execute('SELECT join_code FROM tournaments WHERE id=?', (tid,)).fetchone()
            data['tournament'] = {**data['tournament'], 'join_code': code[0] if code else None}
        everyone = data['standings']
        data['mine'] = next((s for s in everyone if s['team_id'] == team), None)
        data['count'] = len(everyone)
        data['active'] = sum(1 for s in everyone if s['entry_status'] == 'active')
        data['pending'] = sum(1 for s in everyone if s['bot_status'] in ('queued', 'running'))
        data['top'] = [s for s in everyone if s['rank'] and s['rank'] <= 3] if data['tournament']['status'] == 'completed' else []
        rows = search_rows(everyone, ('name', 'bot'))
        data['total'] = len(rows)
        offset, limit = paging(100, 5000)
        data['standings'] = rows[offset:offset + limit]
        return jsonify(data)

    def paging(default, ceiling):
        """?offset=&limit= with sane bounds, so a thousand-team table is never sent whole by accident."""
        def number(name, fallback):
            try:
                return max(0, int(request.args.get(name, fallback)))
            except ValueError:
                return fallback
        return number('offset', 0), min(max(1, number('limit', default)), ceiling)

    def search_rows(rows, keys):
        query = request.args.get('q', '').strip().lower()[:60]
        return [r for r in rows if any(query in str(r[key] or '').lower() for key in keys)] if query else rows

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
        everyone = cache.get(f'round:{tid}:{number}', 3, lambda: round_data(tid, number))
        team = g.user['team_id'] if g.user else None
        rows = search_rows(everyone, ('a_name', 'b_name'))
        offset, limit = paging(100, 2000)
        return jsonify(items=rows[offset:offset + limit], total=len(rows), count=len(everyone),
                       mine=next((p for p in everyone if team and team in (p['a'], p['b'])), None))

    @app.get('/api/tournaments/<tid>/mine')
    def tournament_path(tid):
        user, team = team_required()
        with store.connect() as db:
            if not db.execute('SELECT 1 FROM tournaments WHERE id=?', (tid,)).fetchone():
                raise Problem('Không tìm thấy giải.', 404)
        return jsonify(store.my_tournament_path(tid, team))

    @app.post('/api/tournaments/<tid>/join')
    def join(tid):
        user, team = team_required()
        data = body()
        code = str(data.get('code') or '')[:20]
        if code:
            rate('tournament_code', user)       # a wrong code can be guessed only so many times
        store.join(team, tid, text(data, 'submission_id', 24, 24), user, code=code)
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

    STATUSES = ('running', 'queued', 'done', 'failed', 'cancelled')

    def team_match_filter(query):
        """Games of teams whose name contains `query`, found through the p1/p2 indexes (no table scan)."""
        teams = 'SELECT s.id FROM submissions s JOIN teams t ON t.id=s.team_id WHERE t.name LIKE ?'
        return f'(m.p1 IN ({teams}) OR m.p2 IN ({teams}))', [f'%{query}%'] * 2

    def match_list(status, tid, kind='', query=''):
        where, args = [], []
        if tid:
            where.append('p.tournament_id=?')
            args.append(tid)
        if kind in ('practice', 'ranked', 'tournament', 'campaign', 'friendly'):
            where.append('m.kind=?')
            args.append(kind)
        if query:
            clause, found = team_match_filter(query)
            where.append(clause)
            args += found
        # Running games first, then waiting, then the newest of the rest. One index-backed query per status
        # (newest 100 each) instead of sorting every stored game with a CASE expression.
        statuses = [status] if status in STATUSES else STATUSES
        rows = []
        with store.connect() as db:
            for state in statuses:
                rows += db.execute(MATCH_SELECT + 'WHERE ' + ' AND '.join(['m.status=?', *where]) +
                                   ' ORDER BY m.created DESC LIMIT 100', (state, *args)).fetchall()
        rows.sort(key=lambda r: ({'running': 0, 'queued': 1}.get(r['status'], 2), -r['created'], r['id']))
        return [dict(r) for r in rows[:100]]

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
        kind, query = request.args.get('kind', ''), request.args.get('q', '').strip()[:40]
        return jsonify(items=cache.get(f'matches:{status}:{tid}:{kind}:{query}', 3,
                                       lambda: match_list(status, tid, kind, query)))

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
            sibling, number = store.sibling(db, mid, row['kind'], row['pairing_id'], row['created'], row['requester'],
                                            row['p1'], row['p2'])
        data = dict(row)
        data['sibling'], data['game_number'] = sibling, number
        data['replay'] = json.loads(data['replay']) if data['replay'] else None
        data['progress'] = json.loads(data['progress']) if data['progress'] else None
        data['position'] = position
        for key in ('p1', 'p2', 'requester'):
            data.pop(key, None)
        return data

    @app.get('/api/matches/<mid>')
    def match(mid):
        data = cache.get(f'match:{mid}', 2, lambda: match_data(mid))
        return jsonify({**data, 'viewers': touch_viewer(mid)})

    # In-memory, per process: who has polled a game in the last few seconds.
    viewers, viewers_lock = {}, threading.Lock()

    def touch_viewer(mid):
        key = g.user['id'] if g.user else g.client_ip
        moment = time.monotonic()
        with viewers_lock:
            seen = viewers.setdefault(mid, {})
            seen[key] = moment
            for stale in [k for k, t in seen.items() if moment - t > 8]:
                del seen[stale]
            if len(viewers) > 5000:
                for old in [m for m, v in viewers.items() if not v or max(v.values()) < moment - 60]:
                    del viewers[old]
            return len(seen)

    @app.get('/api/matches/<mid>/live')
    def match_live(mid):
        """Cheap incremental poll: only moves after `since`; `reset` when the game restarted."""
        try:
            since = max(0, int(request.args.get('since', 0)))
        except ValueError:
            since = 0
        live = cache.get(f'live:{mid}', 1, lambda: store.match_live(mid))
        moves = live['moves']
        reset = since > len(moves)
        return jsonify({**{k: v for k, v in live.items() if k != 'moves'}, 'ply': len(moves), 'reset': reset,
                        'moves': moves if reset else moves[since:], 'viewers': touch_viewer(mid),
                        'live': settings().get('live_open') == '1'})

    @app.get('/api/me/live')
    def my_live():
        auth()
        team = g.user['team_id']
        return jsonify(items=cache.get(f'mylive:{team}', 2, lambda: store.live_matches(team)) if team else [])

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
            data.get('seed_from') or None, data.get('seed_count'), data.get('map') or 'standard',
            data.get('final_map') or None, bool(data.get('private')) and not data.get('seed_from'))
        return jsonify(id=tid), 201

    @app.post('/api/admin/tournaments/<tid>/update')
    def update_tournament(tid):
        actor, data = auth(True), body()
        store.update_tournament(tid, actor, data.get('name'), data.get('description'), data.get('deadline'), data.get('rounds'), data.get('map'),
                                data['final_map'] if 'final_map' in data else False)
        return jsonify(ok=True)

    @app.post('/api/admin/tournaments/<tid>/cancel')
    def cancel_tournament(tid):
        return jsonify(store.cancel_tournament(tid, auth(True)))

    @app.post('/api/admin/tournaments/<tid>/entries')
    def add_entry(tid):
        actor, data = auth(True), body()
        return jsonify(store.admin_add_entry(tid, data.get('team'), actor)), 201

    @app.post('/api/admin/tournaments/<tid>/entries/<team>/remove')
    def remove_entry(tid, team):
        store.admin_remove_entry(tid, team, auth(True))
        return jsonify(ok=True)

    @app.post('/api/admin/tournaments/<tid>/freeze')
    def freeze(tid):
        return jsonify(store.freeze(tid, auth(True), bool(body().get('force'))))

    @app.post('/api/admin/tournaments/<tid>/start')
    def start_tournament(tid):
        return jsonify(store.start_tournament(tid, auth(True), bool(body().get('force'))))

    @app.post('/api/admin/tournaments/<tid>/code')
    def tournament_code(tid):
        return jsonify(code=store.set_join_code(tid, auth(True), body().get('action')))

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
            members, emails = {}, {}
            for m in db.execute('SELECT team_id,name,student_id,email FROM users WHERE team_id IN '
                                '(SELECT team_id FROM entries WHERE tournament_id=?)', (tid,)):
                members.setdefault(m['team_id'], []).append(m['name'] + (f" ({m['student_id']})" if m['student_id'] else ''))
                if m['email']:
                    emails.setdefault(m['team_id'], []).append(m['email'])
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(['Hạng', 'Xếp loại', 'Đội', 'Thành viên', 'Bot', 'Điểm cặp', 'Buchholz', 'Điểm ván',
                         'Thắng', 'Hòa', 'Thua', 'BYE', 'Hạt giống', 'Trạng thái', 'Email'])
        for s in rows:
            writer.writerow([s['rank'] or '', s['tier'], sheet_cell(s['name']),
                             sheet_cell('; '.join(members.get(s['team_id'], []))), sheet_cell(s['bot']),
                             s['points'], s['buchholz'], s['game_points'], s['wins'], s['draws'], s['losses'],
                             s['byes'], s['seed'] or '', s['entry_status'],
                             sheet_cell('; '.join(emails.get(s['team_id'], [])))])
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
                "WHERE t.status NOT IN ('system','deleted') AND (t.name LIKE ? OR t.code LIKE ? OR t.id IN "
                '(SELECT team_id FROM users WHERE username LIKE ? OR name LIKE ? OR student_id LIKE ?)) '
                'ORDER BY t.created DESC LIMIT 50 OFFSET ?',
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
        elif action == 'delete':
            return jsonify(store.delete_team(team, actor))
        else:
            raise Problem('Thao tác không hợp lệ.')
        return jsonify(ok=True)

    @app.post('/api/admin/teams-cleanup')
    def teams_cleanup():
        actor, data = auth(True), body()
        return jsonify(store.cleanup_empty_teams(actor, bool(data.get('confirm'))))

    @app.get('/api/admin/users')
    def admin_users():
        auth(True)
        q = f"%{request.args.get('q', '').strip()}%"
        with store.connect() as db:
            rows = db.execute('SELECT u.id,u.username,u.name,u.role,u.created,u.student_id,u.disabled,u.must_change,'
                              'u.email,t.name team,u.team_id,(SELECT COUNT(*) FROM users x WHERE x.team_id=u.team_id) team_size '
                              'FROM users u LEFT JOIN teams t ON t.id=u.team_id WHERE u.username LIKE ? '
                              'OR u.name LIKE ? OR u.student_id LIKE ? OR t.name LIKE ? OR u.email LIKE ? '
                              'ORDER BY u.created DESC LIMIT 50 OFFSET ?', (q, q, q, q, q, page()*50)).fetchall()
        return jsonify(items=[dict(r) for r in rows])

    @app.post('/api/admin/users/<user>')
    def admin_user(user):
        actor, data = auth(True), body()
        action = data.get('action')
        if action == 'delete':
            return jsonify(store.delete_user(user, actor, bool(data.get('delete_team'))))
        if action == 'reset_password':
            secret = temp_password()
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
        q = request.args.get('q', '').strip()[:60]
        if q:
            clause, found = team_match_filter(q)
            where.append(f'({clause} OR m.id LIKE ?)')
            args += [*found, f'{q}%']
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

    # ---- pages ----------------------------------------------------------

    rendered = {}

    def asset(name):
        """A public script, stylesheet or page with its asset URLs stamped with this build's version."""
        suffix = name.rsplit('.', 1)[-1]
        if name not in rendered:
            text = (ROOT / name).read_text(encoding='utf-8')
            if suffix == 'js':
                text = JS_IMPORT.sub(lambda m: f'{m[1]}{m[2]}?v={ASSET_VERSION}{m[3]}', text)
            elif suffix == 'html':
                text = HTML_ASSET.sub(lambda m: f'{m[1]}{m[2]}?v={ASSET_VERSION}{m[3]}', text)
            rendered[name] = text.encode('utf-8')
        body = rendered[name]
        response = Response(body, mimetype={'js': 'text/javascript', 'css': 'text/css', 'html': 'text/html'}[suffix])
        response.set_etag(hashlib.sha256(body).hexdigest()[:20])
        # Only an exact versioned URL may be cached for long; anything else must be revalidated.
        stamped = suffix != 'html' and request.args.get('v') == ASSET_VERSION
        response.headers['Cache-Control'] = 'public, max-age=31536000, immutable' if stamped else 'no-cache'
        return response.make_conditional(request)

    @app.get('/')
    def home():
        return asset('arena/index.html')

    @app.get('/arena')
    @app.get('/arena/')
    def arena():
        return asset('arena/index.html')

    @app.get('/arena/admin')
    @app.get('/arena/admin.html')
    def admin_page():
        return asset('arena/admin.html')

    @app.post('/arena/enable-javascript')
    def no_script():
        # Forms post here when the page's script did not run, so a password is never put in the address bar.
        return Response(NO_SCRIPT_PAGE, status=400, mimetype='text/html')

    @app.get('/<path:filename>')
    def files(filename):
        if filename in PUBLIC_FILES:
            if filename.rsplit('.', 1)[-1] in ('js', 'css', 'html'):
                return asset(filename)
            return send_from_directory(ROOT, filename)
        if filename in SAMPLES:
            return send_from_directory(ROOT, filename, as_attachment=True)
        raise Problem('Không tìm thấy.', 404)

    return app
