"""Real host telemetry in GET /api/metrics -> `resources` (psutil-backed).

Covers the live endpoint (numeric, in-range values, backward-compatible
payload), graceful behaviour without psutil, the no-mock-values guard and the
secret/identity-free guarantee.
"""
from __future__ import annotations

import json
import os
import re
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from astra import host_metrics
from astra.host_metrics import HostMetrics, resources_snapshot

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
        return fh.read()


def _isnum(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


class TestResourcesLive(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.helpers import LiveServer, make_stack
        cls.srv = LiveServer(stack=make_stack())
        cls.data = cls._fetch()
        cls.data2 = cls._fetch()          # second sample -> network rates exist

    @classmethod
    def tearDownClass(cls):
        cls.srv.close() if hasattr(cls.srv, "close") else None

    @classmethod
    def _fetch(cls):
        import time
        time.sleep(0.6)
        with urllib.request.urlopen(cls.srv.base + "/api/metrics", timeout=10) as r:
            return json.loads(r.read())["data"]

    def test_metrics_returns_resources_and_keeps_existing_fields(self):
        for key in ("app", "uptime_s", "requests"):
            self.assertIn(key, self.data)
        self.assertIn("router", self.data)
        self.assertIn("resources", self.data)

    def test_cpu_memory_disk_are_numeric_and_within_range(self):
        r = self.data2["resources"]
        self.assertIs(r["available"], True)
        for section in ("cpu", "memory", "disk"):
            p = r[section]["percent"]
            self.assertTrue(_isnum(p), section)
            self.assertTrue(0 <= p <= 100, section)

    def test_memory_and_disk_byte_fields(self):
        r = self.data2["resources"]
        for k in ("used_bytes", "total_bytes", "available_bytes"):
            self.assertTrue(_isnum(r["memory"][k]), k)
        for k in ("used_bytes", "total_bytes", "free_bytes"):
            self.assertTrue(_isnum(r["disk"][k]), k)
        self.assertGreater(r["memory"]["total_bytes"], 0)
        self.assertGreater(r["disk"]["total_bytes"], 0)

    def test_network_counters_and_throughput_are_numeric(self):
        n = self.data2["resources"]["network"]
        self.assertTrue(_isnum(n["bytes_sent"]))
        self.assertTrue(_isnum(n["bytes_recv"]))
        self.assertTrue(_isnum(n["upload_bps"]))       # second sample: a real rate exists
        self.assertTrue(_isnum(n["download_bps"]))
        self.assertGreaterEqual(n["upload_bps"], 0)
        self.assertGreaterEqual(n["download_bps"], 0)

    def test_no_network_percentage_is_invented(self):
        self.assertNotIn("percent", self.data2["resources"]["network"])

    def test_payload_exposes_no_identity_or_paths(self):
        raw = json.dumps(self.data2["resources"])
        for leak in (os.path.expanduser("~"), os.getcwd(), os.environ.get("USER", "\0"),
                     os.environ.get("USERNAME", "\0")):
            if len(leak) > 2:
                self.assertNotIn(leak, raw)
        self.assertIsNone(re.search(r"([0-9a-f]{2}[:-]){5}[0-9a-f]{2}", raw, re.I))  # MAC
        self.assertIsNone(re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", raw))               # IPv4


class TestWithoutPsutil(unittest.TestCase):
    def test_missing_psutil_reports_unavailable_and_does_not_crash(self):
        hm = HostMetrics(psutil_module=None)
        snap = hm.snapshot()
        self.assertIs(snap["available"], False)
        for k in ("cpu", "memory", "disk", "network"):
            self.assertIsNone(snap[k])

    def test_metrics_endpoint_survives_missing_psutil(self):
        from tests.helpers import LiveServer, make_stack
        with patch.object(host_metrics, "_INSTANCE", HostMetrics(psutil_module=None)):
            srv = LiveServer(stack=make_stack())
            with urllib.request.urlopen(srv.base + "/api/metrics", timeout=10) as r:
                data = json.loads(r.read())["data"]
        self.assertIs(data["resources"]["available"], False)
        self.assertIn("requests", data)

    def test_snapshot_never_raises(self):
        with patch.object(host_metrics, "get_host_metrics", side_effect=RuntimeError("boom")):
            self.assertIs(resources_snapshot()["available"], False)

    def test_a_platform_that_refuses_counters_degrades_per_section(self):
        class Denied:
            def cpu_percent(self, interval=None): raise PermissionError
            def virtual_memory(self): raise PermissionError
            def disk_usage(self, p): raise PermissionError
            def net_io_counters(self): raise PermissionError
        snap = HostMetrics(psutil_module=Denied()).snapshot()
        self.assertIs(snap["available"], False)


class TestSampling(unittest.TestCase):
    def test_disk_candidates_are_platform_appropriate(self):
        c = host_metrics._disk_candidates()
        self.assertTrue(c)
        if os.name == "nt":
            self.assertNotEqual(c[0], "/")
        else:
            self.assertEqual(c[0], os.sep)

    def test_percent_is_clamped(self):
        self.assertEqual(host_metrics._pct(250), 100.0)
        self.assertEqual(host_metrics._pct(-3), 0.0)
        self.assertIsNone(host_metrics._pct(float("nan")))
        self.assertIsNone(host_metrics._pct(None))

    def test_network_rate_is_computed_between_samples_only(self):
        class C:
            def __init__(s, a, b): s.bytes_sent, s.bytes_recv = a, b
        seq = iter([C(0, 0), C(1000, 4000)])

        class PS:
            def cpu_percent(self, interval=None): return 5.0
            def net_io_counters(self): return next(seq)
        hm = HostMetrics(psutil_module=PS())          # priming consumes sample #1
        with patch.object(host_metrics.time, "monotonic", side_effect=[102.0]):
            hm._net_prev = (100.0, 0, 0)
            n = hm._network()
        self.assertEqual((n["upload_bps"], n["download_bps"]), (500, 2000))


class TestLiveEndpoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.helpers import LiveServer, make_stack
        cls.srv = LiveServer(stack=make_stack())

    def _get(self, path="/api/system-resources"):
        with urllib.request.urlopen(self.srv.base + path, timeout=10) as r:
            return r.status, dict(r.headers), json.loads(r.read())

    def test_endpoint_returns_real_host_telemetry(self):
        import time
        self._get(); time.sleep(0.3)
        st, hdr, body = self._get()
        self.assertEqual(st, 200)
        d = body["data"]
        self.assertIs(body["ok"], True)
        self.assertIs(d["available"], True)
        self.assertTrue(_isnum(d["sampled_at"]))
        for sec in ("cpu", "memory", "disk"):
            self.assertTrue(0 <= d[sec]["percent"] <= 100, sec)
        self.assertTrue(_isnum(d["network"]["download_bps"]))

    def test_response_is_never_cached(self):
        _, hdr, _ = self._get()
        h = {k.lower(): v for k, v in hdr.items()}
        self.assertIn("no-store", h.get("cache-control", ""))

    def test_endpoint_is_v1_aliased_and_method_checked(self):
        self.assertEqual(self._get("/api/v1/system-resources")[0], 200)
        req = urllib.request.Request(self.srv.base + "/api/system-resources", data=b"{}", method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(cm.exception.code, 405)

    def test_endpoint_only_returns_host_telemetry(self):
        _, _, body = self._get()
        self.assertEqual(set(body["data"]), {"available", "cpu", "memory", "disk", "network", "sampled_at"})

    def test_polling_does_not_inflate_request_counters(self):
        def count():
            with urllib.request.urlopen(self.srv.base + "/api/metrics", timeout=10) as r:
                return json.loads(r.read())["data"]["requests"]["count"]
        before = count()
        for _ in range(25):
            self._get()
        self.assertEqual(count() - before, 1)        # only the /api/metrics call itself

    def test_endpoint_has_its_own_rate_bucket(self):
        # 60 rapid polls (30s at 500ms) must not trip the shared 300/min budget
        for _ in range(60):
            self.assertEqual(self._get()[0], 200)
        self.assertEqual(self._get("/api/health")[0], 200)

    def test_metrics_aggregate_still_carries_resources(self):
        _, _, body = self._get("/api/metrics")
        self.assertIn("resources", body["data"])


class TestLiveSamplingMath(unittest.TestCase):
    class C:
        def __init__(self, sent, recv): self.bytes_sent, self.bytes_recv = sent, recv

    def _hm(self, counters, cpu=None):
        seq = iter(counters)
        calls = {"cpu": 0, "cpu_intervals": []}

        class PS:
            def cpu_percent(inner, interval=None):
                calls["cpu"] += 1
                calls["cpu_intervals"].append(interval)
                return (cpu or [10.0] * 100)[min(calls["cpu"] - 1, 99)]
            def net_io_counters(inner): return next(seq)
            def virtual_memory(inner):
                class V: percent, total, available = 40.0, 1000, 600
                return V
            def disk_usage(inner, p):
                class D: percent, used, free, total = 30.0, 30, 70, 100
                return D
        return HostMetrics(psutil_module=PS()), calls

    def test_rate_uses_real_elapsed_time_not_an_assumed_second(self):
        hm, _ = self._hm([self.C(0, 0), self.C(3000, 9000)])
        with patch.object(host_metrics.time, "monotonic", side_effect=[10.75]):
            hm._net_prev = (10.0, 0, 0)
            n = hm._network()
        self.assertEqual(n["upload_bps"], 4000)        # 3000 B / 0.75 s
        self.assertEqual(n["download_bps"], 12000)     # 9000 B / 0.75 s

    def test_irregular_intervals_give_accurate_rates(self):
        hm, _ = self._hm([self.C(0, 0), self.C(1000, 1000), self.C(1000, 1000), self.C(2000, 3000)])
        t = {"v": 0.0}
        with patch.object(host_metrics.time, "monotonic", side_effect=lambda: t["v"]):
            hm._net_prev = (0.0, 0, 0)
            t["v"] = 0.5; a = hm._network()
            t["v"] = 2.5; b = hm._network()            # idle for 2s: a measured, genuine zero
            t["v"] = 3.0; c = hm._network()
        self.assertEqual((a["upload_bps"], a["download_bps"]), (2000, 2000))
        self.assertEqual((b["upload_bps"], b["download_bps"]), (0, 0))
        self.assertEqual((c["upload_bps"], c["download_bps"]), (2000, 4000))

    def test_first_sample_has_no_rate_and_is_not_faked_as_zero(self):
        class PS:
            def cpu_percent(inner, interval=None): return 1.0
            def net_io_counters(inner): raise PermissionError       # cannot prime
        hm = HostMetrics(psutil_module=PS())
        self.assertIsNone(hm._net_prev)
        hm2, _ = self._hm([self.C(500, 500), self.C(500, 500)])
        hm2._net_prev = None
        first = hm2._network()
        self.assertIsNone(first["upload_bps"])
        self.assertIsNone(first["download_bps"])
        self.assertEqual(first["bytes_sent"], 500)

    def test_counter_reset_rebaselines_without_a_negative_or_fake_rate(self):
        hm, _ = self._hm([self.C(0, 0), self.C(10, 10)])
        with patch.object(host_metrics.time, "monotonic", side_effect=[5.0]):
            hm._net_prev = (1.0, 10_000, 10_000)                     # counters went backwards
            n = hm._network()
        self.assertIsNone(n["upload_bps"])
        self.assertIsNone(n["download_bps"])
        self.assertEqual(hm._net_prev[1], 10)                        # new baseline adopted

    def test_stale_baseline_is_discarded_instead_of_averaged(self):
        hm, _ = self._hm([self.C(0, 0), self.C(10**6, 10**6)])
        with patch.object(host_metrics.time, "monotonic", side_effect=[1000.0]):
            hm._net_prev = (0.0, 0, 0)                               # 1000s old
            n = hm._network()
        self.assertIsNone(n["download_bps"])

    def test_requests_arriving_faster_than_the_window_reuse_the_last_real_rate(self):
        hm, _ = self._hm([self.C(0, 0), self.C(2000, 2000), self.C(2100, 2100)])
        t = {"v": 0.0}
        with patch.object(host_metrics.time, "monotonic", side_effect=lambda: t["v"]):
            hm._net_prev = (0.0, 0, 0)
            t["v"] = 1.0; a = hm._network()
            t["v"] = 1.05; b = hm._network()
        self.assertEqual(a["download_bps"], 2000)
        self.assertEqual(b["download_bps"], 2000)                    # no divide-by-tiny-dt spike

    def test_loopback_traffic_is_excluded_from_network_counters(self):
        C = self.C

        class PS:
            def cpu_percent(inner, interval=None): return 1.0
            def net_io_counters(inner, pernic=False):
                if not pernic: raise AssertionError("aggregate counter must not be used when per-NIC works")
                return {"lo": C(10**9, 10**9), "lo0": C(5, 5), "Loopback Pseudo-Interface 1": C(7, 7),
                        "eth0": C(100, 200), "Wi-Fi": C(50, 60)}
        hm = HostMetrics(psutil_module=PS())
        _, sent, recv = hm._net_sample()
        self.assertEqual((sent, recv), (150, 260))
        self.assertTrue(host_metrics._is_loopback("lo"))
        self.assertFalse(host_metrics._is_loopback("eth0"))
        self.assertFalse(host_metrics._is_loopback("Local Area Connection"))

    def test_falls_back_to_aggregate_when_per_interface_data_is_unavailable(self):
        C = self.C

        class PS:
            def cpu_percent(inner, interval=None): return 1.0
            def net_io_counters(inner, pernic=False):
                if pernic: raise PermissionError
                return C(42, 43)
        _, sent, recv = HostMetrics(psutil_module=PS())._net_sample()
        self.assertEqual((sent, recv), (42, 43))

    def test_cpu_sampling_is_non_blocking(self):
        hm, calls = self._hm([self.C(0, 0)] * 10)
        hm.snapshot(); hm.snapshot()
        self.assertTrue(calls["cpu_intervals"], "cpu_percent was never called")
        self.assertTrue(all(i is None for i in calls["cpu_intervals"]), calls["cpu_intervals"])

    def test_cpu_calls_closer_than_the_window_reuse_the_last_reading(self):
        hm, calls = self._hm([self.C(0, 0)] * 10, cpu=[0.0, 33.0, 77.0])
        t = {"v": 0.0}
        with patch.object(host_metrics.time, "monotonic", side_effect=lambda: t["v"]):
            t["v"] = 1.0; a = hm._cpu()
            t["v"] = 1.02; b = hm._cpu()                             # < 0.1s later
            t["v"] = 2.0; c = hm._cpu()
        self.assertEqual(a["percent"], 33.0)
        self.assertEqual(b["percent"], 33.0)
        self.assertEqual(c["percent"], 77.0)
        self.assertEqual(calls["cpu"], 3)                            # priming + two real reads

    def test_memory_and_disk_come_straight_from_psutil(self):
        hm, _ = self._hm([self.C(0, 0)] * 4)
        snap = hm.snapshot()
        self.assertEqual(snap["memory"], {"percent": 40.0, "used_bytes": 400,
                                          "total_bytes": 1000, "available_bytes": 600})
        self.assertEqual(snap["disk"]["percent"], 30.0)
        self.assertEqual(snap["disk"]["free_bytes"], 70)

    def test_psutil_failure_mid_flight_degrades_and_recovers(self):
        state = {"fail": False, "n": 0}

        class PS:
            def cpu_percent(inner, interval=None): return 5.0
            def virtual_memory(inner):
                if state["fail"]: raise OSError("boom")
                class V: percent, total, available = 20.0, 10, 8
                return V
            def disk_usage(inner, p): raise OSError("boom")
            def net_io_counters(inner):
                if state["fail"]: raise OSError("boom")
                state["n"] += 1
                return TestLiveSamplingMath.C(state["n"], state["n"])
        hm = HostMetrics(psutil_module=PS())
        ok = hm.snapshot()
        self.assertIsNotNone(ok["memory"]); self.assertIsNone(ok["disk"]); self.assertTrue(ok["available"])
        state["fail"] = True
        bad = hm.snapshot()
        self.assertIsNone(bad["memory"]); self.assertIsNone(bad["network"])
        state["fail"] = False
        self.assertIsNotNone(hm.snapshot()["memory"])


class TestNoMockValues(unittest.TestCase):
    def test_no_random_or_reference_values_in_production_code(self):
        def code(path):
            s = _read(*path)
            s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
            s = re.sub(r"(?m)^\s*#.*$|(?<![:\"'])//[^\n]*", "", s)
            return re.sub(r'""".*?"""', "", s, flags=re.S)
        for path in (("astra", "host_metrics.py"), ("static", "js", "system_health.js")):
            src = code(path)
            self.assertNotIn("random", src.lower(), path)
            self.assertNotIn("Not reported by the API", src, path)
        js = code(("static", "js", "system_health.js"))
        self.assertNotIn("Math.random", js)
        py = code(("astra", "host_metrics.py"))
        for fake in ("28.0", "42.0", "36.0", "18.0"):
            self.assertNotIn(fake, py)

    def test_requirements_declare_psutil_consistently(self):
        req = _read("requirements.txt")
        pp = _read("pyproject.toml")
        self.assertRegex(req, r"(?m)^psutil>=5\.9")
        self.assertIn("psutil>=5.9", pp)


if __name__ == "__main__":
    unittest.main()
