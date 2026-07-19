# 자산 대시보드 (asset.mossol.net)

나무증권 보유종목을 직접 입력해 두면, 무료 시세(FinanceDataReader/yfinance)와 환율을 붙여
계좌별·종목별 평가손익·비중을 보여주는 개인용 대시보드입니다. 브로커 API를 쓰지 않습니다.

## 구성
- FastAPI + Jinja2, SQLite(보유종목 저장)
- HTTP Basic 인증(1인용). 비밀번호는 `ASSET_PASSWORD` 환경변수
- US 티커(SKHY)는 yfinance, 국내 6자리 코드(005930)는 FinanceDataReader로 조회
- 금·현금처럼 조회 불가 항목은 `market=MANUAL` + 수동 평가액

## 로컬 실행
```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
ASSET_PASSWORD=원하는비번 ./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8842
```

## NAS 배포 (Container Manager)
1. `data/` 디렉토리를 NAS에 미리 생성(bind mount source, 자동생성 안 됨)
2. 프로젝트를 NAS로 복사: `scp -O -r ./ mossol:/volume1/docker/asset/`
3. `ASSET_PASSWORD`를 넣어 빌드/기동:
   ```bash
   ASSET_PASSWORD=원하는비번 docker compose up -d --build
   ```
4. DSM 리버스 프록시: `asset.mossol.net` → `localhost:8842` (WebSocket 불필요)
5. Let's Encrypt SAN에 `asset.mossol.net` 추가, DNSZi A 레코드(NAS IP) 등록
6. 첫 접속 시 `seed_positions.csv`로 보유종목 자동 시드(DB 비어있을 때만)

## 매매 후
`종목 관리`에서 수량·평단만 수정하면 됩니다. 스크린샷 불필요.

## 데이터
- 보유종목: `data/asset.db` (SQLite)
- 시드: `seed_positions.csv`
- 비주식 자산(은행/금/스톡옵션) 구글 시트 합산은 다음 단계(Task #4)
