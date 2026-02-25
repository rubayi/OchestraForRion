# OchestraForRion — 설계 문서

> **궁극적 목표: 실제로 수익을 내는 AI 트레이딩 시스템**

---

## 프로젝트 개요

**OchestraForRion**은 기존 [AlgoTradingBot (RionFX)](https://github.com/rubayi/AlgoTradingBot)의 AI 의사결정 레이어를 멀티에이전트 구조로 고도화하는 독립 프로젝트입니다.

- GitHub: https://github.com/rubayi/OchestraForRion

- 기존 봇: 패턴 감지 + Gemini 1번 호출로 모든 판단
- 이 프로젝트: 전문화된 AI 에이전트들의 파이프라인 → 판단 정확도 향상

---

## 설계 배경

### 현재 AlgoTradingBot 성과 (2026-02 기준)
| 패턴 | 거래 수 | 승률 | 평균 수익 | 특이사항 |
|------|---------|------|-----------|----------|
| bamboo | 22건 | 73% | +4.5 pips | ✅ 가장 안정적 |
| manual | 43건 | 67% | -2.1 pips | ⚠️ 손실 평균 -16.7 pips — 수동 개입이 성과 갉아먹음 |
| ma_convergence | 4건 | 50% | -5.2 pips | ⚠️ 개선 필요 |
| ma_box | 1건 | 0% | -32.7 pips | 🔴 전략 재검토 필요 |

**핵심 문제**: Gemini Flash 1번 호출로 시장분석 + 진입판단 + 리스크를 전부 처리 → 각 역할의 깊이 부족

### OpenClaw 개념 적용 (참조: https://www.youtube.com/watch?v=_kZCoW-Qxnc)
- **두뇌/근육 분리**: 비싼 모델은 결정에만, 저렴한 모델은 반복 작업에
- **역프롬프팅**: AI에게 "진입할까?"가 아닌 "지금 뭘 해야 수익날까?" 먼저 물음
- **자기학습**: 거래 결과를 피드백으로 순환

---

## 아키텍처

### 두뇌/근육 모델 분리 전략

```
Claude Haiku (근육, 저렴)          Claude Opus 4.6 (두뇌, 고급)
──────────────────────────         ────────────────────────────
Agent 1: 시장 분석 필터    ──Y──▶  Agent 2: 최종 진입 판단
Agent 3: SL/TP 계산                (패턴 통과한 것만 Opus 호출)
Agent 4: 거래 성과 분석
```

**비용 추정**:
- 하루 패턴 감지 ~10회 → Haiku 필터 후 ~2-3회만 Opus 호출
- Opus 4.6: ~$0.015/call × 3회/일 = **$0.045/일**
- Haiku 4.5: ~$0.0002/call × 10회/일 = **$0.002/일**
- 현재 Gemini Flash 대비 비용 소폭 증가, 정확도 대폭 향상

### 4개 에이전트 상세

#### Agent 1: 시장 분석가 (Market Analyst)
- **모델**: Claude Haiku 4.5 (빠름, 저렴)
- **입력**: `rion_data_now.json` (MT5 실시간 데이터)
- **역할**: 시장 컨텍스트 분석, 패턴 유효성 1차 필터
- **출력**: `{"pass": true/false, "context": "...", "strength": 0-100}`
- **판단 기준**:
  - H4 + H1 추세 정렬 여부
  - 주요 지지/저항선 근접도
  - MA 배열 상태 (200/75/20 순서)
  - 시장 세션 (도쿄/런던/뉴욕)

#### Agent 2: 매매 결정 (Trade Decision) ← 두뇌
- **모델**: Claude Opus 4.6 (정확, 비쌈 → Agent 1 통과 시만 호출)
- **입력**: Agent 1 리포트 + 전체 시장 데이터 + 거래 히스토리 요약
- **역할**: 최종 진입/스킵 판단 + 확신도
- **출력**: `{"decision": "ENTER/SKIP", "confidence": 0-100, "reason": "..."}`

#### Agent 3: 리스크 관리 (Risk Manager)
- **모델**: Claude Haiku 4.5
- **입력**: Agent 2 진입 신호 + 현재 계좌 상태
- **역할**: SL/TP 최적화, 포지션 사이즈 계산
- **출력**: `{"sl_pips": N, "tp_pips": N, "lot_size": N, "rr_ratio": N}`
- **참고**: 최근 3거래 손실 시 자동으로 보수적 모드

#### Agent 5: 개발자 에이전트 (Developer Agent) ← 최종 목표
- **모델**: Claude Opus 4.6 (코드 수정은 정확성이 최우선)
- **실행 주기**: Agent 4 리포트 생성 후 자동 트리거
- **입력**: Agent 4 분석 리포트 + AlgoTradingBot 소스코드 (rion_watcher.py, params.json)
- **역할**: 성과 분석 기반으로 실제 코드/파라미터를 수정하는 **자율 개발자**
- **동작 흐름**:
  ```
  Agent 4: "ma_box 전략 -32 pips, 비활성화 권장"
      ↓
  Developer Agent:
    1. rion_watcher.py + params.json 읽음
    2. 문제 원인 분석 (코드 레벨)
    3. 수정안 생성 (diff 형태)
    4. Telegram으로 사용자에게 전송:
       "이렇게 수정할까요? ✅승인 / ❌거절"
      ↓
  사용자 ✅ 클릭
      ↓
    5. 실제 코드 수정 적용
    6. git commit & push
    7. 봇 재시작
    8. 결과 모니터링
  ```
- **할 수 있는 것들**:
  - `params.json` 파라미터 자동 최적화
  - 손실 패턴 전략 비활성화/수정
  - 새로운 패턴 감지 조건 추가
  - SL/TP 로직 개선
  - AI 프롬프트(RionFX_Persona_AI.md) 개선
- **안전장치**:
  - 모든 변경은 **사용자 Telegram 승인 필수**
  - 자동 git backup (변경 전 항상 커밋)
  - 라이브 계좌에는 절대 미적용 (Demo only)
  - 변경 후 24시간 성과 모니터링 후 보고

#### Agent 4: 성과 분석가 (Performance Analyst) ← 최우선 구현
- **모델**: Claude Haiku 4.5
- **실행 주기**: 매일 새벽 3시 (한국시간) 또는 10거래마다
- **입력**: `data/trades.db` 최근 N건
- **역할**: 패턴별 승률/RR 분석 → params.json 개선안 Telegram 전송
- **출력 예시**:
  ```
  📊 [RionAgent 일일 성과 리포트]
  - bamboo: 73% 승률, 평균 +4.5 pips ✅ 계속 사용
  - manual 개입: 평균 -16.7 pips 손실 ⚠️ 줄일 것
  - ma_box: -32.7 pips 🔴 비활성화 권장
  💡 제안: stop_loss_pips 18→15, bamboo 비중 확대
  ```

---

## 데이터 흐름

```
[AlgoTradingBot]                    [RionAgent]
    │                                    │
    ├── rion_data_now.json  ──────────▶  Agent 1 (Haiku)
    │   (MT5 실시간)                          │ pass=Y
    │                                         ▼
    │                                    Agent 2 (Opus)
    │                                         │ ENTER
    │                                         ▼
    │                                    Agent 3 (Haiku)
    │                                         │
    │◀── signal.json ─────────────────────────┘
    │   (진입신호 + SL/TP)
    │
    ├── data/trades.db  ────────────▶   Agent 4 (Haiku)
    │   (거래 기록)                          │ 매일 새벽
    │                                         ▼
    │                                    Telegram 리포트
    │◀── params_suggestion.json ──────────────┘
        (파라미터 개선안)
```

---

## 구현 일정

### Phase 0: 준비 (현재 완료)
- [x] 프로젝트 설계 문서 작성
- [x] GitHub repo 생성 (`rubayi/rion-agent`)
- [x] 기본 폴더 구조 스캐폴드

### Phase 1: Agent 4 구현 (다음 세션 우선)
> **이유**: 이미 쌓인 데이터로 즉시 가치 창출 가능. 코드 범위 작음.

- [ ] `bridge/trade_db_reader.py` — trades.db 읽기 + 통계 계산
- [ ] `agents/performance_analyst.py` — Haiku 기반 성과 분석
- [ ] `orchestrator.py` — Agent 4 단독 실행 스케줄러
- [ ] AlgoTradingBot Telegram으로 일일 리포트 전송 연동
- [ ] 테스트: 현재 91건 데이터로 검증

### Phase 2: Agent 1 구현
> **이유**: 현재 Gemini가 하는 분석을 전용 에이전트로 분리

- [ ] `bridge/mt5_reader.py` — rion_data_now.json 파싱
- [ ] `agents/market_analyst.py` — Haiku 시장 필터
- [ ] AlgoTradingBot의 `trigger_pattern_analysis()` 와 연동
- [ ] 필터 성능 검증 (False positive 줄이는지 확인)

### Phase 3: Agent 2 구현 (두뇌)
> **이유**: 가장 비용이 크므로 1,2단계 검증 후 도입

- [ ] `agents/trade_decision.py` — Opus 4.6 최종 판단
- [ ] 현재 `analyze_with_gemini()` 대체
- [ ] A/B 테스트: Gemini vs Opus 정확도 비교 (2주)
- [ ] 비용 모니터링 대시보드

### Phase 4: Agent 3 + 역프롬프팅
- [ ] `agents/risk_manager.py` — 동적 SL/TP
- [ ] 역프롬프팅: 30분마다 Opus가 먼저 시장 스캔
- [ ] Mission Control 대시보드 (Telegram 강화)

### Phase 5: 자기학습 루프 완성
- [ ] Agent 4 → params_suggestion.json → 자동 적용 (승인 후)
- [ ] 월간 성과 리포트 자동화
- [ ] 손실 패턴 자동 감지 + 전략 조정

### Phase 6: Developer Agent 구현 ← 최종 목표
> **"AI가 직접 코드를 고쳐서 수익을 개선하는 자율 개발자"**

- [ ] `agents/developer_agent.py` — Opus 기반 코드 수정 에이전트
- [ ] Telegram 승인 인터페이스 (✅/❌ 버튼)
- [ ] git auto-backup + apply 파이프라인
- [ ] 봇 자동 재시작 + 성과 모니터링
- [ ] params.json 자동 최적화 (승인 기반)
- [ ] RionFX_Persona_AI.md 프롬프트 자동 개선
- [ ] **Demo → 실계좌 전환 기준 수립** (승률 75%+ 유지 2주)

---

## Claude API 사용량 관리 전략

```
패턴 감지 10회/일 가정:
┌─────────────────────────────────────────────────┐
│ Agent 1 (Haiku): 10회 × $0.0002 = $0.002/일    │
│ Agent 2 (Opus):  3회  × $0.015  = $0.045/일    │  ← 필터 후 ~30%만
│ Agent 3 (Haiku): 3회  × $0.0002 = $0.001/일    │
│ Agent 4 (Haiku): 1회  × $0.001  = $0.001/일    │
│                                  ────────────── │
│ 합계:                             ~$0.05/일     │
│ 월간:                             ~$1.5/월      │
└─────────────────────────────────────────────────┘
```

**비용 초과 방지 규칙**:
1. Agent 1이 `pass=false`면 Opus 절대 호출 안 함
2. 하루 Opus 최대 호출 횟수: 5회 하드캡
3. 동일 패턴 5분 내 재감지 시 스킵 (이미 있는 쿨다운 활용)

---

## 기술 스택

```
Python 3.11+
anthropic>=0.50.0          # Claude Haiku + Opus
python-telegram-bot>=20.0  # Telegram 연동
sqlite3                    # trades.db 읽기 (내장)
schedule                   # Agent 4 주기 실행
python-dotenv              # API 키 관리
```

---

## 연동 방식 (AlgoTradingBot과의 인터페이스)

### 옵션 A: 파일 기반 (1단계, 단순)
```
rion-agent → signal.json → AlgoTradingBot 읽어서 진입
```

### 옵션 B: subprocess (2단계)
```
AlgoTradingBot이 패턴 감지 시 rion-agent 서브프로세스 호출
```

### 옵션 C: 완전 통합 (최종)
```
rion-agent가 메인 오케스트레이터
AlgoTradingBot은 MT5 실행 레이어만 담당
```

**현재 채택**: 옵션 A → B → C 순서로 점진 전환

---

## 참고 자료
- OpenClaw 개념: https://www.youtube.com/watch?v=_kZCoW-Qxnc
- AlgoTradingBot: https://github.com/rubayi/AlgoTradingBot
- Anthropic API: https://docs.anthropic.com
