"""Explicit single-message requests and deterministic clarification state."""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import re


USER = r'@[A-Za-z][A-Za-z0-9_]{3,31}|[1-9][0-9]{0,19}'
PERSON = rf'(?:{USER}|ему|ей|ему же|этому|этой|его|её|ее)'
DESTINATION = r'(?:в\s+)?(?:лс|личку|личные сообщения|личном чате)'
PREFIX = 'Личное сообщение: '


@dataclass
class DirectMessage:
    user: str | None = 'reply'
    body: str | None = None
    repeated: bool = False
    use_recent: bool = False

    def pending_text(self):
        # Freeze the selected recipient before asking for missing text. Public
        # history or another administrative action cannot retarget the reply.
        return PREFIX + json.dumps({'user': self.user, 'body': self.body, 'use_recent': self.use_recent}, ensure_ascii=False)


def body_text(value):
    value = value.strip()
    for left, right in [('«', '»'), ('"', '"')]:
        if value.startswith(left) and value.endswith(right) and len(value) >= 2:
            value = value[1:-1]
            break
    return value if value.strip() else None


def parse_direct_message(text):
    text = re.sub(r'^(?:(?:дарья|даша|даш|дашка|дашенька)[,\s]+|пожалуйста[,\s]+|(?:а\s+)?теперь\s+)+', '', text.strip(), flags=re.I)
    verb = re.match(r'^(напиши|отправь(?:\s+сообщение)?|пришли(?:\s+сообщение)?|спамь|заспамь)\s+', text, re.I)
    if not verb:
        return None
    rest = text[verb.end():]
    match = re.fullmatch(rf'{DESTINATION}\s+(?P<user>{PERSON})(?:(?:\s*:\s*|\s+)(?P<body>.*))?', rest, re.I | re.S)
    if not match:
        match = re.fullmatch(rf'(?:(?P<user>{PERSON})\s+)?{DESTINATION}(?:\s*[:—-]\s*|\s+)?(?P<body>.*)', rest, re.I | re.S)
    if not match:
        match = re.fullmatch(rf'(?P<user>{USER})(?:(?:\s*:\s*|\s+)(?P<body>.*))?', rest, re.I | re.S)
    unknown_name = not match
    if unknown_name:
        # A named participant still gets a clarification; never guess a name.
        match = re.fullmatch(rf'[^\s:]+\s+{DESTINATION}(?:\s*:\s*|\s+)?(?P<body>.*)', rest, re.I | re.S)
    if not match:
        return None
    person = match.groupdict().get('user')
    user = None if unknown_name else person if person and re.fullmatch(USER, person) else 'reply'
    repeated = verb[1].casefold() in {'спамь', 'заспамь'}
    return DirectMessage(user, None if repeated else body_text(match.groupdict().get('body') or ''), repeated,
                         bool(person and user == 'reply'))


def pending_message(text):
    if not text.startswith(PREFIX):
        return None
    try:
        data = json.loads(text[len(PREFIX):])
        if (set(data) != {'user', 'body', 'use_recent'} or type(data['use_recent']) is not bool
                or data['user'] is not None and (not isinstance(data['user'],str) or not re.fullmatch(rf'{USER}|reply', data['user']))):
            return None
        if data['body'] is not None and not isinstance(data['body'], str):
            return None
        return DirectMessage(**data)
    except (ValueError, TypeError, KeyError):
        return None


def continue_message(request, text):
    # Ordinary chat, unlabelled text and bare numbers cannot authorize a send.
    text = text.strip()
    recipient = re.fullmatch(rf'(?P<user>{USER})(?:\s*:\s*(?P<body>.*))?', text, re.S)
    if recipient:
        return replace(request, user=recipient['user'],
                       body=body_text(recipient['body']) if recipient['body'] is not None else request.body)
    body = re.fullmatch(r'(?:текст|сообщение|отправь)\s*:\s*(.*)', text, re.I | re.S)
    if body:
        return replace(request, body=body_text(body[1]))
    return None
