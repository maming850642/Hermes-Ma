#!/usr/bin/env python3
"""
Hermes 连通性测试脚本
独立运行，测试 LLM API 的连通性。

2026-06-27 (M0)：移除 Qdrant / Embedding 检查（记忆改纯文件，不再依赖二者）。
"""

import sys
import logging

if sys.platform == "win32":
    sys.stdout = open(sys.stdout.fileno(), mode='w', encoding='utf-8', buffering=1)
    sys.stderr = open(sys.stderr.fileno(), mode='w', encoding='utf-8', buffering=1)

# 注：本脚本是独立诊断工具，故意不接入 src/logging_config.py 的分层日志体系。
logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", handlers=[logging.StreamHandler(sys.stderr)])
logger = logging.getLogger("hermes.conn_test")
for handler in logging.getLogger().handlers:
    handler.setLevel(logging.ERROR)


def check_llm_api():
    from config import get_settings
    import httpx
    settings = get_settings()
    url = f"{settings.openai_base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {settings.openai_api_key}"} if settings.openai_api_key else {}
    print(f"  请求: GET {url}")
    print(f"  API Key: {'***' if settings.openai_api_key else '(empty)'}")
    if not settings.openai_api_key:
        print(f"  结果: WARN - API Key 为空，跳过认证检查")
        return True
    try:
        resp = httpx.get(url, headers=headers, timeout=10.0)
        if resp.status_code == 200:
            data = resp.json()
            models = data.get("data", [])
            print(f"  结果: PASS - 成功，可用模型数: {len(models)}")
            if models:
                print(f"  第一个模型: {models[0].get('id', 'unknown')}")
            return True
        if resp.status_code == 401:
            print(f"  结果: FAIL - API Key 无效 (401)")
            return False
        print(f"  结果: WARN - 状态码 {resp.status_code}, 响应: {resp.text[:200]}")
        return True
    except httpx.ConnectTimeout:
        print(f"  结果: FAIL - 连接超时")
        return False
    except httpx.ConnectError as e:
        print(f"  结果: FAIL - 连接失败: {e}")
        return False
    except Exception as e:
        print(f"  结果: FAIL - 异常: {e}")
        return False


def main():
    print("=" * 60)
    print("  Hermes 连通性测试")
    print("=" * 60)
    from config import get_settings
    settings = get_settings()
    print("\nCurrent Configuration:")
    print(f"  LLM API: {settings.openai_base_url}")
    print(f"  LLM Model: {settings.llm_model_name}")
    print(f"  记忆后端: 纯文件 (FileMemoryStore)")
    results = {}
    print("\nStarting tests...")
    print("\n--- LLM API ---")
    results["LLM API"] = check_llm_api()
    print("\n" + "=" * 60)
    print("  Test Results Summary:")
    print("=" * 60)
    all_pass = True
    for name, passed in results.items():
        status = "PASS" if passed else "FAIL"
        if not passed:
            all_pass = False
        print(f"  {name:15s} {status}")
    print()
    if all_pass:
        print("  All checks passed!")
    else:
        print("  Some checks failed, please review the output above.")
    print()
    return 0 if all_pass else 1


if __name__ == "__main__":
    sys.exit(main())
