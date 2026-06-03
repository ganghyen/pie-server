# ============================================================
# 입구 차단기 라우터
# 등록 차량 확인 / 차단기 제어 / 이중주차 역추적
#
# 추가된 기능:
#   1. 점유율 80% 초과 시 차단기 차단
#   2. 빈자리 0개 시 차단기 차단
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


# 차단기 확인 요청 모델
class CheckPlateRequest(BaseModel):
    plate: str  # 입구 카메라가 인식한 번호판


# 입구 통과 로그 요청 모델
class EntryLogRequest(BaseModel):
    c_number:    str   # 차량 번호
    is_resident: bool  # 등록 차량 여부


# ── 점유율 조회 ───────────────────────────────────────────
async def get_occupancy() -> dict:
    """
    Spring Boot에서 전체 주차장 점유율 조회.
    parking_lot 테이블의 total_spaces, used_spaces 합산값 반환.

    반환 형식:
    {
        "total": 20,      전체 주차칸 수
        "used": 17,       현재 사용 중인 칸 수
        "available": 3,   남은 빈자리 수
        "rate": 0.85      점유율 (0.0 ~ 1.0)
    }

    조회 실패 시 차단 안 함 (available: 999, rate: 0.0 반환)
    """
    try:
        async with httpx.AsyncClient() as client:
            # Spring Boot 점유율 API 호출
            res = await client.get(
                SPRING_API["occupancy"],
                timeout=8
            )
            if res.status_code == 200:
                # 정상 응답이면 JSON 반환
                return res.json()
    except Exception as e:
        print(f"[GATE] 점유율 조회 실패: {e}")

    # 조회 실패 시 기본값 반환 (차단 안 함)
    return {"total": 0, "used": 0, "available": 999, "rate": 0.0}


# ── 1. 등록 차량 확인 + 차단기 제어 ──────────────────────
@router.post("/check-plate")
async def check_plate(req: CheckPlateRequest):
    """
    입구 카메라가 인식한 번호판 처리 흐름:
    1. Spring Boot에서 등록 차량 여부 확인
    2. 점유율 80% 초과 시 등록 차량도 차단
    3. 빈자리 0개 시 차단
    4. 등록 차량이면 역추적 대기 목록에 추가
    """

    # Spring Boot에서 등록 차량 여부 확인
    is_resident = False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                SPRING_API["gate_check"],
                json={"plate": req.plate},
                timeout=8,
            )
            if response.status_code == 200:
                # 정상 응답이면 is_resident 값 추출
                is_resident = response.json().get("is_resident", False)
            else:
                print(f"[CHECK PLATE] Spring Boot 에러 ({response.status_code})")
    except Exception as e:
        print(f"[CHECK PLATE] Spring Boot 조회 실패: {e}")

    print(f"[CHECK PLATE] {req.plate} -> is_resident: {is_resident}")

    # ✅ 추가: 점유율 조회
    occupancy = await get_occupancy()
    rate      = occupancy.get("rate", 0.0)       # 현재 점유율 (0.0~1.0)
    available = occupancy.get("available", 999)   # 현재 빈자리 수

    # ✅ 추가: 점유율 80% 초과 시 차단 (등록 차량도 차단)
    if rate >= 0.8:
        print(f"[GATE] 점유율 {int(rate*100)}% ≥ 80% → 차단")
        return {
            "plate":       req.plate,
            "is_resident": is_resident,
            "gate_open":   False,           # 차단기 닫음
            "reason":      f"주차장 {int(rate*100)}% 점유"
        }

    # ✅ 추가: 빈자리 0개 시 차단
    if available <= 0:
        print(f"[GATE] 빈자리 없음 → 차단")
        return {
            "plate":       req.plate,
            "is_resident": is_resident,
            "gate_open":   False,           # 차단기 닫음
            "reason":      "주차장 만차"
        }

    # 등록 차량이면 역추적 대기 목록에 추가
    if is_resident:
        async with pending_lock:
            pending_plates.append({
                "plate":      req.plate,
                "entered_at": datetime.now()  # 입구 통과 시각 기록
            })
        print(f"[PENDING] {req.plate} 대기 목록 추가 (총 {len(pending_plates)}개)")

    return {
        "plate":       req.plate,
        "is_resident": is_resident,
        "gate_open":   is_resident  # 등록 차량이면 차단기 개방
    }


# ── 2. 입구 통과 로그 저장 ────────────────────────────────
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
                    "gate_open":   req.is_resident,  # 등록 차량이면 차단기 개방 여부도 함께 저장
                },
                timeout=8,
            )
        print(f"[ENTRY LOG] {req.c_number} | 등록:{req.is_resident}")
        return {"result": "ok"}
    except Exception as e:
        print(f"[ENTRY LOG] Spring Boot 전달 실패: {e}")
        return {"result": "fail"}


# ── 3. 아두이노 차단기 상태 확인 ──────────────────────────
@router.get("/gate/status")
async def gate_status(plate: str):
    """아두이노 차단기가 특정 번호판의 개방 여부를 확인하는 엔드포인트."""
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                SPRING_API["gate_check"],
                json={"plate": plate},
                timeout=8,
            )
            data        = response.json()
            is_resident = data.get("is_resident", False)
    except Exception as e:
        print(f"[GATE STATUS] Spring Boot 조회 실패: {e}")
        is_resident = False

    print(f"[GATE STATUS] {plate} → gate_open: {is_resident}")
    return {"plate": plate, "gate_open": is_resident}


# ── 4. UNKNOWN 주차 기록에서 구역 매칭 ───────────────────
async def find_unmatched_history_id(zone: str) -> int | None:
    """
    Spring Boot에서 번호판이 UNKNOWN인 진행 중 주차 기록을 조회하고
    현재 주차 구역과 일치하는 history_id를 찾아 반환.
    매칭되는 기록이 없으면 None 반환.
    """
    try:
        async with httpx.AsyncClient() as client:
            # UNKNOWN 번호판 주차 기록 전체 조회
            response = await client.get(SPRING_API["unmatched"], timeout=8)

        if response.status_code >= 400:
            print(f"[ASSIGN] 미매칭 주차 기록 조회 실패: {response.status_code}")
            return None

        data = response.json()

        # 응답 형식이 dict인 경우 내부 리스트 추출
        histories = data
        if isinstance(data, dict):
            histories = data.get("histories") or data.get("data") or []

        if not isinstance(histories, list):
            print("[ASSIGN] 미매칭 주차 기록 응답 형식 오류")
            return None

        # 구역 이름이 일치하는 기록 찾기
        for item in histories:
            if not isinstance(item, dict):
                continue

            # history_zone 또는 zone 키 둘 다 허용
            history_zone = item.get("history_zone") or item.get("zone")
            if history_zone != zone:
                continue

            # history_id 추출 후 정수 변환
            history_id = item.get("history_id")
            try:
                return int(history_id)
            except (TypeError, ValueError):
                print(f"[ASSIGN] history_id 변환 실패: {history_id}")
                return None

        print(f"[ASSIGN] {zone}에 매칭되는 UNKNOWN 주차 기록 없음")
        return None

    except Exception as e:
        print(f"[ASSIGN] 미매칭 주차 기록 조회 실패: {e}")
        return None


# ── 5. 번호판 NULL 입차 → 역추적 ─────────────────────────
async def try_assign_plate_to_null_parking(zone: str):
    """
    번호판 NULL로 입차된 구역에 pending_plates에서 번호판 역추적 매칭.

    로직:
    - 후보 1개: history_id 찾아서 즉시 Spring Boot에 번호판 부여 요청
    - 후보 여러 개: 30초 대기 후 재시도 (다른 구역 매칭으로 후보 줄어들 때까지)
    - 최대 10분(20회) 재시도 후 실패 시 관리자 알림 저장
    """
    print(f"[ASSIGN] {zone} 번호판 NULL → 역추적 시작")

    max_retries    = 20   # 최대 재시도 횟수 (20회 × 30초 = 10분)
    retry_interval = 30   # 재시도 간격 (초)

    for attempt in range(max_retries):
        async with pending_lock:
            now = datetime.now()

            # GATE_CHECK_MINUTES 분 초과된 항목 만료 제거
            expired = [
                p for p in pending_plates
                if (now - p["entered_at"]).total_seconds() > GATE_CHECK_MINUTES * 60
            ]
            for p in expired:
                pending_plates.remove(p)
                print(f"[PENDING] {p['plate']} 만료 제거")

            # 현재 대기 중인 후보 목록 복사
            candidates = list(pending_plates)

        if not candidates:
            # 대기 중인 번호판 없으면 역추적 종료
            print(f"[ASSIGN] {zone} 대기 중인 번호판 없음 → 종료")
            return

        if len(candidates) == 1:
            # 후보 1개 → 즉시 매칭 시도
            plate = candidates[0]["plate"]

            # Spring Boot에서 해당 구역의 UNKNOWN 주차 기록 ID 조회
            history_id = await find_unmatched_history_id(zone)

            if history_id is None:
                # history_id 없으면 잠시 대기 후 재시도
                print(f"[ASSIGN] {zone} history_id 없음 → {retry_interval}초 후 재시도 ({attempt+1}/{max_retries})")
                await asyncio.sleep(retry_interval)
                continue

            try:
                async with httpx.AsyncClient() as client:
                    # Spring Boot에 번호판 부여 요청
                    res = await client.post(
                        SPRING_API["assign_plate"],
                        json={
                            "history_id": history_id,  # 매칭할 주차 기록 ID
                            "plate":      plate,        # 부여할 번호판
                        },
                        timeout=8,
                    )
                if res.status_code == 200:
                    # 매칭 성공 → pending_plates에서 제거
                    async with pending_lock:
                        pending_plates[:] = [
                            p for p in pending_plates if p["plate"] != plate
                        ]
                    print(f"[ASSIGN] {zone} history_id:{history_id} → {plate} 번호판 부여 완료!")
                    return
                else:
                    print(f"[ASSIGN] Spring Boot 에러: {res.status_code} | {res.text}")
            except Exception as e:
                print(f"[ASSIGN] Spring Boot 전달 실패: {e}")
            return

        else:
            # 후보 여러 개 → 다른 구역에서 매칭 완료돼서 후보 줄어들 때까지 대기
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
                # Spring Boot 알림 API로 관리자 알림 저장
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


# ── 6. 외부 호출: 번호판 NULL 입차 시 역추적 시작 ─────────
def start_plate_assignment(zone: str):
    """
    parking.py에서 번호판 NULL 입차 발생 시 호출.
    역추적 함수를 비동기 태스크로 백그라운드 실행.
    """
    asyncio.create_task(try_assign_plate_to_null_parking(zone))
    print(f"[ASSIGN] {zone} 역추적 백그라운드 시작")