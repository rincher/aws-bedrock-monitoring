import json
import boto3
from datetime import datetime, timezone, timedelta

from config import REGION, MODEL_ID, bedrock
from memory import get_facts, build_memory_prompt

SYSTEM_PROMPT = """You are an AWS cloud assistant with direct access to the user's AWS account via tools.
When asked about AWS resources, always use the available tools to fetch real-time data rather than giving generic answers.
Present results clearly with tables or bullet points. Include key details like IDs, states, types, and costs.
If a dedicated tool exists for the task, prefer it. If not, use the call_aws_api tool to call any read-only boto3 method directly.
If a tool call fails, explain why and suggest what permissions might be needed.

Always format your final reply using these two XML tags:
<thinking>
Your internal reasoning — what data you gathered, what it means, gaps or caveats.
</thinking>
<response>
Your clear, human-friendly answer. Write in plain English. Avoid repeating raw JSON. Summarise numbers concisely.
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
                        "dimensions":  {"type": "array", "items": {"type": "object", "properties": {"Name": {"type": "string"}, "Value": {"type": "string"}}}},
                        "hours": {"type": "integer"},
                        "stat":  {"type": "string"}
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
            "name": "call_aws_api",
            "description": (
                "Call any boto3 AWS API method not covered by the other tools. "
                "Use this when a needed AWS operation has no dedicated tool. "
                "Only read-only methods are permitted (those starting with: "
                "describe_, list_, get_, scan_, query_, search_, check_, validate_, filter_). "
                "Example: service='ec2', method='describe_vpcs', params={}, region='ap-northeast-2'."
            ),
            "inputSchema": {
                "json": {
                    "type": "object",
                    "required": ["service", "method"],
                    "properties": {
                        "service": {
                            "type": "string",
                            "description": "AWS service name, e.g. ec2, s3, rds, iam, ecs, eks, route53"
                        },
                        "method": {
                            "type": "string",
                            "description": "boto3 client method name, e.g. describe_vpcs, list_clusters"
                        },
                        "params": {
                            "type": "object",
                            "description": "Optional keyword arguments passed to the method"
                        },
                        "region": {
                            "type": "string",
                            "description": "AWS region override (defaults to ap-northeast-2)"
                        }
                    }
                }
            }
        }
    },
]

# Methods starting with these prefixes are considered safe (read-only)
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
                        "Platform": i.get("Platform", "linux"),
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
            buckets = [{"Name": b["Name"], "CreationDate": b["CreationDate"].isoformat()} for b in resp["Buckets"]]
            return {"buckets": buckets, "count": len(buckets)}

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
                        results.append({
                            "Period": period["TimePeriod"]["Start"],
                            "Service": g["Keys"][0],
                            "Cost_USD": round(cost, 4),
                        })
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
                "Description": sg["Description"],
                "InboundRules": [{
                    "Protocol": p.get("IpProtocol"),
                    "FromPort": p.get("FromPort"),
                    "ToPort": p.get("ToPort"),
                    "CidrRanges": [r["CidrIp"] for r in p.get("IpRanges", [])],
                } for p in sg["IpPermissions"]],
            } for sg in resp["SecurityGroups"]]
            return {"security_groups": sgs, "count": len(sgs)}

        elif tool_name == "get_cloudwatch_metrics":
            cw = boto3.client("cloudwatch", region_name=REGION)
            hours = tool_input.get("hours", 24)
            stat = tool_input.get("stat", "Average")
            end_time = datetime.now(timezone.utc)
            start_time = end_time - timedelta(hours=hours)
            resp = cw.get_metric_statistics(
                Namespace=tool_input["namespace"],
                MetricName=tool_input["metric_name"],
                Dimensions=tool_input["dimensions"],
                StartTime=start_time, EndTime=end_time,
                Period=3600, Statistics=[stat],
            )
            points = sorted(resp["Datapoints"], key=lambda x: x["Timestamp"])
            data = [{"Timestamp": p["Timestamp"].isoformat(), stat: round(p[stat], 4), "Unit": p["Unit"]} for p in points]
            return {"metric": tool_input["metric_name"], "datapoints": data, "count": len(data)}

        elif tool_name == "describe_lambda_functions":
            lmb = boto3.client("lambda", region_name=REGION)
            functions = []
            for page in lmb.get_paginator("list_functions").paginate():
                for f in page["Functions"]:
                    functions.append({
                        "FunctionName": f["FunctionName"],
                        "Runtime": f.get("Runtime", "N/A"),
                        "MemorySize": f["MemorySize"],
                        "Timeout": f["Timeout"],
                        "LastModified": f["LastModified"],
                    })
            return {"functions": functions, "count": len(functions)}

        elif tool_name == "describe_iam_roles":
            iam = boto3.client("iam")
            kwargs = {}
            if tool_input.get("path_prefix"):
                kwargs["PathPrefix"] = tool_input["path_prefix"]
            resp = iam.list_roles(**kwargs)
            roles = [{"RoleName": r["RoleName"], "Path": r["Path"], "CreateDate": r["CreateDate"].isoformat()} for r in resp["Roles"]]
            return {"roles": roles, "count": len(roles)}

        elif tool_name == "call_aws_api":
            service = tool_input.get("service", "").strip().lower()
            method  = tool_input.get("method",  "").strip().lower()
            params  = tool_input.get("params",  {}) or {}
            region  = tool_input.get("region",  REGION)

            if not service or not method:
                return {"error": "Both 'service' and 'method' are required."}

            # Read-only safeguard
            if not any(method.startswith(p) for p in _SAFE_PREFIXES):
                return {
                    "error": (
                        f"Method '{method}' is not permitted. "
                        "Only read-only methods are allowed "
                        f"(must start with one of: {', '.join(_SAFE_PREFIXES)})."
                    )
                }

            client = boto3.client(service, region_name=region)
            if not hasattr(client, method):
                return {"error": f"boto3 client for '{service}' has no method '{method}'."}

            result = getattr(client, method)(**params)
            # Strip ResponseMetadata to keep the response clean
            result.pop("ResponseMetadata", None)
            return result

        else:
            return {"error": f"Unknown tool: {tool_name}"}

    except Exception as e:
        return {"error": str(e)}


def ask_with_tools(question: str, history: list) -> str:
    messages = list(history) + [{"role": "user", "content": [{"text": question}]}]

    facts = get_facts()
    system_text = SYSTEM_PROMPT + build_memory_prompt(facts)

    for _ in range(10):
        response = bedrock.converse(
            modelId=MODEL_ID,
            system=[{"text": system_text}],
            messages=messages,
            toolConfig={"tools": TOOLS},
        )

        stop_reason = response["stopReason"]
        output_message = response["output"]["message"]
        messages.append(output_message)

        if stop_reason == "end_turn":
            return next(b["text"] for b in output_message["content"] if "text" in b)

        if stop_reason == "tool_use":
            tool_results = []
            for block in output_message["content"]:
                if "toolUse" in block:
                    tool_use = block["toolUse"]
                    result = execute_tool(tool_use["name"], tool_use.get("input", {}))
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tool_use["toolUseId"],
                            "content": [{"text": json.dumps(result, default=str)}],
                        }
                    })
            messages.append({"role": "user", "content": tool_results})

    return "Reached maximum tool call iterations without a final answer."
