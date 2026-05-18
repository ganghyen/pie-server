# ============================================================
# 스마트 주차 관리 시스템 - FastAPI 서버
# ============================================================

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import aiomysql
from datetime import datetime
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import DB_CONFIG, HISTORY_DELETE_MINUTES, PLATE_MATCH_THRESHOLD
import Levenshtein

# ── DB 연결 풀 ────────────────────────────────────────────
pool = None
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await aiomysql.create_pool(**DB_CONFIG)
    await init_tables()
    scheduler.add_job(delete_old_history, "interval", minutes=1)
    scheduler.start()
    print("[Server] 시작 완료")
    yield
    scheduler.shutdown()
    pool.close()
    await pool.wait_closed()
    print("[Server] 종료")

app = FastAPI(title="스마트 주차 관리 API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 테이블 초기화 ─────────────────────────────────────────
async def init_tables():
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:

            # 1. 실시간 주차 관리
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS parking_status (
                    status_id          INT AUTO_INCREMENT PRIMARY KEY,
                    status_zone        VARCHAR(20) NOT NULL,
                    status_plate       VARCHAR(20),
                    status_park_type   VARCHAR(20) DEFAULT 'normal',
                    status_linked_zone VARCHAR(20),
                    status_entry_time  DATETIME
                )
            """)

            # 2. 입출차 기록
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS parking_history (
                    history_id         INT AUTO_INCREMENT PRIMARY KEY,
                    history_zone       VARCHAR(30) NOT NULL,
                    history_plate      VARCHAR(20),
                    history_park_type  VARCHAR(20),
                    history_entry_time DATETIME,
                    history_exit_time  DATETIME
                )
            """)

            # 3. 주차 구역 현황 (웹/앱용)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS parking_zone (
                    zone_name   VARCHAR(20) PRIMARY KEY,
                    zone_plate  VARCHAR(20),
                    zone_status VARCHAR(20) DEFAULT 'empty'
                )
            """)

            # 4. 등록 차량 (주민 등록 번호판)
            await cur.execute("""
                CREATE TABLE IF NOT EXISTS registered_vehicles (
                    reg_id    INT AUTO_INCREMENT PRIMARY KEY,
                    reg_plate VARCHAR(20) NOT NULL UNIQUE,
                    reg_name  VARCHAR(50),
                    reg_memo  VARCHAR(100)
                )
            """)

        await conn.commit()
    print("[DB] 테이블 초기화 완료")


# ── 번호판 유사도 매칭 ────────────────────────────────────
async def match_plate(ocr_plate: str) -> str:
    """
    OCR 인식 번호판을 등록된 차량과 비교
    완전 일치 → 그대로 사용
    유사한 번호판 → 자동 보정
    없으면 → 원본 그대로 사용
    """
    if not ocr_plate:
        return ocr_plate

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("SELECT reg_plate FROM registered_vehicles")
            rows = await cur.fetchall()

    if not rows:
        return ocr_plate

    registered = [row[0] for row in rows]

    # 완전 일치 확인
    if ocr_plate in registered:
        print(f"[PlateMatch] 완전 일치: {ocr_plate}")
        return ocr_plate

    # 유사도 비교 (Levenshtein Distance)
    best_plate    = None
    best_distance = float("inf")

    for reg in registered:
        distance = Levenshtein.distance(ocr_plate, reg)
        if distance < best_distance:
            best_distance = distance
            best_plate    = reg

    # 임계값 이하면 자동 보정
    if best_distance <= PLATE_MATCH_THRESHOLD:
        print(f"[PlateMatch] 오인식 보정: {ocr_plate} → {best_plate} (거리:{best_distance})")
        return best_plate
    else:
        print(f"[PlateMatch] 매칭 실패: {ocr_plate} (최소거리:{best_distance}) → 원본 사용")
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

class ZoneDisableRequest(BaseModel):
    zone:        str
    zone_status: str              # disabled / empty

class RegisterVehicle(BaseModel):
    reg_plate: str
    reg_name:  Optional[str] = None
    reg_memo:  Optional[str] = None


# ── 이벤트 수신 (파이 → 서버) ─────────────────────────────
@app.post("/api/event")
async def receive_event(event: ParkingEvent):
    if event.event == "entry":
        return await handle_entry(event)
    elif event.event == "exit":
        return await handle_exit(event)
    elif event.event == "update":
        return await handle_update(event)
    else:
        raise HTTPException(status_code=400, detail="Unknown event type")


# ── 입차 처리 ─────────────────────────────────────────────
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

            # parking_zone UPDATE (없으면 INSERT)
            await cur.execute("""
                INSERT INTO parking_zone (zone_name, zone_plate, zone_status)
                VALUES (%s, %s, 'occupied')
                ON DUPLICATE KEY UPDATE
                    zone_plate  = VALUES(zone_plate),
                    zone_status = 'occupied'
            """, (event.zone, matched_plate))

        await conn.commit()

    print(f"[ENTRY] {event.zone} | OCR:{event.plate} → 저장:{matched_plate} | {event.park_status}")
    return {"result": "ok", "event": "entry", "zone": event.zone,
            "ocr_plate": event.plate, "saved_plate": matched_plate}


# ── 출차 처리 ─────────────────────────────────────────────
async def handle_exit(event: ParkingEvent):
    exit_time = event.exit_time or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:

            # parking_status 에서 해당 구역 정보 가져오기
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
            await cur.execute("""
                DELETE FROM parking_status WHERE status_zone = %s
            """, (event.zone,))

            # parking_history INSERT
            await cur.execute("""
                INSERT INTO parking_history
                    (history_zone, history_plate, history_park_type,
                     history_entry_time, history_exit_time)
                VALUES (%s, %s, %s, %s, %s)
            """, (event.zone, plate, park_type, entry_time, exit_time))

            # parking_zone UPDATE → empty
            await cur.execute("""
                INSERT INTO parking_zone (zone_name, zone_plate, zone_status)
                VALUES (%s, NULL, 'empty')
                ON DUPLICATE KEY UPDATE
                    zone_plate  = NULL,
                    zone_status = 'empty'
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
                UPDATE parking_zone SET zone_plate = %s
                WHERE zone_name = %s
            """, (matched_plate, event.zone))

        await conn.commit()

    print(f"[UPDATE] {event.zone} | OCR:{event.plate} → 저장:{matched_plate}")
    return {"result": "ok", "event": "update", "zone": event.zone,
            "ocr_plate": event.plate, "saved_plate": matched_plate}


# ── 관리자: 구역 상태 변경 ────────────────────────────────
@app.post("/api/admin/zone")
async def admin_zone(req: ZoneDisableRequest):
    if req.zone_status not in ["disabled", "empty"]:
        raise HTTPException(status_code=400, detail="zone_status는 disabled 또는 empty만 가능")

    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO parking_zone (zone_name, zone_plate, zone_status)
                VALUES (%s, NULL, %s)
                ON DUPLICATE KEY UPDATE
                    zone_plate  = NULL,
                    zone_status = VALUES(zone_status)
            """, (req.zone, req.zone_status))
        await conn.commit()

    print(f"[ADMIN] {req.zone} → {req.zone_status}")
    return {"result": "ok", "zone": req.zone, "zone_status": req.zone_status}


# ── 관리자: 차량 등록 ─────────────────────────────────────
@app.post("/api/admin/register")
async def register_vehicle(req: RegisterVehicle):
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


# ── 관리자: 등록 차량 목록 조회 ──────────────────────────
@app.get("/api/admin/register")
async def get_registered():
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM registered_vehicles ORDER BY reg_id")
            rows = await cur.fetchall()
    return {"result": "ok", "data": rows}


# ── 관리자: 등록 차량 삭제 ───────────────────────────────
@app.delete("/api/admin/register/{reg_plate}")
async def delete_registered(reg_plate: str):
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                DELETE FROM registered_vehicles WHERE reg_plate = %s
            """, (reg_plate,))
        await conn.commit()
    return {"result": "ok", "deleted": reg_plate}


# ── 오래된 기록 자동 삭제 ─────────────────────────────────
async def delete_old_history():
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                DELETE FROM parking_history
                WHERE history_exit_time < NOW() - INTERVAL %s MINUTE
            """, (HISTORY_DELETE_MINUTES,))
            deleted = cur.rowcount
        await conn.commit()
    if deleted > 0:
        print(f"[History] {deleted}건 자동 삭제 ({HISTORY_DELETE_MINUTES}분 경과)")


# ── 웹/앱 API ─────────────────────────────────────────────
@app.get("/api/status")
async def get_status():
    """주차장 전체 현황"""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("SELECT * FROM parking_zone ORDER BY zone_name")
            rows = await cur.fetchall()
    return {"result": "ok", "data": rows}


@app.get("/api/find")
async def find_car(plate: str):
    """내 차 위치 조회"""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT * FROM parking_status WHERE status_plate = %s
            """, (plate,))
            row = await cur.fetchone()
    if not row:
        return {"result": "not_found"}
    return {"result": "ok", "data": row}


@app.get("/api/history")
async def get_history(limit: int = 50):
    """입출차 기록 조회"""
    async with pool.acquire() as conn:
        async with conn.cursor(aiomysql.DictCursor) as cur:
            await cur.execute("""
                SELECT * FROM parking_history
                ORDER BY history_exit_time DESC
                LIMIT %s
            """, (limit,))
            rows = await cur.fetchall()
    return {"result": "ok", "data": rows}


@app.get("/")
async def root():
    return {"message": "스마트 주차 관리 서버 동작 중"}