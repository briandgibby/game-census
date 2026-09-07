"""Configuration boundaries that protect first-run setup and operator inputs."""
from __future__ import annotations

import json
import importlib.util
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from game_census.config import (
    ConfigurationError,
    config_path,
    describe_settings,
    generate_profile,
    load_settings,
)


SECRET = "test-password-that-must-not-appear-in-errors"
DATABASE_URL = f"postgresql://game_census:{SECRET}@db:5432/game_census"


def write_config(tmp_path: Path, **sections: object) -> Path:
    payload = {"storage": {"database_url": DATABASE_URL}, **sections}
    path = tmp_path / "operator.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_first_run_generates_complete_loadable_config_in_empty_directory(tmp_path):
    path = tmp_path / "new-instance" / "config" / "local.json"
    result = generate_profile(path)
    settings = load_settings(path)

    assert result == {"status": "created", "config_path": str(path.resolve()), "app_ids": [570]}
    assert settings.tracking.app_ids == [570]
    assert settings.web.publish_host == "127.0.0.1"
    assert settings.storage.database_url.get_secret_value().startswith("postgresql://game_census:")
    assert "**********" not in path.read_text(encoding="utf-8")
    assert "database_url" not in result


def test_existing_profile_is_preserved_byte_for_byte(tmp_path):
    path = tmp_path / "instance.json"
    generate_profile(path, app_ids=[730], port=8123)
    original = path.read_bytes()
    original_time = path.stat().st_mtime_ns

    result = generate_profile(path, app_ids=[570], port=8124)

    assert result["status"] == "existing"
    assert result["app_ids"] == [730]
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == original_time
    assert load_settings(path).web.port == 8123


def test_invalid_existing_profile_is_reported_and_preserved(tmp_path):
    path = tmp_path / "invalid.json"
    original = b'{"tracking":{"app_ids":[0]}}'
    path.write_bytes(original)

    with pytest.raises(ConfigurationError, match="storage"):
        generate_profile(path)

    assert path.read_bytes() == original


@pytest.mark.parametrize("app_ids", [[], [0], [-1], [True], [1.5], ["570"], [570, 570], list(range(1, 27)), [4294967296]])
def test_invalid_tracking_ids_are_rejected_with_named_setting(tmp_path, app_ids):
    path = write_config(tmp_path, tracking={"app_ids": app_ids})
    with pytest.raises(ConfigurationError, match=r"tracking\.app_ids") as error:
        load_settings(path)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("app_ids", [[], [0], [-1], [True], [570, 570], list(range(1, 27))])
def test_generator_rejects_invalid_explicit_ids_before_creating_state(tmp_path, app_ids):
    path = tmp_path / "not-created" / "instance.json"
    with pytest.raises((ConfigurationError, ValidationError), match="app_ids"):
        generate_profile(path, app_ids=app_ids)
    assert not path.exists()


@pytest.mark.parametrize(
    ("section", "setting", "value"),
    [
        ("tracking", "interval_seconds", 299),
        ("tracking", "interval_seconds", 604801),
        ("http", "timeout_seconds", 0),
        ("http", "max_attempts", 6),
        ("http", "min_interval_seconds", 0),
        ("http", "max_response_bytes", 1023),
        ("quota", "webapi_rolling_24h", 100001),
        ("quota", "store_rolling_24h", 0),
        ("metrics", "gap_cap_multiplier", 0.5),
        ("metrics", "freshness_interval_multiplier", 4.1),
        ("web", "port", 65536),
        ("web", "port", 80),
        ("web", "publish_host", "0.0.0.0"),
        ("web", "refresh_seconds", 14),
        ("web", "max_points", 10001),
        ("web", "max_history_days", 366),
        ("sources", "store_metadata_enabled", "false"),
    ],
)
def test_out_of_range_or_wrongly_typed_settings_fail_safely(tmp_path, section, setting, value):
    path = write_config(tmp_path, **{section: {setting: value}})
    with pytest.raises(ConfigurationError) as error:
        load_settings(path)
    assert f"{section}.{setting}" in str(error.value)
    assert "config describe --schema" in str(error.value)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("version", [True, 2, "1"])
def test_schema_version_rejects_unsupported_and_wrongly_typed_values(tmp_path, version):
    path = write_config(tmp_path, schema_version=version)
    with pytest.raises(ConfigurationError, match="schema_version"):
        load_settings(path)


def test_unknown_nested_setting_is_named_in_error(tmp_path):
    path = write_config(tmp_path, tracking={"interval_second": 300})
    with pytest.raises(ConfigurationError, match=r"tracking\.interval_second"):
        load_settings(path)


def test_missing_config_explains_first_run_recovery(tmp_path):
    with pytest.raises(ConfigurationError) as error:
        load_settings(tmp_path / "missing.json")
    assert "config init" in str(error.value)
    assert "quickstart --once" in str(error.value)


def test_describe_redacts_connection_credentials(tmp_path):
    result = describe_settings(load_settings(write_config(tmp_path)))
    serialized = json.dumps(result)
    assert SECRET not in serialized
    assert DATABASE_URL not in serialized
    assert result["storage"]["database_url"] == "**********"


def test_invalid_database_url_does_not_leak_credential(tmp_path):
    path = write_config(tmp_path, storage={"database_url": f"https://user:{SECRET}@db/database"})
    with pytest.raises(ConfigurationError, match=r"storage\.database_url") as error:
        load_settings(path)
    assert SECRET not in str(error.value)


def test_duplicate_json_setting_is_rejected_and_named(tmp_path):
    path = tmp_path / "duplicate.json"
    path.write_text(
        '{"storage":{"database_url":' + json.dumps(DATABASE_URL) + '},'
        '"tracking":{"app_ids":[570],"app_ids":[730]}}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError) as error:
        load_settings(path)
    assert "duplicate" in str(error.value).lower()
    assert "app_ids" in str(error.value)
    assert SECRET not in str(error.value)


@pytest.mark.parametrize("content", [b'{"unterminated":', b'\xff\xfe'])
def test_invalid_json_or_encoding_is_reported_without_raw_input(tmp_path, content):
    path = tmp_path / "malformed.json"
    path.write_bytes(content)
    with pytest.raises(ConfigurationError, match="valid UTF-8 JSON"):
        load_settings(path)


def test_configuration_size_is_bounded(tmp_path):
    path = tmp_path / "oversized.json"
    path.write_text(" " * 65537, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="65536"):
        load_settings(path)


def test_config_path_uses_operator_environment_with_explicit_path_precedence(tmp_path, monkeypatch):
    from_environment = tmp_path / "environment.json"
    explicit = tmp_path / "explicit.json"
    monkeypatch.setenv("GAME_CENSUS_CONFIG", str(from_environment))
    assert config_path() == from_environment.resolve()
    assert config_path(explicit) == explicit.resolve()


def test_module_help_is_available_without_configuration(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "game_census", "--help"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0
    assert "collect" in result.stdout
    assert "initialize" in result.stdout
    assert result.stderr == ""


def test_cli_config_init_bootstraps_empty_directory_without_database(tmp_path, capsys, monkeypatch):
    from game_census import cli

    def database_must_not_be_needed(_dsn):
        pytest.fail("Configuration generation must not require an existing database")

    monkeypatch.setattr(cli, "Database", database_must_not_be_needed)
    path = tmp_path / "empty-instance" / "config.json"
    assert cli.main(["--config", str(path), "config", "init", "--app-id", "730", "--port", "8123"]) == 0
    output = capsys.readouterr()
    result = json.loads(output.out)
    settings = load_settings(path)
    assert result["status"] == "created"
    assert settings.tracking.app_ids == [730]
    assert settings.web.port == 8123
    assert settings.storage.database_url.get_secret_value() not in output.out
    assert output.err == ""


def test_cli_missing_config_returns_nonzero_actionable_error_without_traceback(tmp_path, capsys):
    from game_census import cli

    assert cli.main(["--config", str(tmp_path / "missing.json"), "initialize"]) == 1
    output = capsys.readouterr()
    error = json.loads(output.err)
    assert error["status"] == "failed"
    assert "config init" in error["error"]
    assert "Traceback" not in output.err
    assert SECRET not in output.err
    assert output.out == ""


def test_cli_initialize_reports_database_failure_safely(tmp_path, monkeypatch, capsys):
    from game_census import cli

    class UnavailableDatabase:
        def __init__(self, dsn):
            assert dsn == DATABASE_URL

        def initialize(self, app_ids, interval_seconds):
            # Schema bootstrap precedes stored-cohort resolution and enrollment.
            assert app_ids == []
            assert interval_seconds == 300
            raise cli.DatabaseError("Database unavailable. Start PostgreSQL and retry initialize.")

    monkeypatch.setattr(cli, "Database", UnavailableDatabase)
    assert cli.main(["--config", str(write_config(tmp_path)), "initialize"]) == 1
    output = capsys.readouterr()
    assert json.loads(output.err) == {
        "status": "failed",
        "error": "Database unavailable. Start PostgreSQL and retry initialize.",
    }
    assert SECRET not in output.err + output.out
    assert "Traceback" not in output.err


def test_cli_collect_requires_explicit_once_before_configuration_or_database(capsys):
    from game_census import cli

    with pytest.raises(SystemExit) as error:
        cli.main(["collect"])
    assert error.value.code == 2
    output = capsys.readouterr()
    assert "--once" in output.err
    assert output.out == ""


@pytest.mark.parametrize(("status", "exit_code"), [("succeeded", 0), ("partial", 1), ("failed", 1)])
def test_cli_collect_announces_bounds_and_exposes_partial_failure(tmp_path, monkeypatch, capsys, status, exit_code):
    from game_census import cli, collector

    database = object()
    monkeypatch.setattr(cli, "Database", lambda _dsn: database)

    def fake_collect(settings, db, app_ids):
        assert db is database
        assert app_ids == [570]
        assert settings.tracking.app_ids == [570]
        return {"run_id": "fixture-run", "status": status, "apps": [], "request_count": 2}

    monkeypatch.setattr(collector, "collect_once", fake_collect)
    assert cli.main(["--config", str(write_config(tmp_path)), "collect", "--once"]) == exit_code
    output = capsys.readouterr()
    decoder = json.JSONDecoder()
    announcement, offset = decoder.raw_decode(output.out)
    result = json.loads(output.out[offset:])
    assert announcement == {
        "operation": "collect_once",
        "app_ids": [570],
        "maximum_requests": 2,
        "collection_mode": "manual",
    }
    assert result["status"] == status
    assert output.err == ""
    assert SECRET not in output.out


def test_cli_collect_rejects_unconfigured_app_without_fetching(tmp_path, monkeypatch, capsys):
    from game_census import cli, collector

    monkeypatch.setattr(cli, "Database", lambda _dsn: object())

    def must_not_collect(*_args, **_kwargs):
        pytest.fail("Unconfigured applications must be rejected before collection")

    monkeypatch.setattr(collector, "collect_once", must_not_collect)
    assert cli.main(["--config", str(write_config(tmp_path)), "collect", "--once", "--app-id", "730"]) == 1
    output = capsys.readouterr()
    assert "tracking.app_ids" in json.loads(output.err)["error"]
    assert output.out == ""


def test_cli_history_unknown_app_is_an_actionable_failure(tmp_path, monkeypatch, capsys):
    from game_census import cli

    class HistoryDatabase:
        def __init__(self, dsn):
            assert dsn == DATABASE_URL

        def history(self, app_id, _settings, hours):
            assert app_id == 730
            assert hours == 24
            return None

    monkeypatch.setattr(cli, "Database", HistoryDatabase)
    assert cli.main(["--config", str(write_config(tmp_path)), "history", "--app-id", "730"]) == 1
    output = capsys.readouterr()
    error = json.loads(output.err)
    assert error["status"] == "failed"
    assert "tracking.app_ids" in error["error"]
    assert "initialize" in error["error"]
    assert output.out == ""
    assert SECRET not in output.err


def test_cli_describe_schema_does_not_require_credentials_or_database(tmp_path, monkeypatch, capsys):
    from game_census import cli

    monkeypatch.setattr(cli, "Database", lambda _dsn: pytest.fail("Schema inspection must work before configuration"))
    assert cli.main(["--config", str(tmp_path / "missing.json"), "config", "describe", "--schema"]) == 0
    output = capsys.readouterr()
    assert "storage" in json.loads(output.out)["properties"]
    assert output.err == ""


def test_docker_wrapper_rejects_container_loopback_bind_before_starting(tmp_path, monkeypatch):
    """Native loopback is valid, but Docker's published port cannot reach it."""
    source = Path(__file__).resolve().parents[1] / "tools" / "dev.py"
    spec = importlib.util.spec_from_file_location("game_census_dev_for_test", source)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    monkeypatch.setattr(driver, "ROOT", tmp_path)
    project = driver.Project("test-loopback")
    project.config.mkdir(parents=True)
    (project.config / "local.json").write_text(
        json.dumps({"storage": {"database_url": DATABASE_URL}, "web": {"bind": "127.0.0.1"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(driver, "run", lambda *_args, **_kwargs: json.dumps(describe_settings(load_settings(project.config / "local.json"))))
    with pytest.raises(driver.DriverError, match=r"web\.bind"):
        project.environment()


@pytest.mark.parametrize("reachable", [False, True])
def test_http_doctor_requires_actual_http_readiness_even_when_database_is_healthy(tmp_path, monkeypatch, capsys, reachable):
    import httpx
    from game_census import cli

    class HealthyDatabase:
        def __init__(self, _dsn):
            pass

        def status(self):
            return {"database": "ready"}

    requests = []

    def local_health_only(url, **kwargs):
        requests.append(url)
        assert url == "http://127.0.0.1:8000/health/ready"
        assert 0 < kwargs["timeout"] <= 60
        if not reachable:
            raise httpx.ConnectError("Simulated refused localhost connection " + SECRET)
        return httpx.Response(200, json={"status": "ready"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(cli, "Database", HealthyDatabase)
    monkeypatch.setattr(httpx, "get", local_health_only)
    result = cli.main(["--config", str(write_config(tmp_path)), "doctor", "--http"])
    assert result == (0 if reachable else 1)
    assert requests == ["http://127.0.0.1:8000/health/ready"]
    output = capsys.readouterr()
    assert SECRET not in output.out + output.err
    if reachable:
        assert output.err == ""
        assert json.loads(output.out)["configuration"] == "ok"
    else:
        assert "http" in json.loads(output.err)["error"].lower()
        assert output.out == ""


def test_wrapper_preserves_safe_captured_configuration_diagnostic(monkeypatch):
    source = Path(__file__).resolve().parents[1] / "tools" / "dev.py"
    spec = importlib.util.spec_from_file_location("game_census_dev_for_test", source)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    diagnostic = "Invalid configuration setting(s): web.port. Run config describe --schema for supported bounds."
    monkeypatch.setattr(
        driver.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1, stdout="", stderr=json.dumps({"status": "failed", "error": diagnostic})
        ),
    )
    with pytest.raises(driver.DriverError, match=r"web\.port") as error:
        driver.run(["docker", "run", "test-image", "config", "describe"], capture=True, safe_error=True)
    assert str(error.value) == diagnostic


@pytest.mark.parametrize("safe_error", [False, True])
def test_wrapper_never_echoes_arbitrary_captured_docker_output(monkeypatch, safe_error):
    source = Path(__file__).resolve().parents[1] / "tools" / "dev.py"
    spec = importlib.util.spec_from_file_location("game_census_dev_for_test", source)
    driver = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(driver)
    monkeypatch.setattr(
        driver.subprocess,
        "run",
        lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 1, stdout=DATABASE_URL, stderr="Docker diagnostic may contain " + SECRET
        ),
    )
    with pytest.raises(driver.DriverError) as error:
        driver.run(["docker", "run", "test-image", "config", "describe"], capture=True, safe_error=safe_error)
    assert SECRET not in str(error.value)
    assert DATABASE_URL not in str(error.value)
    assert "exit 1" in str(error.value)
