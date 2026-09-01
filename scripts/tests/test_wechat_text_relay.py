import hashlib
import importlib.util
import sqlite3
import tempfile
import unittest
from pathlib import Path


RELAY_PATH = Path(__file__).resolve().parents[1] / "wechat-text-relay.py"
SPEC = importlib.util.spec_from_file_location("wechat_text_relay", RELAY_PATH)
relay = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(relay)


class TextRelayTests(unittest.TestCase):
    def test_read_messages_returns_scan_watermark_past_filtered_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshot.db"
            table = "Msg_" + hashlib.md5(relay.CHAT_ID.encode()).hexdigest()
            connection = sqlite3.connect(snapshot)
            connection.execute("CREATE TABLE Name2Id(rowid INTEGER PRIMARY KEY,user_name TEXT)")
            connection.execute(
                f'''CREATE TABLE "{table}"(
                    local_id INTEGER, server_id INTEGER, local_type INTEGER,
                    create_time INTEGER, real_sender_id INTEGER, message_content BLOB
                )'''
            )
            connection.execute("INSERT INTO Name2Id(rowid,user_name) VALUES(1,'wxid_test')")
            connection.execute(f'''INSERT INTO "{table}" VALUES(1,11,3,1,1,?)''', (b"ignored",))
            connection.execute(f'''INSERT INTO "{table}" VALUES(2,22,1,2,1,?)''', (b"hello",))
            connection.commit()
            connection.close()

            messages, scanned_max = relay.read_messages(snapshot, table, -1)

            self.assertEqual(scanned_max, 2)
            self.assertEqual(messages[0]["text"], "hello")

    def test_save_state_is_valid_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            relay.save_state(path, {"last_local_id": 42, "chat_id": relay.CHAT_ID})
            self.assertEqual(path.read_text(encoding="utf-8").strip(), '{\n  "last_local_id": 42,\n  "chat_id": "50136408612@chatroom"\n}')


if __name__ == "__main__":
    unittest.main()
