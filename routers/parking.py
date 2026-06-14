# 파일: ~/parking_project/pie/pie/server/routers/parking.py

# ============================================================
# 주차 이벤트 라우터 (파이 → FastAPI → Spring Boot)
# ============================================================

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import asyncio
import httpx
from config import SPRING_API, PLATE_MATCH_THRESHOLD, APARTMENT_NO, AISLE_ZONES
from plate_matcher import evaluate_plate_match

# ✅ 구역별 linked_zone 메모리 (역추적 시 사용)
zone_linked_map: dict[str, str] = {}

# ✅ 구역별 확정 번호판 메모리 (update 중복 방지용)
# entry/entry_quick 때 저장, exit 때 제거
# Spring Boot DB 조회 없이 FastAPI 메모리로 체크
zone_plate_map: dict[str, str] = {}

router = APIRouter()

EXIT_VERIFY_INTERVAL = 30.0
EXIT_VERIFY_MAX      = 10


# ── 통로 구역 여부 확인 (하드코딩된 목록 기준) ────────────
def is_aisle_zone(zone_name: str) -> bool:
    return zone_name in AISLE_ZONES


# ── OCR 오인식 보정 ───────────────────────────────────────
async def match_plate(ocr_plate: str) -> dict:
    if not ocr_plate:
        return evaluate_plate_match(ocr_plate, [], PLATE_MATCH_THRESHOLD)

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(SPRING_API["cars"], timeout=8)
            try:
                data = response.json()
            except Exception:
                return evaluate_plate_match(ocr_plate, [], PLATE_MATCH_THRESHOLD)
            registered = [car["c_number"] for car in data]
    except Exception as e:
        print(f"[PlateMatch] 차량 목록 조회 실패: {e} → 원본 사용")
        return evaluate_plate_match(ocr_plate, [], PLATE_MATCH_THRESHOLD)

    result = evaluate_plate_match(ocr_plate, registered, PLATE_MATCH_THRESHOLD)
    if result["auto_confirmed"] and result["distance"] == 0:
        print(f"[PlateMatch] 완전 일치: {ocr_plate}")
    elif result["auto_confirmed"]:
        print(f"[PlateMatch] 오인식 보정: {ocr_plate} → {result['matched_plate']}")
    elif result["needs_review"]:
        print(f"[PlateMatch] 후보 검토 필요: {ocr_plate} → {result['candidate_list']}")
    else:
        print(f"[PlateMatch] 매칭 실패: {ocr_plate} → 원본 사용")
    return result


def matched_plate_value(match_result: dict | None) -> str | None:
    if not match_result:
        return None
    if match_result.get("needs_review"):
        return None
    return match_result.get("matched_plate")


def correction_payload(match_result: dict | None) -> dict:
    if not match_result:
        return {}
    return {
        "ocr_plate":      match_result.get("ocr_plate"),
        "matched_plate":  match_result.get("matched_plate"),
        "candidate_list": match_result.get("candidate_list") or [],
        "distance":       match_result.get("distance"),
        "auto_confirmed": bool(match_result.get("auto_confirmed")),
        "needs_review":   bool(match_result.get("needs_review")),
    }


def build_update_plate_payload(
        event: "ParkingEvent",
        plate: str | None,
        match_result: dict | None = None,
) -> dict:
    payload = {
        "zone":  event.zone,
        "plate": plate,
        **correction_payload(match_result),
    }
    if event.image_base64:
        payload["image_base64"] = event.image_base64
    return payload


# ── 구역 상태 조회 ────────────────────────────────────────
async def get_zone_status(zone: str) -> str:
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{SPRING_API['zone_status']}/{zone}",
                timeout=8
            )
            try:
                zone_data = res.json()
                return zone_data.get("status_type", "unknown")
            except Exception:
                return "unknown"
    except Exception as e:
        print(f"[ZoneStatus] 조회 실패: {e}")
        return "unknown"


# ── 출차 후 DB 확인 백그라운드 태스크 ────────────────────
async def exit_verify_task(zone_name: str, exit_time: str):
    print(f"[ExitVerify] {zone_name} 감시 시작")

    for attempt in range(1, EXIT_VERIFY_MAX + 1):
        await asyncio.sleep(EXIT_VERIFY_INTERVAL)

        db_status = (await get_zone_status(zone_name)).lower()
        print(f"[ExitVerify] {zone_name} DB: {db_status} ({attempt}/{EXIT_VERIFY_MAX})")

        if db_status in ("empty", "available", "unknown"):
            print(f"[ExitVerify] {zone_name} EMPTY 확인 → 감시 종료")
            return

        print(f"[ExitVerify] {zone_name} 여전히 {db_status} → exit 재전송")
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    SPRING_API["exit"],
                    json={"zone": zone_name, "exit_time": exit_time},
                    timeout=8,
                )
            if res.status_code >= 400:
                print(f"[ExitVerify] {zone_name} 재전송 실패 ({res.status_code})")
            else:
                print(f"[ExitVerify] {zone_name} 재전송 성공")
        except Exception as e:
            print(f"[ExitVerify] {zone_name} 재전송 오류: {e}")

    print(f"[ExitVerify] {zone_name} 최대 재시도 초과 → 수동 확인 필요")


# ── 요청 모델 ─────────────────────────────────────────────
class ParkingEvent(BaseModel):
    event:        str
    zone:         str
    plate:        Optional[str]  = None
    park_type:    Optional[str]  = "normal"
    linked_zone:  Optional[str]  = None
    entry_time:   Optional[str]  = None
    exit_time:    Optional[str]  = None
    apartment_no: Optional[int]  = None
    apartmentNo:  Optional[int]  = None
    a_no:         Optional[int]  = None
    image_base64: Optional[str]  = None
    ocr_error:    Optional[bool] = False


def resolve_apartment_no(event: ParkingEvent) -> int:
    return event.apartment_no or event.apartmentNo or event.a_no or APARTMENT_NO


@router.post("/event")
async def receive_event(event: ParkingEvent):
    if event.event == "entry_quick":
        return await handle_entry_quick(event)
    elif event.event == "entry":
        return await handle_entry(event)
    elif event.event == "exit":
        return await handle_exit(event)
    elif event.event == "update":
        return await handle_update(event)
    else:
        raise HTTPException(status_code=400, detail="Unknown event type")


# ── 입차 즉시 PARKED 상태 전송 ────────────────────────────
async def handle_entry_quick(event: ParkingEvent):
    entry_time = event.entry_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                SPRING_API["entry"],
                json={
                    "zone":         event.zone,
                    "plate":        None,
                    "park_type":    event.park_type,
                    "linked_zone":  None,
                    "entry_time":   entry_time,
                    "image_base64": None,
                },
                timeout=8,
            )

            if res.status_code == 409:
                print(f"[ENTRY QUICK] {event.zone} 이미 주차중 → 무시")
                return {"result": "skip", "reason": "already occupied"}

            if res.status_code >= 400:
                raise HTTPException(
                    status_code=res.status_code,
                    detail=f"Spring Boot 에러: {res.text}"
                )

        # ✅ linked_zone 쌍 메모리에 저장
        if event.linked_zone:
            zone_linked_map[event.zone]        = event.linked_zone
            zone_linked_map[event.linked_zone] = event.zone
            print(f"[LINKED] {event.zone} ↔ {event.linked_zone} 쌍 저장")

        print(f"[ENTRY QUICK] {event.zone} PARKED 상태 DB 업데이트 완료")

        # ✅ 통로구역(하드코딩 목록) 또는 통로주차(카메라 판단)면
        # OCR 기다리지 않고 즉시 역추적 시작
        zone_is_aisle = is_aisle_zone(event.zone)
        if zone_is_aisle or event.park_type == "aisle_block":
            from routers.gate import start_plate_assignment
            start_plate_assignment(event.zone, immediate=True)
            reason = "통로구역(하드코딩)" if zone_is_aisle else "통로주차(카메라)"
            print(f"[ENTRY QUICK] {event.zone} {reason} → 역추적 즉시 시작")

        return {"result": "ok", "event": "entry_quick", "zone": event.zone}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ENTRY QUICK] Spring Boot 전달 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── 입차 ──────────────────────────────────────────────────
async def handle_entry(event: ParkingEvent):
    from routers.gate import start_plate_assignment

    entry_time    = event.entry_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    plate_match   = await match_plate(event.plate) if event.plate else None
    matched_plate = matched_plate_value(plate_match)
    apartment_no  = resolve_apartment_no(event)

    try:
        async with httpx.AsyncClient() as client:

            status = await get_zone_status(event.zone)
            if status == "occupied":
                if matched_plate:
                    print(f"[ENTRY] {event.zone} 이미 주차중 → 번호판만 업데이트")
                    await client.post(
                        SPRING_API["update_plate"],
                        json=build_update_plate_payload(event, matched_plate, plate_match),
                        timeout=8,
                    )

                    zone_plate_map[event.zone] = matched_plate
                    print(f"[PlateMap] {event.zone} → {matched_plate} 저장")

                    if event.ocr_error and event.image_base64:
                        try:
                            await client.post(
                                SPRING_API["alert"],
                                json={
                                    "zone":       event.zone,
                                    "type":       "ocr_error",
                                    "candidates": "OCR 인식 불가",
                                    "time":       entry_time,
                                },
                                timeout=8,
                            )
                        except Exception as e:
                            print(f"[OCR ERROR] 알림 전송 실패: {e}")

                    from routers.gate import remove_from_pending
                    remove_from_pending(matched_plate)

                    linked = zone_linked_map.get(event.zone)
                    if linked and matched_plate:
                        try:
                            await client.post(
                                SPRING_API["update_plate"],
                                json={"zone": linked, "plate": matched_plate},
                                timeout=8,
                            )
                            zone_plate_map[linked] = matched_plate
                            print(f"[LINKED] {event.zone} 번호판 {matched_plate} → {linked} 전송")
                        except Exception as e:
                            print(f"[LINKED] {linked} 번호판 전송 실패: {e}")

                    return {
                        "result":      "ok",
                        "event":       "entry_update",
                        "zone":        event.zone,
                        "saved_plate": matched_plate
                    }
                elif plate_match and plate_match.get("needs_review"):
                    print(f"[ENTRY] {event.zone} 이미 주차중 + 후보 검토 필요 → 검토 기록 저장")
                    await client.post(
                        SPRING_API["update_plate"],
                        json=build_update_plate_payload(event, None, plate_match),
                        timeout=8,
                    )
                    return {"result": "review_required", "zone": event.zone}
                else:
                    if event.ocr_error and event.image_base64:
                        print(f"[ENTRY] {event.zone} 이미 주차중 + OCR 실패 이미지 → 주차 이력 이미지 업데이트")
                        await client.post(
                            SPRING_API["update_plate"],
                            json=build_update_plate_payload(event, None, plate_match),
                            timeout=8,
                        )
                    print(f"[ENTRY] {event.zone} 이미 주차중 + 번호판 없음 → 역추적")
                    start_plate_assignment(event.zone)
                    return {"result": "skip", "reason": "already occupied, no plate"}

            res = await client.post(
                SPRING_API["entry"],
                json={
                    "zone":         event.zone,
                    "plate":        matched_plate,
                    "park_type":    event.park_type,
                    "linked_zone":  None,
                    "entry_time":   entry_time,
                    "image_base64": event.image_base64,
                    **correction_payload(plate_match),
                },
                timeout=8,
            )

            if res.status_code == 409:
                raise HTTPException(status_code=409, detail=f"{event.zone} 이미 주차중")

            if res.status_code >= 400:
                raise HTTPException(
                    status_code=res.status_code,
                    detail=f"Spring Boot 에러: {res.text}"
                )

            entry_result = {}
            try:
                entry_result = res.json()
            except Exception:
                pass
            history_id = (
                entry_result.get("history_id") or entry_result.get("historyId")
            )

            if event.ocr_error and event.image_base64:
                try:
                    alert_payload = {
                        "zone":         event.zone,
                        "type":         "ocr_error",
                        "plate":        matched_plate or event.plate,
                        "candidates":   "OCR 인식 불가",
                        "time":         entry_time,
                        "apartment_no": apartment_no,
                    }
                    if history_id is not None:
                        alert_payload["history_id"] = history_id
                    await client.post(SPRING_API["alert"], json=alert_payload, timeout=8)
                    print(f"[OCR ERROR] {event.zone} 오류 알림 전송")
                except Exception as e:
                    print(f"[OCR ERROR] 알림 전송 실패: {e}")

        print(f"[ENTRY] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")

        if matched_plate is None:
            print(f"[ENTRY] {event.zone} 번호판 없음 → 역추적 시작")
            start_plate_assignment(event.zone)
        else:
            zone_plate_map[event.zone] = matched_plate
            print(f"[PlateMap] {event.zone} → {matched_plate} 저장")

            from routers.gate import remove_from_pending
            remove_from_pending(matched_plate)

            linked = zone_linked_map.get(event.zone)
            if linked and matched_plate:
                try:
                    async with httpx.AsyncClient() as client:
                        await client.post(
                            SPRING_API["update_plate"],
                            json={"zone": linked, "plate": matched_plate},
                            timeout=8,
                        )
                    zone_plate_map[linked] = matched_plate
                    print(f"[LINKED] {event.zone} 번호판 {matched_plate} → {linked} 전송")
                except Exception as e:
                    print(f"[LINKED] {linked} 번호판 전송 실패: {e}")

        return {
            "result":      "ok",
            "event":       "entry",
            "zone":        event.zone,
            "ocr_plate":   event.plate,
            "saved_plate": matched_plate
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[ENTRY] Spring Boot 전달 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── 출차 ──────────────────────────────────────────────────
async def handle_exit(event: ParkingEvent):
    exit_time = event.exit_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                SPRING_API["exit"],
                json={"zone": event.zone, "exit_time": exit_time},
                timeout=8,
            )

            if res.status_code >= 400:
                raise HTTPException(
                    status_code=res.status_code,
                    detail=f"Spring Boot 에러: {res.text}"
                )

        print(f"[EXIT] {event.zone} 저장 완료 → DB 확인 감시 시작")

        asyncio.create_task(
            exit_verify_task(zone_name=event.zone, exit_time=exit_time)
        )

        if event.plate:
            from routers.gate import remove_from_pending
            remove_from_pending(event.plate)

        removed_plate = zone_plate_map.pop(event.zone, None)
        if removed_plate:
            print(f"[PlateMap] {event.zone} → {removed_plate} 제거")

        linked = zone_linked_map.pop(event.zone, None)
        if linked:
            zone_linked_map.pop(linked, None)
            zone_plate_map.pop(linked, None)
            print(f"[LINKED] {event.zone} ↔ {linked} 쌍 제거")

        return {"result": "ok", "event": "exit", "zone": event.zone}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[EXIT] Spring Boot 전달 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── 번호판 업데이트 ───────────────────────────────────────
async def handle_update(event: ParkingEvent):
    plate_match   = await match_plate(event.plate) if event.plate else None
    matched_plate = matched_plate_value(plate_match)

    existing_plate = zone_plate_map.get(event.zone)
    if existing_plate and existing_plate not in (None, "", "UNKNOWN"):
        print(f"[UPDATE] {event.zone} 메모리에 번호판 있음({existing_plate}) → 스킵")
        return {"result": "skip", "reason": "plate already exists", "plate": existing_plate}

    if not matched_plate and not (plate_match and plate_match.get("needs_review")):
        print(f"[UPDATE] {event.zone} OCR 결과 없음 → 스킵")
        return {"result": "skip", "reason": "no plate to update"}

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                SPRING_API["update_plate"],
                json=build_update_plate_payload(event, matched_plate, plate_match),
                timeout=8,
            )
            if res.status_code >= 400:
                raise HTTPException(
                    status_code=res.status_code,
                    detail=f"Spring Boot 에러: {res.text}"
                )

            zone_plate_map[event.zone] = matched_plate
            print(f"[PlateMap] {event.zone} → {matched_plate} 저장 (update)")

            linked = zone_linked_map.get(event.zone) or event.linked_zone
            if linked and matched_plate:
                try:
                    await client.post(
                        SPRING_API["update_plate"],
                        json={"zone": linked, "plate": matched_plate},
                        timeout=8,
                    )
                    zone_plate_map[linked] = matched_plate
                    print(f"[UPDATE] linked {linked} → {matched_plate} 업데이트")
                except Exception as e:
                    print(f"[UPDATE] linked {linked} 업데이트 실패: {e}")

        print(f"[UPDATE] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")
        return {
            "result":      "ok",
            "event":       "update",
            "zone":        event.zone,
            "ocr_plate":   event.plate,
            "saved_plate": matched_plate
        }

    except HTTPException:
        raise
    except Exception as e:
        print(f"[UPDATE] Spring Boot 전달 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))