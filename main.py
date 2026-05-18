# ============================================================
# 스마트 주차 관리 시스템 - FastAPI 서버 메인
# ============================================================

import aiomysql
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from config import DB_CONFIG, HISTORY_DELETE_MINUTES

from routers import parking, gate, admin, query

pool = None
scheduler = AsyncIOScheduler()

@asynccontextmanager
async def lifespan(app: FastAPI):
    global pool
    pool = await aiomysql.create_pool(**DB_CONFIG)

    parking.pool = pool
    gate.pool    = pool
    admin.pool   = pool
    query.pool   = pool

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

# ── 라우터 등록 ───────────────────────────────────────────
app.include_router(parking.router, prefix="/api")       # POST /api/event
app.include_router(gate.router,    prefix="/api")       # POST /api/check-plate, /api/entry-log
app.include_router(admin.router,   prefix="/api/admin") # POST /api/admin/zone
app.include_router(query.router,   prefix="/api")       # GET  /api/status


# ── 테이블 초기화 ─────────────────────────────────────────
async def init_tables():
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:

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

            await cur.execute("""
                CREATE TABLE IF NOT EXISTS parking_zone (
                    pz_no                INT AUTO_INCREMENT PRIMARY KEY,
                    pl_no                INT,
                    area_number          VARCHAR(20) NOT NULL,
                    location             VARCHAR(100),
                    status               VARCHAR(20) DEFAULT 'empty',
                    layout_row           INT,
                    layout_column        INT,
                    status_change_reason VARCHAR(100),
                    current_car_number   VARCHAR(20)
                )
            """)

            await cur.execute("""
                CREATE TABLE IF NOT EXISTS gate_entry_log (
                    log_no             INT PRIMARY KEY AUTO_INCREMENT,
                    gate_car_number    VARCHAR(20) NOT NULL,
                    gate_is_registered BOOLEAN NOT NULL DEFAULT FALSE,
                    gate_open          BOOLEAN NOT NULL DEFAULT FALSE,
                    gate_entry_time    DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            await cur.execute("""
                CREATE TABLE IF NOT EXISTS double_park_alert (
                    alert_id        INT AUTO_INCREMENT PRIMARY KEY,
                    dpa_zone        VARCHAR(20),
                    dpa_time        DATETIME,
                    dpa_candidates  TEXT,
                    dpa_is_resolved BOOLEAN DEFAULT FALSE
                )
            """)

        await conn.commit()
    print("[DB] 테이블 초기화 완료")


# ── 오래된 기록 자동 삭제 ─────────────────────────────────
async def delete_old_history():
    async with pool.acquire() as conn:
        async with conn.cursor() as cur:
            await cur.execute("""
                DELETE FROM parking_history
                WHERE history_exit_time < NOW() - INTERVAL %s MINUTE
            """, (HISTORY_DELETE_MINUTES,))
            deleted_history = cur.rowcount

            await cur.execute("""
                DELETE FROM gate_entry_log
                WHERE gate_entry_time < NOW() - INTERVAL %s MINUTE
            """, (HISTORY_DELETE_MINUTES,))
            deleted_gate = cur.rowcount

        await conn.commit()

    if deleted_history > 0:
        print(f"[History] {deleted_history}건 자동 삭제 ({HISTORY_DELETE_MINUTES}분 경과)")
    if deleted_gate > 0:
        print(f"[GateLog] {deleted_gate}건 자동 삭제 ({HISTORY_DELETE_MINUTES}분 경과)")


@app.get("/")
async def root():
    return {"message": "스마트 주차 관리 서버 동작 중"}