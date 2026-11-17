"""Variable, persistent pacing for continuing individual conversations."""
from datetime import datetime
import random
import time
from zoneinfo import ZoneInfo

from .session_guard import SECURITY_PEER


COMMUNICATIONS = frozenset({'send_text', 'send_voice', 'send_sticker', 'send_gif',
                            'send_media', 'poll', 'call', 'schedule'})
SOURCES = ('autonomous', 'private_initiative')


class PrivateInitiative:
    def __init__(self, runtime):
        self.runtime, self.store = runtime, runtime.store

    def interval(self):
        s = self.store.settings()
        return random.uniform(s['private_initiative_min_minutes'], s['private_initiative_max_minutes']) * 60

    def activity(self, contact_id):
        scope = self.store.dialog_scope(contact_id, 0)
        rows = self.store.rows('''SELECT id,created FROM messages WHERE contact_id=?
            AND context_sender_id=? AND context_generation=? ORDER BY id DESC LIMIT 1''', scope)
        return scope, rows[0] if rows else None

    def state(self, contact_id):
        scope, latest = self.activity(contact_id)
        if latest is None:
            return None
        rows = self.store.rows('SELECT * FROM private_initiatives WHERE contact_id=? AND generation=?', (contact_id, scope[2]))
        if not rows or rows[0]['activity_id'] != latest['id']:
            due = max(time.time(), latest['created']) + self.interval()
            self.store.execute('''INSERT INTO private_initiatives(contact_id,generation,activity_id,next_check)
                VALUES (?,?,?,?) ON CONFLICT(contact_id,generation) DO UPDATE SET
                activity_id=excluded.activity_id,next_check=excluded.next_check''', (contact_id,scope[2],latest['id'],due))
            rows = self.store.rows('SELECT * FROM private_initiatives WHERE contact_id=? AND generation=?', (contact_id,scope[2]))
        return rows[0]

    def pacing(self, contact_id, *, exclude_dispatch=0):
        s = self.store.settings()
        scope = self.store.dialog_scope(contact_id, 0)
        human = self.store.rows('''SELECT id,created FROM messages WHERE contact_id=? AND context_sender_id=?
            AND context_generation=? AND role='user' AND forwarded=0 AND sender_id=?
            ORDER BY id DESC LIMIT 1''', (*scope,contact_id))
        if not human:
            return {'allowed':False, 'reason':'Нет личного разговора в текущем контексте', 'unanswered':0, 'daily_used':0}
        actions = self.store.rows('''SELECT a.id,a.dispatched,a.status FROM telegram_actions a
            JOIN telegram_action_runs r ON r.id=a.run_id WHERE r.focus_id=? AND r.generation=?
            AND r.source IN ('autonomous','private_initiative') AND a.dispatched>0 AND a.id!=?
            AND a.action IN ('send_text','send_voice','send_sticker','send_gif','send_media','poll','call','schedule')''',
            (contact_id,scope[2],exclude_dispatch))
        midnight = datetime.now(ZoneInfo(s['timezone'])).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
        unanswered = [a for a in actions if a['dispatched'] >= human[0]['created'] and a['status'] not in {'failed','blocked'}]
        used = sum(a['dispatched'] >= midnight for a in actions)
        reason = ''
        if any(a['status'] in {'uncertain','dispatch'} for a in unanswered):
            reason = 'Предыдущее обращение не подтверждено; ждёт ответа человека'
        elif len(unanswered) >= s['private_initiative_max_unanswered']:
            reason = 'Ждёт ответа на предыдущие обращения'
        elif used >= s['private_initiative_daily_limit']:
            reason = 'Дневной лимит инициативы для этого человека'
        elif actions and max(a['dispatched'] for a in actions)+s['private_initiative_min_minutes']*60 > time.time():
            reason = 'Пауза после предыдущего обращения'
        return {'allowed':not reason,'reason':reason,'unanswered':len(unanswered),'daily_used':used,'last_human_id':human[0]['id']}

    def eligible(self, contact_id, *, exclude_dispatch=0):
        c = self.store.contact(contact_id)
        if (contact_id in {SECURITY_PEER,self.runtime.telegram.account_id} or contact_id<=0 or c['kind']!='private'
                or not c['enabled'] or not c['proactive'] or self.store.dialog_blocked(contact_id)):
            return False
        if contact_id in self.runtime.incoming or self.store.rows("SELECT 1 FROM pending_replies WHERE contact_id=? AND status IN ('pending','generating','dispatch')",(contact_id,)) or self.runtime.calls.active:
            return False
        if not self.pacing(contact_id,exclude_dispatch=exclude_dispatch)['allowed']:
            return False
        # During the actual dispatch, this run has already claimed its scheduled slot.
        if exclude_dispatch:
            return True
        state = self.state(contact_id)
        return bool(state and state['next_check']<=time.time())

    def postpone(self, contact_id):
        state = self.state(contact_id)
        if state:
            self.store.execute('UPDATE private_initiatives SET next_check=? WHERE contact_id=? AND generation=?',
                (time.time()+self.interval(),contact_id,state['generation']))

    def status(self):
        s = self.store.settings()
        contacts = self.store.rows("SELECT id,name FROM contacts WHERE kind='private' AND enabled=1 AND proactive=1 AND id!=? AND id!=?", (SECURITY_PEER,self.runtime.telegram.account_id or 0))
        return {'enabled':s['private_initiative_enabled'],'contacts':[
            {'id':c['id'],'name':c['name'],**self.pacing(c['id']),
             'next_check':(self.state(c['id']) or {}).get('next_check')} for c in contacts if not self.store.dialog_blocked(c['id'])]}

    def tick(self):
        s = self.store.settings()
        auto = self.runtime.telegram_autonomy
        if (not s['private_initiative_enabled'] or not s['proactive_enabled'] or not s['telegram_autonomy_messages']
                or not auto.allowed() or auto.worker and not auto.worker.done()):
            return
        for c in self.store.rows("SELECT id FROM contacts WHERE kind='private' AND enabled=1 AND proactive=1 ORDER BY last_activity DESC"):
            if c['id'] in self.runtime.busy or not self.eligible(c['id']):
                continue
            auto.worker = self.runtime.spawn(auto.consider(focus=c['id'],private_initiative=True))
            break
