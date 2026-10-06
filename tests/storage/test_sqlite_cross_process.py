"""跨进程 SQLite 写入（M5 多 worker 的数据库前提）。

探查点名的真空档：既有并发测试是"同进程双线程"；这里补
「两个真实进程各持连接，一侧长事务块写、一侧短写交替」，
断言 WAL + busy_timeout=5000 下零异常、数据双方齐全。
"""
import subprocess
import sys
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from src.storage.sqlite_provider import SQLiteProvider

REPO_ROOT = Path(__file__).resolve().parents[2]

_CHILD_SCRIPT = r"""
import sys, time
sys.path.insert(0, {repo!r})
from src.storage.sqlite_provider import SQLiteProvider
p = SQLiteProvider({db!r})
# 一次性 BEGIN IMMEDIATE 长事务（模拟 chat 轮末批量落盘）
conn = p._conn
conn.execute("BEGIN IMMEDIATE")
for i in range(200):
    conn.execute(
        "INSERT INTO kv(scope,key,value,updated_at) VALUES('bench','child' || ?,'x' || ?, 0)",
        (str(i).rjust(4, '0'), str(i)))
time.sleep(1.2)          # 持锁窗口：让父进程的短写必然撞 busy_timeout
conn.execute("COMMIT")
print("CHILD_DONE")
"""


def test_two_processes_long_write_alternating(tmp_path):
    db = tmp_path / "cross.db"
    parent = SQLiteProvider(db)
    try:
        out = tmp_path / "child.out"
        with open(out, "w", encoding="utf-8") as fh:
            proc = subprocess.Popen(
                [sys.executable, "-c",
                 _CHILD_SCRIPT.format(repo=str(REPO_ROOT), db=str(db))],
                stdout=fh, stderr=subprocess.STDOUT)

        time.sleep(0.4)                       # 让子进程先拿到写锁
        errors = []
        stop = threading.Event()

        def short_writer():
            i = 0
            while not stop.is_set() and i < 40:
                try:
                    parent.kv_put("bench", f"parent-{i:04d}", {"i": i})
                except Exception as e:    # 零异常断言的目标
                    errors.append(f"parent write {i}: {e!r}")
                i += 1
                time.sleep(0.05)

        th = threading.Thread(target=short_writer)
        th.start()
        proc.wait(timeout=30)
        stop.set()
        th.join(timeout=10)

        text = out.read_text(encoding="utf-8")
        assert "CHILD_DONE" in text, f"子进程异常: {text[-400:]}"
        assert errors == [], f"父进程写入遭遇异常（busy 饥饿）: {errors[:3]}"

        # 双方数据齐全：子进程 200 条长事务 + 父进程 ≤40 条短写
        n_child = parent.query(
            "SELECT COUNT(*) AS c FROM kv WHERE scope='bench' AND key LIKE 'child%'"
        )[0]["c"]
        n_parent = parent.query(
            "SELECT COUNT(*) AS c FROM kv WHERE scope='bench' AND key LIKE 'parent-%'"
        )[0]["c"]
        assert n_child == 200
        # 短写被 busy 拖慢是预期，但必须全部成功。
        # 2026-08：upsert 增加辅助索引同步（fts/向量），单次持锁变长，
        # 竞争下父进程吞吐下限从 10 放宽到 5——正确性断言是"全部成功"。
        assert 5 <= n_parent <= 40
    finally:
        parent.close()
