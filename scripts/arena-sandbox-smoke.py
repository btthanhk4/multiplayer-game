"""Run only against a locally built sandbox image. Never executes test code on the host."""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from arena_backend.runner import Executor, Referee, observation, BotFault
from arena_backend.store import ROOT

parser=argparse.ArgumentParser()
parser.add_argument('--mode', choices=['docker','docker-dev'],default='docker')
args=parser.parse_args()
executor=Executor(args.mode)
executor.preflight()
referee=Referee()
try:
    obs=observation(referee.call('new'), 100)
finally:
    referee.close()

move="return {'from': o['legalMoves'][0]['from'], 'to': o['legalMoves'][0]['to']}"
success={
    'sample': (ROOT/'sdk/examples/hunter.py').read_text(encoding='utf-8'),
    'large UTF-8 source packet': '#'+('ế'*18000)+'\ndef choose_move(o):\n    '+move,
    'network denied': "import socket\ndef choose_move(o):\n    s=socket.socket()\n    s.settimeout(.3)\n    try:\n        s.connect(('1.1.1.1',443))\n    except OSError:\n        "+move+"\n    raise RuntimeError('Network accessible')",
    'read-only filesystem': "def choose_move(o):\n    try:\n        open('/opt/ott/write-test','w')\n    except OSError:\n        "+move+"\n    raise RuntimeError('Filesystem writable')",
    'child process denied': "import subprocess\ndef choose_move(o):\n    try:\n        subprocess.run(['python','-c','print(1)'],check=True)\n    except OSError:\n        "+move+"\n    raise RuntimeError('Process creation permitted')",
}
failures={
    'infinite loop': "def choose_move(o):\n    while True: pass",
    'wall timeout': "import time\ndef choose_move(o):\n    time.sleep(30)\n    "+move,
    'memory limit': "def choose_move(o):\n    a=bytearray(1024*1024*1024)\n    "+move,
    'output flood': "def choose_move(o):\n    print('x'*100000)\n    "+move,
    'invalid move': "def choose_move(o): return {'from':[0,0],'to':[9,9]}",
    'exit 125 is a bot fault': "import os\ndef choose_move(o): os._exit(125)",
}
for name,source in success.items():
    executor.choose(source,obs)
    print('PASS',name,flush=True)
for name,source in failures.items():
    try:
        executor.choose(source,obs)
    except BotFault:
        print('PASS',name,flush=True)
    else:
        raise AssertionError(name+' was not rejected')
print('Sandbox smoke passed. Mode:',args.mode)
