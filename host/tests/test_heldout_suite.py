"""Guards that keep the held-out tasks held out (no LLM)."""
import unittest

from tasks import files_suite as fs


class HeldoutSuiteTests(unittest.TestCase):
    def test_default_selection_excludes_heldout(self):
        self.assertFalse([t for t in fs.select() if t.tag == "heldout"])
        self.assertFalse([t for t in fs.select(tag="extra") if t.tag == "heldout"])

    def test_heldout_selectable_and_nonempty(self):
        tasks = fs.select(tag="heldout")
        self.assertGreaterEqual(len(tasks), 6)
        self.assertTrue(all(t.id.startswith("h") for t in tasks))

    def test_heldout_requests_do_not_reuse_dev_sort_phrase(self):
        for t in fs.select(tag="heldout"):
            req = t.request(fs.base_of(t)).lower()
            self.assertNotIn("leave the incoming folder empty", req)
            self.assertNotIn("organize the files inside", req)


if __name__ == "__main__":
    unittest.main()
