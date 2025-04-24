"""
VINA의 대화 기록을 기반으로 일간 리포트를 생성하는 자동화 파이프라인

⚙️ 파이프라인 구성
1. jsonl 대화 파일 읽기
2. 날짜 필터링 (예: 2025-04-24)
3. 불용 메시지 제거 + 정제
4. 문서 변환
5. Claude Haiku 프롬프트 생성
6. LLM 응답 수신 (일기 리포트)
7. Markdown 저장 + 메타 저장
8. 리포트 내용 디스코드 'vina-리포트'채널에 형식 맞춰 전송
"""

# 표준 출력 인코딩 설정 (CP949 인코딩 오류 방지)
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import json
import os
import re
import datetime
import argparse
from typing import List, Dict, Any, Tuple
import anthropic
from dotenv import load_dotenv
import discord
from discord import Webhook
import aiohttp
import asyncio

# 환경 변수 로드
load_dotenv()

# 상수 정의
JSONL_LOG_PATH = "vina_memory/logs/vina_history.jsonl"
REPORTS_DIR = "vina_reports"
DISCORD_WEBHOOK_URL = os.getenv("DISCORD_REPORT_WEBHOOK_URL")

# Claude API 클라이언트 설정
claude_client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

# 디스코드 클라이언트 설정
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
intents = discord.Intents.default()
intents.message_content = True
discord_client = discord.Client(intents=intents)

# LlamaIndex 대체를 위한 단순 Document 클래스
class Document:
    """LlamaIndex Document를 대체하는 간단한 문서 클래스"""
    def __init__(self, text: str, metadata: Dict[str, Any] = None):
        self.text = text
        self.metadata = metadata or {}

def parse_arguments():
    """커맨드 라인 인자 파싱"""
    parser = argparse.ArgumentParser(description="VINA 일간 리포트 생성기")
    parser.add_argument("--date", type=str, help="처리할 날짜 (YYYY-MM-DD 형식)")
    parser.add_argument("--no-discord", action="store_true", help="디스코드 전송 기능 비활성화")
    parser.add_argument("--force", action="store_true", help="기존 리포트가 있어도 강제로 재생성")
    
    # 도움말 직접 출력하기 위한 코드 (문제 해결용)
    if len(sys.argv) > 1 and sys.argv[1] == '--help':
        parser.print_help()
        sys.exit(0)
        
    return parser.parse_args()

def load_conversation_data(date_str: str) -> List[Dict[str, Any]]:
    """
    지정된 날짜의 대화 데이터를 로드하고 필터링합니다.
    
    Args:
        date_str: YYYY-MM-DD 형식의 날짜 문자열
    
    Returns:
        해당 날짜의 대화 메시지 리스트
    """
    print(f"🔍 {date_str} 날짜의 대화 데이터 로딩 중...")
    
    if not os.path.exists(JSONL_LOG_PATH):
        print(f"❌ 로그 파일이 존재하지 않습니다: {JSONL_LOG_PATH}")
        return []
    
    messages = []
    try:
        with open(JSONL_LOG_PATH, "r", encoding="utf-8") as f:
            for line in f:
                try:
                    message = json.loads(line)
                    # 날짜 비교를 위해 시간 파싱
                    if "time" in message and message["time"]:
                        msg_time = datetime.datetime.fromisoformat(message["time"])
                        msg_date = msg_time.strftime("%Y-%m-%d")
                        
                        # 지정된 날짜와 일치하는 메시지만 필터링
                        if msg_date == date_str:
                            messages.append(message)
                except json.JSONDecodeError:
                    continue
                except ValueError:
                    # 잘못된 날짜 형식 처리
                    continue
        
        print(f"✅ {len(messages)}개의 메시지를 로드했습니다.")
        return messages
    except Exception as e:
        print(f"❌ 대화 데이터 로드 중 오류 발생: {e}")
        return []

def clean_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    대화 메시지를 정제합니다:
    - 시스템 메시지 제거
    - 빈 메시지 제거
    - 특수 명령어 제거 ('/None' 등)
    - 중복 메시지 정리
    
    Args:
        messages: 원본 메시지 리스트
    
    Returns:
        정제된 메시지 리스트
    """
    print("🧹 불필요한 메시지 정제 중...")
    
    filtered_messages = []
    seen_contents = set()
    
    for msg in messages:
        # 필수 필드가 없는 메시지 건너뛰기
        if not all(key in msg for key in ["role", "content"]):
            continue
        
        # 특수 명령어나 짧은 메시지 건너뛰기
        content = msg.get("content", "").strip()
        if not content or content == "/None" or len(content) < 3:
            continue
            
        # 시스템 메시지 건너뛰기
        if msg.get("role") not in ["user", "assistant"]:
            continue
        
        # 이미 본 내용 중복 제거 (정확히 같은 내용)
        if content in seen_contents:
            continue
            
        seen_contents.add(content)
        filtered_messages.append(msg)
    
    print(f"✅ {len(filtered_messages)}개의 메시지로 정제되었습니다.")
    return filtered_messages

def convert_to_document(messages: List[Dict[str, Any]]) -> Document:
    """
    정제된 메시지를 Document 형식으로 변환합니다.
    
    Args:
        messages: 정제된 메시지 리스트
    
    Returns:
        Document 객체
    """
    print("📄 메시지를 Document로 변환 중...")
    
    # 대화 내용을 하나의 문자열로 조합
    conversation_text = ""
    
    for msg in messages:
        speaker = msg.get("name", "Unknown") if msg.get("role") == "user" else "VINA"
        content = msg.get("content", "")
        time_str = ""
        
        if "time" in msg and msg["time"]:
            try:
                msg_time = datetime.datetime.fromisoformat(msg["time"])
                time_str = msg_time.strftime("%H:%M")
            except ValueError:
                time_str = ""
        
        # 형식: [시간] 화자: 내용
        formatted_msg = f"[{time_str}] {speaker}: {content}\n\n"
        conversation_text += formatted_msg
    
    # Document 객체 생성
    metadata = {
        "source": JSONL_LOG_PATH,
        "date": messages[0].get("time", "").split("T")[0] if messages else "",
        "message_count": len(messages)
    }
    
    document = Document(text=conversation_text, metadata=metadata)
    print(f"✅ {len(messages)}개 메시지가 Document로 변환되었습니다.")
    
    return document

def generate_report_prompt(document: Document, date_str: str) -> str:
    """
    Claude에 전달할 리포트 생성 프롬프트를 작성합니다.
    
    Args:
        document: 대화 내용이 담긴 Document
        date_str: 리포트 대상 날짜
    
    Returns:
        완성된 프롬프트 문자열
    """
    # 날짜 형식 변환 (2025-04-24 -> 2025년 4월 24일)
    date_parts = date_str.split('-')
    formatted_date = f"{date_parts[0]}년 {int(date_parts[1])}월 {int(date_parts[2])}일"
    
    # 메시지 개수와 시간 계산
    message_count = document.metadata.get("message_count", 0)
    
    # 프롬프트 템플릿 작성
    prompt = f"""
당신은 VINA(비나)라는 AI 어시스턴트가 사용자와 나눈 하루치 대화를 분석하여 그날의 일기 형태로 요약해주는 전문가입니다.
아래 대화를 분석하여 {formatted_date}에 있었던 일과 느낌을 회고적인 문어체 보고서로 작성해주세요.

보고서는 다음 형식을 따라야 합니다:
# {formatted_date} 리포트

[오전, 오후, 저녁 시간대별 주요 내용 및 감정 상태 2-3문장으로 요약]

---

**🧠 핵심 키워드**: [대화에서 추출한 주요 키워드 4-5개]
**💬 메시지 수**: {message_count}개
**🕒 총 대화 시간**: [첫 메시지와 마지막 메시지 사이의 시간]
**🌟 오늘의 문장**: [가장 인상적이거나 중요했던, 또는 감정이 담긴 한 문장]

---

주의사항:
1. '~했다', '~인 것 같다'와 같은 회고적 톤을 유지하세요.
2. 내용은 객관적이면서도 감정적인 상태를 중심으로 작성하세요.
3. 사용자와 VINA 간의 상호작용을 중심으로 요약하세요.
4. 너무 길지 않게 간결하게 작성하세요.

대화 내용:
{document.text}
"""
    return prompt

def create_report_with_claude(prompt: str) -> str:
    """
    Claude API를 사용하여 리포트를 생성합니다.
    
    Args:
        prompt: 리포트 생성용 프롬프트
    
    Returns:
        생성된 리포트 문자열
    """
    print("🤖 Claude API로 리포트 생성 중...")
    
    try:
        response = claude_client.messages.create(
            model="claude-3-haiku-20240307",
            max_tokens=1500,
            messages=[
                {"role": "user", "content": prompt}
            ],
            temperature=0.2,
        )
        
        report_text = response.content[0].text
        print(f"✅ 리포트가 성공적으로 생성되었습니다. ({len(report_text)} 자)")
        return report_text
    except Exception as e:
        print(f"❌ Claude API 호출 중 오류 발생: {e}")
        return f"리포트 생성 실패: {e}"

def save_report(report_text: str, stats: Dict[str, Any], date_str: str) -> Tuple[str, str]:
    """
    생성된 리포트와 통계 정보를 파일로 저장합니다.
    
    Args:
        report_text: 생성된 리포트 내용
        stats: 통계 정보를 담은 딕셔너리
        date_str: 리포트 대상 날짜
    
    Returns:
        저장된 리포트 파일 경로와 통계 파일 경로의 튜플
    """
    print(f"💾 {date_str} 리포트 저장 중...")
    
    # 날짜별 디렉토리 생성
    report_dir = os.path.join(REPORTS_DIR, date_str)
    os.makedirs(report_dir, exist_ok=True)
    
    # 리포트 파일 경로
    report_path = os.path.join(report_dir, "report.md")
    stats_path = os.path.join(report_dir, "stats.json")
    
    # 리포트 파일 저장
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_text)
    
    # 통계 파일 저장
    with open(stats_path, "w", encoding="utf-8") as f:
        json.dump(stats, f, ensure_ascii=False, indent=2)
    
    print(f"✅ 리포트가 다음 위치에 저장되었습니다: {report_path}")
    return report_path, stats_path

def extract_stats_from_report(report_text: str, messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    리포트 내용에서 통계 정보를 추출합니다.
    
    Args:
        report_text: 생성된 리포트 내용
        messages: 날짜별 필터링된 메시지 리스트
    
    Returns:
        통계 정보가 담긴 딕셔너리
    """
    stats = {}
    
    # 메시지 수
    stats["message_count"] = len(messages)
    
    # 첫 메시지와 마지막 메시지 시간
    time_stamps = []
    for msg in messages:
        if "time" in msg and msg["time"]:
            try:
                msg_time = datetime.datetime.fromisoformat(msg["time"])
                time_stamps.append(msg_time)
            except ValueError:
                continue
    
    if time_stamps:
        first_time = min(time_stamps)
        last_time = max(time_stamps)
        duration = last_time - first_time
        
        stats["first_message_time"] = first_time.strftime("%H:%M:%S")
        stats["last_message_time"] = last_time.strftime("%H:%M:%S")
        stats["duration_minutes"] = int(duration.total_seconds() / 60)
    
    # 정규식으로 키워드 추출
    keywords_match = re.search(r'\*\*🧠 핵심 키워드\*\*:\s*(.*?)(?:\n|$)', report_text)
    if keywords_match:
        keywords_str = keywords_match.group(1).strip()
        stats["keywords"] = [k.strip() for k in keywords_str.split(',')]
    
    # 오늘의 문장 추출
    todays_quote_match = re.search(r'\*\*🌟 오늘의 문장\*\*:\s*"?(.*?)"?(?:\n|$)', report_text)
    if todays_quote_match:
        stats["todays_quote"] = todays_quote_match.group(1).strip()
    
    return stats

async def send_to_discord(report_path: str, date_str: str) -> bool:
    """
    생성된 리포트를 Discord 채널로 전송합니다.
    
    Args:
        report_path: 리포트 파일 경로
        date_str: 리포트 대상 날짜
    
    Returns:
        전송 성공 여부 (bool)
    """
    if not DISCORD_TOKEN:
        print("⚠️ Discord 토큰이 설정되지 않았습니다. Discord 전송을 건너뜁니다.")
        return False
    
    print("📨 Discord로 리포트 전송 중...")
    
    try:
        # 리포트 내용 읽기
        with open(report_path, "r", encoding="utf-8") as f:
            report_content = f.read()
        
        # 날짜 형식 변환 (2025-04-24 -> 2025년 4월 24일)
        date_parts = date_str.split('-')
        formatted_date = f"{date_parts[0]}년 {int(date_parts[1])}월 {int(date_parts[2])}일"
        
        # Discord Embed 생성
        embed = discord.Embed(
            title=f"📝 {formatted_date} 리포트",
            description=report_content,
            color=0x3498db
        )

        # 디스코드 봇을 사용하여 메시지 전송
        await discord_client.wait_until_ready()
        
        # 'vina-리포트' 채널 찾기
        report_channel = None
        for guild in discord_client.guilds:
            for channel in guild.channels:
                if channel.name == 'vina-리포트':
                    report_channel = channel
                    break
            if report_channel:
                break
        
        if report_channel:
            await report_channel.send(embed=embed)
            print(f"✅ '{report_channel.name}' 채널로 리포트 전송 완료!")
            return True
        else:
            print("❌ 'vina-리포트' 채널을 찾을 수 없습니다.")
            return False
    
    except Exception as e:
        print(f"❌ Discord 전송 중 오류 발생: {e}")
        return False

# 웹훅을 사용한 기존 함수 (대체용)
async def send_to_discord_webhook(report_path: str, date_str: str) -> bool:
    """
    웹훅을 사용하여 Discord 채널로 리포트를 전송합니다.
    
    Args:
        report_path: 리포트 파일 경로
        date_str: 리포트 대상 날짜
    
    Returns:
        전송 성공 여부 (bool)
    """
    if not DISCORD_WEBHOOK_URL:
        print("⚠️ Discord 웹훅 URL이 설정되지 않았습니다. Discord 전송을 건너뜁니다.")
        return False
    
    print("📨 Discord 웹훅으로 리포트 전송 중...")
    
    try:
        # 리포트 내용 읽기
        with open(report_path, "r", encoding="utf-8") as f:
            report_content = f.read()
        
        # 날짜 형식 변환 (2025-04-24 -> 2025년 4월 24일)
        date_parts = date_str.split('-')
        formatted_date = f"{date_parts[0]}년 {int(date_parts[1])}월 {int(date_parts[2])}일"
        
        # Discord Embed 생성
        embed = discord.Embed(
            title=f"📝 {formatted_date} 리포트",
            description=report_content,
            color=0x3498db
        )
        
        # 웹훅으로 메시지 전송
        async with aiohttp.ClientSession() as session:
            webhook = Webhook.from_url(DISCORD_WEBHOOK_URL, session=session)
            await webhook.send(embed=embed, username="VINA 리포트 봇")
        
        print("✅ Discord 웹훅으로 리포트 전송 완료!")
        return True
    except Exception as e:
        print(f"❌ Discord 웹훅 전송 중 오류 발생: {e}")
        return False

async def main():
    """메인 실행 함수"""
    # 명령행 인자 파싱
    args = parse_arguments()
    
    # 날짜 설정 (기본값: 오늘)
    if args.date:
        date_str = args.date
    else:
        # 전날 리포트 생성 (기본값)
        yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
        date_str = yesterday.strftime("%Y-%m-%d")
    
    print(f"🗓️ {date_str} 날짜의 리포트 생성을 시작합니다.")
    
    # 기존 리포트 확인
    report_dir = os.path.join(REPORTS_DIR, date_str)
    report_path = os.path.join(report_dir, "report.md")
    
    if os.path.exists(report_path) and not args.force:
        print(f"⚠️ {date_str} 날짜의 리포트가 이미 존재합니다. --force 옵션을 사용하여 재생성할 수 있습니다.")
        sys.exit(0)
    
    # 1. 날짜별 대화 데이터 로드
    messages = load_conversation_data(date_str)
    
    if not messages:
        print(f"⚠️ {date_str} 날짜의 대화 데이터가 없습니다.")
        sys.exit(1)
    
    # 2. 메시지 정제
    cleaned_messages = clean_messages(messages)
    
    if not cleaned_messages:
        print(f"⚠️ 정제 후 남은 메시지가 없습니다.")
        sys.exit(1)
    
    # 3. Document 변환
    document = convert_to_document(cleaned_messages)
    
    # 4. 리포트 프롬프트 생성
    prompt = generate_report_prompt(document, date_str)
    
    # 5. Claude로 리포트 생성
    report_text = create_report_with_claude(prompt)
    
    # 6. 통계 정보 추출
    stats = extract_stats_from_report(report_text, cleaned_messages)
    
    # 7. 리포트 저장
    report_path, stats_path = save_report(report_text, stats, date_str)
    
    # 8. Discord 전송 (선택 사항)
    if not args.no_discord:
        # 봇 모드에서는 봇을 통해 전송, 일반 모드에서는 웹훅 사용
        success = False
        
        # 먼저 웹훅 사용 시도
        if DISCORD_WEBHOOK_URL:
            try:
                success = await send_to_discord_webhook(report_path, date_str)
            except Exception as e:
                print(f"⚠️ 웹훅 전송 실패: {e}. 다른 방법을 시도합니다.")
        
        # 웹훅 실패 시 봇 전송 시도 (시간 제한 설정)
        if not success and DISCORD_TOKEN:
            print("🤖 디스코드 봇을 통한 전송을 시도합니다...")
            
            try:
                # 봇 명령어 파일 생성
                command_file = os.path.join(REPORTS_DIR, "pending_report.json")
                command_data = {
                    "action": "send_report",
                    "report_path": report_path,
                    "date_str": date_str,
                    "created_at": datetime.datetime.now().isoformat()
                }
                
                with open(command_file, "w", encoding="utf-8") as f:
                    json.dump(command_data, f, ensure_ascii=False, indent=2)
                
                print(f"✅ 봇 명령어 파일 생성: {command_file}")
                print("💡 리포트 봇이 실행 중이라면 곧 리포트가 전송됩니다.")
                
                # 봇이 실행 중인지 확인 방법 안내
                print("💡 봇이 실행 중이 아니라면 다음 명령어로 봇을 실행하세요:")
                print(f"   python run_vina_report_bot.py")
            except Exception as e:
                print(f"❌ 봇 명령어 생성 오류: {e}")
    
    print(f"✅ {date_str} 날짜의 리포트 생성이 완료되었습니다!")

# 리포트 전송 명령 처리
async def check_pending_report():
    """보류 중인 리포트 확인 및 전송"""
    command_file = os.path.join(REPORTS_DIR, "pending_report.json")
    
    if not os.path.exists(command_file):
        return False
    
    try:
        with open(command_file, "r", encoding="utf-8") as f:
            command_data = json.load(f)
        
        # 명령어가 유효한지 확인
        if command_data.get("action") == "send_report":
            report_path = command_data.get("report_path")
            date_str = command_data.get("date_str")
            
            if os.path.exists(report_path):
                print(f"📤 보류 중인 리포트 발견: {date_str}")
                
                # 리포트 전송
                success = await send_to_discord(report_path, date_str)
                
                # 명령어 파일 삭제
                os.remove(command_file)
                
                return success
        
        # 오래된 명령어 파일 삭제
        created_at = command_data.get("created_at")
        if created_at:
            created_time = datetime.datetime.fromisoformat(created_at)
            now = datetime.datetime.now()
            
            # 24시간 이상 지난 명령어는 삭제
            if (now - created_time).total_seconds() > 86400:
                os.remove(command_file)
                print("⚠️ 오래된 명령어 파일을 삭제했습니다.")
    
    except Exception as e:
        print(f"❌ 보류 중인 리포트 처리 오류: {e}")
    
    return False

# 디스코드 봇 이벤트
@discord_client.event
async def on_ready():
    print(f"🤖 디스코드 봇으로 로그인: {discord_client.user}")
    
    # 보류 중인 리포트 확인
    await check_pending_report()
    
    # 주기적으로 보류 중인 리포트 확인 (1분마다)
    discord_client.loop.create_task(periodic_report_check())

# 주기적인 리포트 확인 작업
async def periodic_report_check():
    """주기적으로 보류 중인 리포트 확인"""
    while True:
        await asyncio.sleep(60)  # 1분 대기
        await check_pending_report()

# 메시지 이벤트 처리
@discord_client.event
async def on_message(message):
    # 자기 자신의 메시지는 무시
    if message.author == discord_client.user:
        return
    
    # vina-리포트 채널에서만 명령 처리
    if message.channel.name != 'vina-리포트':
        return
    
    # 명령어 처리
    content = message.content.strip()
    
    # !report 명령어: 특정 날짜의 리포트 생성 및 전송
    if content.startswith('!report'):
        parts = content.split()
        date_str = None
        
        # 날짜 인자 확인
        if len(parts) > 1:
            date_arg = parts[1]
            # YYYY-MM-DD 형식 검증
            if re.match(r'^\d{4}-\d{2}-\d{2}$', date_arg):
                date_str = date_arg
        
        # 날짜가 지정되지 않은 경우 어제 날짜 사용
        if not date_str:
            yesterday = datetime.datetime.now() - datetime.timedelta(days=1)
            date_str = yesterday.strftime("%Y-%m-%d")
        
        # 진행 상황 메시지 전송
        progress_msg = await message.channel.send(f"🔍 {date_str} 날짜의 리포트를 생성 중입니다...")
        
        try:
            # 스레드에서 리포트 생성 작업 실행
            report_dir = os.path.join(REPORTS_DIR, date_str)
            report_path = os.path.join(report_dir, "report.md")
            force = True  # 기존 리포트 덮어쓰기
            
            # 1. 날짜별 대화 데이터 로드
            messages = load_conversation_data(date_str)
            
            if not messages:
                await progress_msg.edit(content=f"⚠️ {date_str} 날짜의 대화 데이터가 없습니다.")
                return
            
            # 2. 메시지 정제
            cleaned_messages = clean_messages(messages)
            
            if not cleaned_messages:
                await progress_msg.edit(content=f"⚠️ {date_str} 날짜의 정제된 메시지가 없습니다.")
                return
            
            # 3. Document 변환
            document = convert_to_document(cleaned_messages)
            
            # 4. 리포트 프롬프트 생성
            prompt = generate_report_prompt(document, date_str)
            
            # 진행 상황 업데이트
            await progress_msg.edit(content=f"🤖 {date_str} 날짜의 리포트를 생성 중입니다... Claude API 호출 중")
            
            # 5. Claude로 리포트 생성
            report_text = create_report_with_claude(prompt)
            
            # 6. 통계 정보 추출
            stats = extract_stats_from_report(report_text, cleaned_messages)
            
            # 7. 리포트 저장
            os.makedirs(report_dir, exist_ok=True)
            
            # 리포트 파일 저장
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(report_text)
            
            # 통계 파일 저장
            stats_path = os.path.join(report_dir, "stats.json")
            with open(stats_path, "w", encoding="utf-8") as f:
                json.dump(stats, f, ensure_ascii=False, indent=2)
            
            # 진행 상황 업데이트
            await progress_msg.edit(content=f"💾 {date_str} 날짜의 리포트가 저장되었습니다. 리포트를 전송합니다...")
            
            # 8. Discord 전송
            date_parts = date_str.split('-')
            formatted_date = f"{date_parts[0]}년 {int(date_parts[1])}월 {int(date_parts[2])}일"
            
            # Discord Embed 생성
            embed = discord.Embed(
                title=f"📝 {formatted_date} 리포트",
                description=report_text,
                color=0x3498db
            )
            
            # 리포트 전송
            await message.channel.send(embed=embed)
            
            # 진행 상황 메시지 삭제
            await progress_msg.delete()
            
        except Exception as e:
            await progress_msg.edit(content=f"❌ 오류 발생: {str(e)}")
    
    # !help 명령어: 도움말 표시
    elif content == '!help':
        help_embed = discord.Embed(
            title="📚 VINA 리포트 봇 도움말",
            description="VINA 대화 기록을 기반으로 일간 리포트를 생성합니다.",
            color=0x2ecc71
        )
        
        help_embed.add_field(
            name="!report [YYYY-MM-DD]",
            value="지정한 날짜의 리포트를 생성하고 전송합니다. 날짜를 지정하지 않으면 어제 날짜를 사용합니다.",
            inline=False
        )
        
        help_embed.add_field(
            name="!help",
            value="이 도움말을 표시합니다.",
            inline=False
        )
        
        await message.channel.send(embed=help_embed)

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == '--discord-bot':
        # 디스코드 봇 모드로 실행
        discord_client.run(DISCORD_TOKEN)
    else:
        # 일반 모드로 실행
        asyncio.run(main())
