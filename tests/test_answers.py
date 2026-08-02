from jobpilot.core import answers


def test_normalize():
    assert answers.normalize("What is your notice period?") == "what is your notice period"
    assert answers.normalize("  Desired   SALARY!! ") == "desired salary"


def test_set_get_upsert(conn):
    answers.set_answer(conn, "Notice period?", "two weeks", "owner")
    entry = answers.get_answer(conn, "notice period")
    assert entry.answer == "two weeks"

    answers.set_answer(conn, "Notice period", "one month", "owner")
    assert answers.get_answer(conn, "NOTICE PERIOD?").answer == "one month"
    assert len(answers.bank(conn).entries) == 1


def test_scoping(conn):
    answers.set_answer(conn, "Salary expectation", "120k", "owner", sensitive=True)
    answers.set_answer(
        conn, "Why Acme?", "The payments platform", "owner", scope="company:acme"
    )

    everywhere = answers.bank(conn)
    assert [entry.question for entry in everywhere.entries] == ["Salary expectation"]

    acme = answers.bank(conn, company="Acme")
    assert {entry.question for entry in acme.entries} == {
        "Salary expectation", "Why Acme?"
    }
    assert acme.entries[0].sensitive or acme.entries[1].sensitive


def test_sensitive_value_lookup(conn):
    answers.set_answer(conn, "Salary expectation", "120000", "owner", sensitive=True)
    assert answers.is_sensitive_answer(conn, "120000")
    assert not answers.is_sensitive_answer(conn, "two weeks")
