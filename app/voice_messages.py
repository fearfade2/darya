"""Per-sender reply format and temporary Telegram voice-note encoding."""
from __future__ import annotations

import asyncio
import math
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path


class VoiceRenderUnavailable(ValueError):
    pass


VOICE = r'(?:голосов(?:ые|ых|ыми(?:\s+сообщениями)?|ое(?:\s+сообщение)?|ым(?:\s+сообщением)?|ухами|уху)|голосом|гс)'
TEXT = r'(?:текстом|текстовыми(?:\s+сообщениями)?|обычными\s+сообщениями)'


def format_request(text, *, standalone=False):
    """Return (format, persistent) for a direct current request, not quoted text."""
    if not isinstance(text, str):
        return None
    text = re.sub(r'«[^»]*»|"[^"\n]*"|\b(?:https?://)\S+', '', text.casefold())
    text = re.sub(r'\s+', ' ', text).strip().rstrip('.!?')
    if re.search(r'\b(?:в\s+(?:звонке|звонках|созвоне)|микрофон|t\.me/call/)', text):
        return None
    matches = []
    patterns = [
        (rf'\b(?:общайся|отвечай|пиши|присылай|отправляй|давай)\s+(?:(?:со\s+мной|мне|дальше|всегда|теперь|только)\s+){{0,3}}{VOICE}', 'voice', True),
        (rf'\b(?:общайся|отвечай|пиши|присылай|отправляй|давай)\s+(?:(?:со\s+мной|мне|дальше|всегда|теперь|только)\s+){{0,3}}{TEXT}', 'text', True),
        (rf'\b(?:больше\s+не|не\s+надо|не\s+хочу|перестань\s+(?:отвечать|писать))\s+{VOICE}', 'text', True),
        (rf'\bне\s+(?:присылай|отправляй|пиши|записывай|отвечай)\s+(?:мне\s+)?{VOICE}', 'text', True),
        (rf'\b{VOICE}\s+(?:мне\s+)?не\s+(?:присылай|отправляй|пиши|записывай)', 'text', True),
        (rf'\b(?:ответь|пришли|отправь|запиши|скинь|можешь\s+ответить)\s+(?:(?:мне|пожалуйста|это|на\s+это|только)\s+){{0,3}}{VOICE}', 'voice', False),
        (rf'\b(?:ответь|пришли|отправь|можешь\s+ответить)\s+(?:(?:мне|пожалуйста|это|на\s+это|только)\s+){{0,3}}{TEXT}', 'text', False),
        (rf'\b{VOICE}\s+(?:(?:мне|пожалуйста|пж|плиз|это|только)\s+){{0,3}}(?:ответь|пришли|отправь|запиши|скинь)', 'voice', False),
        (rf'\b{VOICE}\s+(?:(?:мне|дальше|всегда|теперь|только)\s+){{0,3}}(?:отвечай|присылай|отправляй|пиши)', 'voice', True),
        (rf'\b{TEXT}\s+(?:(?:мне|пожалуйста|это|только)\s+){{0,3}}(?:ответь|пришли|отправь)', 'text', False),
        (rf'\b{TEXT}\s+(?:(?:мне|дальше|всегда|теперь|только)\s+){{0,3}}(?:отвечай|присылай|отправляй|пиши)', 'text', True),
        (rf'\b(?:расскажи|скажи|объясни)\b[^.!?]{{0,300}}?\b{VOICE}', 'voice', False),
    ]
    for pattern, mode, persistent in patterns:
        for match in re.finditer(pattern + r'\b', text):
            if standalone and (not re.fullmatch(r'(?:(?:а|ну|пожалуйста|пж|плиз)[,\s]*)*', text[:match.start()])
                    or not re.fullmatch(r'[,\s]*(?:(?:пожалуйста|пж|плиз)[,\s]*)*', text[match.end():])):
                continue
            preceding = text[max(0, match.start() - 50):match.start()]
            if re.search(r'\b(?:не|никогда\s+не|он\s+сказал|она\s+сказала|пример)\s*$', preceding):
                continue
            matches.append((match.end(), mode, persistent))
    persistent_request = re.fullmatch(rf'(?:теперь|дальше|всегда|только)\s+({VOICE}|{TEXT})', text)
    if persistent_request:
        matches.append((len(text), 'text' if re.fullmatch(TEXT, persistent_request[1]) else 'voice', True))
    shorthand = re.fullmatch(rf'(?:а\s+)?(?:(?:можно|мне|пожалуйста|пж|плиз)[,\s]+){{0,3}}{VOICE}(?:[,\s]+(?:пожалуйста|пж|плиз))?', text)
    if shorthand:
        matches.append((len(text), 'voice', False))
    return max(matches)[1:] if matches else None


def reply_format(store, contact_id, messages):
    if not messages:
        return 'text'
    current = messages[-1]
    contact = store.contact(contact_id)
    sender_id = current.get('sender_id')
    # Preferences belong to the real sender, never to forwarded content or a
    # whole group because one participant asked for voice messages.
    authenticated = (type(sender_id) is int and sender_id > 0 and
                     (contact['kind'] == 'group' or sender_id == contact_id))
    mode = store.reply_mode(contact_id, sender_id) if authenticated else 'text'
    for message in messages:
        if message.get('forwarded') or message.get('sender_id') != sender_id or not authenticated:
            continue
        requested = format_request(message['text'])
        if requested:
            mode, persistent = requested
            if persistent:
                store.set_reply_mode(contact_id, sender_id, mode)
    return mode if store.settings()['voice_messages_enabled'] else 'text'


def spoken_text(text, limit):
    text = re.sub(r'https?://\S+|[*_`#]', '', text).strip()
    if len(text) <= limit:
        return text
    prefix = text[:limit]
    sentence = max(prefix.rfind('. '), prefix.rfind('! '), prefix.rfind('? '))
    if sentence >= limit // 2:
        return prefix[:sentence + 1]
    return prefix.rsplit(' ', 1)[0].rstrip(' ,;:') + '.'


@asynccontextmanager
async def voice_note(engine, directory: Path, text, speed=1, volume=1):
    try:
        pcm = await engine.synthesize(text, speed, volume, max_seconds=180)
    except Exception:
        raise VoiceRenderUnavailable('Синтез голосового ответа временно недоступен') from None
    root = directory / 'outgoing-voice'
    root.mkdir(exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix='reply-', dir=root) as temporary:
        output = Path(temporary) / 'voice.ogg'
        try:
            process = await asyncio.create_subprocess_exec('ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-f', 's16le', '-ar', '48000', '-ac', '1', '-i', 'pipe:0', '-c:a', 'libopus',
            '-b:a', '32k', '-application', 'voip', '-map_metadata', '-1', str(output),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
        except (OSError, ValueError):
            raise VoiceRenderUnavailable('Не удалось подготовить голосовое сообщение') from None
        try:
            try:
                await asyncio.wait_for(process.communicate(pcm), 25)
            except (OSError, asyncio.TimeoutError):
                raise VoiceRenderUnavailable('Кодирование голосового сообщения временно недоступно') from None
            if process.returncode or not output.is_file() or not output.stat().st_size:
                raise VoiceRenderUnavailable('Не удалось подготовить голосовое сообщение')
            output.chmod(0o600)
            yield output, math.ceil(len(pcm) / 96000)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
