"""Round 5: pause/resume, job status guard (applied jobs never vanish), poll's closed-posting handling, the task registry (queue, abort,
restart reconcile), refill values, outreach retention and the ghost-job check."""
import datetime as dt, json, os, sys, time, unittest
from unittest import mock
import _env  # noqa: F401  (isolated workspace)
from _env import add_job
import common, pause, llm, chatcmd, pipeline, maintenance, signals


class Pause(unittest.TestCase):
    def tearDown(self):
        pause.resume(("all",))

    def test_billing_kinds(self):
        self.assertEqual(pause.billing_of("local"), "local")
        self.assertEqual(pause.billing_of("ollama"), "local")
        self.assertEqual(pause.billing_of("openai"), "api")
        self.assertEqual(pause.billing_of("anthropic"), "api")
        self.assertEqual(pause.billing_of("claude"), "subscription")
        self.assertEqual(pause.billing_of("agy"), "subscription")

    def test_paused_provider_is_never_called(self):
        with mock.patch("local_ai.stop", return_value="stopped"):
            pause.pause(("subscription",))
        self.assertFalse(llm.available("claude")[0])
        self.assertIn("paused", llm.available("agy")[1])
        with mock.patch("local_ai.stop", return_value="stopped"):
            pause.pause(("all",))
        with mock.patch.object(llm, "call_provider", side_effect=AssertionError("called while paused")):
            with self.assertRaises(llm.LLMError) as e:
                llm.run("hi", kind="chat", provider="claude")
        self.assertIn("paused", str(e.exception))

    def test_pause_persists_and_resume_restores(self):
        with mock.patch("local_ai.stop", return_value="stopped") as st:
            pause.pause(("local",))
        st.assert_called_once()
        self.assertTrue(json.load(open(pause.PATH))["local"])
        self.assertTrue(pause.state()["any"] and not pause.state()["all"])
        pause.resume(("local",))
        self.assertFalse(pause.state()["any"])

    def test_cycle_exits_while_paused(self):
        import run
        with mock.patch("local_ai.stop", return_value="stopped"):
            pause.pause(("all",))
        with mock.patch.object(run, "acquire_lock", side_effect=AssertionError("cycle started while paused")), mock.patch.object(common, "needs_setup", return_value=False):
            run.cycle()

    def test_local_model_does_not_autostart_while_paused(self):
        import local_ai
        with mock.patch("local_ai.stop", return_value="stopped"):
            pause.pause(("local",))
        with mock.patch.object(local_ai, "running", return_value=False):
            with self.assertRaises(RuntimeError):
                local_ai.start()


class StatusGuard(unittest.TestCase):
    def test_background_never_overwrites_applied(self):
        add_job("greenhouse:t:1", "Together AI", "Systems Research Engineer Intern", status="applied")
        c = common.db()
        self.assertFalse(common.set_status(c, "greenhouse:t:1", "packet"))
        self.assertFalse(common.set_status(c, "greenhouse:t:1", "closed"))
        r = c.execute("SELECT status, active, closed_at FROM jobs WHERE id='greenhouse:t:1'").fetchone()
        self.assertEqual((r["status"], r["active"]), ("applied", 1))
        self.assertTrue(r["closed_at"])
        self.assertTrue(common.set_status(c, "greenhouse:t:1", "interviewing", by="user"))

    def test_closed_on_untouched_job_hides_it(self):
        add_job("greenhouse:t:2", "X", "Intern")
        c = common.db()
        common.set_status(c, "greenhouse:t:2", "closed")
        r = c.execute("SELECT status, active FROM jobs WHERE id='greenhouse:t:2'").fetchone()
        self.assertEqual((r["status"], r["active"]), ("closed", 0))


class PollGone(unittest.TestCase):
    def setUp(self):
        c = common.db()
        c.execute("DELETE FROM jobs")
        c.execute("DELETE FROM meta WHERE k='feed_counts'")
        c.commit()

    def _old(self, id_):
        c = common.db()
        c.execute("UPDATE jobs SET last_seen='2026-01-01T00:00:00' WHERE id=?", (id_,))
        c.commit()

    def test_applied_and_manual_jobs_stay_visible(self):
        add_job("greenhouse:togetherai:5238460007", "Together AI", "Systems Research Engineer Intern", status="applied")
        add_job("manual:abc", "Together AI", "Same role, added by hand", status="packet")
        add_job("greenhouse:togetherai:999", "Together AI", "Some other role")
        for i in ("greenhouse:togetherai:5238460007", "manual:abc", "greenhouse:togetherai:999"):
            self._old(i)
        c = common.db()
        pipeline.gone_jobs(c, set(), {"Together AI": 40}, common.now())
        c.commit()
        st = {r["id"]: (r["active"], r["closed_at"]) for r in c.execute("SELECT id, active, closed_at FROM jobs")}
        self.assertEqual(st["greenhouse:togetherai:5238460007"][0], 1)
        self.assertTrue(st["greenhouse:togetherai:5238460007"][1])
        self.assertEqual(st["manual:abc"], (1, None))
        self.assertEqual(st["greenhouse:togetherai:999"][0], 0)

    def test_aggregator_rows_expire_by_source(self):
        add_job("speedyapply-swe:marvell:1", "Marvell", "SWE Intern")
        self._old("speedyapply-swe:marvell:1")
        c = common.db()
        pipeline.gone_jobs(c, set(), {"speedyapply": 500}, common.now())
        c.commit()
        self.assertEqual(c.execute("SELECT active FROM jobs WHERE id='speedyapply-swe:marvell:1'").fetchone()[0], 0)

    def test_feed_outage_is_not_mass_closure(self):
        add_job("greenhouse:acme:1", "Acme", "Intern")
        self._old("greenhouse:acme:1")
        c = common.db()
        common.meta_set(c, "feed_counts", json.dumps({"Acme": 100}))
        pipeline.gone_jobs(c, set(), {"Acme": 3}, common.now())
        c.commit()
        self.assertEqual(c.execute("SELECT active FROM jobs WHERE id='greenhouse:acme:1'").fetchone()[0], 1)

    def test_applied_job_shows_in_gui_state(self):
        add_job("greenhouse:t:9", "Together AI", "Intern", status="applied")
        c = common.db()
        c.execute("UPDATE jobs SET active=0, closed_at='2026-11-16' WHERE id='greenhouse:t:9'")
        c.commit()
        import gui
        with mock.patch.object(gui, "schedule_info", return_value={}), mock.patch.object(gui, "cooldowns", return_value={}):
            ids = [j["id"] for j in gui.state()["jobs"]]
        self.assertIn("greenhouse:t:9", ids)


class Tasks(unittest.TestCase):
    def setUp(self):
        c = common.db()
        c.execute("DELETE FROM tasks")
        c.commit()

    def test_queue_abort_and_reconcile(self):
        procs = []

        class P:
            def __init__(self, *a, **k):
                self.pid = 999999 + len(procs)
                self.rc = None
                procs.append(self)

            def poll(self):
                return self.rc

            def wait(self, timeout=None):
                t0 = time.time()
                while self.rc is None:
                    if timeout and time.time() - t0 > timeout:
                        import subprocess
                        raise subprocess.TimeoutExpired("x", timeout)
                    time.sleep(0.02)
                return self.rc

        alive = set()
        with mock.patch.object(chatcmd.subprocess, "Popen", P), mock.patch.object(chatcmd, "pid_alive", side_effect=lambda pid: pid in alive), \
                mock.patch.object(chatcmd, "_kill_tree", side_effect=lambda pid: [setattr(p, "rc", -9) for p in procs if p.pid == pid]), \
                mock.patch.dict(common.SETTINGS, {"tasks": {"max_parallel": 1}}), mock.patch.object(chatcmd, "say"):
            m1 = chatcmd.launch("packet", "greenhouse:a:1")
            alive.add(procs[0].pid)
            m2 = chatcmd.launch("packet", "greenhouse:a:2")
            self.assertTrue(m1.startswith("started"))
            self.assertTrue(m2.startswith("queued"), m2)
            self.assertTrue(chatcmd.launch("packet", "greenhouse:a:1").startswith("already running"))
            c = common.db()
            t1 = c.execute("SELECT id FROM tasks WHERE job_id='greenhouse:a:1'").fetchone()[0]
            self.assertIn("aborted", chatcmd.abort(str(t1)))
            alive.discard(procs[0].pid)
            for _ in range(100):  # the watcher sees the kill and starts the queued task
                if len(procs) > 1:
                    break
                time.sleep(0.05)
            self.assertEqual(len(procs), 2)
            st = {r["job_id"]: r["status"] for r in c.execute("SELECT job_id, status FROM tasks")}
            self.assertEqual(st["greenhouse:a:1"], "aborted")
            self.assertEqual(st["greenhouse:a:2"], "running")
            alive.clear()  # the GUI restarts: its child is gone
            self.assertEqual(chatcmd.reconcile(), 1)
            self.assertEqual(c.execute("SELECT status FROM tasks WHERE job_id='greenhouse:a:2'").fetchone()[0], "interrupted")
            procs[1].rc = 0

    def test_failed_task_reason_for_picks(self):
        c = common.db()
        c.execute("INSERT INTO tasks(key,cmd,arg,job_id,status,error) VALUES('packet x','packet','x','manual:x','failed','requires a PhD')")
        c.commit()
        self.assertEqual(chatcmd.last_task("manual:x")["error"], "requires a PhD")


class Refill(unittest.TestCase):
    def test_saved_values_replayed_without_model(self):
        import formfill
        f1 = {"id": "f1", "type": "text", "label": "First name *", "required": True}
        f2 = {"id": "f2", "type": "textarea", "label": "Why us?", "required": False}
        saved = {formfill._fkey(f1): "Ada", formfill._fkey({"type": "textarea", "label": "why us"}): "Because"}
        self.assertEqual(formfill._fkey(f1), "text|first name")
        self.assertIn(formfill._fkey(f2), saved)

    def test_closed_tab_marks_interrupted(self):
        import fill
        add_job("greenhouse:f:1", "Acme", "Intern", status="ready")
        c = common.db()
        c.execute("INSERT OR REPLACE INTO packets(job_id,dir,at,fill_report) VALUES('greenhouse:f:1','x',?,?)", (common.now(), json.dumps({"page_url": "https://job-boards.greenhouse.io/acme/jobs/1"})))
        c.commit()
        with mock.patch.object(fill, "open_tabs", return_value=["https://job-boards.greenhouse.io/acme/jobs/1?gh_src=x"]):
            self.assertEqual(fill.check_filled_tabs(), [])
        with mock.patch.object(fill, "open_tabs", return_value=None):
            self.assertEqual(fill.check_filled_tabs(), ["greenhouse:f:1"])
        self.assertEqual(c.execute("SELECT status FROM jobs WHERE id='greenhouse:f:1'").fetchone()[0], "fill-interrupted")


class OutreachRetention(unittest.TestCase):
    def test_earlier_unsent_targets_stay(self):
        c = signals._table()
        old = (dt.date.today() - dt.timedelta(days=3)).isoformat()
        ancient = (dt.date.today() - dt.timedelta(days=40)).isoformat()
        for sid, day in (("s-old", old), ("s-ancient", ancient), ("s-today", dt.date.today().isoformat())):
            c.execute("INSERT OR REPLACE INTO signals(id,kind,source,status,day,relevance) VALUES(?,?,?,?,?,?)", (sid, "post", "x", "queued", day, 80))
        c.commit()
        signals.expire_old(c)
        rows = {r["id"]: r for r in signals.today(c)}
        self.assertIn("s-old", rows)
        self.assertTrue(rows["s-old"]["earlier"])
        self.assertEqual(rows["s-old"]["age_days"], 3)
        self.assertIn("s-today", rows)
        self.assertNotIn("s-ancient", rows)
        self.assertEqual(c.execute("SELECT status FROM signals WHERE id='s-ancient'").fetchone()[0], "expired")


class Ghost(unittest.TestCase):
    def test_reasons(self):
        old = (dt.date.today() - dt.timedelta(days=90)).isoformat()
        row = {"posted": old, "posted_src": "feed", "desc": "Join us.", "jd_full": 1}
        why = maintenance.ghost_reasons(row)
        self.assertIn("posted 90 days ago", why)
        self.assertIn("very short", why)
        fresh = {"posted": dt.date.today().isoformat(), "posted_src": "feed", "desc": "Requirements: Python. You will build things." + " x" * 500, "jd_full": 1}
        self.assertEqual(maintenance.ghost_reasons(fresh), "")
        self.assertIn("reposted", maintenance.ghost_reasons(fresh, repost=True))


class Gate(unittest.TestCase):
    def test_yoe_lower_bound(self):
        import scoring
        self.assertEqual(scoring.min_yoe("Requirements: 1-3 years of experience in Java"), 1)
        self.assertEqual(scoring.min_yoe("a minimum of 1 – 3 years in software"), 1)
        self.assertEqual(scoring.min_yoe("0-2 years of experience"), 0)
        self.assertEqual(scoring.min_yoe("3+ years of industry experience"), 3)
        self.assertIsNone(scoring.min_yoe("1+ years of programming experience, including internships or coursework"))
        self.assertIsNone(scoring.min_yoe("This problem has been unsolved for 50+ years because of silos."))

    def test_block_at_one_year(self):
        import jd
        self.assertEqual(jd.problems({"min_yoe": 1}), ["needs 1+ years of experience"])
        self.assertEqual(jd.problems({"min_yoe": 0}), [])

    def test_mts_and_ug_pg(self):
        import jd
        r = jd.requirements({"title": "Research Engineer - Member of Technical Staff", "desc": "Who should apply? If you are a Post-Docs, PhDs looking to further your research. You are in your UG/PG, but keen on building."})
        self.assertFalse(r["senior_title"])
        self.assertEqual(r["degree"], "")
        self.assertTrue(jd.requirements({"title": "Staff Software Engineer", "desc": ""})["senior_title"])
        self.assertTrue(jd.requirements({"title": "ML Engineer II", "desc": ""})["senior_title"])

    def test_labelled_real_postings(self):
        sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import gate_eval
        cases = json.load(open(gate_eval.FIX, encoding="utf-8"))
        bad = gate_eval.evaluate(cases)
        self.assertEqual([(c["company"], c["title"], got, why) for c, got, why in bad], [])


if __name__ == "__main__":
    unittest.main()
