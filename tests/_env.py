"""Point JobAgent at a throwaway workspace BEFORE any agent module is imported, so tests never touch real data."""
import os, sys, tempfile
_ws = tempfile.mkdtemp(prefix="jobagent-test-")
os.environ["JOBAGENT_HOME"] = _ws
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))
import common  # noqa: E402

common.PROFILE.setdefault("summary", "Final-year CS student")
common.PROFILE.setdefault("graduation", "2027-08")
common.PROFILE["graduation"] = "2027-08"


def add_job(id_, company, title, desc="", location="Remote", url="https://example.com/job", status="new", score=70):
    c = common.db()
    c.execute("INSERT OR REPLACE INTO jobs(id,company,tier,category,title,location,url,ats,first_seen,last_seen,active,score,loc_class,family,early,desc,status) "
              "VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)", (id_, company, "A", "x", title, location, url, "greenhouse", common.now(), common.now(), score, "remote", "ml_eng", 1, desc, status))
    c.commit()
