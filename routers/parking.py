# ============================================================
# 주차 이벤트 라우터 (파이 → FastAPI → Spring Boot)
#
# 수정사항:
#   1. image_path, ocr_error 필드 추가
#   2. Spring Boot 저장 실패 시 HTTPException으로 실패 상태코드 반환
#      → sender.py가 실패로 판단해서 재전송 큐에 저장되게
# ============================================================

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import Levenshtein
import httpx
from config import SPRING_API, PLATE_MATCH_THRESHOLD

router = APIRouter()


# ── OCR 오인식 보정 ───────────────────────────────────────
async def match_plate(ocr_plate: str) -> str:
    """
    OCR 인식 결과를 DB 등록 차량 목록과 비교해서 오인식 보정.
    Levenshtein 거리 기반으로 가장 유사한 번호판으로 교체.
    동점 후보 2개 이상이면 NULL 처리.
    """
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
    """Spring Boot에서 특정 주차 구역의 현재 상태 조회."""
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


# ── 요청 모델 ─────────────────────────────────────────────
class ParkingEvent(BaseModel):
    event:       str
    zone:        str
    plate:       Optional[str]  = None
    park_type:   Optional[str]  = "normal"
    linked_zone: Optional[str]  = None
    entry_time:  Optional[str]  = None
    exit_time:   Optional[str]  = None
    # ✅ 추가: 입차 시점 스냅샷 경로
    image_path:  Optional[str]  = None
    # ✅ 추가: OCR 인식 불가 여부
    ocr_error:   Optional[bool] = False


@router.post("/event")
async def receive_event(event: ParkingEvent):
    """파이 카메라에서 전송한 입출차 이벤트를 수신하고 Spring Boot로 전달."""
    if event.event == "entry":
        return await handle_entry(event)
    elif event.event == "exit":
        return await handle_exit(event)
    elif event.event == "update":
        return await handle_update(event)
    else:
        raise HTTPException(status_code=400, detail="Unknown event type")


# ── 입차 ──────────────────────────────────────────────────
async def handle_entry(event: ParkingEvent):
    """
    입차 이벤트 처리 흐름:
    1. 구역 중복 점유 여부 확인
    2. OCR 번호판 보정
    3. Spring Boot 입차 저장 요청 (image_path 포함)
    4. OCR null/unreadable 시 관리자 알림 전송
    5. 번호판 NULL이면 역추적 시작
    ✅ 수정: Spring Boot 저장 실패 시 HTTPException 반환
    """
    from routers.gate import start_plate_assignment

    entry_time    = event.entry_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    matched_plate = await match_plate(event.plate) if event.plate else None

    try:
        async with httpx.AsyncClient() as client:

            # 구역 중복 점유 확인
            status = await get_zone_status(event.zone)
            if status == "occupied":
                print(f"[ENTRY] {event.zone} 이미 주차중 → 오류 처리")
                # ✅ 수정: HTTPException으로 반환
                raise HTTPException(
                    status_code=409,
                    detail=f"{event.zone} 이미 주차중"
                )

            # 2칸 주차 시 연결 구역도 확인
            if event.linked_zone:
                linked_status = await get_zone_status(event.linked_zone)
                if linked_status == "occupied":
                    raise HTTPException(
                        status_code=409,
                        detail=f"{event.linked_zone} 이미 주차중 (2칸주차 불가)"
                    )

            # Spring Boot 입차 저장 (image_path 포함)
            res = await client.post(
                SPRING_API["entry"],
                json={
                    "zone":        event.zone,
                    "plate":       matched_plate,
                    "park_type":   event.park_type,
                    "linked_zone": event.linked_zone,
                    "entry_time":  entry_time,
                    # ✅ 추가: 스냅샷 경로 전달
                    "image_path":  event.image_path,
                },
                timeout=8,
            )

            # ✅ 수정: Spring Boot 저장 실패 시 HTTPException 반환
            # sender.py가 HTTP 200이 아니면 실패로 판단해서 재전송 큐에 저장
            if res.status_code == 409:
                raise HTTPException(
                    status_code=409,
                    detail=f"{event.zone} 이미 주차중"
                )

            if res.status_code >= 400:
                raise HTTPException(
                    status_code=res.status_code,
                    detail=f"Spring Boot 에러: {res.text}"
                )

            # ✅ OCR null/unreadable 시 관리자 알림 전송
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
                    print(f"[OCR ERROR] {event.zone} 오류 알림 전송 | {event.image_path}")
                except Exception as e:
                    print(f"[OCR ERROR] 알림 전송 실패: {e}")

        print(f"[ENTRY] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")

        if matched_plate is None:
            start_plate_assignment(event.zone)

        return {
            "result":      "ok",
            "event":       "entry",
            "zone":        event.zone,
            "ocr_plate":   event.plate,
            "saved_plate": matched_plate
        }

    except HTTPException:
        # HTTPException은 그대로 위로 올림
        raise
    except Exception as e:
        print(f"[ENTRY] Spring Boot 전달 실패: {e}")
        # ✅ 수정: 예외 발생 시 500 반환 → sender.py가 재전송 큐에 저장
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# ── 출차 ──────────────────────────────────────────────────
async def handle_exit(event: ParkingEvent):
    """
    출차 이벤트 처리.
    ✅ 수정: Spring Boot 저장 실패 시 HTTPException 반환
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

            # ✅ 수정: Spring Boot 저장 실패 시 HTTPException 반환
            if res.status_code >= 400:
                raise HTTPException(
                    status_code=res.status_code,
                    detail=f"Spring Boot 에러: {res.text}"
                )

        print(f"[EXIT] {event.zone}")
        return {"result": "ok", "event": "exit", "zone": event.zone}

    except HTTPException:
        raise
    except Exception as e:
        print(f"[EXIT] Spring Boot 전달 실패: {e}")
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )


# ── 번호판 업데이트 ───────────────────────────────────────
async def handle_update(event: ParkingEvent):
    """
    번호판 업데이트 처리.
    ✅ 수정: Spring Boot 저장 실패 시 HTTPException 반환
    """
    matched_plate = await match_plate(event.plate) if event.plate else None

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                SPRING_API["update_plate"],
                json={
                    "zone":  event.zone,
                    "plate": matched_plate,
                },
                timeout=8,
            )

            # ✅ 수정: Spring Boot 저장 실패 시 HTTPException 반환
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
        raise HTTPException(
            status_code=500,
            detail=str(e)
        )
