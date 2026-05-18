# ============================================================
# 웹/앱 조회 라우터
# 주차장 현황 / 내 차 위치 / 입출차 기록
# ============================================================

from fastapi import APIRouter
import aiomysql

router = APIRouter()
pool   = None


# ── 주차장 전체 현황 ──────────────────────────────────────
@router.get("/status")
async def get_status():
    """parking_zone 전체 조회 (웹/앱 현황 표시용)"""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT pz_no, pl_no, area_number, location,
                       status, layout_row, layout_column,
                       status_change_reason, current_car_number
                FROM parking_zone
                ORDER BY area_number
            """)
            rows = await cur.fetchall()
    return {"result": "ok", "data": rows}


# ── 내 차 위치 조회 ───────────────────────────────────────
@router.get("/find")
async def find_car(plate: str):
    """번호판으로 현재 주차 구역 조회"""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT * FROM parking_status WHERE status_plate = %s
            """, (plate,))
            row = await cur.fetchone()
    if not row:
        return {"result": "not_found"}
    return {"result": "ok", "data": row}


# ── 입출차 기록 조회 ──────────────────────────────────────
@router.get("/history")
async def get_history(limit: int = 50):
    """입출차 기록 최신순 조회"""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT * FROM parking_history
                ORDER BY history_exit_time DESC
                LIMIT %s
            """, (limit,))
            rows = await cur.fetchall()
    return {"result": "ok", "data": rows}