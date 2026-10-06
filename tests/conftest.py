"""共享测试 fixture —— 全部**选择加入**（无 autouse）：不用就不会改变任何现有测试行为。

背景坑（refactor-roadmap P1-4 登记的那条）：session_store 的 SESSIONS_DIR /
SUMMARIES_DIR 是 import 时求值的模块级常量，**不吃 set_data_root()**；且
src/cli 包把它们 re-export 成了自己的包属性（src/cli/__init__.py 的
re-import 块，P2 拆包前是 src/cli.py:62-63），只 patch 定义处时，cli 里的
import 绑定副本仍指向真实 data/。要隔离就得"定义处 + 绑定副本"两处都打
——这正是 isolated_data_env 存在的原因。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_usage_store(tmp_path):
    """全局兜底（autouse）：usage_store 的默认 provider 每个用例都改道 tmp。

    背景（验收泄漏事故）：D1 记账埋点在 client.py 的 chat()/stream_chat() 里
    调 usage_store.record_usage → 模块级默认 provider（懒加载，路径取
    paths.data_dir("hermes.db")，默认数据根 = 真实 <repo>/data）。任何不带
    db/isolated_data_env 隔离、直接构造真实 LLMClient + stub 内层跑 chat()/
    stream_chat() 的测试（如 tests/agent/test_llm_client_params.py），都会把
    记账行写进生产库 data/hermes.db（model='m'、tokens NULL 的垃圾行，污染
    用量看板）。

    做法（即 tests/agent/test_llm_usage_accounting.py 的 db fixture 上提为
    全局）：usage_store 默认路径本就从 paths.data_root() 推导，setup 把数据根
    改到本用例 tmp_path 的**子目录**（不是 tmp_path 本身——不少测试把待挂载
    项目/工作区直接建在 tmp_path 下，数据根若就是 tmp_path，会触发 workspace
    service 的"拒绝挂载数据根子树"保护，见 tests/cli/test_cli_tools_surface
    的挂载用例）并清空默认连接缓存（逼其按 tmp 重建），teardown 清缓存 +
    恢复默认根。与各测试自己的 data_root/db/isolated_data_env fixture 幂等
    兼容（同值重设、收尾归 None）；不碰存储的测试零开销（provider 懒构建，
    不触发就不建库文件）。
    """
    from src.storage import paths, usage_store

    usage_root = tmp_path / "usage-root"
    paths.set_data_root(usage_root)
    usage_store.reset_default_provider()
    yield usage_root
    usage_store.reset_default_provider()
    paths.set_data_root(None)


@pytest.fixture
def isolated_data_env(tmp_path, monkeypatch, request):
    """一步到位隔离 data/：数据根 + SESSIONS_DIR/SUMMARIES_DIR + 默认库连接缓存。

    适用：会直接/间接读写真实 data/ 的测试（会话 JSON、summaries、hermes.db、
    激活项目指针、任何经 SQLiteProvider 默认路径的存储）。做四件事，全部随
    monkeypatch / finalizer 还原，测试之间互不污染：

    1. set_data_root(tmp_path)：paths.data_root()/data_dir() 全部改道 tmp
       （SQLiteProvider 的默认库路径 data_dir("hermes.db") 随之改道）；
    2. patch SESSIONS_DIR / SUMMARIES_DIR 定义处（src/session_store.py:41,43）
       ——这两个是模块级常量，不吃 set_data_root；
    3. patch src/cli 的 import 绑定副本（src/cli/__init__.py 对
       session_store 的 re-export）——import 时已绑定的名字，第 2 步的
       patch 传导不到它们；
    4. 清空 projects_store._default_provider（get_active_project 的进程级默认
       连接缓存，懒构造一次）：不清理会拿到指向真实库的旧连接，或把 tmp 库
       句柄以"缓存"名义泄漏给后续测试。
    5. 清空 session_state_store 的默认连接缓存（P3 会话状态 kv 权威层的
       进程级缓存，同 4 的道理；其缓存按解析路径键控，不清理也自愈，这里
       显式重置 + 关闭 tmp 连接，防句柄跨测试累积）。

    返回 tmp_path（即数据根），测试里拼路径用它。
    """
    import src.session_store as session_store
    import src.storage.paths as paths
    import src.storage.projects_store as projects_store
    from src.storage import session_state_store

    # 1) 数据根改道（含 SQLiteProvider 默认库路径），测毕恢复默认
    paths.set_data_root(tmp_path)
    request.addfinalizer(lambda: paths.set_data_root(None))

    # 2) + 3) 模块级常量：定义处 + cli 的 import 绑定副本，两处缺一不可
    sessions_dir = tmp_path / "sessions"
    summaries_dir = tmp_path / "summaries"
    monkeypatch.setattr(session_store, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(session_store, "SUMMARIES_DIR", summaries_dir)
    import src.cli as cli  # 延迟 import：conftest 收集期不拖起 cli 的重依赖
    monkeypatch.setattr(cli, "SESSIONS_DIR", sessions_dir)
    monkeypatch.setattr(cli, "SUMMARIES_DIR", summaries_dir)

    # 4) 默认库连接缓存清零，逼它按新 data_root 重建（teardown 还原原值）
    monkeypatch.setattr(projects_store, "_default_provider", None)

    # 5) 会话状态 kv 层的默认连接缓存：setup 清旧 + teardown 关 tmp 连接
    session_state_store.reset_default_provider()
    request.addfinalizer(session_state_store.reset_default_provider)
    return tmp_path


@pytest.fixture
def isolated_project_pointer(tmp_path, monkeypatch):
    """只隔离"当前激活项目"指针，不动数据根与 SESSIONS_DIR。

    适用：只想控制/断言 active slug 读写、又不需要整棵 data/ 隔离的测试。
    get_active_project() 的默认缓存调用方（web_fastapi/routers/chat.py:316、
    web_fastapi/routers/pages.py:22、src/memory/manager.py:58）会全部改读
    tmp 独立库；显式传 provider 的调用方不受影响。

    做法：给 projects_store 的进程级默认连接缓存（懒构造一次，见
    src/storage/projects_store.py:177-196）塞一个指向 tmp 库的 provider，
    测毕关闭连接并还原缓存原值。返回该 provider，测试可直接经
    ProjectStore(provider).set_active(...) 造指针。
    """
    import src.storage.projects_store as projects_store
    from src.storage.sqlite_provider import SQLiteProvider

    provider = SQLiteProvider(tmp_path / "active_pointer.db")
    monkeypatch.setattr(projects_store, "_default_provider", provider)
    # 路径键同步注入（P3 起缓存按解析路径键控，缺这个会被判"路径漂移"重建）
    monkeypatch.setattr(projects_store, "_default_provider_path",
                        str(tmp_path / "active_pointer.db"))
    yield provider
    provider.close()
