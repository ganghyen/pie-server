import unittest

from routers.parking import ParkingEvent, build_update_plate_payload


class ParkingImagePayloadTest(unittest.TestCase):
    def test_update_plate_payload_keeps_snapshot_image(self):
        event = ParkingEvent(
            event="entry",
            zone="a-b1-001",
            plate="12가1235",
            image_base64="data:image/jpeg;base64,abc123",
        )
        match_result = {
            "ocr_plate": "12가1235",
            "matched_plate": "12가1234",
            "candidate_list": ["12가1234"],
            "distance": 1,
            "auto_confirmed": True,
            "needs_review": False,
        }

        payload = build_update_plate_payload(event, "12가1234", match_result)

        self.assertEqual(payload["zone"], "a-b1-001")
        self.assertEqual(payload["plate"], "12가1234")
        self.assertEqual(payload["image_base64"], "data:image/jpeg;base64,abc123")
        self.assertEqual(payload["ocr_plate"], "12가1235")
        self.assertEqual(payload["matched_plate"], "12가1234")
        self.assertEqual(payload["candidate_list"], ["12가1234"])
        self.assertEqual(payload["distance"], 1)
        self.assertTrue(payload["auto_confirmed"])
        self.assertFalse(payload["needs_review"])

    def test_review_payload_keeps_snapshot_even_without_confirmed_plate(self):
        event = ParkingEvent(
            event="entry",
            zone="a-b1-002",
            plate="12가1235",
            image_base64="data:image/jpeg;base64,review",
        )
        match_result = {
            "ocr_plate": "12가1235",
            "matched_plate": None,
            "candidate_list": ["12가1234", "12가1236"],
            "distance": 1,
            "auto_confirmed": False,
            "needs_review": True,
        }

        payload = build_update_plate_payload(event, None, match_result)

        self.assertIsNone(payload["plate"])
        self.assertEqual(payload["image_base64"], "data:image/jpeg;base64,review")
        self.assertEqual(payload["candidate_list"], ["12가1234", "12가1236"])
        self.assertTrue(payload["needs_review"])


if __name__ == "__main__":
    unittest.main()
