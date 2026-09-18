"""核对基础服务和征集格式。"""

import json
import unittest
from pathlib import Path

from service import SERVICE_ID, health_payload


class BaselineContractTest(unittest.TestCase):
    def test_service_identity(self):
        self.assertEqual(health_payload()["service"], SERVICE_ID)

    def test_fixture_keeps_publication_constraints(self):
        data = json.loads(Path("fixtures/sample.json").read_text(encoding="utf-8"))
        self.assertGreaterEqual(data["minimum_video_height"], 1080)
        self.assertFalse(data["ai_generated_allowed"])


if __name__ == "__main__":
    unittest.main()
