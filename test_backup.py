# -*- coding: utf-8 -*-
"""Full backup + restore: secrets and users stay out, restore is all-or-nothing.

Run with:  python -m unittest -v test_backup
"""
import io
import json
import os
import tempfile
import time
import unittest
import zipfile

import test_competitor as tc  # sets the test DB env before db is imported
import backup
from db import db


def _zip_bytes() -> bytes:
    return b"".join(backup.stream_backup(db))


class BackupTest(unittest.TestCase):
    def setUp(self):
        tc._clean()
        for t in ("targets", "categories", "category_items"):
            db._execute(f"DELETE FROM {t}")
        from werkzeug.security import generate_password_hash
        db.create_user("admin@test.dz", generate_password_hash("pass-12345"), role="admin")
        for i in range(5):
            db.save_ad(tc._ad(f"b{i}", "shop.youcan.store", f"عرض رقم {i}"), query="q", country="DZ")
        db.add_target(type="page", target_value="777", country="DZ")
        db.set_setting("ai_api_key", "enc:v1:SECRET-KEY")
        db.set_setting("telegram_bot_token", "123:SECRET")
        db.set_setting("ai_model", "deepseek/x")

    def test_backup_content(self):
        zf = zipfile.ZipFile(io.BytesIO(_zip_bytes()))
        names = set(zf.namelist())
        self.assertIn("manifest.json", names)
        self.assertIn("tables/meta_ads.jsonl", names)
        self.assertIn("ads.csv", names)
        self.assertNotIn("tables/users.jsonl", names)
        self.assertNotIn("tables/user_sessions.jsonl", names)
        manifest = json.loads(zf.read("manifest.json"))
        self.assertEqual(manifest["tables"]["meta_ads"], 5)
        settings = zf.read("tables/settings.jsonl").decode()
        self.assertNotIn("SECRET", settings)
        self.assertIn("deepseek/x", settings)
        csv_text = zf.read("ads.csv").decode("utf-8-sig")
        self.assertEqual(len(csv_text.strip().splitlines()), 6)  # header + 5 ads

    def test_restore_replaces_data_but_keeps_users_and_secrets(self):
        data = _zip_bytes()
        path = os.path.join(tempfile.mkdtemp(), "b.zip")
        with open(path, "wb") as f:
            f.write(data)
        # change everything after the backup
        db._execute("DELETE FROM meta_ads WHERE ad_id IN ('b0', 'b1')")
        db.save_ad(tc._ad("new1", "other.youcan.store", "إعلان جديد"), query="q", country="DZ")
        db.set_setting("ai_model", "changed/model")
        db.set_setting("ai_api_key", "enc:v1:NEW-KEY")
        info = backup.inspect_backup(path, db)
        self.assertIn("meta_ads", info["will_restore"])
        self.assertNotIn("users", info["will_restore"])
        done = backup.restore(path, db)
        self.assertEqual(done["meta_ads"], 5)
        ids = {r["ad_id"] for r in db._query("SELECT ad_id FROM meta_ads")}
        self.assertEqual(ids, {f"b{i}" for i in range(5)})
        s = db.get_settings()
        self.assertEqual(s["ai_model"], "deepseek/x")            # restored
        self.assertEqual(s["ai_api_key"], "enc:v1:NEW-KEY")      # secret untouched
        self.assertTrue(db._query_one("SELECT id FROM users WHERE email='admin@test.dz'"))
        self.assertEqual(len(db._query("SELECT id FROM targets")), 1)

    def test_not_a_backup(self):
        path = os.path.join(tempfile.mkdtemp(), "x.zip")
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("hello.txt", "hi")
        with self.assertRaises(backup.RestoreError):
            backup.inspect_backup(path, db)

    def test_api_download_upload_restore(self):
        import app as app_module
        c = app_module.app.test_client()
        c.post("/api/auth/login", json={"email": "admin@test.dz", "password": "pass-12345"})
        r = c.get("/api/backup/download")
        self.assertEqual(r.status_code, 200)
        data = r.get_data()
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(data)))
        up = c.post("/api/backup/restore/upload", data={"file": (io.BytesIO(data), "b.zip")},
                    content_type="multipart/form-data").get_json()
        self.assertEqual(up["tables"]["meta_ads"], 5)
        self.assertEqual(c.post("/api/backup/restore/start", json={"token": up["token"]}).status_code, 400)
        db._execute("DELETE FROM meta_ads")
        self.assertTrue(c.post("/api/backup/restore/start",
                               json={"token": up["token"], "confirm": "استعادة"}).get_json()["ok"])
        for _ in range(100):
            st = c.get("/api/backup/restore/status").get_json()
            if not st.get("running"):
                break
            time.sleep(0.05)
        self.assertEqual(st.get("error"), "")
        self.assertEqual(len(db._query("SELECT ad_id FROM meta_ads")), 5)


if __name__ == "__main__":
    unittest.main()
