#!/usr/bin/env python3
"""
Provision Amazon Bedrock AgentCore Memory and Runtime for bedrock-qa.

Run once:
  python3 scripts/setup_agentcore.py

Outputs AGENTCORE_MEMORY_ID and AGENTCORE_RUNTIME_ARN — save these to .env.
"""
import boto3
import json
import subprocess
import time
import sys
import os

PROFILE      = "sandbox"
REGION       = "ap-northeast-2"
ECR_REPO     = "bedrock-agent-runtime"
ACCOUNT_ID   = "009215122583"
IMAGE_URI    = f"{ACCOUNT_ID}.dkr.ecr.{REGION}.amazonaws.com/{ECR_REPO}:latest"

# VPC config — same VPC as the existing EC2/Lambda setup
VPC_ID       = "vpc-026fd1b8e1c666ae2"

session  = boto3.Session(profile_name=PROFILE, region_name=REGION)
control  = session.client("bedrock-agentcore-control")
ecr      = session.client("ecr")
ec2      = session.client("ec2")
iam      = session.client("iam")


# ── Step 1: ECR repository ────────────────────────────────────────────────────

def ensure_ecr_repo():
    print("→ Ensuring ECR repository...")
    try:
        ecr.create_repository(repositoryName=ECR_REPO, imageScanningConfiguration={"scanOnPush": True})
        print(f"  Created {ECR_REPO}")
    except ecr.exceptions.RepositoryAlreadyExistsException:
        print(f"  Already exists: {ECR_REPO}")
    return IMAGE_URI


# ── Step 2: Build and push Docker image ──────────────────────────────────────

def build_and_push():
    print("→ Authenticating Docker with ECR...")
    token = ecr.get_authorization_token()["authorizationData"][0]
    registry = token["proxyEndpoint"]
    subprocess.run(
        f"aws ecr get-login-password --region {REGION} --profile {PROFILE} | "
        f"docker login --username AWS --password-stdin {registry.replace('https://', '')}",
        shell=True, check=True,
    )

    repo_dir = os.path.join(os.path.dirname(__file__), "..", "agentcore")
    print(f"→ Building ARM64 image from {repo_dir} ...")
    subprocess.run(
        ["docker", "buildx", "build", "--platform", "linux/arm64",
         "-t", IMAGE_URI, "--push", repo_dir],
        check=True,
    )
    print(f"  Pushed: {IMAGE_URI}")


# ── Step 3: AgentCore Memory ──────────────────────────────────────────────────

def create_memory():
    print("→ Creating AgentCore Memory...")
    resp = control.create_memory(
        name="bedrock-qa-memory",
        description="Session history and semantic facts for bedrock-qa AWS assistant.",
        memoryStrategies=[
            {
                "summaryMemoryStrategy": {
                    "name": "SessionSummarizer",
                    "namespaceTemplates": ["/summaries/{actorId}/{sessionId}/"],
                }
            },
            {
                "semanticMemoryStrategy": {
                    "name": "FactExtractor",
                    "namespaceTemplates": ["/facts/{actorId}/"],
                }
            },
        ],
    )
    memory_id = resp["memory"]["id"]
    print(f"  Memory ID: {memory_id} — polling until ACTIVE...")
    while True:
        status = control.get_memory(memoryId=memory_id)["memory"]["status"]
        if status == "ACTIVE":
            print(f"  Memory ACTIVE: {memory_id}")
            return memory_id
        if status == "FAILED":
            sys.exit("  Memory creation FAILED")
        time.sleep(10)


# ── Step 4: IAM role for AgentCore Runtime ────────────────────────────────────

def ensure_runtime_role():
    role_name = "bedrock-agentcore-runtime-role"
    print(f"→ Ensuring IAM role {role_name}...")
    trust = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock-agentcore.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }]
    }
    try:
        iam.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust),
            Description="Execution role for bedrock-qa AgentCore Runtime",
        )
        # Attach policies the container needs
        for policy_arn in [
            "arn:aws:iam::aws:policy/AmazonBedrockFullAccess",
            "arn:aws:iam::aws:policy/AmazonEC2ReadOnlyAccess",
            "arn:aws:iam::aws:policy/AmazonRDSReadOnlyAccess",
            "arn:aws:iam::aws:policy/CloudWatchReadOnlyAccess",
            "arn:aws:iam::aws:policy/IAMReadOnlyAccess",
        ]:
            iam.attach_role_policy(RoleName=role_name, PolicyArn=policy_arn)
        # Inline policies for DynamoDB, SSM, Cost Explorer, S3
        iam.put_role_policy(
            RoleName=role_name,
            PolicyName="agentcore-runtime-extras",
            PolicyDocument=json.dumps({
                "Version": "2012-10-17",
                "Statement": [
                    {"Effect": "Allow", "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Scan"], "Resource": f"arn:aws:dynamodb:{REGION}:{ACCOUNT_ID}:table/bedrock-sessions"},
                    {"Effect": "Allow", "Action": ["ssm:SendCommand", "ssm:GetCommandInvocation"], "Resource": "*"},
                    {"Effect": "Allow", "Action": ["ce:GetCostAndUsage"], "Resource": "*"},
                    {"Effect": "Allow", "Action": ["s3:ListAllMyBuckets"], "Resource": "*"},
                    {"Effect": "Allow", "Action": ["rds-db:connect"], "Resource": "*"},
                ]
            }),
        )
        print(f"  Created and configured {role_name}")
    except iam.exceptions.EntityAlreadyExistsException:
        print(f"  Already exists: {role_name}")
    return f"arn:aws:iam::{ACCOUNT_ID}:role/{role_name}"


# ── Step 5: VPC subnets + security group for Runtime ─────────────────────────

def get_vpc_config():
    print("→ Looking up VPC subnets and security groups...")
    subnets = ec2.describe_subnets(Filters=[{"Name": "vpc-id", "Values": [VPC_ID]}])
    subnet_ids = [s["SubnetId"] for s in subnets["Subnets"]][:2]  # use first 2 subnets

    # Reuse the same security group as EC2 (already has outbound internet + intra-VPC rules)
    sg_id = "sg-0963eb56dc86ef2ed"
    print(f"  Subnets: {subnet_ids}")
    print(f"  Security group: {sg_id}")
    return subnet_ids, sg_id


# ── Step 6: AgentCore Runtime ─────────────────────────────────────────────────

def create_runtime(role_arn: str, subnet_ids: list, sg_id: str):
    print("→ Creating AgentCore Runtime...")
    resp = control.create_agent_runtime(
        agentRuntimeName="bedrock-qa-runtime",
        description="AWS monitoring chat assistant — AgentCore Runtime",
        agentRuntimeArtifact={
            "containerConfiguration": {
                "containerUri": IMAGE_URI,
            }
        },
        roleArn=role_arn,
        networkConfiguration={
            "networkMode": "VPC",
            "networkModeConfig": {
                "subnets":        subnet_ids,
                "securityGroups": [sg_id],
            },
        },
        lifecycleConfiguration={
            "idleRuntimeSessionTimeout": 600,    # 10 min idle → container paused
            "maxLifetime":               28800,  # 8 hours max session
        },
    )
    runtime_arn = resp["agentRuntimeArn"]
    print(f"  Runtime ARN: {runtime_arn} — polling until ACTIVE...")
    while True:
        status = control.get_agent_runtime(agentRuntimeId=resp["agentRuntimeId"])["status"]
        if status == "ACTIVE":
            print(f"  Runtime ACTIVE")
            return runtime_arn
        if status in ("FAILED", "CREATE_FAILED"):
            sys.exit(f"  Runtime creation FAILED (status: {status})")
        time.sleep(15)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    ensure_ecr_repo()
    build_and_push()
    memory_id   = create_memory()
    role_arn    = ensure_runtime_role()
    subnet_ids, sg_id = get_vpc_config()
    # Brief pause so IAM role is fully propagated
    print("  Waiting 10s for IAM role propagation...")
    time.sleep(10)
    runtime_arn = create_runtime(role_arn, subnet_ids, sg_id)

    print("\n✓ Done. Add these to your .env and Lambda environment variables:\n")
    print(f"  AGENTCORE_MEMORY_ID={memory_id}")
    print(f"  AGENTCORE_RUNTIME_ARN={runtime_arn}")
    print("\nThen update the EC2 proxy env and redeploy.")


if __name__ == "__main__":
    main()
