import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from agent_capacity.cli import append_job, find_job


class DurableStateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = Path(self.directory.name) / "jobs.json"
        self.previous = os.environ.get("AGENT_CAPACITY_JOBS")
        os.environ["AGENT_CAPACITY_JOBS"] = str(self.path)

    def tearDown(self):
        if self.previous is None:
            os.environ.pop("AGENT_CAPACITY_JOBS", None)
        else:
            os.environ["AGENT_CAPACITY_JOBS"] = self.previous
        self.directory.cleanup()

    def test_corrupted_state_recovers_to_a_complete_new_document(self):
        self.path.write_text("{not-json")
        append_job({"id": "recovered", "state": "submitted"})
        value = json.loads(self.path.read_text())
        self.assertEqual(value["version"], 1)
        self.assertEqual(value["jobs"], [{"id": "recovered", "state": "submitted"}])

    def test_concurrent_job_writers_do_not_drop_records(self):
        threads = [
            threading.Thread(target=append_job, args=({"id": f"job-{index}", "state": "submitted"},))
            for index in range(20)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        value = json.loads(self.path.read_text())
        self.assertEqual(len(value["jobs"]), 20)
        self.assertEqual({job["id"] for job in value["jobs"]}, {f"job-{index}" for index in range(20)})
        self.assertEqual(find_job("job-7")["id"], "job-7")


if __name__ == "__main__":
    unittest.main()
