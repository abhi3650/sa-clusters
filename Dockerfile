FROM mysterydemon/botclusters:latest

WORKDIR /app
COPY install.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/install.sh
RUN /usr/local/bin/install.sh

# ── Podman (Docker-compatible, daemonless, native Fedora package) ──────────
# Podman is in Fedora's default repos — no external repo needed.
# It's 100% CLI-compatible with Docker: same flags, same Dockerfile syntax.
# Daemonless = no /var/run/docker.sock required = works on Koyeb/Render/Railway.
RUN dnf install -y podman fuse-overlayfs --nobest && dnf clean all

# Shim: make `docker` call `podman` transparently.
# All our subprocess.run(['docker', ...]) calls work with zero code changes.
RUN printf '#!/bin/sh\nexec podman "$@"\n' > /usr/local/bin/docker && \
    chmod +x /usr/local/bin/docker

# Podman rootless storage config for container environments
RUN mkdir -p /etc/containers && \
    printf '[storage]\ndriver = "overlay"\nrunRoot = "/run/containers/storage"\ngraphRoot = "/var/lib/containers/storage"\n\n[storage.options.overlay]\nmount_program = "/usr/bin/fuse-overlayfs"\n' \
    > /etc/containers/storage.conf

COPY requirements.txt .
RUN pip3 install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 5000

# Note: For Docker bot deployment (building images from Dockerfiles),
# run with --privileged or --device /dev/fuse for fuse-overlayfs support:
#   docker run --privileged -p 5000:5000 your-image
# For git/zip bots only (no Docker bots), no special flags needed.
CMD ["python3", "cluster.py"]
