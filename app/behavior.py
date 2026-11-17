import random
import re


REACTIONS = ["❤", "👍", "😁", "🔥", "🥰", "👏", "🤣", "🤔", "😢", "🎉", "😍", "💔", "😴", "🤯", "🫡"]


def alias_names(aliases):
    names = {name.strip().casefold() for name in aliases.split(",") if name.strip()}
    if names & {'дарья', 'дария', 'daria'}:
        names.update({'дарья', 'дария', 'daria'})
    return names


def addressed(text, aliases):
    names = alias_names(aliases)
    return bool(names and re.search(r"(?<!\w)(?:" + "|".join(re.escape(n) for n in names) + r")(?!\w)", text, re.I))


def delay(settings, name):
    return random.uniform(settings[name + "_min"], settings[name + "_max"])


def chunks(text, enabled, max_parts=3, max_chars=1800):
    parts = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not enabled or len(parts) < 2 or len(text) > max_chars:
        return [text]
    if len(parts) <= max_parts:
        return parts
    return parts[:max_parts-1] + ["\n\n".join(parts[max_parts-1:])]
