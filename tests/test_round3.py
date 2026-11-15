"""Posting dates, logos, grounding, the small-model agent harness, chat threads, stale plans and the desktop app."""
import datetime as dt, json, os, tempfile, unittest
from unittest import mock
import _env  # noqa: F401  (isolated workspace)
from _env import add_job
import common, logos, grounding, agentic, inbox, maintenance, desktop


class PostingDates(unittest.TestCase):
    REF = dt.date(2026, 11, 16)

    def d(self, s):
        return common.approx_date(s, ref=dt.datetime(2026, 11, 16))

    def test_ages(self):
        self.assertEqual(self.d("2d"), "2026-11-14")
        self.assertEqual(self.d("3w"), "2026-10-26")
        self.assertEqual(self.d("19 days"), "2026-10-28")
        self.assertEqual(self.d("yesterday"), "2026-11-15")
        self.assertEqual(self.d("today"), "2026-11-16")

    def test_unreadable(self):
        self.assertIsNone(self.d(""))
        self.assertIsNone(self.d("soon"))
        self.assertIsNone(self.d("Remote"))

    def test_company_key(self):
        self.assertEqual(common.company_key("Hook (YC S21)"), "hook")
        self.assertEqual(common.company_key("Hugging Face"), "huggingface")


class Logos(unittest.TestCase):
    def test_board_slug(self):
        self.assertEqual(logos.board_slug("https://jobs.ashbyhq.com/hookmusic/123"), "hookmusic")
        self.assertEqual(logos.board_slug("https://boards.greenhouse.io/stripe/jobs/1"), "stripe")
        self.assertEqual(logos.board_slug("https://example.com/careers"), "")

    def test_guessed_domains_are_not_trusted(self):
        cands = logos.candidates("Zyxwv Labs", "https://jobs.lever.co/zyxwv/1")
        self.assertTrue(cands)
        self.assertFalse(any(t for d, t in cands if d.endswith(".com") and "zyxwv" in d and "lever" not in d))


class LinkGrounding(unittest.TestCase):
    def test_bare_prefix_does_not_vouch_for_profiles(self):
        seen = "see https://www.linkedin.com/in/ and https://www.linkedin.com/in/real-person-08153850/"
        self.assertEqual(grounding.unseen_urls("https://www.linkedin.com/in/real-person-40a62a56/", seen), ["https://www.linkedin.com/in/real-person-40a62a56/"])
        self.assertEqual(grounding.unseen_urls("https://linkedin.com/in/real-person-08153850", seen), [])

    def test_query_and_shortened_links(self):
        seen = "https://boards.greenhouse.io/stripe/jobs/123?gh_jid=1"
        self.assertEqual(grounding.unseen_urls("https://boards.greenhouse.io/stripe/jobs/123", seen), [])
        self.assertEqual(grounding.unseen_urls("https://evil.example/x", seen), ["https://evil.example/x"])


class AgentHarness(unittest.TestCase):
    def test_actions_need_intent(self):
        self.assertFalse(agentic.allowed_by_goal("referral", "which picks have an outdated referral plan?"))
        self.assertTrue(agentic.allowed_by_goal("referral", "make a new referral plan for Stripe"))
        self.assertTrue(agentic.allowed_by_goal("picks_list", "anything"))

    def test_check_call_suggests_and_validates(self):
        ok, msg = agentic.check_call("pick_list", {})
        self.assertFalse(ok)
        self.assertIn("picks_list", msg)
        ok, _ = agentic.check_call("picks_list", {"plan": "outdated"})
        self.assertTrue(ok)

    def test_list_answers_are_completed_from_tool_output(self):
        res = "3 pick(s) match:\n[a:1] Baseten | SWE | x\n[b:2] Amazon | ML | y\n[c:3] NVIDIA | Kernels | z"
        out = agentic._complete_list("Which picks have outdated plans?", "3 picks match.", [("STEP 1", "", res)])
        self.assertIn("Amazon | ML", out)
        full = agentic._complete_list("Which picks?", "Baseten, Amazon and NVIDIA.", [("STEP 1", "", res)])
        self.assertEqual(full, "Baseten, Amazon and NVIDIA.")
        self.assertEqual(agentic._complete_list("Prepare a packet", "Done.", [("STEP 1", "", res)]), "Done.")

    def test_repeated_call_reshows_result(self):
        replies = iter(['{"tool": "picks_list", "args": {}}', '{"tool": "picks_list", "args": {}}', '{"final": "ok"}'])
        prompts = []

        def fake_run(prompt, **kw):
            prompts.append(prompt)
            return next(replies), {}
        with mock.patch.object(agentic, "run", fake_run), mock.patch.object(agentic, "tool", return_value="[x:1] Acme | Engineer | status new"):
            agentic.loop("list my picks")
        self.assertIn("its full result is repeated here", prompts[2])
        self.assertIn("Acme | Engineer", prompts[2])


class ChatThreads(unittest.TestCase):
    def test_threads_keep_messages_apart(self):
        a, b = inbox.new_thread("A"), inbox.new_thread("B")
        inbox.say("user", "hello from a", a)
        inbox.say("user", "hello from b", b)
        c = common.db()
        self.assertEqual([r["text"] for r in c.execute("SELECT text FROM chat WHERE thread=?", (a,))], ["hello from a"])
        inbox.delete_thread(b)
        self.assertFalse(c.execute("SELECT 1 FROM chat WHERE thread=?", (b,)).fetchone())
        self.assertIn(a, [t["id"] for t in inbox.list_threads()])

    def test_reply_parse(self):
        r, acts = inbox._parse('{"reply": "Sure.", "actions": [{"kind": "referral", "arg": "stripe"}, {"kind": "rm -rf", "arg": "/"}]}')
        self.assertEqual(r, "Sure.")
        self.assertEqual(acts, [("referral", "stripe")])
        r, acts = inbox._parse("Plain answer\nACTION: packet stripe")
        self.assertEqual(acts, [("packet", "stripe")])


class StalePlans(unittest.TestCase):
    def write(self, text):
        p = os.path.join(tempfile.mkdtemp(), "REFERRAL.md")
        with open(p, "w", encoding="utf-8") as f:
            f.write(text)
        return p

    def test_reasons(self):
        now = common.now()
        self.assertEqual(maintenance.stale_reason(self.write("Ask Jo (UNVERIFIED) https://linkedin.com/in/jo"), now, 30), "contains UNVERIFIED claims")
        self.assertEqual(maintenance.stale_reason(self.write("Generic advice only."), now, 30), "names nobody with a profile link")
        old = (dt.datetime.now() - dt.timedelta(days=40)).isoformat(timespec="seconds")
        self.assertIn("older than", maintenance.stale_reason(self.write("Jo https://www.linkedin.com/in/jo"), old, 30))
        self.assertEqual(maintenance.stale_reason(self.write("Jo https://www.linkedin.com/in/jo"), now, 30), "")


class PostedBackfill(unittest.TestCase):
    def test_every_job_gets_a_date(self):
        import pipeline
        add_job("t:posted1", "Acme", "Engineer")
        c = common.db()
        c.execute("UPDATE jobs SET posted=NULL, posted_src=NULL WHERE id='t:posted1'")
        c.commit()
        pipeline.backfill_posted()
        r = c.execute("SELECT posted, posted_src FROM jobs WHERE id='t:posted1'").fetchone()
        self.assertTrue(r["posted"])
        self.assertEqual(r["posted_src"], "seen")


class DesktopApp(unittest.TestCase):
    def test_manifest(self):
        m = json.loads(desktop.manifest())
        self.assertEqual(m["display"], "standalone")
        self.assertTrue(all(os.path.exists(os.path.join(desktop.STATIC, i["src"].lstrip("/"))) for i in m["icons"]))
        self.assertTrue(os.path.exists(os.path.join(desktop.STATIC, "icon.ico")))

    def test_linux_launcher_entry(self):
        home = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}):
            p = desktop._linux_shortcut()[0]
        t = open(p, encoding="utf-8").read()
        self.assertIn("Exec=", t)
        self.assertIn("run.py\" app", t)
        self.assertIn("icon-512.png", t)


if __name__ == "__main__":
    unittest.main()
