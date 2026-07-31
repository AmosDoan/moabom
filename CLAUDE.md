# 모아봄 (moabom) — Claude 셋업·개발 안내

개인 자산·포트폴리오 셀프호스트 대시보드. FastAPI + Jinja2 + SQLite. 무료 시세(yfinance·FinanceDataReader).

## 처음 사용자 자동 셋업 (사용자가 "셋업해줘"라고 하면 이 순서대로)

1. **Docker 확인**: `docker --version`, `docker compose version`. 없으면 설치를 안내한다.
2. **`.env` 생성**: `.env.example`을 `.env`로 복사. `ASSET_USER`는 사용자에게 원하는 아이디를 묻고, `ASSET_PASSWORD`는 강한 값을 제안하거나 사용자가 직접 넣게 한다(평문을 대화에 남기지 말 것).
3. **데이터 폴더**: `mkdir -p data` (SQLite·비밀키가 저장되는 볼륨 마운트 대상).
4. **실행**: `docker compose up -d --build`.
5. **확인**: 잠시 후 `curl -s http://localhost:8842/health` 가 `{"ok":true}` 인지 확인. 사용자에게 브라우저로 `http://localhost:8842` 접속 → `.env`의 아이디/비밀번호로 로그인하라고 안내.
6. **첫 데이터**: 처음엔 예시 종목(`seed_positions.example.csv`)이 들어가 있다. 상단 **종목 관리 → 종목 추가/수정/삭제**로 본인 보유 종목을 넣게 안내. (예시는 지우면 됨.)

## 보유 종목 입력 방법 (사용자에게 설명)

- 기본은 **웹 UI**: `종목 관리`에서 직접 추가. 매매는 각 종목 **거래** 버튼으로 수량·체결가만 넣으면 평단 자동 계산.
- 컬럼: `account`(계좌 이름 자유), `name`, `ticker`(미국=AAPL, 국내=6자리코드 005930, 일본=4689.T), `market`(US/KR/JP/MANUAL), `currency`(USD/KRW/JPY), `shares`, `avg_cost`, `manual_value_krw`(MANUAL 전용).
- 현금·금처럼 시세 조회가 안 되는 건 `market=MANUAL` + `manual_value_krw`에 원화 평가액.

## 환경변수

`ASSET_USER`(필수) · `ASSET_PASSWORD`(필수, 웹에서 변경 가능) · `TZ`(기본 Asia/Seoul).

## 구조

- `app/main.py` — 라우트(대시보드, 종목 CRUD/거래, 종목 상세, 시장, 로그인/비번, `/api/live` 폴링)
- `app/prices.py` — 시세·환율·52주고·뉴스·기업지표·지수·장 세션. stale-while-revalidate 캐시(요청은 캐시 즉시 반환, 갱신은 백그라운드) + 시작 시 워밍.
- `app/db.py` — SQLite(positions/settings/change_log/net_worth_history)
- `templates/` — Jinja2. `base.html`(헤더·테마·비공개), `dashboard.html`, `stock.html`, `market.html` 등
- 데이터·비밀은 `data/`(SQLite, 서명키, 서비스계정키)에만. git에 안 올라감.

## 개발 규칙

- **커밋 메시지에 서명/Generated 문구 금지.** author는 저장소 소유자 단독.
- 비밀값(`.env`, `data/`, 실제 `seed_positions.csv`)은 절대 커밋하지 않는다. `.gitignore` 유지.
- 시세는 무료·지연 데이터. 이 앱은 개인 자산 정리용이며 투자 자문이 아니다.
- 외부 노출 시 반드시 HTTPS + 강한 비밀번호(리버스 프록시로 8842 포트에 붙임). 또는 LAN/VPN 전용.
- `docker-compose.yml`은 `app/`·`templates/`·`static/`을 bind mount + uvicorn `--reload`라, 코드 수정은 재빌드 없이 반영된다. `requirements.txt`를 바꿀 때만 `--build`.
