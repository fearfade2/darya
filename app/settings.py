from __future__ import annotations

import time
from urllib.parse import urlsplit
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona: str | None = Field(default=None, min_length=50, max_length=10000)
    voice_persona: str | None = Field(default=None, min_length=50, max_length=10000)
    enabled: bool | None = None
    proactive_enabled: bool | None = None
    proactive_hours: int | None = Field(default=None, ge=1, le=720)
    quiet_start: int | None = Field(default=None, ge=0, le=23)
    quiet_end: int | None = Field(default=None, ge=0, le=23)
    timezone: str | None = Field(default=None, max_length=80)
    hourly_limit: int | None = Field(default=None, ge=0, le=100)
    daily_contact_limit: int | None = Field(default=None, ge=0, le=100)
    min_interval: int | None = Field(default=None, ge=0, le=60)
    ai_base_url: str | None = Field(default=None, max_length=250)
    ai_model: str | None = Field(default=None, min_length=1, max_length=100)
    temperature: float | None = Field(default=None, ge=0, le=1.5)
    owner_id: int | None = Field(default=None, gt=0)
    hermes_enabled: bool | None = None
    brightdata_enabled: bool | None = None
    gifts_enabled: bool | None = None
    gift_autonomous_enabled: bool | None = None
    gift_max_price: int | None = Field(default=None, ge=0, le=1000000)
    gift_monthly_budget: int | None = Field(default=None, ge=0, le=10000000)
    gift_interval_hours: int | None = Field(default=None, ge=1, le=8760)
    gift_check_hours: int | None = Field(default=None, ge=1, le=168)
    gift_message: str | None = Field(default=None, max_length=1000)
    agent_max_rounds: int | None = Field(default=None, ge=1, le=16)
    natural_enabled: bool | None = None
    read_delay_min: float | None = Field(default=None, ge=0, le=30)
    read_delay_max: float | None = Field(default=None, ge=0, le=30)
    reply_delay_min: float | None = Field(default=None, ge=0, le=60)
    reply_delay_max: float | None = Field(default=None, ge=0, le=60)
    typing_cps: int | None = Field(default=None, ge=5, le=120)
    split_replies: bool | None = None
    reactions_enabled: bool | None = None
    presence_enabled: bool | None = None
    online_during_reply: bool | None = None
    reply_online_immediate: bool | None = None
    reply_idle_seconds: float | None = Field(default=None, ge=0, le=300)
    revise_reply_on_new_message: bool | None = None
    presence_interval_min: int | None = Field(default=None, ge=5, le=1440)
    presence_interval_max: int | None = Field(default=None, ge=5, le=1440)
    presence_duration_min: int | None = Field(default=None, ge=10, le=300)
    presence_duration_max: int | None = Field(default=None, ge=10, le=300)
    group_enabled: bool | None = None
    group_aliases: str | None = Field(default=None, min_length=1, max_length=500)
    vision_enabled: bool | None = None
    vision_model: str | None = Field(default=None, max_length=100)
    image_generation_enabled: bool | None = None
    image_generation_model: str | None = Field(default=None, max_length=200)
    image_generation_base_url: str | None = Field(default=None, max_length=250)
    image_generation_api_mode: Literal['images', 'chat'] | None = None
    burst_window: float | None = Field(default=None, ge=0, le=8)
    group_burst_window: float | None = Field(default=None, ge=0, le=8)
    typing_enabled: bool | None = None
    typing_during_generation: bool | None = None
    typing_start_delay: float | None = Field(default=None, ge=0, le=60)
    read_receipts_enabled: bool | None = None
    typing_min_seconds: float | None = Field(default=None, ge=0, le=120)
    typing_max_seconds: float | None = Field(default=None, ge=0, le=120)
    typing_variation_min: float | None = Field(default=None, ge=0.1, le=3)
    typing_variation_max: float | None = Field(default=None, ge=0.1, le=3)
    typing_pause_enabled: bool | None = None
    typing_pause_threshold: float | None = Field(default=None, ge=0, le=120)
    typing_pause_min: float | None = Field(default=None, ge=0, le=30)
    typing_pause_max: float | None = Field(default=None, ge=0, le=30)
    reply_part_delay_min: float | None = Field(default=None, ge=0, le=30)
    reply_part_delay_max: float | None = Field(default=None, ge=0, le=30)
    reaction_delay_min: float | None = Field(default=None, ge=0, le=30)
    reaction_delay_max: float | None = Field(default=None, ge=0, le=30)
    proactive_variation_min: float | None = Field(default=None, ge=1, le=3)
    proactive_variation_max: float | None = Field(default=None, ge=1, le=3)
    history_messages: int | None = Field(default=None, ge=4, le=100)
    reply_max_tokens: int | None = Field(default=None, ge=50, le=4000)
    reply_max_chars: int | None = Field(default=None, ge=100, le=4000)
    split_max_parts: int | None = Field(default=None, ge=1, le=6)
    split_max_chars: int | None = Field(default=None, ge=100, le=4000)
    reply_instructions: str | None = Field(default=None, max_length=4000)
    memory_enabled: bool | None = None
    group_names_enabled: bool | None = None
    group_mentions_enabled: bool | None = None
    group_replies_enabled: bool | None = None
    group_auto_reply_enabled: bool | None = None
    stickers_enabled: bool | None = None
    gifs_enabled: bool | None = None
    collect_telegram_media: bool | None = None

    private_initiative_enabled: bool | None = None
    private_initiative_min_minutes: int | None = Field(default=None, ge=5, le=1440)
    private_initiative_max_minutes: int | None = Field(default=None, ge=5, le=10080)
    private_initiative_max_unanswered: int | None = Field(default=None, ge=1, le=5)
    private_initiative_daily_limit: int | None = Field(default=None, ge=1, le=30)
    publishing_enabled: bool | None = None
    telegram_autonomy_enabled: bool | None = None
    telegram_autonomy_minutes: int | None = Field(default=None, ge=1, le=1440)
    telegram_autonomy_daily_limit: int | None = Field(default=None, ge=1, le=200)
    telegram_autonomy_max_actions: int | None = Field(default=None, ge=1, le=8)
    telegram_autonomy_instructions: str | None = Field(default=None, max_length=4000)
    telegram_autonomy_profile: bool | None = None
    telegram_autonomy_messages: bool | None = None
    telegram_autonomy_calls: bool | None = None
    telegram_autonomy_groups: bool | None = None
    telegram_autonomy_stories: bool | None = None
    telegram_autonomy_organize: bool | None = None
    telegram_autonomy_discovery: bool | None = None
    telegram_autonomy_new_contacts: bool | None = None
    telegram_autonomy_destructive: bool | None = None
    telegram_auto_answer_calls: bool | None = None
    telegram_call_cooldown_hours: int | None = Field(default=None, ge=1, le=168)

    self_protection_enabled: bool | None = None
    voice_speaker: Literal['xenia','kseniya','baya'] | None = None
    voice_messages_enabled: bool | None = None
    voice_message_speed: float | None = Field(default=None, ge=0.6, le=1.7)
    voice_message_volume: float | None = Field(default=None, ge=0.1, le=2)
    voice_message_max_chars: int | None = Field(default=None, ge=100, le=1000)
    calls_enabled: bool | None = None
    call_mode: Literal['listen', 'talk'] | None = None
    call_language: Literal['ru', 'en', 'auto'] | None = None
    call_voice_speed: float | None = Field(default=None, ge=0.6, le=1.7)
    call_voice_volume: float | None = Field(default=None, ge=0.1, le=2)
    call_silence_seconds: float | None = Field(default=None, ge=0.3, le=3)
    call_vad_threshold: float | None = Field(default=None, ge=0.001, le=0.2)
    call_min_speech_seconds: float | None = Field(default=None, ge=0.2, le=2)
    call_utterance_seconds: int | None = Field(default=None, ge=5, le=45)
    call_max_minutes: int | None = Field(default=None, ge=1, le=240)
    call_reply_to_all: bool | None = None
    call_interrupt_on_speech: bool | None = None
    call_wait_for_silence: bool | None = None
    call_initiative_enabled: bool | None = None
    call_initiative_seconds: int | None = Field(default=None, ge=5, le=600)
    call_initiative_max_unanswered: int | None = Field(default=None, ge=0, le=20)
    call_reply_max_tokens: int | None = Field(default=None, ge=50, le=800)
    call_reply_max_chars: int | None = Field(default=None, ge=100, le=1000)


def validate_url(value):
    url = urlsplit(value)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError("Адрес API должен начинаться с https:// и не содержать ключей")


def validate_settings(saved, values):
    if "timezone" in values:
        try:
            ZoneInfo(values["timezone"])
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Неизвестный часовой пояс") from None
    if "ai_base_url" in values:
        validate_url(values["ai_base_url"])
    if values.get('image_generation_base_url'):
        validate_url(values['image_generation_base_url'])
    merged = saved | values
    for prefix in ["read_delay", "reply_delay", "presence_interval", "presence_duration"]:
        if merged[prefix + "_min"] > merged[prefix + "_max"]:
            raise ValueError("Минимальная пауза не может превышать максимальную")
    for low, high in [("typing_min_seconds", "typing_max_seconds"),
                      *[(prefix + "_min", prefix + "_max") for prefix in
                        ["typing_variation", "typing_pause", "reply_part_delay", "reaction_delay", "proactive_variation"]]]:
        if merged[low] > merged[high]:
            raise ValueError("Минимум не может превышать максимум")
    if merged['private_initiative_min_minutes'] > merged['private_initiative_max_minutes']:
        raise ValueError('Минимальная пауза личной инициативы не может превышать максимальную')
    if "group_aliases" in values and not any(x.strip() for x in values["group_aliases"].split(",")):
        raise ValueError("Укажите хотя бы одно имя для групп")
    if merged["gifts_enabled"] and (merged["gift_max_price"] <= 0 or merged["gift_monthly_budget"] <= 0):
        raise ValueError("Для подарков задайте разовый и месячный бюджет")


class ContactChanges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool | None = None
    proactive: bool | None = None
    gift_allowed: bool | None = None
    hermes_allowed: bool | None = None
    calls_allowed: bool | None = None
    birthday: str | None = Field(default=None, max_length=10)
    notes: str | None = Field(default=None, max_length=4000)
    persona: str | None = Field(default=None, max_length=3000)


def update_contact(store, contact_id, values):
    contact = store.contact(contact_id)
    if values.get("birthday"):
        from datetime import date
        try:
            date.fromisoformat(values["birthday"])
        except ValueError:
            raise ValueError("Укажите дату рождения в формате ГГГГ-ММ-ДД") from None
    merged = contact | values
    with store.db:
        for key, value in values.items():
            if key not in ContactChanges.model_fields:
                raise ValueError("Неизвестная настройка контакта")
            store.db.execute(f"UPDATE contacts SET {key}=? WHERE id=?", (value, contact_id))
        if values.get("proactive") is True:
            store.db.execute("UPDATE contacts SET next_proactive=? WHERE id=?", (time.time() + store.settings()["proactive_hours"] * 3600, contact_id))
        if not merged["enabled"] or not merged["proactive"]:
            store.db.execute("UPDATE tasks SET status='cancelled' WHERE contact_id=? AND status='pending'", (contact_id,))
    return store.contact(contact_id)
