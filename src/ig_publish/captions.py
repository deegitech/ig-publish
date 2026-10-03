"""Caption checks for Reels (the API takes no caption for Stories).

All rules come from the ``[captions]`` and ``[seasons]`` configuration:

* hard limits from Meta's reference: at most 2,200 characters, 30 hashtags and 20 @-mentions;
* ``required_text``: strings every caption must contain (a call to action, a disclosure ...);
* ``banned_words``: case-insensitive *substring* match, so ``#bigsale`` is caught by ``sale``;
* ``licensed_music``: song titles, artists or lyrics that must never be named. Matched ignoring case, spaces,
  punctuation and ``#``, so ``"Song-Title!"`` and ``#SongTitle`` are caught by ``Song Title``;
* season words: a word listed for a season may only appear in items tagged with that season;
* ``forbid_curly_quotes``: optional house style.
"""
from __future__ import annotations

import re

from .config import MENTION_MAX, CaptionRules, Season

CURLY_QUOTES = re.compile('[‘’“”]')
HASHTAG = re.compile(r'#\w+')
MENTION = re.compile(r'(?<![\w.])@[\w.]+')


def squash(text: str) -> str:
    """Lower-case letters and digits only (Unicode-aware)."""
    return re.sub(r'[\W_]+', '', text.casefold())


def caption_problems(caption: str, season: str | None, rules: CaptionRules,
                     seasons: dict[str, Season]) -> list[str]:
    out: list[str] = []
    if len(caption) > rules.max_length:
        out.append(f'caption is {len(caption)} characters (max {rules.max_length})')
    tags = HASHTAG.findall(caption)
    if len(tags) > rules.max_hashtags:
        out.append(f'{len(tags)} hashtags (max {rules.max_hashtags})')
    if len(tags) < rules.min_hashtags:
        out.append(f'{len(tags)} hashtags (min {rules.min_hashtags})')
    mentions = MENTION.findall(caption)
    if len(mentions) > MENTION_MAX:
        out.append(f'{len(mentions)} @-mentions (max {MENTION_MAX})')
    for text in rules.required_text:
        if text not in caption:
            out.append(f'required text missing: {text!r}')
    low = caption.casefold()
    for word in rules.banned_words:
        if word.casefold() in low:
            out.append(f'contains banned word {word!r} (captions.banned_words)')
    sq = squash(caption)
    for term in rules.licensed_music:
        t = squash(term)
        if t and t in sq:
            out.append(f'names licensed music {term!r} (captions.licensed_music): never name a track the post '
                       f'does not carry')
    for name, s in seasons.items():
        if season == name:
            continue
        for word in s.caption_words:
            if word in low:
                out.append(f'"{word}" may only appear in items with "season": "{name}"')
    if rules.forbid_curly_quotes and CURLY_QUOTES.search(caption):
        out.append("curly quotes or apostrophes (captions.forbid_curly_quotes: use straight ' and \")")
    return out
