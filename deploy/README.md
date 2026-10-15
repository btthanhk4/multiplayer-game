# Docker deployment on an existing host

This Compose project uses its own `ottv2` project name, image, container and named data volume. The API listens on host loopback port 8091 by default, so it does not take ports from other services. `sample-worker` is for internal testing with the three unchanged example bots only.

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

The public runner requires Docker with gVisor (`runsc`) and separate operational review. A public origin must use HTTPS; the sample worker deliberately refuses public origins. The host's existing Docker daemon and containers are not changed by this Compose file.
