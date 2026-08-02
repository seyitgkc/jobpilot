"""Generic form extraction from rendered HTML.

Read the form, then fill the form: this builds the FormSchema that everything
downstream works against. Labels resolve through label[for], wrapping labels,
aria attributes, then placeholder; a control with none of those stays
unlabeled and the caller decides whether that blocks the fill.
"""

from __future__ import annotations

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


def _text(node: Tag | None) -> str | None:
    if node is None:
        return None
    text = " ".join(node.get_text(" ", strip=True).split())
    return text or None


def _label_for(soup: BeautifulSoup, control: Tag) -> str | None:
    control_id = control.get("id")
    if control_id:
        label = soup.find("label", attrs={"for": control_id})
        if label is not None:
            return _text(label)
    wrapper = control.find_parent("label")
    if wrapper is not None:
        clone = BeautifulSoup(str(wrapper), "html.parser")
        for inner in clone.find_all(["input", "select", "textarea"]):
            inner.decompose()
        text = " ".join(clone.get_text(" ", strip=True).split())
        if text:
            return text
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
    index = 0

    for control in soup.find_all(["input", "select", "textarea"]):
        index += 1
        tag = control.name
        input_type = (control.get("type") or "").lower() if tag == "input" else ""
        if tag == "input" and input_type in _SKIP_INPUT_TYPES:
            continue
        if control.has_attr("disabled") or control.get("aria-hidden") == "true":
            continue

        label = _label_for(soup, control)
        section = _section_for(control)

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
                label=_section_for(control) or None,
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
        else:
            field_type = _TEXT_TYPES.get(input_type, "text")
            options = []

        fields.append(
            FormField(
                field_id=_field_id(control, label, index),
                label=label,
                type=field_type,
                options=options,
                maxlen=_maxlen(control),
                required=_required(control),
                section=section,
            )
        )

    submit = None
    for button in soup.find_all(["button", "input"]):
        button_type = (button.get("type") or "").lower()
        if button.name == "button" and button_type in ("submit", ""):
            submit = FormField(field_id="submit", label=_text(button) or "Submit")
            break
        if button.name == "input" and button_type == "submit":
            submit = FormField(field_id="submit", label=button.get("value") or "Submit")
            break

    return FormSchema(fields=fields, submit_button=submit)
