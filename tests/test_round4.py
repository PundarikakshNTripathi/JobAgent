"""Browser people search (company aliases, employer matching), the provider-agnostic browser loop's safety rules, per-job notes for rebuilds,
the provider-specific cache, and the outreach signals parsers."""
import json, unittest
from unittest import mock
import _env  # noqa: F401  (isolated workspace)
from _env import add_job
import common, browse, webagent, signals, llm


class CompanyNames(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(browse.company_aliases("Google / Google DeepMind"), ["Google", "Google DeepMind"])
        self.assertEqual(browse.company_aliases("Hook (YC S21)"), ["Hook"])
        self.assertEqual(browse.company_aliases("AT&T"), ["AT&T"])
        self.assertEqual(browse.company_aliases("Stripe"), ["Stripe"])

    def test_current_employer_only(self):
        m = lambda h, cur="", co="Google DeepMind": browse._company_match({"headline": h, "current": cur}, co)
        self.assertTrue(m("Research Scientist, Google DeepMind"))
        self.assertTrue(m("Student Researcher @ Google DeepMind | PhD"))
        self.assertTrue(m("Engineer", cur="Google DeepMind"))
        self.assertTrue(m("Google DeepMind | Gemini"))
        self.assertFalse(m("ex-Google DeepMind, now at OpenAI"))
        self.assertFalse(m("Former Google DeepMind researcher"))
        self.assertFalse(m("Built Google DeepMind-style evals"))

    def test_compound_name_matches_either_part(self):
        self.assertTrue(browse._company_match({"headline": "SWE at Google", "current": ""}, "Google / Google DeepMind"))


class BrowserLoopSafety(unittest.TestCase):
    def test_read_mode(self):
        link = {"tag": "a", "type": "", "label": "People"}
        btn = {"tag": "button", "type": "", "label": "Connect"}
        self.assertIsNone(webagent._allowed("read", "click", link))
        self.assertIsNotNone(webagent._allowed("read", "click", btn))
        self.assertIsNotNone(webagent._allowed("read", "type", {"tag": "input", "type": "text", "label": "Search"}))
        self.assertIsNotNone(webagent._allowed("read", "click", {"tag": "a", "type": "", "label": "Message"}))

    def test_form_mode_never_submits(self):
        self.assertIsNotNone(webagent._allowed("form", "click", {"tag": "button", "type": "submit", "label": "Submit application"}))
        self.assertIsNone(webagent._allowed("form", "click", {"tag": "a", "type": "", "label": "Apply now"}))  # opens the form
        self.assertIsNone(webagent._allowed("form", "type", {"tag": "input", "type": "text", "label": "First name"}))

    def test_save_mode(self):
        self.assertIsNone(webagent._allowed("save", "click", {"tag": "button", "type": "", "label": "Save changes"}))
        self.assertIsNotNone(webagent._allowed("save", "click", {"tag": "button", "type": "", "label": "Apply to Acme"}))
        self.assertIsNotNone(webagent._allowed("save", "click", {"tag": "button", "type": "", "label": "Send message"}))

    def test_element_list_is_compact_and_numbered(self):
        out = webagent._fmt([{"i": 3, "tag": "select", "type": "", "label": "Country", "value": "India", "options": ["India", "USA"], "required": True}])
        self.assertIn('[3] select "Country" = "India" options: India | USA (required)', out)


class JobNotes(unittest.TestCase):
    def test_notes_reach_the_prompt(self):
        add_job("t:note1", "Acme", "ML Engineer")
        self.assertEqual(common.job_context("t:note1", "packet"), "")
        common.add_job_note("t:note1", "Lead with the CUDA kernel work.")
        ctx = common.job_context("t:note1", "referral")
        self.assertIn("Lead with the CUDA kernel work.", ctx)
        self.assertIn("never justify inventing", ctx)
        common.add_job_note("t:note1", "   ")
        self.assertEqual(len(common.job_notes("t:note1")), 1)


class ProviderCache(unittest.TestCase):
    def test_explicit_provider_never_reads_another_providers_answer(self):
        common.SETTINGS["llm"].setdefault("cache_hours", {})["unit-test"] = 24
        common.SETTINGS["llm"]["routing"]["unit-test"] = {"provider": "local", "model": "local", "effort": ""}
        llm.cache_put("unit-test", {"provider": "local", "model": "local"}, "P", "LOCAL ANSWER")
        calls = []

        def fake(name, route, prompt, o):
            calls.append(name)
            return "AGY ANSWER", {"provider": name, "usage": {}}
        with mock.patch.object(llm, "call_provider", side_effect=fake), mock.patch.object(llm, "available", return_value=(True, "")):
            out, meta = llm.run("P", kind="unit-test", provider="agy")
        self.assertEqual(out, "AGY ANSWER")
        self.assertEqual(calls, ["agy"])


class SignalParsers(unittest.TestCase):
    def test_emails(self):
        self.assertEqual(signals.emails_in("reach me: jane.doe@acme.ai or bob [at] startup [dot] io; logo@2x.png noreply@x.com"),
                         ["bob@startup.io", "jane.doe@acme.ai"])

    def test_prose_is_not_an_email(self):
        self.assertEqual(signals.emails_in("We make training@scale.We don't stop"), [])
        self.assertEqual(signals.emails_in("write to x@y.co.uk"), ["x@y.co.uk"])

    def test_senior_posts_are_capped(self):
        c = signals._table()
        signals._put(c, "sen1", "post", "x:search", "u1", "A", "", "", "", "", "Senior ML Engineer", "We're hiring a Senior ML engineer, 5+ years", common.now(), 2)
        signals._put(c, "int1", "post", "x:search", "u2", "B", "", "", "", "", "ML Intern", "We're hiring ML interns for summer 2027", common.now(), 2)
        reply = json.dumps([{"id": "sen1", "relevance": 95, "company": "Unknown", "role": "Senior ML Engineer", "person": "A", "why": "x"},
                            {"id": "int1", "relevance": 90, "company": "Acme", "role": "ML Intern", "person": "B", "why": "y"}])
        with mock.patch("llm.run", return_value=(reply, {})):
            signals.judge(c)
        got = {r["id"]: (r["relevance"], r["company"]) for r in c.execute("SELECT id, relevance, company FROM signals WHERE id IN ('sen1','int1')")}
        self.assertEqual(got["sen1"], (35.0, ""))
        self.assertEqual(got["int1"], (90.0, "Acme"))

    def test_seed_sizes_and_accounts(self):
        self.assertEqual(signals._amount_musd("raises $2.3M seed"), 2.3)
        self.assertEqual(signals._amount_musd("$180M / Series A $1B"), 180.0)
        self.assertEqual(signals._amount_musd("Rs 5 Cr"), 0.6)
        self.assertIsNone(signals._amount_musd("a seed round"))
        self.assertTrue(signals.AGGREGATOR.search("AI Career Girl"))
        self.assertFalse(signals.AGGREGATOR.search("Vineeth Rao"))
        self.assertTrue(signals.NOT_A_DOMAIN.search("x.com"))
        self.assertFalse(signals.NOT_A_DOMAIN.search("aarivi.com"))
        self.assertTrue(signals.FIRST_PERSON.search("Today we raised our seed round"))
        self.assertFalse(signals.FIRST_PERSON.search("Bharat Semi raises $24M seed"))

    def test_one_target_per_person_and_companyless_posts_survive(self):
        c = signals._table()
        c.execute("DELETE FROM signals")
        for i in range(3):
            signals._put(c, f"p{i}", "post", "x:search", f"v{i}", "Ana Ruiz", "https://x.com/ana", "ana", "", "", "Founding Engineer", "We're hiring a founding engineer", common.now(), 1)
        signals._put(c, "q1", "post", "x:search", "w1", "Ben Ode", "https://x.com/ben", "ben", "", "", "ML Intern", "We're hiring ML interns", common.now(), 1)
        c.execute("UPDATE signals SET relevance=80")
        c.commit()
        with mock.patch.object(signals, "find_contact", return_value={"emails": []}), mock.patch.object(signals, "research_target", return_value={}), mock.patch.object(signals, "write_angle", return_value={}):
            n = signals.build_queue(c, dict(signals.cfg(), daily_target=20))
        self.assertEqual(n, 2)  # Ana once, Ben once

    def test_company_from_news(self):
        g = lambda t: signals.guess_company({"text": t})
        self.assertEqual(g("Athena has raised $2.5M in pre-seed funding led by a16z"), "Athena")
        self.assertEqual(g("Czech startup ValkaAI extends pre-Seed round to €13 million"), "ValkaAI")
        self.assertEqual(g("Here's the roundup: - Rhode Island's Project Omega raised $80M"), "Project Omega")
        self.assertEqual(g("An Indian chip startup just raised $24M"), "")

    def test_one_target_per_company(self):
        c = signals._table()
        c.execute("DELETE FROM signals")
        for i in range(3):
            signals._put(c, f"d{i}", "seed", "x:seed", f"u{i}", f"News {i}", "", f"n{i}", "", "Samecorp", "seed round $2M", "Samecorp raises $2M seed", common.now(), 1)
        c.execute("UPDATE signals SET relevance=80")
        c.commit()
        with mock.patch.object(signals, "find_contact", return_value={"emails": []}), mock.patch.object(signals, "seed_people", return_value=([], "")), \
                mock.patch.object(signals, "research_target", return_value={}), mock.patch.object(signals, "write_angle", return_value={"hook": "h"}):
            n = signals.build_queue(c, dict(signals.cfg(), daily_target=20))
        self.assertEqual(n, 1)
        q = c.execute("SELECT author FROM signals WHERE status='queued'").fetchone()
        self.assertEqual(q["author"], "")  # reposted news: the target is the founders, not the account that reposted it
        self.assertEqual(c.execute("SELECT COUNT(*) n FROM signals WHERE status='duplicate'").fetchone()["n"], 2)

    def test_digest_lines(self):
        t = "@kdqg1 is hiring Research Scientists for @AnthropicAI @juliaaneagu is hiring a Researcher (agents) for @databricks"
        self.assertEqual(signals.AGG_LINE.findall(t), [("kdqg1", "Research Scientists", "AnthropicAI"), ("juliaaneagu", "a Researcher (agents)", "databricks")])

    def test_counts_and_ages(self):
        self.assertEqual(signals._num("25 Likes. Like"), 25)
        self.assertEqual(signals._num("1.2K Likes"), 1200)
        self.assertEqual(signals._age_label_hours("3h"), 3)
        self.assertEqual(signals._age_label_hours("2d"), 48)
        self.assertIsNone(signals._age_label_hours("Follow"))

    def test_linkedin_post(self):
        p = signals._parse_li_post({"author_url": "https://www.linkedin.com/in/jo", "text": "Feed post\nJo Smith\n• 2nd\nCTO at Acme AI\n3h\nFollow\n"
                                    "We're hiring a founding ML engineer to work on inference. DM me.\n12 reactions"})
        self.assertEqual((p["name"], p["headline"], p["age_h"], p["likes"]), ("Jo Smith", "CTO at Acme AI", 3, 12))
        self.assertIn("founding ML engineer", p["text"])

    def test_queue_marks(self):
        c = signals._table()
        signals._put(c, "s1", "post", "x:search", "https://x.com/a/status/1", "A", "https://x.com/a", "a", "", "Acme", "ML Eng", "hiring", common.now(), 3)
        signals.mark("s1", "queued")
        signals.mark("s1", "sent", "emailed")
        r = c.execute("SELECT status, note, day FROM signals WHERE id='s1'").fetchone()
        self.assertEqual((r["status"], r["note"]), ("sent", "emailed"))
        self.assertTrue(r["day"])
        with self.assertRaises(ValueError):
            signals.mark("s1", "deleted")


if __name__ == "__main__":
    unittest.main()
