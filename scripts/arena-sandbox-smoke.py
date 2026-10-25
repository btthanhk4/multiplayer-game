"""Run only against a locally built sandbox image. Never executes test code on the host."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arena_backend.runner import Executor, Referee, observation, BotFault
from arena_backend.store import ROOT, LIMITS

parser=argparse.ArgumentParser()
parser.add_argument('--mode', choices=['docker','docker-dev'],default='docker')
args=parser.parse_args()
executor=Executor(args.mode)
executor.preflight()
referee=Referee()
try:
    packet=referee.call('new')
    obs=observation(packet, 100)
    second=observation(referee.call('move', **{k: packet['legalMoves'][0][k] for k in ('from','to')}), 101)
finally:
    referee.close()

move="return {'from': o['legalMoves'][0]['from'], 'to': o['legalMoves'][0]['to']}"
success={
    'sample': (ROOT/'sdk/examples/hunter.py').read_text(encoding='utf-8'),
    'large UTF-8 source packet': '#'+('ế'*18000)+'\ndef choose_move(o):\n    '+move,
    'print goes to the log, not the protocol': "def choose_move(o):\n    print('x'*100000)\n    "+move,
    'network denied': "import socket\ndef choose_move(o):\n    s=socket.socket()\n    s.settimeout(.3)\n    try:\n        s.connect(('1.1.1.1',443))\n    except OSError:\n        "+move+"\n    raise RuntimeError('Network accessible')",
    'read-only filesystem': "def choose_move(o):\n    try:\n        open('/opt/ott/write-test','w')\n    except OSError:\n        "+move+"\n    raise RuntimeError('Filesystem writable')",
    'child process denied': "import subprocess\ndef choose_move(o):\n    try:\n        subprocess.run(['python','-c','print(1)'],check=True)\n    except OSError:\n        "+move+"\n    raise RuntimeError('Process creation permitted')",
}
failures={
    'infinite loop': "def choose_move(o):\n    while True: pass",
    'wall timeout': "import time\ndef choose_move(o):\n    time.sleep(30)\n    "+move,
    'slow import': "import time\ntime.sleep(30)\ndef choose_move(o):\n    "+move,
    'memory limit': "def choose_move(o):\n    a=bytearray(1024*1024*1024)\n    "+move,
    'log flood': "import sys\ndef choose_move(o):\n    sys.stderr.write('x'*5_000_000)\n    "+move,
    'invalid move': "def choose_move(o): return {'from':[0,0],'to':[9,9]}",
    'syntax error': "def choose_move(o)\n    pass",
    'exit 125 is a bot fault': "import os\ndef choose_move(o): os._exit(125)",
}
for name,source in success.items():
    executor.choose(source,obs)
    print('PASS',name,flush=True)
bot=executor.spawn("calls=0\ndef choose_move(o):\n    global calls\n    calls+=1\n    m=o['legalMoves'][calls-1]\n    return {'from':m['from'],'to':m['to']}", 1)
try:
    first,_=bot.move(obs,LIMITS['game_seconds'])
    following,_=bot.move(second,LIMITS['game_seconds'])
    assert following==({k:second['legalMoves'][1][k] for k in ('from','to')}), 'globals did not persist'
    print('PASS one sandbox serves every move of a game',flush=True)
finally:
    bot.close()
for name,source in failures.items():
    try:
        executor.choose(source,obs)
    except BotFault:
        print('PASS',name,flush=True)
    else:
        raise AssertionError(name+' was not rejected')
print('Sandbox smoke passed. Mode:',args.mode)
