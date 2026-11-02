# ============================================================
# 카메라 상태 수신 + DB 비교 → 불일치 구역 exit 처리
# ============================================================

from fastapi import APIRouter
from pydantic import BaseModel
from typing import Optional
from datetime import datetime
import httpx
from config import SPRING_API

router = APIRouter()


class ZoneInfo(BaseModel):
    status: str              # empty / occupied / timeout
    plate:  Optional[str] = None


class StatusReport(BaseModel):
    zones: dict[str, ZoneInfo]  # 전체 구역 상태


@router.post("/status/report")
async def receive_status_report(report: StatusReport):
    """
    카메라에서 주기적으로 전체 구역 상태 수신.
    DB는 occupied인데 카메라는 empty인 구역 → exit 전송.
    """
    now      = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    mismatch = []  # 불일치 구역 목록

    for zone_name, zone_info in report.zones.items():
        cam_status = zone_info.status.lower()

        # 카메라가 EMPTY인 구역만 체크
        # (OCCUPIED/TIMEOUT은 카메라가 알아서 처리)
        if cam_status != "empty":
            continue

        # DB 조회
        try:
            async with httpx.AsyncClient() as client:
                res = await client.get(
                    f"{SPRING_API['zone_status']}/{zone_name}",
                    timeout=8,
                )
                if res.status_code >= 400:
                    continue
                db_data   = res.json()
                db_status = db_data.get("status_type", "unknown").lower()
        except Exception as e:
            print(f"[StatusReport] {zone_name} DB 조회 실패: {e}")
            continue

        # ✅ DB는 occupied인데 카메라는 empty → exit 전송
        if db_status in ("occupied", "parked"):
            print(f"[StatusReport] 불일치 감지! "
                  f"{zone_name} DB:{db_status} / CAM:empty → exit 전송")
            try:
                async with httpx.AsyncClient() as client:
                    res = await client.post(
                        SPRING_API["exit"],
                        json={
                            "zone":      zone_name,
                            "exit_time": now,
                        },
                        timeout=8,
                    )
                if res.status_code < 400:
                    print(f"[StatusReport] {zone_name} exit 전송 성공")
                    mismatch.append(zone_name)
                else:
                    print(f"[StatusReport] {zone_name} exit 전송 실패 ({res.status_code})")
            except Exception as e:
                print(f"[StatusReport] {zone_name} exit 전송 오류: {e}")

    return {
        "result":    "ok",
        "checked":   len(report.zones),
        "mismatch":  mismatch,
        "exit_sent": len(mismatch),
    }