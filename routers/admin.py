# ============================================================
# 관리자 라우터
# 구역 상태 변경 / 차량 등록 관리
# ============================================================

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Optional
import aiomysql

router = APIRouter()
pool   = None


class ZoneDisableRequest(BaseModel):
    zone:        str
    zone_status: str      # disabled / empty

class RegisterVehicle(BaseModel):
    reg_plate: str
    reg_name:  Optional[str] = None
    reg_memo:  Optional[str] = None


# ── 구역 상태 변경 (disabled / empty) ─────────────────────
@router.post("/zone")
async def admin_zone(req: ZoneDisableRequest):
    """관리자가 구역을 사용불가/빈자리로 변경"""
    if req.zone_status not in ["disabled", "empty"]:
        raise HTTPException(status_code=400, detail="zone_status는 disabled 또는 empty만 가능")

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                UPDATE parking_zone
                SET status = %s,
                    current_car_number = NULL,
                    status_change_reason = %s
                WHERE area_number = %s
            """, (req.zone_status,
                  '관리자 설정' if req.zone_status == 'disabled' else '관리자 복구',
                  req.zone))
        await conn.commit()

    print(f"[ADMIN] {req.zone} → {req.zone_status}")
    return {"result": "ok", "zone": req.zone, "zone_status": req.zone_status}


# ── 차량 등록 (OCR 보정용) ────────────────────────────────
@router.post("/register")
async def register_vehicle(req: RegisterVehicle):
    """OCR 보정용 번호판 등록"""
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO registered_vehicles (reg_plate, reg_name, reg_memo)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE
                    reg_name = VALUES(reg_name),
                    reg_memo = VALUES(reg_memo)
            """, (req.reg_plate, req.reg_name, req.reg_memo))
        await conn.commit()

    print(f"[REGISTER] {req.reg_plate} | {req.reg_name}")
    return {"result": "ok", "reg_plate": req.reg_plate}


# ── 등록 차량 목록 조회 ───────────────────────────────────
@router.get("/register")
async def get_registered():
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM registered_vehicles ORDER BY reg_id")
            rows = await cur.fetchall()
    return {"result": "ok", "data": rows}


# ── 등록 차량 삭제 ────────────────────────────────────────
@router.delete("/register/{reg_plate}")
async def delete_registered(reg_plate: str):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                DELETE FROM registered_vehicles WHERE reg_plate = %s
            """, (reg_plate,))
        await conn.commit()
    return {"result": "ok", "deleted": reg_plate}