# ============================================================
# 주차 이벤트 라우터 (파이 → FastAPI → Spring Boot)
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
    if not ocr_plate:
        return ocr_plate

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(SPRING_API["cars"], timeout=3)
            data = response.json()

            # ⚠️ 수정 필요: Spring Boot가 반환하는 형식에 맞게 수정
            # 현재: [{"c_number": "12가1234"}, ...]
            # Spring Boot 응답 형식이 다르면 아래 줄 수정
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

            # 1. 구역 현재 상태 확인
            # ⚠️ 수정 필요: Spring Boot 응답 형식에 맞게 수정
            # 현재: {"status_type": "occupied"} 형식 기대
            res = await client.get(
                f"{SPRING_API['zone_status']}/{event.zone}",
                timeout=3
            )
            zone_data = res.json()

            # ⚠️ 수정 필요: Spring Boot 응답의 상태값 키 확인
            # 현재: zone_data.get("status_type") 로 확인
            if zone_data.get("status_type") == "occupied":
                print(f"[ENTRY] {event.zone} 이미 주차중 → 오류 처리")
                return {"result": "error", "message": f"{event.zone} 이미 주차중"}

            # 2. 입차 저장 요청
            # ⚠️ 수정 필요: Spring Boot가 받는 JSON 형식에 맞게 수정
            await client.post(
                SPRING_API["entry"],
                json={
                    "zone":        event.zone,       # 주차구역
                    "plate":       matched_plate,     # 차량번호 (NULL 가능)
                    "park_type":   event.park_type,  # 주차유형
                    "linked_zone": event.linked_zone, # 2칸주차 연결구역
                    "entry_time":  entry_time,        # 입차시간
                },
                timeout=3,
            )

        print(f"[ENTRY] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")
        return {"result": "ok", "event": "entry", "zone": event.zone,
                "ocr_plate": event.plate, "saved_plate": matched_plate}

    except Exception as e:
        print(f"[ENTRY] Spring Boot 전달 실패: {e}")
        raise HTTPException(status_code=500, detail="Spring Boot 전달 실패")


# ── 출차 ──────────────────────────────────────────────────
async def handle_exit(event: ParkingEvent):
    exit_time = event.exit_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    try:
        async with httpx.AsyncClient() as client:
            # ⚠️ 수정 필요: Spring Boot가 받는 JSON 형식에 맞게 수정
            await client.post(
                SPRING_API["exit"],
                json={
                    "zone":      event.zone,     # 주차구역
                    "exit_time": exit_time,       # 출차시간
                },
                timeout=3,
            )

        print(f"[EXIT] {event.zone}")
        return {"result": "ok", "event": "exit", "zone": event.zone}

    except Exception as e:
        print(f"[EXIT] Spring Boot 전달 실패: {e}")
        raise HTTPException(status_code=500, detail="Spring Boot 전달 실패")


# ── 번호판 업데이트 ───────────────────────────────────────
async def handle_update(event: ParkingEvent):
    matched_plate = await match_plate(event.plate) if event.plate else None

    try:
        async with httpx.AsyncClient() as client:
            # ⚠️ 수정 필요: Spring Boot가 받는 JSON 형식에 맞게 수정
            await client.post(
                SPRING_API["update_plate"],
                json={
                    "zone":  event.zone,    # 주차구역
                    "plate": matched_plate, # 차량번호
                },
                timeout=3,
            )

        print(f"[UPDATE] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")
        return {"result": "ok", "event": "update", "zone": event.zone,
                "ocr_plate": event.plate, "saved_plate": matched_plate}

    except Exception as e:
        print(f"[UPDATE] Spring Boot 전달 실패: {e}")
        raise HTTPException(status_code=500, detail="Spring Boot 전달 실패")