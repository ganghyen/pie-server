# ============================================================
# 스마트 주차 관리 시스템 - FastAPI 서버 메인
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

app.include_router(parking.router, prefix="/api")
app.include_router(gate.router,    prefix="/api")


@app.get("/")
async def root():
    return {"message": "스마트 주차 관리 FastAPI 서버 동작 중"}