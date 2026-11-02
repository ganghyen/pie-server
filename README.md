# Park-On FastAPI 중계 서버

## 1. 프로젝트 역할

`pie-server-master`는 Python 객체인식 모듈과 Spring Boot 백엔드 사이에서 이벤트를 전달하는 FastAPI 중계 서버입니다.

객체인식 모듈이 차량 입차, 출차, 번호판 업데이트 이벤트를 보내면 FastAPI가 요청 형식을 맞춰 Spring Boot API로 전달합니다.

## 2. 주요 기능

- 객체인식 모듈 이벤트 수신
- 입차 이벤트를 Spring Boot `/api/parking/entry`로 전달
- 출차 이벤트를 Spring Boot `/api/parking/exit`로 전달
- 번호판 업데이트를 Spring Boot `/api/parking/update-plate`로 전달
- 등록 차량 목록 조회
- 번호판 자동 보정 후보 처리
- 차단기 번호판 확인 요청 중계
- 관리자 상시개방 상태 조회

## 3. 기술 스택

- Python
- FastAPI
- Uvicorn
- HTTPX
- Pydantic
- python-Levenshtein

## 4. 실행 방법

```powershell
cd C:\Users\xm300\Desktop\졸업작품\python\pie-server-master
pip install -r requirements.txt
uvicorn main:app --reload
```

기본 실행 포트는 `8000`입니다.

## 5. 주요 설정

설정 파일:

```text
config.py
```

확인할 값:

- `SPRING_BOOT_URL`: Spring Boot 백엔드 주소
- `APARTMENT_NO`: 현재 Python 장비가 연결된 아파트 번호
- `SPRING_API["entry"]`: 입차 이벤트 전달 API
- `SPRING_API["exit"]`: 출차 이벤트 전달 API
- `SPRING_API["update_plate"]`: 번호판 업데이트 API
- `SPRING_API["gate_check"]`: 차단기 번호판 확인 API
- `SPRING_API["gate_control"]`: 관리자 상시개방 상태 조회 API

## 6. 전체 연동 흐름

```text
Python 객체인식 모듈
-> FastAPI /api/event
-> Spring Boot 주차 API
-> MySQL DB 저장
-> 관리자 웹/입주민 앱에서 상태 확인
```

차단기 흐름:

```text
입구 카메라 번호판 인식
-> FastAPI가 Spring Boot /api/gate/check 호출
-> Spring Boot가 등록 차량 여부와 차단기 정책 확인
-> gate_open true/false 반환
-> 차단기 장비가 개방 여부 결정
```

## 7. 주의사항

- FastAPI는 DB를 직접 수정하지 않습니다.
- 최종 데이터 저장과 정책 판단은 Spring Boot 백엔드가 담당합니다.
- `config.py`의 서버 주소는 시연 환경 IP 또는 배포 주소에 맞게 확인해야 합니다.
- 과거 SQL 초안에 있던 `parking_status`, `double_park_alert`는 현재 실제 DB 기준 테이블이 아닙니다.
