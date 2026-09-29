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
