# ============================================================
# 입구 차단기 라우터
# 등록 차량 확인 / 차단기 제어 / 이중주차 역추적
# ============================================================

from fastapi import APIRouter
from pydantic import BaseModel
from datetime import datetime
import httpx
import asyncio
from config import SPRING_API, GATE_CHECK_MINUTES

router = APIRouter()

# ── 대기 중인 미매칭 번호판 목록 ─────────────────────────
# 입구 통과했지만 아직 주차 번호판 미부여 차량 목록
# { "plate": "12가1234", "entered_at": datetime }
pending_plates: list[dict] = []
pending_lock = asyncio.Lock()


class CheckPlateRequest(BaseModel):
    plate: str

class EntryLogRequest(BaseModel):
    c_number:    str
    is_resident: bool


# ── 1. 등록 차량 확인 + 차단기 제어 ──────────────────────
@router.post("/check-plate")
async def check_plate(req: CheckPlateRequest):
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                SPRING_API["gate_check"],
                json={"plate": req.plate},
                timeout=8,  # ← 8초
            )

            if response.status_code == 200:
                data = response.json()
                is_resident = data.get("is_resident", False)
            else:
                print(f"[CHECK PLATE] Spring Boot 에러 ({response.status_code})")
                is_resident = False

    except Exception as e:
        print(f"[CHECK PLATE] Spring Boot 조회 실패: {e}")
        is_resident = False

    print(f"[CHECK PLATE] {req.plate} -> is_resident: {is_resident}")

    # 등록 차량이면 대기 목록에 추가
    if is_resident:
        async with pending_lock:
            pending_plates.append({
                "plate":      req.plate,
                "entered_at": datetime.now()
            })
        print(f"[PENDING] {req.plate} 대기 목록 추가 (총 {len(pending_plates)}개)")

    return {
        "plate":       req.plate,
        "is_resident": is_resident,
        "gate_open":   is_resident
    }


# ── 2. 입구 통과 로그 저장 ────────────────────────────────
@router.post("/entry-log")
async def entry_log(req: EntryLogRequest):
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                SPRING_API["gate_log"],
                json={
                    "c_number":    req.c_number,
                    "is_resident": req.is_resident,
                    "gate_open":   req.is_resident,
                },
                timeout=8,  # ← 8초
            )
        print(f"[ENTRY LOG] {req.c_number} | 등록:{req.is_resident}")
        return {"result": "ok"}
    except Exception as e:
        print(f"[ENTRY LOG] Spring Boot 전달 실패: {e}")
        return {"result": "fail"}


# ── 3. 아두이노 차단기 상태 확인 ──────────────────────────
@router.get("/gate/status")
async def gate_status(plate: str):
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                SPRING_API["gate_check"],
                json={"plate": plate},
                timeout=8,  # ← 8초
            )
            data = response.json()
            is_resident = data.get("is_resident", False)
    except Exception as e:
        print(f"[GATE STATUS] Spring Boot 조회 실패: {e}")
        is_resident = False

    print(f"[GATE STATUS] {plate} → gate_open: {is_resident}")
    return {"plate": plate, "gate_open": is_resident}


# ── 4. 번호판 NULL 입차 → 즉시 역추적 ────────────────────
async def try_assign_plate_to_null_parking(zone: str):
    """
    번호판 NULL로 입차된 차량에 즉시 역추적 시도
    - 대기 목록에서 미매칭 번호판 조회
    - 후보 1개 → 즉시 부여
    - 후보 여러개 → 대기 (주차 처리되면서 줄어들면 재시도)
    """
    print(f"[ASSIGN] {zone} 번호판 NULL → 역추적 시작")

    # 최대 10분간 30초마다 재시도
    max_retries = 20
    retry_interval = 30

    for attempt in range(max_retries):
        async with pending_lock:
            # 10분 이상 된 항목 제거
            now = datetime.now()
            expired = [
                p for p in pending_plates
                if (now - p["entered_at"]).total_seconds() > GATE_CHECK_MINUTES * 60
            ]
            for p in expired:
                pending_plates.remove(p)
                print(f"[PENDING] {p['plate']} 만료 제거")

            candidates = list(pending_plates)

        if not candidates:
            print(f"[ASSIGN] {zone} 대기 중인 번호판 없음 → 종료")
            return

        if len(candidates) == 1:
            # 후보 1개 → 즉시 부여
            plate = candidates[0]["plate"]
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.post(
                        SPRING_API["assign_plate"],
                        json={
                            "zone":  zone,
                            "plate": plate,
                        },
                        timeout=8,  # ← 8초
                    )
                if res.status_code == 200:
                    async with pending_lock:
                        pending_plates[:] = [
                            p for p in pending_plates if p["plate"] != plate
                        ]
                    print(f"[ASSIGN] {zone} → {plate} 번호판 부여 완료!")
                    return
                else:
                    print(f"[ASSIGN] Spring Boot 에러: {res.status_code}")
            except Exception as e:
                print(f"[ASSIGN] Spring Boot 전달 실패: {e}")
            return

        else:
            # 후보 여러개 → 대기
            plate_list = [p["plate"] for p in candidates]
            print(f"[ASSIGN] {zone} 후보 {len(candidates)}개 → 대기 중: {plate_list}")
            print(f"[ASSIGN] {retry_interval}초 후 재시도 ({attempt+1}/{max_retries})")
            await asyncio.sleep(retry_interval)

    # 최대 재시도 초과 → 알림 저장
    async with pending_lock:
        candidates = list(pending_plates)

    if candidates:
        candidates_str = ",".join([p["plate"] for p in candidates])
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    SPRING_API["alert"],
                    json={
                        "zone":       zone,
                        "candidates": candidates_str,
                        "time":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    },
                    timeout=8,  # ← 8초
                )
            print(f"[ASSIGN] {zone} 최대 재시도 초과 → 알림 저장 | 후보: {candidates_str}")
        except Exception as e:
            print(f"[ASSIGN] 알림 저장 실패: {e}")


# ── 5. 외부에서 호출: 번호판 NULL 입차 시 역추적 시작 ─────
def start_plate_assignment(zone: str):
    """
    parking.py 에서 번호판 NULL 입차 시 호출
    백그라운드로 역추적 실행
    """
    asyncio.create_task(try_assign_plate_to_null_parking(zone))
    print(f"[ASSIGN] {zone} 역추적 백그라운드 시작")