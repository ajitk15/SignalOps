# Deploying SignalAIOps on a Hostinger VPS (Ubuntu)

From a fresh Ubuntu VPS to a running, HTTPS-served instance. About 20 minutes.

What you get: the app in Docker, with Node.js and the Claude Code CLI baked in
(the implementer agent shells out to it), a persistent data volume, and
automatic HTTPS via Caddy.

---

## 0. Before you start

- A Hostinger VPS running **Ubuntu 22.04 or 24.04**, and its IP address.
- SSH access (Hostinger shows the root password in hPanel, or set an SSH key).
- **Recommended:** a domain or subdomain with an **A record** pointing at the
  VPS IP — this is what lets Caddy issue a real certificate. You can deploy
  without one (see [Without a domain](#without-a-domain)), but HTTPS is what
  makes the production session cookie work.

---

## 1. Connect and update

```bash
ssh root@YOUR_VPS_IP
apt update && apt upgrade -y
```

## 2. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
```

That installs Docker Engine and the Compose plugin. Verify:

```bash
docker --version && docker compose version
```

## 3. Get the code

```bash
apt install -y git
git clone https://github.com/ajitk15/SignalAIOps.git
cd SignalAIOps
```

## 4. Configure

```bash
cp .env.production.example .env
```

Generate the two secrets and paste them in:

```bash
echo "SESSION: $(openssl rand -hex 32)"
echo "SECRET : $(openssl rand -hex 32)"
```

Then edit `.env`:

```bash
nano .env
```

Fill in, at minimum:

| Variable | Value |
|---|---|
| `SIGNALOPS_ADMIN_EMAIL` | your admin login |
| `SIGNALOPS_ADMIN_PASSWORD` | a long passphrase (≥10 chars) |
| `SIGNALOPS_SESSION_SECRET` | the first `openssl` value above |
| `SIGNALOPS_SECRET_KEY` | the second — **back this up**; losing it makes stored connection secrets unreadable |
| `ANTHROPIC_API_KEY` | from console.anthropic.com — without it, agents return placeholder text |

## 5. Point Caddy at your domain

```bash
nano Caddyfile
```

Replace `signalaiops.example.com` with your domain. Make sure its A record
already points at the VPS.

## 6. Open the firewall

Hostinger VPS images usually ship with `ufw`. Allow SSH and web:

```bash
ufw allow OpenSSH
ufw allow 80
ufw allow 443
ufw --force enable
```

## 7. Launch

```bash
docker compose up -d --build
```

The first build takes a few minutes (it installs Node and the Claude CLI).
When it finishes:

```bash
docker compose ps           # both services "running"/"healthy"
docker compose logs -f app  # watch it boot
```

You are looking for `administrator ... is configured from the environment`.
Then open **https://your-domain** and sign in with the admin credentials from
`.env`.

---

## Without a domain

If you have only an IP address, skip Caddy and expose the app directly. Two
edits:

1. In `docker-compose.yml`, under the `app` service, uncomment the `ports`
   block (`"8000:8000"`) and delete the whole `caddy` service.
2. In `.env`, set `SIGNALOPS_ENV=local`.

> **Why `local`?** In production mode the session cookie is marked `Secure` and
> browsers only send it over HTTPS. On plain `http://IP:8000` that cookie never
> comes back and login silently fails. `local` drops the `Secure` flag so it
> works over HTTP. It is fine for a private test box; put a domain and HTTPS in
> front of anything real.

Then `ufw allow 8000`, `docker compose up -d --build`, and open
`http://YOUR_VPS_IP:8000`.

---

## Day-two operations

**Update to the latest code**

```bash
cd SignalAIOps
git pull
docker compose up -d --build
```

Your data — the database, the encryption key, run checkpoints — lives in the
`signalops-data` volume and survives rebuilds.

**Back up** (do this before any risky change)

```bash
docker run --rm -v signalops_signalops-data:/data -v "$PWD":/backup \
  busybox tar czf /backup/signalops-backup-$(date +%F).tar.gz -C /data .
```

That archive contains `signalops.db` and `secret.key` — treat it as sensitive.

**Restore**

```bash
docker run --rm -v signalops_signalops-data:/data -v "$PWD":/backup \
  busybox tar xzf /backup/signalops-backup-YYYY-MM-DD.tar.gz -C /data
docker compose restart app
```

**Logs / restart / stop**

```bash
docker compose logs -f app
docker compose restart app
docker compose down          # stop (data volume is kept)
```

**Recover admin access** — edit `SIGNALOPS_ADMIN_PASSWORD` in `.env` and
`docker compose up -d` again. The admin is re-asserted from the environment on
every start.

---

## Troubleshooting

**`docker compose logs app` shows "No administrator exists and none is
configured"** — `SIGNALOPS_ADMIN_EMAIL`/`SIGNALOPS_ADMIN_PASSWORD` are missing
or the password is under 10 characters. Fix `.env` and `up -d` again.

**Login page loads but sign-in does nothing / bounces back** — almost always the
`Secure` cookie over plain HTTP. Either finish the HTTPS setup (domain + Caddy)
or set `SIGNALOPS_ENV=local` as above.

**Every agent result says "simulated"** — no `ANTHROPIC_API_KEY`. Add it to
`.env` and `docker compose restart app`.

**Caddy can't get a certificate** — the domain's A record must point at this
VPS and ports 80/443 must be open (`ufw status`). Check `docker compose logs
caddy`.

**A ticket-to-PR workflow can't reach GitHub** — that workflow needs a
`GIT_BOT_TOKEN` in `.env` for pushing branches; without it, runs stop at the
prepared-diff stage rather than opening a PR.
