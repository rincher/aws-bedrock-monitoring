import json
import boto3
import re
import time
from datetime import datetime, timezone, timedelta

from config import REGION, MODEL_ID, bedrock, RDS_HOST, RDS_USER, RDS_PORT, RDS_DB, EC2_INSTANCE_ID

MODEL_SONNET = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

SYSTEM_PROMPT = """You are an AWS cloud assistant with direct access to the user's AWS account via tools.
When asked about AWS resources, always use the available tools to fetch real-time data rather than giving generic answers.
Present results clearly with tables or bullet points. Include key details like IDs, states, types, and costs.
If a dedicated tool exists for the task, prefer it. If not, use the call_aws_api tool to call any read-only boto3 method directly.
If a tool call fails, explain why and suggest what permissions might be needed.

You have DIRECT database access to the RDS MySQL instance via the query_rds tool.
For ANY question about databases, tables, schemas, rows, or data — you MUST call query_rds and return the actual results.
NEVER write SQL in your response as an example or suggestion. NEVER tell the user to run a query themselves.
If you are about to write a SQL code block in your answer without having called query_rds first, stop and call the tool instead.
The user cannot run queries — only you can. Always execute and show the real data.

Always format your final reply using exactly these two XML tags — they must wrap your entire reply with no text outside them:
<thinking>
Your internal reasoning — what data you gathered, what it means, gaps or caveats.
</thinking>
<response>
Your clear, human-friendly answer. Always respond in Korean, even if the question contains English technical terms, product names, or mixed-language phrases. Avoid repeating raw JSON. Summarise numbers concisely.
Do NOT use <thinking> or <response> as labels or headers inside your answer — they are only outer wrappers.
</response>"""

TOOLS = [
    {
        "toolSpec": {
            "name": "describe_ec2_instances",
            "description": "List EC2 instances with their state, type, tags, IPs, and launch time.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "instance_ids": {"type": "array", "items": {"type": "string"}},
                        "filters": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "Name": {"type": "string"},
                                    "Values": {"type": "array", "items": {"type": "string"}}
                                }
                            }
                        }
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "describe_rds_instances",
            "description": "List RDS database instances with engine, class, status, storage, and endpoint.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {"db_instance_identifier": {"type": "string"}}
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "list_s3_buckets",
            "description": "List all S3 buckets with their creation date.",
            "inputSchema": {"json": {"type": "object", "properties": {}}}
        }
    },
    {
        "toolSpec": {
            "name": "get_cost_and_usage",
            "description": "Get AWS cost and usage grouped by service for a given time period.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {
                        "days": {"type": "integer"},
                        "granularity": {"type": "string", "enum": ["DAILY", "MONTHLY"]}
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "describe_security_groups",
            "description": "List EC2 security groups with inbound/outbound rules.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {"group_ids": {"type": "array", "items": {"type": "string"}}}
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "get_cloudwatch_metrics",
            "description": "Get CloudWatch metric statistics (e.g. EC2 CPUUtilization, RDS FreeableMemory).",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["namespace", "metric_name", "dimensions"],
                    "properties": {
                        "namespace":   {"type": "string"},
                        "metric_name": {"type": "string"},
                        "dimensions":  {"type": "array", "items": {"type": "object"}},
                        "hours":       {"type": "integer"},
                        "stat":        {"type": "string"}
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "describe_lambda_functions",
            "description": "List Lambda functions with runtime, memory, timeout, and last modified.",
            "inputSchema": {"json": {"type": "object", "properties": {}}}
        }
    },
    {
        "toolSpec": {
            "name": "describe_iam_roles",
            "description": "List IAM roles in the account.",
            "inputSchema": {
                "json": {
                    "type": "object",
                    "properties": {"path_prefix": {"type": "string"}}
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "query_rds",
            "description": (
                "Execute a read-only SQL statement (SELECT, SHOW, DESCRIBE, EXPLAIN) directly against "
                "the RDS MySQL instance (dev-mysql-84). You are already connected — just provide the SQL. "
                "Use this for ANY question about databases, tables, schemas, row counts, or data. "
                "Do not ask the user to connect manually; run the query yourself."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["sql"],
                    "properties": {
                        "sql":      {"type": "string"},
                        "database": {"type": "string"}
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "run_ssm_command",
            "description": (
                "Run a read-only shell command on the EC2 instance via AWS SSM. "
                "Use this to inspect logs, processes, disk/memory usage, running services, "
                "Docker containers, or any live system state. "
                "Only read-only commands are permitted — file writes, deletes, process kills, "
                "package installs, and service restarts are all blocked."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["command"],
                    "properties": {
                        "command":     {"type": "string"},
                        "instance_id": {"type": "string"}
                    }
                }
            }
        }
    },
    {
        "toolSpec": {
            "name": "call_aws_api",
            "description": (
                "Call any boto3 AWS API method not covered by the other tools. "
                "Only read-only methods are permitted (those starting with: "
                "describe_, list_, get_, scan_, query_, search_, check_, validate_, filter_). "
                "Example: service='ec2', method='describe_vpcs', params={}, region='ap-northeast-2'."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["service", "method"],
                    "properties": {
                        "service": {"type": "string"},
                        "method":  {"type": "string"},
                        "params":  {"type": "object"},
                        "region":  {"type": "string"}
                    }
                }
            }
        }
    },
]

_BLOCKED_SSM_PATTERNS = [
    r'(?<![2&])>(?!&)', r'<<',
    r'\brm\b', r'\brmdir\b', r'\bmv\b', r'\bcp\b',
    r'\bdd\b', r'\bmkfs\b', r'\bfdisk\b', r'\bparted\b',
    r'\bchmod\b', r'\bchown\b',
    r'\bkill\b', r'\bpkill\b', r'\bkillall\b',
    r'\btruncate\b', r'\bshred\b', r'\btee\b', r'\bsed\s+-i\b',
    r'\bapt\s+(install|remove|purge|upgrade|update|autoremove|dist-upgrade)\b',
    r'\byum\s+(install|update|upgrade|remove|erase|autoremove|groupinstall|groupremove|downgrade)\b',
    r'\bdnf\s+(install|update|upgrade|remove|erase|autoremove|groupinstall|groupremove|downgrade)\b',
    r'\bpip[23]?\s+install\b', r'\bnpm\s+install\b',
    r'\bsystemctl\s+(stop|restart|disable|mask|daemon-reload)\b',
    r'\bservice\s+\S+\s+(stop|restart)\b',
    r'\bdocker\s+(rm|rmi|stop|kill|restart|exec)\b',
    r'\bcrontab\s+(-e|-r)\b',
    r'\bpasswd\b', r'\buseradd\b', r'\buserdel\b', r'\busermod\b',
    r'\bnohup\b', r'\beval\b',
    r'\b(bash|sh|zsh|fish|ksh)\s+-c\b',
    r'\bpython[23]?\s+-c\b', r'\bperl\s+-e\b', r'\bruby\s+-e\b',
]

_SAFE_PREFIXES = (
    "describe_", "list_", "get_", "scan_", "query_",
    "search_", "check_", "validate_", "filter_", "batch_get_",
)


def execute_tool(tool_name: str, tool_input: dict) -> dict:
    try:
        if tool_name == "describe_ec2_instances":
            ec2 = boto3.client("ec2", region_name=REGION)
            kwargs = {}
            if tool_input.get("instance_ids"):
                kwargs["InstanceIds"] = tool_input["instance_ids"]
            if tool_input.get("filters"):
                kwargs["Filters"] = tool_input["filters"]
            resp = ec2.describe_instances(**kwargs)
            instances = []
            for r in resp["Reservations"]:
                for i in r["Instances"]:
                    name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), "")
                    instances.append({
                        "InstanceId": i["InstanceId"], "Name": name,
                        "State": i["State"]["Name"], "InstanceType": i["InstanceType"],
                        "PublicIpAddress": i.get("PublicIpAddress", "N/A"),
                        "PrivateIpAddress": i.get("PrivateIpAddress", "N/A"),
                        "LaunchTime": i["LaunchTime"].isoformat(),
                    })
            return {"instances": instances, "count": len(instances)}

        elif tool_name == "describe_rds_instances":
            rds = boto3.client("rds", region_name=REGION)
            kwargs = {}
            if tool_input.get("db_instance_identifier"):
                kwargs["DBInstanceIdentifier"] = tool_input["db_instance_identifier"]
            resp = rds.describe_db_instances(**kwargs)
            dbs = [{
                "DBInstanceIdentifier": db["DBInstanceIdentifier"],
                "Engine": f"{db['Engine']} {db.get('EngineVersion', '')}",
                "DBInstanceClass": db["DBInstanceClass"],
                "DBInstanceStatus": db["DBInstanceStatus"],
                "AllocatedStorage": f"{db['AllocatedStorage']} GB",
                "MultiAZ": db["MultiAZ"],
                "Endpoint": db.get("Endpoint", {}).get("Address", "N/A"),
            } for db in resp["DBInstances"]]
            return {"db_instances": dbs, "count": len(dbs)}

        elif tool_name == "list_s3_buckets":
            resp = boto3.client("s3").list_buckets()
            return {"buckets": [{"Name": b["Name"], "CreationDate": b["CreationDate"].isoformat()} for b in resp["Buckets"]]}

        elif tool_name == "get_cost_and_usage":
            ce = boto3.client("ce", region_name="us-east-1")
            days = tool_input.get("days", 30)
            granularity = tool_input.get("granularity", "MONTHLY")
            end = datetime.now(timezone.utc).date()
            start = end - timedelta(days=days)
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
                Granularity=granularity,
                Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
            results = []
            for period in resp["ResultsByTime"]:
                for g in period["Groups"]:
                    cost = float(g["Metrics"]["UnblendedCost"]["Amount"])
                    if cost > 0.01:
                        results.append({"Period": period["TimePeriod"]["Start"], "Service": g["Keys"][0], "Cost_USD": round(cost, 4)})
            results.sort(key=lambda x: x["Cost_USD"], reverse=True)
            return {"costs": results, "period": f"{start} to {end}"}

        elif tool_name == "describe_security_groups":
            ec2 = boto3.client("ec2", region_name=REGION)
            kwargs = {}
            if tool_input.get("group_ids"):
                kwargs["GroupIds"] = tool_input["group_ids"]
            resp = ec2.describe_security_groups(**kwargs)
            sgs = [{
                "GroupId": sg["GroupId"], "GroupName": sg["GroupName"],
                "InboundRules": [{"Protocol": p.get("IpProtocol"), "FromPort": p.get("FromPort"), "ToPort": p.get("ToPort"), "CidrRanges": [r["CidrIp"] for r in p.get("IpRanges", [])]} for p in sg["IpPermissions"]],
            } for sg in resp["SecurityGroups"]]
            return {"security_groups": sgs}

        elif tool_name == "get_cloudwatch_metrics":
            cw = boto3.client("cloudwatch", region_name=REGION)
            hours = tool_input.get("hours", 24)
            stat = tool_input.get("stat", "Average")
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=hours)
            resp = cw.get_metric_statistics(
                Namespace=tool_input["namespace"], MetricName=tool_input["metric_name"],
                Dimensions=tool_input["dimensions"], StartTime=start_time, EndTime=end_time,
                Period=3600, Statistics=[stat],
            )
            points = sorted(resp["Datapoints"], key=lambda x: x["Timestamp"])
            return {"metric": tool_input["metric_name"], "datapoints": [{"Timestamp": p["Timestamp"].isoformat(), stat: round(p[stat], 4)} for p in points]}

        elif tool_name == "describe_lambda_functions":
            lmb = boto3.client("lambda", region_name=REGION)
            functions = []
            for page in lmb.get_paginator("list_functions").paginate():
                for f in page["Functions"]:
                    functions.append({"FunctionName": f["FunctionName"], "Runtime": f.get("Runtime", "N/A"), "MemorySize": f["MemorySize"], "Timeout": f["Timeout"]})
            return {"functions": functions}

        elif tool_name == "describe_iam_roles":
            iam = boto3.client("iam")
            kwargs = {}
            if tool_input.get("path_prefix"):
                kwargs["PathPrefix"] = tool_input["path_prefix"]
            resp = iam.list_roles(**kwargs)
            return {"roles": [{"RoleName": r["RoleName"], "Path": r["Path"]} for r in resp["Roles"]]}

        elif tool_name == "query_rds":
            import pymysql
            import pymysql.cursors
            sql = tool_input.get("sql", "").strip()
            database = tool_input.get("database") or RDS_DB or None
            first_word = sql.split()[0].upper() if sql.split() else ""
            if first_word not in ("SELECT", "SHOW", "DESCRIBE", "DESC", "EXPLAIN"):
                return {"error": f"Only SELECT, SHOW, DESCRIBE, and EXPLAIN are allowed. Got: {first_word}"}
            if not RDS_HOST:
                return {"error": "RDS_HOST is not configured."}
            rds_client = boto3.client("rds", region_name=REGION)
            token = rds_client.generate_db_auth_token(DBHostname=RDS_HOST, Port=RDS_PORT, DBUsername=RDS_USER, Region=REGION)
            conn = pymysql.connect(host=RDS_HOST, port=RDS_PORT, user=RDS_USER, password=token, database=database, ssl={"ssl": True}, connect_timeout=10, cursorclass=pymysql.cursors.DictCursor)
            try:
                with conn.cursor() as cur:
                    cur.execute(sql)
                    rows = cur.fetchmany(200)
                    return {"rows": rows, "count": len(rows)}
            finally:
                conn.close()

        elif tool_name == "call_aws_api":
            service = tool_input.get("service", "").strip().lower()
            method  = tool_input.get("method",  "").strip().lower()
            params  = tool_input.get("params",  {}) or {}
            region  = tool_input.get("region",  REGION)
            if not service or not method:
                return {"error": "Both 'service' and 'method' are required."}
            if not any(method.startswith(p) for p in _SAFE_PREFIXES):
                return {"error": f"Method '{method}' is not permitted. Only read-only methods are allowed."}
            client = boto3.client(service, region_name=region)
            if not hasattr(client, method):
                return {"error": f"boto3 client for '{service}' has no method '{method}'."}
            result = getattr(client, method)(**params)
            result.pop("ResponseMetadata", None)
            return result

        elif tool_name == "run_ssm_command":
            command     = tool_input.get("command", "").strip()
            instance_id = tool_input.get("instance_id", "").strip() or EC2_INSTANCE_ID
            if not command:
                return {"error": "command is required."}
            for pattern in _BLOCKED_SSM_PATTERNS:
                if re.search(pattern, command, re.IGNORECASE):
                    return {"error": f"Command blocked by safety policy (matched: {pattern})"}
            ssm = boto3.client("ssm", region_name=REGION)
            send_resp = ssm.send_command(InstanceIds=[instance_id], DocumentName="AWS-RunShellScript", Parameters={"commands": [command]})
            command_id = send_resp["Command"]["CommandId"]
            # AgentCore has no API Gateway timeout — poll up to 60s
            for _ in range(60):
                time.sleep(1)
                try:
                    inv = ssm.get_command_invocation(CommandId=command_id, InstanceId=instance_id)
                    if inv["Status"] in ("Success", "Failed", "Cancelled", "TimedOut"):
                        return {"status": inv["Status"], "stdout": inv.get("StandardOutputContent", "").strip(), "stderr": inv.get("StandardErrorContent", "").strip()}
                except ssm.exceptions.InvocationDoesNotExist:
                    continue
            return {"error": "SSM command timed out.", "command_id": command_id}

        else:
            return {"error": f"Unknown tool: {tool_name}"}

    except Exception as e:
        return {"error": str(e)}


def ask_with_tools(question: str, history: list, model: str = None, system_suffix: str = "") -> tuple[str, str]:
    messages = list(history) + [{"role": "user", "content": [{"text": question}]}]
    system_text = SYSTEM_PROMPT + system_suffix
    model_id = model or MODEL_ID

    for _ in range(10):
        response = bedrock.converse(
            modelId=model_id,
            system=[{"text": system_text}],
            messages=messages,
            toolConfig={"tools": TOOLS},
        )
        stop_reason     = response["stopReason"]
        output_message  = response["output"]["message"]
        messages.append(output_message)

        tool_use_blocks = [b["toolUse"] for b in output_message["content"] if "toolUse" in b]
        if tool_use_blocks:
            tool_results = []
            for tool_use in tool_use_blocks:
                result = execute_tool(tool_use["name"], tool_use.get("input", {}))
                if tool_use["name"] == "query_rds":
                    model_id = MODEL_SONNET
                tool_results.append({"toolResult": {"toolUseId": tool_use["toolUseId"], "content": [{"text": json.dumps(result, default=str)}]}})
            messages.append({"role": "user", "content": tool_results})
            continue

        if stop_reason == "end_turn":
            text = next((b["text"] for b in output_message["content"] if "text" in b), "")
            return text, model_id

        text = " ".join(b["text"] for b in output_message["content"] if "text" in b)
        return text, model_id

    return "최대 반복 횟수에 도달하여 최종 답변을 생성하지 못했습니다.", model_id
