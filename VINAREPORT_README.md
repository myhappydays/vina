# VINA 일간 리포트 생성기

VINA의 대화 기록을 기반으로 일간 리포트를 생성하는 자동화 파이프라인입니다.

## 주요 기능

- VINA 대화 기록(jsonl) 파일에서 특정 날짜의 대화 추출
- 불필요한 메시지 필터링 및 정제
- LlamaIndex 문서 변환
- Claude 3.5 Haiku를 활용한 회고적 리포트 생성
- 마크다운 형식 및 JSON 통계 저장
- 디스코드 채널에 리포트 전송

## 설치 방법

1. 필요한 패키지 설치:

```bash
pip install -r requirements.txt
```

2. 환경 변수 설정:

`.env` 파일을 생성하고 다음 환경 변수를 설정하세요:

```
# API Keys
ANTHROPIC_API_KEY=your_anthropic_api_key_here
DISCORD_BOT_TOKEN=your_discord_bot_token_here

# Webhook URLs
DISCORD_REPORT_WEBHOOK_URL=your_discord_webhook_url_here
```

## 사용 방법

### 기본 사용법

다음 명령어로 어제 날짜의 리포트를 생성합니다:

```bash
python vinareport.py
```

### 특정 날짜 리포트 생성

```bash
python vinareport.py --date 2025-04-24
```

### Discord 전송 비활성화

```bash
python vinareport.py --no-discord
```

### 기존 리포트 강제 재생성

```bash
python vinareport.py --force
```

### 모든 옵션 확인

```bash
python vinareport.py --help
```

### 디스코드 봇으로 실행

디스코드 봇 모드로 실행하여 'vina-리포트' 채널에서 명령을 받아 처리할 수 있습니다:

```bash
python run_vina_report_bot.py
```

또는:

```bash
python vinareport.py --discord-bot
```

#### 디스코드 명령어

봇이 실행 중일 때 'vina-리포트' 채널에서 다음 명령어를 사용할 수 있습니다:

- `!help` - 도움말 표시
- `!report [YYYY-MM-DD]` - 지정한 날짜의 리포트 생성 및 전송 (날짜 생략 시 어제 날짜 사용)

## 파이프라인 프로세스

1. **대화 로드**: 지정된 날짜의 JSONL 형식 대화 기록 로드
2. **데이터 정제**: 시스템 메시지, 빈 메시지, 중복 메시지 제거
3. **문서 변환**: LlamaIndex Document 형식으로 변환
4. **프롬프트 생성**: Claude에게 전달할 리포트 작성 프롬프트 생성
5. **리포트 생성**: Claude 3.5 Haiku로 리포트 텍스트 생성
6. **통계 추출**: 키워드, 대화 시간 등 통계 정보 추출
7. **파일 저장**: 마크다운 리포트와 JSON 통계 파일 저장
8. **Discord 전송**: 디스코드 웹훅을 통해 리포트 전송

## 리포트 형식

```markdown
# 2025년 4월 24일 리포트

오전에는 실용영어 중간고사를 앞두고 긴장한 모습이 드러났다.  
점심 무렵에는 마라탕을 추천받으며 잠시 기분 전환을 시도했지만, 시험 후 피로가 느껴졌다.  
대화 전체의 흐름은 차분했으며, 강한 감정 기복은 없었던 하루였다.

---

**🧠 핵심 키워드**: 실용영어, 중간고사, 마라탕, 점메추  
**💬 메시지 수**: 35개  
**🕒 총 대화 시간**: 약 3시간  
**🌟 오늘의 문장**: "점메추 해줘!"

---
```

## 테스트 실행

테스트 스크립트로 기능 검증:

```bash
python test_vinareport.py
```

## 디렉토리 구조

```
.
├── vinareport.py         # 메인 스크립트
├── test_vinareport.py    # 테스트 스크립트
├── requirements.txt      # 필요 패키지 목록
├── vina_memory/          # VINA 메모리 디렉토리
│   └── logs/             # 대화 로그 디렉토리
│       └── vina_history.jsonl  # 대화 기록 파일
└── vina_reports/         # 생성된 리포트 저장 디렉토리
    ├── 2025-04-24/       # 날짜별 디렉토리
    │   ├── report.md     # 마크다운 리포트
    │   └── stats.json    # 통계 정보
    └── README.md         # 리포트 디렉토리 설명
``` 