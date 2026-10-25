"""Import bot submissions collected with Google Forms.

Two layouts are supported:
  * the code is pasted into a paragraph answer (CSV export only), or
  * the code is uploaded as a file; the organiser downloads the Drive upload
    folder as a ZIP. Teams name the file after their team code or team name,
    e.g. "K7QF2M9A.py" (Drive appends " - Respondent name").
Rows are processed in CSV order, so a team's last response wins."""
import csv
import io
import os
import re
import secrets
import unicodedata
import zipfile
from datetime import datetime
from pathlib import Path

from werkzeug.security import generate_password_hash

from .store import Problem, Store, LIMITS, name_key, slug, uid, now

MAX_ROWS = 5000
MAX_CSV_BYTES = 10_000_000
MAX_ZIP_MEMBERS = 5000
MAX_ZIP_TOTAL = 200_000_000
ROLES = [
    ('timestamp', ['timestamp', 'dau thoi gian']),
    ('team_code', ['ma doi', 'team code', 'teamcode', 'ma nhom']),
    ('bot_name', ['ten bot', 'bot name', 'ten phien ban']),
    ('file', ['file', 'tep', 'upload', 'tai len']),
    ('source', ['ma nguon', 'source', 'code python', 'ma python', 'bot code', 'dan code', 'code']),
    ('members', ['thanh vien', 'members', 'ho ten', 'mssv']),
    ('email', ['email']),
    ('team', ['ten doi', 'team name', 'ten nhom', 'team', 'doi', 'nhom']),
]


def header_key(text):
    value = unicodedata.normalize('NFD', str(text)).replace('đ', 'd').replace('Đ', 'D')
    value = ''.join(c for c in value if unicodedata.category(c) != 'Mn').lower()
    return ' '.join(re.findall(r'[a-z0-9]+', value))


def detect(headers, overrides=None):
    overrides = {k: v for k, v in (overrides or {}).items() if v}
    mapping, used = {}, set()
    for role, header in overrides.items():
        if header not in headers:
            raise Problem(f'Không có cột "{header}" trong CSV.')
        mapping[role], used = header, used | {header}
    for role, words in ROLES:
        if role in mapping:
            continue
        for header in headers:
            key = f' {header_key(header)} '
            if header not in used and any(f' {word} ' in key for word in words):
                mapping[role] = header
                used.add(header)
                break
    if 'team' not in mapping and 'team_code' not in mapping:
        raise Problem('CSV cần cột tên đội hoặc mã đội.')
    if 'source' not in mapping and 'file' not in mapping:
        raise Problem('CSV cần cột mã nguồn hoặc cột file tải lên.')
    return mapping


def read_csv(data):
    if len(data) > MAX_CSV_BYTES:
        raise Problem('File CSV lớn hơn 10 MB.')
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        raise Problem('CSV cần mã hóa UTF-8 (Google Forms → Tải xuống CSV).') from None
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        raise Problem('CSV trống.')
    headers = [h.strip() for h in rows[0]]
    if len(set(headers)) != len(headers):
        raise Problem('CSV có hai cột trùng tên; đổi tên một cột rồi thử lại.')
    body = [dict(zip(headers, row)) for row in rows[1:] if any(cell.strip() for cell in row)]
    if len(body) > MAX_ROWS:
        raise Problem(f'CSV có hơn {MAX_ROWS} dòng.')
    return headers, body


def decode_source(data):
    if len(data) > LIMITS['source_bytes']:
        raise Problem('File lớn hơn 64 KiB.')
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        text = data.decode('utf-16')
    else:
        try:
            text = data.decode('utf-8-sig')
        except UnicodeDecodeError:
            raise Problem('File không phải UTF-8. Lưu lại dạng UTF-8 rồi nộp lại.') from None
    return text


def tidy(text):
    text = text.replace('\xa0', ' ').replace('​', '').strip('\n')
    fenced = re.fullmatch(r'\s*```[a-zA-Z]*\n(.*?)\n```\s*', text, re.S)
    if fenced:
        text = fenced.group(1)
    if text.lstrip().lower().startswith(('<!doctype html', '<html')):
        raise Problem('Nội dung là trang HTML, không phải file Python (tải sai file từ Drive?).')
    return text


class Files:
    def __init__(self):
        self.items = []

    def add(self, name, data, stamp=0):
        base = Path(name.replace('\\', '/')).name
        if not base or base.startswith('.') or not base.lower().endswith(('.py', '.txt')):
            return
        stem = base.rsplit('.', 1)[0]
        keys = {slug(stem), slug(re.sub(r'\s*\(\d+\)$', '', stem.split(' - ')[0]))} - {''}
        self.items.append({'name': base, 'data': data, 'stamp': stamp, 'keys': keys})

    @classmethod
    def from_zip(cls, data):
        files = cls()
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile:
            raise Problem('File ZIP không hợp lệ.') from None
        members = [m for m in archive.infolist() if not m.is_dir() and '__MACOSX' not in m.filename]
        if len(members) > MAX_ZIP_MEMBERS or sum(m.file_size for m in members) > MAX_ZIP_TOTAL:
            raise Problem('ZIP quá lớn hoặc quá nhiều file.')
        csv_data = None
        for member in members:
            if member.filename.lower().endswith('.csv'):
                csv_data = archive.read(member) if member.file_size <= MAX_CSV_BYTES else None
                continue
            with archive.open(member) as stream:
                # Never trust the header size; read at most one byte past the limit.
                content = stream.read(LIMITS['source_bytes'] + 1)
            files.add(member.filename, content, datetime(*member.date_time).timestamp())
        return files, csv_data

    @classmethod
    def from_directory(cls, directory):
        files = cls()
        for path in Path(directory).rglob('*'):
            if path.is_file() and not path.is_symlink():
                with path.open('rb') as stream:
                    files.add(path.name, stream.read(LIMITS['source_bytes'] + 1), path.stat().st_mtime)
        return files

    def match(self, candidates, explicit=''):
        wanted = {slug(c) for c in candidates if c} - {''}
        if explicit and not explicit.startswith('http'):
            wanted |= {slug(Path(explicit.split(',')[0].strip()).stem)}
        found = [item for item in self.items if item['keys'] & wanted]
        if not found:
            return None, 'Không tìm thấy file của đội trong ZIP/thư mục.'
        if len({item['data'] for item in found}) == 1:
            return found[0], ''
        newest = max(found, key=lambda item: item['stamp'])
        names = ', '.join(sorted(item['name'] for item in found))
        return newest, f'Có nhiều file khớp ({names}); dùng file mới nhất {newest["name"]}.'


def plan(store, csv_data, files=None, overrides=None, create_teams=False):
    headers, rows = read_csv(csv_data)
    mapping = detect(headers, overrides)
    if 'source' not in mapping and files is None:
        raise Problem('CSV chỉ có link file Drive. Tải thư mục file từ Drive (ZIP) và tải lên cùng CSV.')
    cell = lambda row, role: (row.get(mapping.get(role, ''), '') or '').strip()
    latest = {}
    for number, row in enumerate(rows, 2):
        code, team = cell(row, 'team_code').upper(), ' '.join(cell(row, 'team').split())
        key = f'code:{code}' if code else f'name:{name_key(team)}' if team else f'row:{number}'
        latest[key] = number
    result = []
    with store.connect() as db:
        for number, row in enumerate(rows, 2):
            code, team = cell(row, 'team_code').upper(), ' '.join(cell(row, 'team').split())
            key = f'code:{code}' if code else f'name:{name_key(team)}' if team else f'row:{number}'
            item = {'row': number, 'team': team, 'code': code, 'members': cell(row, 'members')[:300],
                    'email': cell(row, 'email')[:120], 'bot_name': (cell(row, 'bot_name') or 'Bản nộp Google Form')[:50],
                    'status': 'ready', 'message': '', 'team_id': None, 'source': None}
            if len(item['bot_name']) < 2:
                item['bot_name'] = 'Bản nộp Google Form'
            result.append(item)
            if not code and not team:
                item.update(status='error', message='Thiếu tên đội và mã đội.')
                continue
            if latest[key] != number:
                item.update(status='superseded', message=f'Đội đã gửi lại ở dòng {latest[key]}; dùng bản mới hơn.')
                continue
            if code:
                found = db.execute('SELECT id,name,status FROM teams WHERE code=?', (code,)).fetchone()
                if not found:
                    item.update(status='error', message='Mã đội không tồn tại trên hệ thống.')
                    continue
            else:
                found = db.execute('SELECT id,name,status FROM teams WHERE name_key=?', (name_key(team),)).fetchone()
            if found:
                if found['status'] != 'active':
                    item.update(status='error', message='Đội đã bị truất quyền.')
                    continue
                item.update(team_id=found['id'], team=found['name'])
            elif not create_teams:
                item.update(status='error', message='Chưa có đội này trên hệ thống (bật "tạo đội mới" nếu muốn).')
                continue
            elif not 2 <= len(team) <= 40:
                item.update(status='error', message='Tên đội cần 2–40 ký tự để tạo đội mới.')
                continue
            try:
                pasted = cell(row, 'source') if 'source' in mapping else ''
                if pasted:
                    source = tidy(pasted)
                else:
                    match, warning = files.match([code, team, item['team']], cell(row, 'file')) if files else (None, '')
                    if not match:
                        raise Problem(warning or 'Không có mã nguồn.')
                    source = tidy(decode_source(match['data']))
                    item['message'] = warning or f'File {match["name"]}.'
                source = Store.clean_source(source)
            except Problem as exc:
                item.update(status='error', message=str(exc))
                continue
            item['source'] = source
            if item['team_id']:
                previous = db.execute('SELECT source FROM submissions WHERE team_id=? ORDER BY created DESC LIMIT 1',
                                      (item['team_id'],)).fetchone()
                if previous and previous['source'] == source:
                    item.update(status='unchanged', message='Trùng bản nộp gần nhất của đội; bỏ qua.')
    summary = {status: sum(1 for item in result if item['status'] == status)
               for status in ('ready', 'unchanged', 'superseded', 'error')}
    summary['rows'] = len(result)
    return {'mapping': mapping, 'rows': result, 'summary': summary}


def password():
    return ''.join(secrets.choice('abcdefghjkmnpqrstuvwxyz23456789') for _ in range(12))


def apply(store, planned, actor, tournament=None, create_accounts=False):
    """Write the planned rows. Password hashing happens before the write lock is taken."""
    ready = [item for item in planned['rows'] if item['status'] == 'ready']
    accounts = {}
    if create_accounts:
        for item in ready:
            if not item['team_id']:
                secret = password()
                accounts[item['row']] = (secret, generate_password_hash(secret, method='scrypt'))
    credentials = []
    with store.connect(True) as db:
        if tournament and not db.execute("SELECT 1 FROM tournaments WHERE id=? AND status='open'",
                                         (tournament,)).fetchone():
            raise Problem('Giải đã khóa danh sách hoặc không tồn tại.', 409)
        for item in ready:
            db.execute('SAVEPOINT row')
            try:
                team = item['team_id']
                if not team:
                    team = store.insert_team(db, item['team'])
                    db.execute('UPDATE teams SET note=? WHERE id=?', (item['members'], team))
                    if item['row'] in accounts:
                        base = (slug(item['team']) or 'doi')[:20]
                        username, suffix = base if len(base) >= 3 else f'doi{base}', 1
                        while db.execute('SELECT 1 FROM users WHERE username=?', (username,)).fetchone():
                            suffix += 1
                            username = f'{base[:20]}{suffix}'
                        secret, digest = accounts[item['row']]
                        user = uid()
                        db.execute('INSERT INTO users(id,username,name,password,role,created,team_id,must_change) '
                                   "VALUES(?,?,?,?,'player',?,?,1)", (user, username, item['team'], digest, now(), team))
                        db.execute('UPDATE teams SET owner=? WHERE id=?', (user, team))
                        credentials.append({'team': item['team'], 'username': username, 'password': secret,
                                            'email': item['email'], 'members': item['members']})
                    item['created_team'] = True
                # No author: the team sees "Google Form", the organiser is recorded in the audit log.
                sid = store.submit(team, None, item['bot_name'], item['source'], 'form', db, limit=False)
                if tournament:
                    store.join(team, tournament, sid, actor, db, admin=True)
                item.update(status='imported', team_id=team, submission_id=sid)
                db.execute('RELEASE row')
            except Problem as exc:
                db.execute('ROLLBACK TO row')
                db.execute('RELEASE row')
                item.update(status='error', message=str(exc))
        store.audit(db, actor, 'import.applied', {'rows': len(planned['rows']), 'imported':
                                                   sum(1 for i in ready if i['status'] == 'imported'),
                                                   'tournament': tournament})
    return credentials


def public_rows(planned):
    return [{k: v for k, v in item.items() if k != 'source'} for item in planned['rows']]


def credentials_csv(credentials):
    out = io.StringIO()
    writer = csv.DictWriter(out, ['team', 'username', 'password', 'email', 'members'])
    writer.writeheader()
    writer.writerows(credentials)
    return '﻿' + out.getvalue()


def run_cli(store, csv_path, files_path=None, tournament=None, create_teams=False, credentials_path=None,
            dry_run=False, overrides=None):
    csv_data = Path(csv_path).read_bytes()
    files = None
    if files_path:
        path = Path(files_path)
        if path.is_dir():
            files = Files.from_directory(path)
        else:
            files, _ = Files.from_zip(path.read_bytes())
    planned = plan(store, csv_data, files, overrides, create_teams)
    credentials = []
    if not dry_run:
        if credentials_path and Path(credentials_path).exists():
            raise Problem('File thông tin đăng nhập đã tồn tại; chọn tên mới.')
        credentials = apply(store, planned, 'local-cli', tournament, bool(credentials_path))
        if credentials:
            fd = os.open(credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
                stream.write(credentials_csv(credentials))
    return planned, credentials
