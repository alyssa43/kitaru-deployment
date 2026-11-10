import io
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from evalve import EvalveError, readiness


class ReadinessTest(unittest.TestCase):
    def test_https_health_and_unauthenticated_401_are_required(self):
        url = "https://evalve.example.com"
        opener = Mock()
        opener.open.side_effect = [
            io.BytesIO(b'{"database":"ok"}'),
            HTTPError(url, 401, "Unauthorized", {}, None),
        ]
        with patch.object(readiness, "build_opener", return_value=opener):
            readiness.wait_for_kitaru(url)
        self.assertEqual(
            [call.args[0] for call in opener.open.call_args_list],
            [f"{url}/health", f"{url}/api/v1/sessions"],
        )

    def test_public_sessions_are_rejected(self):
        opener = Mock()
        opener.open.side_effect = [io.BytesIO(b'{"database":"ok"}'), io.BytesIO(b"[]")]
        with patch.object(readiness, "build_opener", return_value=opener):
            with self.assertRaisesRegex(EvalveError, "without credentials"):
                readiness.wait_for_kitaru("https://evalve.example.com")
