import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from infra import refresh


class RefreshTest(unittest.TestCase):
    config = {
        "application_secret_arn": "app-secret",
        "database_secret_arn": "db-secret",
        "database_host": "db.example.com",
        "hostname": "evalve.example.com",
        "region": "us-east-1",
    }
    application = {
        "jwt_signing_key": "signing-key",
        "encryption_key": "encryption-key",
        "admin_password": "bootstrap-password",
    }

    def test_rotated_password_replaces_private_file_and_restarts_server(self):
        database = {"username": "evalve_admin", "password": "old"}
        secrets = {"app-secret": self.application, "db-secret": database}

        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                refresh, "get_secret", side_effect=lambda arn, region: secrets[arn]
            ),
            patch.object(refresh.subprocess, "run") as run,
        ):
            destination = Path(directory) / "server.env"
            refresh.refresh(self.config, destination)
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            self.assertIn("KITARU_SERVER_DB_PWD=old\n", destination.read_text())

            refresh.refresh(self.config, destination, restart=True)
            run.assert_not_called()

            database["password"] = "rotated"
            refresh.refresh(self.config, destination, restart=True)
            run.assert_called_once_with(
                ["systemctl", "try-restart", "evalve-kitaru"], check=True
            )
            self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
            env = destination.read_text()
            self.assertIn("KITARU_SERVER_DB_PWD=rotated\n", env)
            self.assertIn("KITARU_SERVER_DB_SSL_MODE=verify-full\n", env)
            self.assertIn("KITARU_SERVER_AUTH_SCHEME=local\n", env)
            self.assertIn("KITARU_SERVER_AUTH_COOKIE_SECURE=true\n", env)
            self.assertIn("KITARU_SERVER_SERVER_URL=https://evalve.example.com\n", env)

    def test_secret_cannot_add_environment_variables(self):
        database = {"username": "evalve_admin", "password": "unsafe\nINJECTED=value"}
        with self.assertRaises(ValueError):
            refresh.build_environment(self.config, self.application, database)

    def test_restore_fetches_pinned_application_version_and_current_database_secret(
        self,
    ):
        config = dict(self.config, application_secret_version="saved-version")
        database = {"username": "evalve_admin", "password": "restored-db-password"}
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(
                refresh, "get_secret", side_effect=[self.application, database]
            ) as read,
        ):
            refresh.refresh(config, Path(directory) / "server.env")
        self.assertEqual(
            [call.args for call in read.call_args_list],
            [
                ("app-secret", "us-east-1", "--version-id", "saved-version"),
                ("db-secret", "us-east-1"),
            ],
        )
