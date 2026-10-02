"""Preserve Pixiv tag translations with their actual field and locale evidence."""
from __future__ import annotations

REQUESTED_LOCALE = "zh-cn"
_CHINESE_KEYS = ("zh", "zh-cn")
_FIELDS = ("translated_name", "language", "source", "source_field", "requested_locale", "language_basis")


def _text(value):
    return value if isinstance(value, str) and value.strip() else None


def parse_tag_details(illust, *, requested_locale=REQUESTED_LOCALE):
    """Read explicit Chinese keys first, then the App API's requested-locale field.

    App translated_name has no explicit language key. Its locale evidence is the
    request header, not a language guess based on the characters in its value.
    A present language dictionary takes precedence, including English-only data.
    """
    raw_tags = illust.get("tags", [])
    web_tags = isinstance(raw_tags, dict)
    raw_tags = raw_tags.get("tags", []) if web_tags else raw_tags
    if not isinstance(raw_tags, list):
        return []
    dictionary = illust.get("tagTranslation")
    dictionary = dictionary if isinstance(dictionary, dict) else {}
    details = []
    for tag in raw_tags:
        if not isinstance(tag, dict):
            continue
        name = _text(tag.get("name")) or _text(tag.get("tag"))
        if name is None:
            continue
        detail = {"name": name, **dict.fromkeys(_FIELDS), "requested_locale": requested_locale}
        maps = ((tag.get("translation"), "tags.tags[].translation" if web_tags else "tags[].translation"),
                (dictionary.get(name), "tagTranslation[tag]"))
        for translations, field in maps:
            if not isinstance(translations, dict):
                continue
            for language in _CHINESE_KEYS:
                translated = _text(translations.get(language))
                if translated is not None:
                    detail.update(translated_name=translated, language="zh-cn", source="pixiv",
                                  source_field=f"{field}.{language}", language_basis="explicit_language_key")
                    break
            if detail["translated_name"] is not None:
                break
        if (detail["translated_name"] is None and not web_tags
                and requested_locale.lower() in ("zh-cn", "zh")
                and not any(isinstance(translations, dict) for translations, _ in maps)):
            translated = _text(tag.get("translated_name"))
            if translated is not None:
                detail.update(translated_name=translated, language="zh-cn", source="pixiv",
                              source_field="tags[].translated_name", language_basis="requested_locale")
        details.append(detail)
    return details


def stored_translations(metadata):
    translations = (metadata or {}).get("tag_translations")
    return dict(translations) if isinstance(translations, dict) else {}


def merge_tag_translations(metadata, details, *, checked_at):
    """Merge positive observations only; absent or invalid fields do not erase archives."""
    translations = stored_translations(metadata)
    for detail in details if isinstance(details, list) else []:
        if (not isinstance(detail, dict) or _text(detail.get("name")) is None
                or _text(detail.get("translated_name")) is None
                or detail.get("language") != "zh-cn" or detail.get("source") != "pixiv"
                or detail.get("language_basis") not in ("explicit_language_key", "requested_locale")
                or _text(detail.get("source_field")) is None):
            continue
        record = {key: detail.get(key) for key in _FIELDS}
        old = translations.get(detail["name"])
        if not isinstance(old, dict) or any(old.get(key) != value for key, value in record.items()):
            translations[detail["name"]] = {**record, "checked_at": checked_at}
    return {**(metadata or {}), "tag_translations": translations}


def tag_details(names, metadata):
    translations = stored_translations(metadata)
    result = []
    for name in names:
        record = translations.get(name)
        if (not isinstance(record, dict) or record.get("language") != "zh-cn"
                or record.get("source") != "pixiv" or _text(record.get("translated_name")) is None):
            record = {}
        result.append({"name": name, **{key: record.get(key) for key in _FIELDS},
                       "checked_at": record.get("checked_at")})
    return result
