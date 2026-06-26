<div align="center">

# 🤖 BotClusters Enhanced

**Run, manage, monitor and deploy unlimited Telegram bots — all from one beautiful dashboard.**

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/abhi3650/sa-clusters)
[![Deploy to Koyeb](https://www.koyeb.com/static/images/deploy/button.svg)](https://app.koyeb.com/deploy?type=git&builder=dockerfile&repository=github.com/abhi3650/sa-clusters&branch=main&name=botclusters&ports=5000;http;/)
[![Deploy to Heroku](https://www.herokucdn.com/deploy/button.svg)](https://dashboard.heroku.com/new?template=https://github.com/abhi3650/sa-clusters)

</div>

---

## ✨ What is BotClusters Enhanced?

BotClusters Enhanced is a **self-hosted bot management platform** that lets you deploy, monitor, and control unlimited Python bots from a single web dashboard — no environment variables required for each bot.

Built on top of [MysteryDemon/BotClusters](https://github.com/MysteryDemon/BotClusters) with a complete feature overhaul including Docker/Podman deployment, real-time metrics, crash alerting, Git webhooks, and much more.

---

## 🚀 Features

### 🖥️ Dashboard
- **Real-time bot status** — live updates via WebSocket, no page refresh needed
- **Search & filter** — find bots by name, filter by Running / Stopped / Paused / Docker
- **Stats bar** — see total online, offline, paused counts at a glance
- **Dark themed UI** — clean, modern design with status indicators

### ➕ Bot Deployment (No env vars needed)
Deploy bots directly from the UI — no `CLUSTER1`, `CLUSTER2` env vars required. All configs are stored in `bot_registry.json` and survive restarts.

| Deploy Type | How it works |
|---|---|
| **🐍 Git / Python** | Clone any public or private repo, install deps, run with a custom command |
| **🐳 Dockerfile** | Build and run any repo with a Dockerfile using Podman (works without Docker daemon) |
| **📦 ZIP Upload** | Drag & drop a `.zip` of your bot source — no git URL needed |

### 🎮 Bot Controls
Every bot card has:
- ▶ **Start** / ⏹ **Stop** / ↺ **Restart** — with confirmation dialogs
- ⏸ **Pause** / ▶ **Resume** — SIGSTOP/SIGCONT without killing the process
- 🗑 **Delete** — fully removes the bot, its config, directory, and logs
- 📋 **Logs** — live streaming log viewer with search, filter, and auto-scroll
- 📊 **Metrics** — CPU %, RAM usage charts and uptime history

### 📊 Metrics & Monitoring
- **Live CPU & RAM per bot** — sampled every 60s using psutil, shown on cards and in charts
- **Uptime history** — 7-day hourly bar (green = online, red = crashed, gray = stopped)
- **Restart count tracker** — tracks how many times each bot has auto-restarted
- **Crash alerting** — Telegram and Discord notifications when a bot crashes or recovers

### 🔧 Runtime Controls
- **⌨ Live stdin injection** — send commands directly to a running bot's stdin
- **🔧 Env var editor** — edit environment variables live and hot-reload without a full redeploy
- **🏥 Health check URL** — ping any HTTP endpoint; auto-restart after 3 consecutive failures
- **🚦 Restart rate limiter** — cap restarts to N per hour to stop crash loops

### 🚀 Deployment Features
- **🔗 Git webhook** — auto-deploy on every `git push` to GitHub or GitLab
- **⏪ Rollback** — one-click rollback to any of the last 10 git commits
- **⏰ Scheduled deploys** — auto git pull + restart on a configurable interval (e.g. every 6h)
- **📦 ZIP deploy** — upload source code directly, no git required

### 📤 Export & Import
- **Export** — download all bot configs as a JSON snapshot
- **Import** — restore bots from a backup JSON file

### 🔔 Alert Channels
Configure in the **🔔 Crash Alerts** menu:
- **Telegram** — bot token + chat ID
- **Discord** — webhook URL
- **Test button** — verify your setup instantly

---

## ⚡ Quick Deploy

### Render
1. Fork this repo to your GitHub account
2. Click **Deploy to Render** above
3. Set `UPSTREAM_REPO` to your fork URL (optional — leave empty to run as-is)
4. Deploy — the dashboard will be live in ~2 minutes

### Koyeb
1. Fork this repo
2. Click **Deploy to Koyeb** above
3. Set port to `5000`

### Manual (Docker/Podman)
```bash
docker run -d \
  -p 5000:5000 \
  -e ADMIN_USER=admin \
  -e ADMIN_PASS=yourpassword \
  ghcr.io/abhi3650/sa-clusters:latest
```

---

## 🔐 Login Credentials

Default credentials (change via env vars):

| Field | Default |
|---|---|
| Username | `admin` |
| Password | `password123` |

Set `ADMIN_USER` and `ADMIN_PASS` environment variables to override.

---

## 🐳 Docker Bot Deployment

BotClusters uses **Podman** as a Docker-compatible, daemonless container runtime. No `/var/run/docker.sock` needed.

To deploy a Dockerfile bot:
1. Click **+ Add Bot** → select **🐳 Dockerfile** tab
2. Fill in the bot name (must end with `bot1`, `bot2`, etc.), git URL, and branch
3. Optionally set a **Web Port** to expose the bot's web UI at `/bot1`, `/bot2`, etc.
4. Click **🚀 Deploy**

> **Note:** Podman uses the `vfs` storage driver on Render/Koyeb which works without `--privileged`. Builds are slightly slower than with overlay but fully functional.

---

## 🔗 Git Webhooks (Auto-deploy on push)

1. Click the **🔗** button on any bot card
2. Copy the webhook URL and secret token
3. Add to your repo: **Settings → Webhooks → Add webhook**
   - Content type: `application/json`
   - Paste the URL and secret
   - Select: "Just the push event"
4. Every push will now auto-pull and restart the bot

---

## 🏥 Health Checks

Configure a health check URL per bot:
- BotClusters pings the URL every N seconds (default: 30s)
- After **3 consecutive failures** → bot is auto-restarted + crash alert fires
- Cards show 🟢 Healthy / 🔴 Failing in real time

---

## ⏰ Scheduled Deployments

Set a bot to auto-redeploy on a schedule:
- Click the **⏰** button on a bot card
- Set interval in hours (minimum 15 min)
- BotClusters will `git pull` + restart automatically
- Next and last run timestamps shown in the modal

---

## 🌐 Web UI Proxy (`/botN`)

If a Docker bot exposes a web interface, set its **Web Port** during deployment. BotClusters will:
- Automatically assign a host port
- Proxy all requests through `/bot1`, `/bot2`, etc.
- Show a **🌐 Web UI** button on the bot card

---

## 📦 Custom Packages

Add custom system or pip packages to `install.sh`:

```bash
# System packages (DNF — Fedora base image)
dnf install -y ffmpeg

# Python packages
pip3 install some-package
```

---

## 🗂️ Bot Registry

All bots added via the UI are stored in `/app/bot_registry.json`. This file:
- Persists across restarts
- Is included in exports
- Can be restored via import

---

## 📝 Notes

- **No env var config needed** — add all bots from the dashboard UI
- **Supervisord** manages all bot processes with auto-restart
- **Podman** replaces Docker CLI — same commands, no daemon required
- **Logs** are auto-deleted every 24 hours to save disk space
- For private repos, include your token in the git URL:
  ```
  https://<username>:<token>@github.com/user/repo.git
  ```

---

## 🤝 Contributing

Contributions are welcome! Please submit a Pull Request.

---

## 📚 References

- Original project: [MysteryDemon/BotClusters](https://github.com/MysteryDemon/BotClusters)
- Source inspiration: [bipinkrish/MultiBots](https://github.com/bipinkrish/MultiBots)
