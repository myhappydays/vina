from dotenv import load_dotenv
import os
import discord
import anthropic
import json
import datetime
import time
import asyncio
import re
import subprocess
import importlib.util
import sys

# ─────────────── 기본 설정 ────────────────
load_dotenv()

claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)

JSONL_LOG_PATH = "vina_memory/logs/vina_history.jsonl"
EXPLICIT_RULES_PATH = "vina_memory/explicit_rules.json"
CONTEXTUAL_RULES_PATH = "vina_memory/contextual_rules.md"
FACTS_PATH = "vina_memory/facts.md"

# 마지막 메시지 시간 추적 (단순화)
last_message_time = None

# ───── 시스템 프롬프트 불러오기 ─────
def load_prompt(path):
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # 시스템 프롬프트에 사용자 정보 활용에 대한 지침 추가
    if path.endswith("system_prompt_response.txt"):
        content += """

중요한 추가 지침:
1. 사용자 정보는 자연스러운 대화의 맥락에서 필요할 때만 활용하세요.
2. 대화 주제와 관련이 없는 사용자 정보를 무리하게 언급하지 마세요.
3. 이전 대화 맥락이 있는 경우, 이를 우선적으로 고려하세요.
4. 친근한 대화를 우선하고, 사용자 정보는 어색하지 않게 자연스럽게 활용하세요.
5. 사용자가 특정 답변을 요청했을 때는 그에 직접 응답하는 것을 우선하세요.
"""
    
    return content

response_prompt = load_prompt("vina_config/system_prompt_response.txt")
context_prompt = load_prompt("vina_config/system_prompt_context.txt")

# ───── 로그 저장 함수 (한 줄씩) ─────
def save_conversation_to_jsonl(channel, name, msg, is_ai=False):
    now = datetime.datetime.now().isoformat(timespec="seconds")
    os.makedirs(os.path.dirname(JSONL_LOG_PATH), exist_ok=True)

    role = "assistant" if is_ai else "user"
    data = {
        "role": role,
        "name": name,
        "channel": channel,
        "content": msg,
        "time": now
    }

    with open(JSONL_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False) + "\n")
    
    # 마지막 메시지 시간 업데이트 (단순화)
    global last_message_time
    last_message_time = now
    print(f"🔄 마지막 메시지 시간 업데이트: {now}")

# ───── 최근 대화 불러오기 ─────
def load_recent_messages(channel, user_name=None, limit=5):
    messages = []
    if not os.path.exists(JSONL_LOG_PATH):
        return []

    try:
        # 파일에서 모든 관련 메시지 읽기
        with open(JSONL_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    if msg.get("channel") == channel and msg.get("role") in ["user", "assistant"]:
                        # 사용자 이름이 지정된 경우, 해당 사용자나 AI만 포함
                        if user_name is None or msg["role"] == "assistant" or msg.get("name") == user_name:
                            messages.append(msg)
                except json.JSONDecodeError:
                    continue
        
        # 디버깅 정보
        print(f"📄 채널 '{channel}'에서 {len(messages)}개 메시지 로드됨")
        
        # 가장 최근 메시지 limit개를 반환
        return messages[-limit:] if messages else []
    except Exception as e:
        print(f"❌ 메시지 로드 중 오류: {e}")
        import traceback
        traceback.print_exc()
        return []

# ───── Claude용 포맷 압축 ─────
def format_history_for_prompt(messages):
    lines = []
    for m in messages:
        speaker = m["name"] if m["role"] == "user" else "VINA"
        lines.append(f"- {speaker}: {m['content']}")
    return "\n".join(lines)

# ───── 명시적 규칙 로딩 ─────
def load_explicit_rules():
    try:
        print(f"📝 명시적 규칙 파일 로딩: {EXPLICIT_RULES_PATH}")
        
        if not os.path.exists(EXPLICIT_RULES_PATH):
            print(f"❌ 규칙 파일이 존재하지 않음: {EXPLICIT_RULES_PATH}")
            return []
            
        with open(EXPLICIT_RULES_PATH, "r", encoding="utf-8") as f:
            rules = json.load(f)
            print(f"✅ 규칙 파일 로딩 성공: {len(rules)}개 규칙 로드됨")
            
            # 규칙 요약 출력
            for idx, rule in enumerate(rules):
                rule_id = rule.get("id", "알 수 없음")
                active = "활성" if rule.get("active", False) else "비활성"
                conditions = ", ".join(rule.get("condition_tags", []))
                print(f"  [{idx+1}] {rule_id} ({active}): {conditions}")
                
            return rules
    except json.JSONDecodeError as e:
        print(f"❌ 규칙 파일 JSON 파싱 오류: {e}")
        print(f"📄 파일 내용 확인:")
        try:
            with open(EXPLICIT_RULES_PATH, "r", encoding="utf-8") as f:
                content = f.read()
                print(content[:200] + "..." if len(content) > 200 else content)
        except Exception:
            pass
        return []
    except Exception as e:
        print(f"❌ 규칙 로딩 오류: {e}")
        import traceback
        traceback.print_exc()
        return []

# ───── 마크다운 파일 로딩 ─────
def load_markdown_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"마크다운 로딩 오류: {path} - {e}")
        return ""

# ───── 명시적 규칙 형식 설명 ─────
def get_explicit_rule_format_guide():
    """명시적 규칙의 형식 설명 가이드 반환"""
    return f"""📋 **명시적 규칙 형식 가이드**

명시적 규칙은 다음 JSON 형식으로 작성해야 합니다:

```json
{{
  "id": "규칙_ID",         // 고유 식별자 (영문 권장)
  "name": "규칙 이름",      // 사람이 읽기 쉬운 이름
  "condition_tags": [     // 조건 태그 (아래 형식만 허용)
    "time==08:00",
    "weekday==1-5"
  ],
  "condition_description": "조건 설명",  // 조건에 대한 설명
  "action_description": "행동 설명",    // 실행할 행동 설명
  "active": true         // 활성화 여부 (true/false)
}}
```

**유효한 조건 태그 형식:**
1. `time==HH:MM` - 특정 시간에 실행 (예: 08:00)
   - 24시간 형식으로 표기 (00:00 ~ 23:59)
   - 예: `time==07:30`, `time==22:00`

2. `last_message_elapsed>N` - 마지막 메시지 이후 N초가 경과했을 때
   - N은 초 단위의 정수값
   - 예: `last_message_elapsed>1200` (20분)
   - 예: `last_message_elapsed>3600` (1시간)

3. `weekday==N-M` - 특정 요일 범위에 실행
   - N, M은 1(월요일)~7(일요일) 사이의 정수
   - 예: `weekday==1-5` (평일)
   - 예: `weekday==6-7` (주말)

**중요 유의사항:**
- 규칙 ID는 고유해야 합니다. 기존 ID를 사용하면 해당 규칙이 대체됩니다.
- 조건 태그는 위 형식만 허용되며, 다른 형식은 자동으로 제거됩니다.
- 유효한 조건 태그가 없는 경우 기본값(`time==08:00`)이 사용됩니다.
- 필수 필드가 누락된 경우 기본값이 자동으로 추가됩니다.

**사용 예시:**
```
!메모리 추가 명시적 {{
  "id": "lunch_reminder",
  "name": "점심 알림",
  "condition_tags": ["time==12:00", "weekday==1-5"],
  "condition_description": "평일 점심시간에 실행",
  "action_description": "점심 식사 시간을 알려줍니다",
  "active": true
}}
```

**기존 규칙 수정:**
규칙을 수정하려면 같은 ID를 사용하여 새 규칙을 추가합니다:
```
!메모리 수정 명시적 {{
  "id": "lunch_reminder",
  "name": "점심 알림",
  "condition_tags": ["time==12:30"],
  "condition_description": "매일 12시 30분에 실행",
  "action_description": "점심 식사 시간을 알려줍니다",
  "active": true
}}
```
"""

# ───── 규칙 조건 평가 ─────
def evaluate_rule_condition(condition_tag):
    global last_message_time
    now = datetime.datetime.now()
    
    # 디버깅: 조건 태그 출력
    print(f"🔍 조건 평가: {condition_tag}")
    
    # 시간 조건 (time==HH:MM)
    time_match = re.match(r"time==(\d{2}):(\d{2})", condition_tag)
    if time_match:
        hour, minute = map(int, time_match.groups())
        current_hour, current_minute = now.hour, now.minute
        result = current_hour == hour and current_minute == minute
        print(f"  ⏰ 시간 조건: 현재={current_hour}:{current_minute}, 목표={hour}:{minute}, 결과={result}")
        return result
    
    # 마지막 메시지로부터 경과 시간 (last_message_elapsed>초)
    elapsed_match = re.match(r"last_message_elapsed>(\d+)", condition_tag)
    if elapsed_match:
        seconds = int(elapsed_match.group(1))
        print(f"  ⏱️ 경과 시간 조건: 목표 경과 시간 > {seconds}초")
        
        if not last_message_time:
            print(f"  ⚠️ 마지막 메시지 시간이 없습니다.")
            return False
        
        try:
            last_dt = datetime.datetime.fromisoformat(last_message_time)
            elapsed = (now - last_dt).total_seconds()
            elapsed_mins = elapsed / 60
            elapsed_hours = elapsed_mins / 60
            
            result = elapsed > seconds
            print(f"  ⏱️ 마지막 메시지 시간: {last_message_time}")
            print(f"     ├─ 현재 시간: {now.isoformat()}")
            print(f"     ├─ 경과: {elapsed:.1f}초 ({elapsed_mins:.1f}분, {elapsed_hours:.2f}시간)")
            print(f"     ├─ 목표: {seconds}초")
            print(f"     └─ 결과: {'✅ 충족' if result else '❌ 불충족'}")
            
            return result
        except Exception as e:
            print(f"  ❌ 경과 시간 계산 오류: {e}")
            return False
    
    # 요일 조건 (weekday==1-5) : 1(월요일)~7(일요일)
    weekday_match = re.match(r"weekday==(\d)-(\d)", condition_tag)
    if weekday_match:
        start_day, end_day = map(int, weekday_match.groups())
        current_weekday = now.isoweekday()  # 1(월요일)~7(일요일)
        result = start_day <= current_weekday <= end_day
        print(f"  📅 요일 조건: 현재={current_weekday}, 범위={start_day}-{end_day}, 결과={result}")
        return result
    
    print(f"  ❗ 알 수 없는 조건 태그: {condition_tag}")
    return False

# ───── 규칙 조건 확인 ─────
def check_rule_conditions():
    global last_message_time
    rules = load_explicit_rules()
    triggered_rules = []
    
    print(f"\n📋 규칙 점검 시작: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📊 규칙 수: {len(rules)}")
    if last_message_time:
        print(f"⏱️ 마지막 메시지 시간: {last_message_time}")
    else:
        print(f"⚠️ 마지막 메시지 시간이 없습니다.")
    
    for rule in rules:
        rule_id = rule.get("id", "알 수 없음")
        rule_active = rule.get("active", False)
        print(f"\n🔖 규칙 '{rule_id}' 검사 (활성화: {rule_active})")
        
        if not rule_active:
            print(f"  ⏭️ 규칙 '{rule_id}'는 비활성화 상태")
            continue
        
        all_conditions_met = True
        
        for condition in rule.get("condition_tags", []):
            print(f"  🔎 조건 '{condition}' 검사 중")
            result = evaluate_rule_condition(condition)
            
            if not result:
                all_conditions_met = False
                print(f"  🚫 규칙 '{rule_id}'의 조건 중 하나라도 불충족")
                break
            else:
                print(f"  ✅ 조건 충족")
        
        if all_conditions_met:
            print(f"  🎯 규칙 '{rule_id}' 트리거됨!")
            triggered_rules.append((rule, None))
        else:
            print(f"  ⛔ 규칙 '{rule_id}' 트리거되지 않음")
    
    print(f"\n📑 점검 완료: {len(triggered_rules)}개 규칙 트리거됨\n")
    return triggered_rules

# ───── 규칙 기반 자동 메시지 생성 ─────
async def process_triggered_rules():
    print(f"\n⚡ 규칙 트리거 처리 시작: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    triggered_rules = check_rule_conditions()
    
    for rule, _ in triggered_rules:
        try:
            # 메인 채팅 채널 찾기 (현재 고정된 채널)
            channel_id = 1355113753427054806  # 메인 채팅 채널 ID
            channel_obj = discord_client.get_channel(channel_id)
            
            if channel_obj:
                print(f"📣 채널 '{channel_obj.name}' (ID: {channel_id})에 규칙 '{rule.get('id')}' 적용")
                await auto_llm_response(rule, channel_obj)
            else:
                print(f"❌ 채널 ID {channel_id}를 찾을 수 없음")
        except Exception as e:
            print(f"❌ 규칙 '{rule.get('id')}' 처리 중 오류 발생: {e}")
            import traceback
            traceback.print_exc()
    
    print(f"⚡ 규칙 트리거 처리 완료\n")

# ───── 규칙 트리거 프롬프트 생성 ─────
def create_rule_trigger_prompt(rule, channel):
    global last_message_time
    contextual_rules = load_markdown_file(CONTEXTUAL_RULES_PATH)
    facts = load_markdown_file(FACTS_PATH)
    
    # 채널 ID 가져오기 (규칙 트리거에는 고정된 채널 사용)
    channel_id = "1355113753427054806"  # 메인 채널 ID
    
    # 최근 대화 불러오기 (user_name 지정하지 않고 모든 메시지 로드)
    recent_messages = load_recent_messages(channel_id, limit=5)
    formatted_history = format_history_for_prompt(recent_messages)
    
    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    weekday_names = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
    weekday = weekday_names[now.weekday()]
    
    # 시간대 설정
    time_of_day = ""
    if 5 <= now.hour < 12:
        time_of_day = "아침"
    elif 12 <= now.hour < 18:
        time_of_day = "오후"
    elif 18 <= now.hour < 22:
        time_of_day = "저녁"
    else:
        time_of_day = "밤"
    
    # 마지막 메시지 경과 시간 계산
    last_elapsed = "없음"
    if last_message_time:
        last_dt = datetime.datetime.fromisoformat(last_message_time)
        elapsed_seconds = (now - last_dt).total_seconds()
        elapsed_hours = elapsed_seconds / 3600
        last_elapsed = f"{elapsed_hours:.1f}시간"

    condition = rule.get("condition_description", "")
    action = rule.get("action_description", "")
    
    return f"""
# 1. 사용자 기억 정보
{facts}

# 2. VINA의 행동 규칙
{contextual_rules}

# 3. 상황 맥락
- 현재 시각: {current_time_str} ({weekday}, {time_of_day})
- 마지막 대화 이후 경과: {last_elapsed}

# 4. 이전 대화
{formatted_history}

# 5. 현재 트리거된 규칙
- 트리거 조건: {condition}
- 수행할 행동: {action}

# 응답 가이드
1. 위의 "수행할 행동" 지침에 따라 자연스럽게 대화를 시작하세요.
2. 이것은 자동으로 트리거된 응답임을 사용자가 인지하지 못하도록 자연스럽게 시작하세요.
3. 상황 맥락(시간대, 경과 시간)을 자연스럽게 활용하세요.
4. 이전 대화가 있다면 그 맥락을 고려하여 일관성을 유지하세요.
5. 사용자 정보는 직접 언급하지 말고, 필요한 경우에만 자연스럽게 참고하세요.
6. 특별한 이유 없이 사용자의 취미나 관심사를 무리하게 언급하지 마세요.
7. 대화가 어색하지 않게 자연스럽고 친근한 말투로 말하세요.
8. 메시지를 보내지 않아야 하는 상황(늦은 시간, 대화 필요 없음 등)에는 "/None"만 응답하세요.
"""

# ───── 일반 채팅 프롬프트 생성 ─────
def create_chat_prompt(channel, user_name, user_msg, recent_messages):
    global last_message_time
    contextual_rules = load_markdown_file(CONTEXTUAL_RULES_PATH)
    facts = load_markdown_file(FACTS_PATH)
    
    now = datetime.datetime.now()
    current_time_str = now.strftime("%Y-%m-%d %H:%M:%S")
    weekday_names = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]
    weekday = weekday_names[now.weekday()]
    
    # 시간대 설정
    time_of_day = ""
    if 5 <= now.hour < 12:
        time_of_day = "아침"
    elif 12 <= now.hour < 18:
        time_of_day = "오후"
    elif 18 <= now.hour < 22:
        time_of_day = "저녁"
    else:
        time_of_day = "밤"
    
    # 마지막 메시지 경과 시간 계산
    last_elapsed = "없음"
    if last_message_time:
        last_dt = datetime.datetime.fromisoformat(last_message_time)
        elapsed_seconds = (now - last_dt).total_seconds()
        elapsed_minutes = elapsed_seconds / 60
        
        if elapsed_minutes < 60:
            last_elapsed = f"{elapsed_minutes:.1f}분"
        else:
            elapsed_hours = elapsed_minutes / 60
            last_elapsed = f"{elapsed_hours:.1f}시간"

    # 최근 대화 포맷
    formatted_history = format_history_for_prompt(recent_messages)
    
    prompt_text = f"""
# 1. 사용자 기억 정보
{facts}

# 2. VINA의 행동 규칙
{contextual_rules}

# 3. 상황 맥락
- 현재 시각: {current_time_str} ({weekday}, {time_of_day})
- 마지막 대화 이후 경과: {last_elapsed}

# 4. 이전 대화
{formatted_history}

# 5. 현재 요청
{user_msg}

# 응답 가이드
1. 위 "현재 요청"에 직접적으로 응답하세요.
2. 이전 대화의 맥락을 고려하되, 새로운 정보나 의견을 제공하세요.
3. 사용자 정보는 직접 언급하지 말고, 필요한 경우에만 자연스럽게 참고하세요.
4. 특별한 이유 없이 사용자의 취미나 관심사를 무리하게 언급하지 마세요.
5. 시스템 설정이나 내부 작동 원리에 대해 언급하지 마세요.
6. 장황한 설명보다는 핵심에 집중한 간결한 답변을 제공하세요.
7. 친근하고 자연스러운 말투로 대화하세요.
"""

    return {
        "최근 대화": formatted_history,
        "현재 입력": user_msg,
        "맥락적 규칙": contextual_rules,
        "사용자 정보": facts,
        "상황 정보": f"현재 시간: {current_time_str} ({weekday}, {time_of_day})\n마지막 메시지 경과: {last_elapsed}",
        "전체 프롬프트": prompt_text
    }

# ───── 자동 LLM 호출 응답 ─────
async def auto_llm_response(rule, channel_obj):
    # 특수 명령어 처리 확인
    if rule.get("id") == "daily_report_generator" and ("/run_report" in rule.get("action_description", "") or rule.get("action_description") == "/run_report"):
        print(f"\n🔔 [규칙 트리거 - {rule.get('name')}] - 리포트 생성 명령 감지됨\n")
        
        # 사용자에게 리포트 생성 시작 메시지 전송
        await channel_obj.send("📊 오늘의 대화를 기반으로 일일 리포트를 생성하고 있어요. 잠시만 기다려주세요...")
        
        try:
            # 어제 날짜 (기본값) 대신 오늘 날짜로 리포트 생성 
            today = datetime.datetime.now().strftime("%Y-%m-%d")
            
            # 직접 vinareport.py 실행
            print(f"📋 리포트 생성 프로그램 실행 중...")
            
            # 별도 프로세스로 실행
            try:
                # 외부 스크립트로 실행 - 배치 파일 사용
                with open("run_report.bat", "w") as f:
                    f.write(f'@echo off\n')
                    f.write(f'set "PYTHONIOENCODING=utf-8"\n')  
                    f.write(f'python vinareport.py --force --date {today}\n')
                
                os.system("start run_report.bat")
                
                # 성공 메시지
                await channel_obj.send(f"✅ 일일 리포트 생성이 시작되었습니다. 완료되면 'vina-리포트' 채널에서 확인할 수 있습니다.")
                
            except Exception as e:
                print(f"❌ 리포트 실행 오류: {e}")
                import traceback
                traceback.print_exc()
                await channel_obj.send(f"❌ 리포트 생성 중 오류가 발생했습니다: {str(e)}")
                
            # 대화 기록에 저장
            save_conversation_to_jsonl(channel_obj.name, "VINA", "일일 리포트 생성 명령 실행", is_ai=True)
            return
        except Exception as e:
            print(f"❌ 리포트 생성 명령 처리 중 오류 발생: {e}")
            import traceback
            traceback.print_exc()
            await channel_obj.send(f"❌ 리포트 생성 중 오류가 발생했습니다: {str(e)}")
            return
    
    # 일반 LLM 호출 처리
    prompt = create_rule_trigger_prompt(rule, channel_obj.name)
    
    print(f"\n🔔 [규칙 트리거 - {rule.get('name')}]\n")
    print(f"[SYSTEM 프롬프트]\n{prompt[:200]}...\n")
    
    response = claude_client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=500,
        temperature=1,
        system=response_prompt,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt}
                ]
            }
        ]
    )
    
    full_answer = response.content[0].text.strip()
    print(f"[# {channel_obj.name}] 🤖 VINA → {full_answer}")
    
    # 로그에는 저장하지만 '/None'인 경우 메시지 전송하지 않음
    save_conversation_to_jsonl(channel_obj.name, "VINA", full_answer, is_ai=True)
    
    # '/None' 응답 확인
    if full_answer == "/None" or full_answer.startswith("/None "):
        print(f"🚫 '/None' 응답 감지: 메시지를 보내지 않습니다.")
        return
    
    await channel_obj.send(full_answer)

# ───── 중요정보 감지 및 메모리 업데이트 ─────
def analyze_message_for_memory(user_name, user_msg, channel_id):
    """사용자 메시지에서 중요한 정보를 감지하고 적절한 메모리 파일에 저장"""
    print(f"\n🔍 메시지 분석 시작: '{user_msg[:30]}...'")
    
    # 유효한 조건 태그 패턴 (정규식) 정의
    valid_condition_patterns = [
        r"time==\d{2}:\d{2}",          # 시간 일치 (예: time==08:00)
        r"last_message_elapsed>\d+",    # 메시지 경과 시간 (예: last_message_elapsed>1200)
        r"weekday==\d-\d"              # 요일 범위 (예: weekday==1-5)
    ]
    
    # 명시적 규칙의 유효한 형식 설명
    explicit_rule_format = """
명시적 규칙 형식:
{
  "id": "고유한_규칙_ID", // 규칙을 식별하는 고유 ID 
  "name": "규칙 이름", // 사람이 읽기 쉬운 규칙 이름
  "condition_tags": ["태그1", "태그2"], // 조건 태그 (아래 형식만 유효)
  "condition_description": "조건에 대한 설명",
  "action_description": "수행할 행동 설명",
  "active": true // 활성화 여부 (true/false)
}

유효한 조건 태그 형식:
1. "time==HH:MM" - 특정 시간에 실행 (예: "time==08:00")
2. "last_message_elapsed>N" - 마지막 메시지 후 N초 경과 (예: "last_message_elapsed>1200")
3. "weekday==N-M" - 특정 요일 범위에 실행 (예: "weekday==1-5", 1=월요일, 7=일요일)

다른 형식의 조건 태그는 허용되지 않습니다.
"""
    
    # 특별 명령어: 규칙 삭제/변경 관련 지침
    delete_rule_instruction = """
규칙 삭제 요청 인식 지침:
- 사용자가 특정 규칙 삭제를 요청하는 경우, "rules_to_delete" 배열에 해당 규칙의 ID를 추가하세요.
- 삭제 요청 예시: "morning_greeting 규칙 삭제해줘", "아침 인사 규칙은 필요 없어"
- 규칙 ID를 명확히 언급하지 않았으나 규칙 이름이나 특성으로 식별 가능한 경우, 가장 관련성 높은 ID를 추가하세요.
- 특정 규칙을 새로운 규칙으로 변경하는 요청은, 해당 규칙 ID를 "rules_to_delete"에 추가하고 새 규칙을 "explicit_rules"에 추가하세요.

예시: 
1. "아침 인사 규칙 삭제해줘" → rules_to_delete: ["morning_greeting"]
2. "아침 알림을 저녁 9시로 변경해줘" → rules_to_delete: ["morning_greeting"], explicit_rules: [새로운 저녁 알림 규칙]
"""
    
    # 현재 등록된 규칙 정보 로드
    current_rules = load_explicit_rules()
    current_rules_info = "\n현재 등록된 규칙 목록:\n"
    for rule in current_rules:
        rule_id = rule.get("id", "알 수 없음")
        rule_name = rule.get("name", "이름 없음")
        rule_desc = rule.get("condition_description", "설명 없음")
        current_rules_info += f"- ID: {rule_id}, 이름: {rule_name}, 설명: {rule_desc}\n"
    
    # 분석을 위한 Claude 호출
    prompt = f"""
분석 지침: 다음 사용자 메시지에서 기억할 가치가 있는 정보를 식별하여 분류해주세요.

사용자 메시지:
"{user_msg}"

다음 카테고리로 분류하세요:
1. 사용자 사실 정보 (facts.md에 저장): 사용자의 취향, 선호도, 개인 정보, 일상 루틴, 계획 등
2. 맥락적 규칙 (contextual_rules.md에 저장): 사용자의 요청사항, 대화 스타일, 특정 상황 대응 방법 등
3. 명시적 규칙 (explicit_rules.json에 저장): 특정 시간이나 조건에서 실행할 자동 응답 규칙
4. 규칙 삭제 요청: 사용자가 특정 규칙 삭제를 요청하는 경우

분석 방법:
- 각 카테고리에 해당하는 정보를 추출하세요.
- 기존 정보와 중복되거나 모순되는 내용은 표시하세요.
- 정보가 없는 카테고리는 "없음"으로 표시하세요.
- 확실하지 않은 정보는 포함하지 마세요.
- 각 추출 항목에 대해 0~100 사이의 자신감 점수(confidence)를 부여하세요.

명시적 규칙 (explicit_rules.json) 생성/수정 시 중요 사항:
{explicit_rule_format}

- 기존 규칙을 수정하려면 규칙의 ID와 동일한 ID를 사용해야 합니다.
- 완전히 새로운 규칙을 만들 때는 고유한 ID를 생성해야 합니다.
- 직접 명시된 규칙 ID가 있으면 그 ID를 유지하고, 아니면 명확한 영어 ID를 생성하세요.
- 조건 태그는 위에 명시된 형식만 허용됩니다.

규칙 삭제 관련 지침:
{delete_rule_instruction}
{current_rules_info}

다음 JSON 형식으로 응답하세요:
{{
  "facts": [
    {{ "content": "항목1", "confidence": 85 }},
    {{ "content": "항목2", "confidence": 70 }}
  ],
  "contextual_rules": [
    {{ "content": "항목1", "confidence": 80 }},
    {{ "content": "항목2", "confidence": 60 }}
  ],
  "explicit_rules": [
    {{
      "id": "규칙_ID",
      "name": "규칙 이름",
      "condition_tags": ["time==HH:MM", "weekday==N-M"],
      "condition_description": "조건 설명",
      "action_description": "수행할 행동 설명", 
      "active": true,
      "confidence": 75
    }}
  ],
  "rules_to_delete": [
    {{ "id": "삭제할_규칙_ID", "confidence": 90 }}
  ],
  "analysis": "분석 결과 요약",
  "has_valuable_info": true  // 메시지에 가치 있는 정보가 있는지 여부
}}
"""

    try:
        response = claude_client.messages.create(
            model="claude-3-5-haiku-20241022",
            max_tokens=500,
            temperature=0.2,
            system="당신은 텍스트에서 중요한 정보를 식별하고 분류하는 전문가입니다. 사용자가 제공한 텍스트에서 사실 정보, 선호도, 규칙 등을 식별하여 JSON 형식으로 반환하세요. 특히 명시적 규칙을 작성할 때는 지정된 형식만 사용하세요.",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt}
                    ]
                }
            ]
        )
        
        analysis_text = response.content[0].text.strip()
        
        # JSON 부분 추출
        json_match = re.search(r'```json\n(.*?)\n```|({.*})', analysis_text, re.DOTALL)
        if json_match:
            json_str = json_match.group(1) or json_match.group(2)
            analysis_data = json.loads(json_str)
        else:
            try:
                analysis_data = json.loads(analysis_text)
            except json.JSONDecodeError:
                print(f"❌ JSON 형식을 찾을 수 없음: {analysis_text[:100]}...")
                return None
        
        print(f"✅ 분석 완료: {analysis_data.get('analysis', '요약 없음')}")
        print(f"💡 가치 있는 정보 여부: {analysis_data.get('has_valuable_info', False)}")
        
        # 명시적 규칙 조건 태그 검증
        explicit_rules = analysis_data.get("explicit_rules", [])
        for rule in explicit_rules:
            if isinstance(rule, dict) and "condition_tags" in rule:
                valid_tags = []
                invalid_tags = []
                
                # 태그 검증
                for tag in rule.get("condition_tags", []):
                    if any(re.match(pattern, tag) for pattern in valid_condition_patterns):
                        valid_tags.append(tag)
                    else:
                        invalid_tags.append(tag)
                        print(f"⚠️ 유효하지 않은 조건 태그 제거: '{tag}'")
                
                # 유효한 태그만 유지
                rule["condition_tags"] = valid_tags
                
                # 유효한 태그가 없으면 기본 태그 추가
                if not valid_tags:
                    rule["condition_tags"] = ["time==08:00"]
                    print(f"⚠️ 규칙 '{rule.get('id', '알 수 없음')}'에 유효한 조건이 없어 기본값 추가")
        
        # 삭제할 규칙 처리
        rules_to_delete = analysis_data.get("rules_to_delete", [])
        if rules_to_delete:
            delete_ids = []
            for rule_info in rules_to_delete:
                if isinstance(rule_info, dict):
                    rule_id = rule_info.get("id")
                    confidence = rule_info.get("confidence", 0)
                    if rule_id and confidence >= 70:  # 신뢰도 70% 이상인 경우만 처리
                        delete_ids.append(rule_id)
                        print(f"🗑️ 규칙 삭제 요청 감지: '{rule_id}' (신뢰도: {confidence}%)")
                elif isinstance(rule_info, str):
                    delete_ids.append(rule_info)
                    print(f"🗑️ 규칙 삭제 요청 감지: '{rule_info}'")
            
            if delete_ids:
                analysis_data["rules_to_delete"] = delete_ids
        
        # 메모리 파일 업데이트 (confidence threshold 적용)
        if analysis_data.get('has_valuable_info', False):
            update_memory_files(analysis_data)
        else:
            print("ℹ️ 메모리 업데이트 대상 정보 없음")
        
        return analysis_data
    except Exception as e:
        print(f"❌ 메시지 분석 중 오류 발생: {e}")
        import traceback
        traceback.print_exc()
        return None

def update_memory_files(analysis_data):
    """분석 결과에 따라 메모리 파일들을 업데이트"""
    # confidence threshold 설정 (이 이상의 자신감 점수를 가진 항목만 업데이트)
    CONFIDENCE_THRESHOLD = 70
    
    # 각 카테고리의 항목들 확인
    facts_data = analysis_data.get("facts", [])
    contextual_rules_data = analysis_data.get("contextual_rules", [])
    explicit_rules_data = analysis_data.get("explicit_rules", [])
    rules_to_delete = analysis_data.get("rules_to_delete", [])
    
    # 신뢰도 높은 항목만 필터링
    filtered_facts = []
    for fact in facts_data:
        if isinstance(fact, dict) and fact.get('confidence', 0) >= CONFIDENCE_THRESHOLD:
            filtered_facts.append(fact.get('content'))
        elif isinstance(fact, str):  # 이전 형식 지원
            filtered_facts.append(fact)
    
    filtered_rules = []
    for rule in contextual_rules_data:
        if isinstance(rule, dict) and rule.get('confidence', 0) >= CONFIDENCE_THRESHOLD:
            filtered_rules.append(rule.get('content'))
        elif isinstance(rule, str):  # 이전 형식 지원
            filtered_rules.append(rule)
    
    filtered_explicit_rules = []
    for rule in explicit_rules_data:
        if isinstance(rule, dict) and rule.get('confidence', 0) >= CONFIDENCE_THRESHOLD:
            # confidence 필드는 저장하지 않음
            rule_copy = rule.copy()
            if 'confidence' in rule_copy:
                del rule_copy['confidence']
            filtered_explicit_rules.append(rule_copy)
    
    # 삭제할 규칙 필터링
    filtered_delete_rules = []
    for rule_info in rules_to_delete:
        if isinstance(rule_info, dict):
            rule_id = rule_info.get("id")
            confidence = rule_info.get("confidence", 0)
            if rule_id and confidence >= CONFIDENCE_THRESHOLD:
                filtered_delete_rules.append(rule_id)
        elif isinstance(rule_info, str):
            filtered_delete_rules.append(rule_info)
    
    updates_made = 0
    
    # facts.md 업데이트
    if filtered_facts:
        print(f"📝 {len(filtered_facts)}개 사용자 정보 업데이트 중... (신뢰도 {CONFIDENCE_THRESHOLD}% 이상)")
        updates_made += update_facts_file(filtered_facts)
    
    # contextual_rules.md 업데이트
    if filtered_rules:
        print(f"📝 {len(filtered_rules)}개 맥락적 규칙 업데이트 중... (신뢰도 {CONFIDENCE_THRESHOLD}% 이상)")
        updates_made += update_contextual_rules_file(filtered_rules)
    
    # 규칙 삭제 처리
    if filtered_delete_rules:
        print(f"🗑️ {len(filtered_delete_rules)}개 명시적 규칙 삭제 중... (신뢰도 {CONFIDENCE_THRESHOLD}% 이상)")
        updates_made += delete_explicit_rules(filtered_delete_rules)
    
    # explicit_rules.json 업데이트 (삭제 후 추가)
    if filtered_explicit_rules:
        print(f"📝 {len(filtered_explicit_rules)}개 명시적 규칙 업데이트 중... (신뢰도 {CONFIDENCE_THRESHOLD}% 이상)")
        updates_made += update_explicit_rules_file(filtered_explicit_rules)
    
    print(f"✅ 메모리 업데이트 완료: {updates_made}개 파일 변경됨")
    return updates_made

def update_facts_file(new_facts):
    """facts.md 파일 업데이트"""
    try:
        facts_content = load_markdown_file(FACTS_PATH)
        if not facts_content:
            # 파일이 없거나 비어있는 경우 새로 생성
            facts_content = "# 사용자 관련 정보\n\n"
        
        # 항목들 분류 (기존 섹션 식별)
        sections = {}
        current_section = "기타"
        
        for line in facts_content.splitlines():
            if line.startswith("## "):
                current_section = line[3:].strip()
                sections[current_section] = []
            elif line.startswith("- "):
                sections.setdefault(current_section, []).append(line)
        
        # 새 항목 추가 여부 결정 (기본은 "기타" 섹션에 추가)
        for fact in new_facts:
            # 이미 존재하는 항목인지 확인
            found = False
            fact_key = fact.split(":")[0].strip() if ":" in fact else fact
            
            for section, items in sections.items():
                for i, item in enumerate(items):
                    if fact_key in item:
                        # 항목 업데이트
                        sections[section][i] = f"- {fact}"
                        found = True
                        break
                if found:
                    break
            
            if not found:
                # 새 항목 추가 (기타 섹션에)
                sections.setdefault("기타", []).append(f"- {fact}")
        
        # 새로운 파일 내용 구성
        new_content = "# 사용자 관련 정보\n\n"
        
        for section, items in sections.items():
            if items:  # 항목이 있는 섹션만 포함
                new_content += f"## {section}\n"
                new_content += "\n".join(items) + "\n\n"
        
        # 파일 저장
        os.makedirs(os.path.dirname(FACTS_PATH), exist_ok=True)
        with open(FACTS_PATH, "w", encoding="utf-8") as f:
            f.write(new_content)
        
        return 1  # 업데이트 성공
    except Exception as e:
        print(f"❌ facts.md 업데이트 중 오류: {e}")
        import traceback
        traceback.print_exc()
        return 0  # 업데이트 실패

def update_contextual_rules_file(new_rules):
    """contextual_rules.md 파일 업데이트"""
    try:
        rules_content = load_markdown_file(CONTEXTUAL_RULES_PATH)
        if not rules_content:
            # 파일이 없거나 비어있는 경우 새로 생성
            rules_content = "# 비나의 맥락적 규칙\n\n"
        
        # 섹션 분류
        sections = {}
        current_section = "기타 규칙"
        
        for line in rules_content.splitlines():
            if line.startswith("## "):
                current_section = line[3:].strip()
                sections[current_section] = []
            elif line.startswith("- "):
                sections.setdefault(current_section, []).append(line)
        
        # 새 규칙 추가 (기본은 "기타 규칙" 섹션에 추가)
        for rule in new_rules:
            # 이미 존재하는 규칙인지 확인
            found = False
            rule_keywords = rule.lower().split()[:3]  # 첫 몇 단어로 유사성 확인
            
            for section, items in sections.items():
                for i, item in enumerate(items):
                    # 키워드 매칭으로 유사 규칙 확인
                    if all(keyword in item.lower() for keyword in rule_keywords):
                        # 규칙 업데이트
                        sections[section][i] = f"- {rule}"
                        found = True
                        break
                if found:
                    break
            
            if not found:
                # 새 규칙 추가 
                # 규칙 내용에 따라 적절한 섹션 선택
                target_section = "기타 규칙"
                if "금지" in rule.lower() or "하지 않" in rule.lower():
                    target_section = "금지 사항"
                elif "감정" in rule.lower() or "슬픔" in rule.lower():
                    target_section = "감정 대응"
                elif "상황" in rule.lower() or "경우" in rule.lower():
                    target_section = "상황별 대응 규칙"
                
                sections.setdefault(target_section, []).append(f"- {rule}")
        
        # 새로운 파일 내용 구성
        new_content = "# 비나의 맥락적 규칙\n\n"
        
        for section, items in sections.items():
            if items:  # 항목이 있는 섹션만 포함
                new_content += f"## {section}\n"
                new_content += "\n".join(items) + "\n\n"
        
        # 파일 저장
        os.makedirs(os.path.dirname(CONTEXTUAL_RULES_PATH), exist_ok=True)
        with open(CONTEXTUAL_RULES_PATH, "w", encoding="utf-8") as f:
            f.write(new_content)
        
        return 1  # 업데이트 성공
    except Exception as e:
        print(f"❌ contextual_rules.md 업데이트 중 오류: {e}")
        import traceback
        traceback.print_exc()
        return 0  # 업데이트 실패

def update_explicit_rules_file(new_rules):
    """explicit_rules.json 파일 업데이트"""
    try:
        # 기존 규칙 로드
        current_rules = load_explicit_rules()
        
        # 유효한 조건 태그 패턴 (정규식)
        valid_condition_patterns = [
            r"time==\d{2}:\d{2}",          # 시간 일치 (예: time==08:00)
            r"last_message_elapsed>\d+",    # 메시지 경과 시간 (예: last_message_elapsed>1200)
            r"weekday==\d-\d"              # 요일 범위 (예: weekday==1-5)
        ]
        
        for new_rule in new_rules:
            # ID로 동일 규칙 확인
            rule_id = new_rule.get("id")
            if not rule_id:
                print(f"⚠️ 규칙 ID가 없는 규칙 무시: {new_rule}")
                continue
            
            # 조건 태그 검증
            invalid_tags = []
            if "condition_tags" in new_rule:
                valid_tags = []
                for tag in new_rule["condition_tags"]:
                    # 태그가 유효한 패턴인지 확인
                    if any(re.match(pattern, tag) for pattern in valid_condition_patterns):
                        valid_tags.append(tag)
                    else:
                        invalid_tags.append(tag)
                        print(f"⚠️ 유효하지 않은 조건 태그 무시: '{tag}'")
                
                # 유효한 태그만 저장
                new_rule["condition_tags"] = valid_tags
            
            # 유효하지 않은 태그가 있다면 경고
            if invalid_tags:
                print(f"❌ 규칙 '{rule_id}'에 {len(invalid_tags)}개의 유효하지 않은 태그가 있습니다. 형식은 다음 중 하나여야 합니다:")
                print(f"  - time==HH:MM (예: time==08:00)")
                print(f"  - last_message_elapsed>N (예: last_message_elapsed>1200)")
                print(f"  - weekday==N-N (예: weekday==1-5)")
            
            # 전체 규칙 목록에서 기존 규칙을 제거 (완전 대체 방식)
            new_current_rules = [rule for rule in current_rules if rule.get("id") != rule_id]
            
            # 새 규칙이 유효한 태그를 가지고 있는지 확인
            if "condition_tags" not in new_rule or not new_rule["condition_tags"]:
                print(f"⚠️ 규칙 '{rule_id}'에 유효한 조건이 없어 기본값 추가")
                new_rule["condition_tags"] = ["time==08:00"]  # 기본 조건 추가
            
            # 필수 필드 확인 및 추가
            required_fields = ["id", "name", "condition_tags", "condition_description", "action_description", "active"]
            for field in required_fields:
                if field not in new_rule:
                    if field == "active":
                        new_rule["active"] = True  # 기본값
                    elif field == "name":
                        new_rule["name"] = f"규칙 {rule_id}"  # 기본값
                    elif field == "condition_description":
                        tags_str = ", ".join(new_rule.get("condition_tags", ["없음"]))
                        new_rule["condition_description"] = f"조건: {tags_str}"
                    elif field == "action_description":
                        new_rule["action_description"] = "자동 생성된 행동 설명"
            
            # 규칙 추가
            new_current_rules.append(new_rule)
            
            # 기존 규칙이 있었는지 출력
            if len(new_current_rules) < len(current_rules):
                print(f"✅ 기존 규칙 '{rule_id}' 대체 완료")
            else:
                print(f"✅ 새 규칙 '{rule_id}' 추가")
            
            # 현재 규칙 목록 업데이트
            current_rules = new_current_rules
        
        # 파일 저장
        os.makedirs(os.path.dirname(EXPLICIT_RULES_PATH), exist_ok=True)
        with open(EXPLICIT_RULES_PATH, "w", encoding="utf-8") as f:
            json.dump(current_rules, f, ensure_ascii=False, indent=2)
        
        return 1  # 업데이트 성공
    except Exception as e:
        print(f"❌ explicit_rules.json 업데이트 중 오류: {e}")
        import traceback
        traceback.print_exc()
        return 0  # 업데이트 실패

# 규칙 삭제 처리 함수 추가
def delete_explicit_rules(rule_ids):
    """명시적 규칙 ID 목록을 받아 해당 규칙들 삭제"""
    if not rule_ids:
        return 0
    
    try:
        # 기존 규칙 로드
        current_rules = load_explicit_rules()
        
        # 각 규칙 ID 처리
        deleted_count = 0
        new_rules = []
        
        for rule in current_rules:
            rule_id = rule.get("id")
            if rule_id in rule_ids:
                print(f"🗑️ 규칙 '{rule_id}' 삭제")
                deleted_count += 1
            else:
                new_rules.append(rule)
        
        # 변경된 경우에만 파일 업데이트
        if deleted_count > 0:
            os.makedirs(os.path.dirname(EXPLICIT_RULES_PATH), exist_ok=True)
            with open(EXPLICIT_RULES_PATH, "w", encoding="utf-8") as f:
                json.dump(new_rules, f, ensure_ascii=False, indent=2)
            
            print(f"✅ {deleted_count}개 규칙 삭제 완료")
            return 1  # 성공적으로 업데이트됨
        else:
            print("ℹ️ 삭제할 규칙이 없음")
            return 0  # 변경 없음
    except Exception as e:
        print(f"❌ 규칙 삭제 중 오류: {e}")
        import traceback
        traceback.print_exc()
        return 0

# ───── 메시지 처리 메인 함수 ─────
async def message_response(input_message):
    author = input_message.author.name
    channel = input_message.channel.name
    channel_id = str(input_message.channel.id)  # 채널 ID를 문자열로 저장
    user_msg = input_message.content

    GREEN = "\033[92m"
    BLUE = "\033[94m"
    RESET = "\033[0m"

    print(f"\n[# {channel} (ID: {channel_id})] {BLUE}{author}{RESET} → {user_msg}")
    save_conversation_to_jsonl(channel_id, author, user_msg, is_ai=False)  # 채널 ID로 저장

    # 메시지에서 중요 정보 분석 및 메모리 업데이트
    memory_analysis = analyze_message_for_memory(author, user_msg, channel_id)
    
    # 최근 대화 불러오기 (개선된 함수 사용)
    recent_messages = load_recent_messages(channel_id, author, limit=5)
    
    # 프롬프트 생성
    prompt_data = create_chat_prompt(channel_id, author, user_msg, recent_messages)
    
    # 응답 생성
    print("\n💬 [Claude 응답 생성 요청]\n")
    print(f"[최근 대화 메시지 수]: {len(recent_messages)}개")
    print(f"[현재 입력]\n{user_msg}")

    response = claude_client.messages.create(
        model="claude-3-5-haiku-20241022",
        max_tokens=500,
        temperature=1,
        system=response_prompt,
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_data['전체 프롬프트']}
                ]
            }
        ]
    )

    full_answer = response.content[0].text.strip()
    print(f"[# {channel}] {GREEN}VINA{RESET} → {full_answer}")
    
    # 로그에는 저장하지만 '/None'인 경우 메시지 전송하지 않음
    save_conversation_to_jsonl(channel_id, "VINA", full_answer, is_ai=True)
    
    # '/None' 응답 확인
    if full_answer == "/None" or full_answer.startswith("/None "):
        print(f"🚫 '/None' 응답 감지: 메시지를 보내지 않습니다.")
        return
    
    await input_message.channel.send(full_answer)

# ───── 규칙 체크 주기적 실행 ─────
async def periodic_rule_check():
    print(f"\n🔄 주기적 규칙 체크 작업 시작됨: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    check_count = 0
    
    while True:
        try:
            check_count += 1
            print(f"\n🔄 규칙 체크 #{check_count} - {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            await process_triggered_rules()
        except Exception as e:
            print(f"❌ 규칙 처리 오류: {e}")
            import traceback
            traceback.print_exc()
        
        next_check = datetime.datetime.now() + datetime.timedelta(minutes=1)
        print(f"⏰ 다음 규칙 체크: {next_check.strftime('%Y-%m-%d %H:%M:%S')}")
        await asyncio.sleep(60)  # 1분마다 체크

# ───── 시작 시 메시지 기록 로드 ─────
def load_initial_message_time():
    print(f"\n📂 메시지 기록 파일에서 마지막 메시지 시간 로드 중...")
    if not os.path.exists(JSONL_LOG_PATH):
        print(f"❌ 메시지 기록 파일이 존재하지 않습니다: {JSONL_LOG_PATH}")
        return None

    last_time = None
    try:
        with open(JSONL_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    msg = json.loads(line)
                    time_str = msg.get("time")
                    if time_str and (last_time is None or time_str > last_time):
                        last_time = time_str
                except json.JSONDecodeError:
                    continue
        
        if last_time:
            print(f"✅ 마지막 메시지 시간 로드 완료: {last_time}")
            try:
                last_dt = datetime.datetime.fromisoformat(last_time)
                now = datetime.datetime.now()
                elapsed = (now - last_dt).total_seconds()
                print(f"  - 경과 시간: {elapsed:.1f}초 ({elapsed/60:.1f}분)")
            except Exception as e:
                print(f"  - 처리 오류: {e}")
        else:
            print(f"⚠️ 로드된 메시지가 없습니다.")
        
        return last_time
    except Exception as e:
        print(f"❌ 메시지 기록 로드 중 오류 발생: {e}")
        import traceback
        traceback.print_exc()
        return None

# ───── 디스코드 봇 이벤트 설정 ─────
@discord_client.event
async def on_ready():
    print(f"\n✅ 디스코드 봇 로그인 완료: {discord_client.user}")
    print(f"🕒 현재 시간: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"📂 설정 파일 경로: \n - 규칙: {EXPLICIT_RULES_PATH}\n - 맥락: {CONTEXTUAL_RULES_PATH}\n - 정보: {FACTS_PATH}")
    
    # 메시지 기록에서 마지막 메시지 시간 로드
    global last_message_time
    last_message_time = load_initial_message_time()
    
    # 시작 시 한 번 규칙 체크
    print("\n🚀 최초 규칙 체크 실행...")
    await process_triggered_rules()
    
    # 주기적 규칙 체크 시작
    print("\n⏱️ 주기적 규칙 체크 작업 시작...")
    asyncio.create_task(periodic_rule_check())

@discord_client.event
async def on_message(message):
    if message.author == discord_client.user:
        return
        
    # 진단 명령 (디버깅용)
    if message.content.startswith("!진단"):
        await diagnose_command(message)
        return
    
    # 메모리 관리 명령
    elif message.content.startswith("!메모리"):
        await memory_command(message)
        return
    
    # 리포트 생성 명령
    elif message.content.startswith("!리포트"):
        await report_command(message)
        return
        
    if message.channel.id == 1355113753427054806:
        await message_response(message)

# ───── 메모리 명령 처리 ─────
async def memory_command(message):
    """메모리 관리 명령어 처리"""
    cmd_parts = message.content.split(maxsplit=2)  # 최대 2개 공백으로 분리
    
    if len(cmd_parts) == 1:
        # 도움말 표시
        help_text = f"""📋 **메모리 관리 명령어**
!메모리 상태 - 현재 저장된 메모리 파일 상태 확인
!메모리 추출 [메시지] - 입력한 메시지에서 중요 정보 추출
!메모리 분석 [메시지ID] - 이전 메시지 ID에서 중요 정보 추출
!메모리 추가 사실 [내용] - facts.md에 새 정보 추가
!메모리 추가 규칙 [내용] - contextual_rules.md에 새 규칙 추가
!메모리 추가 명시적 - explicit_rules.json에 새 규칙 추가 가이드
!메모리 삭제 명시적 [ID] - 명시적 규칙 삭제
!메모리 수정 명시적 [JSON] - 명시적 규칙 수정
!메모리 형식 명시적 - 명시적 규칙 형식 상세 설명
!메모리 규칙 목록 - 현재 등록된 명시적 규칙 목록 보기
!메모리 보기 - 현재 저장된 메모리 파일들의 내용 보기
!메모리 설정 - 메모리 관리 설정 보기 및 변경
!메모리 검증 - 메모리 파일 형식 검증 및 수정

또한 일반 대화 중에 '아침 인사 규칙 삭제해줘', '저녁 알림을 9시로 변경해줘'와 같은 
자연어 요청으로도 규칙을 관리할 수 있습니다."""
        await message.channel.send(help_text)
        return
    
    # 메모리 상태 확인
    if cmd_parts[1] == "상태":
        facts_content = load_markdown_file(FACTS_PATH)
        rules_content = load_markdown_file(CONTEXTUAL_RULES_PATH)
        explicit_rules = load_explicit_rules()
        
        facts_lines = len(facts_content.splitlines()) if facts_content else 0
        rules_lines = len(rules_content.splitlines()) if rules_content else 0
        
        status_text = f"""🧠 **메모리 파일 상태**
📄 facts.md: {facts_lines}줄
📄 contextual_rules.md: {rules_lines}줄
📄 explicit_rules.json: {len(explicit_rules)}개 규칙"""
        await message.channel.send(status_text)
        return
    
    # 새 메시지에서 메모리 추출
    elif cmd_parts[1] == "추출" and len(cmd_parts) >= 3:
        user_msg = cmd_parts[2]
        await message.channel.send(f"🔍 메시지 분석 중...")
        
        analysis_data = analyze_message_for_memory(message.author.name, user_msg, str(message.channel.id))
        if analysis_data:
            facts = analysis_data.get("facts", [])
            ctx_rules = analysis_data.get("contextual_rules", [])
            expl_rules = analysis_data.get("explicit_rules", [])
            
            reply = f"✅ **메시지 분석 결과**\n"
            
            if facts and len(facts) > 0:
                reply += f"**사용자 정보({len(facts)}개):**\n"
                for i, fact in enumerate(facts[:5]):  # 최대 5개까지만 표시
                    content = fact.get('content') if isinstance(fact, dict) else fact
                    confidence = f" ({fact.get('confidence')}%)" if isinstance(fact, dict) else ""
                    reply += f"- {content}{confidence}\n"
                if len(facts) > 5:
                    reply += f"- ... 외 {len(facts)-5}개\n"
            else:
                reply += "**사용자 정보:** 없음\n"
                
            if ctx_rules and len(ctx_rules) > 0:
                reply += f"\n**맥락적 규칙({len(ctx_rules)}개):**\n"
                for i, rule in enumerate(ctx_rules[:3]):  # 최대 3개까지만 표시
                    content = rule.get('content') if isinstance(rule, dict) else rule
                    confidence = f" ({rule.get('confidence')}%)" if isinstance(rule, dict) else ""
                    reply += f"- {content}{confidence}\n"
                if len(ctx_rules) > 3:
                    reply += f"- ... 외 {len(ctx_rules)-3}개\n"
            else:
                reply += "\n**맥락적 규칙:** 없음\n"
                
            if expl_rules and len(expl_rules) > 0:
                reply += f"\n**명시적 규칙({len(expl_rules)}개):**\n"
                for i, rule in enumerate(expl_rules[:2]):  # 최대 2개까지만 표시
                    rule_id = rule.get("id", "알 수 없음")
                    rule_name = rule.get("name", "이름 없음") 
                    confidence = f" ({rule.get('confidence')}%)" if 'confidence' in rule else ""
                    reply += f"- {rule_id} ({rule_name}){confidence}\n"
                if len(expl_rules) > 2:
                    reply += f"- ... 외 {len(expl_rules)-2}개\n"
            else:
                reply += "\n**명시적 규칙:** 없음\n"
                
            await message.channel.send(reply)
        else:
            await message.channel.send("❌ 메시지 분석 중 오류가 발생했습니다.")
        return
    
    # 메모리 설정 보기/변경
    elif cmd_parts[1] == "설정":
        # 메모리 관리 설정 표시 (향후 확장 가능)
        CONFIDENCE_THRESHOLD = 70  # 현재 하드코딩된 값
        settings_text = f"""⚙️ **메모리 관리 설정**
- 자동 업데이트: 활성화
- 신뢰도 기준값: {CONFIDENCE_THRESHOLD}% (이 값 이상의 신뢰도를 가진 정보만 자동 저장)
- 자동 분석: 모든 메시지 대상
- 저장 경로: `{os.path.dirname(FACTS_PATH)}`

향후 업데이트에서 위 설정들을 사용자가 변경할 수 있도록 할 예정입니다."""
        await message.channel.send(settings_text)
        return
    
    # 메모리 직접 추가 (사실)
    elif cmd_parts[1] == "추가" and len(cmd_parts) >= 3:
        parts = cmd_parts[2].split(maxsplit=1)
        if len(parts) < 2:
            await message.channel.send("❌ 형식이 잘못되었습니다. `!메모리 추가 [유형] [내용]` 형식으로 입력해주세요.")
            return
            
        mem_type, content = parts
        
        if mem_type == "사실":
            # facts.md에 추가
            update_facts_file([content])
            await message.channel.send(f"✅ 사용자 정보에 추가되었습니다: `{content}`")
            
        elif mem_type == "규칙":
            # contextual_rules.md에 추가
            update_contextual_rules_file([content])
            await message.channel.send(f"✅ 맥락적 규칙에 추가되었습니다: `{content}`")
            
        else:
            await message.channel.send(f"❌ 알 수 없는 메모리 유형: `{mem_type}`. 사용 가능한 유형: `사실`, `규칙`")
        return
    
    # 메모리 파일 내용 보기
    elif cmd_parts[1] == "보기":
        if len(cmd_parts) >= 3:
            mem_type = cmd_parts[2]
            
            if mem_type == "사실":
                content = load_markdown_file(FACTS_PATH)
                await message.channel.send(f"📄 **사용자 정보 (facts.md)**\n```md\n{content[:1900]}```")
                
            elif mem_type == "규칙":
                content = load_markdown_file(CONTEXTUAL_RULES_PATH)
                await message.channel.send(f"📄 **맥락적 규칙 (contextual_rules.md)**\n```md\n{content[:1900]}```")
                
            elif mem_type == "명시적":
                rules = load_explicit_rules()
                content = json.dumps(rules, ensure_ascii=False, indent=2)
                await message.channel.send(f"📄 **명시적 규칙 (explicit_rules.json)**\n```json\n{content[:1900]}```")
                
            else:
                await message.channel.send(f"❌ 알 수 없는 메모리 유형: `{mem_type}`. 사용 가능한 유형: `사실`, `규칙`, `명시적`")
                
        else:
            # 모든 메모리 파일 내용을 요약해서 보여줌
            facts_content = load_markdown_file(FACTS_PATH)
            rules_content = load_markdown_file(CONTEXTUAL_RULES_PATH)
            
            reply = "📄 **메모리 파일 내용 요약**\n\n"
            
            # 사실 정보 (facts.md) 요약
            facts_sections = {}
            current_section = "기타"
            for line in facts_content.splitlines():
                if line.startswith("## "):
                    current_section = line[3:].strip()
                    facts_sections[current_section] = []
                elif line.startswith("- "):
                    facts_sections.setdefault(current_section, []).append(line)
            
            reply += "**사용자 정보 (facts.md)**\n"
            for section, items in facts_sections.items():
                reply += f"- {section}: {len(items)}개 항목\n"
            
            # 맥락적 규칙 (contextual_rules.md) 요약
            rules_sections = {}
            current_section = "기타"
            for line in rules_content.splitlines():
                if line.startswith("## "):
                    current_section = line[3:].strip()
                    rules_sections[current_section] = []
                elif line.startswith("- "):
                    rules_sections.setdefault(current_section, []).append(line)
            
            reply += "\n**맥락적 규칙 (contextual_rules.md)**\n"
            for section, items in rules_sections.items():
                reply += f"- {section}: {len(items)}개 항목\n"
            
            # 명시적 규칙 (explicit_rules.json) 요약
            explicit_rules = load_explicit_rules()
            active_rules = sum(1 for rule in explicit_rules if rule.get("active", False))
            
            reply += f"\n**명시적 규칙 (explicit_rules.json)**\n"
            reply += f"- 총 {len(explicit_rules)}개 규칙 (활성: {active_rules}개, 비활성: {len(explicit_rules) - active_rules}개)\n"
            
            await message.channel.send(reply)
        return
    
    # 명시적 규칙 형식 설명
    elif cmd_parts[1] == "형식" and len(cmd_parts) >= 3 and cmd_parts[2] == "명시적":
        guide_text = get_explicit_rule_format_guide()
        await message.channel.send(guide_text)
        return
    
    # 명시적 규칙 추가 가이드
    elif cmd_parts[1] == "추가" and len(cmd_parts) >= 3 and cmd_parts[2] == "명시적":
        # 간단한 가이드 표시
        guide_text = f"""📝 **명시적 규칙 추가 방법**

규칙은 다음 형식의 JSON 데이터로 정의합니다:
```json
{{
  "id": "규칙_아이디",
  "name": "규칙 이름",
  "condition_tags": ["time==HH:MM", "last_message_elapsed>N"],
  "condition_description": "조건에 대한 설명",
  "action_description": "수행할 행동 설명",
  "active": true
}}
```

**유효한 조건 태그:**
- `time==HH:MM` - 특정 시간에 실행 (예: `time==08:00`)
- `last_message_elapsed>N` - 마지막 메시지 후 N초 경과 (예: `last_message_elapsed>1200`)
- `weekday==N-M` - 특정 요일 범위에 실행 (예: `weekday==1-5`)

자세한 설명은 `!메모리 형식 명시적` 명령어로 볼 수 있습니다.

**사용 예시:**
`!메모리 추가 명시적 {{
  "id": "morning_coffee",
  "name": "아침 커피 알림",
  "condition_tags": ["time==07:30", "weekday==1-5"],
  "condition_description": "평일 아침 7시 30분에 실행",
  "action_description": "아침 커피 마실 시간임을 알림",
  "active": true
}}`
"""
        await message.channel.send(guide_text)
        
        # 규칙 직접 추가 (가이드 다음에 JSON이 있는 경우)
        if len(cmd_parts) >= 4:
            try:
                json_str = cmd_parts[3]
                new_rule = json.loads(json_str)
                
                # 규칙 추가
                update_explicit_rules_file([new_rule])
                await message.channel.send(f"✅ 명시적 규칙 '{new_rule.get('id', '알 수 없음')}'이(가) 추가되었습니다.")
            except json.JSONDecodeError:
                await message.channel.send("❌ JSON 형식이 잘못되었습니다. 위 가이드를 참고하여 올바른 형식으로 작성해주세요.")
            except Exception as e:
                await message.channel.send(f"❌ 규칙 추가 중 오류가 발생했습니다: {str(e)}")
        
        return
    
    # 명시적 규칙 삭제
    elif cmd_parts[1] == "삭제" and len(cmd_parts) >= 3 and "명시적" in cmd_parts[2]:
        # "명시적 [ID]" 형식 처리
        parts = cmd_parts[2].split(maxsplit=1)
        rule_id = parts[1] if len(parts) > 1 else ""
        
        if not rule_id:
            await message.channel.send("❌ 삭제할 규칙의 ID를 입력해주세요. 예: `!메모리 삭제 명시적 morning_greeting`")
            return
            
        # 실제 삭제 처리
        result = delete_explicit_rules([rule_id])
        
        if result > 0:
            await message.channel.send(f"✅ 규칙 '{rule_id}'이(가) 삭제되었습니다.")
        else:
            await message.channel.send(f"❌ 규칙 '{rule_id}'을(를) 찾을 수 없거나 삭제할 수 없습니다.")
        
        return

    # 규칙 목록 보기
    elif cmd_parts[1] == "규칙" and len(cmd_parts) >= 3 and cmd_parts[2] == "목록":
        rules = load_explicit_rules()
        
        if not rules:
            await message.channel.send("ℹ️ 현재 등록된 명시적 규칙이 없습니다.")
            return
        
        reply = "📋 **등록된 명시적 규칙 목록**\n\n"
        
        for i, rule in enumerate(rules, 1):
            rule_id = rule.get("id", "알 수 없음")
            rule_name = rule.get("name", "이름 없음")
            rule_active = "✅ 활성" if rule.get("active", False) else "❌ 비활성"
            rule_tags = ", ".join(rule.get("condition_tags", ["없음"]))
            rule_desc = rule.get("condition_description", "설명 없음")
            
            reply += f"**{i}. {rule_name}** (`{rule_id}`)\n"
            reply += f"  - 상태: {rule_active}\n"
            reply += f"  - 조건: `{rule_tags}`\n"
            reply += f"  - 설명: {rule_desc}\n\n"
        
        reply += "규칙을 삭제하려면 `!메모리 삭제 명시적 [ID]` 명령을 사용하세요."
        await message.channel.send(reply)
        return
    
    # 명시적 규칙 수정
    elif cmd_parts[1] == "수정" and len(cmd_parts) >= 3 and cmd_parts[2].startswith("명시적 "):
        parts = cmd_parts[2].split(maxsplit=1)
        
        if len(parts) < 2:
            await message.channel.send("❌ 수정할 규칙의 JSON 데이터를 입력해주세요.")
            return
            
        try:
            json_str = parts[1]
            new_rule = json.loads(json_str)
            
            # 규칙 업데이트
            if "id" not in new_rule:
                await message.channel.send("❌ 규칙에 ID가 없습니다. 수정할 규칙의 ID를 반드시 포함해주세요.")
                return
                
            update_explicit_rules_file([new_rule])
            await message.channel.send(f"✅ 명시적 규칙 '{new_rule.get('id')}'이(가) 수정되었습니다.")
        except json.JSONDecodeError:
            await message.channel.send("❌ JSON 형식이 잘못되었습니다. 올바른 형식으로 작성해주세요.")
        except Exception as e:
            await message.channel.send(f"❌ 규칙 수정 중 오류가 발생했습니다: {str(e)}")
        
        return
    
    # 메모리 파일 검증 및 수정
    elif cmd_parts[1] == "검증":
        await message.channel.send("🔍 메모리 파일 검증 및 수정 중...")
        
        try:
            # explicit_rules.json 검증
            explicit_rules = load_explicit_rules()
            
            # 유효한 조건 태그 패턴
            valid_condition_patterns = [
                r"time==\d{2}:\d{2}",          # 시간 일치
                r"last_message_elapsed>\d+",    # 메시지 경과 시간
                r"weekday==\d-\d"              # 요일 범위
            ]
            
            # 오류 있는 규칙 식별
            invalid_rules = []
            for rule in explicit_rules:
                rule_id = rule.get("id", "알 수 없음")
                
                # 필수 필드 확인
                if not all(key in rule for key in ["id", "name", "condition_tags", "action_description"]):
                    invalid_rules.append((rule_id, "필수 필드 누락"))
                    continue
                
                # 조건 태그 검증
                invalid_tags = []
                for tag in rule.get("condition_tags", []):
                    if not any(re.match(pattern, tag) for pattern in valid_condition_patterns):
                        invalid_tags.append(tag)
                
                if invalid_tags:
                    invalid_rules.append((rule_id, f"유효하지 않은 조건 태그: {', '.join(invalid_tags)}"))
            
            # 결과 보고
            if invalid_rules:
                reply = f"⚠️ **{len(invalid_rules)}개의 문제 있는 규칙이 발견되었습니다**\n\n"
                
                for rule_id, issue in invalid_rules:
                    reply += f"- 규칙 `{rule_id}`: {issue}\n"
                
                reply += "\n자동 수정을 원하시면 `!메모리 검증 수정`을 입력하세요."
                await message.channel.send(reply)
            else:
                await message.channel.send("✅ 모든 규칙이 유효합니다.")
            
            # 자동 수정 요청 확인
            if len(cmd_parts) >= 3 and cmd_parts[2] == "수정":
                if invalid_rules:
                    # 규칙 수정
                    fixed_rules = []
                    for rule in explicit_rules:
                        rule_id = rule.get("id", "알 수 없음")
                        
                        # 필수 필드 추가
                        if "id" not in rule:
                            rule["id"] = f"rule_{int(time.time())}"
                        if "name" not in rule:
                            rule["name"] = f"자동 생성 규칙 {rule.get('id')}"
                        if "condition_tags" not in rule or not rule["condition_tags"]:
                            rule["condition_tags"] = ["time==08:00"]  # 기본값
                        if "condition_description" not in rule:
                            rule["condition_description"] = "자동 생성된 조건 설명"
                        if "action_description" not in rule:
                            rule["action_description"] = "자동 생성된 행동 설명"
                        if "active" not in rule:
                            rule["active"] = False  # 안전을 위해 기본값은 비활성
                        
                        # 조건 태그 수정
                        valid_tags = []
                        for tag in rule.get("condition_tags", []):
                            if any(re.match(pattern, tag) for pattern in valid_condition_patterns):
                                valid_tags.append(tag)
                        
                        # 유효한 태그가 없으면 기본값 추가
                        if not valid_tags:
                            valid_tags = ["time==08:00"]  # 기본값
                        
                        rule["condition_tags"] = valid_tags
                        fixed_rules.append(rule)
                    
                    # 파일 저장
                    with open(EXPLICIT_RULES_PATH, "w", encoding="utf-8") as f:
                        json.dump(fixed_rules, f, ensure_ascii=False, indent=2)
                    
                    await message.channel.send(f"✅ {len(invalid_rules)}개의 규칙이 수정되었습니다.")
                else:
                    await message.channel.send("ℹ️ 수정할 규칙이 없습니다.")
        except Exception as e:
            await message.channel.send(f"❌ 파일 검증 중 오류가 발생했습니다: {str(e)}")
        
        return

    else:
        await message.channel.send("❓ 알 수 없는 메모리 명령어입니다. `!메모리`를 입력하면 도움말을 볼 수 있습니다.")

# ───── 진단 명령 ─────
async def diagnose_command(message):
    global last_message_time
    cmd_parts = message.content.split()
    
    if len(cmd_parts) == 1:
        # 기본 진단
        now = datetime.datetime.now()
        reply = f"🔍 **시스템 진단 보고서**\n"
        reply += f"⏰ 현재 시간: {now.strftime('%Y-%m-%d %H:%M:%S')}\n"
        
        if last_message_time:
            try:
                last_dt = datetime.datetime.fromisoformat(last_message_time)
                elapsed = (now - last_dt).total_seconds()
                reply += f"📌 마지막 메시지:\n"
                reply += f"  - 시간: {last_message_time}\n"
                reply += f"  - 경과: {elapsed:.1f}초 ({elapsed/60:.1f}분)\n"
            except Exception as e:
                reply += f"❌ 마지막 메시지 시간 처리 오류: {e}\n"
        else:
            reply += f"⚠️ 마지막 메시지 기록이 없습니다.\n"
        
        await message.channel.send(reply)
        
    elif cmd_parts[1] == "규칙":
        # 규칙 진단
        rules = load_explicit_rules()
        reply = f"📜 **규칙 진단 보고서**\n"
        reply += f"📊 총 규칙 수: {len(rules)}개\n\n"
        
        for rule in rules:
            rule_id = rule.get("id", "알 수 없음")
            active = "✅ 활성" if rule.get("active", False) else "❌ 비활성"
            conditions = ", ".join(rule.get("condition_tags", []))
            
            reply += f"📌 규칙 `{rule_id}`\n"
            reply += f"  - 상태: {active}\n"
            reply += f"  - 조건: {conditions}\n"
            
            # 개별 조건 평가
            reply += f"  - 조건 평가:\n"
            all_true = True
            
            for condition in rule.get("condition_tags", []):
                result = evaluate_rule_condition(condition)
                cond_result = f"{'✅ 충족' if result else '❌ 불충족'}"
                if not result:
                    all_true = False
                reply += f"    - `{condition}`: {cond_result}\n"
            
            if rule.get("active", False):
                final_status = "✅ 트리거 가능" if all_true else "❌ 트리거 불가"
            else:
                final_status = "❌ 비활성화 상태"
                
            reply += f"  - 최종 상태: {final_status}\n\n"
            
        await message.channel.send(reply)
    
    elif cmd_parts[1] == "강제실행" and len(cmd_parts) >= 3:
        # 특정 규칙 강제 실행
        rule_id = cmd_parts[2]
        found = False
        
        rules = load_explicit_rules()
        for rule in rules:
            if rule.get("id") == rule_id:
                found = True
                await message.channel.send(f"⚠️ 규칙 `{rule_id}` 강제 실행 중...")
                await auto_llm_response(rule, message.channel)
                break
                
        if not found:
            await message.channel.send(f"❌ 규칙 `{rule_id}`를 찾을 수 없습니다.")
    
    elif cmd_parts[1] == "메시지추가":
        # 현재 채널에 메시지 기록 추가 (테스트용)
        last_message_time = datetime.datetime.now().isoformat(timespec="seconds")
        await message.channel.send(f"✅ 마지막 메시지 시간 업데이트: {last_message_time}")
        
    elif cmd_parts[1] == "시뮬레이션" and len(cmd_parts) >= 3:
        # 특정 조건 시뮬레이션
        test_condition = " ".join(cmd_parts[2:])
        reply = f"🧪 **조건 시뮬레이션**: `{test_condition}`\n\n"
        
        result = evaluate_rule_condition(test_condition)
        reply += f"{'✅ 조건 충족!' if result else '❌ 조건 불충족'}\n"
            
        await message.channel.send(reply)
        
    else:
        help_text = f"""🔍 **진단 명령어 도움말**
!진단 - 기본 시스템 상태 보기
!진단 규칙 - 모든 규칙의 상태와 조건 평가
!진단 강제실행 [규칙ID] - 특정 규칙 강제 실행 (예: !진단 강제실행 long_absence)
!진단 메시지추가 - 마지막 메시지 시간 업데이트 (테스트용)
!진단 시뮬레이션 [조건] - 특정 조건 시뮬레이션 (예: !진단 시뮬레이션 last_message_elapsed>60)
"""
        await message.channel.send(help_text)

# ───── 리포트 명령 처리 ─────
async def report_command(message):
    """리포트 생성 명령어 처리"""
    cmd_parts = message.content.split(maxsplit=1)
    date_str = None
    
    # 날짜 인자 확인
    if len(cmd_parts) > 1:
        date_arg = cmd_parts[1].strip()
        # YYYY-MM-DD 형식 검증
        if re.match(r'^\d{4}-\d{2}-\d{2}$', date_arg):
            date_str = date_arg
    
    # 날짜 지정이 없으면 오늘 날짜 사용
    if not date_str:
        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    
    # 진행 상황 메시지 전송
    status_msg = await message.channel.send(f"📊 {date_str} 날짜의 대화를 기반으로 일일 리포트를 생성하고 있어요. 잠시만 기다려주세요...")
    
    try:
        # 직접 vinareport.py 실행
        print(f"📋 리포트 생성 프로그램 실행 중... 날짜: {date_str}")
        
        # 별도 프로세스로 실행
        try:
            # 외부 스크립트로 실행 - 배치 파일 사용
            batch_filename = f"run_report_{date_str.replace('-', '')}.bat"
            with open(batch_filename, "w") as f:
                f.write(f'@echo off\n')
                f.write(f'set "PYTHONIOENCODING=utf-8"\n')  
                f.write(f'python vinareport.py --force --date {date_str}\n')
            
            os.system(f"start {batch_filename}")
            
            # 성공 메시지
            await status_msg.edit(content=f"✅ {date_str} 일일 리포트 생성이 시작되었습니다. 완료되면 'vina-리포트' 채널에서 확인할 수 있습니다.")
            
        except Exception as e:
            print(f"❌ 리포트 실행 오류: {e}")
            import traceback
            traceback.print_exc()
            await status_msg.edit(content=f"❌ 리포트 생성 중 오류가 발생했습니다: {str(e)}")
        
    except Exception as e:
        print(f"❌ 리포트 생성 명령 처리 중 오류 발생: {e}")
        import traceback
        traceback.print_exc()
        await status_msg.edit(content=f"❌ 리포트 생성 중 오류가 발생했습니다: {str(e)}")

# ───── 실행 ─────
discord_client.run(DISCORD_TOKEN)
