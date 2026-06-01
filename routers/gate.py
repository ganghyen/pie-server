# ============================================================
# 입구 차단기 라우터
# 등록 차량 확인 / 차단기 제어 / 번호판 NULL 역추적
# ============================================================

from fastapi import APIRouter
from pydantic import BaseModel
from datetime import datetime
import httpx
import asyncio
from config import SPRING_API, GATE_CHECK_MINUTES

router = APIRouter()

# 입구 통과했지만 아직 주차 구역 번호판 미부여 차량 대기 목록
# 형식: {"plate": "12가1234", "entered_at": datetime}
pending_plates: list[dict] = []
# 비동기 동시 접근 방지용 락
pending_lock = asyncio.Lock()


class CheckPlateRequest(BaseModel):
    plate: str  # 입구 카메라가 인식한 번호판


class EntryLogRequest(BaseModel):
    c_number:    str   # 차량 번호
    is_resident: bool  # 등록 차량 여부


@router.post("/check-plate")
async def check_plate(req: CheckPlateRequest):
    """
    입구 카메라가 인식한 번호판을 Spring Boot로 전달해서
    등록 차량인지 확인하고 차단기 개방 여부 반환.
    등록 차량이면 pending_plates에 추가 (역추적용).
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                SPRING_API["gate_check"],
                json={"plate": req.plate},
                timeout=8,
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

    if is_resident:
        # 등록 차량이면 역추적 대기 목록에 추가
        async with pending_lock:
            pending_plates.append({
                "plate":      req.plate,
                "entered_at": datetime.now()
            })
        print(f"[PENDING] {req.plate} 대기 목록 추가 (총 {len(pending_plates)}개)")

    return {
        "plate":       req.plate,
        "is_resident": is_resident,
        "gate_open":   is_resident  # 등록 차량이면 차단기 개방
    }


@router.post("/entry-log")
async def entry_log(req: EntryLogRequest):
    """입구 통과 결과를 Spring Boot gate_entry_log에 저장."""
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                SPRING_API["gate_log"],
                json={
                    "c_number":    req.c_number,
                    "is_resident": req.is_resident,
                    "gate_open":   req.is_resident,
                },
                timeout=8,
            )
        print(f"[ENTRY LOG] {req.c_number} | 등록:{req.is_resident}")
        return {"result": "ok"}
    except Exception as e:
        print(f"[ENTRY LOG] Spring Boot 전달 실패: {e}")
        return {"result": "fail"}


@router.get("/gate/status")
async def gate_status(plate: str):
    """아두이노 차단기가 현재 특정 번호판의 개방 여부를 확인하는 엔드포인트."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                SPRING_API["gate_check"],
                json={"plate": plate},
                timeout=8,
            )
            data = response.json()
            is_resident = data.get("is_resident", False)
    except Exception as e:
        print(f"[GATE STATUS] Spring Boot 조회 실패: {e}")
        is_resident = False

    print(f"[GATE STATUS] {plate} → gate_open: {is_resident}")
    return {"plate": plate, "gate_open": is_resident}


async def try_assign_plate_to_null_parking(zone: str):
    """
    번호판 NULL로 입차된 구역에 pending_plates에서 번호판 역추적 매칭.

    로직:
    - 후보 1개: 즉시 Spring Boot에 번호판 부여 요청
    - 후보 여러 개: 30초 대기 후 재시도 (다른 구역에서 매칭되면 후보 줄어듦)
    - 최대 10분(20회) 재시도 후 실패 시 관리자 알림 저장
    """
    print(f"[ASSIGN] {zone} 번호판 NULL → 역추적 시작")

    max_retries    = 20   # 최대 재시도 횟수
    retry_interval = 30   # 재시도 간격 (초)

    for attempt in range(max_retries):
        async with pending_lock:
            # GATE_CHECK_MINUTES 분 초과 항목 만료 제거
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
            # 대기 중인 번호판 없으면 역추적 종료
            print(f"[ASSIGN] {zone} 대기 중인 번호판 없음 → 종료")
            return

        if len(candidates) == 1:
            # 후보가 1개면 즉시 매칭
            plate = candidates[0]["plate"]
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.post(
                        SPRING_API["assign_plate"],
                        json={
                            "zone":  zone,
                            "plate": plate,
                        },
                        timeout=8,
                    )
                if res.status_code == 200:
                    # 매칭 성공 → pending_plates에서 제거
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
            # 후보 여러 개 → 다른 구역 매칭 완료로 후보가 줄어들 때까지 대기
            plate_list = [p["plate"] for p in candidates]
            print(f"[ASSIGN] {zone} 후보 {len(candidates)}개 → 대기 중: {plate_list}")
            print(f"[ASSIGN] {retry_interval}초 후 재시도 ({attempt+1}/{max_retries})")
            await asyncio.sleep(retry_interval)

    # 최대 재시도 초과 → 관리자에게 알림
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
                    timeout=8,
                )
            print(f"[ASSIGN] {zone} 최대 재시도 초과 → 알림 저장 | 후보: {candidates_str}")
        except Exception as e:
            print(f"[ASSIGN] 알림 저장 실패: {e}")


def start_plate_assignment(zone: str):
    """
    parking.py에서 번호판 NULL 입차 발생 시 호출.
    역추적 함수를 비동기 태스크로 백그라운드 실행.
    """
    asyncio.create_task(try_assign_plate_to_null_parking(zone))
    print(f"[ASSIGN] {zone} 역추적 백그라운드 시작")