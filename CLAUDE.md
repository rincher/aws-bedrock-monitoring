# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

AWS monitoring platform: Grafana + CloudWatch dashboards on EC2 (Docker), plus a Lambda-based Bedrock chat assistant (`bedrock-qa`) that queries live AWS resources via tools.

## Deploying Lambda

```bash
cd lambda && zip -r /tmp/bedrock-qa.zip .
aws lambda update-function-code --function-name bedrock-qa \
  --zip-file fileb:///tmp/bedrock-qa.zip --profile sandbox --region ap-northeast-2
```

## Deploying to EC2 (no public IP — SSM only)

```bash
# Push a file
CONTENT=$(cat <file> | base64)
aws ssm send-command --instance-ids i-06ffb77afac77d17b \
  --document-name "AWS-RunShellScript" \
  --parameters "commands=[\"echo '$CONTENT' | base64 -d > /home/ec2-user/monitoring/<dest>\"]" \
  --profile sandbox --region ap-northeast-2 --query 'Command.CommandId' --output text

# Restart Grafana
aws ssm send-command --instance-ids i-06ffb77afac77d17b \
  --document-name "AWS-RunShellScript" \
  --parameters 'commands=["cd /home/ec2-user/monitoring && docker compose restart grafana"]' \
  --profile sandbox --region ap-northeast-2 --query 'Command.CommandId' --output text
```

## Re-provisioning a Grafana dashboard

Grafana blocks API updates to provisioned dashboards. Use the remove→restart→restore→restart cycle to force a clean re-provision from file.

## Architecture

### Lambda (lambda/)

- `lambda_function.py` — router only; loads `ui.html` via `open()` at cold-start (not inline string) to avoid Python `\n` escaping breaking JS
- `agent.py` — Bedrock Converse agentic loop (≤10 iterations). 9 tools + generic `call_aws_api` (read-only boto3, method prefix allowlist). System prompt requires `<thinking>` / `<response>` XML tags in every reply.
- `session.py` — DynamoDB primary, S3 fallback. `compact_handler` summarises long sessions via Bedrock.
- `memory.py` — Global facts in `bedrock-memory` DynamoDB table (`memory_id = "global"`). Called after every `/ask`; extracts 0–3 facts, deduplicates, caps at 50. Injected into system prompt via `build_memory_prompt()`.
- `config.py` — `bedrock` client is **us-east-1**; all other clients default to **ap-northeast-2**.

### Grafana dashboards

- `bedrock-qa.json` — iframe of Lambda `/ui`. Hidden `q` textbox variable; iframe src appends `?q=${q}` so data links from CloudWatch dashboard can pre-fill questions via `var-q=`.
- `cloudwatch-aws.json` — EC2 (`AWS/EC2`), Lambda (`AWS/Lambda`), and CloudWatch Agent (`CWAgent`) metrics. Disk panels require all 4 CWAgent dimensions: `InstanceId`, `path`, `device` (`nvme0n1p1`), `fstype` (`xfs`), with `matchExact: false`.

### IAM roles

- Lambda: `bedrock-qa-lambda-role`
- EC2: `dev-AllowSSMCloudwatch` (also has inline `grafana-cloudwatch-metrics-ro`)
- Grafana assumes `grafana-cloudwatch-ro` via the EC2 role — trust policy on that role must include `dev-AllowSSMCloudwatch` as principal

## Key constants

| Resource | Value |
|---|---|
| EC2 instance | `i-06ffb77afac77d17b` |
| Bedrock model | `us.amazon.nova-micro-v1:0` |
| DynamoDB tables | `bedrock-sessions`, `bedrock-memory` |
| Grafana | `https://grafana.hyundong.shop` |

## Sensitive values

Real values are in `.env` (gitignored). GitHub has placeholders. EC2 has a real `.env` at `/home/ec2-user/monitoring/.env`. Restore real values before deploying to EC2; re-apply placeholders before committing.
