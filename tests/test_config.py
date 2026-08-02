import pytest

from jobpilot.config import Config, ConfigError, DataPaths, load_config


def test_missing_file_gives_defaults(tmp_path):
    config = load_config(tmp_path / "absent.toml")
    assert config == Config()
    assert config.gates.resume_glance is True
    assert config.gates.auto_submit is False
    assert config.browser.tab_cap == 8
    assert config.mail.match_threshold == 0.75


def test_partial_file_overrides(tmp_path):
    file = tmp_path / "config.toml"
    file.write_text(
        "[gates]\nauto_submit = true\n\n[browser]\ntab_cap = 4\n\n"
        "[notify]\nchannels = [\"ntfy\"]\nntfy_topic = \"jp-test\"\n"
    )
    config = load_config(file)
    assert config.gates.auto_submit is True
    assert config.gates.resume_glance is True
    assert config.browser.tab_cap == 4
    assert config.notify.channels == ("ntfy",)
    assert config.notify.ntfy_topic == "jp-test"
    assert config.ai.provider == "anthropic"


def test_unknown_section_rejected(tmp_path):
    file = tmp_path / "config.toml"
    file.write_text("[gaets]\nresume_glance = true\n")
    with pytest.raises(ConfigError):
        load_config(file)


def test_unknown_key_rejected(tmp_path):
    file = tmp_path / "config.toml"
    file.write_text("[gates]\nresume_glanse = true\n")
    with pytest.raises(ConfigError):
        load_config(file)


def test_example_config_matches_defaults(tmp_path):
    from pathlib import Path

    example = Path(__file__).resolve().parent.parent / "config.example.toml"
    assert load_config(example) == Config()


def test_paths_layout(tmp_path):
    paths = DataPaths(data_root=tmp_path / "data")
    assert paths.db_path == tmp_path / "data" / "jobpilot.db"
    assert paths.job_dir(7) == tmp_path / "data" / "jobs" / "7"
    absolute = paths.absolute("jobs/7/resume.pdf")
    assert paths.relative_to_root(absolute) == "jobs/7/resume.pdf"
