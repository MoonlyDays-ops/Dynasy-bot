"""Offline tests for database behavior; do not require Discord login."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from storage import Storage


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = Storage(str(Path(self.tempdir.name) / "data" / "database.sqlite3"))

    def tearDown(self):
        self.store.close()
        self.tempdir.cleanup()

    def test_review_lifecycle_and_duplicate_guard(self):
        app_id = self.store.reserve_application(100, 200)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.reserve_application(100, 200)
        self.store.attach_application(app_id, 300, 400, 500)
        self.assertEqual(self.store.application_for_message(400)["applicant_id"], 200)
        self.assertTrue(self.store.claim_application(app_id))
        self.assertFalse(self.store.claim_application(app_id))
        self.store.finish_application(app_id, "accepted", 600)
        self.assertEqual(self.store.application_for_message(400)["status"], "accepted")
        self.assertIsNone(self.store.active_application(100, 200))
        self.assertIsInstance(self.store.reserve_application(100, 200), int)

    def test_rollback_and_thread_reset(self):
        app_id = self.store.reserve_application(1, 2)
        self.store.abort_application(app_id)
        self.assertIsNone(self.store.active_application(1, 2))
        app_id = self.store.reserve_application(1, 2)
        self.store.attach_application(app_id, 3, 4, 5)
        self.store.reset_missing_thread(3)
        self.assertIsNone(self.store.active_application(1, 2))

    def test_voice_room_ownership(self):
        self.store.create_room(10, 20, 30)
        self.assertEqual(self.store.owned_room(20, 30)["channel_id"], 10)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_room(11, 20, 30)
        self.store.transfer_room(10, 40)
        self.assertIsNone(self.store.owned_room(20, 30))
        self.assertEqual(self.store.owned_room(20, 40)["channel_id"], 10)
        self.store.remove_room(10)
        self.assertIsNone(self.store.room(10))

    def test_restart_recovery(self):
        first = self.store.reserve_application(100, 200)
        self.store.attach_application(first, 1000, 2000, 3000)
        self.store.claim_application(first)
        second = self.store.reserve_application(101, 201)
        self.store.recover_incomplete_operations()
        self.assertEqual(self.store.application_for_message(2000)["status"], "pending")
        self.assertIsNone(self.store.active_application(101, 201))
        self.assertTrue(self.store.claim_application(first))

    def test_configuration(self):
        values = dict(guild_id=1, approved_role_id=2, crime_role_id=2,
                      state_role_id=10, moderator_role_id=3,
                      application_channel_id=4, review_channel_id=4, log_channel_id=6,
                      control_channel_id=7, lobby_channel_id=8, voice_category_id=9)
        self.store.save_config(values)
        self.assertEqual(self.store.config(1)["crime_role_id"], 2)
        self.assertEqual(self.store.config(1)["state_role_id"], 10)
        self.store.set_application_roles(1, 11, 12)
        self.assertEqual(self.store.config(1)["crime_role_id"], 11)
        self.assertEqual(self.store.config(1)["state_role_id"], 12)

    def test_new_application_fields_and_private_thread(self):
        app_id = self.store.reserve_application(
            100, 200, game_nick="Alex_Dynasty", static_number="000123",
            irl_name_age="Александр, 20", direction="State", history="Из друзей",
        )
        self.store.attach_application(app_id, 300, 400)  # no anchor in private thread
        data = self.store.application_for_message(400)
        self.assertEqual(data["static_number"], "000123")
        self.assertEqual(data["direction"], "State")
        self.assertIsNone(data["anchor_message_id"])

    def test_old_database_is_migrated_without_data_loss(self):
        import sqlite3
        from pathlib import Path
        # Create an OLD database by removing the new columns, then reconnect.
        self.store.close()
        db_file = Path(self.tempdir.name) / "data" / "database.sqlite3"
        with sqlite3.connect(db_file) as conn:
            conn.execute("ALTER TABLE guild_config DROP COLUMN crime_role_id")
            conn.execute("ALTER TABLE guild_config DROP COLUMN state_role_id")
            for col in ("game_nick", "static_number", "irl_name_age", "direction", "history"):
                conn.execute(f"ALTER TABLE applications DROP COLUMN {col}")
            conn.execute("""INSERT INTO guild_config(guild_id, approved_role_id, moderator_role_id,
                application_channel_id, review_channel_id, log_channel_id,
                control_channel_id, lobby_channel_id, voice_category_id)
                VALUES (1, 2, 3, 4, 5, 6, 7, 8, 9)""")
            conn.execute("INSERT INTO applications(guild_id, applicant_id, status) VALUES (1, 20, 'pending')")
        self.store = Storage(str(db_file))
        self.assertEqual(self.store.config(1)["application_channel_id"], 4)
        self.assertIsNone(self.store.config(1)["crime_role_id"])
        self.assertEqual(self.store.active_application(1, 20)["status"], "pending")
        self.store.set_application_roles(1, 50, 60)
        self.assertEqual(self.store.config(1)["state_role_id"], 60)


if __name__ == "__main__":
    unittest.main()
