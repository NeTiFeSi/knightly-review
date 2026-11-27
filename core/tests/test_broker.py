"""The shared Stockfish pool: priorities, cancelling, broken processes, and the lease across processes."""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

import chess.engine
from knightly.cache import SqliteCache
from knightly.config import Config
from knightly.engine.broker import BACKFILL, CONFIRM, INTERACTIVE, LEASE_KEY, SCAN, Lease, Pool
from knightly.http import Cancelled


class Proc:
    def __init__(self, n: int = 0):
        self.n = n
        self.id = {"name": "Stockfish 17"}
        self.quit_called = False

    def quit(self):
        self.quit_called = True


def tmpcache(test: unittest.TestCase) -> SqliteCache:
    d = tempfile.TemporaryDirectory()
    test.addCleanup(d.cleanup)
    c = SqliteCache(Path(d.name) / "c.sqlite")
    test.addCleanup(c.close)
    return c


def starter():
    made = []

    def start():
        made.append(Proc(len(made)))
        return made[-1]

    return start, made


def wait_until(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > end:
            raise AssertionError("timed out")
        time.sleep(0.01)


def waiting(pool: Pool, n: int):
    return lambda: len(pool._waiting) == n


class PoolTest(unittest.TestCase):
    def test_processes_start_when_needed_and_are_reused(self):
        start, made = starter()
        pool = Pool(Config(), start=start)
        with pool.acquire() as p:
            self.assertIs(p, made[0])
        with pool.acquire():
            pass
        self.assertEqual((len(made), pool.started, pool.name), (1, 1, "Stockfish 17"))
        pool.close()
        self.assertTrue(made[0].quit_called)

    def test_the_most_urgent_waiting_request_goes_first(self):
        start, _ = starter()
        pool = Pool(Config(), start=start)
        order: list[str] = []
        release = threading.Event()

        def hold():
            with pool.acquire(CONFIRM):
                order.append("first")
                release.wait(5)

        def ask(name, priority):
            with pool.acquire(priority):
                order.append(name)

        threads = [threading.Thread(target=hold)]
        threads[0].start()
        wait_until(lambda: order == ["first"])
        for name, prio in (("backfill", BACKFILL), ("scan", SCAN), ("scan 2", SCAN), ("interactive", INTERACTIVE)):
            th = threading.Thread(target=ask, args=(name, prio))
            th.start()
            threads.append(th)
            wait_until(waiting(pool, len(threads) - 1))
        release.set()
        for th in threads:
            th.join(5)
        self.assertEqual(order, ["first", "interactive", "scan", "scan 2", "backfill"])

    def test_several_processes_run_at_once(self):
        start, made = starter()
        pool = Pool(Config(), size=2, start=start)
        with pool.acquire() as a, pool.acquire() as b:
            self.assertIsNot(a, b)
        self.assertEqual(len(made), 2)

    def test_cancel_while_waiting(self):
        start, _ = starter()
        pool = Pool(Config(), start=start)
        cancel = threading.Event()
        got: list[object] = []

        def waiter():
            try:
                with pool.acquire(SCAN, cancel):
                    got.append("ran")
            except Cancelled:
                got.append("cancelled")

        with pool.acquire():
            th = threading.Thread(target=waiter)
            th.start()
            wait_until(lambda: len(pool._waiting) == 1)
            cancel.set()
            th.join(5)
        self.assertEqual(got, ["cancelled"])
        self.assertEqual(pool._waiting, [])
        with pool.acquire():  # the cancelled request no longer blocks anyone
            pass

    def test_a_broken_process_is_replaced(self):
        start, made = starter()
        pool = Pool(Config(), start=start)
        with self.assertRaises(chess.engine.EngineTerminatedError), pool.acquire():
            raise chess.engine.EngineTerminatedError("crashed")
        self.assertTrue(made[0].quit_called)
        with pool.acquire() as p:
            self.assertIs(p, made[1])

    def test_idle_processes_are_stopped_and_the_lease_given_back(self):
        c = tmpcache(self)
        start, made = starter()
        pool = Pool(Config(), lease=Lease(c), start=start)
        with pool.acquire():
            pass
        self.assertEqual(pool.reap(max_idle=60), 0)  # idle for less than a minute: kept
        self.assertIsNotNone(c.get(LEASE_KEY))
        self.assertEqual(pool.reap(max_idle=0), 1)
        self.assertTrue(made[0].quit_called)
        self.assertIsNone(c.get(LEASE_KEY))
        with pool.acquire() as p:  # a new process, and the lease again
            self.assertIs(p, made[1])
        self.assertIsNotNone(c.get(LEASE_KEY))

    def test_new_engine_settings_replace_the_processes(self):
        start, made = starter()
        pool = Pool(Config(), start=start)
        with pool.acquire():
            pass
        pool.reconfigure(Config.from_dict({"deck": "other"}))  # not the engine's: kept
        self.assertFalse(made[0].quit_called)
        with pool.acquire() as busy:
            pool.reconfigure(Config.from_dict({"hash_mb": 128}))
            self.assertFalse(busy.quit_called)  # stopped when its search returns
        self.assertTrue(made[0].quit_called)
        with pool.acquire() as p:
            self.assertIs(p, made[1])
        self.assertEqual(pool.cfg.engine.hash_mb, 128)

    def test_a_failing_start_frees_its_place(self):
        calls = []

        def start():
            calls.append(1)
            if len(calls) == 1:
                raise OSError("no such file")
            return Proc()

        pool = Pool(Config(), start=start)
        with self.assertRaises(OSError), pool.acquire():
            pass
        with pool.acquire():
            pass
        self.assertEqual(len(calls), 2)


class LeaseTest(unittest.TestCase):
    def test_one_process_at_a_time(self):
        c = tmpcache(self)
        ours, theirs = Lease(c, owner="a"), Lease(c, owner="b")
        ours.acquire()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(Cancelled):
            theirs.acquire(cancel, poll=0.01)
        ours.release()
        theirs.acquire()
        self.assertEqual(c.get(LEASE_KEY)["owner"], "b")

    def test_an_expired_lease_is_taken(self):
        c = tmpcache(self)
        self.assertTrue(c.claim(LEASE_KEY, "dead", -1))  # expired at once
        Lease(c, owner="b").acquire()
        self.assertEqual(c.get(LEASE_KEY)["owner"], "b")

    def test_the_owner_renews(self):
        c = tmpcache(self)
        self.assertTrue(c.claim(LEASE_KEY, "a", 60))
        self.assertTrue(c.claim(LEASE_KEY, "a", 60))
        self.assertFalse(c.claim(LEASE_KEY, "b", 60))

    def test_pools_of_one_process_share_its_lease(self):
        c = tmpcache(self)
        first, second = Lease(c), Lease(c)
        first.acquire()
        second.acquire()  # same process: no waiting
        first.release()
        self.assertIsNotNone(c.get(LEASE_KEY))  # still held for `second`
        second.release()
        self.assertIsNone(c.get(LEASE_KEY))

    def test_a_pool_waits_for_another_processes_lease(self):
        c = tmpcache(self)
        self.assertTrue(c.claim(LEASE_KEY, "someone else", 60))
        start, made = starter()
        pool = Pool(Config(), lease=Lease(c), start=start)
        cancel = threading.Event()
        threading.Timer(0.05, cancel.set).start()
        with self.assertRaises(Cancelled), pool.acquire(cancel=cancel):
            pass
        self.assertEqual(made, [])  # no Stockfish started while the other process runs one
        c.unclaim(LEASE_KEY, "someone else")
        with pool.acquire():
            pass
        pool.close()
        self.assertIsNone(c.get(LEASE_KEY))


if __name__ == "__main__":
    unittest.main()
