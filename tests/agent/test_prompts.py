"""System Prompt 构建测试。

验证 Task 12.5 的核心改动：时间戳注入。
验证 Task 12 的核心改动：remember 引导段存在。
"""
import re
from datetime import datetime

from src.prompts import build_system_prompt


def test_system_prompt_contains_current_date():
    """build_system_prompt 必须包含今天的日期（YYYY-MM-DD）。"""
    prompt = build_system_prompt("alice")
    today = datetime.now().strftime("%Y-%m-%d")
    assert today in prompt, f"prompt 中找不到今天的日期 {today}"


def test_system_prompt_contains_current_weekday():
    """包含中文星期，帮助 LLM 理解"上周/这周"等相对时间。"""
    prompt = build_system_prompt("alice")
    assert re.search(r"星期[一二三四五六日天]|周[一二三四五六日天]", prompt), \
        "prompt 中找不到星期信息"


def test_system_prompt_contains_current_time():
    """包含当前时段（HH:MM），支持"刚才/今天早上"等表达。"""
    prompt = build_system_prompt("alice")
    assert re.search(r"\d{2}:\d{2}", prompt), "prompt 中找不到当前时间"


def test_system_prompt_user_id_still_present():
    """时间戳加入后，原有 user_id 仍在。"""
    prompt = build_system_prompt("bob")
    assert "bob" in prompt


def test_time_info_not_hardcoded():
    """防御时间被写成常量——提取出的日期应等于今天。"""
    prompt = build_system_prompt("alice")
    today = datetime.now().strftime("%Y-%m-%d")
    dates = re.findall(r"\d{4}-\d{2}-\d{2}", prompt)
    assert today in dates


def test_remember_guidance_present():
    prompt = build_system_prompt("alice")
    assert "remember" in prompt
    assert "profile.md" in prompt or "原子事实" in prompt


def test_memory_correction_guidance_present():
    prompt = build_system_prompt("alice")
    assert "remember" in prompt
    assert "原子事实" in prompt or "profile.md" in prompt


# ============================================
# role 参数（thinktank 退役后为兼容形参，一律默认人格）
# ============================================


def test_build_prompt_default_persona():
    """role=None → 用默认 Hermes 助手人格。"""
    prompt = build_system_prompt("alice")
    assert "Hermes-Ma" in prompt or "智能助手" in prompt


def test_build_prompt_role_ignored_after_thinktank_removal():
    """role 传任意值 → 按默认助手人格处理（兼容形参，不报错）。"""
    prompt = build_system_prompt("alice", role="whatever")
    assert "Hermes-Ma" in prompt or "智能助手" in prompt
    # waker_persona 通道不受影响
    p2 = build_system_prompt("alice", role="x", waker_persona="## 数字员工人格\n测试")
    assert "数字员工人格" in p2


def test_system_prompt_shell_platform_notice():
    """Shell 段必须声明 Windows + Git Bash 平台与 `/` 双语义。

    背景：模型曾按 Unix 习惯 `cd /`、`find /` 全盘扫描（Git Bash 的
    `/` 是 MSYS 虚拟根=Git 安装目录），30s 超时三连。平台说明进
    系统提示后模型应在工作区内/盘符路径查找。
    """
    prompt = build_system_prompt("alice")
    assert "Windows" in prompt
    assert "Git Bash" in prompt
    assert "MSYS" in prompt          # `/` 语义说明
    assert "find /" in prompt        # 禁止性示例出现
