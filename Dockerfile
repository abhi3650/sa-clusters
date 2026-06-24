FROM mysterydemon/botclusters:latest

WORKDIR /app
COPY install.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/install.sh
RUN /usr/local/bin/install.sh

# Install Docker CLI so BotClusters can build & run Docker bots
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg lsb-release && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://download.docker.com/linux/debian/gpg | \
        gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
        https://download.docker.com/linux/debian $(lsb_release -cs) stable" \
        > /etc/apt/sources.list.d/docker.list && \
    apt-get update && apt-get install -y --no-install-recommends docker-ce-cli && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 5000

# Mount the Docker socket at runtime:
#   docker run -v /var/run/docker.sock:/var/run/docker.sock ...
CMD ["python3", "cluster.py"]
