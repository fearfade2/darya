from __future__ import annotations

import re
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit


CITIES = {'all': 'all', 'россия': 'all', 'вся россия': 'all', 'всей россии': 'all'}
for names, slug in [
    ('москва,москве,москву', 'moskva'), ('санкт-петербург,санкт-петербурге,спб', 'sankt-peterburg'),
    ('казань,казани', 'kazan'), ('симферополь,симферополе,симферополю', 'simferopol'),
    ('севастополь,севастополе', 'sevastopol'), ('ялта,ялте', 'yalta'), ('керчь,керчи', 'kerch'),
    ('евпатория,евпатории', 'evpatoriya'), ('феодосия,феодосии', 'feodosiya'), ('алушта,алуште', 'alushta'),
    ('санкт петербург,санкт петербурге', 'sankt-peterburg'),
    ('нижний новгород,нижнем новгороде', 'nizhniy_novgorod'),
    ('ростов-на-дону,ростове-на-дону', 'rostov-na-donu'), ('тверь,твери', 'tver'),
    ('орёл,орел,орле', 'orel'), ('пермь,перми', 'perm'), ('рязань,рязани', 'ryazan'),
    ('астрахань,астрахани', 'astrahan'), ('екатеринбург,екатеринбурге', 'ekaterinburg'),
    ('новосибирск,новосибирске', 'novosibirsk'), ('краснодар,краснодаре', 'krasnodar'),
    ('красноярск,красноярске', 'krasnoyarsk'), ('владивосток,владивостоке', 'vladivostok'),
    ('челябинск,челябинске', 'chelyabinsk'), ('самара,самаре', 'samara'), ('саратов,саратове', 'saratov'),
    ('уфа,уфе', 'ufa'), ('омск,омске', 'omsk'), ('томск,томске', 'tomsk'), ('сочи', 'sochi'),
    ('тюмень,тюмени', 'tyumen'), ('воронеж,воронеже', 'voronezh'), ('иркутск,иркутске', 'irkutsk'),
    ('ярославль,ярославле', 'yaroslavl'), ('калининград,калининграде', 'kaliningrad'),
    ('великий новгород,великом новгороде', 'velikiy_novgorod'),
]:
    CITIES.update({name: slug for name in names.split(',')})
    CITIES[slug] = slug

TRANSLIT = dict(zip('абвгдеёжзийклмнопрстуфхцчшщъыьэюя',
                    ['a','b','v','g','d','e','e','zh','z','i','y','k','l','m','n','o','p','r','s','t','u','f','h','ts','ch','sh','sch','','y','','e','yu','ya']))


def city_slug(city):
    if not isinstance(city, str) or not 1 <= len(city.strip()) <= 80:
        raise ValueError('Укажи название города или всю Россию')
    name = re.sub(r'^(?:город\s+|г\.\s*)', '', city.casefold().strip())
    if name in CITIES:
        return CITIES[name]
    if not re.fullmatch(r'[a-zа-яё][a-zа-яё _-]*', name):
        raise ValueError('Укажи название города без ссылок и специальных символов')
    # Avito confirms the location when the page and listing paths are read.
    return ''.join(TRANSLIT.get(ch, ch) for ch in name).replace(' ', '_')


def canonical_query(query):
    return re.sub(r'\bайфон(?:ов|ы|а|у|ом|е)?\b', 'iphone', query.strip(), flags=re.I)


def canonical_description(description):
    # Normalize the owner's wording only in search conditions, preserving negation.
    description = re.sub(r'\b(не)?заблоканн(ый|ая|ое|ые|ых|ого|ому|ой|ую|ым|ыми)\b',
                         lambda m: (m[1] or '')+'заблокированн'+m[2], description, flags=re.I)
    return re.sub(r'\b(не)?заблокан(а|о|ы)?\b',
                  lambda m: (m[1] or '')+'заблокирован'+(m[2] or ''), description, flags=re.I)


def validate_filters(filters):
    if not isinstance(filters, dict) or set(filters) - {'query', 'city_slug', 'max_price', 'min_price', 'description', 'description_optional'}:
        raise ValueError('Неизвестные условия поиска')
    for key in ('max_price', 'min_price'):
        if key in filters and (type(filters[key]) is not int or not 0 <= filters[key] <= 1000000000):
            raise ValueError('Цена должна быть целым числом рублей')
    if filters.get('min_price', 0) > filters.get('max_price', 1000000000):
        raise ValueError('Минимальная цена больше максимальной')
    for key in ('query', 'description'):
        if key in filters and (not isinstance(filters[key], str) or len(filters[key]) > 1000):
            raise ValueError('Описание поиска слишком длинное')
    if 'description_optional' in filters and type(filters['description_optional']) is not bool:
        raise ValueError('Укажи, обязательно ли описание')
    if 'city_slug' in filters:
        city_slug(filters['city_slug'])
    return filters | {'description': canonical_description(filters['description'])} if 'description' in filters else filters


def search_url(query, city='all', newest=True, max_price=None, min_price=None, description='', description_optional=False):
    if not isinstance(query, str) or not 1 <= len(query.strip()) <= 300:
        raise ValueError('Укажи товар для поиска на Авито')
    if type(newest) is not bool:
        raise ValueError('Укажи порядок поиска')
    filters = {'description': description, 'description_optional': description_optional}
    filters.update({key: value for key, value in [('max_price', max_price), ('min_price', min_price)] if value is not None})
    validate_filters(filters)
    region = city_slug(city)
    if region == 'all':
        region = 'rossiya'
    query = canonical_query(query)
    category = '/telefony' if re.search(r'\b(?:iphone|samsung|телефон)\b', query, re.I) else (
        '/noutbuki' if re.search(r'\b(?:ноутбук\w*|laptop\w*)\b',query,re.I) else '')
    params = {'q': query}
    if newest: params['s'] = '104'
    if max_price is not None: params['pmax'] = str(max_price)
    if min_price is not None: params['pmin'] = str(min_price)
    return f'https://www.avito.ru/{region}{category}?' + urlencode(params)


def parse_request(text):
    text = re.sub(r'^(?:Даша|Дарья)[,\s]+', '', text.strip(), flags=re.I)
    if not re.match(r'^(?:найди|поищи|подбери|собери|покажи)\b', text, re.I):
        return None
    if not re.search(r'\bавито\b|\b(?:айфон(?:ов|ы|а|у|ом|е)?|iphone)\s*\d+', text, re.I) or 'https://' in text or 'например' in text.casefold():
        return None
    body = re.sub(r'^(?:найди|поищи|подбери|собери|покажи)\s+(?:мне\s+)?(?:(?:на\s+)?авито\s*)?', '', text, flags=re.I)
    body = re.sub(r'\bна\s+авито\b','',body,flags=re.I)
    body = re.sub(r'\bайфон(?:ов|ы|а|у|ом|е)\b','айфон',body,flags=re.I)
    args = {'max_items': 5, 'newest': True}
    count = re.search(r'\bтоп\s*(\d{1,2})\b', body, re.I) or re.match(r'(\d{1,2})\s+', body)
    if count:
        args['max_items'] = int(count[1]); body = body[:count.start()] + body[count.end():]
    if re.search(r'\b(?:по\s+)?(?:всей\s+)?россии\b|\bвся\s+россия\b', body, re.I):
        args['city'] = 'all'
        body = re.sub(r'\b(?:по\s+)?(?:всей\s+)?россии\b|\bвся\s+россия\b', '', body, flags=re.I)
    else:
        for name in sorted(CITIES, key=len, reverse=True):
            if name in {'all','россия','вся россия','всей россии'}: continue
            location = re.search(r'\b(?:в|по)\s+' + re.escape(name) + r'(?=\s|$|[.,!?])', body, re.I)
            if location:
                args['city'] = name
                body = body[:location.start()] + body[location.end():]
                break
        # Unknown inflected city names need the language planner, not a guessed region.
        if 'city' not in args and re.search(r'\b(?:в|по)\s+[а-яёa-z]', body, re.I):
            return None
    price = re.search(r'(?:до|не\s+дороже)\s*(\d(?:[\d \u00a0]*\d)?(?:[.,]\d+)?)\s*(к|k|тыс\w*)?(?:\s*(?:₽|руб\w*))?', body, re.I)
    if price:
        value = float(re.sub(r'\s+', '', price[1]).replace(',', '.'))
        args['max_price'] = int(value * (1000 if price[2] else 1))
        body = body[:price.start()] + body[price.end():]
    price = re.search(r'\bот\s*(\d(?:[\d \u00a0]*\d)?(?:[.,]\d+)?)\s*(к|k|тыс\w*)?(?:\s*(?:₽|руб\w*))?', body, re.I)
    if price:
        value = float(re.sub(r'\s+', '', price[1]).replace(',', '.'))
        args['min_price'] = int(value * (1000 if price[2] else 1))
        body = body[:price.start()] + body[price.end():]
    description = re.search(r'\b(можно(?:\s+чтоб(?:ы)?)?|чтоб(?:ы)?|обязательно|только)\s+(?:он\s+)?(?:был\s+)?(.+)', body, re.I)
    if description:
        value = re.sub(r'\b(?:недавн\w*|свеж\w*|об[ъь]?явлен\w*)\b', '', description[2], flags=re.I).strip(' .,!?')
        if value:
            args['description'] = value
            args['description_optional'] = description[1].casefold().startswith('можно')
        body = body[:description.start()]
    body = re.sub(r'\b(?:топ|дешев\w*|самых|свеж\w*|новых|новые|последних|недавн\w*|об[ъь]?явлен\w*)\b', '', body, flags=re.I)
    phone = re.search(r'\b(?:айфон|iphone)\s*\d+(?:\s+(?:pro(?:\s+max)?|про(?:\s+макс)?|max|макс))?', body, re.I)
    if phone:
        extra = body[phone.end():].strip()
        if extra and 'description' not in args:
            args['description'], args['description_optional'] = extra, False
            body = body[:phone.end()]
    args['query'] = re.sub(r'\s+', ' ', body).strip(' .!?«»"')
    if 'description' in args:
        args['description'] = canonical_description(args['description'])
    return args if args['query'] else None


def filters_from_url(url):
    parsed = urlsplit(url)
    if parsed.hostname not in {'avito.ru', 'www.avito.ru'}: return {}
    query = parse_qs(parsed.query)
    region = parsed.path.strip('/').split('/')[0] or 'all'
    filters = {'query': query.get('q', [''])[0], 'city_slug': 'all' if region == 'rossiya' else region}
    for parameter, key in [('pmax','max_price'),('pmin','min_price')]:
        if query.get(parameter) and query[parameter][0].isdigit(): filters[key] = int(query[parameter][0])
    return filters


def clean_listing_url(url):
    parsed = urlsplit(url)
    if parsed.hostname in {'avito.ru','www.avito.ru'} and re.search(r'_\d+$', parsed.path):
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, '', ''))
    return url


def rubles(value):
    if re.search(r'/мес|в месяц|договор|\bот\b', value, re.I): return None
    match = re.fullmatch(r'\s*(\d[\d\s\u00a0]*)(?:[.,](\d{1,2}))?\s*(?:₽|руб\.?|RUB)?\s*', value, re.I)
    if not match: return None
    return int(re.sub(r'\D', '', match[1])) + (float('0.' + match[2]) if match[2] else 0)


def filter_items(items, filters):
    validate_filters(filters)
    query = canonical_query(filters.get('query', ''))
    phone = re.search(r'\biphone\s*(\d+)\b', query, re.I)
    selected = []
    for item in items:
        parsed = urlsplit(item['url'])
        if 'city_slug' in filters and (parsed.hostname not in {'avito.ru', 'www.avito.ru'} or not re.search(r'_\d+$', parsed.path)):
            continue
        price = rubles(item['price'])
        if 'max_price' in filters and (price is None or price > filters['max_price']): continue
        if 'min_price' in filters and (price is None or price < filters['min_price']): continue
        region = filters.get('city_slug', 'all')
        if region not in {'all','rossiya'} and urlsplit(item['url']).path.strip('/').split('/')[0] != region: continue
        if phone:
            title = canonical_query(item['title'])
            if not re.search(r'\biphone\s*' + re.escape(phone[1]) + r'\b', title, re.I): continue
            if not re.search(r'\b(?:pro|про|max|макс)\b', query, re.I) and re.search(r'\b(?:pro|про|max|макс)\b', title, re.I): continue
            accessories = r'\b(?:чехол|стекло|плата|дисплей|корпус|аккумулятор)\b'
            if re.search(accessories, title, re.I) and not re.search(accessories, query, re.I): continue
        selected.append(item)
    return selected
