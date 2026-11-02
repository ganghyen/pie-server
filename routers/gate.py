# ============================================================
# 입구 차단기 라우터
# ============================================================

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import httpx
import asyncio
from config import SPRING_API, GATE_CHECK_MINUTES, APARTMENT_NO

router = APIRouter()

pending_plates: list[dict] = []
pending_lock = asyncio.Lock()

_assign_task_running = False
_assign_task_lock    = asyncio.Lock()


class CheckPlateRequest(BaseModel):
    plate:        str
    apartment_no: Optional[int] = None
    apartmentNo:  Optional[int] = None
    a_no:         Optional[int] = None


class EntryLogRequest(BaseModel):
    c_number:    str
    is_resident: bool
    gate_open:   Optional[bool] = None


def resolve_apartment_no(
    apartment_no: Optional[int] = None,
    apartmentNo:  Optional[int] = None,
    a_no:         Optional[int] = None,
) -> int:
    return apartment_no or apartmentNo or a_no or APARTMENT_NO


# ── 1. 등록 차량 확인 + 차단기 제어 ──────────────────────
@router.post("/check-plate")
async def check_plate(req: CheckPlateRequest):
    apartment_no = resolve_apartment_no(
        req.apartment_no, req.apartmentNo, req.a_no
    )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                SPRING_API["gate_check"],
                json={"plate": req.plate, "apartment_no": apartment_no},
                timeout=8,
            )

        if response.status_code >= 400:
            print(f"[CHECK PLATE] Spring Boot 에러 ({response.status_code})")
            return {
                "plate": req.plate, "apartment_no": apartment_no,
                "is_resident": False, "is_registered": False,
                "gate_open": False, "reason": "Spring Boot 차량 확인 실패"
            }

        data = response.json()

    except Exception as e:
        print(f"[CHECK PLATE] Spring Boot 조회 실패: {e}")
        return {
            "plate": req.plate, "apartment_no": apartment_no,
            "is_resident": False, "is_registered": False,
            "gate_open": False, "reason": "Spring Boot 연결 실패"
        }

    gate_open     = bool(data.get("gate_open", False))
    is_registered = bool(data.get("is_registered", data.get("is_resident", False)))

    if gate_open and is_registered:
        async with pending_lock:
            already_exists = any(p["plate"] == req.plate for p in pending_plates)
            if not already_exists:
                pending_plates.append({
                    "plate":      req.plate,
                    "entered_at": datetime.now()
                })
                print(f"[PENDING] {req.plate} 대기 목록 추가 (총 {len(pending_plates)}개)")
            else:
                print(f"[PENDING] {req.plate} 이미 대기 중 → 중복 추가 생략")

    print(f"[CHECK PLATE] {req.plate} | registered:{is_registered} | gate_open:{gate_open}")

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
    gate_open = req.gate_open if req.gate_open is not None else req.is_resident

    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                SPRING_API["gate_log"],
                json={
                    "c_number":    req.c_number,
                    "is_resident": req.is_resident,
                    "gate_open":   gate_open,
                },
                timeout=8,
            )
        print(f"[ENTRY LOG] {req.c_number} | 등록:{req.is_resident} | 개방:{gate_open}")
        return {"result": "ok"}
    except Exception as e:
        print(f"[ENTRY LOG] Spring Boot 전달 실패: {e}")
        return {"result": "fail"}


# ── 3. 관리자 상시개방 상태 조회 ──────────────────────────
@router.get("/gate/control")
async def gate_control(
    apartment_no: Optional[int] = None,
    apartmentNo:  Optional[int] = None,
    a_no:         Optional[int] = None,
):
    resolved = resolve_apartment_no(apartment_no, apartmentNo, a_no)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                SPRING_API["gate_control_base"],
                params={"apartmentNo": resolved},
                timeout=8,
            )
        if response.status_code >= 400:
            return {"apartment_no": resolved, "gate_open": False, "mode": "ERROR"}
        data = response.json()
        return {
            "apartment_no":            data.get("apartment_no", resolved),
            "gate_open":               bool(data.get("gate_open", False)),
            "mode":                    data.get("mode", "NORMAL"),
            "gate_force_open_enabled": data.get("gate_force_open_enabled"),
            "force_open_enabled":      data.get("force_open_enabled"),
            "reason":                  data.get("reason"),
        }
    except Exception as e:
        print(f"[GATE CONTROL] Spring Boot 조회 실패: {e}")
        return {"apartment_no": resolved, "gate_open": False, "mode": "ERROR"}


# ── 4. 아두이노 차단기 상태 확인 ──────────────────────────
@router.get("/gate/status")
async def gate_status(
    plate: str,
    apartment_no: Optional[int] = None,
    apartmentNo:  Optional[int] = None,
    a_no:         Optional[int] = None,
):
    resolved = resolve_apartment_no(apartment_no, apartmentNo, a_no)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                SPRING_API["gate_check"],
                json={"plate": plate, "apartment_no": resolved},
                timeout=8,
            )
            data      = response.json()
            gate_open = bool(data.get("gate_open", False))
    except Exception as e:
        print(f"[GATE STATUS] Spring Boot 조회 실패: {e}")
        gate_open = False
        data      = {}

    return {
        "plate":                   data.get("plate", plate),
        "apartment_no":            data.get("apartment_no", resolved),
        "gate_open":               gate_open,
        "reason":                  data.get("reason"),
        "force_open_enabled":      data.get("force_open_enabled"),
        "occupancy_block_enabled": data.get("occupancy_block_enabled"),
    }


# ── 5. UNKNOWN 주차 기록 전체 조회 ───────────────────────
async def get_all_unmatched_histories() -> list[dict]:
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(SPRING_API["unmatched"], timeout=8)
        if response.status_code >= 400:
            return []
        data = response.json()
        if isinstance(data, list):
            return data
        return data.get("histories") or data.get("data") or []
    except Exception as e:
        print(f"[ASSIGN] 미매칭 주차 기록 조회 실패: {e}")
        return []


# ── 6. 번호판 부여 ────────────────────────────────────────
async def assign_plate_to_history(history_id: int, plate: str) -> bool:
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                SPRING_API["assign_plate"],
                json={"history_id": history_id, "plate": plate},
                timeout=8,
            )
        return res.status_code == 200
    except Exception as e:
        print(f"[ASSIGN] 번호판 부여 실패: {e}")
        return False


# ── 7. linked_zone 번호판 업데이트 헬퍼 ──────────────────
async def update_linked_zone(matched_unknown: dict, plate: str):
    from routers.parking import zone_linked_map
    matched_zone = (
        matched_unknown.get("zone") or
        matched_unknown.get("history_zone")
    )
    linked_zone = (
        matched_unknown.get("linked_zone") or
        zone_linked_map.get(matched_zone)
    )
    if linked_zone:
        try:
            async with httpx.AsyncClient() as client:
                await client.post(
                    SPRING_API["update_plate"],
                    json={"zone": linked_zone, "plate": plate},
                    timeout=8,
                )
            print(f"[ASSIGN] linked {linked_zone} → {plate} 업데이트")
        except Exception as e:
            print(f"[ASSIGN] linked 업데이트 실패: {e}")


# ── 8. 전체 역추적 태스크 ─────────────────────────────────
# ✅ immediate=True면 첫 시도 바로 실행 (통로주차용)
# ✅ immediate=False면 기존대로 5초 대기 후 시작 (일반주차용)
async def global_assign_task(immediate: bool = False):
    global _assign_task_running

    max_retries    = 20
    retry_interval = 5

    print(f"[ASSIGN] 전체 역추적 시작 "
          f"(최대 {max_retries}회 × {retry_interval}초 "
          f"{'즉시' if immediate else '5초 후'} 첫 시도)")

    for attempt in range(max_retries):
        # ✅ immediate=True(통로주차)면 첫 시도 바로
        # immediate=False(일반주차)면 기존대로 매번 5초 대기
        if not immediate or attempt > 0:
            await asyncio.sleep(retry_interval)

        async with pending_lock:
            now     = datetime.now()
            expired = [
                p for p in pending_plates
                if (now - p["entered_at"]).total_seconds() > GATE_CHECK_MINUTES * 60
            ]
            for p in expired:
                pending_plates.remove(p)
                print(f"[PENDING] {p['plate']} 만료 제거")
            candidates = list(pending_plates)

        if not candidates:
            print(f"[ASSIGN] 대기 번호판 없음 → 역추적 종료")
            break

        histories = await get_all_unmatched_histories()
        unknowns  = [
            h for h in histories
            if isinstance(h, dict) and
            (h.get("plate") in (None, "", "UNKNOWN") or
             h.get("c_number") in (None, "", "UNKNOWN"))
        ]

        if not unknowns:
            print(f"[ASSIGN] UNKNOWN 주차 기록 없음 → 대기 유지 ({attempt+1}/{max_retries})")
            continue

        print(f"[ASSIGN] 대기:{len(candidates)}개 | 언노운:{len(unknowns)}개 ({attempt+1}/{max_retries})")
        if candidates:
            print(f"[ASSIGN] 대기 목록: {[p['plate'] for p in candidates]}")

        if len(candidates) == 1 and len(unknowns) == 1:
            plate      = candidates[0]["plate"]
            history_id = unknowns[0].get("history_id")
            if history_id and await assign_plate_to_history(int(history_id), plate):
                async with pending_lock:
                    pending_plates[:] = [p for p in pending_plates if p["plate"] != plate]
                print(f"[ASSIGN] 매칭 완료: {plate} → history:{history_id}")
                await update_linked_zone(unknowns[0], plate)
            continue

        if len(candidates) == 1 and len(unknowns) > 1:
            plate  = candidates[0]["plate"]
            latest = max(unknowns, key=lambda h: h.get("entry_time", "") or "")
            history_id = latest.get("history_id")
            if history_id and await assign_plate_to_history(int(history_id), plate):
                async with pending_lock:
                    pending_plates[:] = [p for p in pending_plates if p["plate"] != plate]
                print(f"[ASSIGN] 최근 언노운에 매칭: {plate} → history:{history_id}")
                await update_linked_zone(latest, plate)
            continue

        if len(candidates) > 1 and len(unknowns) == 1:
            print(f"[ASSIGN] 대기 여러개({len(candidates)}) + 언노운 1개 → 특정 불가, 대기 유지")
            continue

        if len(candidates) > 1 and len(unknowns) > 1:
            candidates_str = ",".join([p["plate"] for p in candidates])
            print(f"[ASSIGN] 대기 여러개 + 언노운 여러개 → 알림 전송")
            try:
                async with httpx.AsyncClient() as client:
                    await client.post(
                        SPRING_API["alert"],
                        json={
                            "type":       "assign_fail",
                            "candidates": candidates_str,
                            "time":       datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        },
                        timeout=8,
                    )
            except Exception as e:
                print(f"[ASSIGN] 알림 전송 실패: {e}")

            async with pending_lock:
                pending_plates[:] = [
                    p for p in pending_plates
                    if (datetime.now() - p["entered_at"]).total_seconds()
                    <= GATE_CHECK_MINUTES * 60
                ]
            break

    print(f"[ASSIGN] 전체 역추적 종료")
    async with _assign_task_lock:
        _assign_task_running = False


# ── 9. 역추적 시작 (외부 호출용) ─────────────────────────
# ✅ immediate 파라미터 추가
def start_plate_assignment(zone: str = "", immediate: bool = False):
    async def _start():
        global _assign_task_running
        async with _assign_task_lock:
            if _assign_task_running:
                print(f"[ASSIGN] 역추적 이미 실행 중 → 스킵 (zone={zone})")
                return
            _assign_task_running = True
        asyncio.create_task(global_assign_task(immediate=immediate))
        print(f"[ASSIGN] 전체 역추적 백그라운드 시작 "
              f"(zone={zone} immediate={immediate})")
    asyncio.create_task(_start())


# ── 10. 번호판 인식 성공 차량 pending에서 제거 ────────────
def remove_from_pending(plate: str):
    async def _remove():
        async with pending_lock:
            before = len(pending_plates)
            pending_plates[:] = [p for p in pending_plates if p["plate"] != plate]
            after  = len(pending_plates)
            if before != after:
                print(f"[PENDING] {plate} 인식 성공 → 대기 목록 제거")
    asyncio.create_task(_remove())