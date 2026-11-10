"""Stage the packaged Terraform configuration and use the locked S3 backend."""

from __future__ import annotations

import fcntl
import json
import os
import shlex
import shutil
import subprocess
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from evalve import EvalveError
from evalve.aws import STATE_KEY, Aws


@contextmanager
def operation_directory(aws: Aws):
    directory = Path.home() / ".local" / "share" / "evalve" / aws.account / aws.region
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "operation.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EvalveError(
                "Another local Evalve command is using this deployment."
            ) from exc
        recovery = directory / "errored.tfstate"
        if not recovery.is_file():
            recovery = next(directory.glob("apply-*/errored.tfstate"), None)
        if recovery is not None:
            raise EvalveError(
                f"Terraform recovery state is at {recovery}. Reconcile it with S3 before retrying."
            )
        yield directory


def template_directory() -> Path:
    packaged = Path(__file__).parent / "infra"
    return (
        packaged if packaged.is_dir() else Path(__file__).resolve().parents[2] / "infra"
    )


def terraform_environment(aws: Aws, workspace: Path, directory: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in aws.env.items()
        if not key.startswith(("TF_CLI_ARGS", "TF_VAR_", "TF_LOG"))
    }
    home = Path.home() / ".aws"
    config = Path(env.get("AWS_CONFIG_FILE", home / "config")).expanduser().resolve()
    credentials = (
        Path(env.get("AWS_SHARED_CREDENTIALS_FILE", home / "credentials"))
        .expanduser()
        .resolve()
    )
    command = shlex.join(
        [
            "env",
            f"AWS_CONFIG_FILE={config}",
            f"AWS_SHARED_CREDENTIALS_FILE={credentials}",
            "aws",
            "--profile",
            aws.profile,
            "--no-cli-pager",
            "--no-cli-auto-prompt",
            "configure",
            "export-credentials",
            "--format",
            "process",
        ]
    )
    if "\n" in command or "\r" in command:
        raise EvalveError("AWS profile configuration paths must be single-line paths.")
    # Ask the AWS CLI for credentials on demand so long Terraform applies can
    # refresh a console login. Only the command, never its output, is stored here.
    process_config = workspace / "aws-config"
    process_config.write_text(
        f"[profile evalve-process]\ncredential_process = {command}\n"
    )
    return dict(
        env,
        AWS_CONFIG_FILE=str(process_config),
        AWS_SHARED_CREDENTIALS_FILE=os.devnull,
        AWS_PROFILE="evalve-process",
        AWS_REGION=aws.region,
        AWS_DEFAULT_REGION=aws.region,
        TF_DATA_DIR=str(directory / "terraform-data"),
        TF_WORKSPACE="default",
        TF_IN_AUTOMATION="1",
    )


def run_terraform(
    workspace: Path, env: dict, *arguments: str, capture: bool = False
) -> str:
    result = subprocess.run(
        ["terraform", arguments[0], "-no-color", *arguments[1:]],
        cwd=workspace,
        env=env,
        stdout=subprocess.PIPE if capture else None,
        text=True,
    )
    if result.returncode:
        raise EvalveError(
            f"Terraform {arguments[0]} failed. Review the output and retry the same Evalve command."
        )
    return result.stdout if capture else ""


@contextmanager
def terraform_workspace(aws: Aws, directory: Path, variables: dict):
    with TemporaryDirectory(prefix="apply-", dir=directory) as temporary:
        workspace = Path(temporary)
        for source in template_directory().iterdir():
            if source.is_file() and source.suffix in {".tf", ".tftpl", ".py", ".hcl"}:
                shutil.copyfile(source, workspace / source.name)
        (workspace / "backend.tf").write_text(
            "terraform {\n"
            '  backend "s3" {\n'
            f"    bucket              = {json.dumps(aws.bucket)}\n"
            f"    key                 = {json.dumps(STATE_KEY)}\n"
            f"    region              = {json.dumps(aws.region)}\n"
            f"    allowed_account_ids = [{json.dumps(aws.account)}]\n"
            "    encrypt             = true\n"
            "    use_lockfile        = true\n"
            "  }\n"
            "}\n"
        )
        (workspace / "deployment.auto.tfvars.json").write_text(json.dumps(variables))
        env = terraform_environment(aws, workspace, directory)
        run_terraform(workspace, env, "init", "-input=false", "-reconfigure")
        try:
            yield workspace, env
        except (EvalveError, KeyboardInterrupt) as exc:
            # Terraform writes this file if a remote state write fails. Keep it
            # out of the temporary directory so a retry cannot lose it.
            failed_state = workspace / "errored.tfstate"
            if failed_state.is_file():
                recovery = directory / failed_state.name
                failed_state.chmod(0o600)
                failed_state.replace(recovery)
                raise EvalveError(
                    f"Terraform could not save state to S3. Recovery state was saved to {recovery}."
                ) from exc
            raise


def saved_plan(workspace: Path, env: dict, *options: str) -> dict:
    run_terraform(
        workspace,
        env,
        "plan",
        "-input=false",
        "-lock-timeout=30s",
        "-out=deploy.tfplan",
        *options,
    )
    return json.loads(
        run_terraform(workspace, env, "show", "-json", "deploy.tfplan", capture=True)
    )


def apply_plan(workspace: Path, env: dict) -> None:
    run_terraform(
        workspace, env, "apply", "-input=false", "-lock-timeout=30s", "deploy.tfplan"
    )


def apply(aws: Aws, directory: Path, variables: dict) -> dict:
    with terraform_workspace(aws, directory, variables) as (workspace, env):
        plan = saved_plan(workspace, env)
        for resource in plan.get("resource_changes", []):
            if (
                resource["address"] == "aws_db_instance.main"
                and "delete" in resource["change"]["actions"]
            ):
                raise EvalveError(
                    "The Terraform plan would delete the database. Evalve stopped before applying it."
                )
        apply_plan(workspace, env)
        outputs = json.loads(
            run_terraform(workspace, env, "output", "-json", capture=True)
        )
        return {name: output["value"] for name, output in outputs.items()}


def unlocks_only(plan: dict, database_resource_id: str) -> bool:
    configured = next(
        (
            resource["expressions"]
            for resource in plan["configuration"]["root_module"]["resources"]
            if resource["address"] == "aws_db_instance.main"
        ),
        {},
    )
    updated = False
    for resource in plan.get("resource_changes", []):
        change = resource["change"]
        if resource.get("mode") != "managed" or change["actions"] == ["no-op"]:
            continue
        if (
            updated
            or resource["address"] != "aws_db_instance.main"
            or change["actions"] != ["update"]
            or change["before"].get("resource_id") != database_resource_id
        ):
            raise EvalveError(
                "Terraform would change another resource before teardown. Review the plan and retry."
            )
        before, after = change["before"], change["after"]
        changed = {
            key
            for key in configured
            if before.get(key) != after.get(key)
            or change.get("after_unknown", {}).get(key)
        }
        if (
            not changed
            or changed - {"deletion_protection", "skip_final_snapshot"}
            or after.get("deletion_protection") is not False
            or after.get("skip_final_snapshot") is not True
        ):
            raise EvalveError(
                "Terraform would change more than the database deletion settings. Review the plan and retry."
            )
        updated = True
    return updated


def destroy(
    aws: Aws, directory: Path, variables: dict, database_resource_id: str | None
) -> None:
    variables = dict(variables, verified_shutdown=database_resource_id is not None)
    with terraform_workspace(aws, directory, variables) as (workspace, env):
        if database_resource_id:
            plan = saved_plan(workspace, env, "-target=aws_db_instance.main")
            if unlocks_only(plan, database_resource_id):
                apply_plan(workspace, env)

        plan = saved_plan(workspace, env, "-destroy")
        for resource in plan.get("resource_changes", []):
            if resource.get("mode") != "managed":
                continue
            change = resource["change"]
            if change["actions"] not in (["no-op"], ["delete"]):
                raise EvalveError(
                    "Terraform would create or update infrastructure during teardown."
                )
            if resource.get("type") == "aws_db_instance" and change["actions"] == [
                "delete"
            ]:
                before = change["before"]
                if (
                    not database_resource_id
                    or before.get("resource_id") != database_resource_id
                    or before.get("deletion_protection") is not False
                    or before.get("skip_final_snapshot") is not True
                ):
                    raise EvalveError(
                        "The database deletion does not match the verified backup."
                    )
        apply_plan(workspace, env)
