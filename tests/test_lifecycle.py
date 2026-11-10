import io
import json
import unittest
from contextlib import nullcontext, redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

from evalve import EvalveError, backup, cli, lifecycle, power, terraform
from evalve.aws import STATE_KEY

ACCOUNT = "123456789012"
REGION = "us-east-1"
INSTANCE = "i-0123456789abcdef0"
DB = "db-0123456789ABCDEFG"
SECRET = f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:evalve/application-test"
VERSION = "12345678-1234-1234-1234-123456789abc"
SNAPSHOT = "evalve-20261110170000-123456789abc"
CONFIG = {
    "server_instance_type": "t3.small",
    "database_instance_class": "db.t4g.micro",
    "backup_retention_days": 1,
    "application_secret_arn": SECRET,
    "application_secret_version": VERSION,
    "domain": "example.com",
}


def resource(kind, name, attributes):
    return {
        "mode": "managed",
        "type": kind,
        "name": name,
        "instances": [{"attributes": attributes}],
    }


class PowerTest(unittest.TestCase):
    def test_start_reuses_same_ec2_without_touching_database(self):
        aws = Mock()
        with (
            patch.object(power, "instance_state", return_value="stopped"),
            patch.object(power, "run_server_command") as command,
        ):
            power.start(aws, INSTANCE)
        self.assertEqual(
            [call.args for call in aws.call.call_args_list],
            [
                ("ec2", "start-instances", "--instance-ids", INSTANCE),
                ("ec2", "wait", "instance-running", "--instance-ids", INSTANCE),
            ],
        )
        command.assert_called_once_with(aws, INSTANCE, power.START)

    def test_stop_never_sends_ec2_stop_after_failed_drain(self):
        aws = Mock()
        with (
            patch.object(power, "instance_state", return_value="running"),
            patch.object(
                power, "run_server_command", side_effect=EvalveError("drain failed")
            ),
        ):
            with self.assertRaisesRegex(EvalveError, "drain failed"):
                power.stop(aws, INSTANCE)
        aws.call.assert_not_called()

    def test_stop_retry_waits_without_sending_stop_again(self):
        aws = Mock()
        with patch.object(power, "instance_state", return_value="stopping"):
            power.stop(aws, INSTANCE)
        aws.call.assert_called_once_with(
            "ec2", "wait", "instance-stopped", "--instance-ids", INSTANCE
        )


class BackupTest(unittest.TestCase):
    def setUp(self):
        self.aws = Mock(account=ACCOUNT, region=REGION)
        self.snapshot = {
            "DBSnapshotArn": f"arn:aws:rds:{REGION}:{ACCOUNT}:snapshot:{SNAPSHOT}",
            "DbiResourceId": DB,
            "DBInstanceIdentifier": "evalve",
            "SnapshotType": "manual",
            "Engine": "postgres",
            "EngineVersion": "16.12",
            "Encrypted": True,
            "Status": "available",
            "KmsKeyId": "key-id",
        }
        self.manifest = {
            "schema_version": 1,
            "account_id": ACCOUNT,
            "region": REGION,
            "snapshot_id": SNAPSHOT,
            "database_resource_id": DB,
            "kms_key_id": "key-id",
            "configuration": dict(CONFIG, database_engine_version="16.12"),
        }

    def test_backup_requires_exact_source_and_secret_version(self):
        self.aws.call.side_effect = [
            {"DBSnapshots": [self.snapshot]},
            {"KeyMetadata": {"KeyState": "Enabled"}},
            {
                "ARN": SECRET,
                "VersionId": VERSION,
                "SecretString": json.dumps(
                    {
                        "jwt_signing_key": "test",
                        "encryption_key": "test",
                        "admin_password": "test",
                    }
                ),
            },
        ]
        backup.verify_manifest(self.aws, self.manifest, SNAPSHOT)
        self.aws.call.assert_called_with(
            "secretsmanager",
            "get-secret-value",
            "--secret-id",
            SECRET,
            "--version-id",
            VERSION,
        )

    def test_same_database_name_cannot_substitute_for_resource_id(self):
        self.aws.call.return_value = {
            "DBSnapshots": [dict(self.snapshot, DbiResourceId="db-other-generation")]
        }
        with self.assertRaisesRegex(EvalveError, "does not match"):
            backup.verify_manifest(self.aws, self.manifest, SNAPSHOT)
        self.assertEqual(self.aws.call.call_count, 1)

    def test_retry_reuses_existing_snapshot_without_creating_another(self):
        self.assertEqual(
            backup.take_snapshot(self.aws, SNAPSHOT, DB, self.snapshot), self.snapshot
        )
        self.aws.call.assert_not_called()


class ShutdownTest(unittest.TestCase):
    def setUp(self):
        self.aws = Mock(account=ACCOUNT, region=REGION, profile="evalve")
        self.aws.caller_account.return_value = ACCOUNT
        self.aws.ensure_application_secret.return_value = SECRET
        self.aws.read_document.side_effect = self.read
        self.aws.create_document.side_effect = self.create
        self.aws.delete_document.side_effect = self.delete
        self.documents = {
            STATE_KEY: {
                "outputs": {"deployment": {"value": CONFIG}},
                "resources": [
                    resource(
                        "aws_instance",
                        "server",
                        {"id": INSTANCE, "instance_type": "t3.small"},
                    ),
                    resource(
                        "aws_db_instance",
                        "main",
                        {
                            "identifier": "evalve",
                            "resource_id": DB,
                            "instance_class": "db.t4g.micro",
                        },
                    ),
                ],
            }
        }
        self.events = []

    def read(self, key):
        if key.startswith("evalve/snapshots/"):
            self.events.append("read-manifest")
        return self.documents.get(key), '"etag"' if key in self.documents else None

    def create(self, key, document):
        self.assertNotIn(key, self.documents)
        self.documents[key] = document
        self.events.append(
            "checkpoint" if key == lifecycle.OPERATION_KEY else "save-manifest"
        )

    def delete(self, key, etag):
        self.assertEqual(etag, '"etag"')
        del self.documents[key]
        self.events.append("complete")

    def invoke(self, *, answer="evalve", verification_fails=False, destroy_fails=False):
        def verify(*args):
            self.events.append("verify")
            if verification_fails:
                raise EvalveError("backup verification failed")

        def destroy(*args):
            self.events.append("destroy")
            self.assertEqual(args[-1], DB)
            if destroy_fails:
                raise EvalveError("Terraform interrupted")
            self.documents[STATE_KEY] = {"resources": []}

        def snapshot(*args):
            self.events.append("snapshot")
            return {"EngineVersion": "16.12", "KmsKeyId": "key-id"}

        def find_snapshot(*args):
            return (
                {"Status": "available"}
                if any(key.startswith("evalve/snapshots/") for key in self.documents)
                else None
            )

        with (
            patch("builtins.input", return_value=answer),
            redirect_stdout(io.StringIO()) as output,
            patch.object(
                power, "stop", side_effect=lambda *a, **kw: self.events.append("drain")
            ),
            patch.object(power, "instance_state", return_value="stopped"),
            patch.object(backup, "secret_version", return_value=VERSION),
            patch.object(backup, "get_snapshot", side_effect=find_snapshot),
            patch.object(backup, "take_snapshot", side_effect=snapshot),
            patch.object(backup, "verify_manifest", side_effect=verify),
            patch.object(terraform, "destroy", side_effect=destroy),
        ):
            lifecycle.down(self.aws, Path("/unused"))
        return output.getvalue()

    def test_declining_teardown_leaves_aws_untouched(self):
        self.invoke(answer="no")
        self.assertEqual(self.events, [])
        self.aws.create_document.assert_not_called()

    def test_verified_backup_and_metadata_readback_precede_destroy(self):
        output = self.invoke()
        self.assertEqual(
            self.events,
            [
                "checkpoint",
                "drain",
                "read-manifest",
                "snapshot",
                "verify",
                "save-manifest",
                "read-manifest",
                "destroy",
                "complete",
            ],
        )
        self.assertIn("evalve up --restore", output)
        self.assertNotIn(lifecycle.OPERATION_KEY, self.documents)

    def test_verification_failure_cannot_destroy_database(self):
        with self.assertRaisesRegex(EvalveError, "verification failed"):
            self.invoke(verification_fails=True)
        self.assertNotIn("destroy", self.events)
        self.assertIn(lifecycle.OPERATION_KEY, self.documents)

    def test_interrupted_destroy_reuses_snapshot_and_reverifies(self):
        with self.assertRaisesRegex(EvalveError, "Terraform interrupted"):
            self.invoke(destroy_fails=True)
        saved = self.documents[lifecycle.OPERATION_KEY]["snapshot_id"]
        output = self.invoke()
        self.assertIn(saved, output)
        self.assertEqual(self.events.count("snapshot"), 1)
        self.assertEqual(self.events.count("verify"), 2)
        self.assertEqual(self.events.count("drain"), 1)


class RestoreTest(unittest.TestCase):
    def test_restore_cli_passes_saved_configuration_and_retains_checkpoint_until_ready(
        self,
    ):
        for healthy in (False, True):
            with self.subTest(healthy=healthy):
                aws = Mock(account=ACCOUNT, region=REGION, profile="evalve")
                aws.state_outputs.return_value = {}
                aws.caller_account.return_value = ACCOUNT
                aws.ensure_application_secret.return_value = SECRET
                result = dict(
                    CONFIG,
                    snapshot_id=SNAPSHOT,
                    domain=None,
                    database_engine_version="16.12",
                )
                with (
                    patch.object(cli, "check_tools"),
                    patch.object(cli, "Aws", return_value=aws),
                    patch("builtins.input", return_value="yes"),
                    redirect_stdout(io.StringIO()),
                    redirect_stderr(io.StringIO()),
                    patch.object(
                        lifecycle, "pending_operation", return_value=(None, None)
                    ),
                    patch.object(lifecycle, "load_restore", return_value=result),
                    patch.object(
                        lifecycle, "record_restore", return_value='"restore-etag"'
                    ),
                    patch.object(
                        terraform,
                        "operation_directory",
                        return_value=nullcontext(Path("/unused")),
                    ),
                    patch.object(
                        terraform,
                        "apply",
                        return_value={"url": "https://example.sslip.io"},
                    ) as apply,
                    patch.object(
                        cli,
                        "wait_for_kitaru",
                        side_effect=None if healthy else EvalveError("not ready"),
                    ),
                ):
                    code = cli.main(
                        ["up", "--restore", SNAPSHOT, "--monthly-estimate", "35.00"]
                    )
                variables = apply.call_args.args[2]
                self.assertEqual(variables["snapshot_id"], SNAPSHOT)
                self.assertEqual(variables["application_secret_version"], VERSION)
                self.assertEqual(variables["backup_retention_days"], 1)
                self.assertIsNone(variables["domain"])
                self.assertEqual(code, 0 if healthy else 1)
                if healthy:
                    aws.delete_document.assert_called_once_with(
                        lifecycle.OPERATION_KEY, '"restore-etag"'
                    )
                else:
                    aws.delete_document.assert_not_called()

    def test_restore_refuses_existing_resources(self):
        aws = Mock()
        aws.read_document.return_value = (
            {"resources": [resource("aws_instance", "server", {"id": INSTANCE})]},
            None,
        )
        with self.assertRaisesRegex(EvalveError, "Run down"):
            lifecycle.load_restore(aws, SNAPSHOT, None)

    def test_restore_uses_saved_secret_version_and_starts_with_sslip(self):
        aws = Mock()
        manifest = {"configuration": CONFIG}
        aws.read_document.side_effect = [
            ({"resources": []}, None),
            (manifest, '"etag"'),
        ]
        with patch.object(backup, "verify_manifest") as verify:
            result = lifecycle.load_restore(aws, SNAPSHOT, None)
        verify.assert_called_once_with(aws, manifest, SNAPSHOT)
        self.assertEqual(result, dict(CONFIG, snapshot_id=SNAPSHOT, domain=None))


class DeletionPlanTest(unittest.TestCase):
    def test_destroy_rejects_an_unverified_database_even_with_deletion_protection_off(
        self,
    ):
        wrong_database = {
            "resource_id": "db-other-generation",
            "deletion_protection": False,
            "skip_final_snapshot": True,
        }
        destroy_plan = {
            "resource_changes": [
                {
                    "mode": "managed",
                    "type": "aws_db_instance",
                    "change": {"actions": ["delete"], "before": wrong_database},
                }
            ]
        }
        with (
            patch.object(
                terraform,
                "terraform_workspace",
                return_value=nullcontext((Path("/unused"), {})),
            ),
            patch.object(terraform, "saved_plan", side_effect=[{}, destroy_plan]),
            patch.object(terraform, "unlocks_only", return_value=False),
            patch.object(terraform, "apply_plan") as apply,
        ):
            with self.assertRaisesRegex(
                EvalveError, "does not match the verified backup"
            ):
                terraform.destroy(Mock(), Path("/unused"), {}, DB)
        apply.assert_not_called()

    def test_only_deletion_guard_changes_are_permitted(self):
        before = {
            "resource_id": DB,
            "deletion_protection": True,
            "skip_final_snapshot": False,
            "engine_version": "16",
        }
        change = {
            "actions": ["update"],
            "before": before,
            "after": dict(before, deletion_protection=False, skip_final_snapshot=True),
        }
        plan = {
            "configuration": {
                "root_module": {
                    "resources": [
                        {
                            "address": "aws_db_instance.main",
                            "expressions": {key: {} for key in before},
                        }
                    ]
                }
            },
            "resource_changes": [
                {"mode": "managed", "address": "aws_db_instance.main", "change": change}
            ],
        }
        self.assertTrue(terraform.unlocks_only(plan, DB))
        change["after"]["engine_version"] = "17"
        with self.assertRaisesRegex(
            EvalveError, "more than the database deletion settings"
        ):
            terraform.unlocks_only(plan, DB)
