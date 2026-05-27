# ============================================================
# 주차 이벤트 라우터 (파이 → FastAPI → Spring Boot)
# ============================================================

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import Levenshtein
import httpx
from config import SPRING_API, PLATE_MATCH_THRESHOLD


router = APIRouter()


# ── OCR 오인식 보정 ───────────────────────────────────────
async def match_plate(ocr_plate: str) -> str:
    if not ocr_plate:
        return ocr_plate

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(SPRING_API["cars"], timeout=3)
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

    # 완전 일치
    if ocr_plate in registered:
        print(f"[PlateMatch] 완전 일치: {ocr_plate}")
        return ocr_plate

    # 유사도 비교
    best_distance = float("inf")
    for reg in registered:
        distance = Levenshtein.distance(ocr_plate, reg)
        if distance < best_distance:
            best_distance = distance

    same_distance = [r for r in registered if Levenshtein.distance(ocr_plate, r) == best_distance]

    # 후보 2개 이상 → NULL 처리
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
    """
    Spring Boot에서 구역 현재 상태 조회
    반환: 'empty' / 'occupied' / 'disabled' / 'unknown'
    """
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                f"{SPRING_API['zone_status']}/{zone}",
                timeout=3
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
    plate:       Optional[str] = None
    park_type:   Optional[str] = "normal"
    linked_zone: Optional[str] = None
    entry_time:  Optional[str] = None
    exit_time:   Optional[str] = None


@router.post("/event")
async def receive_event(event: ParkingEvent):
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
    entry_time    = event.entry_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    matched_plate = await match_plate(event.plate) if event.plate else None

    try:
        async with httpx.AsyncClient() as client:

            # 1. 메인 구역 상태 확인
            status = await get_zone_status(event.zone)
            if status == "occupied":
                print(f"[ENTRY] {event.zone} 이미 주차중 → 오류 처리")
                return {"result": "error", "message": f"{event.zone} 이미 주차중"}

            # 2. ★ 2칸 주차 시 linked_zone도 상태 확인
            if event.linked_zone:
                linked_status = await get_zone_status(event.linked_zone)
                if linked_status == "occupied":
                    print(f"[ENTRY] {event.linked_zone} 이미 주차중 → 2칸주차 오류 처리")
                    return {
                        "result": "error",
                        "message": f"{event.linked_zone} 이미 주차중 (2칸주차 불가)"
                    }

            # 3. 입차 저장 요청
            res = await client.post(
                SPRING_API["entry"],
                json={
                    "zone":        event.zone,
                    "plate":       matched_plate,
                    "park_type":   event.park_type,
                    "linked_zone": event.linked_zone,
                    "entry_time":  entry_time,
                },
                timeout=3,
            )

            # ★ 500 에러 방지: 응답 상태 코드 확인
            if res.status_code == 409:
                print(f"[ENTRY] {event.zone} Spring Boot CONFLICT → 이미 주차중")
                return {"result": "error", "message": f"{event.zone} 이미 주차중"}

            if res.status_code >= 400:
                print(f"[ENTRY] Spring Boot 에러: {res.status_code}")
                return {"result": "error", "message": f"Spring Boot 에러: {res.status_code}"}

        print(f"[ENTRY] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")
        return {"result": "ok", "event": "entry", "zone": event.zone,
                "ocr_plate": event.plate, "saved_plate": matched_plate}

    except Exception as e:
        print(f"[ENTRY] Spring Boot 전달 실패: {e}")
    except Exception as e:
        print(f"[ENTRY]Spring Boot fail: {e}")
        return JSONResponse(status_code+500,
            content={"result": 
            "error", "message": f"In Pie entry communication fail: {str(e)}"}
           ) 


# ── 출차 ──────────────────────────────────────────────────
async def handle_exit(event: ParkingEvent):
    exit_time = event.exit_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                SPRING_API["exit"],
                json={
                    "zone":      event.zone,
                    "exit_time": exit_time,
                },
                timeout=3,
            )

            # ★ 500 에러 방지
            if res.status_code >= 400:
                print(f"[EXIT] Spring Boot 에러: {res.status_code}")
                return {"result": "error", "message": f"Spring Boot 에러: {res.status_code}"}

        print(f"[EXIT] {event.zone}")
        return {"result": "ok", "event": "exit", "zone": event.zone}

    except Exception as e:
        print(f"[EXIT] Spring Boot 전달 실패: {e}")
    except Exception as e:
        print(f"[EXIT] Spring Boot fail: {e}")
        return JSONResponse(
            status_code=500,
            content={"result":
                "error", "message": f"In Pie Exit Communication Fail: {str(e)}"}
                )


# ── 번호판 업데이트 ───────────────────────────────────────
async def handle_update(event: ParkingEvent):
    matched_plate = await match_plate(event.plate) if event.plate else None

    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                SPRING_API["update_plate"],
                json={
                    "zone":  event.zone,
                    "plate": matched_plate,
                },
                timeout=3,
            )

            # ★ 500 에러 방지
            if res.status_code >= 400:
                print(f"[UPDATE] Spring Boot 에러: {res.status_code}")
                return {"result": "error", "message": f"Spring Boot 에러: {res.status_code}"}

        print(f"[UPDATE] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")
        return {"result": "ok", "event": "update", "zone": event.zone,
                "ocr_plate": event.plate, "saved_plate": matched_plate}

    except Exception as e:
        print(f"[UPDATE] Spring Boot 전달 실패: {e}")
    except Exception as e:
        print(f"[UPDATE] Spring Boot fail: {e}")
        return JSONResponse(
            status_code=500,
            content={"result":
                "error", "message": f" In Pie Number Update Communication fail: {str(e)}"}
                )
