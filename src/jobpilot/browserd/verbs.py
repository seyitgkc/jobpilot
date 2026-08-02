"""Selector resolution and the constrained verb set.

These are the only DOM mutations permitted anywhere in the system: set,
select, check, upload, and click_mapped against controls resolved into the
selector map. Resolution prefers semantic locators (labels, roles) and falls
back to CSS; anything unresolved is reported, never guessed at.

Descriptors are plain JSON so the resolved map survives in the DB and can be
re-validated and reused after restarts or deferred answers.
"""

from __future__ import annotations

from playwright.async_api import Locator, Page

VERBS = frozenset({"set", "select", "check", "upload", "click_mapped"})

_TAG_FOR_TYPE = {
    "select": "select",
    "multiselect": "select",
    "textarea": "textarea",
    "checkbox": "input[type=checkbox]",
    "file": "input[type=file]",
}


def _locator(page: Page, descriptor: dict) -> Locator:
    kind = descriptor.get("kind")
    if kind == "label":
        locator = page.get_by_label(descriptor["text"], exact=bool(descriptor.get("exact")))
        if descriptor.get("tag"):
            locator = locator.and_(page.locator(descriptor["tag"]))
        return locator.first
    if kind == "role":
        return page.get_by_role(descriptor["role"], name=descriptor["name"]).first
    if kind == "css":
        return page.locator(descriptor["selector"]).first
    if kind == "radio":
        # Resolved per option at apply time; this locator is the group.
        return page.locator(f"input[type=radio][name='{descriptor['name_attr']}']").first
    raise ValueError(f"unknown locator kind {kind!r}")


async def _try(page: Page, descriptor: dict) -> bool:
    try:
        locator = _locator(page, descriptor)
        await locator.wait_for(state="attached", timeout=1500)
        return True
    except Exception:
        return False


async def _count(page: Page, descriptor: dict) -> int:
    kind = descriptor.get("kind")
    try:
        if kind == "label":
            locator = page.get_by_label(
                descriptor["text"], exact=bool(descriptor.get("exact"))
            )
            if descriptor.get("tag"):
                locator = locator.and_(page.locator(descriptor["tag"]))
            return await locator.count()
        if kind == "css":
            return await page.locator(descriptor["selector"]).count()
        if kind == "role":
            return await page.get_by_role(
                descriptor["role"], name=descriptor["name"]
            ).count()
    except Exception:
        return 0
    return 0


async def resolve_field(page: Page, field: dict) -> dict | None:
    field_type = field.get("type", "text")
    label = field.get("label")
    field_id = field.get("field_id", "")
    tag = _TAG_FOR_TYPE.get(field_type)

    if field_type == "radio":
        group = {"kind": "radio", "name_attr": field_id}
        if await _try(page, group):
            return group
        return None

    if label:
        for exact in (False, True):
            descriptor = {"kind": "label", "text": label, "exact": exact}
            if tag:
                descriptor["tag"] = tag
            count = await _count(page, descriptor)
            if count == 1:
                return descriptor
            if count > 1 and exact:
                return descriptor | {"ambiguous": count}

    for selector in (f"[name='{field_id}']", f"#{field_id}"):
        descriptor = {"kind": "css", "selector": selector}
        if await _count(page, descriptor) == 1:
            return descriptor

    if field_type == "file":
        descriptor = {"kind": "css", "selector": "input[type=file]"}
        if await _count(page, descriptor) == 1:
            return descriptor
    return None


async def resolve_submit(page: Page, submit: dict | None) -> dict | None:
    label = (submit or {}).get("label") or "Submit"
    descriptor = {"kind": "role", "role": "button", "name": label}
    if await _count(page, descriptor) >= 1:
        return descriptor
    fallback = {"kind": "css", "selector": "button[type=submit], input[type=submit]"}
    if await _count(page, fallback) >= 1:
        return fallback
    return None


async def resolve_schema(page: Page, schema: dict) -> dict:
    resolved: dict[str, dict] = {}
    unresolved: list[str] = []
    for field in schema.get("fields", []):
        descriptor = await resolve_field(page, field)
        if descriptor is None or descriptor.get("ambiguous"):
            unresolved.append(field["field_id"])
            continue
        resolved[field["field_id"]] = descriptor
    submit = await resolve_submit(page, schema.get("submit_button"))
    if submit is not None:
        resolved["submit"] = submit
    elif schema.get("submit_button"):
        unresolved.append("submit")
    return {"resolved": resolved, "unresolved": unresolved}


async def _apply_one(page: Page, action: dict, selector_map: dict) -> dict:
    verb = action.get("verb")
    field_id = action.get("field_id", "")
    result = {"field_id": field_id, "verb": verb, "ok": False, "verified": False}
    if verb not in VERBS:
        result["error"] = f"unknown verb {verb!r}"
        return result
    descriptor = selector_map.get(field_id)
    if descriptor is None:
        result["error"] = "field is not in the selector map"
        return result

    try:
        if verb == "set":
            value = str(action.get("value") or "")
            locator = _locator(page, descriptor)
            await locator.fill(value, timeout=8000)
            read_back = await locator.input_value(timeout=3000)
            result["ok"] = True
            result["verified"] = read_back.strip() == value.strip()
        elif verb == "select":
            value = action.get("value")
            if descriptor.get("kind") == "radio":
                option = page.get_by_role("radio", name=str(value)).first
                await option.check(timeout=8000)
                result["ok"] = True
                result["verified"] = await option.is_checked()
            else:
                locator = _locator(page, descriptor)
                labels = value if isinstance(value, list) else [str(value)]
                try:
                    chosen = await locator.select_option(label=labels, timeout=8000)
                except Exception:
                    chosen = await locator.select_option(
                        value=[str(item) for item in labels], timeout=8000
                    )
                result["ok"] = True
                result["verified"] = len(chosen) == len(labels)
        elif verb == "check":
            locator = _locator(page, descriptor)
            desired = bool(action.get("value"))
            await locator.set_checked(desired, timeout=8000)
            result["ok"] = True
            result["verified"] = (await locator.is_checked()) == desired
        elif verb == "upload":
            locator = _locator(page, descriptor)
            await locator.set_input_files(action["path"], timeout=8000)
            read_back = await locator.input_value(timeout=3000)
            result["ok"] = True
            result["verified"] = bool(read_back)
        elif verb == "click_mapped":
            locator = _locator(page, descriptor)
            await locator.click(timeout=8000)
            result["ok"] = True
            result["verified"] = True
    except Exception as error:
        result["error"] = str(error).splitlines()[0][:300]
    return result


async def apply_actions(page: Page, actions: list[dict], selector_map: dict) -> list[dict]:
    results = []
    for action in actions:
        results.append(await _apply_one(page, action, selector_map))
    return results


async def crop(page: Page, descriptor: dict) -> bytes:
    locator = _locator(page, descriptor)
    await locator.scroll_into_view_if_needed(timeout=5000)
    return await locator.screenshot(type="png", timeout=5000)


async def validate_fields(page: Page, fields: list[dict], selector_map: dict) -> dict:
    outcome: dict[str, dict] = {}
    for field in fields:
        field_id = field.get("field_id", "")
        descriptor = selector_map.get(field_id)
        entry = {"present": False, "matches": False, "reason": None}
        if descriptor is None:
            entry["reason"] = "not in the selector map"
            outcome[field_id] = entry
            continue
        try:
            locator = _locator(page, descriptor)
            await locator.wait_for(state="attached", timeout=2500)
            entry["present"] = True
            if await locator.is_disabled():
                entry["reason"] = "control is disabled"
                outcome[field_id] = entry
                continue
            expected_options = field.get("options") or []
            if expected_options and descriptor.get("kind") != "radio":
                current = await locator.locator("option").all_text_contents()
                current_set = {option.strip() for option in current}
                missing = [
                    option for option in expected_options if option not in current_set
                ]
                if missing:
                    entry["reason"] = f"options changed; missing {missing[:5]}"
                    outcome[field_id] = entry
                    continue
            entry["matches"] = True
        except Exception as error:
            entry["reason"] = str(error).splitlines()[0][:200]
        outcome[field_id] = entry
    return outcome
