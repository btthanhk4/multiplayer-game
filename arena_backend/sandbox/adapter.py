"""Runs INSIDE the isolated container; never import submitted code on the API host.

Protocol 2 (one line of JSON per message):
  adapter -> OTT_READY
  runner  -> {"source": ..., "seed": ...}
  adapter -> {"ready": true}
  runner  -> observation        (repeated for every move of this bot)
  adapter -> {"move": {...}} | {"error": "..."}
"""
import json
import os
import random
import sys
import traceback

# Apply after the container runtime has started Python. Applying NPROC=1 to
# Docker's init process can prevent the initial exec on Linux hosts.
if sys.platform == 'linux':
    import resource
    resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))

# Keep the protocol on a private copy of stdout; print() from the bot goes to stderr.
protocol = os.fdopen(os.dup(1), 'w', encoding='utf-8')
os.dup2(2, 1)
sys.stdout = sys.stderr
reader = sys.stdin.buffer


def send(message):
    protocol.write(json.dumps(message, allow_nan=False, separators=(',', ':')) + '\n')
    protocol.flush()


def fail(prefix, error):
    traceback.print_exc(limit=5)
    sys.stderr.flush()
    send({'error': f'{prefix}: {type(error).__name__}: {str(error)[:200]}'})
    sys.exit(1)


protocol.write('OTT_READY\n')
protocol.flush()
packet = json.loads(reader.readline(512_000))
random.seed(packet['seed'])
namespace = {'__name__': 'ott_bot'}
# Isolation is provided by the container runtime, NOT by filtering Python builtins.
try:
    exec(compile(packet['source'], 'bot.py', 'exec'), namespace)
    choose_move = namespace['choose_move']
    if not callable(choose_move):
        raise TypeError('choose_move không phải hàm')
except BaseException as error:  # noqa: BLE001 - report every load failure to the runner
    fail('Không nạp được bot', error)
send({'ready': True})

while True:
    line = reader.readline(512_000)
    if not line:
        break
    observation = json.loads(line)
    try:
        move = choose_move(observation)
    except BaseException as error:  # noqa: BLE001
        fail('Bot lỗi thực thi', error)
    try:
        send({'move': move})
    except (TypeError, ValueError) as error:
        fail('Kết quả không phải JSON', error)
