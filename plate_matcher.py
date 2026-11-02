try:
    import Levenshtein
except ModuleNotFoundError:
    Levenshtein = None


def levenshtein_distance(left: str, right: str) -> int:
    if Levenshtein is not None:
        return Levenshtein.distance(left, right)

    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    previous_row = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current_row = [left_index]
        for right_index, right_char in enumerate(right, start=1):
            insert_cost = current_row[right_index - 1] + 1
            delete_cost = previous_row[right_index] + 1
            replace_cost = previous_row[right_index - 1] + (left_char != right_char)
            current_row.append(min(insert_cost, delete_cost, replace_cost))
        previous_row = current_row
    return previous_row[-1]


def _base_result(ocr_plate: str):
    return {
        "ocr_plate": ocr_plate,
        "matched_plate": ocr_plate,
        "candidate_list": [],
        "distance": None,
        "auto_confirmed": False,
        "needs_review": False,
    }


def evaluate_plate_match(ocr_plate: str, registered: list[str], threshold: int = 2) -> dict:
    result = _base_result(ocr_plate)
    if not ocr_plate:
        result["matched_plate"] = None
        return result

    normalized_registered = [plate for plate in registered if plate]
    if not normalized_registered:
        return result

    if ocr_plate in normalized_registered:
        result["candidate_list"] = [ocr_plate]
        result["distance"] = 0
        result["auto_confirmed"] = True
        return result

    distances = [
        (plate, levenshtein_distance(ocr_plate, plate))
        for plate in normalized_registered
    ]
    distances.sort(key=lambda item: item[1])
    best_distance = distances[0][1]
    best_candidates = [
        plate for plate, distance in distances
        if distance == best_distance
    ]

    result["candidate_list"] = best_candidates
    result["distance"] = best_distance

    if best_distance > threshold:
        result["matched_plate"] = ocr_plate
        return result

    if len(best_candidates) >= 2:
        result["matched_plate"] = None
        result["needs_review"] = True
        return result

    result["matched_plate"] = best_candidates[0]
    result["auto_confirmed"] = True
    return result
