# Docker deployment on an existing host

This Compose project uses its own `ottv2` project name, image, container and named data volume. The API listens on host loopback port 8091 by default, so it does not take ports from other services. `sample-worker` is for internal testing with the three unchanged example bots only.

When a remotely managed Cloudflare Tunnel is shared by connectors on different machines, its published application route must use an address reachable from every connector. Set `OTT_BIND_HOST` to this server's private LAN IP in `deploy/.env` and use `http://<LAN-IP>:8091` as the route's Service URL. Keep the port private to the LAN. Set `ARENA_ORIGIN` in `deploy/.env.server` to the exact public HTTPS origin before users sign in through Cloudflare.

```sh
git clone https://github.com/btthanhk4/multiplayer-game.git ~/ottv2
cd ~/ottv2
cp deploy/.env.example deploy/.env.server
```

Set `ARENA_ORIGIN=http://localhost:8091` in `deploy/.env.server` for a local pilot reached through an SSH tunnel, and replace the invitation code. Keep this file private (`chmod 600 deploy/.env.server`). This address is not a public production URL. For public access, first configure HTTPS and use its exact origin in this file.

```sh
docker compose -f deploy/compose.yaml build api
docker compose -f deploy/compose.yaml up -d api
docker compose -f deploy/compose.yaml ps
curl -fsS http://127.0.0.1:8091/api/health
```

Create an administrator interactively:

```sh
docker compose -f deploy/compose.yaml run --rm --no-deps api \
  python -m arena_backend admin admin
```

To test the three bundled bots without accepting arbitrary contestant code:

```sh
docker compose -f deploy/compose.yaml --profile demo up -d sample-worker
```

Open an SSH tunnel from a workstation with `ssh -L 8091:127.0.0.1:8091 USER@SERVER`, then visit `http://localhost:8091/arena/`. The database persists in the `ottv2_arena_data` Docker volume. Do not use `docker compose down -v` unless you intend to delete it.

The runner requires Docker with gVisor (`runsc`). Build the bot image, run the sandbox smoke test and start the runner only after `docker info` shows a `runsc` runtime:

```sh
docker build -t ottv2-bot:1 arena_backend/sandbox
docker run --rm --network none \
  --group-add "$(stat -c %g /var/run/docker.sock)" \
  -v /var/run/docker.sock:/var/run/docker.sock \
  --entrypoint python ottv2-arena-api:local \
  scripts/arena-sandbox-smoke.py --mode docker
OTT_DOCKER_GID="$(stat -c %g /var/run/docker.sock)" \
  docker compose -f deploy/compose.yaml --profile runner up -d runner
```

The runner service shares the private data volume, has no network and accesses the Docker socket through the host socket group. Access to that socket grants powerful control of the host; only the runner receives it. The Compose file leaves the existing services and their ports alone. A public origin must use HTTPS; the sample worker deliberately refuses public origins.
