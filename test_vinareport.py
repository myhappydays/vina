"""
VINA 리포트 스크립트 테스트

이 스크립트는 실제 데이터로 리포트 작성 기능을 테스트합니다.
"""

import os
import sys
import datetime
import pytest
from vinareport import (
    load_conversation_data,
    clean_messages,
    convert_to_document,
    generate_report_prompt,
    extract_stats_from_report
)

def test_load_conversation_data():
    """날짜별 대화 데이터 로드 함수 테스트"""
    # 2025-04-24 날짜 데이터 테스트
    test_date = "2025-04-24"
    messages = load_conversation_data(test_date)
    
    # 2025-04-24에 대화가 있는지 확인
    assert len(messages) > 0, f"{test_date} 날짜의 대화 데이터가 없습니다."
    
    # 모든 메시지가 해당 날짜의 것인지 확인
    for msg in messages:
        if "time" in msg and msg["time"]:
            msg_date = datetime.datetime.fromisoformat(msg["time"]).strftime("%Y-%m-%d")
            assert msg_date == test_date, f"잘못된 날짜의 메시지가 포함되어 있습니다: {msg_date}"
    
    print(f"✅ 테스트 성공: {test_date} 날짜의 {len(messages)}개 메시지 로드됨")
    return messages

def test_clean_messages(messages):
    """메시지 정제 함수 테스트"""
    cleaned_msgs = clean_messages(messages)
    
    # 정제 후에도 메시지가 남아있는지 확인
    assert len(cleaned_msgs) > 0, "정제 후 남은 메시지가 없습니다."
    
    # 정제 후 메시지가 원본보다 적은지 확인 (필터링 작동 확인)
    assert len(cleaned_msgs) <= len(messages), "정제 후 메시지 수가 원본과 같거나 더 많습니다."
    
    # 모든 메시지가 필수 필드를 가지고 있는지 확인
    for msg in cleaned_msgs:
        assert "role" in msg, "role 필드가 없는 메시지가 있습니다."
        assert "content" in msg, "content 필드가 없는 메시지가 있습니다."
        assert msg["role"] in ["user", "assistant"], f"잘못된 role 값이 있습니다: {msg['role']}"
    
    print(f"✅ 테스트 성공: {len(messages)}개 -> {len(cleaned_msgs)}개로 정제됨")
    return cleaned_msgs

def test_document_conversion(cleaned_msgs):
    """Document 변환 함수 테스트"""
    document = convert_to_document(cleaned_msgs)
    
    # Document 객체가 생성되었는지 확인
    assert document is not None, "Document 객체가 생성되지 않았습니다."
    
    # Document에 텍스트가 포함되어 있는지 확인
    assert len(document.text) > 0, "Document 텍스트가 비어 있습니다."
    
    # 메타데이터가 설정되었는지 확인
    assert "message_count" in document.metadata, "메타데이터에 message_count가 없습니다."
    assert document.metadata["message_count"] == len(cleaned_msgs), "메타데이터의 message_count가 일치하지 않습니다."
    
    print(f"✅ 테스트 성공: {len(document.text)} 자의 Document 생성됨")
    return document

def test_prompt_generation(document, date_str="2025-04-24"):
    """프롬프트 생성 함수 테스트"""
    prompt = generate_report_prompt(document, date_str)
    
    # 프롬프트가 생성되었는지 확인
    assert prompt is not None, "프롬프트가 생성되지 않았습니다."
    assert len(prompt) > 0, "프롬프트가 비어 있습니다."
    
    # 프롬프트에 날짜 정보가 포함되어 있는지 확인
    formatted_date = f"{date_str.split('-')[0]}년 {int(date_str.split('-')[1])}월 {int(date_str.split('-')[2])}일"
    assert formatted_date in prompt, f"프롬프트에 변환된 날짜({formatted_date})가 포함되어 있지 않습니다."
    
    # 프롬프트에 대화 내용이 포함되어 있는지 확인
    assert document.text in prompt, "프롬프트에 대화 내용이 포함되어 있지 않습니다."
    
    print(f"✅ 테스트 성공: {len(prompt)} 자의 프롬프트 생성됨")
    return prompt

def test_stats_extraction():
    """통계 정보 추출 함수 테스트"""
    # 샘플 리포트 텍스트
    sample_report = """# 2025년 4월 24일 리포트

오전에는 실용영어 중간고사를 앞두고 긴장한 모습이 드러났다.  
점심 무렵에는 마라탕을 추천받으며 잠시 기분 전환을 시도했지만, 시험 후 피로가 느껴졌다.  
대화 전체의 흐름은 차분했으며, 강한 감정 기복은 없었던 하루였다.

---

**🧠 핵심 키워드**: 실용영어, 중간고사, 마라탕, 점메추  
**💬 메시지 수**: 35개  
**🕒 총 대화 시간**: 약 3시간  
**🌟 오늘의 문장**: "점메추 해줘!"

---
"""
    
    # 샘플 메시지 데이터
    sample_messages = [
        {"role": "user", "content": "안녕", "time": "2025-04-24T10:00:00"},
        {"role": "assistant", "content": "안녕하세요!", "time": "2025-04-24T10:05:00"}
    ]
    
    stats = extract_stats_from_report(sample_report, sample_messages)
    
    # 통계 정보가 추출되었는지 확인
    assert "message_count" in stats, "메시지 수가 추출되지 않았습니다."
    assert "keywords" in stats, "키워드가 추출되지 않았습니다."
    assert "todays_quote" in stats, "오늘의 문장이 추출되지 않았습니다."
    
    # 추출된 정보가 맞는지 확인
    assert stats["message_count"] == len(sample_messages), "메시지 수가 일치하지 않습니다."
    assert "실용영어" in stats["keywords"], "키워드가 올바르게 추출되지 않았습니다."
    assert stats["todays_quote"] == "점메추 해줘!", "오늘의 문장이 올바르게 추출되지 않았습니다."
    
    print(f"✅ 테스트 성공: 통계 정보 추출 성공 - {stats}")
    return stats

def run_tests():
    """모든 테스트 실행"""
    try:
        messages = test_load_conversation_data()
        cleaned_msgs = test_clean_messages(messages)
        document = test_document_conversion(cleaned_msgs)
        prompt = test_prompt_generation(document)
        stats = test_stats_extraction()
        
        print("\n🎉 모든 테스트가 성공적으로 완료되었습니다!")
        return True
    except AssertionError as e:
        print(f"\n❌ 테스트 실패: {str(e)}")
        return False
    except Exception as e:
        print(f"\n❌ 예상치 못한 오류 발생: {str(e)}")
        return False

if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1) 