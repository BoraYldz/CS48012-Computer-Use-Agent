"""Tests for deterministic planner path grounding."""
import unittest

from agent.planning import apply_explicit_location_anchor, apply_plan_path_rules


class ExplicitLocationAnchorTests(unittest.TestCase):
    def test_prefixes_missing_desktop_from_all_check_paths(self):
        subgoals = [
            {"id": 0, "check_spec": {"checks": [
                {"kind": "path_exists", "path": "Proje", "is_dir": True},
            ]}},
            {"id": 1, "check_spec": {"checks": [
                {"kind": "path_exists", "path": "Proje/Girdiler", "is_dir": True},
            ]}},
        ]

        result = apply_explicit_location_anchor("In Desktop, create Proje", subgoals)

        self.assertEqual(result["anchor"], "Desktop")
        self.assertEqual(subgoals[0]["check_spec"]["checks"][0]["path"], "Desktop/Proje")
        self.assertEqual(
            subgoals[1]["check_spec"]["checks"][0]["path"],
            "Desktop/Proje/Girdiler",
        )
        self.assertEqual(len(result["changes"]), 2)

    def test_does_not_double_prefix_existing_anchor(self):
        subgoals = [{"id": 0, "check_spec": {"checks": [
            {"kind": "path_exists", "path": "Desktop/Proje", "is_dir": True},
        ]}}]

        result = apply_explicit_location_anchor("In Desktop, create Proje", subgoals)

        self.assertEqual(subgoals[0]["check_spec"]["checks"][0]["path"], "Desktop/Proje")
        self.assertEqual(result["changes"], [])

    def test_does_not_rewrite_absolute_or_traversal_paths(self):
        subgoals = [{"id": 0, "check_spec": {"checks": [
            {"kind": "path_exists", "path": "../outside", "is_dir": True},
            {"kind": "path_exists", "path": "/tmp/outside", "is_dir": True},
            {"kind": "path_exists", "path": "C:\\outside", "is_dir": True},
        ]}}]

        result = apply_explicit_location_anchor("In Desktop, create Proje", subgoals)

        paths = [c["path"] for c in subgoals[0]["check_spec"]["checks"]]
        self.assertEqual(paths, ["../outside", "/tmp/outside", "C:\\outside"])
        self.assertEqual(result["changes"], [])

    def test_does_not_infer_location_from_nonleading_mention(self):
        subgoals = [{"id": 0, "check_spec": {"checks": [
            {"kind": "path_exists", "path": "Proje", "is_dir": True},
        ]}}]

        result = apply_explicit_location_anchor("Create Proje on Desktop", subgoals)

        self.assertIsNone(result)
        self.assertEqual(subgoals[0]["check_spec"]["checks"][0]["path"], "Proje")


class ScopePathRuleTests(unittest.TestCase):
    def test_workspace_scope_does_not_add_desktop_prefix(self):
        subgoals = [{"id": 0, "check_spec": {"checks": [
            {"kind": "path_exists", "path": "Project/README.txt"}
        ]}}]
        result = apply_plan_path_rules("In Desktop create Project", subgoals, "workspace")
        self.assertEqual(result["changes"], [])
        self.assertEqual(subgoals[0]["check_spec"]["checks"][0]["path"], "Project/README.txt")

    def test_sandbox_scope_keeps_desktop_anchor_behavior(self):
        subgoals = [{"id": 0, "check_spec": {"checks": [
            {"kind": "path_exists", "path": "Project/README.txt"}
        ]}}]
        apply_plan_path_rules("In Desktop create Project", subgoals, "sandbox")
        self.assertEqual(subgoals[0]["check_spec"]["checks"][0]["path"],
                         "Desktop/Project/README.txt")


if __name__ == "__main__":
    unittest.main()
