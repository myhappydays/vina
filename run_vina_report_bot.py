"""
VINA 리포트 봇 실행 스크립트

이 스크립트는 VINA 리포트 봇을 실행합니다.
봇이 실행되면 'vina-리포트' 채널에 리포트를 전송할 수 있습니다.
"""

import os
import sys
import discord
from dotenv import load_dotenv

# 환경 변수 로드
load_dotenv()

# 디스코드 봇 토큰 확인
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
if not DISCORD_TOKEN:
    print("❌ 오류: DISCORD_BOT_TOKEN 환경 변수가 설정되지 않았습니다.")
    print("💡 .env 파일에 DISCORD_BOT_TOKEN을 설정하세요.")
    sys.exit(1)

print("🚀 VINA 리포트 봇을 실행합니다...")

# vinareport.py를 봇 모드로 실행
os.system(f"{sys.executable} vinareport.py --discord-bot")

print("✅ 봇이 성공적으로 종료되었습니다.") 