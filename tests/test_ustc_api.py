"""Offline checks for the portal's enrollment request contract."""

import io
import json
import unittest
import urllib.parse
from unittest.mock import Mock

from ustc_api import SessionExpiredError, UstcReportClient


class EnrollmentTests(unittest.TestCase):
    def setUp(self):
        self.client = UstcReportClient()
        self.client.opener = Mock()

    def respond(self, payload):
        self.client.opener.open.return_value = io.BytesIO(
            json.dumps(payload).encode("utf-8")
        )

    def test_enroll_matches_browser_form(self):
        self.respond({"code": "0", "msg": "选课成功"})

        self.assertEqual(self.client.enroll("report-id"), (True, "选课成功"))

        request = self.client.opener.open.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.full_url,
            "https://yjs1.ustc.edu.cn/gsapp/sys/xsbgglappustc/"
            "xsbgxkController/selectBg.do",
        )
        self.assertEqual(request.data, b"data=%7B%22BGBM%22%3A%22report-id%22%7D")
        self.assertEqual(request.get_header("Origin"), "https://yjs1.ustc.edu.cn")
        self.assertEqual(request.get_header("X-requested-with"), "XMLHttpRequest")
        self.assertEqual(
            request.get_header("Content-type"),
            "application/x-www-form-urlencoded; charset=UTF-8",
        )
        self.assertEqual(
            request.get_header("Referer"),
            "https://yjs1.ustc.edu.cn/gsapp/sys/xsbgglappustc/*default/index.do",
        )

    def test_report_id_survives_json_and_form_encoding(self):
        self.respond({"code": 0})
        report_id = 'report+&="编号'
        self.client.enroll(report_id)
        request = self.client.opener.open.call_args.args[0]
        form = urllib.parse.parse_qs(request.data.decode("utf-8"))
        self.assertEqual(set(form), {"data"})
        self.assertEqual(json.loads(form["data"][0]), {"BGBM": report_id})

    def test_business_rejection_keeps_reason(self):
        self.respond({"code": "1", "msg": "人数已满"})
        self.assertEqual(self.client.enroll("report-id"), (False, "人数已满"))

    def test_expired_session_is_not_a_business_rejection(self):
        self.respond({"code": "#E214001", "msg": "not login"})
        with self.assertRaises(SessionExpiredError):
            self.client.enroll("report-id")


if __name__ == "__main__":
    unittest.main()
