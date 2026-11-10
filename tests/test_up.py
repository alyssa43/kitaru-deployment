import io
import json
import tempfile
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from evalve import EvalveError, cli, terraform
from evalve.aws import PUBLIC_ACCESS_BLOCK, Aws, AwsError

ACCOUNT = "123456789012"
SECRET = f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:evalve/application-test"


class CredentialsTest(unittest.TestCase):
    def test_prints_only_the_dashboard_login_using_the_deployed_secret_version(self):
        for version in (None, "saved-version"):
            with self.subTest(version=version):
                aws = Mock(account=ACCOUNT, region="us-east-1")
                aws.state_outputs.return_value = {
                    "deployment": {
                        "application_secret_arn": SECRET,
                        "application_secret_version": version,
                    }
                }
                aws.call.return_value = {
                    "ARN": SECRET,
                    "VersionId": version or "current-version",
                    "SecretString": json.dumps(
                        {
                            "admin_password": "test-password",
                            "jwt_signing_key": "test-signing-key",
                            "encryption_key": "test-encryption-key",
                        }
                    ),
                }
                with (
                    patch.object(cli, "check_tools") as check,
                    patch.object(cli, "Aws", return_value=aws),
                    redirect_stdout(io.StringIO()) as output,
                ):
                    self.assertEqual(cli.main(["credentials"]), 0)
                check.assert_called_once_with(needs_terraform=False)
                arguments = ("--version-id", version) if version else ()
                aws.call.assert_called_once_with(
                    "secretsmanager",
                    "get-secret-value",
                    "--secret-id",
                    SECRET,
                    *arguments,
                )
                self.assertEqual(
                    output.getvalue(), "Username: default\nPassword: test-password\n"
                )

    def test_missing_deployment_refuses_to_fetch_any_secret(self):
        aws = Mock()
        aws.state_outputs.return_value = {}
        with (
            patch.object(cli, "check_tools"),
            patch.object(cli, "Aws", return_value=aws),
            redirect_stdout(io.StringIO()) as output,
            redirect_stderr(io.StringIO()) as errors,
        ):
            self.assertEqual(cli.main(["credentials"]), 1)
        self.assertIn("Run up first", errors.getvalue())
        self.assertEqual(output.getvalue(), "")
        aws.call.assert_not_called()


class UpTest(unittest.TestCase):
    def invoke(self, args, answers, *, existing=None, caller_account=ACCOUNT):
        aws = Mock(
            account=ACCOUNT,
            region="us-east-1",
            profile="evalve",
            bucket=f"evalve-state-{ACCOUNT}-us-east-1",
        )
        aws.state_outputs.return_value = existing or {}
        aws.read_document.return_value = (None, None)
        aws.caller_account.return_value = caller_account
        aws.ensure_application_secret.return_value = SECRET
        output = io.StringIO()
        errors = io.StringIO()
        with (
            patch.object(cli, "check_tools"),
            patch.object(cli, "Aws", return_value=aws) as factory,
            patch.object(
                terraform,
                "operation_directory",
                return_value=nullcontext(Path("/unused")),
            ),
            patch.object(
                terraform, "apply", return_value={"url": "https://evalve.example.com"}
            ) as apply,
            patch.object(cli, "verify_dns") as dns,
            patch.object(cli, "wait_for_kitaru") as ready,
            patch("builtins.input", side_effect=answers),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            code = cli.main(["up", *args])
        factory.assert_called_once_with("evalve", None)
        return code, aws, apply, ready, dns, output.getvalue(), errors.getvalue()

    def test_declining_cost_review_only_reads_aws(self):
        code, aws, apply, ready, _, output, _ = self.invoke(
            ["--server-type", "t3.small", "--database-class", "db.t4g.micro"],
            ["32.50", "n"],
        )
        self.assertEqual(code, 0)
        self.assertIn("$32.50/month", output)
        self.assertIn(ACCOUNT, output)
        aws.ensure_bucket.assert_not_called()
        aws.ensure_application_secret.assert_not_called()
        apply.assert_not_called()
        ready.assert_not_called()

    def test_confirmed_first_up_bootstraps_then_applies_and_checks_readiness(self):
        code, aws, apply, ready, _, output, _ = self.invoke(
            [
                "--server-type",
                "c7i-flex.large",
                "--database-class",
                "db.t4g.micro",
                "--monthly-estimate",
                "40",
            ],
            ["yes"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(
            [call[0] for call in aws.method_calls],
            [
                "read_document",
                "state_outputs",
                "verify_server_type",
                "caller_account",
                "read_document",
                "ensure_bucket",
                "ensure_application_secret",
            ],
        )
        self.assertEqual(apply.call_args.args[2]["application_secret_arn"], SECRET)
        self.assertEqual(apply.call_args.args[2]["backup_retention_days"], 7)
        self.assertIsNone(apply.call_args.args[2]["domain"])
        ready.assert_called_once_with("https://evalve.example.com")
        self.assertIn("Kitaru dashboard:", output)

    def test_first_up_can_use_one_day_of_automated_backups(self):
        code, _, apply, _, _, output, _ = self.invoke(
            [
                "--server-type",
                "t3.small",
                "--database-class",
                "db.t4g.micro",
                "--backup-retention-days",
                "1",
                "--monthly-estimate",
                "36",
            ],
            ["yes"],
        )
        self.assertEqual(code, 0)
        self.assertEqual(apply.call_args.args[2]["backup_retention_days"], 1)
        self.assertIn("backup retention: 1 day", output)

    def test_profile_account_change_blocks_writes(self):
        code, aws, apply, _, _, _, errors = self.invoke(
            [
                "--server-type",
                "t3.small",
                "--database-class",
                "db.t4g.micro",
                "--monthly-estimate",
                "35",
            ],
            ["y"],
            caller_account="999999999999",
        )
        self.assertEqual(code, 1)
        self.assertIn("changed accounts", errors)
        aws.ensure_bucket.assert_not_called()
        apply.assert_not_called()

    def test_custom_domain_reuses_existing_sizes_and_application_secret(self):
        existing = {
            "public_ip": "203.0.113.10",
            "deployment": {
                "server_instance_type": "t3.small",
                "database_instance_class": "db.t4g.micro",
                "backup_retention_days": 1,
                "application_secret_arn": SECRET,
                "domain": None,
            },
        }
        code, aws, apply, _, dns, _, _ = self.invoke(
            ["--domain", "evalve.example.com", "--monthly-estimate", "35"],
            ["yes"],
            existing=existing,
        )
        self.assertEqual(code, 0)
        dns.assert_called_once_with("evalve.example.com", "203.0.113.10")
        aws.ensure_application_secret.assert_called_once_with(SECRET)
        self.assertEqual(apply.call_args.args[2]["domain"], "evalve.example.com")
        self.assertEqual(apply.call_args.args[2]["server_instance_type"], "t3.small")
        self.assertEqual(apply.call_args.args[2]["backup_retention_days"], 1)

    def test_custom_domain_requires_an_existing_ip(self):
        code, aws, apply, _, dns, _, errors = self.invoke(
            [
                "--domain",
                "evalve.example.com",
                "--server-type",
                "t3.small",
                "--database-class",
                "db.t4g.micro",
            ],
            [],
        )
        self.assertEqual(code, 1)
        self.assertIn("default sslip.io address", errors)
        aws.ensure_bucket.assert_not_called()
        dns.assert_not_called()
        apply.assert_not_called()


class AwsBootstrapTest(unittest.TestCase):
    def setUp(self):
        self.aws = Aws.__new__(Aws)
        self.aws.account = ACCOUNT
        self.aws.region = "us-west-2"
        self.aws.bucket = f"evalve-state-{ACCOUNT}-us-west-2"

    def test_new_bucket_is_private_and_versioned(self):
        with (
            patch.object(self.aws, "bucket_exists", return_value=False),
            patch.object(self.aws, "call") as create,
            patch.object(
                self.aws,
                "bucket_call",
                side_effect=[{"LocationConstraint": "us-west-2"}, {}, {}, {}, {}],
            ) as bucket,
        ):
            self.aws.ensure_bucket()
        create.assert_called_once_with(
            "s3api",
            "create-bucket",
            "--bucket",
            self.aws.bucket,
            "--create-bucket-configuration",
            "LocationConstraint=us-west-2",
        )
        self.assertEqual(
            [call.args[0] for call in bucket.call_args_list],
            [
                "get-bucket-location",
                "get-public-access-block",
                "put-public-access-block",
                "get-bucket-versioning",
                "put-bucket-versioning",
            ],
        )
        self.assertEqual(
            json.loads(bucket.call_args_list[2].args[-1]), PUBLIC_ACCESS_BLOCK
        )

    def test_existing_private_versioned_bucket_needs_no_writes(self):
        with (
            patch.object(self.aws, "bucket_exists", return_value=True),
            patch.object(self.aws, "call") as create,
            patch.object(
                self.aws,
                "bucket_call",
                side_effect=[
                    {"LocationConstraint": "us-west-2"},
                    {"PublicAccessBlockConfiguration": PUBLIC_ACCESS_BLOCK},
                    {"Status": "Enabled"},
                ],
            ) as bucket,
        ):
            self.aws.ensure_bucket()
        create.assert_not_called()
        self.assertTrue(
            all(call.args[0].startswith("get-") for call in bucket.call_args_list)
        )

    def test_unverified_bucket_ownership_fails_closed(self):
        with patch.object(
            self.aws, "bucket_call", side_effect=AwsError("head-bucket", "403")
        ):
            with self.assertRaisesRegex(EvalveError, "Cannot verify access"):
                self.aws.bucket_exists()

    def test_state_preview_reads_remote_outputs_without_mutation(self):
        def download(action, _, key, filename):
            self.assertEqual(action, "get-object")
            self.assertEqual(key, "evalve/terraform.tfstate")
            Path(filename).write_text(
                '{"outputs":{"public_ip":{"value":"203.0.113.10"}}}'
            )
            return {}

        with (
            patch.object(self.aws, "bucket_exists", return_value=True),
            patch.object(self.aws, "bucket_call", side_effect=download),
        ):
            self.assertEqual(self.aws.state_outputs(), {"public_ip": "203.0.113.10"})

    def test_secret_creation_uses_stdin_and_retry_reuses_arn(self):
        with patch.object(
            self.aws,
            "call",
            side_effect=[
                AwsError("describe-secret", "ResourceNotFoundException"),
                {"ARN": SECRET},
                {"ARN": SECRET, "Tags": [{"Key": "ManagedBy", "Value": "evalve"}]},
            ],
        ) as call:
            self.assertEqual(self.aws.ensure_application_secret(), SECRET)
            self.assertEqual(self.aws.ensure_application_secret(), SECRET)
        create = call.call_args_list[1]
        self.assertIn("file:///dev/stdin", create.args)
        payload = json.loads(create.kwargs["payload"])
        self.assertEqual(
            set(payload), {"jwt_signing_key", "encryption_key", "admin_password"}
        )
        self.assertTrue(all(value not in create.args for value in payload.values()))
        self.assertEqual(call.call_args_list[-1].args[1], "describe-secret")

    def test_missing_existing_secret_cannot_be_replaced(self):
        with patch.object(
            self.aws,
            "call",
            side_effect=AwsError("describe-secret", "ResourceNotFoundException"),
        ) as call:
            with self.assertRaisesRegex(EvalveError, "Recover the original secret"):
                self.aws.ensure_application_secret(SECRET)
        call.assert_called_once_with(
            "secretsmanager", "describe-secret", "--secret-id", SECRET
        )


class TerraformApplyTest(unittest.TestCase):
    def test_remote_state_write_failure_preserves_state_and_blocks_retry(self):
        aws = Mock(
            account=ACCOUNT,
            region="us-east-1",
            bucket=f"evalve-state-{ACCOUNT}-us-east-1",
            profile="evalve",
            env={},
        )

        def run(workspace, env, *arguments, capture=False):
            if arguments[0] == "apply":
                (workspace / "errored.tfstate").write_text("recoverable state")
                raise EvalveError("Terraform apply failed")
            return "{}"

        with (
            tempfile.TemporaryDirectory() as home,
            patch.object(Path, "home", return_value=Path(home)),
            patch.object(terraform, "run_terraform", side_effect=run),
        ):
            with terraform.operation_directory(aws) as directory:
                with self.assertRaisesRegex(EvalveError, "Recovery state was saved"):
                    terraform.apply(aws, directory, {"account_id": ACCOUNT})
            self.assertEqual(
                (directory / "errored.tfstate").read_text(), "recoverable state"
            )
            with self.assertRaisesRegex(EvalveError, "Reconcile it with S3"):
                with terraform.operation_directory(aws):
                    self.fail("A recovery file must block retries")

    def test_saved_plan_requires_remote_lock_and_rejects_database_replacement(self):
        aws = Mock(
            account=ACCOUNT,
            region="us-east-1",
            bucket=f"evalve-state-{ACCOUNT}-us-east-1",
            profile="evalve",
            env={},
        )
        commands = []

        def run(workspace, env, *arguments, capture=False):
            commands.append(arguments[0])
            if arguments[0] == "init":
                backend = (workspace / "backend.tf").read_text()
                self.assertIn('backend "s3"', backend)
                self.assertIn("use_lockfile        = true", backend)
                self.assertIn(f'allowed_account_ids = ["{ACCOUNT}"]', backend)
                self.assertIn(
                    "export-credentials", Path(env["AWS_CONFIG_FILE"]).read_text()
                )
                self.assertTrue((workspace / ".terraform.lock.hcl").is_file())
            if arguments[0] == "show":
                return json.dumps(
                    {
                        "resource_changes": [
                            {
                                "address": "aws_db_instance.main",
                                "change": {"actions": ["delete", "create"]},
                            }
                        ]
                    }
                )
            return ""

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(terraform, "run_terraform", side_effect=run),
        ):
            with self.assertRaisesRegex(EvalveError, "would delete the database"):
                terraform.apply(aws, Path(directory), {"account_id": ACCOUNT})
        self.assertEqual(commands, ["init", "plan", "show"])
