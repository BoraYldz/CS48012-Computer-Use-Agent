import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from agent import sheet_tools as st


def sheet_rows(path, sheet):
    wb = load_workbook(path)
    try:
        return [list(r) for r in wb[sheet].iter_rows(values_only=True)]
    finally:
        wb.close()


class FilterToSheetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "o.xlsx"
        st.write(self.p, [["Order", "Amount"], ["a", 50], ["b", 250], ["c", 100], ["d", 175], ["e", 101]], "Orders")

    def tearDown(self):
        self.tmp.cleanup()

    def test_filter_sort_writes_all_matches(self):
        msg = st.filter_to_sheet(self.p, "Orders", "Big", [{"column": "Amount", "op": ">", "value": 100}],
                                 "Amount", True)
        self.assertEqual(sheet_rows(self.p, "Big"),
                         [["Order", "Amount"], ["b", 250], ["d", 175], ["e", 101]])
        self.assertIn("3 matching", msg)
        self.assertEqual(len(sheet_rows(self.p, "Orders")), 6)       # source untouched

    def test_column_projection(self):
        st.filter_to_sheet(self.p, "Orders", "Names", None, "Amount", False, ["Order"])
        self.assertEqual(sheet_rows(self.p, "Names")[0], ["Order"])
        self.assertEqual(len(sheet_rows(self.p, "Names")), 6)


class AddColumnTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.p = Path(self.tmp.name) / "t.xlsx"
        st.write(self.p, [["Task", "Owner"], ["x", "A"], ["y", "B"], ["z", "C"]], "Tasks")

    def tearDown(self):
        self.tmp.cleanup()

    def test_constant_column_writes_header_and_values(self):
        st.add_column(self.p, "Tasks", "C", "Done", value="no")
        self.assertEqual(sheet_rows(self.p, "Tasks"),
                         [["Task", "Owner", "Done"], ["x", "A", "no"], ["y", "B", "no"], ["z", "C", "no"]])

    def test_values_list_must_match_row_count(self):
        with self.assertRaises(ValueError):
            st.add_column(self.p, "Tasks", "C", "N", values=[1, 2])
        st.add_column(self.p, "Tasks", "C", "N", values=[1, 2, 3])
        self.assertEqual(sheet_rows(self.p, "Tasks")[3], ["z", "C", 3])

    def test_needs_exactly_one_of_value_or_values(self):
        with self.assertRaises(ValueError):
            st.add_column(self.p, "Tasks", "C", "N")
        with self.assertRaises(ValueError):
            st.add_column(self.p, "Tasks", "C", "N", value=1, values=[1, 2, 3])

    def test_refuses_to_overwrite_occupied_column(self):
        with self.assertRaises(ValueError):
            st.add_column(self.p, "Tasks", "B", "Owner2", value="q")


if __name__ == "__main__":
    unittest.main()
