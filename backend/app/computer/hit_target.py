"""Whether a click's stated target is what the point actually holds.

Pure functions over two plain descriptions: the few words the model named, and
what the page reported at the coordinate before the click ran.  No network, no
DOM, no agent -- the decision is meant to be reasoned about and tested without a
browser, because it is the decision that decides whether a click happens at all.

The rule is one-sided on purpose.  Refusing costs the model one retry and tells
it exactly what was under its pointer; letting the click through costs a click
on the wrong control and a history line claiming the right one.  So this only
refuses when the two descriptions disagree about something specific, and stays
whenever they cannot be compared: no page, no element, no name, or a target that
says nothing but "button".  A target the model did not give is never a mismatch,
which is what keeps the check off the path of every click that claimed nothing.

Three disagreements are worth a refusal, in order of how sure they are:

1. the point is not on the page at all -- browser chrome or another window --
   while the model named a control it expected to find there;
2. the model asked for somewhere text can be typed and the point holds a
   control that takes no text (a link, a tab, a menu item);
3. both sides carry real words and not one of them appears in the other.
"""

from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, Optional

#: A target is a few words, not a sentence: bound it so a model cannot spend a
#: paragraph on the field and turn the check into a text-matching exercise.
MAX_TARGET_LENGTH = 80

_TOKEN_RE = re.compile(r"[a-z0-9]+")

#: Words that carry no meaning about a control on either side: articles,
#: prepositions, pronouns.  Dropped from both sides before comparing, so that
#: "the button on the top bar" and "top bar button" mean the same thing here.
STOPWORDS = frozenset("""
a an the at on in of to for and or is it its my your you this that there here
with by from as be are please just now very
""".split())

#: Words that name the *kind* of thing rather than which one, dropped from both
#: sides for the same reason.  "Post button" and "Post" have to be the same
#: target, and they are only the same target once "button" stops counting as a
#: word that has to match.  Colors, sizes, positions and ordinals are here too:
#: a target that says "blue round button" or "the first button" has said
#: nothing the page can be expected to echo back.
#:
#: So are the verbs.  "Click" is what the command is, not what the target is,
#: and leaving it in meant a target of "click the textarea" had to find the
#: words "click" and "textarea" inside an accessible name -- which a page that
#: labelled its field "Write a comment" never would, so a correct click was
#: refused for a page being helpful about its own labels.
#:
#: Checked separately where they matter: a field word ("box", "field") is the
#: half of rule 2 that says *what kind of control* was asked for, and stays
#: meaningful even though it is not something a name can match.  Rule 2 reads
#: them out of the raw tokens, so dropping them from here costs that check
#: nothing and keeps rule 3 to words that identify one control rather than
#: describe a class of them.
GENERIC = frozenset("""
button link icon tab menu item widget control element page site thing one box
field input row column list bar label text value name word point spot place
area region side corner edge blue red green yellow black white gray grey dark
light bright big small large little round square top bottom left right upper
lower new old main other same current
click press tap choose select open close submit type
first second third fourth last next previous prev
toggle switch dropdown checkbox radio
textarea textbox textfield searchbox editor editing caret cursor composer
""".split())

#: Words that ask for somewhere text can be typed.  Deliberately narrow and
#: deliberately free of the ambiguous ones: "search box" is a claim about a
#: field, while "search" alone could be the button that submits it, and "check
#: box" is a checkbox, not a text field.
#:
#: Read from the raw tokens rather than from the content words, so a word can
#: be listed here and in `GENERIC` at once -- one says *what kind of control*
#: was wanted, the other keeps it out of the name comparison, and the two never
#: ask the same question.
FIELD_HINTS = frozenset("""
type typing typed write writing fill filled input textbox textarea searchbox
textfield editor editing caret cursor box field composer
""".split())

#: Roles that plainly take no text, which is what makes rule 2 a real
#: disagreement rather than a difference of opinion.  Checkboxes and radios are
#: absent on purpose: "check box" asks for one and must not be refused for
#: finding one.
NON_TEXT_ROLES = frozenset("""
button link tab menuitem menuitemcheckbox menuitemradio switch slider
listbox option combobox
""".split())


def clean_target(raw: Any) -> str:
    """The target as it will be compared: whitespace collapsed, bounded.

    Anything that is not a string is not a target, so a model that sends an
    object or a list gets no check rather than a check over a description of its
    own type error.
    """
    if not isinstance(raw, str):
        return ""
    return " ".join(raw.split())[:MAX_TARGET_LENGTH]


def _tokens(text: str) -> FrozenSet[str]:
    return frozenset(_TOKEN_RE.findall((text or "").lower()))


def _content(text: str) -> FrozenSet[str]:
    """What the words say, without the words that say nothing."""
    return _tokens(text) - STOPWORDS - GENERIC


def _forms(token: str) -> FrozenSet[str]:
    """The token and the spellings it could plausibly have been written as.

    Both sides go through this, so the question is only whether the two writers
    spelled the same word the same way -- "following" against "follows" is a
    match, and it is a match in both directions because both are expanded the
    same way.  No stemming library: a handful of suffixes covers how control
    names are actually written, and anything it misses errs towards accepting
    the click, which is the safe direction.
    """
    forms = {token}
    word = token
    if len(word) > 4 and word.endswith("ies"):
        forms.add(word[:-3] + "y")
    if len(word) > 4 and word.endswith("es"):
        forms.add(word[:-2])
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        forms.add(word[:-1])
    if len(word) > 5 and word.endswith("ing"):
        forms.add(word[:-3])
        forms.add(word[:-3] + "e")
    if len(word) > 4 and word.endswith("ed"):
        forms.add(word[:-2])
        forms.add(word[:-1])
    if len(word) > 3 and word.endswith("e"):
        forms.add(word[:-1])
    if len(word) > 3 and word.endswith("ly"):
        forms.add(word[:-2])
    return forms


def _describe(element: Dict[str, Any]) -> str:
    """How the element is named in a refusal: what it is, then what it is called."""
    role = " ".join(str(element.get("role") or "").split())[:40]
    name = " ".join(str(element.get("name") or "").split())[:60]
    if role and name:
        return f'{role} "{name}"'
    if name:
        return f'"{name}"'
    if role:
        return f"a {role}"
    tag = " ".join(str(element.get("tag") or "").split())[:20]
    if tag:
        return f"a <{tag}>"
    return "something else"


def describe_element(element: Any) -> str:
    """The same description, for a report line rather than a refusal.

    Kept beside the refusal text so the two cannot drift: what the loop tells
    the model a click landed on and what it tells it a click was refused for
    are phrased the same way.
    """
    if not isinstance(element, dict) or not element:
        return ""
    return _describe(element)


def target_mismatch(target: str, hit: Optional[Dict[str, Any]],
                    x: float, y: float) -> str:
    """Why this click is not the control it claims to be, or "" when it may run.

    `hit` is what the page reported at the coordinate before the click, or None
    when the page could not be asked.  None is not a mismatch: an unreadable
    page is a reason to run the click unverified, never a reason to refuse it,
    and refusing here would turn every browser the daemon cannot reach into a
    loop that refuses to move.
    """
    target = clean_target(target)
    if not target:
        return ""
    if not isinstance(hit, dict) or not hit.get("ok"):
        return ""
    if hit.get("in_page") is False:
        # The model named a control it expects at a point that is not on the
        # page.  Its own claim is what makes this refuseable: without a target
        # there is no expectation to contradict, and clicking the desktop is
        # the click command's own business to refuse or not.
        return (
            f"click was not performed: ({x:g}, {y:g}) is not on the page -- it is "
            f"browser chrome or another window -- and you asked for {target!r}"
        )
    element = hit.get("element")
    if not isinstance(element, dict) or not element:
        return ""

    role = str(element.get("role") or "").lower()
    target_words = _tokens(target)
    if (FIELD_HINTS & target_words
            and element.get("editable") is False
            and role in NON_TEXT_ROLES):
        return (
            f"click was not performed: ({x:g}, {y:g}) is over "
            f"{_describe(element)}, which takes no text, and you asked for "
            f"{target!r}"
        )

    wanted = _content(target)
    actual = _content(str(element.get("name") or ""))
    if not wanted or not actual:
        # One side said nothing that could disagree.  Accepting is the only
        # honest answer: there is no claim here to check.
        return ""
    # Both sides are expanded the same way, so "following" against "follows"
    # is a match whichever way round the two are written.
    actual_forms = frozenset().union(*(_forms(word) for word in actual))
    for word in wanted:
        if _forms(word) & actual_forms:
            return ""
    return (
        f"click was not performed: ({x:g}, {y:g}) is over "
        f"{_describe(element)}, not {target!r}"
    )
