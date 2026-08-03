"""Generic form extraction from rendered HTML.

Read the form, then fill the form: this builds the FormSchema that everything
downstream works against. Labels resolve through label[for], wrapping labels,
aria attributes, then placeholder; a control with none of those stays
unlabeled and the caller decides whether that blocks the fill.
"""

from __future__ import annotations

import json
import re

from bs4 import BeautifulSoup, Tag

from ..providers.schemas import FormField, FormSchema

_TEXT_TYPES = {
    "text": "text",
    "email": "email",
    "tel": "phone",
    "url": "url",
    "number": "number",
    "date": "date",
    "search": "text",
    "": "text",
}

_SKIP_INPUT_TYPES = {"hidden", "submit", "button", "image", "reset"}

# Consent banners sit above the form in the DOM and their dismiss control is
# often the first button on the page, so it wins any document-order scan.
_CONSENT_MARKERS = ("cc-btn", "cc-dismiss", "cookie", "consent", "gdpr")
_SUBMIT_TEXT = re.compile(r"\b(submit|send application)\b", re.I)


def _is_consent_control(node: Tag) -> bool:
    tokens = " ".join(node.get("class") or []) + " " + (node.get("id") or "")
    return any(marker in tokens.lower() for marker in _CONSENT_MARKERS)


def _is_hidden(node: Tag) -> bool:
    if node.has_attr("hidden"):
        return True
    if "hidden" in [name.lower() for name in (node.get("class") or [])]:
        return True
    style = (node.get("style") or "").replace(" ", "").lower()
    return "display:none" in style or "visibility:hidden" in style


# A honeypot is a field a person cannot see and therefore never fills, so
# filling it identifies the filler as automated. Anything hidden, sized to
# nothing, or parked off the canvas is left alone, along with the field names
# the common form libraries use as bait.
_HONEYPOT_NAMES = (
    "honeypot", "honey-pot", "hp-field", "bot-field", "botfield",
    "_gotcha", "do-not-fill", "donotfill", "leaveblank", "leave-blank",
)
_OFFSCREEN = re.compile(r"(?:left|top|text-indent)\s*:\s*-\s*\d{3,}")


def _is_offscreen(node: Tag) -> bool:
    style = (node.get("style") or "").lower()
    if _OFFSCREEN.search(style):
        return True
    squashed = style.replace(" ", "")
    return any(
        marker in squashed
        for marker in ("opacity:0", "width:0", "height:0", "font-size:0")
    )


def _is_honeypot(control: Tag) -> bool:
    identifiers = " ".join(
        [
            " ".join(control.get("class") or []),
            control.get("id") or "",
            control.get("name") or "",
        ]
    ).lower()
    if any(marker in identifiers for marker in _HONEYPOT_NAMES):
        return True
    for node in [control, *control.parents]:
        if not isinstance(node, Tag):
            continue
        if node.name in ("body", "html"):
            break
        if _is_hidden(node) or _is_offscreen(node):
            return True
        if node.get("aria-hidden") == "true":
            return True
    return False


def _text(node: Tag | None) -> str | None:
    if node is None:
        return None
    text = " ".join(node.get_text(" ", strip=True).split())
    return text or None


# Boards mark a required field with a glyph inside the label, and it reads as
# part of the question everywhere the label is shown.
_REQUIRED_MARK = re.compile(r"[\s*✱＊]+$")


def _clean_label(text: str | None) -> str | None:
    if text is None:
        return None
    return _REQUIRED_MARK.sub("", text).strip() or None


def _inner_label(wrapper: Tag) -> str | None:
    """The label's own caption, when the wrapper also holds the control's
    chrome. Typeahead widgets park status text like 'No location found'
    inside the label, which otherwise reads as part of the question."""
    for node in wrapper.find_all(["div", "span", "p"]):
        classes = " ".join(node.get("class") or []).lower()
        if "label" in classes and not node.find(["input", "select", "textarea"]):
            if text := _text(node):
                return text
    return None


def _label_for(soup: BeautifulSoup, control: Tag) -> str | None:
    control_id = control.get("id")
    if control_id:
        label = soup.find("label", attrs={"for": control_id})
        if label is not None:
            return _clean_label(_text(label))
    wrapper = control.find_parent("label")
    if wrapper is not None:
        if caption := _inner_label(wrapper):
            return _clean_label(caption)
        clone = BeautifulSoup(str(wrapper), "html.parser")
        for inner in clone.find_all(["input", "select", "textarea"]):
            inner.decompose()
        text = " ".join(clone.get_text(" ", strip=True).split())
        if text:
            return _clean_label(text)
    aria = control.get("aria-label")
    if aria and aria.strip():
        return aria.strip()
    labelled_by = control.get("aria-labelledby")
    if labelled_by:
        parts = [
            _text(soup.find(id=ref)) for ref in labelled_by.split() if soup.find(id=ref)
        ]
        text = " ".join(part for part in parts if part)
        if text:
            return text
    placeholder = control.get("placeholder")
    if placeholder and placeholder.strip():
        return placeholder.strip()
    return None


_CARD_TEMPLATE = re.compile(r"^cards\[([0-9a-f-]+)\]\[baseTemplate\]$", re.I)


def _card_questions(soup: BeautifulSoup) -> tuple[dict[str, str], dict[str, str]]:
    """Question text and card name for grouped custom questions.

    Some boards render a card of custom questions as bare controls named
    cards[uuid][fieldN] and keep the wording in a hidden input holding the
    card definition as JSON. Reading it is the difference between asking the
    owner about "cards[uuid][field2]" and asking them the actual question.
    Returns question text by field id, and card name by field id.
    """
    questions: dict[str, str] = {}
    sections: dict[str, str] = {}
    for hidden in soup.find_all("input", attrs={"type": "hidden"}):
        match = _CARD_TEMPLATE.match(hidden.get("name") or "")
        if match is None:
            continue
        try:
            payload = json.loads(hidden.get("value") or "")
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        card = match.group(1)
        name = " ".join((payload.get("text") or "").split()) or None
        entries = payload.get("fields")
        if not isinstance(entries, list):
            continue
        for position, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            field_id = f"cards[{card}][field{position}]"
            text = " ".join((entry.get("text") or "").split())
            if text:
                questions[field_id] = text
            if name:
                sections[field_id] = name
    return questions, sections


def _section_for(control: Tag) -> str | None:
    fieldset = control.find_parent("fieldset")
    if fieldset is not None:
        legend = fieldset.find("legend")
        if legend is not None:
            return _text(legend)
    return None


def _field_id(control: Tag, label: str | None, index: int) -> str:
    name = control.get("name")
    if name:
        return name
    control_id = control.get("id")
    if control_id:
        return control_id
    if label:
        slug = "".join(ch if ch.isalnum() else "_" for ch in label.lower()).strip("_")
        if slug:
            return slug
    return f"field_{index}"


def _required(control: Tag) -> bool:
    return control.has_attr("required") or control.get("aria-required") == "true"


def _maxlen(control: Tag) -> int | None:
    value = control.get("maxlength")
    try:
        return int(value) if value else None
    except ValueError:
        return None


def scan(html: str) -> FormSchema:
    soup = BeautifulSoup(html, "html.parser")
    fields: list[FormField] = []
    radios: dict[str, FormField] = {}
    card_questions, card_sections = _card_questions(soup)
    index = 0

    for control in soup.find_all(["input", "select", "textarea"]):
        index += 1
        tag = control.name
        input_type = (control.get("type") or "").lower() if tag == "input" else ""
        if tag == "input" and input_type in _SKIP_INPUT_TYPES:
            continue
        if control.has_attr("disabled") or control.get("aria-hidden") == "true":
            continue
        if _is_honeypot(control):
            continue

        label = _label_for(soup, control)
        section = _section_for(control)
        # On a radio, label is the option's own text, so the card question
        # names the group and never replaces the option.
        name = control.get("name") or ""
        question = card_questions.get(name)
        section = section or card_sections.get(name)

        if tag == "input" and input_type == "radio":
            group = control.get("name") or f"radio_{index}"
            option = label or control.get("value") or ""
            if group in radios:
                if option:
                    radios[group].options.append(option)
                if _required(control):
                    radios[group].required = True
                continue
            field = FormField(
                field_id=group,
                label=question or _section_for(control) or None,
                type="radio",
                options=[option] if option else [],
                required=_required(control),
                section=section,
            )
            radios[group] = field
            fields.append(field)
            continue

        if tag == "input" and input_type == "checkbox":
            field_type = "checkbox"
            options: list[str] = []
        elif tag == "input" and input_type == "file":
            field_type = "file"
            options = []
        elif tag == "textarea":
            field_type = "textarea"
            options = []
        elif tag == "select":
            field_type = "multiselect" if control.has_attr("multiple") else "select"
            options = [
                text
                for option in control.find_all("option")
                if (text := _text(option)) and option.get("value", text) != ""
            ]
        elif control.get("role") == "combobox":
            field_type = "select"
            options = _listbox_options(soup, control)
        else:
            field_type = _TEXT_TYPES.get(input_type, "text")
            options = []

        fields.append(
            FormField(
                field_id=_field_id(control, label, index),
                label=question or label,
                type=field_type,
                options=options,
                maxlen=_maxlen(control),
                required=_required(control),
                section=section,
            )
        )

    # Styled dropdowns built from non-form elements: anything else carrying
    # the combobox role.
    for control in soup.find_all(attrs={"role": "combobox"}):
        if control.name in ("input", "select", "textarea"):
            continue
        if control.has_attr("disabled") or control.get("aria-hidden") == "true":
            continue
        index += 1
        label = _label_for(soup, control)
        fields.append(
            FormField(
                field_id=_field_id(control, label, index),
                label=label,
                type="select",
                options=_listbox_options(soup, control),
                required=_required(control),
                section=_section_for(control),
            )
        )

    # Name the control before trusting its type. Boards drive submission from
    # JS, so the real button is often type=button, while a hidden type=submit
    # button belonging to a verification widget sits earlier in the document.
    submit = None
    fallback = None
    for button in soup.find_all(["button", "input"]):
        if _is_consent_control(button) or _is_hidden(button):
            continue
        button_type = (button.get("type") or "").lower()
        if button.name == "input":
            if button_type == "submit" and fallback is None:
                fallback = FormField(
                    field_id="submit", label=button.get("value") or "Submit"
                )
            continue
        marker = f"{button.get('data-qa') or ''} {button.get('id') or ''}"
        text = _text(button) or ""
        if "submit" in marker.lower() or _SUBMIT_TEXT.search(text):
            submit = FormField(field_id="submit", label=text or "Submit")
            break
        if button_type in ("submit", "") and fallback is None:
            fallback = FormField(field_id="submit", label=text or "Submit")
    submit = submit or fallback

    return FormSchema(
        fields=fields, submit_button=submit, apply_button=_apply_control(soup)
    )


def _listbox_options(soup: BeautifulSoup, control: Tag) -> list[str]:
    """Options of a combobox's linked listbox, when it is present in the DOM
    rather than rendered on open."""
    for attr in ("aria-controls", "aria-owns"):
        ref = control.get(attr)
        if not ref:
            continue
        listbox = soup.find(id=ref.split()[0])
        if listbox is None:
            continue
        return [
            text
            for option in listbox.find_all(attrs={"role": "option"})
            if (text := _text(option))
        ]
    return []


_APPLY_WORD = re.compile(r"\bapply\b", re.IGNORECASE)
_APPLY_THIRD_PARTY = re.compile(r"linkedin|indeed|glassdoor", re.IGNORECASE)


def _apply_control(soup: BeautifulSoup) -> FormField | None:
    """The button or link that reveals the application form on posting
    pages that gate it. Plain apply controls outrank third-party ones."""
    candidates = []
    for control in soup.find_all(["a", "button"]):
        text = _text(control)
        if not text or len(text) > 60 or not _APPLY_WORD.search(text):
            continue
        candidates.append((bool(_APPLY_THIRD_PARTY.search(text)), len(text), text))
    if not candidates:
        return None
    candidates.sort()
    return FormField(field_id="apply", label=candidates[0][2])
