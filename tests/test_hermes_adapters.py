import subprocess

from hermes_fleet.hermes_adapter import HermesCliAdapterV1


def test_adapter_probe_reports_version_and_capabilities():
    probe = HermesCliAdapterV1("/bin/echo", 5).probe()
    assert probe["adapter_version"] == "hermes-cli-v1"
    assert probe["available"] is True
    assert probe["supports_profiles"] is True


def test_adapter_never_reports_embedded_provider_error_as_success(monkeypatch):
    result = subprocess.CompletedProcess([], 0, "API call failed after 3 retries: HTTP 429: usage limit reached\n", "")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: result)
    execution = HermesCliAdapterV1("hermes", 5).execute("default", "test")
    assert execution.status == "failed"
    assert "HTTP 429" in execution.error
    assert execution.output == ""
