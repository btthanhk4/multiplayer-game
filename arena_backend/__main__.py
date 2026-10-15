import argparse
import getpass
import os
import re
import time
from pathlib import Path

from werkzeug.security import generate_password_hash
from .store import Store, ROOT, uid, now


def main():
    parser = argparse.ArgumentParser(description='OTTv2 Arena service')
    parser.add_argument('--data', default=os.getenv('ARENA_DATA', str(ROOT / 'arena-data')))
    commands = parser.add_subparsers(dest='command', required=True)
    serve = commands.add_parser('serve')
    serve.add_argument('--host', default='127.0.0.1')
    serve.add_argument('--port', type=int, default=8080)
    admin = commands.add_parser('admin', help='Create an admin or reset its password locally')
    admin.add_argument('username')
    admin.add_argument('--name', default='Ban tổ chức')
    worker = commands.add_parser('worker')
    worker.add_argument('--mode', choices=['docker','docker-dev','samples'], default='docker')
    worker.add_argument('--image', default=os.getenv('ARENA_BOT_IMAGE', 'ottv2-bot:1'))
    backup = commands.add_parser('backup')
    backup.add_argument('destination')
    args = parser.parse_args()
    store = Store(args.data)
    if args.command == 'serve':
        from waitress import serve as run
        from .app import create_app
        print(f'OTTv2: http://{args.host}:{args.port}/arena/', flush=True)
        run(create_app(args.data), host=args.host, port=args.port, threads=8,
            max_request_body_size=400_000, channel_timeout=30, connection_limit=100)
    elif args.command == 'admin':
        if not re.fullmatch(r'[a-z0-9_]{3,32}', args.username):
            parser.error('Username: 3–32 lowercase letters, numbers or underscores.')
        password = getpass.getpass('Password (12+ characters): ')
        if len(password) < 12 or len(password) > 128 or password != password.strip():
            parser.error('Password must have 12–128 characters, no leading/trailing spaces.')
        with store.connect(True) as db:
            row = db.execute('SELECT id FROM users WHERE username=?', (args.username,)).fetchone()
            if row:
                db.execute("UPDATE users SET password=?,role='admin' WHERE id=?",
                           (generate_password_hash(password, method='scrypt'), row['id']))
                db.execute('DELETE FROM sessions WHERE user_id=?', (row['id'],))
            else:
                db.execute('INSERT INTO users VALUES(?,?,?,?,?,?)',
                           (uid(),args.username,args.name,generate_password_hash(password, method='scrypt'),'admin',now()))
            store.audit(db, 'local-cli', 'admin.provisioned', {'username':args.username})
        print('Admin ready. Sign in at /arena/.')
    elif args.command == 'backup':
        target = Path(args.destination).resolve()
        if target.exists():
            parser.error('Choose a new backup filename; existing files are not overwritten.')
        store.backup(target)
        print('Backup written:', target)
    else:
        from .runner import Executor, Worker
        origin = os.getenv('ARENA_ORIGIN', 'http://localhost:8080')
        if args.mode != 'docker' and not origin.startswith(('http://localhost:', 'http://127.0.0.1:')):
            parser.error('Public deployment requires --mode docker (gVisor).')
        executor = Executor(args.mode, args.image)
        executor.preflight()
        processor = Worker(store, executor)
        print('Runner ready:', args.mode, flush=True)
        try:
            while True:
                if not processor.run_once():
                    time.sleep(2)
        except KeyboardInterrupt:
            print('Runner stopped; unfinished job will be recovered after its lease expires.')


if __name__ == '__main__':
    main()
