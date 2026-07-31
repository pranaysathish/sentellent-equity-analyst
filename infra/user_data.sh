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

mkdir -p /opt/sentellent /var/www/html
cd /opt/sentellent

if [ ! -f /var/www/html/index.html ]; then
  echo '<!doctype html><meta charset="utf-8"><title>Sentellent</title>
<body style="font-family:system-ui;background:#0b0f14;color:#e6edf3;display:grid;place-items:center;height:100vh;margin:0">
<div style="text-align:center"><h1>Sentellent Equity Analyst</h1>
<p style="color:#8b98a9">Deploying — the application will appear here shortly.</p></div>'     > /var/www/html/index.html
fi

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
  api:
    image: $ECR_REPOSITORY:latest
    restart: unless-stopped
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
      - /var/www/html:/usr/share/nginx/html:ro
COMPOSE

# nginx exists so that the origin has one stable port and can answer health
# checks even while the API container is restarting during a deploy.
cat > /opt/sentellent/nginx.conf <<'NGINX'
# The origin guard is evaluated once at server level rather than inside each
# location. nginx's own documentation warns that `if` inside a location is
# unpredictable when combined with `try_files` — the two are processed in
# different phases, and the documented-safe uses of `if` are only `return` and
# `rewrite ... last`. A server-level `if` with `return` avoids that interaction
# entirely and is evaluated before any location is selected.
# The origin token is a 48-character random string, which overflows nginx's
# default map hash bucket and makes it refuse to start with
# "could not build map_hash". The bucket has to be sized for the longest key.
map_hash_bucket_size 128;

map $http_x_origin_token $origin_allowed {
    default              0;
    "ORIGIN_TOKEN_PLACEHOLDER" 1;
}

server {
    listen 80 default_server;
    server_name _;

    client_max_body_size 2m;
    root /usr/share/nginx/html;
    index index.html;

    # Compression is left off. API Gateway proxies the body through without
    # re-negotiating Content-Encoding, and a mismatch there surfaces in the
    # browser as an undiagnosable "page couldn't load" while curl succeeds.
    gzip off;

    # Everything reaching this instance should have arrived through API
    # Gateway, which stamps this header. A request sent straight to the public
    # IP carries no such header and is refused. The security group cannot
    # express this, because API Gateway's egress addresses are not a fixed range.
    if ($origin_allowed = 0) {
        return 403;
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

    # Hashed asset filenames are immutable and safe to cache hard.
    location /_next/static/ {
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # Static export: /dashboard/ is dashboard/index.html on disk. Falling back
    # to /index.html keeps client-side routes working.
    location / {
        try_files $uri $uri/index.html $uri.html /index.html;
    }
}
NGINX

# The token is templated in by Terraform rather than written into the file
# above, so the literal secret never appears in a heredoc that also contains
# nginx variables.
sed -i "s|ORIGIN_TOKEN_PLACEHOLDER|${origin_token}|g" /opt/sentellent/nginx.conf

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

# Refresh the static frontend that CI published to S3. Kept in the same
# script so one SSM call deploys both halves atomically enough for a
# single-instance deployment.
mkdir -p /var/www/html
if aws s3 ls "s3://${frontend_bucket}/frontend/" --region "$AWS_REGION" >/dev/null 2>&1; then
  aws s3 sync "s3://${frontend_bucket}/frontend/" /var/www/html/     --region "$AWS_REGION" --delete
  chmod -R a+rX /var/www/html
  docker compose exec -T edge nginx -s reload 2>/dev/null || true
else
  echo "no frontend build in S3 yet; skipping"
fi
DEPLOY
chmod +x /opt/sentellent/deploy.sh

# The very first boot races CI: the ECR image may not exist yet. Failing here
# would leave the box half-configured, so a miss is tolerated and the next
# deploy picks it up.
# On a first boot the ECR image does not exist yet, so the full deploy will
# fail. That must not leave the host with nothing listening on port 80 — the
# public URL would return 503 instead of a holding page, and there would be no
# way to tell "still deploying" apart from "broken". Bring up the database and
# the edge regardless; CI supplies the API container moments later.
if ! /opt/sentellent/deploy.sh; then
  echo "initial deploy incomplete (API image not published yet)"
  docker compose up -d edge || true
fi

# --------------------------------------------------------------------------- #
# Scheduled news + sentiment refresh.
# --------------------------------------------------------------------------- #
cat > /opt/sentellent/refresh.sh <<'REFRESH'
#!/bin/bash
# Scheduled news and sentiment refresh.
#
# Talks to the API container directly rather than through nginx on port 80.
# nginx only accepts requests carrying the origin token that API Gateway
# stamps, and a local cron job has no such header — routing this through the
# edge returned 403 and silently disabled the scheduled refresh.
set -euo pipefail
cd /opt/sentellent

TOKEN="$(grep '^INTERNAL_REFRESH_TOKEN=' /opt/sentellent/.env | cut -d= -f2-)"

docker compose exec -T api   curl -fsS -X POST http://localhost:8000/api/internal/refresh   -H "x-internal-token: $TOKEN"   --max-time 900   | logger -t sentellent-refresh
REFRESH
chmod +x /opt/sentellent/refresh.sh

dnf install -y cronie
systemctl enable --now crond
echo '${news_refresh_cron} root /opt/sentellent/refresh.sh' > /etc/cron.d/sentellent-refresh
chmod 644 /etc/cron.d/sentellent-refresh

echo "bootstrap complete"
