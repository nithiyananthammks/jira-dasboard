import unittest
from unittest.mock import patch

import app


def make_ticket(summary, story_points=None, role_sp=None):
    return {
        "key": "ORANGE-1",
        "summary": summary,
        "status": "Open",
        "type": "Technical task",
        "priority": "Medium",
        "storyPoints": story_points,
        "parent": {
            "key": "ORANGE-2",
            "summary": "Parent",
            "status": "In Progress",
            "type": "Story",
            "storyPoints": None,
            "_spField": 5,
        },
        "roleSP": role_sp,
        "bugs": [{"key": "ORANGE-3", "status": "Open"}],
        "sprint": None,
    }


class QaAutomationTicketTests(unittest.TestCase):
    def test_detects_supported_qa_automation_summary_formats(self):
        summaries = [
            "QA Automation ORANGE-123",
            "QA - Automation ORANGE-123",
            "QA-Automation ORANGE-123",
            "QA – Automation ORANGE-123",
            "qa — automation orange-123",
        ]

        for summary in summaries:
            with self.subTest(summary=summary):
                self.assertTrue(
                    app.is_qa_automation_ticket({"summary": summary}))

    def test_does_not_match_manual_qa_or_other_automation_titles(self):
        summaries = [
            "QA - Referring Practice / PCP Care Journey Support",
            "QA - Regression ORANGE-123",
            "Careco Test Automation",
            "Automation follow-up",
        ]

        for summary in summaries:
            with self.subTest(summary=summary):
                self.assertFalse(
                    app.is_qa_automation_ticket({"summary": summary}))

    def test_resolve_role_sp_skips_automation_but_calculates_manual_qa(self):
        automation = make_ticket("QA - Automation ORANGE-123")
        manual = make_ticket("QA - Manual verification", story_points=3)

        with patch.object(
                app, "_lookup_sp",
                side_effect=AssertionError("automation ticket triggered SP lookup")):
            app.resolve_role_sp([automation, manual], "QA")

        self.assertIsNone(automation["roleSP"])
        self.assertEqual(manual["roleSP"], 3.0)

    def test_suppression_hides_only_automation_metrics(self):
        automation = make_ticket(
            "QA Automation ORANGE-123", story_points=2, role_sp=2)
        manual = make_ticket(
            "QA - Manual verification", story_points=2, role_sp=2)

        app.suppress_qa_automation_metrics([automation, manual])

        self.assertIsNone(automation["roleSP"])
        self.assertIsNone(automation["parent"])
        self.assertEqual(automation["bugs"], [])
        self.assertEqual(manual["roleSP"], 2)
        self.assertIsNotNone(manual["parent"])
        self.assertNotEqual(manual["bugs"], [])


if __name__ == "__main__":
    unittest.main()
