import json, time, unittest
from unittest import mock
import _env  # noqa: F401  (isolated workspace)
import common, jd, chatcmd, browse, referrals, llm, rag, schedule


class ExtractJson(unittest.TestCase):
    def test_array_is_not_its_first_element(self):
        self.assertIsInstance(common.extract_json('[{"a": 1}]'), list)
        self.assertEqual(len(common.extract_json('noise [ {"a":1}, {"b":2} ] tail')), 2)

    def test_object_and_fences(self):
        self.assertEqual(common.extract_json('```json\n{"k": [1, 2]}\n```'), {"k": [1, 2]})
        self.assertEqual(common.extract_json('text {"a": {"b": 2}} end'), {"a": {"b": 2}})


class Requirements(unittest.TestCase):
    def req(self, title, desc):
        return jd.problems(jd.requirements({"title": title, "desc": desc}))

    def test_years(self):
        self.assertIn("needs 3+ years of experience", self.req("Software Engineer", "Basic Qualifications\n- 3+ years of professional software development experience"))
        self.assertEqual(self.req("Software Engineer Intern", "Requirements: pursuing a Bachelor's in CS. Python."), [])

    def test_years_by_degree(self):
        d = "Basic Qualifications - A degree with: 5+ years of applied ML experience (Bachelor's), or 3+ years (Master's), or 1+ year (PhD)"
        self.assertIn("needs 5+ years of experience", self.req("Applied Scientist", d))

    def test_degree_required_vs_mentioned(self):
        self.assertTrue(any("Master" in p for p in self.req("Intern", "What we need to see: Pursuing an MS or PhD in Computer Science.")))
        self.assertEqual(self.req("Student Researcher, BS/MS", "Requirements: The program is open to students at all degree levels, the majority of projects are designed for PhD talent."), [])
        self.assertEqual(self.req("Intern", "Requirements: pursuing a Bachelor's or Master's degree in computer science."), [])
        self.assertEqual(self.req("Predoctoral", "Requirements: BS in CS. Alumni became a PhD student at MIT after completing our program."), [])

    def test_senior_and_citizenship(self):
        self.assertIn("senior-level title", self.req("Senior ML Engineer", "Requirements: Python"))
        self.assertEqual(self.req("Machine Learning Engineer I", "Requirements: Python"), [])
        self.assertIn("requires citizenship or a security clearance", self.req("Engineer", "Requirements: must be a U.S. citizen. Active secret clearance."))
        self.assertNotIn("requires citizenship or a security clearance", self.req("Engineer", "Requirements: Python. Military careers. Veterans encouraged."))

    def test_grad_window(self):
        self.assertTrue(any("graduates" in p for p in self.req("Fellow", "Eligibility: graduating by summer 2026.")))
        self.assertEqual(self.req("Intern", "Eligibility: you will graduate in fall 2027 or spring 2028 with a Bachelor's degree"), [])


class SlashCommands(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _env.add_job("t:1", "Acme", "Applied Science Intern", "x" * 900)
        _env.add_job("t:2", "Acme", "Software Engineer Intern", "x" * 900)
        _env.add_job("t:3", "Zeta Labs", "Kernel Engineer", "x" * 900)

    def test_help_and_unknown(self):
        self.assertIn("/linkedin", chatcmd.run_slash("/help"))
        self.assertIn("Unknown command", chatcmd.run_slash("/nonsense"))
        self.assertIn("Scout LinkedIn", chatcmd.run_slash("/help linkedin"))

    def test_resolve(self):
        self.assertEqual(chatcmd.resolve("t:3")[0]["id"], "t:3")
        self.assertEqual(chatcmd.resolve("zeta kernel")[0]["id"], "t:3")
        job, msg = chatcmd.resolve("acme")
        self.assertIsNone(job)
        self.assertIn("several jobs", msg)

    def test_mark_needs_status_word(self):
        self.assertIn("Usage", chatcmd.run_slash("/mark zeta"))

    def test_nl_policy(self):
        done, props = chatcmd.execute_nl_actions("what do you think of acme", [("mark", "t:1 skip"), ("fill", "t:1"), ("agent", "x")])
        self.assertEqual(done, [])
        self.assertEqual([p["kind"] for p in props], ["mark", "fill", "agent"])


class LinkedInParsing(unittest.TestCase):
    def test_card(self):
        p = browse._parse_card({"url": "https://www.linkedin.com/in/x/", "text": "Jane Doe\n• 2nd\nEngineer @ Stripe | ex-Google\nBengaluru\nConnect\nCurrent: Software Engineer at Stripe\nA, B & 3 other mutual connections"})
        self.assertEqual((p["name"], p["degree"]), ("Jane Doe", "2nd"))
        self.assertTrue(browse._company_match(p, "Stripe"))

    def test_employer_only(self):
        no = {"headline": "AI Lead @ UBS", "current": "Co-Founder ... implemented Stripe-based payments"}
        ex = {"headline": "Engineer | ex-Stripe", "current": ""}
        self.assertFalse(browse._company_match(no, "Stripe"))
        self.assertFalse(browse._company_match(ex, "Stripe"))
        self.assertTrue(browse._company_match({"headline": "Research @ Together AI", "current": ""}, "Together AI"))


class ReferralVerification(unittest.TestCase):
    def test_drops_unreachable_and_scrubs(self):
        r = {"targets": [{"name": "A Person", "profile_url": "https://www.linkedin.com/in/a/", "evidence": "builds X (UNVERIFIED)"},
                         {"name": "No Url", "profile_url": ""}, {"name": "UNVERIFIED", "profile_url": "https://x"}], "timing": "ask after applying (UNVERIFIED)"}
        out = referrals._verify({"company": "Acme"}, r, "linkedin")
        self.assertEqual([t["name"] for t in out["targets"]], ["A Person"])
        self.assertNotIn("UNVERIFIED", json.dumps(out).upper())


class ProviderPolicy(unittest.TestCase):
    def setUp(self):
        common.SETTINGS["llm"]["routing"]["packet"] = {"provider": "claude", "model": "m", "effort": "high"}
        common.SETTINGS["llm"]["routing"]["rank"] = {"provider": "claude", "model": "m", "effort": "low"}
        common.SETTINGS["llm"]["fallbacks"] = ["claude", "agy"]
        common.SETTINGS["llm"]["defer_in_background"] = ["packet"]
        common.SETTINGS["llm"]["cache_hours"] = {"rank": 1}

    def tearDown(self):
        common.BACKGROUND = False

    def test_background_quality_work_defers(self):
        calls = []
        with mock.patch.object(llm, "available", lambda n: (n != "claude", "quota")), mock.patch.object(llm, "call_provider", lambda n, *a: calls.append(n) or ("x", {})):
            common.BACKGROUND = True
            with self.assertRaises(llm.LLMError) as e:
                llm.run("p", kind="packet")
            self.assertIn("deferred", str(e.exception))
            self.assertEqual(calls, [])
            common.BACKGROUND = False
            self.assertEqual(llm.run("p", kind="packet")[0], "x")  # interactive work still falls back
            self.assertEqual(calls, ["agy"])

    def test_cache(self):
        n = []
        with mock.patch.object(llm, "available", lambda n_: (True, "")), mock.patch.object(llm, "call_provider", lambda *a: n.append(1) or ("answer", {})):
            self.assertEqual(llm.run("same prompt", kind="rank")[0], "answer")
            self.assertEqual(llm.run("same prompt", kind="rank")[0], "answer")
        self.assertEqual(len(n), 1)


class Retrieval(unittest.TestCase):
    def test_index_and_query(self):
        _env.add_job("r:1", "Inferix", "Inference Kernel Intern", "CUDA kernels and KV cache work. " * 40, status="triaged")
        rag.refresh()
        hits = rag.retrieve("kv cache kernels", 5, kinds=["job"])
        self.assertTrue(hits and hits[0]["ref"] == "job:r:1")
        self.assertEqual(rag.refresh(), 0)  # incremental: nothing changed


class Dedupe(unittest.TestCase):
    def test_keeps_direct_and_different_text(self):
        import pipeline
        _env.add_job("greenhouse:dup:1", "DupCo", "ML Engineer", "Same text " * 50, location="Bengaluru")
        _env.add_job("speedyapply-ai:dup", "DupCo", "ML Engineer", "aggregator copy", location="Bengaluru")
        _env.add_job("greenhouse:dup:2", "DupCo", "ML Engineer", "Same text " * 50, location="Bengaluru")
        _env.add_job("greenhouse:dup:3", "DupCo", "ML Engineer", "A different team entirely " * 30, location="Bengaluru")
        pipeline.dedupe()
        act = {r[0]: r[1] for r in common.db().execute("SELECT id, active FROM jobs WHERE company='DupCo'")}
        self.assertEqual(act["speedyapply-ai:dup"], 0)
        self.assertEqual(act["greenhouse:dup:3"], 1)
        self.assertEqual(act["greenhouse:dup:1"] + act["greenhouse:dup:2"], 1)


class Schedule(unittest.TestCase):
    def test_times_validation(self):
        common.SETTINGS["schedule_times"] = ["09:00", "25:00", "18:30", "bad"]
        self.assertEqual(schedule.times(), ["09:00", "18:30"])

    def test_python_prefers_venv_or_current(self):
        self.assertTrue(schedule.python())


if __name__ == "__main__":
    unittest.main()
