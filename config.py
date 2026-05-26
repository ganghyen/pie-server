# ============================================================
# 서버 설정
# ============================================================

# ── Spring Boot 서버 주소 ──────────────────────────────────
# ⚠️ 수정 필요: Spring Boot EC2 주소 받으면 여기만 변경
# 예시: "http://12.34.56.78:8080"
SPRING_BOOT_URL = "http://172.20.10.13:8080"

# ── Spring Boot API 엔드포인트 ────────────────────────────
# ⚠️ 수정 필요: Spring Boot 팀이 만들어준 API 경로에 맞게 수정
SPRING_API = {
    # 주차 이벤트
    # ⚠️ Spring Boot 팀에게 요청: 구역 현재 상태 조회 API
    "zone_status":  f"{SPRING_BOOT_URL}/api/parking/zone",

    # ⚠️ Spring Boot 팀에게 요청: 입차 저장 API
    # parking_status UPDATE (occupied) + parking_history INSERT
    # car 테이블에서 c_number로 u_no 조회해서 저장
    "entry":        f"{SPRING_BOOT_URL}/api/parking/entry",

    # ⚠️ Spring Boot 팀에게 요청: 출차 저장 API
    # parking_status UPDATE (empty) + parking_history exit_time UPDATE
    "exit":         f"{SPRING_BOOT_URL}/api/parking/exit",

    # ⚠️ Spring Boot 팀에게 요청: 번호판 업데이트 API
    # parking_history history_plate UPDATE
    "update_plate": f"{SPRING_BOOT_URL}/api/parking/update-plate",

    # 차량 조회
    # ⚠️ Spring Boot 팀에게 요청: 등록 차량 전체 목록 API
    # car + registered_cars 테이블에서 c_number 전체 반환
    # 반환 형식: [{"c_number": "12가1234"}, ...]
    "cars":         f"{SPRING_BOOT_URL}/api/parking/cars",

    # 입구 차단기
    # ⚠️ Spring Boot 팀에게 요청: 등록 차량 확인 API
    # car + registered_cars 에서 번호판 조회
    # 반환 형식: {"is_resident": true/false}
    "gate_check":   f"{SPRING_BOOT_URL}/api/gate/check",

    # ⚠️ Spring Boot 팀에게 요청: 입구 통과 로그 저장 API
    # gate_entry_log INSERT
    "gate_log":     f"{SPRING_BOOT_URL}/api/gate/log",

    # 이중주차 역추적
    # ⚠️ Spring Boot 팀에게 요청: 번호판 없는 주차 기록 조회 API
    # parking_history WHERE history_plate IS NULL AND history_exit_time IS NULL
    # 반환 형식: [{"history_id": 1, "history_zone": "A-1"}, ...]
    "unmatched":    f"{SPRING_BOOT_URL}/api/gate/unmatched",

    # ⚠️ Spring Boot 팀에게 요청: 번호판 자동 부여 API
    # parking_history UPDATE history_plate WHERE history_id = ?
    # car 테이블에서 c_number로 u_no 조회해서 u_no도 업데이트
    "assign_plate": f"{SPRING_BOOT_URL}/api/gate/assign-plate",

    # ⚠️ Spring Boot 팀에게 요청: 이중주차 알림 저장 API
    # double_park_alert INSERT + 차주/관리자 FCM 알림 발송
    "alert":        f"{SPRING_BOOT_URL}/api/gate/alert",
}

# ── 번호판 유사도 매칭 임계값 ──────────────────────────────
# 1: 글자 1개 차이까지 보정
# 2: 글자 2개 차이까지 보정
# ⚠️ 수정 가능: 인식률에 따라 조절
PLATE_MATCH_THRESHOLD = 2

# ── 입구 통과 후 주차 확인 시간 (분) ──────────────────────
# ⚠️ 수정 가능: 테스트 시 1~2분으로 줄여서 테스트
GATE_CHECK_MINUTES = 10
