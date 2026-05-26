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
                timeout=3,
            )
           
            # 스프링부트가 정상 응답(200)을 준 경우에만 안전하게 파싱
            if response.status_code == 200:
                data = response.json()
                is_resident = data.get("is_resident", False)
            else:
                print(f"[CHECK PLATE] SPRING BOOT ERROR (CODE: {response.status_code})")
                is_resident = False
               
    except Exception as e:
        print(f"[CHECK PLATE] Spring Boot 조회 실패: {e}")
        is_resident = False

    print(f"[CHECK PLATE] {req.plate} -> is_resident: {is_resident}")

    # 등록 차량이면 10분 후 역추적 실행
    if is_resident:
        asyncio.create_task(check_unmatched_after_delay(req.plate))

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
            # ⚠️ 수정 필요: Spring Boot가 받는 JSON 형식에 맞게 수정
            await client.post(
                SPRING_API["gate_log"],
                json={
                    "c_number":    req.c_number,    # 차량번호
                    "is_resident": req.is_resident, # 등록 차량 여부
                    "gate_open":   req.is_resident, # 차단기 열림 여부
                },
                timeout=3,
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
                # ⚠️ 수정 필요: Spring Boot가 받는 JSON 형식에 맞게 수정
                json={"plate": plate},
                timeout=3,
            )
            data = response.json()
            # ⚠️ 수정 필요: Spring Boot 응답의 키 이름 확인
            is_resident = data.get("is_resident", False)
    except Exception as e:
        print(f"[GATE STATUS] Spring Boot 조회 실패: {e}")
        is_resident = False

    print(f"[GATE STATUS] {plate} → gate_open: {is_resident}")
    return {"plate": plate, "gate_open": is_resident}


# ── 4. 이중주차 번호판 역추적 (10분 후 자동 실행) ─────────
async def check_unmatched_after_delay(plate: str):
    # ⚠️ 수정 가능: GATE_CHECK_MINUTES 값 config.py에서 조절
    await asyncio.sleep(GATE_CHECK_MINUTES * 60)

    print(f"[UNMATCHED] {plate} 역추적 시작")

    try:
        async with httpx.AsyncClient() as client:

            # 번호판 없는 주차 기록 조회
            # ⚠️ 수정 필요: Spring Boot 응답 형식에 맞게 수정
            # 기대 형식: [{"history_id": 1, "history_zone": "A-1"}, ...]
            res = await client.get(SPRING_API["unmatched"], timeout=3)
            unmatched = res.json()

            if not unmatched:
                print(f"[UNMATCHED] 번호판 없는 주차 없음")
                return

            # 후보 1대 → 자동 번호판 부여
            if len(unmatched) == 1:
                # ⚠️ 수정 필요: Spring Boot가 받는 JSON 형식에 맞게 수정
                await client.post(
                    SPRING_API["assign_plate"],
                    json={
                        "history_id": unmatched[0]["history_id"], # 기록 ID
                        "plate":      plate,                       # 부여할 번호판
                    },
                    timeout=3,
                )
                print(f"[UNMATCHED] 자동 부여: {unmatched[0]['history_zone']} → {plate}")

            # 후보 2대 이상 → 알림 저장
            else:
                candidates = [u["history_zone"] for u in unmatched]
                candidates_str = ",".join(candidates)

                # ⚠️ 수정 필요: Spring Boot가 받는 JSON 형식에 맞게 수정
                # Spring Boot에서 FCM으로 차주 + 관리자 알림 발송 처리
                await client.post(
                    SPRING_API["alert"],
                    json={
                        "candidates": candidates_str,                              # 후보 구역 목록
                        "plate":      plate,                                        # 입구 통과 번호판
                        "time":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"), # 감지 시간
                    },
                    timeout=3,
                )
                print(f"[UNMATCHED] 알림 저장 | 후보: {candidates_str}")

    except Exception as e:
        print(f"[UNMATCHED] Spring Boot 조회 실패: {e}")
