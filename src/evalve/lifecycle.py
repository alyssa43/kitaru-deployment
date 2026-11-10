"""Coordinate EC2 power changes and a resumable snapshot-protected teardown."""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from evalve import EvalveError, backup, power, terraform
from evalve.aws import STATE_KEY, Aws
from evalve.readiness import wait_for_kitaru

OPERATION_KEY = "evalve/operation.json"
TF_SETTINGS = (
    "server_image",
    "database_engine_version",
    "application_secret_version",
    "snapshot_id",
)
PHASE_TWO_IMAGE = "zenmldocker/kitaru-server@sha256:05ea00482c0adf4609a34d2c95575a6530ce5d60f6cea0e79716336d9e251d47"


def managed_resources(state: dict | None) -> list[dict]:
    return [
        resource
        for resource in (state or {}).get("resources", [])
        if resource["mode"] == "managed" and resource["instances"]
    ]


def resource_attributes(resources: list[dict], kind: str, name: str) -> dict:
    instances = [
        instance
        for resource in resources
        if resource["type"] == kind
        and resource["name"] == name
        and "module" not in resource
        for instance in resource["instances"]
    ]
    if len(instances) > 1 or any(instance.get("deposed") for instance in instances):
        raise EvalveError(
            f"Terraform has an incomplete replacement of {kind}.{name}. Reconcile it before continuing."
        )
    return instances[0]["attributes"] if instances else {}


def verify_account(aws: Aws) -> None:
    if aws.caller_account() != aws.account:
        raise EvalveError(
            "The AWS profile changed accounts after confirmation. Run the command again to review the new target."
        )


def pending_operation(aws: Aws) -> tuple[dict | None, str | None]:
    operation, etag = aws.read_document(OPERATION_KEY)
    if operation is not None and (
        operation.get("schema_version") != 1
        or operation.get("account_id") != aws.account
        or operation.get("region") != aws.region
        or operation.get("action") not in {"down", "restore"}
        or not etag
    ):
        raise EvalveError(
            "The pending operation record could not be verified. Inspect it in the Evalve state bucket."
        )
    return operation, etag


def require_idle(operation: dict | None) -> None:
    if operation:
        retry = (
            "evalve down"
            if operation["action"] == "down"
            else f"evalve up --restore {operation['snapshot_id']}"
        )
        raise EvalveError(
            f"An Evalve operation is unfinished. Resume it with `{retry}`."
        )


def load_restore(aws: Aws, snapshot_id: str, operation: dict | None) -> dict:
    if operation:
        if operation["action"] != "restore" or operation["snapshot_id"] != snapshot_id:
            require_idle(operation)
    else:
        state, _ = aws.read_document(STATE_KEY)
        if managed_resources(state):
            raise EvalveError(
                "This deployment still has managed resources. Run down before restoring a snapshot."
            )
    manifest, _ = aws.read_document(backup.snapshot_key(snapshot_id))
    if not manifest:
        raise EvalveError(
            f"No Evalve restore metadata was found for {snapshot_id} in this account and region."
        )
    backup.verify_manifest(aws, manifest, snapshot_id)
    return dict(manifest["configuration"], snapshot_id=snapshot_id, domain=None)


def record_restore(aws: Aws, snapshot_id: str, operation: dict | None) -> str:
    if operation is None:
        state, _ = aws.read_document(STATE_KEY)
        if managed_resources(state):
            raise EvalveError(
                "The deployment changed while awaiting confirmation. Run up again to review it."
            )
        operation = {
            "schema_version": 1,
            "account_id": aws.account,
            "region": aws.region,
            "action": "restore",
            "snapshot_id": snapshot_id,
            "id": uuid4().hex,
        }
        aws.create_document(OPERATION_KEY, operation)
    current, etag = pending_operation(aws)
    if current != operation or not etag:
        raise EvalveError(
            "The restore checkpoint could not be read back. Retry up with the same snapshot ID."
        )
    return etag


def power_change(aws: Aws, action: str) -> None:
    require_idle(pending_operation(aws)[0])
    outputs = aws.state_outputs()
    if not outputs.get("instance_id") or not outputs.get("url"):
        raise EvalveError("No Evalve server is recorded. Run up to provision it.")
    print(
        f"AWS account: {aws.account} | Region: {aws.region} | EC2: {outputs['instance_id']}"
    )
    if input(
        f"{action.capitalize()} Evalve's EC2 server? RDS stays running and billable. [y/N] "
    ).strip().lower() not in {"y", "yes"}:
        print("Operation cancelled.")
        return
    verify_account(aws)
    require_idle(pending_operation(aws)[0])
    if action == "start":
        print("Starting EC2 and checking the existing HTTPS endpoint…", flush=True)
        power.start(aws, outputs["instance_id"])
        wait_for_kitaru(outputs["url"])
        print(f"Kitaru dashboard: {outputs['url']}")
    else:
        print("Draining Kitaru before stopping EC2…", flush=True)
        power.stop(aws, outputs["instance_id"])
        print("EC2 is stopped. Run evalve start to resume at the same URL.")


def new_shutdown(aws: Aws, state: dict, server: dict, database: dict) -> dict:
    deployed = state.get("outputs", {}).get("deployment", {}).get("value", {})
    secret = aws.ensure_application_secret(
        deployed.get("application_secret_arn"), create=False
    )
    config = {key: deployed[key] for key in TF_SETTINGS if key in deployed}
    config.setdefault("server_image", PHASE_TWO_IMAGE)
    config["backup_retention_days"] = deployed.get(
        "backup_retention_days", database.get("backup_retention_period", 7)
    )
    config.update(
        server_instance_type=deployed.get("server_instance_type")
        or server.get("instance_type")
        or "",
        database_instance_class=deployed.get("database_instance_class")
        or database.get("instance_class")
        or "",
        application_secret_arn=secret,
        domain=deployed.get("domain"),
    )
    if database:
        if not database.get("resource_id") or database.get("identifier") != "evalve":
            raise EvalveError(
                "The RDS database in Terraform state could not be verified."
            )
        config["application_secret_version"] = backup.secret_version(
            aws, secret, config.get("application_secret_version")
        )
    return {
        "schema_version": 1,
        "account_id": aws.account,
        "region": aws.region,
        "action": "down",
        "id": uuid4().hex,
        "instance_id": server.get("id"),
        "database_resource_id": database.get("resource_id"),
        "configuration": config,
        "snapshot_id": f"evalve-{datetime.now(UTC):%Y%m%d%H%M%S}-{uuid4().hex[:12]}"
        if database
        else None,
    }


def save_backup(aws: Aws, operation: dict, database: dict) -> None:
    snapshot_id = operation["snapshot_id"]
    instance_id = operation["instance_id"]
    snapshot = backup.get_snapshot(aws, snapshot_id) if snapshot_id else None
    if instance_id:
        if snapshot:
            if power.instance_state(aws, instance_id, allow_gone=True) not in {
                "stopped",
                "terminated",
            }:
                raise EvalveError(
                    "EC2 is running after the saved snapshot. Resolve the out-of-band restart before retrying down."
                )
        else:
            print("Draining and stopping EC2 before the database snapshot…", flush=True)
            power.stop(aws, instance_id, allow_gone=True)
    if not snapshot_id:
        return
    key = backup.snapshot_key(snapshot_id)
    manifest, _ = aws.read_document(key)
    if manifest is None:
        if not database:
            raise EvalveError(
                "The database disappeared before its restore metadata was saved."
            )
        print(f"Waiting for encrypted RDS snapshot {snapshot_id}…", flush=True)
        snapshot = backup.take_snapshot(
            aws, snapshot_id, operation["database_resource_id"], snapshot
        )
        manifest = {
            "schema_version": 1,
            "account_id": aws.account,
            "region": aws.region,
            "snapshot_id": snapshot_id,
            "database_resource_id": operation["database_resource_id"],
            "kms_key_id": snapshot.get("KmsKeyId"),
            "configuration": dict(
                operation["configuration"],
                database_engine_version=snapshot["EngineVersion"],
            ),
        }
        backup.verify_manifest(aws, manifest, snapshot_id)
        aws.create_document(key, manifest)
        if aws.read_document(key)[0] != manifest:
            raise EvalveError(
                "The verified snapshot metadata could not be read back. Retry down."
            )
    else:
        if (
            manifest["database_resource_id"] != operation["database_resource_id"]
            or manifest["configuration"]["application_secret_arn"]
            != operation["configuration"]["application_secret_arn"]
            or manifest["configuration"]["application_secret_version"]
            != operation["configuration"]["application_secret_version"]
        ):
            raise EvalveError(
                "The saved backup does not match the shutdown checkpoint."
            )
        backup.verify_manifest(aws, manifest, snapshot_id)


def down(aws: Aws, directory: Path) -> None:
    operation, etag = pending_operation(aws)
    if operation and operation["action"] != "down":
        require_idle(operation)
    state, _ = aws.read_document(STATE_KEY)
    resources = managed_resources(state)
    if not resources and operation is None:
        print(
            "No Evalve infrastructure is recorded. Saved snapshots and the state bucket are retained."
        )
        return
    server = resource_attributes(resources, "aws_instance", "server")
    database = resource_attributes(resources, "aws_db_instance", "main")
    print(f"AWS account: {aws.account} | Region: {aws.region}")
    print(
        "Evalve will stop serving traffic. The state bucket, application secret, and snapshots will be retained."
    )
    if input("Type evalve to confirm teardown: ").strip() != "evalve":
        print("Teardown cancelled.")
        return
    verify_account(aws)
    if operation is None:
        operation = new_shutdown(aws, state, server, database)
        aws.create_document(OPERATION_KEY, operation)
        current, etag = pending_operation(aws)
        if current != operation or not etag:
            raise EvalveError(
                "The shutdown checkpoint could not be read back. Retry down."
            )
    if (server and server.get("id") != operation["instance_id"]) or (
        database and database.get("resource_id") != operation["database_resource_id"]
    ):
        raise EvalveError("The deployment no longer matches the shutdown checkpoint.")
    save_backup(aws, operation, database)
    if resources:
        config = dict(
            operation["configuration"], account_id=aws.account, region=aws.region
        )
        terraform.destroy(aws, directory, config, database.get("resource_id"))
        state, _ = aws.read_document(STATE_KEY)
        if managed_resources(state):
            raise EvalveError(
                "Terraform still records managed resources. Retry down to finish cleanup."
            )
    aws.delete_document(OPERATION_KEY, etag)
    if operation["snapshot_id"]:
        print(f"Saved snapshot: {operation['snapshot_id']}")
        print(
            f"Restore with: evalve up --restore {operation['snapshot_id']} --profile {aws.profile} --region {aws.region}"
        )
        if not operation["configuration"]["server_instance_type"]:
            print(
                "Also pass --server-type when restoring. This partial deployment had no recorded EC2 size."
            )
    else:
        print("Teardown complete. The partial deployment had no database to snapshot.")
