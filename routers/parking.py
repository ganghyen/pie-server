# ============================================================
# 주차 이벤트 라우터 (파이 → 서버)
# 입차 / 출차 / 번호판 업데이트
# parking_zone: current_car_number 컬럼 사용 (앱 팀 요청 형식)
# ============================================================

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import Levenshtein
from config import PLATE_MATCH_THRESHOLD

router = APIRouter()
pool   = None


# ── OCR 오인식 보정 ───────────────────────────────────────
async def match_plate(ocr_plate: str) -> str:
    if not ocr_plate:
        return ocr_plate

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT c_number FROM car")
            rows = await cur.fetchall()

    if not rows:
        return ocr_plate

    registered = [row[0] for row in rows]

    if ocr_plate in registered:
        print(f"[PlateMatch] 완전 일치: {ocr_plate}")
        return ocr_plate

    best_distance = float("inf")
    for reg in registered:
        distance = Levenshtein.distance(ocr_plate, reg)
        if distance < best_distance:
            best_distance = distance

    same_distance = [r for r in registered if Levenshtein.distance(ocr_plate, r) == best_distance]
    if len(same_distance) >= 2:
        print(f"[PlateMatch] 후보 다수: {same_distance} → 원본 사용")
        return ocr_plate

    best_plate = same_distance[0]

    if best_distance <= PLATE_MATCH_THRESHOLD:
        print(f"[PlateMatch] 오인식 보정: {ocr_plate} → {best_plate} (거리:{best_distance})")
        return best_plate
    else:
        print(f"[PlateMatch] 매칭 실패: {ocr_plate} → 원본 사용")
        return ocr_plate


# ── 요청 모델 ─────────────────────────────────────────────
class ParkingEvent(BaseModel):
    event:       str
    zone:        str
    plate:       Optional[str] = None
    park_status: Optional[str] = "normal"
    linked_zone: Optional[str] = None
    entry_time:  Optional[str] = None
    exit_time:   Optional[str] = None


# ── 이벤트 수신 ───────────────────────────────────────────
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

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:

            # parking_status INSERT
            await cur.execute("""
                INSERT INTO parking_status
                    (status_zone, status_plate, status_park_type,
                     status_linked_zone, status_entry_time)
                VALUES (%s, %s, %s, %s, %s)
            """, (event.zone, matched_plate, event.park_status,
                  event.linked_zone, entry_time))

            # parking_zone UPDATE (area_number 기준, current_car_number 사용)
            await cur.execute("""
                UPDATE parking_zone
                SET status = 'occupied',
                    current_car_number = %s,
                    status_change_reason = '입차'
                WHERE area_number = %s
            """, (matched_plate, event.zone))

        await conn.commit()

    print(f"[ENTRY] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")
    return {"result": "ok", "event": "entry", "zone": event.zone,
            "ocr_plate": event.plate, "saved_plate": matched_plate}


# ── 출차 ──────────────────────────────────────────────────
async def handle_exit(event: ParkingEvent):
    exit_time = event.exit_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:

            # parking_status 에서 정보 가져오기
            await cur.execute("""
                SELECT status_plate, status_park_type, status_entry_time
                FROM parking_status
                WHERE status_zone = %s
                ORDER BY status_entry_time DESC
                LIMIT 1
            """, (event.zone,))
            row = await cur.fetchone()

            plate      = row[0] if row else event.plate
            park_type  = row[1] if row else "normal"
            entry_time = row[2] if row else None

            # parking_status DELETE
            await cur.execute("DELETE FROM parking_status WHERE status_zone = %s", (event.zone,))

            # parking_history INSERT
            await cur.execute("""
                INSERT INTO parking_history
                    (history_zone, history_plate, history_park_type,
                     history_entry_time, history_exit_time)
                VALUES (%s, %s, %s, %s, %s)
            """, (event.zone, plate, park_type, entry_time, exit_time))

            # parking_zone UPDATE → empty
            await cur.execute("""
                UPDATE parking_zone
                SET status = 'empty',
                    current_car_number = NULL,
                    status_change_reason = '출차'
                WHERE area_number = %s
            """, (event.zone,))

        await conn.commit()

    print(f"[EXIT] {event.zone} | {plate}")
    return {"result": "ok", "event": "exit", "zone": event.zone}


# ── 번호판 업데이트 ───────────────────────────────────────
async def handle_update(event: ParkingEvent):
    matched_plate = await match_plate(event.plate) if event.plate else None

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE parking_status SET status_plate = %s
                WHERE status_zone = %s
            """, (matched_plate, event.zone))

            await cur.execute("""
                UPDATE parking_zone SET current_car_number = %s
                WHERE area_number = %s
            """, (matched_plate, event.zone))

        await conn.commit()

    print(f"[UPDATE] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")
    return {"result": "ok", "event": "update", "zone": event.zone,
            "ocr_plate": event.plate, "saved_plate": matched_plate}