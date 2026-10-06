"""P2-6：lifespan 启动 ops 心跳（app.py _log_startup_ops_heartbeat）单测。

契约：
- 装配完成后打恰好一条 INFO，一行可 grep：[ops-heartbeat] worker_slots=…
  wal_bytes=… events_rows=… memories_rows=…；
- 查询失败/全新环境静默降级：缺项渲染 "-"，采集异常只落 debug，
  绝不让启动失败（函数本身绝不抛）。
"""
import logging
from types import SimpleNamespace

import pytest

from web_fastapi.app import _log_startup_ops_heartbeat


class _Mgr:
    def all_slots(self):
        return [(("u"), "main")]


class _App:
    state = SimpleNamespace(worker_manager=_Mgr())


class _BoomMgr:
    def all_slots(self):
        raise RuntimeError("boom")


def test_heartbeat_logs_single_grepable_line(caplog, isolated_data_env):
    with caplog.at_level(logging.INFO, logger="hermes.web"):
        _log_startup_ops_heartbeat(_App())  # storage=None → 自建默认库（tmp）
    recs = [r for r in caplog.records if "[ops-heartbeat]" in r.getMessage()]
    assert len(recs) == 1
    msg = recs[0].getMessage()
    assert msg.count("\n") == 0
    assert "worker_slots=1" in msg
    assert "wal_bytes=" in msg and "events_rows=" in msg and "memories_rows=" in msg


def test_heartbeat_degrades_silently_on_query_failure(caplog):
    """provider 各项查询全炸 → 缺项渲染 "-"，仍是一条 INFO；绝不抛异常。"""

    class _BoomProvider:
        def wal_size_bytes(self):
            raise RuntimeError("boom")

        def query(self, sql, params=()):
            raise RuntimeError("boom")

    with caplog.at_level(logging.INFO, logger="hermes.web"):
        _log_startup_ops_heartbeat(_App(), storage=_BoomProvider())
    recs = [r for r in caplog.records if r.levelno == logging.INFO
            and "[ops-heartbeat]" in r.getMessage()]
    assert len(recs) == 1
    assert "wal_bytes=-" in recs[0].getMessage()
    assert "events_rows=-" in recs[0].getMessage()
    assert "memories_rows=-" in recs[0].getMessage()


def test_heartbeat_never_raises(caplog):
    """连槽位枚举都失败（worker_manager 异常）也只落 debug，不向 lifespan 传异常。"""
    with caplog.at_level(logging.DEBUG, logger="hermes.web"):
        _log_startup_ops_heartbeat(SimpleNamespace(
            state=SimpleNamespace(worker_manager=_BoomMgr())))
    assert not [r for r in caplog.records
                if r.levelno == logging.INFO and "[ops-heartbeat]" in r.getMessage()]
    assert [r for r in caplog.records
            if r.levelno == logging.DEBUG and "采集失败" in r.getMessage()]


@pytest.mark.parametrize("v,expect", [(None, "-"), (0, "0"), (123, "123")])
def test_hb_value_rendering(v, expect):
    from web_fastapi.app import _hb_value
    assert _hb_value(v) == expect
