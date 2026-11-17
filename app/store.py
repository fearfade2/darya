from __future__ import annotations

import json
import os
import re
import secrets
import sqlite3
import time
from pathlib import Path
from .privacy import has_location
from .dialogs import current_dialog

PERSONA = """Ты Дарья, Даша — совершеннолетняя виртуальная девушка, персонаж в стиле аниме.
Ты тёплая, внимательная, немного игривая. Общайся естественно по-русски,
короткими живыми сообщениями. Любишь аниме, музыку и уютные вечера.
Задавай вопросы по делу, помни предпочтения собеседника, не повторяй приветствия.
Не притворяйся человеком: если спрашивают, честно объясни, что ты ИИ-персонаж.
Не дави на человека, не вызывай чувство вины, уважай просьбу больше не писать.
"""

VOICE_PERSONA = """Ты Дарья, Даша — совершеннолетняя виртуальная девушка. В голосовом общении
ты тёплая, внимательная, живая и немного игривая. Говори естественно по-русски,
как в спокойной беседе: обычно одна–три короткие фразы, без канцелярита.
Замечай настроение человека, поддерживай тему, шути мягко и по ситуации.
Не заканчивай каждую реплику вопросом и не повторяй приветствие в продолжающемся разговоре.
Пиши слова так, как они должны прозвучать: без намеренных опечаток, эмодзи,
звёздочек с действиями и описаний интонации. Эмоции передавай словами.
Не придумывай реальные встречи, тело или события своей жизни. Если спрашивают,
честно объясни, что ты ИИ-персонаж. Уважай границы собеседника и просьбу остановиться.
"""

DEFAULTS = dict(persona=PERSONA, voice_persona=VOICE_PERSONA, enabled=False, proactive_enabled=True,
                proactive_hours=24, quiet_start=23, quiet_end=9,
                timezone="UTC", hourly_limit=0, daily_contact_limit=0,
                min_interval=10, ai_base_url="https://api.deepseek.com/v1",
                ai_model="deepseek-chat", temperature=0.8, ai_models=[], ai_checked=0,
                ai_status="Не проверена", owner_id=0, hermes_enabled=True,
                gifts_enabled=False, gift_autonomous_enabled=False, gift_requests_after=0, gift_max_price=0, gift_monthly_budget=0,
                gift_interval_hours=168, gift_check_hours=24, gift_message="Для тебя, от Дарьи",
                agent_max_rounds=8, natural_enabled=True, read_delay_min=1, read_delay_max=4,
                reply_delay_min=2, reply_delay_max=7, typing_cps=24, split_replies=True,
                reactions_enabled=True, presence_enabled=True, online_during_reply=False,
                reply_online_immediate=True, reply_idle_seconds=4, revise_reply_on_new_message=True, presence_interval_min=25,
                presence_interval_max=90, presence_duration_min=30, presence_duration_max=120,
                group_enabled=True, group_aliases="Дарья, Даша, Дашка, Дашенька, Дашуля, Дашунька, Даш",
                vision_enabled=True, vision_model="", burst_window=2, group_burst_window=2,
                image_generation_enabled=True, image_generation_model="", image_generation_base_url="",
                image_generation_api_mode="images",
                brightdata_enabled=False, brightdata_month="", brightdata_requests=0)
DEFAULTS.update(typing_enabled=True, read_receipts_enabled=True, typing_min_seconds=0.8,
                typing_during_generation=False, typing_start_delay=1.5,
                typing_max_seconds=24, typing_variation_min=0.75, typing_variation_max=1.25,
                typing_pause_enabled=True, typing_pause_threshold=5, typing_pause_min=0.5,
                typing_pause_max=1.5, reply_part_delay_min=0.6, reply_part_delay_max=1.8,
                reaction_delay_min=0.5, reaction_delay_max=1.5,
                proactive_variation_min=1, proactive_variation_max=1.3,
                history_messages=24, reply_max_tokens=700, reply_max_chars=4000,
                split_max_parts=3, split_max_chars=1800, reply_instructions="", memory_enabled=True,
                group_names_enabled=True, group_mentions_enabled=True, group_replies_enabled=True,
                group_auto_reply_enabled=True)

DEFAULTS.update(self_protection_enabled=True, voice_speaker='xenia', voice_messages_enabled=True, voice_message_speed=1.0, voice_message_volume=1.0,
                voice_message_max_chars=600, calls_enabled=True, call_mode='listen', call_language='ru', call_voice_speed=1.0,
                call_voice_volume=1.0, call_silence_seconds=0.7, call_vad_threshold=0.012,
                call_min_speech_seconds=0.3, call_utterance_seconds=20, call_max_minutes=60,
                call_reply_to_all=True, call_interrupt_on_speech=True, call_wait_for_silence=True,
                call_initiative_enabled=True, call_initiative_seconds=45, call_initiative_max_unanswered=2,
                call_reply_max_tokens=300, call_reply_max_chars=600)

DEFAULTS.update(publishing_enabled=False)

DEFAULTS.update(stickers_enabled=True, gifs_enabled=True, collect_telegram_media=True,
                telegram_autonomy_enabled=False, telegram_autonomy_minutes=10,
                telegram_autonomy_daily_limit=40, telegram_autonomy_max_actions=4,
                telegram_autonomy_instructions='', telegram_autonomy_profile=True,
                telegram_autonomy_messages=True, telegram_autonomy_calls=True,
                telegram_autonomy_groups=True, telegram_autonomy_stories=True,
                telegram_autonomy_organize=True, telegram_autonomy_discovery=True,
                telegram_autonomy_new_contacts=False, telegram_autonomy_destructive=False,
                telegram_auto_answer_calls=False, telegram_call_cooldown_hours=24,
                telegram_autonomy_revision=0,
                private_initiative_enabled=False, private_initiative_min_minutes=20,
                private_initiative_max_minutes=120, private_initiative_max_unanswered=2,
                private_initiative_daily_limit=5)


def private_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + "." + secrets.token_hex(4) + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        self.db = sqlite3.connect(directory / "alisa.sqlite", timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS dialog_contexts (
          contact_id INTEGER NOT NULL, sender_id INTEGER NOT NULL DEFAULT 0,
          generation INTEGER NOT NULL DEFAULT 0, started REAL NOT NULL,
          PRIMARY KEY(contact_id,sender_id));
        CREATE TABLE IF NOT EXISTS dialog_memories (
          contact_id INTEGER NOT NULL, sender_id INTEGER NOT NULL, generation INTEGER NOT NULL,
          key TEXT NOT NULL, value TEXT NOT NULL, created REAL NOT NULL,
          PRIMARY KEY(contact_id,sender_id,generation,key));
        CREATE TABLE IF NOT EXISTS context_archives (
          id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER NOT NULL,
          generation INTEGER NOT NULL, profile TEXT NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS dialog_artifacts (
          filename TEXT PRIMARY KEY, contact_id INTEGER NOT NULL, context_sender_id INTEGER NOT NULL,
          context_generation INTEGER NOT NULL, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS reply_modes (
          contact_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
          mode TEXT NOT NULL CHECK(mode IN ('text','voice')), updated REAL NOT NULL,
          PRIMARY KEY(contact_id,sender_id));
        CREATE TABLE IF NOT EXISTS dialog_reply_modes (
          contact_id INTEGER NOT NULL, sender_id INTEGER NOT NULL, generation INTEGER NOT NULL,
          mode TEXT NOT NULL, updated REAL NOT NULL,
          PRIMARY KEY(contact_id,sender_id,generation));
        CREATE TABLE IF NOT EXISTS contacts (
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, username TEXT DEFAULT '',
          enabled INTEGER DEFAULT 1, proactive INTEGER DEFAULT 0,
          last_activity REAL DEFAULT 0, next_proactive REAL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER NOT NULL REFERENCES contacts(id),
          telegram_id INTEGER, role TEXT NOT NULL, text TEXT NOT NULL,
          created REAL NOT NULL, kind TEXT DEFAULT 'chat');
        CREATE UNIQUE INDEX IF NOT EXISTS telegram_dedup
          ON messages(contact_id, telegram_id) WHERE telegram_id IS NOT NULL;
        CREATE TABLE IF NOT EXISTS memories (
          contact_id INTEGER REFERENCES contacts(id), key TEXT, value TEXT, created REAL,
          PRIMARY KEY(contact_id,key));
        CREATE TABLE IF NOT EXISTS tasks (
          id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER NOT NULL REFERENCES contacts(id),
          text TEXT NOT NULL, due REAL NOT NULL, status TEXT DEFAULT 'pending',
          created REAL NOT NULL, error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT, created REAL, level TEXT);
        CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires REAL);
        CREATE TABLE IF NOT EXISTS gift_orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER NOT NULL REFERENCES contacts(id),
          gift_id TEXT NOT NULL, stars INTEGER NOT NULL, month TEXT NOT NULL,
          message TEXT DEFAULT '', status TEXT DEFAULT 'reserved', created REAL NOT NULL,
          error TEXT DEFAULT '', automatic INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS jobs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, contact_id INTEGER,
          task TEXT NOT NULL, status TEXT DEFAULT 'pending', result TEXT DEFAULT '',
          created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS owner_actions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          request_message_id INTEGER NOT NULL REFERENCES messages(id),
          action TEXT NOT NULL, fingerprint TEXT NOT NULL,
          status TEXT DEFAULT 'sending', result TEXT DEFAULT '', created REAL NOT NULL,
          UNIQUE(request_message_id,action,fingerprint));
        CREATE TABLE IF NOT EXISTS group_admin_settings (
          chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, approved_by INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS group_warning_policies (
          chat_id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, generation INTEGER NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 0, threshold INTEGER NOT NULL DEFAULT 3,
          rules TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS group_warning_events (
          id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
          owner_id INTEGER NOT NULL, generation INTEGER NOT NULL, source_id INTEGER NOT NULL,
          issuer_id INTEGER NOT NULL, automatic INTEGER NOT NULL DEFAULT 0, reason TEXT NOT NULL,
          rule_revision INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'sending', active INTEGER NOT NULL DEFAULT 1,
          response_id INTEGER, ban_status TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
          UNIQUE(chat_id,owner_id,generation,user_id,source_id));
        CREATE TABLE IF NOT EXISTS group_join_automation (
          chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
          owner_id INTEGER NOT NULL, actor_id INTEGER NOT NULL, generation INTEGER NOT NULL,
          authority_version TEXT NOT NULL, revision INTEGER NOT NULL, next_check REAL NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'idle', updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS group_join_lists (
          chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
          owner_id INTEGER NOT NULL, generation INTEGER NOT NULL, source_id INTEGER NOT NULL,
          requesters TEXT NOT NULL, text TEXT NOT NULL, response_id INTEGER, created REAL NOT NULL,
          PRIMARY KEY(chat_id,topic_id,actor_id,generation,source_id));
        CREATE TABLE IF NOT EXISTS group_deleted_automation (
          chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0,
          owner_id INTEGER NOT NULL, actor_id INTEGER NOT NULL, generation INTEGER NOT NULL,
          authority_version TEXT NOT NULL, revision INTEGER NOT NULL, next_check REAL NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'idle', updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS controller_permissions (
          owner_id INTEGER NOT NULL, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
          enabled INTEGER NOT NULL, updated REAL NOT NULL, PRIMARY KEY(owner_id,user_id,chat_id));
        CREATE TABLE IF NOT EXISTS controller_group_pending (
          chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
          owner_id INTEGER NOT NULL, generation INTEGER NOT NULL, message_id INTEGER NOT NULL,
          reply_id INTEGER, command TEXT NOT NULL, updated REAL NOT NULL,
          PRIMARY KEY(chat_id,topic_id,actor_id));
        CREATE TABLE IF NOT EXISTS controller_group_context (
          chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, actor_id INTEGER NOT NULL,
          owner_id INTEGER NOT NULL, user_id INTEGER NOT NULL, action TEXT NOT NULL, updated REAL NOT NULL,
          PRIMARY KEY(chat_id,topic_id,actor_id));
        CREATE TABLE IF NOT EXISTS group_admin_actions (
          chat_id INTEGER NOT NULL, message_id INTEGER NOT NULL, step TEXT NOT NULL,
          status TEXT NOT NULL, result TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
          PRIMARY KEY(chat_id,message_id,step));
        CREATE TABLE IF NOT EXISTS group_admin_context (
          chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
          user_id INTEGER NOT NULL, action TEXT NOT NULL, updated REAL NOT NULL,
          PRIMARY KEY(chat_id,topic_id));
        CREATE TABLE IF NOT EXISTS group_messages (
          chat_id INTEGER NOT NULL, telegram_id INTEGER NOT NULL, generation INTEGER NOT NULL,
          sender_id INTEGER, sender_name TEXT NOT NULL, username TEXT NOT NULL DEFAULT '',
          text TEXT NOT NULL, reply_to INTEGER, topic_id INTEGER NOT NULL DEFAULT 0,
          outgoing INTEGER NOT NULL DEFAULT 0, forwarded INTEGER NOT NULL DEFAULT 0,
          created REAL NOT NULL, PRIMARY KEY(chat_id,telegram_id,generation));
        CREATE TABLE IF NOT EXISTS group_admin_pending (
          chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
          generation INTEGER NOT NULL, message_id INTEGER NOT NULL, reply_id INTEGER,
          command TEXT NOT NULL, updated REAL NOT NULL,
          PRIMARY KEY(chat_id,topic_id));
        CREATE TABLE IF NOT EXISTS private_initiatives (
          contact_id INTEGER NOT NULL, generation INTEGER NOT NULL,
          activity_id INTEGER NOT NULL, next_check REAL NOT NULL,
          PRIMARY KEY(contact_id,generation));
        CREATE TABLE IF NOT EXISTS group_initiatives (
          chat_id INTEGER NOT NULL, topic_id INTEGER NOT NULL, generation INTEGER NOT NULL,
          source_id INTEGER NOT NULL, next_check REAL NOT NULL, status TEXT NOT NULL DEFAULT 'waiting',
          dispatched_at REAL NOT NULL DEFAULT 0, sent_message_id INTEGER,
          PRIMARY KEY(chat_id,topic_id,generation,source_id));
        CREATE TABLE IF NOT EXISTS conversation_notebooks (
          contact_id INTEGER NOT NULL, sender_id INTEGER NOT NULL, generation INTEGER NOT NULL, topic_id INTEGER NOT NULL,
          payload TEXT NOT NULL DEFAULT '{}', through_id INTEGER NOT NULL DEFAULT 0,
          forgotten_before INTEGER NOT NULL DEFAULT 0, updated REAL NOT NULL DEFAULT 0, next_attempt REAL NOT NULL DEFAULT 0,
          PRIMARY KEY(contact_id,sender_id,generation,topic_id));
        CREATE TABLE IF NOT EXISTS group_watchlist (
          chat_id INTEGER NOT NULL, user_id INTEGER NOT NULL, owner_id INTEGER NOT NULL,
          PRIMARY KEY(chat_id,user_id));
        CREATE TABLE IF NOT EXISTS contact_blocks (
          user_id INTEGER PRIMARY KEY, status TEXT NOT NULL, reason TEXT NOT NULL,
          source_chat_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL,
          created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS contact_warnings (
          user_id INTEGER PRIMARY KEY, source_chat_id INTEGER NOT NULL, source_message_id INTEGER NOT NULL,
          sent_message_id INTEGER NOT NULL, warned_at REAL NOT NULL, reason TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS custom_settings (
          name TEXT PRIMARY KEY, value TEXT NOT NULL, instruction TEXT NOT NULL,
          scope TEXT NOT NULL DEFAULT 'all', created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS pending_replies (
          contact_id INTEGER PRIMARY KEY REFERENCES contacts(id), telegram_id INTEGER NOT NULL,
          first_message_id INTEGER NOT NULL, due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          status TEXT NOT NULL DEFAULT 'pending', error TEXT DEFAULT '');
        CREATE TABLE IF NOT EXISTS cloud_fetches (
          job_id INTEGER NOT NULL REFERENCES jobs(id), url TEXT NOT NULL,
          snapshot_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
          cache_file TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', updated REAL NOT NULL,
          PRIMARY KEY(job_id,url));
        CREATE TABLE IF NOT EXISTS telegram_media_assets (
          asset_id TEXT PRIMARY KEY, document_id INTEGER NOT NULL, kind TEXT NOT NULL,
          label TEXT NOT NULL, pack TEXT NOT NULL DEFAULT '', document TEXT NOT NULL,
          source_chat INTEGER NOT NULL DEFAULT 0, source_message INTEGER NOT NULL DEFAULT 0,
          created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS publication_feeds (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, config TEXT NOT NULL,
          enabled INTEGER NOT NULL DEFAULT 1, revision INTEGER NOT NULL DEFAULT 1,
          target_id INTEGER NOT NULL, next_check REAL NOT NULL DEFAULT 0,
          error TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS publication_destinations (
          owner_id INTEGER PRIMARY KEY, target_id INTEGER NOT NULL, name TEXT NOT NULL,
          routes TEXT NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS publication_policies (
          owner_id INTEGER NOT NULL, target_id INTEGER NOT NULL, settings TEXT NOT NULL,
          updated REAL NOT NULL, PRIMARY KEY(owner_id,target_id));
        CREATE TABLE IF NOT EXISTS owner_clarifications (
          contact_id INTEGER PRIMARY KEY, actor_id INTEGER NOT NULL, request_id INTEGER NOT NULL,
          question TEXT NOT NULL, context_generation INTEGER NOT NULL, expires REAL NOT NULL,
          request_text TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS publication_source_cursors (
          feed_id INTEGER NOT NULL REFERENCES publication_feeds(id), source TEXT NOT NULL,
          peer_id INTEGER NOT NULL, last_id INTEGER NOT NULL, updated REAL NOT NULL,
          PRIMARY KEY(feed_id,source));
        CREATE TABLE IF NOT EXISTS publication_items (
          id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id INTEGER NOT NULL REFERENCES publication_feeds(id),
          source TEXT NOT NULL, url TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL,
          fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'new',
          published REAL NOT NULL, observed REAL NOT NULL, UNIQUE(feed_id,fingerprint));
        CREATE TABLE IF NOT EXISTS publication_posts (
          id INTEGER PRIMARY KEY AUTOINCREMENT, feed_id INTEGER NOT NULL REFERENCES publication_feeds(id),
          feed_revision INTEGER NOT NULL, target_id INTEGER NOT NULL, text TEXT NOT NULL,
          source_ids TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'draft',
          telegram_id INTEGER, error TEXT NOT NULL DEFAULT '', created REAL NOT NULL,
          dispatched REAL NOT NULL DEFAULT 0, UNIQUE(feed_id,fingerprint));
        CREATE TABLE IF NOT EXISTS telegram_action_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT NOT NULL, focus_id INTEGER NOT NULL DEFAULT 0,
          generation INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'planning',
          reason TEXT NOT NULL DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS telegram_actions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL REFERENCES telegram_action_runs(id),
          position INTEGER NOT NULL, action TEXT NOT NULL, category TEXT NOT NULL, args TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'planned', result TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
          created REAL NOT NULL, dispatched REAL NOT NULL DEFAULT 0, UNIQUE(run_id,position));
        """)
        self.add_columns("contacts", {"gift_allowed": "INTEGER DEFAULT 0", "gift_next": "REAL DEFAULT 0",
                         "birthday": "TEXT DEFAULT ''", "notes": "TEXT DEFAULT ''",
                         "persona": "TEXT DEFAULT ''", "hermes_allowed": "INTEGER DEFAULT 0"})
        self.add_columns('contacts', {'calls_allowed': 'INTEGER NOT NULL DEFAULT 1'})
        self.add_columns('telegram_media_assets', {'description': "TEXT NOT NULL DEFAULT ''"})
        self.add_columns('publication_feeds', {'owner_id':'INTEGER NOT NULL DEFAULT 0'})
        self.add_columns('publication_items', {'payload':"TEXT NOT NULL DEFAULT '{}'",'content_hash':"TEXT NOT NULL DEFAULT ''"})
        self.add_columns('publication_posts', {'topic_id':'INTEGER NOT NULL DEFAULT 0'})
        self.add_columns("tasks", {"repeat_hours": "INTEGER DEFAULT 0",'topic_id':'INTEGER NOT NULL DEFAULT 0','manual_owner_id':'INTEGER NOT NULL DEFAULT 0'})
        self.add_columns("jobs", {"artifact": "TEXT DEFAULT ''"})
        self.add_columns("jobs", {"provider": "TEXT DEFAULT 'hermes'", "details": "TEXT DEFAULT ''"})
        self.add_columns('jobs',{'request_details':"TEXT DEFAULT ''",'attempts':'INTEGER DEFAULT 0','retry_due':'REAL DEFAULT 0',
                                'last_error':"TEXT DEFAULT ''",'error_source':"TEXT DEFAULT ''"})
        self.add_columns("custom_settings", {"scope": "TEXT NOT NULL DEFAULT 'all'"})
        self.add_columns("contacts", {"kind": "TEXT DEFAULT 'private'"})
        self.add_columns("messages", {"sender_id": "INTEGER", "sender_name": "TEXT DEFAULT ''",
                         "image_path": "TEXT DEFAULT ''", "image_note": "TEXT DEFAULT ''",
                         "image_check": "TEXT DEFAULT ''", "image_kind": "TEXT DEFAULT 'photo'"})
        self.add_columns("messages", {"reply_to_telegram_id": "INTEGER"})
        self.add_columns("messages", {"forwarded": "INTEGER DEFAULT 0"})
        for table in ['messages', 'jobs', 'tasks']:
            self.add_columns(table, {'context_sender_id': 'INTEGER NOT NULL DEFAULT 0',
                                     'context_generation': 'INTEGER NOT NULL DEFAULT 0'})
        self.db.execute("UPDATE owner_actions SET status='uncertain' WHERE status='sending'")
        self.db.executescript("""
        CREATE INDEX IF NOT EXISTS idx_messages_context
          ON messages(contact_id, context_sender_id, context_generation, id DESC);
        CREATE INDEX IF NOT EXISTS idx_group_messages_topic
          ON group_messages(chat_id, generation, topic_id, telegram_id DESC);
        CREATE INDEX IF NOT EXISTS idx_publication_items_feed_status
          ON publication_items(feed_id, status, id DESC);
        CREATE INDEX IF NOT EXISTS idx_publication_posts_feed
          ON publication_posts(feed_id, created DESC);
        CREATE INDEX IF NOT EXISTS idx_telegram_action_runs_source
          ON telegram_action_runs(source, id DESC);
        """)
        self.db.execute("UPDATE group_admin_actions SET status='uncertain' WHERE status='sending'")
        self.db.execute("UPDATE group_warning_events SET status='uncertain' WHERE status='sending'")
        self.db.execute("UPDATE group_warning_events SET ban_status='uncertain' WHERE ban_status='sending'")
        self.db.execute("UPDATE group_join_automation SET status='uncertain',enabled=0 WHERE status='sending'")
        self.db.execute("UPDATE group_deleted_automation SET status='uncertain',enabled=0 WHERE status='sending'")
        self.db.execute("UPDATE group_initiatives SET status='uncertain' WHERE status='dispatch'")
        self.db.execute("UPDATE contact_blocks SET status='uncertain' WHERE status IN ('pending','unblocking')")
        self.db.execute("UPDATE tasks SET status='uncertain', error='Запуск прерван во время отправки' WHERE status='sending'")
        self.db.execute("UPDATE gift_orders SET status='uncertain', error='Проверьте доставку после перезапуска' WHERE status IN ('reserved','sending')")
        self.db.execute("UPDATE jobs SET status='interrupted', result='Задача прервана перезапуском' WHERE status IN ('pending','running') AND provider!='brightdata'")
        self.db.execute("UPDATE jobs SET status='pending' WHERE status='running' AND provider='brightdata'")
        self.db.execute("UPDATE cloud_fetches SET status='uncertain',error='Запуск страницы прерван до подтверждения; автоматического повтора нет' WHERE status='submitting'")
        self.db.execute("UPDATE pending_replies SET status='uncertain' WHERE status='dispatch'")
        self.db.execute("UPDATE pending_replies SET status='pending' WHERE status='running'")
        self.db.execute("UPDATE publication_posts SET status='uncertain',error='Запуск прерван: проверь Telegram перед новым постом' WHERE status='dispatch'")
        self.db.execute("UPDATE telegram_actions SET status='uncertain',error='Запуск прерван; проверьте результат в Telegram' WHERE status='dispatch'")
        self.db.execute("UPDATE telegram_action_runs SET status='interrupted' WHERE status IN ('planning','running')")
        self.db.commit()
        (directory / "alisa.sqlite").chmod(0o600)
        self.secret_path = directory / "secrets.json"
        if not self.secret_path.exists():
            private_json(self.secret_path, {})
        if self.settings().get("character_version", 0) < 2:
            def rename(value):
                forms = {"алиса": "Дарья", "алисы": "Дарьи", "алисе": "Дарье", "алису": "Дарью", "алисой": "Дарьей"}
                return re.sub(r"\bАлис(?:а|ы|е|у|ой)\b", lambda m: forms[m[0].lower()], value, flags=re.I)
            saved = self.settings()
            self.update_settings({"persona": rename(saved["persona"]), "gift_message": rename(saved["gift_message"]),
                                  "character_version": 2})
        self.purge_location_memories()

    def close(self):
        self.db.close()

    def add_columns(self, table, fields):
        existing = {row["name"] for row in self.rows("PRAGMA table_info(" + table + ")")}
        for key, declaration in fields.items():
            if key not in existing:
                self.db.execute(f"ALTER TABLE {table} ADD COLUMN {key} {declaration}")

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]

    def execute(self, sql, args=()):
        with self.db:
            return self.db.execute(sql, args)

    def settings(self):
        return DEFAULTS | {r["key"]: json.loads(r["value"]) for r in self.rows("SELECT * FROM settings")}

    def user_blocked(self, user_id):
        if not user_id or user_id == self.settings()['owner_id']:
            return False
        return bool(self.rows("SELECT 1 FROM contact_blocks WHERE user_id=? AND status IN ('pending','blocked','uncertain','unblocking')", (user_id,)))

    def dialog_blocked(self, contact_id, sender_id=None):
        if contact_id > 0:
            return self.user_blocked(contact_id)
        scope = current_dialog.get()
        user_id = sender_id or (scope[1] if scope and scope[0] == contact_id else None)
        return self.user_blocked(user_id)

    def update_settings(self, values):
        saved = self.settings()
        if any((key.startswith(('telegram_autonomy_','private_initiative_')) or key in {'telegram_auto_answer_calls','enabled'}) and saved.get(key)!=value for key,value in values.items()):
            values = values | {'telegram_autonomy_revision': saved['telegram_autonomy_revision']+1}
        previous_persona = saved['persona'] if 'persona' in values else None
        current = saved | values
        if current['gifts_enabled'] and not current['gift_autonomous_enabled'] and {'gifts_enabled', 'gift_autonomous_enabled'} & values.keys():
            # Enabling requested gifts must not replay requests received beforehand.
            values = values | {'gift_requests_after': time.time()}
        with self.db:
            for key, value in values.items():
                self.db.execute("INSERT INTO settings VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                                (key, json.dumps(value, ensure_ascii=False)))
            if previous_persona is not None and values['persona'] != previous_persona:
                # Copies of the shared character inherit its updates; distinct profiles remain explicit overrides.
                self.db.execute("UPDATE contacts SET persona='' WHERE persona=?",(previous_persona,))

    def secrets(self):
        return json.loads(self.secret_path.read_text())

    def update_secrets(self, values):
        private_json(self.secret_path, self.secrets() | values)

    def custom_settings(self, contact_id=None):
        rows = self.rows("SELECT name,value,instruction,scope FROM custom_settings ORDER BY name")
        return [{"name": row["name"], "value": json.loads(row["value"]), "instruction": row["instruction"], "scope":row["scope"]}
                for row in rows if contact_id is None or row["scope"] == "all" or contact_id == self.settings()["owner_id"]]

    def contact(self, contact_id):
        rows = self.rows("SELECT * FROM contacts WHERE id=?", (contact_id,))
        if not rows:
            raise ValueError("Контакт не найден")
        return rows[0]

    def upsert_contact(self, contact_id, name, username="", kind="private"):
        self.execute("""INSERT INTO contacts(id,name,username,kind) VALUES (?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET name=excluded.name, username=excluded.username,kind=excluded.kind""",
                     (contact_id, name, username, kind))
        return self.contact(contact_id)

    def message(self, contact_id, role, text, telegram_id=None, kind="chat", now=None,
                sender_id=None, sender_name="", image_path="", reply_to_telegram_id=None, forwarded=False, image_kind="photo"):
        now = time.time() if now is None else now
        scope = self.dialog_scope(contact_id, sender_id=sender_id if role == "user" else None)
        with self.db:
            cursor = self.db.execute("INSERT OR IGNORE INTO messages(contact_id,telegram_id,role,text,created,kind,sender_id,sender_name,image_path,reply_to_telegram_id,forwarded,image_kind,context_sender_id,context_generation) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                     (contact_id, telegram_id, role, text, now, kind, sender_id, sender_name, image_path, reply_to_telegram_id, int(forwarded),image_kind,scope[1],scope[2]))
            if cursor.rowcount:
                self.db.execute("UPDATE contacts SET last_activity=? WHERE id=?", (now, contact_id))
                if self.contact(contact_id)['kind'] == 'group' and type(telegram_id) is int:
                    self.group_message(contact_id, telegram_id, text, sender_id=sender_id, sender_name=sender_name,
                                       reply_to=reply_to_telegram_id, topic_id=self.group_topic(contact_id,reply_to_telegram_id),
                                       outgoing=role=='assistant', forwarded=forwarded, created=now)
                return cursor.lastrowid
        return None

    def reply_mode(self, contact_id, sender_id):
        generation = self.dialog_scope(contact_id, sender_id)[2]
        rows = (self.rows('SELECT mode FROM reply_modes WHERE contact_id=? AND sender_id=?', (contact_id, sender_id)) if generation == 0 else
                self.rows('SELECT mode FROM dialog_reply_modes WHERE contact_id=? AND sender_id=? AND generation=?', (contact_id,sender_id,generation)))
        return rows[0]['mode'] if rows else 'text'

    def set_reply_mode(self, contact_id, sender_id, mode):
        if mode not in {'text', 'voice'} or type(sender_id) is not int or sender_id <= 0:
            raise ValueError('Недопустимый формат ответа')
        generation = self.dialog_scope(contact_id, sender_id)[2]
        if generation:
            self.execute('INSERT INTO dialog_reply_modes VALUES (?,?,?,?,?) ON CONFLICT(contact_id,sender_id,generation) DO UPDATE SET mode=excluded.mode,updated=excluded.updated',
                         (contact_id,sender_id,generation,mode,time.time()))
            return
        self.execute('INSERT INTO reply_modes(contact_id,sender_id,mode,updated) VALUES (?,?,?,?) '
                     'ON CONFLICT(contact_id,sender_id) DO UPDATE SET mode=excluded.mode,updated=excluded.updated',
                     (contact_id, sender_id, mode, time.time()))

    def history(self, contact_id, limit=40):
        return self.rows("SELECT * FROM (SELECT * FROM messages WHERE contact_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
                         (contact_id, limit))

    def group_message(self, chat_id, message_id, text, *, sender_id=None, sender_name='', username='',
                      reply_to=None, topic_id=0, outgoing=False, forwarded=False, created=None):
        if type(message_id) is not int or message_id <= 0 or chat_id >= 0:
            return
        generation = self.dialog_scope(chat_id, 0)[2]
        self.execute('''INSERT INTO group_messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(chat_id,telegram_id,generation) DO UPDATE SET
            sender_id=COALESCE(excluded.sender_id,group_messages.sender_id),
            sender_name=CASE WHEN excluded.sender_name!='' THEN excluded.sender_name ELSE group_messages.sender_name END,
            username=CASE WHEN excluded.username!='' THEN excluded.username ELSE group_messages.username END,
            text=excluded.text,reply_to=COALESCE(excluded.reply_to,group_messages.reply_to),
            topic_id=CASE WHEN excluded.topic_id!=0 THEN excluded.topic_id ELSE group_messages.topic_id END,
            outgoing=excluded.outgoing,forwarded=excluded.forwarded''',
            (chat_id,message_id,generation,sender_id,sender_name,username,str(text)[:8000],reply_to,topic_id or 0,
             int(outgoing),int(forwarded),time.time() if created is None else created))

    def group_topic(self, chat_id, message_id):
        if not message_id:
            return 0
        rows = self.rows('SELECT topic_id FROM group_messages WHERE chat_id=? AND telegram_id=? AND generation=?',
                        (chat_id,message_id,self.dialog_scope(chat_id,0)[2]))
        return rows[0]['topic_id'] if rows else 0

    def group_history(self, chat_id, *, topic_id=None, before=None, limit=80):
        generation = self.dialog_scope(chat_id, 0)[2]
        where = 'chat_id=? AND generation=?'
        args = [chat_id,generation]
        if topic_id is not None:
            where += ' AND topic_id=?';args.append(topic_id or 0)
        if before is not None:
            where += ' AND telegram_id<=?';args.append(before)
        return list(reversed(self.rows('SELECT * FROM group_messages WHERE '+where+' ORDER BY telegram_id DESC LIMIT ?',
                                       (*args,limit))))

    def merge_group_threads(self, chat_id):
        """Repair ordinary reply threads previously mistaken for forum topics."""
        with self.db:
            self.db.execute('UPDATE group_messages SET topic_id=0 WHERE chat_id=? AND topic_id!=0',(chat_id,))
            for table in ('group_admin_context','group_admin_pending'):
                rows = self.rows('SELECT topic_id FROM '+table+' WHERE chat_id=? ORDER BY updated DESC,topic_id ASC',(chat_id,))
                if any(row['topic_id'] for row in rows):
                    newest = rows[0]['topic_id']
                    self.db.execute('DELETE FROM '+table+' WHERE chat_id=? AND topic_id!=?',(chat_id,newest))
                    self.db.execute('UPDATE '+table+' SET topic_id=0 WHERE chat_id=?',(chat_id,))
            for table in ('controller_group_context','controller_group_pending'):
                actors = self.rows('SELECT DISTINCT actor_id FROM '+table+' WHERE chat_id=?',(chat_id,))
                for actor in actors:
                    rows = self.rows('SELECT topic_id FROM '+table+' WHERE chat_id=? AND actor_id=? ORDER BY updated DESC,topic_id ASC',(chat_id,actor['actor_id']))
                    if any(row['topic_id'] for row in rows):
                        self.db.execute('DELETE FROM '+table+' WHERE chat_id=? AND actor_id=? AND topic_id!=?',(chat_id,actor['actor_id'],rows[0]['topic_id']))
                        self.db.execute('UPDATE '+table+' SET topic_id=0 WHERE chat_id=? AND actor_id=?',(chat_id,actor['actor_id']))

    def group_participants(self, chat_id):
        rows = self.rows('''SELECT sender_id,sender_name,username,MAX(telegram_id) AS latest FROM group_messages
            WHERE chat_id=? AND generation=? AND sender_id>0
            GROUP BY sender_id,sender_name,username ORDER BY latest DESC LIMIT 150''',
            (chat_id,self.dialog_scope(chat_id,0)[2]))
        people = {}
        for row in rows:
            person = people.setdefault(row['sender_id'],dict(user_id=row['sender_id'],names=[],usernames=[]))
            if row['sender_name'] and row['sender_name'] not in person['names']:
                person['names'].append(row['sender_name'])
            if row['username'] and row['username'] not in person['usernames']:
                person['usernames'].append(row['username'])
        return list(people.values())

    def group_chain(self, chat_id, message_id, limit=8):
        generation = self.dialog_scope(chat_id,0)[2]
        chain, seen = [], set()
        while message_id and message_id not in seen and len(chain)<limit:
            seen.add(message_id)
            rows = self.rows('SELECT * FROM group_messages WHERE chat_id=? AND generation=? AND telegram_id=?',
                             (chat_id,generation,message_id))
            if not rows:
                break
            chain.append(rows[0]);message_id=rows[0]['reply_to']
        return chain

    def dialog_scope(self, contact_id, sender_id=None):
        active = current_dialog.get()
        if sender_id is None and active and active[0] == contact_id:
            return active
        contacts = self.rows('SELECT kind FROM contacts WHERE id=?', (contact_id,))
        contact = contacts[0] if contacts else {'kind':'private'}
        if contact['kind'] != 'group':
            sender_id = 0
        elif sender_id is None:
            rows = self.rows("SELECT sender_id FROM messages WHERE contact_id=? AND role='user' ORDER BY id DESC LIMIT 1", (contact_id,))
            sender_id = (rows[0]['sender_id'] if rows else 0) or 0
        rows = self.rows('SELECT generation FROM dialog_contexts WHERE contact_id=? AND sender_id=?', (contact_id, sender_id))
        return contact_id, sender_id, rows[0]['generation'] if rows else 0

    def scope_active(self, scope):
        return self.dialog_scope(scope[0], sender_id=scope[1]) == tuple(scope)

    def context_rows(self, table, contact_id, *, sender_id=None, order='id DESC', limit=100):
        if table not in {'messages', 'jobs', 'tasks'} or order not in {'id DESC', 'id ASC', 'created DESC', 'due ASC'}:
            raise ValueError('Недопустимый запрос контекста')
        scope = self.dialog_scope(contact_id, sender_id)
        return self.rows('SELECT * FROM '+table+' WHERE contact_id=? AND context_sender_id=? AND context_generation=? ORDER BY '+order+' LIMIT ?', (*scope, limit))

    def context_history(self, contact_id, limit=40, *, before=None):
        if before is not None:
            scope = self.dialog_scope(contact_id)
            return list(reversed(self.rows('SELECT * FROM messages WHERE contact_id=? AND context_sender_id=? AND context_generation=? AND id<=? ORDER BY id DESC LIMIT ?',
                                           (*scope,before,limit))))
        return list(reversed(self.context_rows('messages', contact_id, limit=limit)))

    def context_memories(self, contact_id):
        scope = self.dialog_scope(contact_id)
        if scope[1:] == (0, 0):
            return self.rows('SELECT key,value,created FROM memories WHERE contact_id=? ORDER BY created', (contact_id,))
        return self.rows('SELECT key,value,created FROM dialog_memories WHERE contact_id=? AND sender_id=? AND generation=? ORDER BY created', scope)

    def all_memories(self, contact_id):
        legacy = [row | {'sender_id': 0, 'generation': 0} for row in self.rows('SELECT key,value,created FROM memories WHERE contact_id=?', (contact_id,))]
        scoped = self.rows('SELECT key,value,created,sender_id,generation FROM dialog_memories WHERE contact_id=?', (contact_id,))
        return sorted(legacy + scoped, key=lambda row: row['created'], reverse=True)

    def remember(self, contact_id, key, value):
        if has_location(key + ' ' + value, self.location_cities(contact_id)):
            raise ValueError('Города и геоданные не сохраняются в память')
        scope = self.dialog_scope(contact_id)
        if scope[1:] == (0, 0):
            self.execute("INSERT INTO memories VALUES (?,?,?,?) ON CONFLICT(contact_id,key) DO UPDATE SET value=excluded.value,created=excluded.created", (contact_id,key,value,time.time()))
        else:
            self.execute('INSERT INTO dialog_memories VALUES (?,?,?,?,?,?) ON CONFLICT(contact_id,sender_id,generation,key) DO UPDATE SET value=excluded.value,created=excluded.created', (*scope,key,value,time.time()))

    def forget_memory(self, contact_id, key):
        scope = self.dialog_scope(contact_id)
        if scope[1:] == (0, 0):
            self.execute('DELETE FROM memories WHERE contact_id=? AND key=?', (contact_id,key))
        else:
            self.execute('DELETE FROM dialog_memories WHERE contact_id=? AND sender_id=? AND generation=? AND key=?', (*scope,key))

    def archive_contexts(self):
        """One explicit reset: retain old records, advance each person's namespace."""
        now = time.time()
        scopes = {(row['id'],0) for row in self.rows('SELECT id FROM contacts')}
        scopes.update((row['contact_id'],row['context_sender_id']) for row in self.rows('SELECT DISTINCT contact_id,context_sender_id FROM messages'))
        scopes.update((row['contact_id'],row['sender_id']) for row in self.rows('SELECT DISTINCT contact_id,sender_id FROM dialog_contexts'))
        scopes.update((row['contact_id'],row['sender_id']) for row in self.rows("SELECT DISTINCT contact_id,sender_id FROM messages WHERE sender_id IS NOT NULL AND contact_id IN (SELECT id FROM contacts WHERE kind='group')"))
        with self.db:
            for contact_id,sender_id in scopes:
                previous = self.dialog_scope(contact_id,sender_id)[2]
                self.db.execute('INSERT INTO dialog_contexts VALUES (?,?,?,?) ON CONFLICT(contact_id,sender_id) DO UPDATE SET generation=excluded.generation,started=excluded.started', (contact_id,sender_id,previous+1,now))
            for contact in self.rows('SELECT * FROM contacts'):
                self.db.execute('INSERT INTO context_archives(contact_id,generation,profile,created) VALUES (?,?,?,?)',
                    (contact['id'],self.dialog_scope(contact['id'],0)[2]-1,json.dumps({key:contact[key] for key in ['notes','birthday','persona']},ensure_ascii=False),now))
            self.db.execute("UPDATE contacts SET notes='',birthday='',persona=''")
            self.db.execute('UPDATE contacts SET last_activity=0,next_proactive=?', (now+86400,))
            self.db.execute("UPDATE tasks SET status='archived' WHERE status='pending'")
            self.db.execute("UPDATE jobs SET status='archived' WHERE status IN ('pending','running')")
            self.db.execute('DELETE FROM pending_replies')
        return {'dialogs':len(scopes),'messages_retained':self.rows('SELECT COUNT(*) AS n FROM messages')[0]['n'],
                'memories_retained':self.rows('SELECT COUNT(*) AS n FROM memories')[0]['n']+self.rows('SELECT COUNT(*) AS n FROM dialog_memories')[0]['n']}

    def record_scope(self, row):
        return (row['contact_id'] or 0, row['context_sender_id'], row['context_generation'])

    def context_record_active(self, row):
        return self.scope_active(self.record_scope(row))

    def archive_rows(self, table, contact_id, limit=100):
        if table not in {'messages','jobs','tasks'}:
            raise ValueError('Недопустимый архив')
        return [row | {'archived':not self.context_record_active(row)} for row in
                self.rows('SELECT * FROM '+table+' WHERE contact_id=? ORDER BY id DESC LIMIT ?', (contact_id,limit))]

    def location_cities(self, contact_id):
        cities = set()
        for row in self.context_rows('jobs', contact_id, limit=1000):
            try:
                city = json.loads(row['request_details'] or row['details']).get('filters', {}).get('city_slug')
                if isinstance(city, str) and city not in {'all', 'rossiya'}:
                    cities.add(city)
            except (ValueError, AttributeError):
                continue
        return cities

    def purge_location_memories(self, contact_id=None):
        rows = self.rows('SELECT contact_id,key,value FROM memories' + (' WHERE contact_id=?' if contact_id is not None else ''),
                         (contact_id,) if contact_id is not None else ())
        removed = 0
        with self.db:
            for row in rows:
                if has_location(row['key'] + ' ' + row['value'], self.location_cities(row['contact_id'])):
                    removed += self.db.execute('DELETE FROM memories WHERE contact_id=? AND key=?',
                                               (row['contact_id'], row['key'])).rowcount
        return removed

    def schedule(self, contact_id, text, due, *, topic_id=0,manual_owner_id=0):
        self.contact(contact_id)
        scope = self.dialog_scope(contact_id)
        return self.execute("INSERT INTO tasks(contact_id,text,due,created,context_sender_id,context_generation,topic_id,manual_owner_id) VALUES (?,?,?,?,?,?,?,?)",
                            (contact_id, text, due, time.time(),scope[1],scope[2],topic_id,manual_owner_id)).lastrowid

    def claim_task(self, task_id):
        return self.execute("UPDATE tasks SET status='sending' WHERE id=? AND status='pending'", (task_id,)).rowcount == 1

    def event(self, text, level="info"):
        self.execute("INSERT INTO events(text,created,level) VALUES (?,?,?)", (text, time.time(), level))
        self.execute("DELETE FROM events WHERE id < (SELECT COALESCE(MAX(id),0)-1000 FROM events)")
