from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import tempfile
import threading
import unittest

from collab.common import CollabError
from collab.store import Store


class LightweightStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / "state")
        self.spec = {"goal": "fixture", "root": self.temp.name, "workspace": self.temp.name,
                     "read_inputs": [], "write_paths": ["out.txt"], "constraints": [],
                     "acceptance": ["out exists"], "agent_preset": "collab-worker",
                     "timeout_seconds": 60}

    def test_submit_claim_delivery_review_and_idempotency(self):
        first = self.store.submit(self.spec, "request-1")
        replay = self.store.submit(self.spec, "request-1")
        run_id = first["run"]["id"]
        self.assertFalse(first["idempotent_replay"])
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["run"]["id"], run_id)
        with self.assertRaises(CollabError) as caught:
            self.store.submit({**self.spec, "goal": "different"}, "request-1")
        self.assertEqual(caught.exception.code, "IDEMPOTENCY_CONFLICT")

        running = self.store.claim_next()
        self.assertEqual(running["state"], "RUNNING")
        self.assertEqual(running["version"], 2)
        delivered = self.store.finish(run_id, {"status": "EXECUTION_COMPLETED"},
                                      {"changes": [], "out_of_scope": []})
        self.assertEqual(delivered["state"], "DELIVERED")
        reviewed = self.store.review(run_id, 3, "ACCEPTED", "checked", ["actual diff checked"],
                                     {"available": True, "changes": []}, "review-1")
        self.assertEqual(reviewed["run"]["state"], "ACCEPTED")
        self.assertEqual(reviewed["run"]["version"], 4)
        with self.assertRaises(CollabError) as caught:
            self.store.review(run_id, 4, "ACCEPTED", "again", ["again"], {}, "review-2")
        self.assertEqual(caught.exception.code, "INVALID_STATE")

    def test_cancel_running_and_recover_interrupted(self):
        run = self.store.submit(self.spec, "cancel-running")["run"]
        self.store.claim_next()
        cancelled = self.store.cancel(run["id"], 2, "cancel-request")
        self.assertTrue(cancelled["run"]["cancel_requested"])
        self.assertEqual(cancelled["run"]["state"], "RUNNING")
        final = self.store.finish(run["id"], {"status": "CANCELLED"}, {"changes": [], "out_of_scope": []})
        self.assertEqual(final["state"], "CANCELLED")

        queued = self.store.submit(self.spec, "cancel-queued")["run"]
        result = self.store.cancel(queued["id"], 1, "cancel-queued-request")
        self.assertEqual(result["run"]["state"], "CANCELLED")

        interrupted = self.store.submit(self.spec, "recover-me")["run"]
        self.store.claim_next()
        recovered = self.store.recover_interrupted()
        self.assertIn(interrupted["id"], recovered)
        self.assertEqual(self.store.get(interrupted["id"])["state"], "FAILED")
        self.assertTrue(self.store.get(interrupted["id"])["result"]["summary"]["outcome_unknown"])

    def test_list_and_history_use_versions(self):
        run = self.store.submit(self.spec, "history")["run"]
        self.store.claim_next()
        listed = self.store.list(after_row=0, limit=1)
        self.assertEqual(listed["items"][0]["id"], run["id"])
        history = self.store.history(run["id"])
        self.assertEqual([item["state"] for item in history], ["RUNNING", "QUEUED"])

    def test_same_root_concurrent_submit_is_atomic(self):
        barrier = threading.Barrier(2)
        prepared = []

        def submit(index):
            barrier.wait(timeout=5)
            try:
                return self.store.submit(self.spec, f"concurrent-{index}",
                                         prepare=prepared.append)["run"]["state"]
            except CollabError as error:
                return error.code

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(submit, range(2)))
        self.assertCountEqual(outcomes, ["QUEUED", "ROOT_BUSY"])
        self.assertEqual(self.store.counts(), {"QUEUED": 1})
        self.assertEqual(len(prepared), 1)

    def test_root_lease_tracks_blocking_states_inside_submit(self):
        run = self.store.submit(self.spec, "lease-first")["run"]
        for state in ("QUEUED", "RUNNING", "PAUSED", "DELIVERED"):
            with self.subTest(blocking_state=state):
                with self.store._db(immediate=True) as db:
                    db.execute("UPDATE runs SET state=? WHERE id=?", (state, run["id"]))
                    db.commit()
                with self.assertRaises(CollabError) as caught:
                    self.store.submit(self.spec, "lease-next")
                self.assertEqual(caught.exception.code, "ROOT_BUSY")
        other = self.store.submit({**self.spec, "root": self.temp.name + "/other"}, "other-root")
        self.assertEqual(other["run"]["state"], "QUEUED")
        for state in ("ACCEPTED", "REJECTED", "FAILED", "CANCELLED"):
            with self.subTest(released_state=state):
                with self.store._db(immediate=True) as db:
                    db.execute("UPDATE runs SET state=? WHERE id=?", (state, run["id"]))
                    db.commit()
                next_run = self.store.submit(self.spec, "released-" + state)["run"]
                self.assertEqual(next_run["state"], "QUEUED")
                run = next_run


if __name__ == "__main__":
    unittest.main()
