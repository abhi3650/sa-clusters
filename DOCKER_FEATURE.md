# BotClusters — Docker Deployment & Web UI Proxy

## What's new

### 1. Dockerfile-based bot deployment
You can now deploy any bot that ships a `Dockerfile` directly from the BotClusters dashboard.

**How it works**
1. BotClusters clones the repo, builds the Docker image, creates the container, and registers it with supervisord — all from the UI.
2. Supervisord manages the container lifecycle (`docker start -a <name>`) with auto-restart on crash.
3. Stopping/restarting a bot from the dashboard also stops/restarts the underlying Docker container.

**UI**
Click the 🐳 **Deploy Docker** button in the header, fill in the form:

| Field | Required | Description |
|---|---|---|
| Process Name | ✅ | Must end with `bot1`, `bot2`, etc. (e.g. `mybot bot3`) |
| Git URL | ✅ | Public or private repo containing a Dockerfile |
| Branch | | Default: `main` |
| Web Port | | Internal port your app listens on (enables `/botN` proxy) |
| Env Vars | | JSON object of environment variables passed to the container |
| Build Args | | JSON object of `--build-arg` values for the Docker build |

---

### 2. `/botN` reverse proxy
If a Docker bot exposes a web interface (e.g. a dashboard, status page, or web app), set the **Web Port** during deployment.

BotClusters will:
- Map that port to a random host port automatically
- Proxy all requests to `/bot1`, `/bot2`, etc. through Flask

The bot card on the dashboard will show a **🌐 Web UI** button that opens the proxied interface in a new tab.

---

### 3. Docker-in-Docker setup
To use Docker deployment, mount the host Docker socket when running BotClusters:

```bash
docker run -d \
  -p 5000:5000 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -e CLUSTER1='["my prefix bot1","https://github.com/user/repo","main","app.py",{"TOKEN":"abc"},null]' \
  your-botclusters-image
```

---

### Config format (existing git bots — unchanged)
```json
{
  "clusters": [
    { "name": "CLUSTER1" },
    { "name": "CLUSTER2" }
  ]
}
```
Set each cluster's env var to a JSON array:
```
["bot_number", "git_url", "branch", "run_command", {env_dict}, "python_version"]
```

### Config format (new Docker bots — via UI or API)
POST to `/docker/deploy`:
```json
{
  "process_name": "my prefix bot2",
  "git_url": "https://github.com/user/dockerbot",
  "branch": "main",
  "web_port": 8080,
  "env": { "TOKEN": "secret" },
  "build_args": {}
}
```
