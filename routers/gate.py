# ============================================================
# 입구 차단기 라우터
# 등록 차량 확인 / 차단기 제어 / 이중주차 역추적
#
# 수정사항:
#   1. gate_open 기준으로 차단기 제어
#   2. 점유율 80% 이상 시 방문 차량만 차단, 입주민은 개방
#   3. entry_log에 실제 gate_open 값 저장
# ============================================================

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import httpx
import asyncio
from config import SPRING_API, GATE_CHECK_MINUTES

router = APIRouter()

# 입구 통과했지만 아직 주차 구역 번호판 미부여 차량 대기 목록
pending_plates: list[dict] = []
pending_lock = asyncio.Lock()


class CheckPlateRequest(BaseModel):
    plate: str


class EntryLogRequest(BaseModel):
    c_number:    str
    is_resident: bool
    # ✅ 수정: 실제 차단기 개방 여부 추가 (없으면 is_resident 기준으로 처리)
    gate_open:   Optional[bool] = None


# ── 점유율 조회 ───────────────────────────────────────────
async def get_occupancy() -> dict:
    """
    Spring Boot에서 전체 주차장 점유율 조회.
    반환: {"total": 20, "used": 17, "available": 3, "rate": 0.85}
    조회 실패 시 차단 안 함 (rate: 0.0 반환)
    """
    try:
        async with httpx.AsyncClient() as client:
            res = await client.get(
                SPRING_API["occupancy"],
                timeout=8
            )
            if res.status_code == 200:
                return res.json()
    except Exception as e:
        print(f"[GATE] 점유율 조회 실패: {e}")
    return {"total": 0, "used": 0, "available": 999, "rate": 0.0}


# ── 1. 등록 차량 확인 + 차단기 제어 ──────────────────────
@router.post("/check-plate")
async def check_plate(req: CheckPlateRequest):
    """
    입구 카메라가 인식한 번호판 처리 흐름:
    1. Spring Boot에서 등록 차량 여부 확인 (입주민 + 방문 차량)
    2. 입주민 차량 여부 별도 확인 (/api/parking/cars)
    3. 점유율 80% 이상 또는 빈자리 0개 시
       - 입주민 차량: 차단기 개방
       - 방문 차량: 차단기 차단
    4. 정상 범위면 등록 차량 모두 개방
    5. gate_open 기준으로 차단기 제어값 반환
    """

    # 1. 등록 차량 여부 확인 (입주민 + 방문 차량 둘 다)
    is_resident = False
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                SPRING_API["gate_check"],
                json={"plate": req.plate},
                timeout=8,
            )
            if response.status_code == 200:
                is_resident = response.json().get("is_resident", False)
            else:
                print(f"[CHECK PLATE] Spring Boot 에러 ({response.status_code})")
    except Exception as e:
        print(f"[CHECK PLATE] Spring Boot 조회 실패: {e}")

    print(f"[CHECK PLATE] {req.plate} -> is_resident: {is_resident}")

    # 2. 입주민 차량 여부 별도 확인
    # /api/parking/cars 는 입주민 차량(car 테이블)만 반환
    # 방문 차량(registered_cars)은 포함 안 됨
    is_resident_vehicle = False
    try:
        async with httpx.AsyncClient() as client:
            cars_res = await client.get(
                SPRING_API["cars"],
                timeout=8
            )
            if cars_res.status_code == 200:
                car_numbers = [
                    c["c_number"] for c in cars_res.json()
                ]
                # 번호판이 입주민 차량 목록에 있으면 True
                is_resident_vehicle = req.plate in car_numbers
    except Exception as e:
        print(f"[GATE] 입주민 차량 조회 실패: {e}")

    print(
        f"[GATE] {req.plate} "
        f"is_resident_vehicle: {is_resident_vehicle}"
    )

    # 3. 점유율 조회
    occupancy = await get_occupancy()
    rate      = occupancy.get("rate", 0.0)
    available = occupancy.get("available", 999)

    # 4. 점유율 80% 이상 시
    # 입주민 차량이면 개방
    # 방문 차량이면 차단
    if rate >= 0.8:
        if is_resident_vehicle:
            print(
                f"[GATE] 점유율 {int(rate*100)}% 이상이지만 "
                f"입주민 차량 → 개방"
            )
        else:
            print(
                f"[GATE] 점유율 {int(rate*100)}% ≥ 80% "
                f"방문 차량 → 차단"
            )
            return {
                "plate":       req.plate,
                "is_resident": is_resident,
                "gate_open":   False,
                "reason":      f"주차장 {int(rate*100)}% 점유"
            }

    # 5. 빈자리 0개 시
    # 입주민 차량이면 개방
    # 방문 차량이면 차단
    if available <= 0:
        if is_resident_vehicle:
            print(
                f"[GATE] 빈자리 없지만 "
                f"입주민 차량 → 개방"
            )
        else:
            print(f"[GATE] 빈자리 없음 방문 차량 → 차단")
            return {
                "plate":       req.plate,
                "is_resident": is_resident,
                "gate_open":   False,
                "reason":      "주차장 만차"
            }

    # 6. 등록 차량이면 역추적 대기 목록에 추가
    if is_resident:
        async with pending_lock:
            pending_plates.append({
                "plate":      req.plate,
                "entered_at": datetime.now()
            })
        print(f"[PENDING] {req.plate} 대기 목록 추가 (총 {len(pending_plates)}개)")

    # ✅ 수정: gate_open 기준으로 반환
    # is_resident가 아닌 gate_open 값을 차단기 제어에 사용
    gate_open = is_resident
    return {
        "plate":       req.plate,
        "is_resident": is_resident,
        "gate_open":   gate_open
    }


# ── 2. 입구 통과 로그 저장 ────────────────────────────────
@router.post("/entry-log")
async def entry_log(req: EntryLogRequest):
    """
    입구 통과 결과를 Spring Boot gate_entry_log에 저장.
    ✅ 수정: 실제 차단기 개방 여부(gate_open)를 저장
    gate_open이 없으면 is_resident 기준으로 처리
    """
    # ✅ 수정: 실제 차단기 개방 여부 계산
    # gate_open이 명시적으로 전달되면 그 값 사용
    # 없으면 is_resident 기준으로 처리
    gate_open = req.gate_open if req.gate_open is not None else req.is_resident

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                SPRING_API["gate_log"],
                json={
                    "c_number":    req.c_number,
                    "is_resident": req.is_resident,
                    "gate_open":   gate_open,  # ✅ 수정: 실제 개방 여부 저장
                },
                timeout=8,
            )
        print(f"[ENTRY LOG] {req.c_number} | 등록:{req.is_resident} | 개방:{gate_open}")
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
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(SPRING_API["unmatched"], timeout=8)

        if response.status_code >= 400:
            print(f"[ASSIGN] 미매칭 주차 기록 조회 실패: {response.status_code}")
            return None

        data = response.json()
        histories = data
        if isinstance(data, dict):
            histories = data.get("histories") or data.get("data") or []

        if not isinstance(histories, list):
            print("[ASSIGN] 미매칭 주차 기록 응답 형식 오류")
            return None

        for item in histories:
            if not isinstance(item, dict):
                continue
            history_zone = item.get("history_zone") or item.get("zone")
            if history_zone != zone:
                continue
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
    후보 1개: 즉시 매칭
    후보 여러 개: 30초 대기 후 재시도
    최대 10분 후 실패 시 관리자 알림
    """
    print(f"[ASSIGN] {zone} 번호판 NULL → 역추적 시작")

    max_retries    = 20
    retry_interval = 30

    for attempt in range(max_retries):
        async with pending_lock:
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
            plate      = candidates[0]["plate"]
            history_id = await find_unmatched_history_id(zone)

            if history_id is None:
                print(f"[ASSIGN] {zone} history_id 없음 → {retry_interval}초 후 재시도 ({attempt+1}/{max_retries})")
                await asyncio.sleep(retry_interval)
                continue

            try:
                async with httpx.AsyncClient() as client:
                    res = await client.post(
                        SPRING_API["assign_plate"],
                        json={
                            "history_id": history_id,
                            "plate":      plate,
                        },
                        timeout=8,
                    )
                if res.status_code == 200:
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
            plate_list = [p["plate"] for p in candidates]
            print(f"[ASSIGN] {zone} 후보 {len(candidates)}개 → 대기 중: {plate_list}")
            print(f"[ASSIGN] {retry_interval}초 후 재시도 ({attempt+1}/{max_retries})")
            await asyncio.sleep(retry_interval)

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


# ── 6. 외부 호출: 번호판 NULL 입차 시 역추적 시작 ─────────
def start_plate_assignment(zone: str):
    """parking.py에서 번호판 NULL 입차 발생 시 호출."""
    asyncio.create_task(try_assign_plate_to_null_parking(zone))
    print(f"[ASSIGN] {zone} 역추적 백그라운드 시작")
