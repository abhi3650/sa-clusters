FROM mysterydemon/botclusters:latest

WORKDIR /app
COPY install.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/install.sh
RUN /usr/local/bin/install.sh

# Install Docker CLI
# Base image uses Fedora with DNF5, so we add the repo via curl + dnf5 config-manager
RUN curl -fsSL https://download.docker.com/linux/fedora/docker-ce.repo \
        -o /etc/yum.repos.d/docker-ce.repo && \
    dnf install -y docker-ce-cli --nobest --allowerasing && \
    dnf clean all

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 5000

# Mount the Docker socket at runtime:
#   docker run -v /var/run/docker.sock:/var/run/docker.sock ...
CMD ["python3", "cluster.py"]
