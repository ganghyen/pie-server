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


async def match_plate(ocr_plate: str) -> str:
    """
    OCR 인식 결과를 DB 등록 차량 목록과 비교해서 오인식 보정.
    Levenshtein 거리 기반으로 가장 유사한 번호판으로 교체.
    후보가 2개 이상으로 동점이면 NULL 처리 (모호한 경우 안전하게).
    """
    if not ocr_plate:
        return ocr_plate

    try:
        async with httpx.AsyncClient() as client:
            # Spring Boot에서 등록 차량 전체 목록 조회
            response = await client.get(SPRING_API["cars"], timeout=8)
            try:
                data = response.json()
            except Exception:
                print(f"[PlateMatch] 응답 파싱 실패 → 원본 사용")
                return ocr_plate

            # 번호판 문자열만 추출
            registered = [car["c_number"] for car in data]

    except Exception as e:
        print(f"[PlateMatch] 차량 목록 조회 실패: {e} → 원본 사용")
        return ocr_plate

    if not registered:
        return ocr_plate

    # 완전 일치하는 경우 바로 반환
    if ocr_plate in registered:
        print(f"[PlateMatch] 완전 일치: {ocr_plate}")
        return ocr_plate

    # 모든 등록 차량과 Levenshtein 거리 계산 후 최솟값 찾기
    best_distance = float("inf")
    for reg in registered:
        distance = Levenshtein.distance(ocr_plate, reg)
        if distance < best_distance:
            best_distance = distance

    # 최솟값과 같은 거리의 후보 목록
    same_distance = [r for r in registered if Levenshtein.distance(ocr_plate, r) == best_distance]

    if len(same_distance) >= 2:
        # 동점 후보가 2개 이상 → 어느 차인지 특정 불가, NULL 처리
        print(f"[PlateMatch] 후보 다수: {same_distance} → NULL 처리")
        return None

    best_plate = same_distance[0]

    if best_distance <= PLATE_MATCH_THRESHOLD:
        # 거리가 임계값 이하 → 보정 적용
        print(f"[PlateMatch] 오인식 보정: {ocr_plate} → {best_plate}")
        return best_plate
    else:
        # 거리가 너무 크면 원본 그대로 사용
        print(f"[PlateMatch] 매칭 실패: {ocr_plate} → 원본 사용")
        return ocr_plate


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


# 파이에서 받는 이벤트 요청 모델
class ParkingEvent(BaseModel):
    event:       str                  # "entry" / "exit" / "update"
    zone:        str                  # 구역 이름 (예: "a-b1-001")
    plate:       Optional[str] = None # 번호판 (없으면 null)
    park_type:   Optional[str] = "normal"   # 주차 유형
    linked_zone: Optional[str] = None       # 2칸 주차 연결 구역
    entry_time:  Optional[str] = None       # 입차 시각
    exit_time:   Optional[str] = None       # 출차 시각


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


async def handle_entry(event: ParkingEvent):
    """
    입차 이벤트 처리 흐름:
    1. 구역 중복 점유 여부 확인
    2. OCR 번호판 보정
    3. Spring Boot 입차 저장 요청
    4. 번호판 NULL이면 역추적 시작
    """
    # gate 모듈 import (순환 import 방지를 위해 함수 내부에서)
    from routers.gate import start_plate_assignment

    # 입차 시각 없으면 현재 시각 사용
    entry_time    = event.entry_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    # OCR 결과 번호판 보정
    matched_plate = await match_plate(event.plate) if event.plate else None

    try:
        async with httpx.AsyncClient() as client:

            # 1. 메인 구역 현재 상태 확인 (이미 점유 중이면 중복 입차 방지)
            status = await get_zone_status(event.zone)
            if status == "occupied":
                print(f"[ENTRY] {event.zone} 이미 주차중 → 오류 처리")
                return {"result": "error", "message": f"{event.zone} 이미 주차중"}

            # 2. 2칸 주차 시 연결 구역도 점유 여부 확인
            if event.linked_zone:
                linked_status = await get_zone_status(event.linked_zone)
                if linked_status == "occupied":
                    print(f"[ENTRY] {event.linked_zone} 이미 주차중 → 2칸주차 오류 처리")
                    return {
                        "result": "error",
                        "message": f"{event.linked_zone} 이미 주차중 (2칸주차 불가)"
                    }

            # 3. Spring Boot 입차 저장 요청
            res = await client.post(
                SPRING_API["entry"],
                json={
                    "zone":        event.zone,
                    "plate":       matched_plate,
                    "park_type":   event.park_type,
                    "linked_zone": event.linked_zone,
                    "entry_time":  entry_time,
                },
                timeout=8,
            )

            # 409 Conflict: Spring Boot에서 이미 점유 중으로 판단
            if res.status_code == 409:
                print(f"[ENTRY] {event.zone} Spring Boot CONFLICT → 이미 주차중")
                return {"result": "error", "message": f"{event.zone} 이미 주차중"}

            if res.status_code >= 400:
                print(f"[ENTRY] Spring Boot 에러: {res.status_code}")
                return {"result": "error", "message": f"Spring Boot 에러: {res.status_code}"}

        print(f"[ENTRY] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")

        # 4. 번호판이 NULL이면 입구 카메라 기록과 역추적 매칭 시작
        if matched_plate is None:
            print(f"[ENTRY] {event.zone} 번호판 NULL → 역추적 시작")
            start_plate_assignment(event.zone)

        return {"result": "ok", "event": "entry", "zone": event.zone,
                "ocr_plate": event.plate, "saved_plate": matched_plate}

    except Exception as e:
        print(f"[ENTRY] Spring Boot 전달 실패: {e}")
        return {"result": "fail", "message": str(e)}


async def handle_exit(event: ParkingEvent):
    """출차 이벤트: 출차 시각과 함께 Spring Boot로 출차 저장 요청."""
    # 출차 시각 없으면 현재 시각 사용
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
                print(f"[EXIT] Spring Boot 에러: {res.status_code}")
                return {"result": "error", "message": f"Spring Boot 에러: {res.status_code}"}

        print(f"[EXIT] {event.zone}")
        return {"result": "ok", "event": "exit", "zone": event.zone}

    except Exception as e:
        print(f"[EXIT] Spring Boot 전달 실패: {e}")
        return {"result": "fail", "message": str(e)}


async def handle_update(event: ParkingEvent):
    """번호판 업데이트: OCR 보정 후 Spring Boot로 번호판 갱신 요청."""
    # OCR 결과 보정
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

            if res.status_code >= 400:
                print(f"[UPDATE] Spring Boot 에러: {res.status_code}")
                return {"result": "error", "message": f"Spring Boot 에러: {res.status_code}"}

        print(f"[UPDATE] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")
        return {"result": "ok", "event": "update", "zone": event.zone,
                "ocr_plate": event.plate, "saved_plate": matched_plate}

    except Exception as e:
        print(f"[UPDATE] Spring Boot 전달 실패: {e}")
        return {"result": "fail", "message": str(e)}