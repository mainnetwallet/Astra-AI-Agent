"""Per-(provider, API key, model) health: which key served a model, whether
it worked, saved (and restored after a restart), and used to steer live calls
away from keys / models already known to fail."""
from __future__ import annotations

import unittest
from unittest import mock

from astra.ai.adapters.base import CompatibleAdapter
from astra.ai.adapters.groq import GroqAdapter
from astra.ai.credentials import CredentialPool
from astra.ai.router import AstraRouter, RoutingRequest
from astra.core.config import Config
from astra.core.exceptions import ProviderError
from astra.store import Store


def _adapter(keys="good,bad", models="m1,m2", name_cls=GroqAdapter):
    cfg = Config()
    cfg._runtime.update(GROQ_API_KEYS=keys, GROQ_MODELS=models)
    return name_cls(config=cfg)


def _fake_post(bad_secrets=(), bad_models=()):
    """A `_post` that fails (like a real 403) for chosen keys/models."""
    calls = []

    def post(self, url, body, cred):
        secret = self.pool.get_secret_for(cred)
        calls.append((secret, body["model"]))
        if secret in bad_secrets:
            self._done(cred, True, reason="http 403", auth_failure=True)
            raise ProviderError(f"{self.name} authorization denied")
        if body["model"] in bad_models:
            self._done(cred, True, reason="http 404")
            raise ProviderError(f"{self.name} model not found")
        return {"choices": [{"message": {"content": "pong"}}]}
    post.calls = calls
    return post


def _router(adapter, store=None):
    return AstraRouter([adapter], max_retries=2, backoff_s=0.0, store=store)


def _key_ids(adapter):
    return {k["label"]: k["key_id"] for k in adapter.pool.keys()}


class KeyIdentityTests(unittest.TestCase):
    def test_key_id_is_stable_and_not_the_secret(self):
        a = CredentialPool("groq", ["sk-secret-one", "sk-secret-two"])
        b = CredentialPool("groq", ["sk-secret-one"])
        ids = [k["key_id"] for k in a.keys()]
        self.assertEqual(len(set(ids)), 2)
        self.assertEqual(ids[0], b.keys()[0]["key_id"])      # stable across restarts
        blob = repr(a.keys()) + repr(a._creds[0].to_metadata())
        self.assertNotIn("sk-secret", blob)
        self.assertEqual([k["label"] for k in a.keys()], ["key 1", "key 2"])

    def test_pin_forces_that_key_even_if_unhealthy(self):
        p = CredentialPool("groq", ["k1", "k2"])
        kid = p.keys()[0]["key_id"]
        p._creds[0].healthy = False
        with p.pinned(kid):
            self.assertEqual(p.get_secret_for(p.pick()), "k1")
            p.report_success(p.last_key())
        self.assertTrue(p._creds[0].healthy)                # re-validated
        self.assertIsNone(p.pinned_key())

    def test_pick_skips_key_known_bad_for_model_but_keeps_last_resort(self):
        p = CredentialPool("groq", ["k1", "k2"])
        bad = p.keys()[0]["key_id"]
        p.model_status = lambda key_id, model: False if (key_id == bad and model == "m") else None
        picks = {p.get_secret_for(p.pick("m")) for _ in range(6)}
        self.assertEqual(picks, {"k2"})
        p.model_status = lambda key_id, model: False              # all bad -> still tries
        self.assertIsNotNone(p.pick("m"))


class PerKeyTestingTests(unittest.TestCase):
    def test_test_through_a_specific_key_is_saved_per_key(self):
        ad = _adapter()
        r = _router(ad)
        ids = _key_ids(ad)
        post = _fake_post(bad_secrets=("bad",))
        with mock.patch.object(CompatibleAdapter, "_post", post):
            good = r.test_provider_model("groq", "m1", ids["key 1"])
            bad = r.test_provider_model("groq", "m1", ids["key 2"])
        self.assertTrue(good["ok"])
        self.assertEqual((good["key"], good["key_id"]), ("key 1", ids["key 1"]))
        self.assertFalse(bad["ok"])
        self.assertIn("authorization denied", bad["error"])
        self.assertEqual([c[0] for c in post.calls], ["good", "bad"])   # 1 attempt each, no rotation
        saved = r.key_health("groq")["m1"]
        self.assertTrue(saved[ids["key 1"]]["ok"])
        self.assertFalse(saved[ids["key 2"]]["ok"])
        self.assertEqual(saved[ids["key 2"]]["source"], "test")
        self.assertNotIn("bad", repr(r.health()))            # secrets never surface

    def test_pinned_test_revives_key_and_unknown_key_is_rejected(self):
        ad = _adapter(keys="only")
        r = _router(ad)
        kid = ad.pool.keys()[0]["key_id"]
        ad.pool._creds[0].healthy = False                    # e.g. an old transient 401
        with mock.patch.object(CompatibleAdapter, "_post", _fake_post()):
            res = r.test_provider_model("groq", "m1", kid)
        self.assertTrue(res["ok"])
        self.assertTrue(ad.pool.keys()[0]["healthy"])
        self.assertFalse(r.test_provider_model("groq", "m1", "deadbeef")["ok"])

    def test_results_survive_restart(self):
        store = Store(":memory:")
        ad = _adapter()
        r = _router(ad, store)
        ids = _key_ids(ad)
        with mock.patch.object(CompatibleAdapter, "_post", _fake_post(bad_secrets=("bad",))):
            r.test_provider_model("groq", "m1", ids["key 1"])
            r.test_provider_model("groq", "m1", ids["key 2"])
        r2 = _router(_adapter(), store)                      # new process, same DB
        saved = r2.key_health("groq")["m1"]
        self.assertTrue(saved[ids["key 1"]]["ok"])
        self.assertFalse(saved[ids["key 2"]]["ok"])
        self.assertEqual(r2.health()["groq"]["key_results"]["m1"], saved)


class LiveCallRoutingTests(unittest.TestCase):
    def _chat(self, r, model):
        return r.route_request(RoutingRequest(
            task_type="simple_chat", messages=[{"role": "user", "content": "hi"}],
            preferred_provider="groq", preferred_model=model, no_fallback=True))

    def test_live_calls_record_which_key_served_them(self):
        ad = _adapter(keys="good")
        r = _router(ad)
        with mock.patch.object(CompatibleAdapter, "_post", _fake_post()):
            self.assertTrue(self._chat(r, "m1").ok)
        row = r.key_health("groq")["m1"][ad.pool.keys()[0]["key_id"]]
        self.assertTrue(row["ok"])
        self.assertEqual(row["source"], "live")

    def test_live_call_avoids_key_known_bad_for_that_model(self):
        ad = _adapter(keys="good,bad")
        r = _router(ad)
        ids = _key_ids(ad)
        post = _fake_post(bad_secrets=("bad",))
        with mock.patch.object(CompatibleAdapter, "_post", post):
            r.test_provider_model("groq", "m1", ids["key 2"])     # learn: bad key fails m1
            ad.pool._creds[1].healthy = True                      # even if it looks healthy
            ad.pool._creds[1].cooldown_until = 0.0
            post.calls.clear()
            for _ in range(6):
                self.assertTrue(self._chat(r, "m1").ok)
        self.assertEqual({c[0] for c in post.calls}, {"good"})

    def test_provider_with_all_keys_failing_a_model_ranks_after_healthy_ones(self):
        a = _adapter(keys="k1")
        a.name = "aaa"
        b = _adapter(keys="k2")
        b.name = "bbb"
        r = AstraRouter([a, b], max_retries=0, backoff_s=0.0)
        req = RoutingRequest(task_type="simple_chat",
                             messages=[{"role": "user", "content": "hi"}])
        first = lambda: r.route_request(req).provider
        with mock.patch.object(CompatibleAdapter, "_post", _fake_post()):
            before = first()
        loser = before
        loser_ad = a if before == "aaa" else b
        r._key_model[(loser, loser_ad.pool.keys()[0]["key_id"], "m1")] = {
            "ok": False, "latency_ms": 0, "error": "x", "source": "test",
            "tested_at": "", "ts": __import__("time").time(), "key_label": "key 1"}
        r._key_model[(loser, loser_ad.pool.keys()[0]["key_id"], "m2")] = dict(
            r._key_model[(loser, loser_ad.pool.keys()[0]["key_id"], "m1")])
        with mock.patch.object(CompatibleAdapter, "_post", _fake_post()):
            after = first()
        self.assertNotEqual(after, loser)


if __name__ == "__main__":
    unittest.main()
