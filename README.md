# Mimic! — Leaderboard Server

A tiny, dependency-free, **self-hostable** leaderboard server for the *Chest
Chaser* game (SKG Jam 2026). One readable Python file, standard library only,
SQLite storage.

## Why this exists

This is the game's **Stop Killing Games end-of-life plan**. The game talks to a
leaderboard over a plain, open HTTP+JSON protocol, and the client lets you point
it at **any** server URL. So if the "official" leaderboard ever disappears:

- The game **keeps working** — it falls back to a local, on-device leaderboard.
- **Anyone can host this server** and keep a community leaderboard alive.

No lock-in, no proprietary backend, no death when a company loses interest. The
whole server is ~250 lines of stdlib Python you can read, audit, and run forever.

## Quick start (local)

Requires only Python 3.11+ (no `pip install`, no build step):

```sh
python3 server.py
# mimic-leaderboard listening on :8080 (db: leaderboard.db)
```

Try it:

```sh
curl localhost:8080/health
curl -X POST localhost:8080/submit \
  -d '{"name":"AAA","score":18100,"time_ms":161000,"reached_goal":true}'
curl "localhost:8080/leaderboard?sort=time"
```

## API

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/health` | Liveness check → `{"status":"ok",...}` |
| `GET` | `/leaderboard?sort=time&limit=20` | Top entries (see below) |
| `POST` | `/submit` | Add a run |

### `GET /leaderboard`

- `sort=time` *(default)* — **fastest run to the prize**, ascending. Only runs
  that reached the goal (`reached_goal=true`) qualify. This is the main board:
  it rewards *finishing fast*, so slow-grinding never tops it.
- `sort=score` — highest score, descending. Every run counts.
- `limit` — 1–100 (default 20).

Response:

```json
{
  "sort": "time",
  "count": 1,
  "entries": [
    {"rank": 1, "name": "AAA", "score": 18100, "time_ms": 161000,
     "reached_goal": true, "created_at": "2026-07-20T22:39:54+00:00"}
  ]
}
```

### `POST /submit`

Body (JSON):

```json
{"name": "AAA", "score": 18100, "time_ms": 161000, "reached_goal": true}
```

- `name` — string, trimmed/sanitized, capped at `MAX_NAME_LEN` (16).
- `score` — integer, `0 … 100000000`.
- `time_ms` — integer milliseconds, `1 … 86400000` (24h). Runs claiming
  `reached_goal` must also be at least `MIN_WIN_TIME_MS` (60s by default) —
  faster "wins" are rejected as implausible.
- `reached_goal` — bool; `true` means the run banked the prize (qualifies for the
  time board).

Response: `{"ok": true, "id": 42, "time_rank": 3, "score_rank": 5}`
(`time_rank` is `null` when `reached_goal` is false).

## Configuration (environment variables)

| Var | Default | Meaning |
|---|---|---|
| `PORT` | `8080` | Listen port (cloud hosts usually set this for you) |
| `DB_PATH` | `leaderboard.db` | SQLite file location |
| `GAME_KEY` | *(unset)* | If set, `POST /submit` must send matching `X-Game-Key` header |
| `CORS_ORIGIN` | `*` | Allowed browser origin (set to your game's URL to lock it down) |
| `RATE_LIMIT` | `20` | Max submits per client IP per window |
| `RATE_WINDOW` | `600` | Rate-limit window in seconds |
| `MAX_NAME_LEN` | `16` | Max characters kept from a name |
| `KEEP_PER_PLAYER` | `10` | Per-name retention: top-N kept on each board (see below) |
| `MIN_WIN_TIME_MS` | `60000` | Fastest believable prize win; faster `reached_goal` submits are rejected |

### Per-player retention

After every submit, the server prunes that player's rows down to the **union**
of their top-`KEEP_PER_PLAYER` scores and their top-`KEEP_PER_PLAYER` fastest
goal-reaching times — so one prolific player can't flood the boards, and the
database stays tiny forever. A just-submitted run that doesn't make either of
the player's personal top-10 cuts is pruned immediately; that's expected (the
returned `time_rank`/`score_rank` still tell the client where it *would* have
placed).

## Security — read this

This is a **community, trust-based** leaderboard, not a cheat-proof one. Scores
are submitted by the client, so a determined person *can* forge a submission.
The mitigations here are deliberately lightweight:

- **`GAME_KEY`** — a shared secret in the `X-Game-Key` header. Keeps out casual
  drive-by spam, but since it ships in the client it is **not** a real secret.
- **Rate limiting** — per-IP, in-memory, to blunt floods.
- **Sanity bounds & input validation** — no absurd values or malformed rows.

For a jam / small community this is the right amount of effort. If you need
stronger guarantees, add server-side replay validation or authenticated accounts.

## Deploy on a free Google Cloud `e2-micro` (always-free)

GCP's Always Free tier includes **one `e2-micro` VM** (in `us-central1`,
`us-west1`, or `us-east1`) plus a 30 GB standard disk — enough to run this
server **always-on for $0**, with a real persistent disk so SQLite survives
reboots. You'll add HTTPS yourself (browsers block an `https://` game from
calling an `http://` server), which is a one-time ~15-minute setup with Caddy.

> Always-free quotas and regions change over time — confirm current limits in the
> [GCP free tier docs](https://cloud.google.com/free/docs/free-cloud-features)
> before relying on $0. Watch egress (1 GB/mo free in North America); leaderboard
> JSON is tiny, so you won't get close.

### 1. Create the VM

- New project → **Compute Engine → VM instances → Create**.
- Machine type **`e2-micro`**, region one of the free ones above.
- Boot disk: Debian/Ubuntu, **standard** persistent disk ≤ 30 GB.
- Firewall: check **Allow HTTP** and **Allow HTTPS**.
- Reserve a **static external IP** (free while attached to a running instance)
  so your domain keeps pointing at it.

### 2. Install and run the server

SSH into the VM, then:

```sh
sudo apt-get update && sudo apt-get install -y git python3
sudo git clone https://github.com/lucaslizama/mimic-leaderboard.git /opt/mimic-leaderboard
sudo useradd --system --home /opt/mimic-leaderboard leaderboard || true
sudo mkdir -p /var/lib/mimic-leaderboard
sudo chown -R leaderboard: /var/lib/mimic-leaderboard /opt/mimic-leaderboard

# run it as a service (unit provided in deploy/)
sudo cp /opt/mimic-leaderboard/deploy/leaderboard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now leaderboard
curl localhost:8080/health   # -> {"status":"ok",...}
```

### 3. Add HTTPS with Caddy (automatic Let's Encrypt)

Point a domain's `A` record at your static IP, then:

```sh
sudo apt-get install -y caddy
sudo cp /opt/mimic-leaderboard/deploy/Caddyfile /etc/caddy/Caddyfile
sudo nano /etc/caddy/Caddyfile        # replace leaderboard.example.com with your domain
sudo systemctl restart caddy
```

Caddy fetches and renews a TLS cert automatically and reverse-proxies `:443` to
the server on `:8080`. Your leaderboard is now at `https://your-domain/`.

### 4. Point the game at it

In the game, set the leaderboard server URL to `https://your-domain` (see the
game's settings / leaderboard screen). Done.

## Other hosts

The server is host-agnostic — it's just a Python process that listens on `PORT`
and writes SQLite to `DB_PATH`. It runs anywhere Python does: another VPS, a
Raspberry Pi on your desk, a container platform, etc. Anything that gives it a
**persistent disk** (for the SQLite file) and **HTTPS** works. Platforms with
ephemeral disks (e.g. free serverless tiers) will lose scores on redeploy unless
you attach durable storage.

## License

MIT — see [LICENSE](LICENSE). Host it, fork it, keep it alive.
