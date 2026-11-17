"""Public replies requested by a controller, bound to this conversation."""
from __future__ import annotations

import re
import time

from .direct_messages import USER, DESTINATION, body_text


def parse_group_reply(text):
    text = re.sub(r'^(?:(?:дарья|даш[а-яё]*)[,\s]+|пожалуйста[,\s]+|(?:а\s+)?теперь\s+)+', '', text.strip(), flags=re.I)
    text = re.sub(r'^(?:можешь\s+(?:пожалуйста\s+)?|давай\s+)', '', text, flags=re.I)
    match = re.fullmatch(rf'(?P<verb>скажи|скажешь|ответь|ответишь|напиши|напишешь|передай|объясни|расскажи|сообщи)\s+'
                        rf'(?P<user>ему|ей|им|этому|этой|{USER})\s*(?P<body>.*)', text, re.I | re.S)
    if not match:
        return None
    body = match['body'].strip()
    if re.match(rf'^{DESTINATION}(?:\b|\s|:)', body, re.I):
        return None
    if re.match(r'^в\s+(?:видеочате|звонке|голосовом\s+чате)\b', body, re.I):
        return None
    user = match['user']
    # Preserve the existing shorthand for an exact private recipient. A request
    # to say/reply to someone has public conversation semantics instead.
    if re.fullmatch(USER, user) and match['verb'].casefold() in {'напиши', 'напишешь'}:
        return None
    body = re.sub(r'^(?:здесь|тут|в\s+(?:этой\s+)?группе|в\s+(?:этом\s+)?чате)\s*[:—,-]?\s*', '', body, flags=re.I)
    literal = body.startswith((':','«','"'))
    compose = not literal and bool(re.match(r'^(?:что[\s-]+(?:то|нибудь|либо)\b|как\b|почему\b|про\b|о\b|об\b)',body,re.I))
    if literal:
        body = re.sub(r'^\s*[:—,-]?\s*', '', body)
    elif not compose:
        body = re.sub(r'^\s*[:—,-]?\s*(?:что\s+|мол\s+)?', '', body, flags=re.I)
    return {'action': 'reply_in_group', 'user': user if re.fullmatch(USER, user) else 'reply',
            'body': body_text(body), **({'compose':True} if compose else {})}


def reply_target(store, chat_id, topic, actor_id, self_id, before, *, reply_id=None, user=None, message_id=None):
    generation = store.dialog_scope(chat_id, 0)[2]

    def row(id):
        rows = store.rows('SELECT * FROM group_messages WHERE chat_id=? AND topic_id=? AND generation=? AND telegram_id=? AND telegram_id<?',
                          (chat_id, topic, generation, id, before))
        return rows[0] if rows else None

    def matches(target):
        return (user in {None, 'reply', 'recent'} or user == str(target['sender_id']) or
                user.casefold() == ('@'+target['username']).casefold())

    def follow(id):
        seen = set()
        for _ in range(8):
            if not id or id in seen:
                return None
            seen.add(id)
            target = row(id)
            if not target:
                return None
            if (not target['outgoing'] and target['sender_id'] and target['sender_id'] > 0
                    and target['sender_id'] not in {actor_id, self_id}):
                return target if matches(target) else None
            id = target['reply_to']
        return None

    # A real reply binds the target more strongly than any inferred history.
    if reply_id:
        target = follow(reply_id)
        if target:
            if message_id and message_id != target['telegram_id']:
                raise ValueError('Указанное сообщение не совпадает с текущим ответом')
            return target
        raise ValueError('Неясно, кому ответить. Ответь на нужное сообщение в этой теме')
    if message_id:
        target = follow(message_id)
        if target and target['telegram_id'] == message_id:
            return target
        raise ValueError('Сообщение не найдено в текущей теме и контексте')

    # Continue the authenticated actor's latest explicit conversation thread,
    # never the last moderation target or another participant's instructions.
    turns = store.rows('SELECT * FROM group_messages WHERE chat_id=? AND topic_id=? AND generation=? '
                       'AND sender_id=? AND outgoing=0 AND forwarded=0 AND telegram_id<? AND created>? '
                       'AND reply_to IS NOT NULL ORDER BY telegram_id DESC LIMIT 1',
                       (chat_id, topic, generation, actor_id, before, time.time()-1800))
    if turns:
        target = follow(turns[0]['reply_to'])
        if target:
            return target
    if user not in {None, 'reply', 'recent'}:
        for target in reversed(store.group_history(chat_id, topic_id=topic, before=before-1)):
            if (not target['outgoing'] and target['sender_id'] and target['sender_id'] > 0
                    and target['sender_id'] not in {actor_id, self_id} and matches(target)):
                return target
    raise ValueError('Неясно, кому ответить в группе. Ответь на нужное сообщение или укажи @username')
