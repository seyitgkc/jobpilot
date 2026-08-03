from jobpilot.core import formscan

FORM = """
<form>
  <fieldset>
    <legend>Basics</legend>
    <label for="fn">First name</label><input id="fn" name="first_name" required>
    <label for="em">Email address</label><input id="em" name="email" type="email">
    <input name="middle_name" placeholder="Middle name">
    <input name="mystery">
  </fieldset>
  <label for="exp">Years of experience</label>
  <select id="exp" name="experience" required>
    <option value="">Select one</option>
    <option>1-3</option>
    <option>4-6</option>
    <option>7+</option>
  </select>
  <label for="skills">Skills</label>
  <select id="skills" name="skills" multiple><option>Go</option><option>SQL</option></select>
  <fieldset>
    <legend>Work authorization</legend>
    <label for="wa_yes">Yes</label><input id="wa_yes" type="radio" name="work_auth" value="yes" required>
    <label for="wa_no">No</label><input id="wa_no" type="radio" name="work_auth" value="no">
  </fieldset>
  <label>Cover letter<textarea name="cover" maxlength="4000"></textarea></label>
  <label for="agree">I agree to the terms</label><input id="agree" name="agree" type="checkbox">
  <label for="cv">Resume</label><input id="cv" name="resume" type="file" required>
  <input type="hidden" name="token" value="x">
  <input type="submit" value="Send application">
</form>
"""


def by_id(schema, field_id):
    field = schema.by_id(field_id)
    assert field is not None, field_id
    return field


def test_scan_extracts_fields_and_labels():
    schema = formscan.scan(FORM)
    ids = [field.field_id for field in schema.fields]
    assert "token" not in ids

    first = by_id(schema, "first_name")
    assert first.label == "First name"
    assert first.required
    assert first.section == "Basics"

    assert by_id(schema, "email").type == "email"
    assert by_id(schema, "middle_name").label == "Middle name"
    assert by_id(schema, "mystery").label is None

    experience = by_id(schema, "experience")
    assert experience.type == "select"
    assert experience.options == ["1-3", "4-6", "7+"]
    assert experience.required

    assert by_id(schema, "skills").type == "multiselect"

    radio = by_id(schema, "work_auth")
    assert radio.type == "radio"
    assert radio.options == ["Yes", "No"]
    assert radio.required

    cover = by_id(schema, "cover")
    assert cover.type == "textarea"
    assert cover.label == "Cover letter"
    assert cover.maxlen == 4000

    assert by_id(schema, "agree").type == "checkbox"
    assert by_id(schema, "resume").type == "file"


def test_scan_finds_submit():
    schema = formscan.scan(FORM)
    assert schema.submit_button is not None
    assert schema.submit_button.label == "Send application"


def test_scan_button_element():
    schema = formscan.scan("<form><button type='submit'>Apply now</button></form>")
    assert schema.submit_button.label == "Apply now"


def test_consent_banner_does_not_become_the_submit_button():
    # The banner's dismiss control sits above the form and used to win on
    # document order, and the verification widget's own submit button is
    # hidden. The real control carries type=button because the board submits
    # from JS.
    schema = formscan.scan(
        """
        <div class="cc-window">
          <button class="button button-sm cc-btn cc-dismiss" href="#">Dismiss</button>
        </div>
        <form>
          <label for="fn">First name</label><input id="fn" name="first_name">
          <button class="hidden" id="hcaptchaSubmitBtn" type="submit"></button>
          <button data-qa="btn-submit" id="btn-submit" type="button">
            Submit application
          </button>
        </form>
        """
    )
    assert schema.submit_button is not None
    assert schema.submit_button.label == "Submit application"


def test_honeypot_fields_are_not_offered_for_filling():
    schema = formscan.scan(
        """
        <form>
          <label for="fn">First name</label><input id="fn" name="first_name">
          <input name="honeypot" aria-label="Leave this empty">
          <input name="bot-field" aria-label="Bot field">
          <div style="display:none">
            <label for="h1">Company URL</label><input id="h1" name="company_url">
          </div>
          <div style="position:absolute; left:-9999px">
            <label for="h2">Fax</label><input id="h2" name="fax">
          </div>
          <input name="nickname" style="opacity:0" aria-label="Nickname">
        </form>
        """
    )
    assert [field.field_id for field in schema.fields] == ["first_name"]


def test_combobox_widgets_scan_as_selects():
    schema = formscan.scan(
        """
        <span id="sen-label">Seniority level</span>
        <div id="sen" role="combobox" aria-labelledby="sen-label"
             aria-controls="sen-list" aria-required="true">Choose</div>
        <ul id="sen-list" role="listbox">
          <li role="option">Junior</li><li role="option">Senior</li>
        </ul>
        <input name="team" role="combobox" aria-label="Team" aria-owns="team-list">
        <ul id="team-list" role="listbox"><li role="option">Platform</li></ul>
        """
    )
    by_id = {field.field_id: field for field in schema.fields}
    assert by_id["sen"].type == "select"
    assert by_id["sen"].label == "Seniority level"
    assert by_id["sen"].options == ["Junior", "Senior"]
    assert by_id["sen"].required
    assert by_id["team"].type == "select"
    assert by_id["team"].options == ["Platform"]


def test_apply_control_detected_behind_gate():
    schema = formscan.scan(
        """
        <h1>Role</h1>
        <label for="s">Link to This Job</label><input id="s" name="share_link">
        <a href="#">Apply with LinkedIn</a>
        <a href="#">Apply for This Job</a>
        """
    )
    assert schema.submit_button is None
    assert schema.apply_button is not None
    assert schema.apply_button.label == "Apply for This Job"


def test_apply_control_absent_on_real_form():
    schema = formscan.scan(
        "<form><label for='e'>Email</label><input id='e' name='email'>"
        "<button type='submit'>Submit application</button></form>"
    )
    assert schema.apply_button is None


def test_scan_empty_page():
    schema = formscan.scan("<html><body><p>Nothing here</p></body></html>")
    assert schema.fields == []
    assert schema.submit_button is None


CARD_HTML = """
<h4>Screening</h4>
<input type="hidden" name="cards[abc-123][baseTemplate]"
  value='{"text":"Screening Questions","fields":[
    {"type":"multiple-choice","text":"Are you authorized to work here?"},
    {"type":"textarea","text":"What are your salary expectations?"}]}'>
<label><input type="radio" name="cards[abc-123][field0]" value="Yes">Yes</label>
<label><input type="radio" name="cards[abc-123][field0]" value="No">No</label>
<textarea name="cards[abc-123][field1]"></textarea>
"""


def test_card_template_names_grouped_questions():
    schema = formscan.scan(CARD_HTML)
    by_id = {field.field_id: field for field in schema.fields}

    choice = by_id["cards[abc-123][field0]"]
    assert choice.label == "Are you authorized to work here?"
    assert choice.options == ["Yes", "No"]
    assert choice.section == "Screening Questions"

    free_text = by_id["cards[abc-123][field1]"]
    assert free_text.label == "What are your salary expectations?"
    assert free_text.type == "textarea"


def test_card_template_survives_unreadable_json():
    schema = formscan.scan(
        "<input type='hidden' name='cards[abc-123][baseTemplate]' value='{not json'>"
        "<textarea name='cards[abc-123][field0]'></textarea>"
    )
    assert schema.fields[0].label is None


def test_label_drops_required_glyph_and_widget_chrome():
    schema = formscan.scan(
        """
        <li><label>
          <div class="application-label">Current location <span class="required">✱</span></div>
          <div class="application-field">
            <input name="location" required>
            <div class="dropdown-status">No location found. Try entering a
              different location Loading</div>
          </div>
        </label></li>
        """
    )
    field = schema.fields[0]
    assert field.label == "Current location"
    assert field.required


def test_label_keeps_plain_wrapping_text():
    schema = formscan.scan("<label>Cover letter<textarea name='cover'></textarea></label>")
    assert schema.fields[0].label == "Cover letter"


def test_dropzone_file_input_is_not_bait():
    schema = formscan.scan(
        "<div class='dnd-file-input-container'>"
        "<input type='file' id='fileInput' style='display: none;'>"
        "<span>Drag and drop resume/CV or Upload</span></div>"
    )
    assert [field.type for field in schema.fields] == ["file"]


def test_hidden_text_input_is_still_bait():
    schema = formscan.scan(
        "<input type='text' name='website' style='display: none;'>"
        "<label for='e'>Email</label><input id='e' name='email'>"
    )
    assert [field.field_id for field in schema.fields] == ["email"]


def test_application_tab_outranks_a_header_apply_button():
    schema = formscan.scan(
        "<div class='header-buttons'><button class='primary'>Apply</button></div>"
        "<div role='tablist'>"
        "<a role='tab'>Overview</a><a role='tab'>Application</a></div>"
    )
    assert schema.apply_button is not None
    assert schema.apply_button.label == "Application"


def test_apply_button_still_wins_without_a_tab():
    schema = formscan.scan(
        "<h1>Role</h1><a href='/apply'>Apply for This Job</a>"
    )
    assert schema.apply_button.label == "Apply for This Job"
