# ============================================================
# 스마트 주차 관리 시스템 - FastAPI 서버 메인
# 역할: 파이/입구카메라 데이터 수신 → 검증/보정 → Spring Boot 전달
# ============================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from routers import parking, gate


@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[Server] 시작 완료")
    yield
    print("[Server] 종료")


app = FastAPI(title="스마트 주차 관리 FastAPI", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 라우터 등록 ───────────────────────────────────────────
app.include_router(parking.router, prefix="/api")  # POST /api/event
app.include_router(gate.router,    prefix="/api")  # POST /api/check-plate, /api/entry-log


@app.get("/")
async def root():
    return {"message": "스마트 주차 관리 FastAPI 서버 동작 중"}