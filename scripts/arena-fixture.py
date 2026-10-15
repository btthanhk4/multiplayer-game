"""Disposable browser-test data only. Refuses paths outside artifacts/."""
import json
import os
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from werkzeug.security import generate_password_hash
from arena_backend.store import Store, uid, now, RULES_HASH, LIMITS
from arena_backend.runner import Referee

directory=Path(sys.argv[1]).resolve()
if not directory.is_relative_to(ROOT/'artifacts'):
    raise SystemExit('Fixture requires a disposable artifacts/ directory.')
store=Store(directory)
password=os.environ['ARENA_TEST_PASSWORD']
users=[]
with store.connect(True) as db:
    for name,role in [('arena_admin','admin'),('river','player'),('stone','player')]:
        user=uid(); users.append(user)
        db.execute('INSERT INTO users VALUES(?,?,?,?,?,?)',(user,name,name,generate_password_hash(password),'admin' if role=='admin' else 'player',now()))
sids=[]
for user,name in zip(users,['Thợ săn','Người gác','Nước đầu']):
    sids.append(store.submit(user,name,(ROOT/'sdk/examples/hunter.py').read_text(encoding='utf-8')))
with store.connect(True) as db:
    db.execute("UPDATE submissions SET status='valid',log='Browser test fixture'")
    db.execute("UPDATE jobs SET status='done'")
    tid=uid()
    db.execute('INSERT INTO tournaments(id,name,format,rounds,deadline,rules_hash,config,created) VALUES(?,?,?,?,?,?,?,?)',
               (tid,'Khai màn · Mùa chiến thuật','swiss',5,now()+86400,RULES_HASH,json.dumps(LIMITS),now()))
for user,sid in zip(users,sids): store.join(user,tid,sid)
ref=Referee()
try:
    packet=ref.call('new'); moves=[]
    while not packet['state']['winner']:
        selected=packet['legalMoves'][0]
        move={key:selected[key] for key in ('from','to')}
        side=packet['state']['turn']
        packet=ref.call('move',**move)
        moves.append({**move,'side':side,'notation':packet['state']['lastMove']['notation']})
    result={'winner':packet['state']['winner'],'reason':packet['state']['winReason'],'moves':moves,
            'final_state':packet['state'],'rules_hash':RULES_HASH,'execution_mode':'fixture','finished':now()}
finally: ref.close()
with store.connect(True) as db:
    for index in range(6):
        mid=store.create_match(db,sids[index%3],sids[(index+1)%3],users[0])
        db.execute("UPDATE matches SET status='done',winner=?,reason=?,replay=? WHERE id=?",
                   (result['winner'],result['reason'],json.dumps(result),mid))
        db.execute("UPDATE jobs SET status='done' WHERE ref=?",(mid,))
print('Browser fixtures ready.')
