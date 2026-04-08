import os
import boto3

MODEL_ID       = os.getenv("BEDROCK_MODEL", "us.amazon.nova-micro-v1:0")
REGION         = os.getenv("REGION", "ap-northeast-2")
API_URL        = os.getenv("API_URL", "https://YOUR_API_ID.execute-api.ap-northeast-2.amazonaws.com/prod/ask")
SESSION_TABLE  = os.getenv("SESSION_TABLE", "bedrock-sessions")
SESSION_BUCKET = os.getenv("SESSION_BUCKET", "")
SESSION_PREFIX = "bedrock-sessions/"

bedrock = boto3.client("bedrock-runtime", region_name="us-east-1")
