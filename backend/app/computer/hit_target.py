"""Whether a click's stated target is what the point actually holds.

Pure functions over two plain descriptions: the few words the model named, and
what the page reported at the coordinate before the click ran.  No network, no
DOM, no agent -- the decision is meant to be reasoned about and tested without a
browser, because it is the decision that decides whether a click happens at all.

The rule is one-sided on purpose.  Refusing costs the model one retry and tells
it exactly what was under its pointer; letting the click through costs a click
on the wrong control and a history line claiming the right one.  A target the
model did not give is never a mismatch -- but the click tool takes no click
without a target, so in practice there is always a claim in hand and this
function always has a claim to check.

Which is why the check fails closed wherever the claim cannot be checked.  The
page would not answer, the point is not on the page, or nothing at the
coordinate carries a name or a role: in each of those the click is refused with
the reason, because a click whose justification was "the control here is
*Post button*" and which confirmed nothing has not happened as described either
way.  An unreadable page used to be a pass here, and it read as success to a
model that had just been told its click landed.

And it fails open wherever the two descriptions cannot disagree, because a
target names a control the way a person does and a page names it the way a page
does.  Several signals are compared at once -- accessible name, aria-label,
title, placeholder, visible text, nearby context, and the kind of control that
the role, tag and input type describe -- and any one of them agreeing is
enough.  No site, no selector and no label of any particular page appears
anywhere in that comparison.

Four disagreements are worth a refusal, in order of how sure they are:

1. the point is not on the page at all -- browser chrome or another window --
   while the model named a control it expected to find there;
2. the model asked for somewhere text can be typed and the point holds a
   control that takes no text (a link, a tab, a menu item);
3. nothing at the coordinate can be named at all, so nothing can be confirmed;
4. not one word, kind or piece of context appears on both sides.
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


#: The ways an element can say what it is, read from the page's own DOM.  Every
#: one of them is a generic platform facility -- the accessibility tree, the
#: HTML attributes -- so the same list works on any page.
ELEMENT_TEXT_FIELDS = ("name", "aria_label", "title", "placeholder", "text",
                       "context")

#: How a role says itself to a person, so a target that says it the same way
#: can agree with an element that reports only its role.  Pure ARIA vocabulary:
#: the map recognises the words anyone uses to describe any control, and no
#: control name of any particular site appears in it.
_ROLE_KINDS: Dict[str, FrozenSet[str]] = {
    "button": frozenset("button"),
    "link": frozenset("link anchor"),
    "tab": frozenset("tab"),
    "checkbox": frozenset("checkbox"),
    "radio": frozenset("radio"),
    "switch": frozenset("switch toggle"),
    "slider": frozenset("slider"),
    "spinbutton": frozenset("spinbutton"),
    "option": frozenset("option item"),
    "menu": frozenset("menu"),
    "menuitem": frozenset("menuitem menu"),
    "menuitemcheckbox": frozenset("menuitem menu checkbox"),
    "menuitemradio": frozenset("menuitem menu radio"),
    "listbox": frozenset("listbox list"),
    "combobox": frozenset("combobox dropdown select list"),
    "textbox": frozenset("textbox field box input write editor"),
    "searchbox": frozenset("searchbox field box input write"),
    "img": frozenset("img image picture photo avatar"),
    "heading": frozenset("heading title"),
    "dialog": frozenset("dialog modal popup window"),
}

#: The same, for tags and input types, which is how an element describes itself
#: when it carries no ARIA role at all.
_TAG_KINDS: Dict[str, FrozenSet[str]] = {
    "a": frozenset("link anchor"),
    "button": frozenset("button"),
    "input": frozenset("field box input"),
    "textarea": frozenset("field box input write editor textarea"),
    "select": frozenset("dropdown select list"),
    "option": frozenset("option item"),
    "img": frozenset("img image picture photo avatar"),
    "summary": frozenset("disclosure button"),
}

_TYPE_KINDS: Dict[str, FrozenSet[str]] = {
    "button": frozenset("button"),
    "submit": frozenset("button"),
    "reset": frozenset("button"),
    "checkbox": frozenset("checkbox"),
    "radio": frozenset("radio"),
    "range": frozenset("slider"),
    "search": frozenset("searchbox field box input"),
    "text": frozenset("field box input"),
    "email": frozenset("field box input"),
    "password": frozenset("field box input"),
    "url": frozenset("field box input"),
    "tel": frozenset("field box input"),
    "number": frozenset("spinbutton field box input"),
}

#: Every word any of those maps can produce: the words a target may use to say
#: what *kind* of control it meant.  Derived from the maps rather than written
#: twice, so a word is only ever a kind word because something in the DOM could
#: have answered with it.
KIND_WORDS: FrozenSet[str] = frozenset(
    word
    for mapping in (_ROLE_KINDS, _TAG_KINDS, _TYPE_KINDS)
    for words in mapping.values()
    for word in words
)


def _element_kinds(element: Dict[str, Any]) -> FrozenSet[str]:
    """What kind of control this is, said in the words a person would use.

    Read from role, tag and input type -- three generic facts about any
    element.  The result only ever *widens* an agreement: a matching kind makes
    a click more confirmable, and a kind that disagrees with the target never
    refuses anything on its own (rule 2 is the one place a kind is decisive,
    and there it is about taking text at all).
    """
    kinds = set()
    kinds.update(_ROLE_KINDS.get(str(element.get("role") or "").lower(), ()))
    kinds.update(_TAG_KINDS.get(str(element.get("tag") or "").lower(), ()))
    kinds.update(_TYPE_KINDS.get(str(element.get("type") or "").lower(), ()))
    return frozenset(kinds)


def _element_name(element: Dict[str, Any]) -> str:
    """The element's best name, across every way a page is allowed to give one.

    In the order a person would reach for them: what it calls itself, what the
    accessibility tree resolved for it, its tooltip, its placeholder, then the
    text it shows.  A control with only visible text still gets described by
    that text rather than by "something else".
    """
    for key in ("name", "aria_label", "title", "placeholder"):
        value = " ".join(str(element.get(key) or "").split())
        if value:
            return value[:60]
    text = " ".join(str(element.get("text") or "").split())
    if text:
        return text[:60]
    return ""


def _is_identifying(element: Dict[str, Any]) -> bool:
    """Whether the element carries anything a person could recognise it by.

    A role or any one of the text signals is enough.  Nothing at all -- an
    anonymous box with no name, no role, no text and no context -- means the
    click cannot be confirmed against anything, which is a refusal rather than a
    shrug: the model claimed a control and the DOM offered no way to check.
    """
    if str(element.get("role") or "").strip():
        return True
    for key in ELEMENT_TEXT_FIELDS:
        if str(element.get(key) or "").strip():
            return True
    return False


def _describe(element: Dict[str, Any]) -> str:
    """How the element is named in a refusal: what it is, then what it is called."""
    role = " ".join(str(element.get("role") or "").split())[:40]
    name = _element_name(element)
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
    when the page could not be asked.  With a target in hand, an answer that did
    not arrive is a refusal rather than a pass: the click's whole justification
    is that the control at this point was confirmed to be the named one, and
    "could not confirm" is not "confirmed".  The refusal says so plainly instead
    of letting a click on unknown ground report itself as one on a named
    control.

    Refusals are specific, acceptances are cheap: any single signal agreeing --
    a word of the name, the kind of control, a piece of nearby context -- sends
    the click on its way, and only a disagreement about something the model
    actually said stops it.
    """
    target = clean_target(target)
    if not target:
        return ""
    if not isinstance(hit, dict) or not hit.get("ok"):
        reason = ""
        if isinstance(hit, dict):
            reason = str(hit.get("error") or "").strip()[:120]
        detail = f": {reason}" if reason else ""
        return (
            f"click was not performed: the live page could not be read at "
            f"({x:g}, {y:g}){detail}, so {target!r} could not be confirmed"
        )
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
        return (
            f"click was not performed: nothing is at ({x:g}, {y:g}) on the page, "
            f"so {target!r} could not be confirmed"
        )
    if not _is_identifying(element):
        return (
            f"click was not performed: nothing at ({x:g}, {y:g}) has a name or a "
            f"role, so {target!r} could not be confirmed"
        )

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
    if not wanted:
        # The target named no specific control -- "button", "the link".  There
        # is no claim here that the point can contradict, so there is nothing
        # to refuse.
        return ""

    # Everything the element says about itself, across all of its signals, and
    # the kind of control it is.  Either half agreeing is enough: a page may
    # carry the words in its context and only its role in its name, or the
    # reverse, and a person naming the control saw one or the other.
    seen = set()
    for key in ELEMENT_TEXT_FIELDS:
        seen |= _content(str(element.get(key) or ""))
    kinds = _element_kinds(element)
    seen_forms = frozenset().union(*(_forms(word) for word in seen)) if seen else frozenset()
    kind_forms = frozenset().union(*(_forms(word) for word in kinds)) if kinds else frozenset()
    for word in wanted:
        forms = _forms(word)
        if forms & (seen_forms | kind_forms):
            return ""
    # The kind words the target used -- "button", "field", "checkbox" -- are
    # checked against the element's kind rather than against its content, since
    # a page rarely writes the word "button" anywhere on the control itself.
    for word in (target_words & KIND_WORDS):
        if _forms(word) & kind_forms:
            return ""
    return (
        f"click was not performed: ({x:g}, {y:g}) is over "
        f"{_describe(element)}, not {target!r}"
    )
