"""Strip every non-demo row from a copy of the demo DB, then recompute metrics + judge rows from demo data only."""
import sys

from scorer import judge, metrics
from scorer.db import connect

db = sys.argv[1]
c = connect(db)
keep = "SELECT id FROM session WHERE id LIKE 'demo-%'"
turns = f"SELECT id FROM turn WHERE session_id IN ({keep})"
tools = f"SELECT id FROM tool_call WHERE turn_id IN ({turns})"
c.executescript(f"""
DELETE FROM metric_evidence; DELETE FROM metric_value; DELETE FROM judge_action;
DELETE FROM file_edit WHERE tool_call_id NOT IN ({tools});
DELETE FROM tool_call WHERE turn_id IS NULL OR turn_id NOT IN ({turns});
DELETE FROM api_call WHERE turn_id IS NULL OR turn_id NOT IN ({turns});
DELETE FROM git_change WHERE turn_id IS NULL OR turn_id NOT IN ({turns});
DELETE FROM event WHERE session_id IS NULL OR session_id NOT IN ({keep});
DELETE FROM turn WHERE session_id NOT IN ({keep});
DELETE FROM session WHERE id NOT LIKE 'demo-%';
""")
try:
    c.execute("DELETE FROM ui_cache")
except Exception:
    pass
run_id = c.execute("INSERT INTO ingest_run(at, parser_version) VALUES(datetime('now'), 'demo-only/1')").lastrowid
c.commit()
metrics.compute_all(c, run_id)
judge.run_judge(c, run_id, day=None, chain=["api"], max_calls=0)
c.commit()
print("run", run_id, {t: c.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                      for t in ("session", "turn", "api_call", "tool_call", "event", "metric_value")})
