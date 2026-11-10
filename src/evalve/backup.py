"""Create and verify the durable inputs needed for a PostgreSQL restore."""

import re

from evalve import EvalveError
from evalve.aws import Aws, AwsError, read_application_secret


def snapshot_key(snapshot_id: str) -> str:
    if not re.fullmatch(r"evalve-[0-9]{14}-[a-f0-9]{12}", snapshot_id):
        raise EvalveError("Provide a snapshot ID printed by evalve down.")
    return f"evalve/snapshots/{snapshot_id}.json"


def get_snapshot(aws: Aws, snapshot_id: str) -> dict | None:
    try:
        snapshots = aws.call(
            "rds", "describe-db-snapshots", "--db-snapshot-identifier", snapshot_id
        )["DBSnapshots"]
    except AwsError as exc:
        if exc.code in {"DBSnapshotNotFound", "DBSnapshotNotFoundFault"}:
            return None
        raise
    if len(snapshots) != 1:
        raise EvalveError(f"Cannot uniquely identify RDS snapshot {snapshot_id}.")
    return snapshots[0]


def check_snapshot_source(
    aws: Aws, snapshot: dict, snapshot_id: str, database_resource_id: str
) -> None:
    arn = f"arn:aws:rds:{aws.region}:{aws.account}:snapshot:{snapshot_id}"
    if (
        snapshot.get("DBSnapshotArn") != arn
        or snapshot.get("DbiResourceId") != database_resource_id
        or snapshot.get("DBInstanceIdentifier") != "evalve"
        or snapshot.get("SnapshotType") != "manual"
        or snapshot.get("Engine") != "postgres"
        or not snapshot.get("Encrypted")
    ):
        raise EvalveError(
            f"Snapshot {snapshot_id} does not match the encrypted Evalve database in this account and region."
        )


def take_snapshot(
    aws: Aws, snapshot_id: str, database_resource_id: str, snapshot: dict | None
) -> dict:
    if snapshot is None:
        databases = aws.call(
            "rds", "describe-db-instances", "--db-instance-identifier", "evalve"
        )["DBInstances"]
        if (
            len(databases) != 1
            or databases[0].get("DbiResourceId") != database_resource_id
        ):
            raise EvalveError(
                "The live database does not match the Evalve resource recorded in Terraform state."
            )
        aws.call(
            "rds", "wait", "db-instance-available", "--db-instance-identifier", "evalve"
        )
        try:
            aws.call(
                "rds",
                "create-db-snapshot",
                "--db-instance-identifier",
                "evalve",
                "--db-snapshot-identifier",
                snapshot_id,
                "--tags",
                "Key=ManagedBy,Value=evalve",
            )
        except AwsError as exc:
            if exc.code not in {
                "DBSnapshotAlreadyExists",
                "DBSnapshotAlreadyExistsFault",
            }:
                raise
    else:
        check_snapshot_source(aws, snapshot, snapshot_id, database_resource_id)
    if snapshot is None or snapshot["Status"] != "available":
        aws.call(
            "rds",
            "wait",
            "db-snapshot-available",
            "--db-snapshot-identifier",
            snapshot_id,
        )
        snapshot = get_snapshot(aws, snapshot_id)
    if not snapshot or snapshot["Status"] != "available":
        raise EvalveError(f"RDS snapshot {snapshot_id} is not available.")
    check_snapshot_source(aws, snapshot, snapshot_id, database_resource_id)
    return snapshot


def secret_version(aws: Aws, arn: str, version: str | None) -> str:
    return read_application_secret(aws, arn, version)[1]


def verify_manifest(aws: Aws, manifest: dict, snapshot_id: str) -> None:
    if (
        manifest.get("schema_version") != 1
        or manifest.get("account_id") != aws.account
        or manifest.get("region") != aws.region
        or manifest.get("snapshot_id") != snapshot_id
    ):
        raise EvalveError("Snapshot metadata does not match this Evalve deployment.")
    snapshot = get_snapshot(aws, snapshot_id)
    if not snapshot or snapshot.get("Status") != "available":
        raise EvalveError(f"RDS snapshot {snapshot_id} is not available.")
    check_snapshot_source(aws, snapshot, snapshot_id, manifest["database_resource_id"])
    kms_key = snapshot.get("KmsKeyId")
    if (
        kms_key != manifest["kms_key_id"]
        or snapshot["EngineVersion"]
        != manifest["configuration"]["database_engine_version"]
    ):
        raise EvalveError(
            "The snapshot no longer matches its saved encryption key or engine version."
        )
    if (
        kms_key
        and aws.call("kms", "describe-key", "--key-id", kms_key)["KeyMetadata"][
            "KeyState"
        ]
        != "Enabled"
    ):
        raise EvalveError("The snapshot encryption key is not enabled.")
    if not kms_key and snapshot.get("EncryptionType") != "sse-rds":
        raise EvalveError("The snapshot encryption key could not be verified.")
    config = manifest["configuration"]
    secret_version(
        aws, config["application_secret_arn"], config["application_secret_version"]
    )
