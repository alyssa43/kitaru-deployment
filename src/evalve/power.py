"""Graceful power changes for the EC2 instance already in Terraform state."""

import json
import time

from evalve import EvalveError
from evalve.aws import Aws, AwsError

DRAIN = [
    "systemctl stop evalve-refresh.timer evalve-refresh.service",
    "systemctl stop evalve-kitaru.service",
    "running=$(docker ps --filter name=^/evalve-kitaru$ --format '{{.ID}}')",
    'test -z "$running"',
]
START = [
    "cloud-init status --wait",
    "systemctl start evalve-kitaru.service evalve-refresh.timer evalve-caddy.service",
]


def instance_state(aws: Aws, instance_id: str, *, allow_gone: bool = False) -> str:
    try:
        result = aws.call("ec2", "describe-instances", "--instance-ids", instance_id)
    except AwsError as exc:
        if allow_gone and exc.code == "InvalidInstanceID.NotFound":
            return "terminated"
        raise
    instances = [
        item
        for reservation in result["Reservations"]
        for item in reservation["Instances"]
    ]
    if len(instances) != 1 or instances[0]["InstanceId"] != instance_id:
        raise EvalveError("The recorded EC2 instance could not be verified.")
    instance = instances[0]
    if {tag["Key"]: tag["Value"] for tag in instance.get("Tags", [])}.get(
        "ManagedBy"
    ) != "evalve":
        raise EvalveError(
            "The recorded EC2 instance does not have the Evalve ownership tag."
        )
    return instance["State"]["Name"]


def run_server_command(aws: Aws, instance_id: str, commands: list[str]) -> None:
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = aws.call(
            "ssm",
            "describe-instance-information",
            "--filters",
            f"Key=InstanceIds,Values={instance_id}",
        )
        if any(
            item["InstanceId"] == instance_id and item["PingStatus"] == "Online"
            for item in result["InstanceInformationList"]
        ):
            break
        time.sleep(5)
    else:
        raise EvalveError(
            "EC2 is not online in Systems Manager. Check instance IAM, networking, and bootstrap logs."
        )

    command_id = aws.call(
        "ssm",
        "send-command",
        "--instance-ids",
        instance_id,
        "--document-name",
        "AWS-RunShellScript",
        "--parameters",
        json.dumps({"commands": ["set -eu", *commands], "executionTimeout": ["240"]}),
        "--timeout-seconds",
        "60",
    )["Command"]["CommandId"]
    deadline = time.monotonic() + 320
    while time.monotonic() < deadline:
        try:
            status = aws.call(
                "ssm",
                "get-command-invocation",
                "--command-id",
                command_id,
                "--instance-id",
                instance_id,
            )["Status"]
        except AwsError as exc:
            if exc.code != "InvocationDoesNotExist":
                raise
        else:
            if status == "Success":
                return
            if status not in {"Pending", "InProgress", "Delayed"}:
                raise EvalveError(
                    f"Systems Manager command {command_id} ended with status {status}. Check its details and retry."
                )
        time.sleep(3)
    raise EvalveError(
        f"Systems Manager command {command_id} timed out. Check its status before retrying."
    )


def start(aws: Aws, instance_id: str) -> None:
    state = instance_state(aws, instance_id)
    if state == "stopping":
        aws.call("ec2", "wait", "instance-stopped", "--instance-ids", instance_id)
        state = "stopped"
    if state == "stopped":
        aws.call("ec2", "start-instances", "--instance-ids", instance_id)
    elif state not in {"running", "pending"}:
        raise EvalveError(f"Cannot start the recorded EC2 instance in state {state}.")
    if state != "running":
        aws.call("ec2", "wait", "instance-running", "--instance-ids", instance_id)
    run_server_command(aws, instance_id, START)


def stop(aws: Aws, instance_id: str, *, allow_gone: bool = False) -> None:
    state = instance_state(aws, instance_id, allow_gone=allow_gone)
    if state == "stopped" or (allow_gone and state == "terminated"):
        return
    if state == "pending":
        aws.call("ec2", "wait", "instance-running", "--instance-ids", instance_id)
        state = "running"
    if state == "running":
        run_server_command(aws, instance_id, DRAIN)
        aws.call("ec2", "stop-instances", "--instance-ids", instance_id)
    elif state != "stopping":
        raise EvalveError(f"Cannot stop the recorded EC2 instance in state {state}.")
    aws.call("ec2", "wait", "instance-stopped", "--instance-ids", instance_id)
