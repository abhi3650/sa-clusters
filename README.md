## 🎓 BotClusters v8.0 — Rebuilt

Run **multiple Telegram (or any Python) bots** in one Docker instance, each with its own supervisor process. Bots that expose a web UI get a reverse-proxied panel at `/bot1`, `/bot2`, etc.

---

## ✨ What's New in v8

| Feature | Description |
|---|---|
| **Add Bot** | Add any bot at runtime via the dashboard — no restart needed |
| **Remove Bot** | Stop, unregister, and delete a bot's files in one click |
| **Pause / Resume** | Freeze a running process with SIGSTOP / SIGCONT |
| **Web Panel Proxy** | Any bot with a web UI is proxied at `/botN` |
| **`/` dashboard** | BotClusters panel always at the root URL |
| **`ADMIN_PASSWORD` env** | Set your own login password via environment variable |

---

## 🗂 URL Layout

| URL | What it shows |
|---|---|
| `/` | BotClusters dashboard (login required) |
| `/bot1` | Web panel of bot 1 (proxied, login required) |
| `/bot2` | Web panel of bot 2 … |
| `/logstream` | Live log stream |

---

## 🚀 Deploy to Render

1. Fork this repo
2. In Render → **New → Blueprint** → connect your fork
3. Add env vars:

| Key | Value |
|---|---|
| `CLUSTER_01` | `["mybot","https://github.com/you/bot.git","main","bot.py",{"TOKEN":"xxx"}]` |
| `ADMIN_PASSWORD` | your chosen password |
| `APP_URL` | your Render service URL (for keep-alive pings) |

> Add a **Disk** at `/app` (1 GB) so panel registrations and dynamic bots survive restarts.

---

## 🚀 Deploy to Koyeb

1. Fork this repo
2. Koyeb → **Create App → GitHub** → your fork, branch `main`, builder **Dockerfile**, port **5000**
3. Add the same env vars above (Koyeb doesn't need a disk — `/app` persists)

---

## ➕ Adding a Bot at Runtime

Click **Add Bot** in the dashboard and fill in:

| Field | Example |
|---|---|
| Bot Name | `myalertbot` |
| Git URL | `https://github.com/you/alertbot.git` |
| Branch | `main` |
| Run Command | `bot.py` |
| Env Variables | `TOKEN=xxx`, `API_ID=123` |
| Python Version | `3.11` (optional) |
| Panel Port | `8080` (optional — if your bot has a web UI) |

The bot is cloned, dependencies installed, supervisord config written, and the process started — all live, no redeploy needed.

---

## 📝 `CLUSTER_XX` Format (startup bots)

```
["botname", "git_url", "branch", "run_command", {"ENV_KEY": "value"}, "python_version"]
```

---

## 🔐 Login

Default credentials (override with `ADMIN_PASSWORD` env var):

- **Username:** `admin`
- **Password:** `password123`
