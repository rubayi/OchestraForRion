# 🎼 OchestraForRion

> GBPAUD 전문 AI 트레이딩 멀티에이전트 시스템 — **목표: 실제 수익**

[AlgoTradingBot](https://github.com/rubayi/AlgoTradingBot)의 AI 의사결정을 멀티에이전트 구조로 고도화합니다.

## 아키텍처

```
패턴 감지
    ↓
Agent 1 (Haiku): 시장 분석 필터
    ↓ pass=Y만
Agent 2 (Opus): 최종 진입 판단  ← 두뇌
    ↓ ENTER
Agent 3 (Haiku): SL/TP 리스크 관리
    ↓
MT5 주문 실행
    ↓ (매일 새벽)
Agent 4 (Haiku): 거래 성과 분석
    ↓
Agent 5 (Opus): Developer Agent ← 코드 자동 수정 + 사용자 승인
```

## 상세 설계

👉 [DESIGN.md](./DESIGN.md) 참조

## 구현 현황

- [x] Phase 0: 설계 문서 + 프로젝트 구조
- [x] Phase 1: Agent 4 (성과 분석 + Telegram 일일 리포트) ✅
  - `bridge/trade_db_reader.py` — trades.db 패턴별 통계 계산
  - `agents/performance_analyst.py` — Haiku 4.5 분석 + Telegram 전송
  - `orchestrator.py` — 매일 03:00 KST 스케줄러 + `--now` 즉시 실행
- [ ] Phase 2: Agent 1 (시장 분석 필터) **← 다음**
- [ ] Phase 3: Agent 2 (Opus 두뇌)
- [ ] Phase 4: Agent 3 (리스크 관리) + 역프롬프팅
- [ ] Phase 5: 자기학습 루프
- [ ] Phase 6: Developer Agent (자율 코드 수정) **← 최종 목표**

## 실행 방법 (Agent 4)

```bash
# .env 설정 (.env.example 참조)
cp .env.example .env  # API 키 입력

# 즉시 실행 (테스트)
python orchestrator.py --now

# 특정 기간 분석
python orchestrator.py --now --days 7

# 스케줄러 모드 (매일 03:00 KST)
python orchestrator.py
```

## 연동 대상

- [AlgoTradingBot](https://github.com/rubayi/AlgoTradingBot) — MT5 실행 레이어
