from __future__ import annotations

import asyncio
import io
import json
import math
import re
import time
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import ValidationError
from telethon import errors

from .settings import Settings, ContactChanges, update_contact, validate_settings
from .vision import MAX_BYTES, normalize
from .voice import CallUnavailable
from .voice_messages import format_request
from .group_admin import AdminOperation, operation_schema
from .avito import parse_request as parse_avito_request, search_url as avito_search_url, city_slug, validate_filters
from .telegram_account import ACCOUNT_NAMES, account_tools, account_result_text, parse_account_command, telegram_error
from .authority import authorized, parse_delegation, DELEGATION_ACTIONS
from .groups import parse_navigation_command, membership_text
from .telegram_actions import action_tools
from .telegram_media import parse_media_command
from .privacy import redact_locations
from .publications import publication_tools
from .group_conversation import attention_ping


CONTROL_FIELDS = frozenset(Settings.model_fields) - {"owner_id", "ai_base_url"}
OWNER_NAMES = {"owner_settings", "configure_darya", "set_reply_speed", "set_profile_photo",
               "set_profile_details", "send_message_to", "owner_contacts", "configure_contact", "search_avito",
               "create_custom_setting", "update_custom_setting", "delete_custom_setting", "list_custom_settings", "join_group", "leave_group", "join_call", "leave_call", "set_call_mode", "call_status", "accept_call", "manage_group", "list_blocked_users", "unblock_user"}
OWNER_NAMES |= ACCOUNT_NAMES
OWNER_NAMES.add('speak_in_call')
OWNER_NAMES.add('manage_delegation')
OWNER_NAMES |= {'telegram_action','call_user','manage_publications','configure_publications'}
resolved_request = ContextVar('resolved_owner_request', default=None)
group_request = ContextVar('verified_group_control_request', default=None)

SETTING_LABELS = {
    'publishing_enabled':'Мониторинг источников и самостоятельные посты',
    'private_initiative_enabled':'Сама продолжать личные разговоры',
    'private_initiative_min_minutes':'Минимальная пауза личной инициативы, минут',
    'private_initiative_max_minutes':'Максимальная пауза личной инициативы, минут',
    'private_initiative_max_unanswered':'Обращений без ответа до паузы',
    'private_initiative_daily_limit':'Инициативных обращений одному человеку в день',
    'telegram_autonomy_enabled':'Самостоятельность в Telegram',
    'telegram_autonomy_minutes':'Как часто выбирать самостоятельное занятие, минут',
    'telegram_autonomy_daily_limit':'Самостоятельных действий в день',
    'telegram_autonomy_max_actions':'Действий в одном занятии',
    'telegram_autonomy_instructions':'Постоянные интересы и цели самостоятельности',
    'telegram_autonomy_profile':'Самостоятельно менять свой профиль и приватность',
    'telegram_autonomy_messages':'Самостоятельно писать и отправлять медиа',
    'telegram_autonomy_calls':'Самостоятельно звонить',
    'telegram_autonomy_groups':'Самостоятельно действовать в группах и каналах',
    'telegram_autonomy_stories':'Самостоятельно публиковать истории и реагировать на них',
    'telegram_autonomy_organize':'Самостоятельно организовывать чаты и Избранное',
    'telegram_autonomy_discovery':'Самостоятельно искать людей и медиа',
    'telegram_autonomy_new_contacts':'Самостоятельно начинать общение с найденными людьми',
    'telegram_autonomy_destructive':'Самостоятельно удалять и исключать',
    'telegram_auto_answer_calls':'Самостоятельно принимать входящие звонки',
    'telegram_call_cooldown_hours':'Пауза между самостоятельными звонками одному человеку, часов',
    'stickers_enabled': 'Самостоятельно выбирать и отправлять стикеры',
    'gifs_enabled': 'Самостоятельно выбирать и отправлять GIF',
    'collect_telegram_media': 'Собирать стикеры и GIF из доступных сообщений',
    "self_protection_enabled": "Самостоятельная блокировка за неприятное общение (кроме владельца)",
    "voice_persona": "Характер в звонках и голосовых сообщениях",
    "voice_speaker": "Женский голос: xenia, kseniya или baya",
    "voice_messages_enabled": "Голосовые сообщения по просьбе собеседника",
    "voice_message_speed": "Скорость голосовых сообщений", "voice_message_volume": "Громкость голосовых сообщений",
    "voice_message_max_chars": "Максимум текста голосового сообщения",
    "calls_enabled": "Звонки", "call_mode": "Режим звонка: listen / talk",
    "call_language": "Язык распознавания речи", "call_voice_speed": "Скорость голоса",
    "call_voice_volume": "Громкость голоса", "call_silence_seconds": "Пауза для завершения реплики, с",
    "call_vad_threshold": "Порог громкости речи", "call_min_speech_seconds": "Минимум речи, с",
    "call_utterance_seconds": "Максимум одной реплики, с", "call_max_minutes": "Максимум звонка, мин",
    "call_reply_to_all": "Отвечать всем репликам в звонке",
    "call_interrupt_on_speech": "Остановить голос, когда участник заговорил",
    "call_wait_for_silence": "Ждать тишины перед собственной репликой в звонке",
    "call_initiative_enabled": "Первой начинать разговор в звонке",
    "call_initiative_seconds": "Пауза перед инициативой в звонке, с",
    "call_initiative_max_unanswered": "Реплик подряд без ответа в звонке; 0 — без ограничения",
    "call_reply_max_tokens": "Объём голосового ответа модели", "call_reply_max_chars": "Длина голосового ответа",
    "typing_enabled": "Индикатор печатания", "read_receipts_enabled": "Отметка о прочтении",
    "typing_during_generation": "Печатать во время подготовки ответа",
    "typing_start_delay": "Начинать печатание через столько секунд после сообщения",
    "typing_cps": "Скорость печатания, знаков/с", "typing_min_seconds": "Минимум печатания, с",
    "typing_max_seconds": "Максимум печатания, с", "typing_pause_enabled": "Паузы при печатании",
    "typing_pause_threshold": "Пауза при печатании дольше, с", "history_messages": "Сообщений в контексте",
    "reply_max_tokens": "Объём ответа модели", "reply_max_chars": "Длина ответа, символов",
    "reply_instructions": "Правила ответов", "memory_enabled": "Память", "persona": "Характер",
    "natural_enabled": "Паузы перед прочтением и ответом", "enabled": "Автоматические ответы",
    "proactive_enabled": "Инициатива", "proactive_hours": "Интервал инициативы, ч",
    "quiet_start": "Начало тихих часов", "quiet_end": "Конец тихих часов", "timezone": "Часовой пояс",
    "hourly_limit": "Сообщений в час (0 — без лимита)", "daily_contact_limit": "Сообщений контакту в день (0 — без лимита)",
    "min_interval": "Минимальный интервал сообщений, с", "temperature": "Вариативность ответов",
    "ai_model": "Модель диалога", "vision_model": "Модель фото", "vision_enabled": "Распознавание фото",
    "hermes_enabled": "Hermes", "brightdata_enabled": "Сбор сайтов через Bright Data",
    "gifts_enabled": "Подарки", "gift_autonomous_enabled": "Подарки по собственной инициативе", "gift_max_price": "Цена подарка, Stars", "gift_monthly_budget": "Бюджет подарков в месяц, Stars",
    "gift_interval_hours": "Интервал подарков, ч", "gift_check_hours": "Проверка повода для подарка, ч", "gift_message": "Подпись подарка",
    "agent_max_rounds": "Действий модели за ответ", "split_replies": "Разделение ответа на реплики",
    "split_max_parts": "Максимум реплик", "split_max_chars": "Разделять ответы короче, символов",
    "reactions_enabled": "Реакции", "presence_enabled": "Самостоятельный онлайн",
    "online_during_reply": "В сети во время переписки",
    "reply_online_immediate": "Входить в сеть сразу при получении сообщения",
    "reply_idle_seconds": "Уходить из сети через столько секунд после ответа",
    "revise_reply_on_new_message": "Обновлять незавершённый ответ при новом сообщении",
    "group_enabled": "Ответы в группах", "group_aliases": "Имена в группах",
    "group_names_enabled": "Ответ по имени в группе", "group_mentions_enabled": "Ответ на упоминание в группе",
    "group_replies_enabled": "Ответ на реплику в группе", "burst_window": "Сбор сообщений подряд, с",
    "group_auto_reply_enabled": "Участие в беседе группы по смыслу, без имени или упоминания",
}
for prefix, label in {"read_delay": "Пауза перед прочтением, с", "reply_delay": "Пауза перед ответом, с",
                      "presence_interval": "Интервал онлайна, мин", "presence_duration": "Продолжительность онлайна, с",
                      "typing_variation": "Вариация скорости печатания", "typing_pause": "Пауза внутри печатания, с",
                      "reply_part_delay": "Пауза между репликами, с", "reaction_delay": "Пауза перед реакцией, с",
                      "proactive_variation": "Вариация интервала инициативы"}.items():
    SETTING_LABELS[prefix + "_min"] = label + " (мин.)"
    SETTING_LABELS[prefix + "_max"] = label + " (макс.)"


def describe_settings(values, changed=False):
    lines = []
    for key, value in values.items():
        shown = "включено" if value is True else "выключено" if value is False else str(value)
        if changed and key in {"persona", "voice_persona", "reply_instructions"}:
            shown = "обновлены"
        elif len(values) > 3 and isinstance(value, str) and len(value) > 300:
            shown = value[:300] + "…"
        lines.append(SETTING_LABELS.get(key, key) + ": " + shown)
    return "\n".join(lines)


def normalize_actions(actions, allowed_names=None):
    if not isinstance(actions, list) or len(actions) > 8:
        raise ValueError("Не удалось разобрать команду управления")
    normalized = []
    for action in actions:
        if not isinstance(action, dict):
            raise ValueError("Неподдерживаемая команда управления")
        name = action.get("name")
        if not isinstance(name, str) or name not in (OWNER_NAMES if allowed_names is None else allowed_names):
            raise ValueError("Неподдерживаемая команда управления")
        argument_keys = set(action) & {"args", "arguments", "parameters"}
        if argument_keys:
            if len(argument_keys) != 1 or set(action) != {"name", *argument_keys}:
                raise ValueError("Неоднозначные параметры управления")
            args = action[next(iter(argument_keys))]
        else:
            args = {key: value for key, value in action.items() if key != "name"}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                raise ValueError("Неподдерживаемые параметры управления") from None
        if not isinstance(args, dict):
            raise ValueError("Неподдерживаемые параметры управления")
        if name == "configure_darya" and args and set(args) <= CONTROL_FIELDS:
            args = {"settings": args}
        normalized.append({"name": name, "args": args})
    return normalized


def owner_tools(tool):
    fields = Settings.model_json_schema()["properties"]
    properties = {key: next(item for item in fields[key]["anyOf"] if item.get("type") != "null")
                  for key in sorted(CONTROL_FIELDS)}
    for key in properties:
        properties[key] = properties[key] | {"description": SETTING_LABELS.get(key, key)}
    return account_tools(tool) + action_tools(tool) + publication_tools(tool) + [
        tool('call_user','Позвонить пользователю по текущей команде. Настоящий исходящий личный звонок Telegram, с прослушиванием и голосовым ответом. Не является подключением к групповому голосовому чату.',
             {'recipient':{'type':'string','maxLength':64},'mode':{'type':'string','enum':['listen','talk']}},['recipient']),
        tool('manage_delegation', 'Только настоящий владелец может разрешить или запретить другому пользователю давать Дарье поручения. Это отдельное разрешение, не права администратора Telegram. В личке область global.',
             {'action': {'type': 'string', 'enum': sorted(DELEGATION_ACTIONS)}, 'user': {'type': 'string'},
              'scope': {'type': 'string', 'enum': ['global']}}, ['action']),
        tool('speak_in_call', 'С разрешения владельца произнести указанную фразу в текущем звонке. Ничего не пишет в другие чаты, не начинает новый звонок. volume от 0.1 до 2 меняет громкость только этой фразы.',
             {'text': {'type': 'string', 'minLength': 1, 'maxLength': 1000}, 'volume': {'type': 'number', 'minimum': 0.1, 'maximum': 2}}, ['text']),
        tool('list_blocked_users', 'Показать личные блокировки Дарьи и их причины. Только владельцу.', {}, []),
        tool('unblock_user', 'По прямой просьбе владельца разблокировать пользователя в Telegram и разрешить общение с ним.',
             {'recipient': {'type': 'string', 'maxLength': 64}}, ['recipient']),
        tool('manage_group', 'Использовать права администратора в указанной группе по прямой команде владельца: участники, права, сообщения, закрепы, оформление, приглашения, заявки, удалённые аккаунты (kick_deleted_accounts — разовая очистка, auto_kick_deleted_accounts — постоянное автоудаление удалённых аккаунтов), темы, антиспам, варны и голосовые чаты. configure_warnings задаёт реальный порог бана и сохраняет rules для самостоятельных варнов; warn выдаёт варн, unwarn снимает последний, reset_warns сбрасывает счётчик, warns показывает его. Перед действием проверяются права. Для promote укажи конкретные rights. В личном чате нужны явные user/message_id/topic_id; reply относится только к команде внутри группы.',
             {'group':{'type':'string','maxLength':500},'operation':operation_schema()}, ['group','operation']),
        tool("join_group", "По прямой текущей просьбе владельца вступить в группу по @username, ID или пригласительной ссылке. Заявка на одобрение отличается от вступления",
             {"target": {"type": "string", "maxLength": 500}}, ["target"]),
        tool("leave_group", "По прямой просьбе владельца выйти из указанной группы",
             {"target": {"type": "string", "maxLength": 500}}, ["target"]),
        tool("join_call", "Подключиться к существующему голосовому чату группы или конференции t.me/call/... по прямой текущей команде. Не создаёт новый звонок. Только один звонок одновременно",
             {"target": {"type": "string", "maxLength": 500}, "mode": {"type": "string", "enum": ["listen", "talk"]}}, ["target"]),
        tool("leave_call", "Выйти из текущего звонка и очистить временную историю речи", {}, []),
        tool("set_call_mode", "Режим звонков: listen — слушать с выключенным микрофоном, talk — слушать и отвечать голосом. Сохраняется и применяется к текущему звонку",
             {"mode": {"type": "string", "enum": ["listen", "talk"]}}, ["mode"]),
        tool("call_status", "Проверить реальное состояние звонка и входящие звонки; доступно только владельцу", {}, []),
        tool("accept_call", "По явной текущей команде владельца принять входящий звонок. Без recipient принимает только звонок владельца; для другого человека нужен точный @username/ID. Самостоятельно никому не звонит",
             {"recipient": {"type": "string"}, "mode": {"type": "string", "enum": ["listen", "talk"]}}, []),
        tool("owner_settings", "Посмотреть все настройки или выбранные параметры и недавние фото владельца для аватара",
             {"fields": {"type": "array", "items": {"type": "string", "enum": sorted(CONTROL_FIELDS)}, "maxItems": len(CONTROL_FIELDS)}}, []),
        tool("configure_darya", "Изменить настройки по прямой просьбе владельца. Меняй только запрошенное. Для характера сначала прочитай текущую persona и сохрани остальное. Паузы задавай согласованными парами min/max",
             {"settings": {"type": "object", "properties": properties, "additionalProperties": False}}, ["settings"]),
        tool("set_reply_speed", "Владелец просит отвечать быстрее/медленнее: fast убирает искусственные паузы, normal возвращает обычный темп, slow увеличивает паузы. Скорость API зависит от провайдера",
             {"speed": {"type": "string", "enum": ["fast", "normal", "slow"]}}, ["speed"]),
        tool("set_profile_photo", "Поставить на аватар фото, присланное владельцем, только по его прямой просьбе. Без photo_message_id выбирается фото текущего сообщения, фото в ответе или последнее за сутки. ID фото можно взять из owner_settings",
             {"photo_message_id": {"type": "integer"}}, []),
        tool("set_profile_details", "По просьбе владельца изменить имя, фамилию или описание своего профиля Telegram",
             {"first_name": {"type": "string", "minLength": 1, "maxLength": 64},
              "last_name": {"type": "string", "maxLength": 64},
              "bio": {"type": "string", "maxLength": 70}}, []),
        tool("send_message_to", "По явной текущей команде владельца отправить сообщение указанному @username или Telegram ID. Один текст одному адресату за команду; не повторяй отправку после неопределённого результата. Пример или обсуждение не являются командой",
             {"recipient": {"type": "string"}, "text": {"type": "string", "minLength": 1, "maxLength": 4000}}, ["recipient", "text"]),
        tool("owner_contacts", "Показать контакты и разрешения на ответы, инициативу, подарки и Hermes", {}, []),
        tool("list_custom_settings", "Показать созданные владельцем настройки и правила ответов", {}, []),
        tool("create_custom_setting", "Создать новый параметр поведения модели: имя, начальное значение и правило его применения к ответам. Не реализует новые функции сервера или Telegram",
             {"name": {"type": "string", "minLength": 1, "maxLength": 64},
              "value": {"anyOf": [{"type": "boolean"}, {"type": "number"}, {"type": "string", "maxLength": 1000}]},
              "scope": {"type": "string", "enum": ["all", "owner"], "description": "all — все диалоги; owner — только ответы владельцу"},
              "instruction": {"type": "string", "minLength": 5, "maxLength": 1000}}, ["name", "value", "instruction"]),
        tool("update_custom_setting", "Изменить значение или правило существующего пользовательского параметра",
             {"name": {"type": "string"}, "value": {"anyOf": [{"type": "boolean"}, {"type": "number"}, {"type": "string"}]},
              "scope": {"type": "string", "enum": ["all", "owner"]},
              "instruction": {"type": "string", "minLength": 5, "maxLength": 1000}}, ["name"]),
        tool("delete_custom_setting", "Удалить пользовательскую настройку по явной просьбе владельца", {"name": {"type": "string"}}, ["name"]),
        tool("configure_contact", "Настроить конкретный контакт по @username или ID: ответы, инициативу, разрешения подарков/Hermes, заметки, день рождения, индивидуальный характер. Меняй только запрошенное",
             {"recipient": {"type": "string"}, "settings": {"type": "object", "properties": {
                 key: next(item for item in field["anyOf"] if item.get("type") != "null")
                 for key, field in ContactChanges.model_json_schema()["properties"].items()}, "additionalProperties": False}}, ["recipient", "settings"]),
        tool("search_avito", "Найти реальные объявления на Авито по товару и городу без ссылки. Сбор идёт фоном, результат и CSV придут в этот чат. Не говори, что нет доступа, до результата сбора",
             {"query": {"type": "string", "minLength": 1, "maxLength": 300},
              "city": {"type": "string", "description": "Любой город в именительном падеже или all (вся Россия, по умолчанию)"},
              "max_items": {"type": "integer", "minimum": 1, "maximum": 50},
              "max_price": {"type": "integer", "minimum": 0, "description": "Максимальная цена в рублях: 8к = 8000"},
              "min_price": {"type": "integer", "minimum": 0},
              "description": {"type": "string", "maxLength": 1000, "description": "Любые пожелания к состоянию, комплекту или другим свойствам товара"},
              "description_optional": {"type": "boolean", "description": "Можно/допускается — true; обязательно/только — false"},
              "pages": {"type": "integer", "minimum": 1, "maximum": 3},
              "newest": {"type": "boolean", "description": "Новые объявления первыми"}}, ["query"]),
    ]


def owner_request(store, contact_id, telegram_id):
    owner = store.settings()["owner_id"]
    if contact_id != owner or telegram_id is None or store.contact(contact_id)["kind"] != "private":
        return None
    rows = store.rows("SELECT * FROM messages WHERE contact_id=? AND telegram_id=? AND role='user' AND sender_id=? AND forwarded=0",
                      (contact_id, telegram_id, owner))
    return rows[0] if rows else None


def controller_request(store, contact_id, telegram_id):
    if telegram_id is None or not authorized(store, contact_id) or store.contact(contact_id)['kind'] != 'private':
        return None
    rows = store.rows("SELECT * FROM messages WHERE contact_id=? AND telegram_id=? AND role='user' AND sender_id=? AND forwarded=0",
                      (contact_id, telegram_id, contact_id))
    return rows[0] if rows else None


class OwnerControl:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime.store

    def require(self, contact_id, telegram_id, trusted):
        external=group_request.get()
        if trusted and external and external['request']['contact_id']==contact_id and external['request']['telegram_id']==telegram_id:
            external['guard']()
            return external['request']
        request = controller_request(self.store, contact_id, telegram_id) if trusted else None
        if not request:
            raise ValueError("Управление доступно владельцу и разрешённым им пользователям в личном чате")
        continuation=resolved_request.get()
        if continuation and continuation['contact_id']==contact_id and continuation['telegram_id']==telegram_id:
            original=controller_request(self.store,contact_id,continuation['original_telegram_id'])
            if not original or original['sender_id']!=request['sender_id'] or not self.store.scope_active(self.store.record_scope(original)):
                raise ValueError('Поручение для уточнения больше недоступно')
            request=request|{'text':continuation['text']+'\n'+request['text']}
        return request

    @contextmanager
    def verified_group_request(self, request, guard):
        if self.store.contact(request['contact_id'])['kind']!='group':
            raise ValueError('Нужна подлинная команда внутри группы')
        guard()
        token=group_request.set({'request':request,'guard':guard})
        try:yield
        finally:group_request.reset(token)

    def pending(self, request):
        rows=self.store.rows('SELECT * FROM owner_clarifications WHERE contact_id=?',(request['contact_id'],))
        if not rows:return None
        row=rows[0]
        original=self.store.rows('SELECT * FROM messages WHERE id=?',(row['request_id'],))
        if (row['expires']<time.time() or row['actor_id']!=request['sender_id'] or
                row['context_generation']!=request['context_generation'] or not original or
                not controller_request(self.store,request['contact_id'],original[0]['telegram_id'])):
            self.clear_pending(request['contact_id'])
            return None
        return {'original_request':row['request_text'],'question':row['question'],
                'request_id':row['request_id'],'original_telegram_id':original[0]['telegram_id']}

    def clear_pending(self, contact_id):
        self.store.execute('DELETE FROM owner_clarifications WHERE contact_id=?',(contact_id,))

    def ask(self, request, question, texts, continuation=None):
        original_id=continuation['request_id'] if continuation else request['id']
        text=((continuation['original_request']+'\n') if continuation else '')+'\n'.join(texts)
        self.store.execute('INSERT OR REPLACE INTO owner_clarifications VALUES (?,?,?,?,?,?,?)',
            (request['contact_id'],request['sender_id'],original_id,question,request['context_generation'],time.time()+600,text))

    @contextmanager
    def continue_request(self, contact_id, telegram_id, continuation):
        token=resolved_request.set({'contact_id':contact_id,'telegram_id':telegram_id,
            'original_telegram_id':continuation['original_telegram_id'],'text':continuation['original_request']} if continuation else None)
        try:yield
        finally:resolved_request.reset(token)

    def photos(self, request):
        return [dict(photo_message_id=row['id'],telegram_id=row['telegram_id'],text=row['text']) for row in
                reversed(self.store.context_history(request['contact_id'],100)) if row['sender_id']==request['sender_id'] and row['role']=='user' and not row['forwarded'] and row['image_path'] and row['id']<=request['id'] and row['created']>=time.time()-86400][:10]

    def view(self, request):
        settings = self.store.settings()
        publications=self.runtime.publications.status()
        return {"settings": {key: settings[key] for key in sorted(CONTROL_FIELDS)}, "photos": self.photos(request),
                "custom_settings": self.store.custom_settings(),
                "recent_dialogue": [{'role':row['role'],'text':redact_locations(row['text'])[:500]} for row in
                    self.store.context_history(request['contact_id'],8) if row['id']<=request['id']],
                "actor_id": request['sender_id'], "is_owner": request['sender_id'] == settings['owner_id'],
                "pending":self.pending(request),
                "publications":{"destination":publications['destination'],
                    "feeds":[{'id':feed['id'],'config':feed['config']} for feed in publications['feeds'] if feed.get('owner_id')==settings['owner_id']]}}

    async def command_view(self,request,texts):
        view=self.view(request)
        chats=self.store.rows("SELECT id,name,username,kind FROM contacts WHERE kind='group' ORDER BY last_activity DESC LIMIT 50")
        view['known_chats']=[chat|{'participants':self.store.group_participants(chat['id'])[:50]} for chat in chats]
        destination=view['publications']['destination']
        if destination and re.search(r'ключ|публик|монитор|тем[ауеы]', ' '.join(texts),re.I):
            guard=lambda:self.require(request['contact_id'],request['telegram_id'],True)
            try:
                async with asyncio.timeout(12):
                    entity,_=await self.runtime.publications.target(str(destination['target_id']),guard)
                    routes=await self.runtime.publications.telegram_sources.routes(entity,guard)
                view['publications']['destination']=destination|{'routes':routes,'topics_fresh':True}
            except (ValueError,TimeoutError):
                view['publications']['destination']=destination|{'topics_fresh':False}
        return view

    def parse(self, texts):
        """Handle clear commands without depending on provider function calling."""
        visual_prompts = {'посмотри на фото и ответь по ситуации',
                          'посмотри gif и ответь по ситуации',
                          'посмотри стикер и ответь по ситуации'}
        def visual_reply(value):
            return value.strip().casefold().rstrip('.! ') in visual_prompts
        names = ('даша','дарья','дашка','даш','дашенька','дашуля','дашуль')
        # Generated media captions and simple greetings/pings are conversation.
        # Avoid a second model request just to classify them as non-commands.
        if texts and all(visual_reply(value) or attention_ping(value, names) for value in texts):
            return []
        if len(texts) > 1:
            separate = [self.parse([value]) for value in texts]
            if any(separate) and all(actions or visual_reply(value) or attention_ping(value, names)
                                     for value, actions in zip(texts, separate)):
                return [action for actions in separate if actions for action in actions]
        text = "\n".join(texts).strip()
        text = re.sub(r"^(?:(?:Даша|Дарья|Дашка|Дашенька)[,\s]+|пожалуйста[,\s]+|снова\s+|и\s+)+", "", text, flags=re.I)
        lower = text.casefold().rstrip(".! ")
        def command(name, args):
            return [{"name": name, "args": args}]
        from .publication_telegram import parse_monitor_command
        monitor=parse_monitor_command(text)
        if monitor:
            args={'action':{'monitor_source':'monitor','stop_monitoring':'stop_monitor','monitoring_status':'monitor_status'}[monitor['action']],
                **{('source' if k=='target' else k):v for k,v in monitor.items() if k!='action'}}
            return command('manage_publications',args)
        if re.fullmatch(r'(?:не меняй|не изменяй|не обновляй|перестань менять)\s+(?:(?:своё|свое|свой)\s+)?(?:описание|био|профиль)',lower):
            return command('configure_darya',{'settings':{'telegram_autonomy_profile':False}})
        delegation = parse_delegation(text)
        if delegation:
            return command('manage_delegation', delegation)
        if lower in {'включи личную инициативу','пиши сама в лс','сама продолжай личные разговоры'}:
            return command('configure_darya',{'settings':{'telegram_autonomy_enabled':True,'private_initiative_enabled':True,'proactive_enabled':True}})
        if lower in {'выключи личную инициативу','не пиши сама в лс'}:
            return command('configure_darya',{'settings':{'private_initiative_enabled':False}})
        dial=re.fullmatch(r'(?:позвони|набери|созвонись с)\s+(@?[A-Za-z][A-Za-z0-9_]{3,31}|[1-9][0-9]{0,19})(?:\s+и\s+(говори|слушай))?',text,re.I)
        if dial:
            return command('call_user',{'recipient':dial[1],**({'mode':'listen' if dial[2].casefold()=='слушай' else 'talk'} if dial[2] else {})})
        if re.fullmatch(r'(?:включи|разрешаю|разреши)(?:\s+полную)?\s+(?:самостоятельность|автономность)(?:\s+в\s+(?:тг|телеграме|telegram))?',lower):
            return command('configure_darya',{'settings':{'telegram_autonomy_enabled':True}})
        if re.fullmatch(r'(?:выключи|отключи|запрети)\s+(?:самостоятельность|автономность)(?:\s+в\s+(?:тг|телеграме|telegram))?',lower):
            return command('configure_darya',{'settings':{'telegram_autonomy_enabled':False}})
        navigation = parse_navigation_command(text)
        if navigation:
            return command(navigation['action'], {key: value for key, value in navigation.items() if key != 'action'})
        if format_request(text, standalone=True):
            return []
        media = parse_media_command(text)
        if media:
            return command('telegram_action',media)
        account = parse_account_command(text)
        if account is not None:
            return account
        if re.fullmatch(r'(?:за)?кричи(?:\s+сильно|\s+громко)?(?:\s+в\s+(?:видеочате|видеочат|голосовом чате|звонке))?', lower):
            return command('speak_in_call', {'text': 'А-а-а-а!', 'volume': 2})
        spoken = re.fullmatch(r'(?:скажи|произнеси)\s+в\s+(?:видеочате|голосовом чате|звонке)\s*[:,-]?\s*(.+)', text, re.I | re.S)
        if spoken:
            return command('speak_in_call', {'text': spoken[1].strip()})
        unblock = re.fullmatch(r'разблокируй\s+(@?[A-Za-z][A-Za-z0-9_]{3,31}|[0-9]{1,20})', lower)
        if unblock:
            return command('unblock_user', {'recipient': unblock[1]})
        if re.fullmatch(r'(?:покажи\s+)?(?:заблокированных|список заблокированных|кого ты заблокировала)', lower):
            return command('list_blocked_users', {})
        group = re.fullmatch(r'(?:вступи|присоединись|зайди|вступить)(?:\s+(?:в|к))?\s+групп[ауеуы]\s+(\S+)', text, re.I)
        if group:
            return command('join_group', {'target': group[1]})
        leave_group = re.fullmatch(r'(?:выйди|покинь)(?:\s+из)?\s+групп[уыау]\s+(\S+)', text, re.I)
        if leave_group:
            return command('leave_group', {'target': leave_group[1]})
        call = re.fullmatch(r'(?:подключись|присоединись|зайди|вступи)(?:\s+(?:к|в))?\s+(?:звонок|звонку|созвон|созвону|голосовой чат|голосовому чату)\s+(\S+?)(?:\s+(?:и|в режиме)\s+(слушай|говори|разговаривай|слушать|разговаривать|talk|listen))?', text, re.I)
        if call:
            args = {'target': call[1]}
            if call[2]:
                args['mode'] = 'listen' if call[2].casefold() in {'слушай', 'слушать', 'listen'} else 'talk'
            return command('join_call', args)
        if re.fullmatch(r'(?:выйди|отключись)(?: из| от)? (?:звонка|созвона|голосового чата)', lower):
            return command('leave_call', {})
        if re.fullmatch(r'(?:статус звонка|что со звонком|ты в звонке|проверь звонок)[?]?', lower):
            return command('call_status', {})
        if re.fullmatch(r'(?:в звонке (?:говори|разговаривай)|(?:говори|разговаривай|отвечай голосом) в звонк[еах]|включи микрофон)', lower):
            return command('set_call_mode', {'mode': 'talk'})
        if re.fullmatch(r'(?:в звонке (?:слушай|молчи)|(?:только )?слушай в звонк[еах]|выключи микрофон)', lower):
            return command('set_call_mode', {'mode': 'listen'})
        accept = re.fullmatch(r'(?:прими|ответь на) звонок(?:\s+(?:от\s+)?(@?[A-Za-z][A-Za-z0-9_]{3,31}|[0-9]{1,20}))?', text, re.I)
        if accept:
            return command('accept_call', {'recipient': accept[1]} if accept[1] else {})
        if re.fullmatch(r'(?:ку|привет|хай|здравствуй(?:те)?|доброе утро|добрый день|добрый вечер)[!?\s]*',lower):
            return []
        typing_start = re.fullmatch(r'(?:начинай|начни)\s+печатать\s+через\s+(\d+(?:[.,]\d+)?)\s*(?:секунд\w*|сек\.?|с)',lower)
        if typing_start:
            return command('configure_darya',{'settings':{'typing_enabled':True,'typing_during_generation':True,
                'typing_start_delay':float(typing_start[1].replace(',','.'))}})
        def value(raw):
            raw = raw.strip(' «»')
            if raw.casefold() in {'включено', 'включить', 'да', 'true'}:
                return True
            if raw.casefold() in {'выключено', 'выключить', 'нет', 'false'}:
                return False
            try:
                return json.loads(raw)
            except ValueError:
                return raw.strip('"')
        pause = re.fullmatch(r'(?:нет\s+)?(?:пауза|задержка)\s+перед\s+(?:прочтением|чтением)(?:\s+и\s+(?:заходом|захода|входом|входа)\s+в\s+сеть)?\s*(\d+(?:[.,]\d+)?)\s*(?:секунд\w*|сек\.?|с)', lower)
        if pause:
            seconds = float(pause[1].replace(',', '.'))
            settings = {'read_delay_min':seconds,'read_delay_max':seconds}
            if re.search(r'\sи\s',lower):
                settings['reply_online_immediate'] = False
            return command('configure_darya', {'settings':settings})
        if re.fullmatch(r'(?:входи|заходи) в сеть сразу',lower):
            return command('configure_darya', {'settings':{'online_during_reply':True,'reply_online_immediate':True}})
        if re.fullmatch(r'(?:входи|заходи) в сеть после паузы перед (?:прочтением|чтением)',lower):
            return command('configure_darya', {'settings':{'reply_online_immediate':False}})
        idle = re.search(r'\b(?:через\s+(\d+(?:[.,]\d+)?)\s*(?:секунд\w*|сек\.?|с)\s+(?:уходи|выходи)|(?:уходи|выходи)(?:\s+из\s+сети)?\s+через\s+(\d+(?:[.,]\d+)?)\s*(?:секунд\w*|сек\.?|с))', lower)
        if idle:
            return command('configure_darya', {'settings':{'reply_idle_seconds':float((idle[1] or idle[2]).replace(',','.'))}})
        scale = re.fullmatch(r'(?:создай|добавь) (?:свою |новую )?настройку (.+?) (\d+(?:[.,]\d+)?) из (\d+)', text, re.I)
        if scale:
            number = float(scale[2].replace(',', '.'))
            return command('create_custom_setting', {'name':scale[1].strip(' «»"'), 'value':int(number) if number.is_integer() else number,
                'instruction':f'Уровень «{scale[1]}» в ответах по шкале от 0 до {scale[3]}; используй текущее числовое значение.', 'scope':'all'})
        create = re.fullmatch(r'(?:создай|добавь) (?:свою |новую )?настройку (.+?)\s*=\s*(.+?)\s*:\s*(.+)', text, re.I | re.S)
        if create:
            return command('create_custom_setting', {'name':create[1].strip(' «»"'),'value':value(create[2]),'instruction':create[3].strip(),
                'scope':'owner' if re.search(r'\b(?:меня|мне|со мной)\b',create[3],re.I) else 'all'})
        rule = re.fullmatch(r'(?:создай|добавь) (?:новое )?правило (.+)', text, re.I | re.S)
        if rule:
            description = rule[1].strip()
            return command('create_custom_setting', {'name':description[:64].strip(' «»"'), 'value':True, 'instruction':description,
                'scope':'owner' if re.search(r'\b(?:меня|мне|со мной)\b',description,re.I) else 'all'})
        remove = re.fullmatch(r'удали (?:пользовательскую )?(?:настройку|правило) (.+)', text, re.I)
        if remove:
            return command('delete_custom_setting', {'name':remove[1].strip(' «»"')})
        update = re.fullmatch(r'(?:поставь|установи|измени|поменяй) (?:значение настройки |настройку )?(.+?) на (.+)', text, re.I | re.S)
        if update and any(row['name']==update[1].strip(' «»"').casefold() for row in self.store.custom_settings()):
            return command('update_custom_setting', {'name':update[1].strip(' «»"'),'value':value(update[2])})
        update_rule = re.fullmatch(r'(?:измени|поменяй) правило (.+?) на (.+)', text, re.I | re.S)
        if update_rule:
            return command('update_custom_setting', {'name':update_rule[1].strip(' «»"'),'instruction':update_rule[2].strip()})
        if (re.search(r'\b(?:в сети|онлайн)\b', lower) and re.search(r'\b(?:ответ|ответишь|отвечаешь|пришлешь|пришлёшь|отправишь)\w*\b', lower)
                and re.search(r'\b(?:будь|оставайся|не выходи|держи)\b', lower)):
            return command("configure_darya", {"settings": {"online_during_reply": True}})
        if re.fullmatch(r'(?:покажи|список) (?:мои |свои |новые |пользовательские )?(?:правила|пользовательские настройки|созданные настройки)', lower):
            return command("list_custom_settings", {})
        if re.fullmatch(r'(?:не запоминай|не сохраняй|забудь|удали) (?:мои |все |эти )?(?:города|геоданные|адреса)(?: из памяти)?', lower):
            # The ordinary agent handles forget_locations; unrelated memories stay enabled.
            return []
        avito = parse_avito_request(text)
        if avito:
            # Only a fully identified phone model is safe for the fast parser.
            # Other products and free-form goals need the intent planner.
            if not re.fullmatch(r'(?:айфон|iphone)\s*\d+(?:\s+(?:pro(?:\s+max)?|про(?:\s+макс)?|max|макс))?',avito['query'],re.I):
                return None
            if re.search(r'\b(?:чтоб(?:ы)?|короче|мне)\b',avito.get('description',''),re.I):
                return None
            return command("search_avito", avito)
        send = re.fullmatch(r"(?:напиши|отправь(?:\s+сообщение)?)\s+(?:(?:ему|ей|сообщение)\s+)?(@[A-Za-z][A-Za-z0-9_]{3,31}|-?[0-9]{1,20})\s*:?\s+(.+)", text, re.I | re.S)
        if send:
            body = send[2].strip()
            for left, right in [('«', '»'), ('"', '"')]:
                if body.startswith(left) and body.endswith(right):
                    body = body[1:-1]
                    break
            return command("send_message_to", {"recipient": send[1], "text": body})
        if re.fullmatch(r"(?:отвечай|пиши|отвечай мне)\s+(?:побыстрее|быстрее|без пауз)(?:[, ]+пожалуйста)?", lower):
            return command("set_reply_speed", {"speed": "fast"})
        if re.fullmatch(r"(?:отвечай|пиши)\s+(?:медленнее|помедленнее)(?:[, ]+пожалуйста)?", lower):
            return command("set_reply_speed", {"speed": "slow"})
        if re.fullmatch(r"(?:верни|включи)\s+(?:обычную|нормальную)\s+скорость(?: ответов)?", lower):
            return command("set_reply_speed", {"speed": "normal"})
        if re.fullmatch(r"(?:только )?симуляцию (?:печатания|набора) (?:оставь|сохрани)|(?:оставь|сохрани) (?:только )?(?:симуляцию печатания|индикатор набора)", lower):
            return command("configure_darya", {"settings": {"typing_enabled": True}})
        if re.fullmatch(r"(?:поставь (?:себя )?на паузу|выключись|отключи ответы(?: людям)?|перестань отвечать людям)", lower):
            return command("configure_darya", {"settings": {"enabled": False}})
        if re.fullmatch(r"(?:продолжай отвечать|включись|(?:включи|возобнови) (?:ответы|отвечать)(?: людям)?)", lower) or (lower == "продолжай" and not self.store.settings()["enabled"]):
            return command("configure_darya", {"settings": {"enabled": True}})
        toggles = {"реакции": "reactions_enabled", "ответы в группах": "group_enabled",
                   "инициативу": "proactive_enabled", "распознавание фото": "vision_enabled",
                   "онлайн": "presence_enabled", "печатание": "typing_enabled",
                   "онлайн до ответа": "online_during_reply", "онлайн во время ответа": "online_during_reply",
                   "обновление ответа при новых сообщениях": "revise_reply_on_new_message",
                   "симуляцию печатания": "typing_enabled", "индикатор набора": "typing_enabled",
                   "отметку о прочтении": "read_receipts_enabled", "память": "memory_enabled", "голосовые ответы": "voice_messages_enabled"}
        toggle = re.fullmatch(r"(включи|отключи|выключи) (.+)", lower)
        if toggle and toggle[2] in toggles:
            return command("configure_darya", {"settings": {toggles[toggle[2]]: toggle[1] == "включи"}})
        if (re.match(r"^(?:поменяй|измени|смени|поставь|установи)\b", lower) and
                re.search(r"(?:аватар|\bаву\b|фото (?:своего )?профиля)", lower) and
                (re.search(r"\b(?:это|эту|последнее|последнюю)\b", lower) or lower in {"смени аватар", "поменяй аватар"})):
            return command("set_profile_photo", {})
        profile = re.fullmatch(r"(?:поменяй|измени|смени) (?:своё |свое )?(имя|описание профиля|био) на (.+)", text, re.I | re.S)
        if profile:
            return command("set_profile_details", {"first_name" if profile[1].casefold() == "имя" else "bio": profile[2].strip(' «»"')})
        if re.fullmatch(r"(?:покажи|прочитай) (?:свои |текущие )?настройки", lower):
            return command("owner_settings", {})
        return None

    async def execute_plan(self, contact_id, telegram_id, actions, *, request_ids=None, continuation=None):
        with self.continue_request(contact_id,telegram_id,continuation):
            return await self._execute_plan(contact_id,telegram_id,actions,request_ids=request_ids)

    async def _execute_plan(self, contact_id, telegram_id, actions, *, request_ids=None):
        from .research import RESEARCH_NAMES, TELEGRAM_RESEARCH_NAMES
        if not isinstance(actions, list) or not 1 <= len(actions) <= 8:
            raise ValueError("Не удалось разобрать команду управления")
        from .owner_planning import catalog
        available=catalog()
        for action in actions:
            if not isinstance(action, dict) or set(action) != {"name", "args"} or not isinstance(action["name"], str) or action["name"] not in available or not isinstance(action["args"], dict):
                raise ValueError("Модель вернула неподдерживаемую команду управления")
        from .owner_planning import validate_actions
        request=self.require(contact_id,telegram_id,True)
        if group_request.get() and not authorized(self.store,request['sender_id']):
            raise ValueError('Это разрешение действует только в группе; управление аккаунтом и общими настройками не разрешено')
        actions=validate_actions(actions,self.view(request))
        for action in actions:
            name,args=action['name'],action['args']
            if request['sender_id']!=self.store.settings()['owner_id'] and (name in {'manage_publications','configure_publications','manage_delegation'} or
                    (name=='configure_darya' and any(key.startswith(('telegram_autonomy_','private_initiative_')) or key in {'telegram_auto_answer_calls','telegram_call_cooldown_hours','publishing_enabled'} for key in args['settings']))):
                raise ValueError('Эти постоянные разрешения меняет только настоящий владелец')
        if request_ids is not None:
            if len(request_ids) != len(actions):
                raise ValueError('Не удалось связать команды с сообщениями владельца')
            for source_id in request_ids:
                self.require(contact_id, source_id, True)
        replies = []
        research_results = []
        research_reply_at = None
        for index, action in enumerate(actions):
            name, args = action["name"], action["args"]
            source_id = request_ids[index] if request_ids is not None else telegram_id
            if name in OWNER_NAMES:
                result = await self.action(contact_id, source_id, name, args, trusted=True)
            else:
                async def ordinary():
                    self.require(contact_id,source_id,True)
                    if name=='skip_message':return {'value':{'skipped':True}}
                    value=await self.runtime.action(contact_id,source_id,name,args,actor_id=request['sender_id'])
                    return {'value':value}
                result=await self.perform(request,name,json.dumps(args,sort_keys=True,ensure_ascii=False),ordinary)
                result=result['value']
            research_name = name if name in RESEARCH_NAMES else TELEGRAM_RESEARCH_NAMES.get(args.get('action')) if name == 'telegram_action' else None
            if research_name:
                if research_reply_at is None:
                    research_reply_at = len(replies)
                    replies.append('')
                research_results.append({'tool':research_name,'result':result,
                                         'request':self.require(contact_id,source_id,True)['text']})
                continue
            if name not in OWNER_NAMES:
                replies.append(self.task_result(name,args,result))
            elif name == 'manage_delegation':
                replies.append(result['text'])
            elif name == 'configure_publications':
                replies.append(result['text'])
            elif name == 'manage_publications':
                if result.get('text'):
                    replies.append(result['text'])
                elif args['action'] == 'list':
                    replies.append('\n'.join(('Включён: ' if feed['enabled'] else 'Остановлен: ')+feed['name']
                                             for feed in result['feeds']) or 'Источники мониторинга пока не заданы.')
                elif args['action'] == 'check':
                    replies.append('Проверка источников запущена.' if result['accepted'] else result['message'])
                else:
                    replies.append({'create':'Мониторинг сохранён.','update':'Мониторинг обновлён.',
                                    'remove':'Мониторинг остановлен.',
                                    'publish':'Публикация отправлена.','discard':'Черновик удалён.',
                                    'destination':'Группа публикации сохранена.'}[args['action']])
            elif name in ACCOUNT_NAMES:
                replies.append(account_result_text(name, result))
            elif name == 'telegram_action':
                if args.get('action') in {'send_sticker','send_gif','send_photo'} and result.get('sent'):
                    continue
                if args.get('action') == 'save_media':
                    replies.append('Сохранила '+('стикер в избранное.' if result['kind']=='sticker' else 'GIF в сохранённые.')
                                   if result['saved'] else 'Убрала из сохранённых.')
                elif args.get('action') == 'profile':
                    replies.append(account_result_text('get_telegram_profile',result))
                else:
                    replies.append(result.get('text') or json.dumps(result,ensure_ascii=False,default=str)[:3500])
            elif name == 'speak_in_call':
                replies.append(result['text'])
            elif name == 'manage_group':
                replies.append(result['text'])
            elif name in {'join_group', 'leave_group'}:
                replies.append(membership_text(result))
            elif name in {'join_call', 'accept_call', 'call_status','call_user'}:
                if result['connected']:
                    replies.append('В звонке: ' + result['name'] + '. ' +
                        ('Слушаю и могу отвечать голосом.' if result['mode'] == 'talk' else 'Слушаю, микрофон выключен.') +
                        (' Администратор запретил мне говорить.' if result['admin_muted'] else '') +
                        (' ' + result['error'] if result['error'] else ''))
                else:
                    replies.append('Сейчас я не в звонке.' + (' Есть входящий звонок.' if result['pending_callers'] else ''))
            elif name == 'leave_call':
                replies.append('Вышла из звонка.' if result['left'] else 'Сейчас я не в звонке.')
            elif name == 'set_call_mode':
                replies.append('Режим звонков: ' + ('слушать и отвечать голосом.' if args['mode'] == 'talk' else 'только слушать, микрофон выключен.'))
            elif name == "send_message_to":
                replies.append("Отправила " + result["recipient"] + ".")
            elif name == "set_profile_photo":
                replies.append("Поставила фото на аватар.")
            elif name == "set_profile_details":
                replies.append("Обновила профиль.")
            elif name == "set_reply_speed":
                replies.append({"fast": "Убрала паузы перед ответом, теперь быстрее.", "normal": "Вернула обычный темп ответов.", "slow": "Теперь отвечаю медленнее."}[args["speed"]])
            elif name == "configure_darya":
                enabled = result["updated"].get("enabled")
                replies.append("Поставила себя на паузу. Через этот чат ты можешь включить меня обратно." if enabled is False else "Ответы снова включены." if enabled is True else "Сохранила:\n" + describe_settings(result["updated"], changed=True))
            elif name == "configure_contact":
                replies.append("Настройки " + result["recipient"] + " сохранены.")
            elif name == "owner_contacts":
                replies.append("\n".join(("@" + row["username"] if row["username"] else row["name"]) +
                              (": отвечает" if row["enabled"] else ": ответы выключены") +
                              (", пишет первой" if row["proactive"] else ", без инициативы") for row in result["contacts"]) or "Контактов пока нет.")
            elif name == 'list_blocked_users':
                replies.append('\n'.join(f"{row['user_id']}: {row['reason']} ({row['status']})" for row in result['blocked']) or 'Личных блокировок нет.')
            elif name == 'unblock_user':
                replies.append('Пользователь разблокирован: ' + str(result['user_id']))
            elif name == "search_avito":
                region = args.get('city','all')
                region = 'вся Россия' if city_slug(region)=='all' else region
                message = 'Запустила поиск на Авито: '+args['query']+'.\nРегион: '+region
                if args.get('max_price') is not None:
                    message += ', до '+f"{args['max_price']:,}".replace(',',' ')+' ₽'
                if args.get('min_price') is not None:
                    message += ', от '+f"{args['min_price']:,}".replace(',',' ')+' ₽'
                if args.get('description'):
                    message += '\n'+('Допускается: ' if args.get('description_optional') else 'Проверю: ')+args['description']
                if args.get('newest',True):
                    message += '\nНовые объявления первыми.'
                replies.append(message+'\nРезультат и CSV пришлю сюда после сбора.')
            elif name in {"create_custom_setting", "update_custom_setting"}:
                setting = result["setting"]
                replies.append(("Создала" if name == "create_custom_setting" else "Изменила") + " настройку ответов «" + setting["name"] + "»: " +
                               json.dumps(setting["value"], ensure_ascii=False) + "\nПравило: " + setting["instruction"])
            elif name == "delete_custom_setting":
                replies.append("Удалила настройку «" + result["name"] + "».")
            elif name == "list_custom_settings":
                replies.append(self.describe_custom(result["custom_settings"]))
            else:
                replies.append(describe_settings(result["settings"]) + "\n\n" + self.describe_custom(result["custom_settings"]))
        if research_results:
            def research_guard():
                self.require(contact_id,telegram_id,True)
                for source_id in request_ids or []:self.require(contact_id,source_id,True)
            replies[research_reply_at] = await self.runtime.research.answer(request['text'],research_results,research_guard)
        return "\n".join(replies)[:4000]

    def task_result(self,name,args,result):
        from .research import RESEARCH_NAMES, research_fallback, reply_source
        if name in RESEARCH_NAMES:return research_fallback([{'result':reply_source(result)}])[:3500]
        if name in {'skip_message','react','send_media','send_sticker','send_gif','send_photo','generate_image'}:return ''
        if name=='schedule_message':return 'Напоминание назначено: '+args['when']+' · '+args['text']
        if name=='remember':return 'Запомнила.' if result.get('saved') else result.get('reason','Факт не сохранён.')
        if name in {'forget','forget_locations'}:return 'Запрошенные сведения удалены из памяти.'
        if name=='cancel_reminder':return 'Напоминание отменено.' if result.get('cancelled') else 'Такого активного напоминания нет.'
        if name=='stop_proactive':return 'Инициатива в этом диалоге отключена.'
        if isinstance(result,dict):
            if result.get('job_id'):return 'Задача запущена в фоне. Результат придёт сюда. Номер: '+str(result['job_id'])
            if isinstance(result.get('text'),str):return result['text'][:3500]
        return json.dumps(result,ensure_ascii=False,default=str)[:3500]

    def describe_custom(self, settings):
        return "Пользовательские настройки:\n" + ("\n".join(
            row["name"] + ": " + json.dumps(row["value"], ensure_ascii=False) + " — " + row["instruction"] for row in settings)
            or "Пока нет. Можно создать свой параметр и правило его применения к ответам.")

    def custom_action(self, name, args):
        if not isinstance(args.get("name"), str):
            raise ValueError("Укажи имя пользовательской настройки")
        key = re.sub(r'\s+', ' ', args["name"].strip()).casefold()
        if not 1 <= len(key) <= 64 or not re.fullmatch(r'[\w а-яё-]+', key) or key in self.store.settings() or key in {'ai_key','secrets','owner_id'}:
            raise ValueError("Выбери отдельное имя для своей настройки")
        existing = next((row for row in self.store.custom_settings() if row["name"] == key), None)
        if name == "delete_custom_setting":
            if set(args) != {"name"} or existing is None:
                raise ValueError("Пользовательская настройка не найдена")
            self.store.execute("DELETE FROM custom_settings WHERE name=?", (key,))
            self.store.event("Владелец удалил пользовательскую настройку: " + key)
            return {"name": key}
        if set(args) - {"name", "value", "instruction", "scope"} or (name == "create_custom_setting" and not {"name", "value", "instruction"} <= set(args)):
            raise ValueError("Укажи имя, значение и правило применения настройки")
        if name == "create_custom_setting" and existing:
            raise ValueError("Такая настройка уже есть; попроси изменить её")
        if name == "update_custom_setting" and (existing is None or set(args) == {"name"}):
            raise ValueError("Укажи существующую настройку и её новое значение или правило")
        value = args.get("value", existing["value"] if existing else None)
        instruction = args.get("instruction", existing["instruction"] if existing else None)
        scope = args.get("scope", existing["scope"] if existing else "all")
        if not isinstance(scope,str) or scope not in {"all", "owner"}:
            raise ValueError("Укажи область правила: все диалоги или только владелец")
        if type(value) not in {bool, int, float, str} or (type(value) in {int,float} and (abs(value) > 1e12 or not math.isfinite(value))) or (type(value) is str and len(value) > 1000):
            raise ValueError("Значение настройки: переключатель, число или текст до 1000 символов")
        if not isinstance(instruction, str) or not 5 <= len(instruction.strip()) <= 1000:
            raise ValueError("Опиши правило применения настройки к ответам (5–1000 символов)")
        same_type = existing is None or type(value) is type(existing["value"]) or (type(value) in {int,float} and type(existing["value"]) in {int,float})
        if not same_type:
            raise ValueError("Сохрани тип значения настройки")
        if existing is None and len(self.store.custom_settings()) >= 50:
            raise ValueError("Уже создано 50 пользовательских настроек; удали ненужную")
        now = time.time()
        self.store.execute("INSERT INTO custom_settings(name,value,instruction,scope,created,updated) VALUES (?,?,?,?,?,?) ON CONFLICT(name) DO UPDATE SET value=excluded.value,instruction=excluded.instruction,scope=excluded.scope,updated=excluded.updated",
                           (key, json.dumps(value,ensure_ascii=False), instruction.strip(), scope, now, now))
        self.store.event("Владелец изменил пользовательскую настройку: " + key)
        return {"setting": {"name":key,"value":value,"instruction":instruction.strip(),"scope":scope}}

    def configure(self, values):
        if not isinstance(values, dict) or not values or set(values) - CONTROL_FIELDS:
            raise ValueError("Через чат доступны настройки Дарьи; ID владельца, ключи и служебные счётчики защищены")
        try:
            values = Settings.model_validate(values, strict=True).model_dump(exclude_none=True)
        except ValidationError:
            raise ValueError("Значения настроек выходят за допустимые пределы") from None
        if not values:
            raise ValueError("Укажите настройки для изменения")
        if values.get("enabled") and (not self.runtime.telegram.ready() or not self.runtime.model.key()):
            raise ValueError("Сначала подключите Telegram и модель")
        validate_settings(self.store.settings(), values)
        self.store.update_settings(values)
        self.store.event("Владелец изменил настройки через личный чат: " + ", ".join(sorted(values)))
        return {"updated": values}

    def reserve(self, request, action, fingerprint):
        inserted = self.store.execute("INSERT OR IGNORE INTO owner_actions(request_message_id,action,fingerprint,created) VALUES (?,?,?,?)",
                                      (request["id"], action, fingerprint, time.time()))
        row = self.store.rows("SELECT * FROM owner_actions WHERE request_message_id=? AND action=? AND fingerprint=?",
                              (request["id"], action, fingerprint))[0]
        if inserted.rowcount:
            return row["id"], None
        if row["status"] == "sent":
            return None, json.loads(row["result"]) | {"already_completed": True}
        raise ValueError("Это действие уже запускалось. Результат не подтверждён: проверь Telegram; автоматически не повторяю")

    async def perform(self, request, action, fingerprint, operation):
        action_id, previous = self.reserve(request, action, fingerprint)
        if previous is not None:
            return previous
        try:
            result = await operation()
        except CallUnavailable as error:
            self.store.execute("UPDATE owner_actions SET status='failed' WHERE id=?", (action_id,))
            raise ValueError(str(error)) from None
        except errors.FloodWaitError as error:
            self.runtime.flood_until = time.time() + error.seconds
            self.store.execute("UPDATE owner_actions SET status='blocked' WHERE id=?", (action_id,))
            raise ValueError(f"Telegram просит подождать {error.seconds} секунд. Действие не повторяется автоматически") from None
        except errors.RPCError as error:
            self.store.execute("UPDATE owner_actions SET status='failed' WHERE id=?", (action_id,))
            raise ValueError(telegram_error(error)) from None
        except ValueError as error:
            detail = str(error)[:350]
            self.store.execute("UPDATE owner_actions SET status='uncertain',result=? WHERE id=?",
                               (json.dumps({'error': detail, 'error_type': type(error).__name__}, ensure_ascii=False), action_id))
            self.store.event(f'Команда владельца {action}: {detail}', 'error')
            raise
        except (Exception, asyncio.CancelledError) as error:
            self.store.execute("UPDATE owner_actions SET status='uncertain',result=? WHERE id=?",
                               (json.dumps({'error_type': type(error).__name__}), action_id))
            self.store.event(f'Команда владельца {action}: {type(error).__name__}', 'error')
            if isinstance(error, asyncio.CancelledError):
                raise
            if action == 'manage_publications':
                raise ValueError('Не удалось завершить настройку мониторинга. Часть действий могла выполниться; проверь «покажи мониторинг».') from None
            raise ValueError("Telegram не подтвердил результат. Проверь профиль или доставку; автоматически не повторяю") from None
        self.store.execute("UPDATE owner_actions SET status='sent',result=? WHERE id=?", (json.dumps(result, ensure_ascii=False), action_id))
        return result

    def photo(self, request, photo_id=None):
        candidates = self.photos(request)
        if photo_id is None:
            direct = [row for row in candidates if row["photo_message_id"] == request["id"] or
                      row["telegram_id"] == request["reply_to_telegram_id"]]
            photo_id = (direct or candidates or [{}])[0].get("photo_message_id")
        if type(photo_id) is not int or not any(row["photo_message_id"] == photo_id for row in candidates):
            raise ValueError("Пришли фото в личный чат и попроси поставить его на аватар")
        relative = self.store.rows("SELECT image_path FROM messages WHERE id=?", (photo_id,))[0]["image_path"]
        original = self.store.directory / relative
        incoming = self.store.directory / "incoming"
        if incoming.is_symlink() or original.is_symlink() or original.resolve().parent != incoming.resolve() or not original.is_file():
            raise ValueError("Фото не найдено")
        if original.stat().st_size > MAX_BYTES:
            raise ValueError("Фото слишком большое")
        # Upload a normalized in-memory copy, never a model-supplied path.
        photo = io.BytesIO(normalize(original.read_bytes()))
        photo.name = "profile.jpg"
        return photo_id, photo

    async def action(self, contact_id, telegram_id, name, args, trusted=False, retry_failed=False):
        request = self.require(contact_id, telegram_id, trusted)
        if name=='configure_publications':
            if request['sender_id']!=self.store.settings()['owner_id']:
                raise ValueError('Настройки публикаций меняет только настоящий владелец')
            def guard():
                current=self.require(contact_id,telegram_id,trusted)
                if current['sender_id']!=self.store.settings()['owner_id']:raise ValueError('Владелец мониторинга изменился')
            return await self.perform(request,name,json.dumps(args,sort_keys=True,ensure_ascii=False),
                lambda:self.runtime.publications.configure(args,guard))
        if name=='manage_publications':
            if request['sender_id']!=self.store.settings()['owner_id']:
                raise ValueError('Источники и постоянные публикации настраивает только настоящий владелец')
            def guard():
                current=self.require(contact_id,telegram_id,trusted)
                if current['sender_id']!=self.store.settings()['owner_id']:
                    raise ValueError('Владелец мониторинга изменился')
            if args.get('action')=='monitor':
                if not args.get('source') or args['source'] not in request['text']:
                    raise ValueError('Укажи точную ссылку или @username источника в текущем поручении')
                if args.get('join') and not re.search(r'вступ|зайди|подпиш|присоедин',request['text'],re.I):
                    raise ValueError('Вступление требует команды «вступи в @источник и мониторь»')
            if args.get('action') in {'list','check','monitor_status'}:
                return await self.runtime.publications.execute(args,guard)
            return await self.perform(request,name,json.dumps(args,sort_keys=True,ensure_ascii=False),
                lambda:self.runtime.publications.execute(args,guard))
        if name=='configure_darya'  and request['sender_id']!=self.store.settings()['owner_id'] and any(
                key.startswith(('telegram_autonomy_','private_initiative_')) or key in {'telegram_auto_answer_calls','telegram_call_cooldown_hours','publishing_enabled'}
                for key in args.get('settings',{})):
            raise ValueError('Постоянные разрешения самостоятельности меняет только настоящий владелец')
        if name in {'telegram_action','call_user'}:
            values=args if name=='telegram_action' else {'action':'call',**args}
            guard=lambda:self.require(contact_id,telegram_id,trusted)
            return await self.runtime.telegram_actions.execute(values,guard,
                lambda fingerprint,operation:self.perform(request,name,fingerprint,operation),focus_id=contact_id,
                source=request,requester_id=request['sender_id'])
        if name == 'manage_delegation':
            guard = lambda: self.require(contact_id, telegram_id, trusted)
            return await self.perform(request, name, json.dumps(args, sort_keys=True),
                lambda: self.runtime.authority.execute(request['sender_id'], args, guard))
        if name == 'speak_in_call':
            if set(args) - {'text', 'volume'} or 'text' not in args:
                raise ValueError('Укажи фразу и, при необходимости, её громкость')
            guard = lambda: self.require(contact_id, telegram_id, trusted)
            return await self.perform(request, name, 'current_call',
                lambda: self.runtime.calls.say(args['text'], guard, volume=args.get('volume')))
        if name in ACCOUNT_NAMES:
            guard = lambda: self.require(contact_id, telegram_id, trusted)
            try:
                return await self.runtime.account.execute(name, args, guard,
                    lambda fingerprint, operation: self.perform(request, name, fingerprint, operation),requester_id=request['sender_id'])
            except errors.FloodWaitError as error:
                self.runtime.flood_until = time.time() + error.seconds
                raise ValueError(f'Telegram просит подождать {error.seconds} секунд') from None
            except errors.RPCError as error:
                raise ValueError(telegram_error(error)) from None
            except asyncio.TimeoutError:
                raise ValueError('Telegram не ответил; результат не подтверждён') from None
        if name == 'list_blocked_users':
            if args:
                raise ValueError('Эта команда не принимает параметры')
            return {'blocked': self.runtime.contact_blocks.listing()}
        if name == 'unblock_user':
            if set(args) != {'recipient'} or not isinstance(args['recipient'], str) or not re.fullmatch(r'@?[A-Za-z][A-Za-z0-9_]{3,31}|[0-9]{1,20}', args['recipient']):
                raise ValueError('Укажи @username или ID пользователя')
            return await self.perform(request, name, args['recipient'].casefold(),
                lambda: self.runtime.contact_blocks.unblock(args['recipient'], lambda: self.require(contact_id, telegram_id, trusted)))
        if name == 'manage_group':
            if set(args) != {'group','operation'} or not isinstance(args['group'],str):
                raise ValueError('Укажи группу и действие')
            operation = AdminOperation.model_validate(args['operation']).checked()
            guard = lambda: self.require(contact_id,telegram_id,trusted)
            return await self.perform(request,name,json.dumps(args,sort_keys=True,ensure_ascii=False),
                lambda: self.runtime.group_admin.execute(args['group'],operation,guard,actor_id=request['sender_id'],
                    **({'source_message_id':-request['id']} if operation.action=='warn' else {})))
        if name in {'join_group', 'leave_group', 'join_call', 'leave_call', 'set_call_mode', 'call_status', 'accept_call'}:
            allowed = {'join_group': {'target'}, 'leave_group': {'target'}, 'join_call': {'target', 'mode'},
                       'leave_call': set(), 'set_call_mode': {'mode'}, 'call_status': set(), 'accept_call': {'recipient', 'mode'}}[name]
            if set(args) - allowed or (name in {'join_group', 'leave_group', 'join_call'} and 'target' not in args) or (name == 'set_call_mode' and set(args) != {'mode'}):
                raise ValueError('Неверные параметры группы или звонка')
            from .groups import target
            if 'target' in args:
                target(args['target'], calls=name == 'join_call')
            mode = args.get('mode', self.store.settings()['call_mode'])
            self.runtime.calls.validate_mode(mode)
            recipient = args.get('recipient')
            if recipient is not None and (not isinstance(recipient, str) or not re.fullmatch(r'@?[A-Za-z][A-Za-z0-9_]{3,31}|[0-9]{1,20}', recipient)):
                raise ValueError('Укажи @username или Telegram ID звонящего')
            if name == 'call_status':
                return self.runtime.calls.status()
            if name == 'set_call_mode':
                result = self.configure({'call_mode': mode})
                if self.runtime.calls.active:
                    await self.runtime.calls.mode(mode)
                return result
            guard = lambda: self.require(contact_id, telegram_id, trusted)
            async def operation():
                guard()
                if name == 'join_group':
                    return await self.runtime.calls.groups.join(args['target'], guard)
                if name == 'leave_group':
                    return await self.runtime.calls.groups.leave(args['target'], guard)
                if name == 'join_call':
                    return await self.runtime.calls.join(args['target'], mode, guard)
                if name == 'accept_call':
                    return await self.runtime.calls.accept(recipient, mode, guard)
                return await self.runtime.calls.leave()
            return await self.perform(request, name, name, operation)
        if name in {"create_custom_setting", "update_custom_setting", "delete_custom_setting"}:
            return self.custom_action(name, args)
        if name == "list_custom_settings":
            if args:
                raise ValueError("У списка пользовательских настроек нет параметров")
            return {"custom_settings": self.store.custom_settings()}
        if name == "search_avito":
            if set(args) - {"query", "city", "max_items", "newest", "max_price", "min_price", "description", "description_optional", "pages"}:
                raise ValueError("Неизвестные параметры поиска Авито")
            filters = validate_filters({key: args[key] for key in ("query", "max_price", "min_price", "description", "description_optional") if key in args})
            filters["city_slug"] = city_slug(args.get("city", "all"))
            url = avito_search_url(args.get("query"), args.get("city", "all"), args.get("newest", True),
                max_price=args.get("max_price"), min_price=args.get("min_price"),
                description=args.get("description", ""), description_optional=args.get("description_optional", False))
            count = args.get("max_items", 5)
            if type(count) is not int or not 1 <= count <= 50:
                raise ValueError("Выберите 1–50 объявлений")
            if not self.runtime.websites.cloud.configured():
                raise ValueError("Облачный сбор Авито выключен. Включи Bright Data в настройках Дарьи")
            pages = args.get("pages", 3)
            if type(pages) is not int or not 1 <= pages <= 3:
                raise ValueError("Выберите 1–3 страницы")
            async def submit():
                return await self.runtime.parse_website(url, max_items=count, pages=pages, contact_id=contact_id, filters=filters)
            fingerprint = json.dumps(dict(url=url, count=count, pages=pages, filters=filters), sort_keys=True)
            if retry_failed:
                # Only a panel-requested retry of a failed read-only collection can start again.
                while True:
                    rows = self.store.rows("SELECT status,result FROM owner_actions WHERE request_message_id=? AND action=? AND fingerprint=?",
                                           (request["id"], name, fingerprint))
                    if not rows or rows[0]["status"] != "sent":
                        break
                    previous_job = json.loads(rows[0]["result"]).get("job_id")
                    jobs = self.store.rows("SELECT status FROM jobs WHERE id=? AND contact_id=? AND provider='brightdata'", (previous_job,contact_id))
                    if not jobs or jobs[0]["status"] not in {"failed", "interrupted"}:
                        break
                    fingerprint += "|retry:" + str(previous_job)
            return await self.perform(request, name, fingerprint, submit)
        if name == "owner_settings":
            fields = args.get("fields")
            if set(args) - {"fields"} or (fields is not None and (not isinstance(fields, list) or any(not isinstance(key, str) or key not in CONTROL_FIELDS for key in fields))):
                raise ValueError("Неизвестная настройка")
            view = self.view(request)
            if fields is not None:
                view["settings"] = {key: view["settings"][key] for key in fields}
            return view
        if name == "owner_contacts":
            return {"contacts": self.store.rows("SELECT id,name,username,kind,enabled,proactive,gift_allowed,hermes_allowed,birthday,notes,persona FROM contacts ORDER BY last_activity DESC LIMIT 100")}
        if name == "configure_contact":
            if set(args) != {"recipient", "settings"} or not isinstance(args["recipient"], str) or not isinstance(args["settings"], dict):
                raise ValueError("Укажи адресата и его настройки")
            try:
                values = ContactChanges.model_validate(args["settings"], strict=True).model_dump(exclude_none=True)
            except ValidationError:
                raise ValueError("Недопустимые настройки контакта") from None
            if not values:
                raise ValueError("Укажи настройки контакта")
            contact = await self.runtime.telegram.resolve(args["recipient"])
            self.require(contact_id, telegram_id, trusted)
            update_contact(self.store, contact["id"], values)
            self.store.event("Владелец изменил контакт через личный чат: " + contact["name"])
            return {"updated": values, "recipient": args["recipient"]}
        if name == "configure_darya":
            if set(args) != {"settings"}:
                raise ValueError("Укажите объект settings")
            result = self.configure(args["settings"])
            if self.runtime.calls.active:
                if result['updated'].get('calls_enabled') is False:
                    await self.runtime.calls.leave()
                elif 'call_mode' in result['updated']:
                    await self.runtime.calls.mode(result['updated']['call_mode'])
            return result
        if name == "set_reply_speed":
            speeds = {
                "fast": dict(read_delay_min=0, read_delay_max=0, reply_delay_min=0, reply_delay_max=0,
                             burst_window=0.2, min_interval=0),
                "normal": dict(read_delay_min=1, read_delay_max=4, reply_delay_min=2, reply_delay_max=7,
                               burst_window=2, min_interval=10, natural_enabled=True),
                "slow": dict(read_delay_min=3, read_delay_max=7, reply_delay_min=8, reply_delay_max=15,
                             burst_window=3, min_interval=15, natural_enabled=True),
            }
            if set(args) != {"speed"} or not isinstance(args["speed"], str) or args["speed"] not in speeds:
                raise ValueError("Выберите fast, normal или slow")
            return self.configure(speeds[args["speed"]])
        if name == "set_profile_photo":
            if set(args) - {"photo_message_id"}:
                raise ValueError("Выберите фото, присланное владельцем")
            photo_id, photo = self.photo(request, args.get("photo_message_id"))
            async def upload():
                self.require(contact_id, telegram_id, trusted)
                await self.runtime.telegram.set_profile_photo(photo)
                self.runtime.account.profiles.clear()
                return {"photo_changed": True, "photo_message_id": photo_id}
            return await self.perform(request, name, str(photo_id), upload)
        if name == "set_profile_details":
            limits = {"first_name": (1, 64), "last_name": (0, 64), "bio": (0, 70)}
            if not args or set(args) - limits.keys() or any(not isinstance(value, str) or not limits[key][0] <= len(value) <= limits[key][1] for key, value in args.items()):
                raise ValueError("Имя/фамилия до 64 символов, описание до 70")
            async def update():
                self.require(contact_id, telegram_id, trusted)
                await self.runtime.telegram.set_profile_details(args)
                self.runtime.account.profiles.clear()
                return {"profile_changed": True}
            return await self.perform(request, name, "profile", update)
        if name == "send_message_to":
            recipient, text = args.get("recipient"), args.get("text")
            if set(args) != {"recipient", "text"} or not isinstance(recipient, str) or not re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}|-?[0-9]{1,20}", recipient):
                raise ValueError("Укажи @username или Telegram ID адресата")
            if not isinstance(text, str) or not 1 <= len(text) <= 4000 or not text.strip():
                raise ValueError("Сообщение должно содержать 1–4000 символов")
            try:
                contact = await self.runtime.telegram.resolve(recipient)
            except Exception:
                raise ValueError("Не удалось найти адресата. Проверь @username; по ID аккаунт должен уже знать контакт") from None
            async with self.runtime.send_lock:
                self.require(contact_id, telegram_id, trusted)
                self.runtime.telegram.require_unblocked(contact["id"])
                if not self.store.contact(contact["id"])["enabled"]:
                    raise ValueError("Отправка этому контакту отключена в панели")
                self.runtime.rate_check(contact["id"], time.time(), self.store.settings())
                async def send():
                    message_id = await self.runtime.telegram.send(contact["id"], text, manual=True,
                        before_dispatch=lambda: self.require(contact_id, telegram_id, trusted))
                    self.store.message(contact["id"], "assistant", text, message_id, "owner_command")
                    self.store.event(f"Владелец отправил сообщение через чат: {contact['name']}")
                    return {"sent": True, "recipient": recipient, "message_id": message_id}
                # A retry with a reworded text or an alias must not send twice.
                return await self.perform(request, name, str(contact["id"]), send)
        raise ValueError("Неизвестная команда владельца")
