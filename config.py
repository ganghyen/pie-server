# ============================================================
# 서버 설정
# ============================================================

# Spring Boot 서버 주소
# ⚠️ 시연 전 실제 실행 환경 IP로 확인 필요
SPRING_BOOT_URL = "http://springweb-env.eba-shnyk9a8.ap-northeast-2.elasticbeanstalk.com"

# 현재 Python 장비가 연결된 아파트 번호
# 여러 아파트를 운영하면 장비별로 이 값을 바꿔서 사용
APARTMENT_NO = 1

# Spring Boot API 엔드포인트 전체 목록
SPRING_API = {
    # 특정 주차칸 현재 상태 조회
    "zone_status":   f"{SPRING_BOOT_URL}/api/parking/zone",

    # 입차 이벤트 저장
    "entry":         f"{SPRING_BOOT_URL}/api/parking/entry",

    # 출차 이벤트 저장
    "exit":          f"{SPRING_BOOT_URL}/api/parking/exit",

    # 번호판 업데이트
    "update_plate":  f"{SPRING_BOOT_URL}/api/parking/update-plate",

    # 등록 차량 전체 번호판 목록 조회 (OCR 보정용)
    # 반환 형식: [{"c_number": "12가1234"}, ...]
    "cars":          f"{SPRING_BOOT_URL}/api/parking/cars",

    # 입구 차단기: 번호판 확인 + 차단기 개방 여부 반환
    # 반환 형식: {"gate_open": true/false, "is_resident": true/false, ...}
    "gate_check":    f"{SPRING_BOOT_URL}/api/gate/check",

    # 입구 통과 로그 저장
    "gate_log":      f"{SPRING_BOOT_URL}/api/gate/log",

    # 번호판 NULL인 진행 중 주차 기록 조회 (역추적용)
    "unmatched":     f"{SPRING_BOOT_URL}/api/gate/unmatched",

    # 차단기 인식 번호판을 UNKNOWN 주차 기록에 연결
    "assign_plate":  f"{SPRING_BOOT_URL}/api/gate/assign-plate",

    # 이중주차 알림 + OCR 오류 알림
    "alert":         f"{SPRING_BOOT_URL}/api/gate/alert",

    # 차단기 상시개방 상태 조회 (관리자 설정용)
    # 반환 형식: {"gate_open": true/false, "mode": "FORCE_OPEN", ...}
    "gate_control_base": f"{SPRING_BOOT_URL}/api/gate/control",
    "gate_control":  f"{SPRING_BOOT_URL}/api/gate/control?apartmentNo={APARTMENT_NO}",
}

# Levenshtein 거리 기반 번호판 보정 임계값
PLATE_MATCH_THRESHOLD = 2

# 입구 통과 후 주차 확인 대기 시간 (분)
GATE_CHECK_MINUTES = 10