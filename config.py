# ============================================================
# 서버 설정
# ============================================================

# Spring Boot 서버 주소 (포트 8080)
SPRING_BOOT_URL = "http://localhost:8080"

# Spring Boot API 엔드포인트 전체 목록
SPRING_API = {
    # 특정 주차칸 현재 상태 조회 (occupied/empty)
    "zone_status":  f"{SPRING_BOOT_URL}/api/parking/zone",
    # 입차 이벤트 저장 (parking_zone occupied + parking_history INSERT)
    "entry":        f"{SPRING_BOOT_URL}/api/parking/entry",
    # 출차 이벤트 저장 (parking_zone empty + parking_history exit_time 갱신)
    "exit":         f"{SPRING_BOOT_URL}/api/parking/exit",
    # 번호판 업데이트 (parking_history history_plate 갱신)
    "update_plate": f"{SPRING_BOOT_URL}/api/parking/update-plate",
    # 등록 차량 전체 번호판 목록 조회 (OCR 보정용)
    # 반환 형식: [{"c_number": "12가1234"}, ...]
    "cars":         f"{SPRING_BOOT_URL}/api/cars",
    # 입구 차단기: 번호판이 등록 차량인지 확인
    # 반환 형식: {"is_resident": true/false}
    "gate_check":   f"{SPRING_BOOT_URL}/api/gate/check",
    # 입구 통과 로그 저장 (gate_entry_log INSERT)
    "gate_log":     f"{SPRING_BOOT_URL}/api/gate/log",
    # 번호판 NULL인 진행 중 주차 기록 조회 (역추적용)
    # 반환 형식: [{"history_id": 1, "history_zone": "A-1"}, ...]
    "unmatched":    f"{SPRING_BOOT_URL}/api/gate/unmatched",
    # 차단기 인식 번호판을 UNKNOWN 주차 기록에 연결
    "assign_plate": f"{SPRING_BOOT_URL}/api/gate/assign-plate",
    # 이중주차 알림 저장 + FCM 발송 요청
    "alert":        f"{SPRING_BOOT_URL}/api/gate/alert",
}

# Levenshtein 거리 기반 번호판 보정 임계값
# 1: 글자 1개 차이까지 보정 / 2: 글자 2개 차이까지 보정
PLATE_MATCH_THRESHOLD = 2

# 입구 통과 후 주차 확인 대기 시간 (분)
# 이 시간 내에 주차 구역에서 번호판 매칭 시도
GATE_CHECK_MINUTES = 10