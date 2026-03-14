

#Install Meilisearch

The easiest way of running Meilisearch is probably as a Docker container, but if you prefer not to run it inside a container, here is how to:

## Create meilisearch system user and group
```sh
# Create a dedicated meilisearch user to run the service
sudo useradd -r -s /usr/sbin/nologin -d /var/lib/meilisearch meilisearch
# Create folders for data, binary and config in suitable folders
sudo mkdir -p /var/lib/meilisearch /opt/meilisearch /etc/meilisearch
# Make those folders owned by the dedicated meilisearch user
sudo chown -R meilisearch:meilisearch /var/lib/meilisearch /opt/meilisearch /etc/meilisearch

```
## Download Meilisearch binary 
Download Meilisearch binary (example v1.31.0 — update as needed)
```sh
# Download meilisearch binary 
sudo wget -O /opt/meilisearch/meilisearch https://github.com/meilisearch/MeiliSearch/releases/download/v1.31.0/meilisearch-linux-amd64
# Make the binary executable
sudo chmod 755 /opt/meilisearch/meilisearch
# Make meilisearch system user the owner
sudo chown meilisearch:meilisearch /opt/meilisearch/meilisearch
```

## Create a master key
Meilisearch recommends a 32-character master key. Use a secure, URL-safe 32-char secret, e.g.:
```sh
N=32; tr -dc 'A-Za-z0-9._~-' < /dev/urandom | head -c "$N"; echo
```
(That produces 32 URL-safe chars; store it for later use!)

## Create environment file (set master key)
Exchange your_master_key with the key you just generated.

```sh
sudo tee /etc/meilisearch/meilisearch.env > /dev/null <<'ENV'
MEILI_MASTER_KEY=your_master_key
MEILI_ENV=production
ENV
sudo chmod 640 /etc/meilisearch/meilisearch.env
sudo chown meilisearch:meilisearch /etc/meilisearch/meilisearch.env
```
## Create systemd service file
Change the 0.0.0.0:7700 to a fixed IP if you want to run the service on a fixed ip adress rather than "all" IP addresses.

```sh
sudo tee /etc/systemd/system/meilisearch.service > /dev/null <<'SERVICE'
[Unit]
Description=Meilisearch
After=network.target

[Service]
Type=simple
User=meilisearch
Group=meilisearch
WorkingDirectory=/var/lib/meilisearch
EnvironmentFile=/etc/meilisearch/meilisearch.env
ExecStart=/opt/meilisearch/meilisearch --db-path /var/lib/meilisearch --http-addr 0.0.0.0:7700
Restart=on-failure
LimitNOFILE=65536

[Install]
WantedBy=multi-user.target
SERVICE
```
## Enable and start the service
```sh
sudo systemctl daemon-reload
sudo systemctl enable --now meilisearch.service
```
## Verify the service
```sh
sudo systemctl status meilisearch.service
sudo journalctl -u meilisearch.service -f
```