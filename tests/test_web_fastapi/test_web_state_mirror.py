"""权限模式/prefs 镜像持久化（data/web_state.json）+ GET 读镜像。

历史行为：模式只活在 worker 内存，main 槽重启不重放，新标签页把
sessionStorage 默认值静默 PUT 回去——"完全访问"开关经常莫名失效。
现契约：镜像落盘为唯一事实源，重启保持；GET 零 IPC 读镜像。
"""
import json

from web_fastapi.worker_manager import WorkerManager


def test_remember_persists_to_disk(tmp_path):
    p = tmp_path / "web_state.json"
    m = WorkerManager(max_parallel=2, state_path=p)
    m.remember_permission_mode("full_access")
    m.remember_prefs({"temperature": 0.5})

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["permission_mode"] == "full_access"
    assert data["prefs"] == {"temperature": 0.5}


def test_remember_prefs_merges_keys_across_submissions(tmp_path):
    """Fix8：分次提交合并而非替换——先设 temperature 再设 max_tokens，镜像两键都在。

    替换语义会让后一次提交悄悄丢掉前一次的键（新槽 spawn 重放时旧键消失，
    多实例 prefs 语义漂移）。"""
    p = tmp_path / "web_state.json"
    m = WorkerManager(max_parallel=2, state_path=p)
    m.remember_prefs({"temperature": 0.5})
    m.remember_prefs({"max_tokens": 4096})
    assert m._mirror_prefs == {"temperature": 0.5, "max_tokens": 4096}

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["prefs"] == {"temperature": 0.5, "max_tokens": 4096}

    # 全新 Manager 从盘读回：合并结果持久有效
    m2 = WorkerManager(max_parallel=2, state_path=p)
    assert m2._mirror_prefs == {"temperature": 0.5, "max_tokens": 4096}


def test_reload_keeps_mode_after_restart(tmp_path):
    p = tmp_path / "web_state.json"
    m1 = WorkerManager(max_parallel=2, state_path=p)
    m1.remember_permission_mode("full_access")

    # 模拟 web 进程重启：全新 Manager 从同一文件读回
    m2 = WorkerManager(max_parallel=2, state_path=p)
    assert m2.get_permission_mode() == "full_access"


def test_get_permission_mode_defaults_without_file(tmp_path):
    m = WorkerManager(max_parallel=2, state_path=tmp_path / "absent.json")
    assert m.get_permission_mode() == "before_changes"


def test_corrupt_state_file_falls_back(tmp_path):
    p = tmp_path / "web_state.json"
    p.write_text("不是JSON{{{", encoding="utf-8")
    m = WorkerManager(max_parallel=2, state_path=p)
    assert m.get_permission_mode() == "before_changes"
    # 损坏文件不阻塞后续写入
    m.remember_permission_mode("plan")
    assert m.get_permission_mode() == "plan"


def test_invalid_mode_value_in_file_ignored(tmp_path):
    p = tmp_path / "web_state.json"
    p.write_text(json.dumps({"permission_mode": "yolo_hack"}), encoding="utf-8")
    m = WorkerManager(max_parallel=2, state_path=p)
    assert m.get_permission_mode() == "before_changes"


def test_get_route_reads_mirror_not_worker():
    """GET /api/config/permission-mode 必须读 Manager 镜像（零 IPC）——
    历史版本走 worker.send，chat 忙时拿不到锁，读取失败被回落成默认值。"""
    from unittest.mock import MagicMock
    from web_fastapi.routers.config_router import get_permission_mode

    req = MagicMock()
    req.app.state.worker_manager.get_permission_mode.return_value = "full_access"
    assert get_permission_mode(req) == {"permission_mode": "full_access"}
