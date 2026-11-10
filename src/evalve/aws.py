"""AWS operations scoped to the operator's selected profile and region."""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
from tempfile import NamedTemporaryFile

from evalve import EvalveError

STATE_KEY = "evalve/terraform.tfstate"
APPLICATION_SECRET = "evalve/application"
PUBLIC_ACCESS_BLOCK = {
    "BlockPublicAcls": True,
    "IgnorePublicAcls": True,
    "BlockPublicPolicy": True,
    "RestrictPublicBuckets": True,
}
AWS_CREDENTIAL_ENV = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_DEFAULT_PROFILE",
    "AWS_PROFILE",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
)


class AwsError(EvalveError):
    def __init__(self, operation: str, code: str):
        self.code = code
        super().__init__(f"AWS {operation} failed ({code}).")


def read_application_secret(
    aws: Aws, arn: str, version: str | None
) -> tuple[dict, str]:
    prefix = f"arn:aws:secretsmanager:{aws.region}:{aws.account}:secret:"
    if not arn.startswith(prefix):
        raise EvalveError(
            "The application secret must belong to the deployment account and region."
        )
    arguments = ("--version-id", version) if version else ()
    result = aws.call(
        "secretsmanager", "get-secret-value", "--secret-id", arn, *arguments
    )
    values = json.loads(result["SecretString"])
    if not isinstance(values, dict) or not all(
        isinstance(values.get(key), str) and values[key]
        for key in ("jwt_signing_key", "encryption_key", "admin_password")
    ):
        raise EvalveError(
            "The saved application secret does not contain all required Kitaru credentials."
        )
    if result["ARN"] != arn or (version and result["VersionId"] != version):
        raise EvalveError("The saved application secret version could not be verified.")
    return values, result["VersionId"]


class Aws:
    def __init__(self, profile: str, region: str | None):
        self.profile = profile
        self.env = {
            key: value
            for key, value in os.environ.items()
            if key not in AWS_CREDENTIAL_ENV
        }
        self.env["AWS_EC2_METADATA_DISABLED"] = "true"
        self.command = [
            "aws",
            "--profile",
            profile,
            "--no-cli-pager",
            "--no-cli-auto-prompt",
        ]
        if region is None:
            result = subprocess.run(
                [*self.command, "configure", "get", "region"],
                env=self.env,
                capture_output=True,
                text=True,
            )
            region = result.stdout.strip() if result.returncode == 0 else ""
        if not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region):
            raise EvalveError(
                f"Set a region in AWS profile {profile!r} or pass --region."
            )
        self.region = region
        self.command += ["--region", region, "--output", "json"]
        self.account = self.caller_account()
        self.bucket = f"evalve-state-{self.account}-{self.region}"

    def call(
        self, service: str, action: str, *arguments: str, payload: str | None = None
    ) -> dict:
        result = subprocess.run(
            [*self.command, service, action, *arguments],
            env=self.env,
            capture_output=True,
            text=True,
            input=payload,
        )
        if result.returncode:
            # Do not echo CLI output on errors, including commands with secret input.
            match = re.search(r"An error occurred \(([A-Za-z0-9_.-]+)\)", result.stderr)
            raise AwsError(
                f"{service} {action}",
                match[1] if match else f"exit {result.returncode}",
            )
        return json.loads(result.stdout) if result.stdout.strip() else {}

    def caller_account(self) -> str:
        try:
            identity = self.call("sts", "get-caller-identity")
        except AwsError as exc:
            raise EvalveError(
                f"{exc} Check your sign-in with `aws login --profile {self.profile}`."
            ) from exc
        account = identity.get("Account", "")
        if not re.fullmatch(r"[0-9]{12}", account) or not identity.get(
            "Arn", ""
        ).startswith("arn:aws:"):
            raise EvalveError(
                "Evalve requires an identity in the commercial AWS partition."
            )
        return account

    def bucket_call(self, action: str, *arguments: str) -> dict:
        return self.call(
            "s3api",
            action,
            "--bucket",
            self.bucket,
            "--expected-bucket-owner",
            self.account,
            *arguments,
        )

    def bucket_exists(self) -> bool:
        try:
            self.bucket_call("head-bucket")
        except AwsError as exc:
            if exc.code in {"404", "NotFound", "NoSuchBucket"}:
                return False
            raise EvalveError(
                f"Cannot verify access to S3 bucket {self.bucket}. {exc}"
            ) from exc
        return True

    def state_outputs(self) -> dict:
        state, _ = self.read_document(STATE_KEY)
        return {
            name: output["value"]
            for name, output in (state or {}).get("outputs", {}).items()
        }

    def read_document(self, key: str) -> tuple[dict | None, str | None]:
        if not self.bucket_exists():
            return None, None
        # A read-only preview must not initialize the backend before confirmation.
        with NamedTemporaryFile() as state:
            try:
                response = self.bucket_call("get-object", "--key", key, state.name)
            except AwsError as exc:
                if exc.code in {"404", "NoSuchKey", "NotFound"}:
                    return None, None
                raise
            document = json.load(state)
            if not isinstance(document, dict):
                raise EvalveError(f"The S3 record {key} is not a JSON object.")
            return document, response.get("ETag")

    def create_document(self, key: str, value: dict) -> None:
        with NamedTemporaryFile(mode="w") as document:
            json.dump(value, document, sort_keys=True)
            document.flush()
            try:
                self.bucket_call(
                    "put-object",
                    "--key",
                    key,
                    "--body",
                    document.name,
                    "--server-side-encryption",
                    "AES256",
                    "--if-none-match",
                    "*",
                )
            except AwsError as exc:
                if exc.code not in {
                    "412",
                    "PreconditionFailed",
                    "409",
                    "ConditionalRequestConflict",
                }:
                    raise
                existing, _ = self.read_document(key)
                if existing != value:
                    raise EvalveError(
                        f"An existing record at {key} belongs to another operation. Retry to inspect it."
                    ) from exc

    def delete_document(self, key: str, etag: str) -> None:
        self.bucket_call("delete-object", "--key", key, "--if-match", etag)

    def ensure_bucket(self) -> None:
        if not self.bucket_exists():
            configuration = ()
            if self.region != "us-east-1":
                configuration = (
                    "--create-bucket-configuration",
                    f"LocationConstraint={self.region}",
                )
            try:
                self.call(
                    "s3api", "create-bucket", "--bucket", self.bucket, *configuration
                )
            except AwsError as exc:
                if exc.code != "BucketAlreadyOwnedByYou":
                    raise

        location = self.bucket_call("get-bucket-location").get("LocationConstraint")
        if {None: "us-east-1", "EU": "eu-west-1"}.get(
            location, location
        ) != self.region:
            raise EvalveError(f"State bucket {self.bucket} is in the wrong region.")
        try:
            block = self.bucket_call("get-public-access-block").get(
                "PublicAccessBlockConfiguration"
            )
        except AwsError as exc:
            if exc.code != "NoSuchPublicAccessBlockConfiguration":
                raise
            block = None
        if block != PUBLIC_ACCESS_BLOCK:
            self.bucket_call(
                "put-public-access-block",
                "--public-access-block-configuration",
                json.dumps(PUBLIC_ACCESS_BLOCK),
            )
        if self.bucket_call("get-bucket-versioning").get("Status") != "Enabled":
            self.bucket_call(
                "put-bucket-versioning", "--versioning-configuration", "Status=Enabled"
            )

    def ensure_application_secret(
        self, existing_arn: str | None = None, *, create: bool = True
    ) -> str:
        try:
            secret = self.call(
                "secretsmanager",
                "describe-secret",
                "--secret-id",
                existing_arn or APPLICATION_SECRET,
            )
        except AwsError as exc:
            if exc.code != "ResourceNotFoundException":
                raise
            if existing_arn or not create:
                raise EvalveError(
                    "The deployment's application secret is missing. Recover the original secret before applying."
                ) from exc
            payload = json.dumps(
                {
                    "jwt_signing_key": secrets.token_urlsafe(48),
                    "encryption_key": secrets.token_urlsafe(48),
                    "admin_password": secrets.token_urlsafe(32),
                }
            )
            try:
                return self.call(
                    "secretsmanager",
                    "create-secret",
                    "--name",
                    APPLICATION_SECRET,
                    "--secret-string",
                    "file:///dev/stdin",
                    "--tags",
                    "Key=ManagedBy,Value=evalve",
                    payload=payload,
                )["ARN"]
            except AwsError as create_error:
                if create_error.code != "ResourceExistsException":
                    raise
            secret = self.call(
                "secretsmanager", "describe-secret", "--secret-id", APPLICATION_SECRET
            )

        if secret.get("DeletedDate"):
            raise EvalveError(
                f"Secret {APPLICATION_SECRET} is scheduled for deletion. Recover it before retrying."
            )
        if {tag["Key"]: tag["Value"] for tag in secret.get("Tags", [])}.get(
            "ManagedBy"
        ) != "evalve":
            raise EvalveError(
                f"Secret {APPLICATION_SECRET} already exists without the Evalve ownership tag."
            )
        return secret["ARN"]

    def verify_server_type(self, instance_type: str) -> None:
        result = self.call(
            "ec2", "describe-instance-types", "--instance-types", instance_type
        )
        types = result.get("InstanceTypes", [])
        if (
            not types
            or "x86_64" not in types[0]["ProcessorInfo"]["SupportedArchitectures"]
        ):
            raise EvalveError(
                f"EC2 type {instance_type} must support the x86-64 Amazon Linux image."
            )
