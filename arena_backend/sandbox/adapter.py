"""Runs INSIDE the isolated container; never import submitted code on the API host."""
import json
import random
import sys

# Apply after the container runtime has started Python. Applying NPROC=1 to
# Docker's init process can prevent the initial exec on Linux hosts.
if sys.platform == 'linux':
    import resource
    resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))

print('OTT_READY', flush=True)
packet = json.loads(sys.stdin.buffer.readline(512_000))
random.seed(packet['observation']['seed'])
namespace = {'__name__': 'ott_bot'}
# Isolation is provided by the container runtime, NOT by filtering Python builtins.
exec(compile(packet['source'], 'bot.py', 'exec'), namespace)
move = namespace['choose_move'](packet['observation'])
print(json.dumps(move, allow_nan=False, separators=(',', ':')), flush=True)
