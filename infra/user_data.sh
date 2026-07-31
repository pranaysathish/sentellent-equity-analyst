#!/bin/bash
# Bootstraps the application host on first boot.
#
# Everything here is idempotent, because `user_data_replace_on_change` means
# this can run again on a fresh instance and must converge to the same state.
set -euxo pipefail

exec > >(tee /var/log/sentellent-bootstrap.log | logger -t sentellent -s 2>/dev/console) 2>&1

AWS_REGION="${aws_region}"
PROJECT="${project}"
ECR_REPOSITORY="${ecr_repository}"

# --------------------------------------------------------------------------- #
# Swap. A t3.micro has 1 GiB of RAM; ingestion briefly spikes when embedding a
# batch of articles. Swap turns a would-be OOM kill into a slow moment.
# --------------------------------------------------------------------------- #
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -w vm.swappiness=10

# --------------------------------------------------------------------------- #
# Docker
# --------------------------------------------------------------------------- #
dnf update -y
dnf install -y docker jq
systemctl enable --now docker
usermod -aG docker ec2-user

DOCKER_CLI_PLUGINS=/usr/local/lib/docker/cli-plugins
mkdir -p "$DOCKER_CLI_PLUGINS"
if [ ! -x "$DOCKER_CLI_PLUGINS/docker-compose" ]; then
  curl -fsSL "https://github.com/docker/compose/releases/download/v2.32.4/docker-compose-linux-x86_64" \
    -o "$DOCKER_CLI_PLUGINS/docker-compose"
  chmod +x "$DOCKER_CLI_PLUGINS/docker-compose"
fi

mkdir -p /opt/sentellent
cd /opt/sentellent

# --------------------------------------------------------------------------- #
# Configuration is pulled from Parameter Store at boot rather than baked into
# the AMI or the image, so rotating a secret is a parameter update plus a
# restart — no rebuild, and no secret ever lands in the image layers.
# --------------------------------------------------------------------------- #
cat > /opt/sentellent/load-env.sh <<'LOADENV'
#!/bin/bash
set -euo pipefail
REGION="$1"
PROJECT="$2"
OUT=/opt/sentellent/.env

umask 077
: > "$OUT"
aws ssm get-parameters-by-path \
  --region "$REGION" \
  --path "/$PROJECT" \
  --with-decryption \
  --query 'Parameters[].[Name,Value]' \
  --output text \
| while IFS=$'\t' read -r name value; do
    key="$(basename "$name")"
    printf '%s=%s\n' "$key" "$value" >> "$OUT"
  done
chmod 600 "$OUT"
LOADENV
chmod +x /opt/sentellent/load-env.sh
/opt/sentellent/load-env.sh "$AWS_REGION" "$PROJECT"

# --------------------------------------------------------------------------- #
# Compose stack: Postgres with pgvector, the API, and nginx as the edge.
# --------------------------------------------------------------------------- #
cat > /opt/sentellent/docker-compose.yml <<COMPOSE
name: sentellent

services:
  db:
    image: pgvector/pgvector:pg16
    restart: unless-stopped
    environment:
      POSTGRES_USER: sentellent
      POSTGRES_DB: sentellent
      POSTGRES_PASSWORD: \$${POSTGRES_PASSWORD}
    volumes:
      - pgdata:/var/lib/postgresql/data
    # Tuned down for a 1 GiB host: the defaults assume far more memory and
    # will have the kernel reap Postgres under load.
    command: >
      postgres
      -c shared_buffers=128MB
      -c work_mem=4MB
      -c maintenance_work_mem=64MB
      -c max_connections=25
      -c effective_cache_size=384MB
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U sentellent -d sentellent"]
      interval: 10s
      timeout: 5s
      retries: 10

  api:
    image: $ECR_REPOSITORY:latest
    restart: unless-stopped
    depends_on:
      db:
        condition: service_healthy
    env_file: /opt/sentellent/.env
    environment:
      DB_POOL_MAX: "8"
    healthcheck:
      test: ["CMD-SHELL", "curl -fsS http://localhost:8000/api/health || exit 1"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 40s
    logging:
      driver: awslogs
      options:
        awslogs-region: $AWS_REGION
        awslogs-group: ${log_group}
        awslogs-stream: api

  edge:
    image: nginx:1.27-alpine
    restart: unless-stopped
    depends_on:
      - api
    ports:
      - "80:80"
    volumes:
      - /opt/sentellent/nginx.conf:/etc/nginx/conf.d/default.conf:ro

volumes:
  pgdata:
COMPOSE

# nginx exists so that the origin has one stable port and can answer health
# checks even while the API container is restarting during a deploy.
cat > /opt/sentellent/nginx.conf <<'NGINX'
server {
    listen 80 default_server;
    server_name _;

    client_max_body_size 2m;

    # CloudFront checks this; it must not depend on the API being up.
    location = /edge-health {
        access_log off;
        return 200 "edge ok\n";
    }

    location /api/ {
        proxy_pass         http://api:8000;
        proxy_http_version 1.1;
        proxy_set_header   Host              $host;
        proxy_set_header   X-Real-IP         $remote_addr;
        proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto https;

        # Ingestion and long agent turns can legitimately take a while.
        proxy_connect_timeout 10s;
        proxy_read_timeout    120s;
        proxy_send_timeout    120s;
    }

    location / {
        return 404 '{"detail":"Not found. The UI is served by CloudFront."}';
        add_header Content-Type application/json always;
    }
}
NGINX

# --------------------------------------------------------------------------- #
# Deploy helper. Also invoked by CI through SSM Run Command.
# --------------------------------------------------------------------------- #
cat > /opt/sentellent/deploy.sh <<DEPLOY
#!/bin/bash
set -euo pipefail
cd /opt/sentellent

# Refresh config first: a deploy may follow a secret or URL change.
/opt/sentellent/load-env.sh "$AWS_REGION" "$PROJECT"

aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "\$(echo "$ECR_REPOSITORY" | cut -d/ -f1)"

docker compose pull
docker compose up -d --remove-orphans
docker image prune -f
DEPLOY
chmod +x /opt/sentellent/deploy.sh

# The very first boot races CI: the ECR image may not exist yet. Failing here
# would leave the box half-configured, so a miss is tolerated and the next
# deploy picks it up.
/opt/sentellent/deploy.sh || echo "initial deploy skipped: image not published yet"

# --------------------------------------------------------------------------- #
# Scheduled news + sentiment refresh.
# --------------------------------------------------------------------------- #
cat > /opt/sentellent/refresh.sh <<'REFRESH'
#!/bin/bash
set -euo pipefail
TOKEN="$(grep '^INTERNAL_REFRESH_TOKEN=' /opt/sentellent/.env | cut -d= -f2-)"
curl -fsS -X POST http://localhost/api/internal/refresh \
  -H "x-internal-token: $TOKEN" \
  --max-time 900 \
  | logger -t sentellent-refresh
REFRESH
chmod +x /opt/sentellent/refresh.sh

dnf install -y cronie
systemctl enable --now crond
echo '${news_refresh_cron} root /opt/sentellent/refresh.sh' > /etc/cron.d/sentellent-refresh
chmod 644 /etc/cron.d/sentellent-refresh

echo "bootstrap complete"
