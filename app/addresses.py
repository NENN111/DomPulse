"""Единый вид адреса дома и ключ для сопоставления в московском MVP."""
import re
import unicodedata

MOSCOW_PREFIX = re.compile(r'^(?:(?:россия|рф)\s*,\s*)?(?:(?:г|город)\.?\s*)?москва\b\s*,?\s*', re.IGNORECASE)
STREET = re.compile(
    r'\b(?:улица|ул\.?|аллея|проезд|проспект|пр-кт|шоссе|бульвар|переулок|'
    r'набережная|площадь|тупик|линия)\b', re.IGNORECASE,
)
LOWER_WORDS = {
    'улица', 'аллея', 'проезд', 'проспект', 'шоссе', 'бульвар', 'переулок',
    'набережная', 'площадь', 'тупик', 'линия', 'дом', 'корпус', 'строение', 'город',
}


def _clean(value: str) -> str:
    return re.sub(r'\s+', ' ', unicodedata.normalize('NFKC', value)).strip(' ,')


def address_key(value: str) -> str:
    text = _clean(value).casefold().replace('ё', 'е')
    text = MOSCOW_PREFIX.sub('', text)
    text = re.sub(r'\bул\.?(?=\s)', 'улица', text)
    text = re.sub(r'\b(?:дом|д)\.?(?=\s*\d)', ' ', text)
    text = re.sub(r'[.,;:]+', ' ', text)
    return re.sub(r'\s+', ' ', text).strip()


def canonical_house_address(value: str) -> str:
    text = _clean(value)
    if not text:
        return text
    match = MOSCOW_PREFIX.match(text)
    if match:
        city, body = 'Москва', text[match.end():].strip(' ,')
    else:
        parts = text.split(',', 1)
        has_other_city = len(parts) == 2 and not STREET.search(parts[0]) and STREET.search(parts[1])
        if has_other_city:
            city, body = parts[0].strip(), parts[1].strip(' ,')
        elif STREET.search(text):
            city, body = 'Москва', text
        else:
            city, body = '', text

    number = re.search(r'(?<!\w)(\d+[а-яa-z]?(?:/\d+[а-яa-z]?)?)\s*$', body, re.IGNORECASE)
    if number and STREET.search(body):
        before = body[:number.start()].rstrip(' ,')
        if not re.search(r'(?:^|[\s,])(?:корпус|к\.|строение|стр\.)\s*$', before, re.IGNORECASE):
            before = re.sub(r'(?:^|[\s,])(?:дом|д\.)\s*$', '', before, flags=re.IGNORECASE).rstrip(' ,')
            body = f'{before}, дом {number.group(1)}'

    def word_case(match):
        word = match.group(0).lower()
        return word if word in LOWER_WORDS else word[0].upper() + word[1:]

    body = re.sub(r'[^\W\d_]+', word_case, body, flags=re.UNICODE)
    if city and city != 'Москва':
        city = re.sub(r'[^\W\d_]+', word_case, city, flags=re.UNICODE)
    return f'{city}, {body}' if city and body else city or body
