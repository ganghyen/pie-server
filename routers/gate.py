# ============================================================
# 입구 차단기 라우터
# 앱 팀 요청 형식에 맞춤
# POST /api/check-plate  → 등록 차량 확인
# POST /api/entry-log    → 입차 로그 저장
# GET  /api/gate/status  → 아두이노용 차단기 상태 확인
# ============================================================

from fastapi import APIRouter
from pydantic import BaseModel
from datetime import datetime
import aiomysql

router = APIRouter()
pool   = None


# ── 요청 모델 ─────────────────────────────────────────────
class CheckPlateRequest(BaseModel):
    plate: str                    # 앱 팀 필드명

class EntryLogRequest(BaseModel):
    c_number:    str              # 앱 팀 필드명
    is_resident: bool


# ── 1. 등록 차량 확인 ─────────────────────────────────────
@router.post("/check-plate")
async def check_plate(req: CheckPlateRequest):
    """
    앱 팀 입구 카메라 → POST /api/check-plate
    { "plate": "12가1234" }
    car 테이블 c_number 에서 등록 차량 확인
    → { "is_resident": true/false }
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT c_number FROM car WHERE c_number = %s
            """, (req.plate,))
            row = await cur.fetchone()

    is_resident = row is not None
    print(f"[CHECK PLATE] {req.plate} → is_resident: {is_resident}")
    return {"is_resident": is_resident}


# ── 2. 입차 로그 저장 ─────────────────────────────────────
@router.post("/entry-log")
async def entry_log(req: EntryLogRequest):
    """
    앱 팀 입구 카메라 → POST /api/entry-log
    { "c_number": "12가1234", "is_resident": true }
    gate_entry_log 테이블에 저장
    """
    gate_open = req.is_resident

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO gate_entry_log
                    (gate_car_number, gate_is_registered, gate_open)
                VALUES (%s, %s, %s)
            """, (req.c_number, req.is_resident, gate_open))
        await conn.commit()

    print(f"[ENTRY LOG] {req.c_number} | 등록:{req.is_resident} | 차단기:{gate_open}")
    return {"result": "ok", "c_number": req.c_number, "gate_open": gate_open}


# ── 3. 아두이노용 차단기 상태 확인 ────────────────────────
@router.get("/gate/status")
async def gate_status(plate: str):
    """
    아두이노가 호출하는 API
    등록 차량이면 gate_open: true → 서보모터 동작
    미등록이면 gate_open: false → 차단기 유지
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT c_number FROM car WHERE c_number = %s
            """, (plate,))
            row = await cur.fetchone()

    gate_open = row is not None
    print(f"[GATE STATUS] {plate} → gate_open: {gate_open}")
    return {"plate": plate, "gate_open": gate_open}


# ── 4. 이중주차 번호판 역추적 ─────────────────────────────
@router.get("/gate/unmatched")
async def gate_unmatched(zone: str):
    """
    이중주차 구역 번호판 NULL → gate_entry_log 기반 역추적
    1대 → 자동 번호판 부여
    2대 이상 → double_park_alert 저장
    """
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                SELECT gate_car_number FROM gate_entry_log
                WHERE gate_open = TRUE
                AND gate_car_number NOT IN (
                    SELECT COALESCE(status_plate, '') FROM parking_status
                )
                ORDER BY gate_entry_time DESC
            """)
            candidates = [row[0] for row in await cur.fetchall()]

        if len(candidates) == 1:
            plate = candidates[0]
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE parking_status SET status_plate = %s
                    WHERE status_zone = %s AND status_plate IS NULL
                """, (plate, zone))
                await cur.execute("""
                    UPDATE parking_zone SET zone_plate = %s
                    WHERE area_number = %s
                """, (plate, zone))
            await conn.commit()
            print(f"[UNMATCHED] 자동 부여: {zone} → {plate}")
            return {"result": "auto_assigned", "zone": zone, "plate": plate}

        elif len(candidates) >= 2:
            candidates_str = ",".join(candidates)
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO double_park_alert
                        (dpa_zone, dpa_time, dpa_candidates, dpa_is_resolved)
                    VALUES (%s, %s, %s, FALSE)
                """, (zone, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), candidates_str))
            await conn.commit()
            print(f"[UNMATCHED] 알림 저장: {zone} | 후보: {candidates_str}")
            return {"result": "alert_created", "zone": zone, "candidates": candidates}

        else:
            return {"result": "no_candidates", "zone": zone}


# ── 5. 이중주차 알림 목록 ─────────────────────────────────
@router.get("/gate/alerts")
async def get_alerts():
    """미해결 이중주차 알림 목록"""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT * FROM double_park_alert
                WHERE dpa_is_resolved = FALSE
                ORDER BY dpa_time DESC
            """)
            rows = await cur.fetchall()
    return {"result": "ok", "data": rows}


# ── 6. 이중주차 알림 해결 처리 ────────────────────────────
@router.post("/gate/alerts/{alert_id}/resolve")
async def resolve_alert(alert_id: int):
    """관리자가 이중주차 알림 해결 처리"""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE double_park_alert SET dpa_is_resolved = TRUE
                WHERE alert_id = %s
            """, (alert_id,))
        await conn.commit()
    return {"result": "ok", "alert_id": alert_id}