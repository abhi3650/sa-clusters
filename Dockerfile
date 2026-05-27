FROM mysterydemon/botclusters:latest

WORKDIR /app
COPY install.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/install.sh
RUN /usr/local/bin/install.sh

COPY requirements.txt ./
RUN echo "supervisor" >> requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Ensure persistent data dirs exist
RUN mkdir -p /app /var/log/supervisor /etc/supervisor/conf.d

COPY . .

EXPOSE 5000
CMD ["python3", "cluster.py"]
