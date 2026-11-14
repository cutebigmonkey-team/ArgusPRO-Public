from pathlib import Path
import unittest
from types import SimpleNamespace

from release_tests import HOSTED_ONLY, verified_suite_result


class FakeTest:
    def __init__(self, identity):
        self.identity = identity

    def id(self):
        return self.identity


def result(skips=(), success=True):
    return SimpleNamespace(
        wasSuccessful=lambda: success,
        skipped=[(FakeTest(x), "fixture") for x in skips],
    )


class HostedSkipPolicyTests(unittest.TestCase):
    def test_exact_hosted_only_set_allowed_in_collect(self):
        self.assertEqual(len(HOSTED_ONLY), 17)
        self.assertTrue(
            verified_suite_result("collect", result(HOSTED_ONLY))
        )

    def test_unknown_or_missing_skip_rejected(self):
        names = sorted(HOSTED_ONLY)
        self.assertFalse(
            verified_suite_result("collect", result(names[:-1]))
        )
        self.assertFalse(
            verified_suite_result(
                "collect", result([*names, "collect.test_unknown"])
            )
        )

    def test_failed_tests_never_pass(self):
        self.assertFalse(
            verified_suite_result(
                "collect", result(HOSTED_ONLY, success=False)
            )
        )

    def test_hostless_and_drive_never_allow_skips(self):
        for name in ("hostless", "drive"):
            self.assertTrue(
                verified_suite_result(name, result())
            )
            self.assertFalse(
                verified_suite_result(name, result(HOSTED_ONLY))
            )


class HostedWorkflowParityTests(unittest.TestCase):
    def test_hosted_workflow_covers_exactly_the_allowed_skips(self):
        import ast

        root = Path(__file__).resolve().parents[2]
        workflow = (
            root / ".github/workflows/hosted-adapters.yml"
        ).read_text()

        start_marker = "          tests = [\n"
        end_marker = "          ]\n"

        self.assertEqual(workflow.count(start_marker), 1)

        contents = workflow.split(start_marker, 1)[1]
        self.assertIn(end_marker, contents)

        contents = contents.split(end_marker, 1)[0]
        configured = ast.literal_eval("[" + contents + "]")

        self.assertEqual(len(configured), 17)
        self.assertEqual(len(configured), len(set(configured)))

        expected = {"collect." + name for name in configured}
        self.assertEqual(expected, HOSTED_ONLY)
