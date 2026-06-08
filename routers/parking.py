# ============================================================
# 주차 이벤트 라우터 (파이 → FastAPI → Spring Boot)
#
# 수정사항:
#   1. image_path, ocr_error 필드 추가
#   2. Spring Boot 저장 실패 시 HTTPException으로 실패 상태코드 반환
#      → sender.py가 실패로 판단해서 재전송 큐에 저장되게
#   3. ✅ 출차 후 DB 확인/재전송 FastAPI가 전담
#      - exit 받으면 Spring Boot에 저장 후 백그라운드 감시 시작
#      - 30초마다 Spring Boot DB 조회
#      - DB OCCUPIED → exit 재전송
#      - DB EMPTY → 감시 종료
# ============================================================

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import asyncio
import Levenshtein
import httpx
from config import SPRING_API, PLATE_MATCH_THRESHOLD, APARTMENT_NO

router = APIRouter()

# ✅ 출차 후 DB 확인 설정
EXIT_VERIFY_INTERVAL = 30.0  # DB 확인 주기 (초)
EXIT_VERIFY_MAX      = 10    # 최대 재시도 횟수 (10회 × 30초 = 5분)


# ── OCR 오인식 보정 ───────────────────────────────────────
async def match_plate(ocr_plate: str) -> str:
    if not ocr_plate:
        return ocr_plate

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(SPRING_API["cars"], timeout=8)
            try:
                data = response.json()
            except Exception:
                print(f"[PlateMatch] 응답 파싱 실패 → 원본 사용")
                return ocr_plate
            registered = [car["c_number"] for car in data]

    except Exception as e:
        print(f"[PlateMatch] 차량 목록 조회 실패: {e} → 원본 사용")
        return ocr_plate

    if not registered:
        return ocr_plate

    if ocr_plate in registered:
        print(f"[PlateMatch] 완전 일치: {ocr_plate}")
        return ocr_plate

    best_distance = float("inf")
    for reg in registered:
        distance = Levenshtein.distance(ocr_plate, reg)
        if distance < best_distance:
            best_distance = distance

    same_distance = [
        r for r in registered
        if Levenshtein.distance(ocr_plate, r) == best_distance
    ]

    if len(same_distance) >= 2:
        print(f"[PlateMatch] 후보 다수: {same_distance} → NULL 처리")
        return None

    best_plate = same_distance[0]

    if best_distance <= PLATE_MATCH_THRESHOLD:
        print(f"[PlateMatch] 오인식 보정: {ocr_plate} → {best_plate}")
        return best_plate
    else:
        print(f"[PlateMatch] 매칭 실패: {ocr_plate} → 원본 사용")
        return ocr_plate


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
                print(f"[ZoneStatus] 응답 파싱 실패")
                return "unknown"
    except Exception as e:
        print(f"[ZoneStatus] 조회 실패: {e}")
        return "unknown"


# ✅ 출차 후 DB 확인 백그라운드 태스크
async def exit_verify_task(zone_name: str, exit_time: str):
    """
    출차 저장 후 DB가 EMPTY로 반영됐는지 주기적으로 확인.
    FastAPI가 전담 - 카메라는 exit 한 번만 보내면 끝.

    흐름:
      1. Spring Boot에 DB 상태 조회
      2. DB EMPTY  → 정상, 감시 종료
      3. DB OCCUPIED → Spring Boot에 exit 직접 재전송
      4. EXIT_VERIFY_MAX 초과 → 로그만 남기고 종료
    """
    print(f"[ExitVerify] {zone_name} 감시 시작 "
          f"(최대 {EXIT_VERIFY_MAX}회 × {EXIT_VERIFY_INTERVAL}초)")

    for attempt in range(1, EXIT_VERIFY_MAX + 1):
        await asyncio.sleep(EXIT_VERIFY_INTERVAL)

        # Spring Boot에서 DB 상태 조회
        db_status = await get_zone_status(zone_name)
        db_status = db_status.lower()

        print(f"[ExitVerify] {zone_name} DB 상태: {db_status} "
              f"({attempt}/{EXIT_VERIFY_MAX})")

        # DB EMPTY → 정상 반영됨
        if db_status in ("empty", "available", "unknown"):
            print(f"[ExitVerify] {zone_name} DB EMPTY 확인 → 감시 종료")
            return

        # DB OCCUPIED → Spring Boot에 exit 재전송
        print(f"[ExitVerify] {zone_name} DB 여전히 {db_status} "
              f"→ exit 재전송 ({attempt}/{EXIT_VERIFY_MAX})")
        try:
            async with httpx.AsyncClient() as client:
                res = await client.post(
                    SPRING_API["exit"],
                    json={
                        "zone":      zone_name,
                        "exit_time": exit_time,
                    },
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
    event:       str
    zone:        str
    plate:       Optional[str]  = None
    park_type:   Optional[str]  = "normal"
    linked_zone: Optional[str]  = None
    entry_time:  Optional[str]  = None
    exit_time:   Optional[str]  = None
    apartment_no: Optional[int] = None
    apartmentNo: Optional[int] = None
    a_no: Optional[int] = None
    image_path:  Optional[str]  = None
    ocr_error:   Optional[bool] = False


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
                    "zone":        event.zone,
                    "plate":       None,
                    "park_type":   event.park_type,
                    "linked_zone": event.linked_zone,
                    "entry_time":  entry_time,
                    "image_path":  None,
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

        print(f"[ENTRY QUICK] {event.zone} PARKED 상태 DB 업데이트 완료")

        from routers.gate import start_plate_assignment
        start_plate_assignment(event.zone)
        print(f"[ENTRY QUICK] {event.zone} 역추적 시작")

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
    matched_plate = await match_plate(event.plate) if event.plate else None
    apartment_no  = resolve_apartment_no(event)

    try:
        async with httpx.AsyncClient() as client:

            status = await get_zone_status(event.zone)
            if status == "occupied":
                if matched_plate:
                    print(f"[ENTRY] {event.zone} 이미 주차중 → 번호판만 업데이트")
                    res = await client.post(
                        SPRING_API["update_plate"],
                        json={"zone": event.zone, "plate": matched_plate},
                        timeout=8,
                    )
                    if event.ocr_error and event.image_path:
                        try:
                            await client.post(
                                SPRING_API["alert"],
                                json={
                                    "zone":       event.zone,
                                    "type":       "ocr_error",
                                    "candidates": f"OCR 인식 불가 | 이미지: {event.image_path}",
                                    "time":       entry_time,
                                },
                                timeout=8,
                            )
                        except Exception as e:
                            print(f"[OCR ERROR] 알림 전송 실패: {e}")

                    from routers.gate import remove_from_pending
                    remove_from_pending(matched_plate)

                    return {
                        "result":      "ok",
                        "event":       "entry_update",
                        "zone":        event.zone,
                        "saved_plate": matched_plate
                    }
                else:
                    print(f"[ENTRY] {event.zone} 이미 주차중 + 번호판 없음 → 역추적만")
                    start_plate_assignment(event.zone)
                    return {"result": "skip", "reason": "already occupied, no plate"}

            if event.linked_zone:
                linked_status = await get_zone_status(event.linked_zone)
                if linked_status == "occupied":
                    raise HTTPException(
                        status_code=409,
                        detail=f"{event.linked_zone} 이미 주차중 (2칸주차 불가)"
                    )

            res = await client.post(
                SPRING_API["entry"],
                json={
                    "zone":        event.zone,
                    "plate":       matched_plate,
                    "park_type":   event.park_type,
                    "linked_zone": event.linked_zone,
                    "entry_time":  entry_time,
                    "image_path":  event.image_path,
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
                entry_result = {}
            history_id = (
                entry_result.get("history_id") or entry_result.get("historyId")
            )

            if event.ocr_error and event.image_path:
                try:
                    alert_payload = {
                        "zone":         event.zone,
                        "type":         "ocr_error",
                        "plate":        matched_plate or event.plate,
                        "candidates":   "OCR 인식 불가",
                        "time":         entry_time,
                        "image_path":   event.image_path,
                        "apartment_no": apartment_no,
                    }
                    if history_id is not None:
                        alert_payload["history_id"] = history_id
                    await client.post(SPRING_API["alert"], json=alert_payload, timeout=8)
                    print(f"[OCR ERROR] {event.zone} 오류 알림 전송 | {event.image_path}")
                except Exception as e:
                    print(f"[OCR ERROR] 알림 전송 실패: {e}")

        print(f"[ENTRY] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")

        if matched_plate is None:
            start_plate_assignment(event.zone)
        else:
            from routers.gate import remove_from_pending
            remove_from_pending(matched_plate)

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
    """
    출차 처리 흐름:
      1. Spring Boot에 exit 저장 요청
      2. 저장 성공 후 백그라운드로 DB 확인 감시 시작
      3. DB 미반영 시 FastAPI가 알아서 Spring Boot에 재전송
      카메라는 exit 한 번만 보내면 끝
    """
    exit_time = event.exit_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                SPRING_API["exit"],
                json={
                    "zone":      event.zone,
                    "exit_time": exit_time,
                },
                timeout=8,
            )

            if res.status_code >= 400:
                raise HTTPException(
                    status_code=res.status_code,
                    detail=f"Spring Boot 에러: {res.text}"
                )

        print(f"[EXIT] {event.zone} 저장 완료 → DB 확인 감시 시작")

        # ✅ 백그라운드로 DB 확인 감시 시작
        # FastAPI가 주기적으로 Spring Boot 조회해서 미반영 시 재전송
        asyncio.create_task(
            exit_verify_task(
                zone_name=event.zone,
                exit_time=exit_time,
            )
        )

        return {"result": "ok", "event": "exit", "zone": event.zone}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[EXIT] Spring Boot 전달 실패: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── 번호판 업데이트 ───────────────────────────────────────
async def handle_update(event: ParkingEvent):
    matched_plate = await match_plate(event.plate) if event.plate else None

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                SPRING_API["update_plate"],
                json={"zone": event.zone, "plate": matched_plate},
                timeout=8,
            )

            if res.status_code >= 400:
                raise HTTPException(
                    status_code=res.status_code,
                    detail=f"Spring Boot 에러: {res.text}"
                )

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
