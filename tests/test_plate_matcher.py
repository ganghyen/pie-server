import unittest

from plate_matcher import evaluate_plate_match


class PlateMatcherTest(unittest.TestCase):
    def test_ambiguous_candidates_require_review(self):
        result = evaluate_plate_match(
            "12가1235",
            ["12가1234", "12가1236", "34나5678"],
            threshold=2,
        )

        self.assertIsNone(result["matched_plate"])
        self.assertEqual(result["ocr_plate"], "12가1235")
        self.assertEqual(result["candidate_list"], ["12가1234", "12가1236"])
        self.assertEqual(result["distance"], 1)
        self.assertFalse(result["auto_confirmed"])
        self.assertTrue(result["needs_review"])

    def test_clear_single_candidate_is_auto_confirmed(self):
        result = evaluate_plate_match(
            "12가1235",
            ["12가1234", "34나5678"],
            threshold=2,
        )

        self.assertEqual(result["matched_plate"], "12가1234")
        self.assertEqual(result["candidate_list"], ["12가1234"])
        self.assertEqual(result["distance"], 1)
        self.assertTrue(result["auto_confirmed"])
        self.assertFalse(result["needs_review"])


if __name__ == "__main__":
    unittest.main()
