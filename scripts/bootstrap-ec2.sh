#!/usr/bin/env bash
# Bootstrap a fresh EC2 host as the Grafana + Prometheus + bedrock-api node.
#
# Idempotent — safe to re-run. Pulls provisioning/ from S3, substitutes
# environment-specific placeholders, writes docker-compose.yml + prometheus.yml,
# then starts the stack.
#
# Required env (sourced from /home/ec2-user/monitoring/.env or instance UserData):
#   PROVISIONING_BUCKET     S3 bucket containing provisioning/ (uploaded by deploy-provisioning.sh)
#   AWS_REGION              region for S3 + AWS calls (default ap-northeast-2)
#   GRAFANA_HOST            public hostname Grafana is reachable at (e.g. grafana.example.com)
#   API_GATEWAY_HOST        API Gateway hostname (e.g. abc123.execute-api.ap-northeast-2.amazonaws.com)
#   GF_ADMIN_PASSWORD       Grafana admin password
#   AGENTCORE_RUNTIME_ARN   optional, for the bedrock-api proxy
#   SMTP_HOST/USER/PASSWORD/FROM_ADDRESS  optional, for Grafana SMTP alerts
#   SLACK_WEBHOOK           optional
set -euo pipefail

M=/home/ec2-user/monitoring
ENV_FILE="$M/.env"

# ── 1. Load env ───────────────────────────────────────────────────────────────
[ -f "$ENV_FILE" ] && set -a && . "$ENV_FILE" && set +a

: "${PROVISIONING_BUCKET:?PROVISIONING_BUCKET is required}"
: "${GRAFANA_HOST:?GRAFANA_HOST is required}"
: "${API_GATEWAY_HOST:?API_GATEWAY_HOST is required}"
: "${GF_ADMIN_PASSWORD:?GF_ADMIN_PASSWORD is required}"
AWS_REGION="${AWS_REGION:-ap-northeast-2}"

mkdir -p "$M/provisioning"

# ── 2. Sync provisioning/ from S3 ─────────────────────────────────────────────
echo "→ syncing provisioning from s3://$PROVISIONING_BUCKET/provisioning/"
aws s3 sync "s3://$PROVISIONING_BUCKET/provisioning/" "$M/provisioning/" \
  --delete --region "$AWS_REGION"

# ── 3. Substitute placeholders ────────────────────────────────────────────────
echo "→ substituting __GRAFANA_HOST__ and __API_GATEWAY_HOST__"
find "$M/provisioning" -type f \( -name '*.json' -o -name '*.yaml' -o -name '*.yml' \) \
  -exec sed -i \
    -e "s|__GRAFANA_HOST__|${GRAFANA_HOST}|g" \
    -e "s|__API_GATEWAY_HOST__|${API_GATEWAY_HOST}|g" \
    {} +

# ── 4. Write docker-compose.yml + prometheus.yml ──────────────────────────────
cat > "$M/prometheus.yml" <<'PROM'
global:
  scrape_interval: 15s
scrape_configs:
  - job_name: prometheus
    static_configs:
      - targets: ['localhost:9090']
  - job_name: grafana
    static_configs:
      - targets: ['grafana:3000']
PROM

cat > "$M/docker-compose.yml" <<'COMPOSE'
services:
  prometheus:
    image: prom/prometheus:latest
    container_name: prometheus
    ports: ["9090:9090"]
    volumes:
      - ./prometheus.yml:/etc/prometheus/prometheus.yml
      - prometheus-storage:/prometheus
    command:
      - '--config.file=/etc/prometheus/prometheus.yml'
      - '--storage.tsdb.path=/prometheus'
      - '--web.enable-lifecycle'
    networks: [monitoring]
    restart: unless-stopped

  grafana:
    image: grafana/grafana:latest
    container_name: grafana
    ports: ["3000:3000"]
    environment:
      - GF_SECURITY_ADMIN_USER=admin
      - GF_SECURITY_ADMIN_PASSWORD=${GF_ADMIN_PASSWORD}
      - GF_USERS_ALLOW_SIGN_UP=false
      - GF_PANELS_DISABLE_SANITIZE_HTML=true
      - GF_INSTALL_PLUGINS=yesoreyeram-infinity-datasource
      - GF_SMTP_ENABLED=${SMTP_ENABLED:-false}
      - GF_SMTP_HOST=${SMTP_HOST:-}
      - GF_SMTP_USER=${SMTP_USER:-}
      - GF_SMTP_PASSWORD=${SMTP_PASSWORD:-}
      - GF_SMTP_FROM_ADDRESS=${SMTP_FROM_ADDRESS:-}
      - GF_SMTP_FROM_NAME=Grafana AWS Monitoring
    volumes:
      - grafana-storage:/var/lib/grafana
      - ./provisioning:/etc/grafana/provisioning
    networks: [monitoring]
    restart: unless-stopped

  bedrock-api:
    build: ./proxy
    container_name: bedrock-api
    ports: ["8000:8000"]
    environment:
      - REGION=${AWS_REGION}
      - LAMBDA_FUNCTION=bedrock-qa
      - AGENTCORE_RUNTIME_ARN=${AGENTCORE_RUNTIME_ARN:-}
    networks: [monitoring]
    restart: unless-stopped

volumes:
  prometheus-storage:
  grafana-storage:

networks:
  monitoring:
    driver: bridge
COMPOSE

chown -R ec2-user:ec2-user "$M"

# ── 5. Start the stack ────────────────────────────────────────────────────────
cd "$M"
docker compose pull
docker compose up -d

echo "✓ bootstrap complete."
echo "   grafana: http://localhost:3000  (public: https://${GRAFANA_HOST})"
echo "   bedrock-api: http://localhost:8000"
