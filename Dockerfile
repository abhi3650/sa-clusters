FROM mysterydemon/botclusters:latest

WORKDIR /app
COPY install.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/install.sh
RUN /usr/local/bin/install.sh

# Install Docker CLI — base image is Fedora so we use dnf
# Docker's official Fedora repo ships docker-ce-cli for all Fedora versions
RUN dnf install -y dnf-plugins-core && \
    dnf config-manager --add-repo https://download.docker.com/linux/fedora/docker-ce.repo && \
    dnf install -y docker-ce-cli --nobest --allowerasing && \
    dnf clean all

COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt
COPY . .

EXPOSE 5000

# Mount the Docker socket at runtime so BotClusters can build & manage containers:
#   docker run -v /var/run/docker.sock:/var/run/docker.sock ...
CMD ["python3", "cluster.py"]
