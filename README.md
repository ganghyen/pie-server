-- 주차 관리
CREATE TABLE parking_status (
    status_zone VARCHAR(20) PRIMARY KEY,
    status_type VARCHAR(20) DEFAULT 'empty'
);

-- 구역 초기 데이터
INSERT INTO parking_status (status_zone, status_type) VALUES
('A-1', 'empty'),
('A-2', 'empty'),
('A-3', 'empty'),
('B-1', 'empty'),
('B-2', 'empty'),
('B-3', 'empty');

-- 입출차 기록
CREATE TABLE parking_history (
    history_id         INT AUTO_INCREMENT PRIMARY KEY,
    u_no               INT NOT NULL,
    history_zone       VARCHAR(20) NOT NULL,
    history_plate      VARCHAR(20),
    history_entry_time DATETIME,
    history_exit_time  DATETIME,
    FOREIGN KEY (u_no) REFERENCES user(u_no)
);

-- ── 1. 입구 차단기 통과 로그 ──────────────────────────────
-- 10분 후 자동 삭제
CREATE TABLE gate_entry_log (
    log_no           INT AUTO_INCREMENT PRIMARY KEY,
    gate_plate       VARCHAR(20) NOT NULL,          -- 인식된 번호판
    gate_is_resident BOOLEAN     DEFAULT FALSE,     -- 등록 차량 여부
    gate_open        BOOLEAN     DEFAULT FALSE,     -- 차단기 열림 여부
    gate_time        DATETIME    DEFAULT CURRENT_TIMESTAMP  -- 통과 시간
);

-- ── 2. 이중주차 알림 ──────────────────────────────────────
-- 번호판 역추적 실패 시 (후보 2대 이상)
-- 차주들 + 관리자 알림용
CREATE TABLE double_park_alert (
    alert_id         INT AUTO_INCREMENT PRIMARY KEY,
    alert_time       DATETIME,   -- 감지 시간
    alert_candidates TEXT,       -- 후보 차량 번호판 목록 (쉼표 구분)
    alert_resolved   BOOLEAN DEFAULT FALSE  -- 해결 여부
);
