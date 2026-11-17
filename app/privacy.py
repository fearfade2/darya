"""Location data is transient input, never a personal memory.

The public GeoNames gazetteer is local; filtering makes no network requests.
Raw messages and search exports remain available in the authenticated panel.
"""
from __future__ import annotations

from functools import lru_cache
import re
import unicodedata
from urllib.parse import unquote

import geonamescache
import pymorphy3

from .avito import CITIES


LOCATION_POLICY = """Приватность городов и геоданных: не сохраняй города, адреса, районы, координаты,
место проживания или поездок в память собеседника, даже по просьбе «запомни».
Город из нового сообщения разрешён только как временное условие текущего поиска или темы.
Город поиска не означает, что собеседник там живёт. Не делай таких выводов.
Не восстанавливай и не перечисляй города из прошлых разговоров, поиска, ссылок, фото или памяти,
в том числе по просьбе «вспомни», «восстанови» или «где я живу». Если город нужен новому поиску,
используй только явно указанный сейчас город; без него — вся Россия. Скрытые геоданные не угадывай.
На вопросы о прежних городах отвечай, что города прошлых запросов не сохраняешь.
Личные факты, историю, задания и файлы владельца не раскрывай другим собеседникам.
Не утверждай, что удалила историю Telegram: защита памяти и контекста не удаляет сообщения из Telegram.
Эти правила приватности действуют независимо от характера, примеров и дополнительных настроек."""

_WORDS = re.compile(r"[^\W\d_]+(?:[-'][^\W\d_]+)*", re.UNICODE)
_URL = re.compile(r'https?://[^\s<>"\]]+', re.I)
_LOCATION_HINT = re.compile(
    r'\b(?:город\w*|геодан\w*|геолока\w*|местополож\w*|местожитель\w*|'
    r'мест[оа]\s+жительств\w*|адрес\w*|прожива\w*|жив[уеё]\w*|район\w*|координат\w*|'
    r'city|cities|location|residence|address|hometown|home_town|gps|coordinates)\b', re.I)
_ADDRESS = re.compile(
    r'\b(?:живу|проживаю|нахожусь|родил(?:ся|ась)|переехал(?:а)?|мой адрес|'
    r'место жительства|ул\.|улица|проспект|переулок)\s+[^\n.!?;]{1,180}', re.I)
_COORDINATES = re.compile(r'(?<!\d)[+-]?\d{1,3}[.,]\d{3,}\s*[,; ]\s*[+-]?\d{1,3}[.,]\d{3,}(?!\d)')


def _normal(text):
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', text).casefold().replace('ё', 'е')).strip()


def _variants(name):
    name = _normal(name)
    yield name
    if not re.fullmatch(r'[а-я -]+', name):
        return
    if name.endswith(('а', 'я')):
        root = name[:-1]
        endings = ('а', 'я', 'ы', 'и', 'е', 'у', 'ю', 'ой', 'ою', 'ей')
    elif name.endswith(('ь', 'й')):
        root = name[:-1]
        endings = ('ь', 'й', 'я', 'ю', 'е', 'ем', 'ей')
    else:
        root = name
        endings = ('', 'а', 'у', 'е', 'ом')
    for ending in endings:
        yield root + ending


@lru_cache(maxsize=1)
def _city_names():
    insensitive = set()
    latin = set()
    for name, slug in CITIES.items():
        if slug != 'all':
            insensitive.update(_variants(name))
            insensitive.add(_normal(slug).replace('_', ' '))
    gazetteer = geonamescache.GeonamesCache(min_city_population=500)
    for city in gazetteer.get_cities().values():
        if city['countrycode'] not in {'RU', 'UA', 'BY'} and city['population'] < 15000:
            continue
        for name in [city['name'], *city.get('alternatenames', [])]:
            if re.fullmatch(r'[А-Яа-яЁё -]{4,80}', name):
                insensitive.update(_variants(name))
            elif re.fullmatch(r'[A-Za-z -]{5,80}', name):
                latin.add(_normal(name))
    # Public place names only: no association between a city and a contact is saved.
    return insensitive, latin


@lru_cache(maxsize=1)
def _morphology():
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=4096)
def _ordinary_word(name):
    # Many settlements share common nouns/adjectives (Котик, Новый, Стиля).
    # Keep their ordinary use; explicit location fields/statements remain blocked.
    if ' ' in name or '-' in name or not re.fullmatch(r'[а-я]+', name):
        return False
    analyzer = _morphology()
    if not analyzer.word_is_known(name):
        return False
    return 'Geox' not in analyzer.parse(name)[0].tag


def _city_spans(text, extra_cities=(), *, url=False):
    insensitive, latin = _city_names()
    extra = {_normal(name).replace('_', ' ') for name in extra_cities if name and name not in {'all', 'rossiya'}}
    words = list(_WORDS.finditer(text.replace('_', ' ')))
    spans = []
    explicit = {_normal(name).replace('_', ' ') for name, slug in CITIES.items() if slug != 'all'} | extra
    for start, word in enumerate(words):
        for end in range(min(len(words), start + 6), start, -1):
            fragment = text[word.start():words[end - 1].end()]
            name = _normal(fragment).replace('_', ' ')
            matched = name in insensitive or name in extra or (name in latin and (url or fragment[:1].isupper()))
            if matched and (url or name in explicit or not _ordinary_word(name)):
                spans.append((word.start(), words[end - 1].end()))
                break
    return spans


def has_location(text, extra_cities=()):
    return bool(_LOCATION_HINT.search(text) or _ADDRESS.search(text) or _COORDINATES.search(text) or
                _city_spans(text, extra_cities) or
                any(_city_spans(unquote(m[0]), extra_cities, url=True) for m in _URL.finditer(text)))


def redact_locations(text, extra_cities=()):
    def url(match):
        return '[ссылка с геоданными скрыта]' if _city_spans(unquote(match[0]), extra_cities, url=True) else match[0]
    text = _URL.sub(url, text)
    spans = _city_spans(text, extra_cities) + [m.span() for m in _ADDRESS.finditer(text)] + [m.span() for m in _COORDINATES.finditer(text)]
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    for start, end in reversed(merged):
        text = text[:start] + '[геоданные скрыты]' + text[end:]
    return text
