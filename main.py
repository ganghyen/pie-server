# ============================================================
# 스마트 주차 관리 시스템 - FastAPI 서버 메인 6
# 역할: 파이/입구카메라 데이터 수신 → 검증/보정 → Spring Boot 전달
# ============================================================

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
from routers import parking, gate


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 서버 시작 시 실행
    print("[Server] 시작 완료")
    yield
    # 서버 종료 시 실행
    print("[Server] 종료")


app = FastAPI(title="스마트 주차 관리 FastAPI", lifespan=lifespan)

# CORS 설정: 모든 출처 허용 (개발 환경용)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 등록
# parking: POST /api/event (파이 카메라 입출차 이벤트)
app.include_router(parking.router, prefix="/api")
# gate: POST /api/check-plate, /api/entry-log (입구 차단기)
app.include_router(gate.router,    prefix="/api")


@app.get("/")
async def root():
    # 서버 상태 확인용 헬스체크 엔드포인트
    return {"message": "스마트 주차 관리 FastAPI 서버 동작 중"}
