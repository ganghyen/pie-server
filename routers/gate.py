# ============================================================
# 입구 차단기 라우터
# 등록 차량 확인 / 차단기 제어 / 이중주차 역추적
#
# 수정사항:
#   1. check_plate: Python 자체 80% 판단 제거
#      Spring Boot gate_open 값을 그대로 사용
#   2. gate_status: is_resident 대신 gate_open 사용
#   3. entry_log: 실제 gate_open 값 저장
#   4. pending_plates: gate_open=true인 차량만 추가
# ============================================================

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import httpx
import asyncio
from config import SPRING_API, GATE_CHECK_MINUTES, APARTMENT_NO

router = APIRouter()

# 입구 통과했지만 아직 주차 구역 번호판 미부여 차량 대기 목록
pending_plates: list[dict] = []
pending_lock = asyncio.Lock()


class CheckPlateRequest(BaseModel):
    plate: str
    # Spring Boot는 apartment_no/apartmentNo/a_no 모두 받을 수 있게 해둠
    # Python 장비가 어느 아파트 입구인지 명확히 전달하기 위한 값
    apartment_no: Optional[int] = None
    apartmentNo: Optional[int] = None
    a_no: Optional[int] = None


class EntryLogRequest(BaseModel):
    c_number:    str
    is_resident: bool
    # 실제 차단기 개방 여부 (없으면 is_resident 기준으로 처리)
    gate_open:   Optional[bool] = None


def resolve_apartment_no(
    apartment_no: Optional[int] = None,
    apartmentNo: Optional[int] = None,
    a_no: Optional[int] = None,
) -> int:
    """요청에 아파트 번호가 없으면 config.APARTMENT_NO를 기본값으로 사용."""
    return apartment_no or apartmentNo or a_no or APARTMENT_NO


# ── 1. 등록 차량 확인 + 차단기 제어 ──────────────────────
@router.post("/check-plate")
async def check_plate(req: CheckPlateRequest):
    """
    Spring Boot /api/gate/check 호출 후
    응답의 gate_open 값을 그대로 차단기 제어에 사용.

    Python에서 직접 판단하지 않는 것:
    - 주차장 점유율 80% 이상 여부
    - 입주민/방문 차량 구분
    - 관리자 설정

    위 판단은 모두 Spring Boot에서 처리.
    """
    apartment_no = resolve_apartment_no(
        req.apartment_no, req.apartmentNo, req.a_no
    )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                SPRING_API["gate_check"],
                json={
                    "plate":        req.plate,
                    "apartment_no": apartment_no,
                },
                timeout=8,
            )

        # Spring Boot 오류 시 차단기 닫음
        if response.status_code >= 400:
            print(
                f"[CHECK PLATE] Spring Boot 에러 "
                f"({response.status_code}) | {response.text}"
            )
            return {
                "plate":        req.plate,
                "apartment_no": apartment_no,
                "is_resident":  False,
                "is_registered": False,
                "gate_open":    False,
                "reason":       "Spring Boot 차량 확인 실패"
            }

        data = response.json()

    except Exception as e:
        print(f"[CHECK PLATE] Spring Boot 조회 실패: {e}")
        return {
            "plate":        req.plate,
            "apartment_no": apartment_no,
            "is_resident":  False,
            "is_registered": False,
            "gate_open":    False,
            "reason":       "Spring Boot 연결 실패"
        }

    # Spring Boot 응답의 gate_open을 최종 차단기 개방 여부로 사용
    gate_open    = bool(data.get("gate_open", False))
    is_registered = bool(
        data.get("is_registered", data.get("is_resident", False))
    )

    # gate_open=true인 차량만 역추적 대기 목록에 추가
   # gate_open=true인 차량만 역추적 대기 목록에 추가
    # 이미 대기 목록에 있는 번호판은 중복 추가 안 함
    if gate_open and is_registered:
        async with pending_lock:
            already_exists = any(
                p["plate"] == req.plate for p in pending_plates
            )
            if not already_exists:
                pending_plates.append({
                    "plate":      req.plate,
                    "entered_at": datetime.now()
                })
                print(
                    f"[PENDING] {req.plate} 대기 목록 추가 "
                    f"(총 {len(pending_plates)}개)"
                )
            else:
                print(f"[PENDING] {req.plate} 이미 대기 중 → 중복 추가 생략")

    print(
        f"[CHECK PLATE] {req.plate} | "
        f"registered:{is_registered} | "
        f"gate_open:{gate_open} | "
        f"reason:{data.get('reason')}"
    )

    # Spring Boot 응답 전체를 그대로 전달
    return {
        "plate":                   data.get("plate", req.plate),
        "apartment_no":            data.get("apartment_no", apartment_no),
        "is_resident":             bool(data.get("is_resident", is_registered)),
        "is_registered":           is_registered,
        "is_resident_vehicle":     bool(data.get("is_resident_vehicle", False)),
        "is_visitor":              bool(data.get("is_visitor", False)),
        "gate_open":               gate_open,
        "reason":                  data.get("reason"),
        "occupancy_block_enabled": data.get("occupancy_block_enabled"),
        "force_open_enabled":      data.get("force_open_enabled"),
        "total":                   data.get("total"),
        "used":                    data.get("used"),
        "available":               data.get("available"),
        "rate":                    data.get("rate"),
    }


# ── 2. 입구 통과 로그 저장 ────────────────────────────────
@router.post("/entry-log")
async def entry_log(req: EntryLogRequest):
    """
    입구 통과 결과를 Spring Boot gate_entry_log에 저장.
    실제 차단기 개방 여부(gate_open)를 정확히 저장.
    gate_open이 없으면 is_resident 기준으로 처리.
    """
    # gate_open이 명시적으로 전달되면 그 값 사용
    # 없으면 is_resident 기준으로 처리
    gate_open = (
        req.gate_open if req.gate_open is not None
        else req.is_resident
    )

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                SPRING_API["gate_log"],
                json={
                    "c_number":    req.c_number,
                    "is_resident": req.is_resident,
                    "gate_open":   gate_open,  # 실제 개방 여부 저장
                },
                timeout=8,
            )
        print(
            f"[ENTRY LOG] {req.c_number} | "
            f"등록:{req.is_resident} | 개방:{gate_open}"
        )
        return {"result": "ok"}
    except Exception as e:
        print(f"[ENTRY LOG] Spring Boot 전달 실패: {e}")
        return {"result": "fail"}


# ── 3. 관리자 상시개방 상태 조회 ──────────────────────────
@router.get("/gate/control")
async def gate_control(
    apartment_no: Optional[int] = None,
    apartmentNo: Optional[int] = None,
    a_no: Optional[int] = None,
):
    """
    장비가 주기적으로 호출해서 관리자 상시개방 설정을 확인.
    Spring Boot /api/gate/control 값을 그대로 전달한다.
    """
    resolved_apartment_no = resolve_apartment_no(
        apartment_no, apartmentNo, a_no
    )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                SPRING_API["gate_control_base"],
                params={"apartmentNo": resolved_apartment_no},
                timeout=8,
            )

        if response.status_code >= 400:
            print(
                f"[GATE CONTROL] Spring Boot 에러 "
                f"({response.status_code}) | {response.text}"
            )
            return {
                "apartment_no": resolved_apartment_no,
                "gate_open": False,
                "mode": "ERROR",
                "reason": "Spring Boot 상시개방 상태 조회 실패",
            }

        data = response.json()
        return {
            "apartment_no": data.get("apartment_no", resolved_apartment_no),
            "gate_open": bool(data.get("gate_open", False)),
            "mode": data.get("mode", "NORMAL"),
            "gate_force_open_enabled": data.get("gate_force_open_enabled"),
            "force_open_enabled": data.get("force_open_enabled"),
            "reason": data.get("reason"),
        }

    except Exception as e:
        print(f"[GATE CONTROL] Spring Boot 조회 실패: {e}")
        return {
            "apartment_no": resolved_apartment_no,
            "gate_open": False,
            "mode": "ERROR",
            "reason": "Spring Boot 연결 실패",
        }


# ── 4. 아두이노 차단기 상태 확인 ──────────────────────────
@router.get("/gate/status")
async def gate_status(
    plate: str,
    apartment_no: Optional[int] = None,
    apartmentNo: Optional[int] = None,
    a_no: Optional[int] = None,
):
    """
    아두이노 차단기가 특정 번호판의 개방 여부를 확인.
    is_resident가 아닌 Spring Boot 응답의 gate_open 사용.

    이유:
    - 관리자가 차단기 상시개방을 켠 경우
      미등록 차량이어도 gate_open=true가 될 수 있음
    - 관리자가 방문차량 혼잡도 차단을 켠 경우
      등록된 방문차량이어도 gate_open=false가 될 수 있음
    """
    resolved_apartment_no = resolve_apartment_no(
        apartment_no, apartmentNo, a_no
    )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                SPRING_API["gate_check"],
                json={
                    "plate":        plate,
                    "apartment_no": resolved_apartment_no,
                },
                timeout=8,
            )
            data      = response.json()
            gate_open = bool(data.get("gate_open", False))
    except Exception as e:
        print(f"[GATE STATUS] Spring Boot 조회 실패: {e}")
        gate_open = False
        data      = {}

    print(f"[GATE STATUS] {plate} → gate_open: {gate_open}")
    return {
        "plate":                   data.get("plate", plate),
        "apartment_no":            data.get("apartment_no", resolved_apartment_no),
        "gate_open":               gate_open,
        "reason":                  data.get("reason"),
        "force_open_enabled":      data.get("force_open_enabled"),
        "occupancy_block_enabled": data.get("occupancy_block_enabled"),
    }


# ── 5. UNKNOWN 주차 기록에서 구역 매칭 ───────────────────
async def find_unmatched_history_id(zone: str) -> int | None:
    """
    Spring Boot에서 번호판이 UNKNOWN인 진행 중 주차 기록을 조회하고
    현재 주차 구역과 일치하는 history_id를 찾아 반환.
    """
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                SPRING_API["unmatched"], timeout=8
            )

        if response.status_code >= 400:
            print(
                f"[ASSIGN] 미매칭 주차 기록 조회 실패: "
                f"{response.status_code}"
            )
            return None

        data = response.json()
        histories = data
        if isinstance(data, dict):
            histories = (
                data.get("histories") or data.get("data") or []
            )

        if not isinstance(histories, list):
            print("[ASSIGN] 미매칭 주차 기록 응답 형식 오류")
            return None

        for item in histories:
            if not isinstance(item, dict):
                continue
            history_zone = (
                item.get("history_zone") or item.get("zone")
            )
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


# ── 6. 번호판 NULL 입차 → 역추적 ─────────────────────────
async def try_assign_plate_to_null_parking(zone: str):
    """
    번호판 NULL로 입차된 구역에 pending_plates에서 번호판 역추적 매칭.
    후보 1개: 즉시 매칭
    후보 여러 개: 30초 대기 후 재시도
    최대 10분 후 실패 시 관리자 알림
    """
    print(f"[ASSIGN] {zone} 번호판 NULL → 역추적 시작")

    max_retries    = 20
    retry_interval = 5

    for attempt in range(max_retries):
        async with pending_lock:
            now = datetime.now()
            expired = [
                p for p in pending_plates
                if (now - p["entered_at"]).total_seconds()
                > GATE_CHECK_MINUTES * 60
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
                print(
                    f"[ASSIGN] {zone} history_id 없음 → "
                    f"{retry_interval}초 후 재시도 "
                    f"({attempt+1}/{max_retries})"
                )
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
                            p for p in pending_plates
                            if p["plate"] != plate
                        ]
                    print(
                        f"[ASSIGN] {zone} history_id:{history_id} "
                        f"→ {plate} 번호판 부여 완료!"
                    )
                    return
                else:
                    print(
                        f"[ASSIGN] Spring Boot 에러: "
                        f"{res.status_code} | {res.text}"
                    )
            except Exception as e:
                print(f"[ASSIGN] Spring Boot 전달 실패: {e}")
            return

        else:
            plate_list = [p["plate"] for p in candidates]
            print(
                f"[ASSIGN] {zone} 후보 {len(candidates)}개 "
                f"→ 대기 중: {plate_list}"
            )
            print(
                f"[ASSIGN] {retry_interval}초 후 재시도 "
                f"({attempt+1}/{max_retries})"
            )
            await asyncio.sleep(retry_interval)

    # 최대 재시도 초과 → 관리자 알림
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
                        "time":       datetime.now().strftime(
                            "%Y-%m-%d %H:%M:%S"
                        ),
                    },
                    timeout=8,
                )
            print(
                f"[ASSIGN] {zone} 최대 재시도 초과 → 알림 저장 "
                f"| 후보: {candidates_str}"
            )
        except Exception as e:
            print(f"[ASSIGN] 알림 저장 실패: {e}")


# ── 7. 외부 호출: 번호판 NULL 입차 시 역추적 시작 ─────────
def start_plate_assignment(zone: str):
    """parking.py에서 번호판 NULL 입차 발생 시 호출."""
    asyncio.create_task(try_assign_plate_to_null_parking(zone))
    print(f"[ASSIGN] {zone} 역추적 백그라운드 시작")
    
# ── 8. 번호판 인식 성공 차량 pending에서 제거 ─────────────
def remove_from_pending(plate: str):
    """번호판 인식 성공한 차량을 pending_plates에서 제거."""
    async def _remove():
        async with pending_lock:
            before = len(pending_plates)
            pending_plates[:] = [
                p for p in pending_plates
                if p["plate"] != plate
            ]
            after = len(pending_plates)
            if before != after:
                print(f"[PENDING] {plate} 인식 성공 → 대기 목록 제거")
    asyncio.create_task(_remove())
