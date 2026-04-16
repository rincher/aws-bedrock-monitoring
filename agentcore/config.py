import os
import boto3

MODEL_ID        = os.getenv("BEDROCK_MODEL", "us.amazon.nova-micro-v1:0")
REGION          = os.getenv("REGION", "ap-northeast-2")
RDS_HOST        = os.getenv("RDS_HOST", "")
RDS_USER        = os.getenv("RDS_USER", "ec2_user")
RDS_PORT        = int(os.getenv("RDS_PORT", "3306"))
RDS_DB          = os.getenv("RDS_DB", "")
EC2_INSTANCE_ID = os.getenv("EC2_INSTANCE_ID", "i-06ffb77afac77d17b")
MEMORY_ID       = os.getenv("AGENTCORE_MEMORY_ID", "")
SESSION_TABLE   = os.getenv("SESSION_TABLE", "bedrock-sessions")

bedrock         = boto3.client("bedrock-runtime", region_name="us-east-1")
agentcore_data  = boto3.client("bedrock-agentcore", region_name=REGION)
