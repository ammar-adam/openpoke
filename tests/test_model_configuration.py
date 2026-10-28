"""Model configuration precedence; no API requests or provider assumptions."""
import pytest

from server.config import Settings


ROLES = {
    "interaction_agent_model": "OPENPOKE_INTERACTION_MODEL",
    "execution_agent_model": "OPENPOKE_EXECUTION_MODEL",
    "execution_agent_search_model": "OPENPOKE_EXECUTION_SEARCH_MODEL",
    "summarizer_model": "OPENPOKE_SUMMARIZER_MODEL",
    "email_classifier_model": "OPENPOKE_EMAIL_CLASSIFIER_MODEL",
}


@pytest.fixture(autouse=True)
def clear_model_environment(monkeypatch):
    for name in ("OPENPOKE_MODEL", *ROLES.values()):
        monkeypatch.delenv(name, raising=False)


def test_all_roles_use_explicit_documented_default():
    settings = Settings()
    assert {getattr(settings, field) for field in ROLES} == {"anthropic/claude-sonnet-4.6"}


def test_common_model_is_resolved_when_settings_are_constructed(monkeypatch):
    monkeypatch.setenv("OPENPOKE_MODEL", "example/common-model")
    assert {getattr(Settings(), field) for field in ROLES} == {"example/common-model"}
    monkeypatch.setenv("OPENPOKE_MODEL", "example/replacement-model")
    assert Settings().interaction_agent_model == "example/replacement-model"


@pytest.mark.parametrize("field,variable", ROLES.items())
def test_role_override_wins_without_changing_other_roles(monkeypatch, field, variable):
    monkeypatch.setenv("OPENPOKE_MODEL", "example/common-model")
    monkeypatch.setenv(variable, "example/role-model")
    settings = Settings()
    assert getattr(settings, field) == "example/role-model"
    assert all(getattr(settings, other) == "example/common-model" for other in ROLES if other != field)


def test_explicit_constructor_wins_and_unknown_identifier_is_not_substituted(monkeypatch):
    monkeypatch.setenv("OPENPOKE_MODEL", "example/common-model")
    monkeypatch.setenv("OPENPOKE_INTERACTION_MODEL", "operator/unlisted-model")
    assert Settings().interaction_agent_model == "operator/unlisted-model"
    assert Settings(interaction_agent_model="injected/eval-model").interaction_agent_model == "injected/eval-model"


@pytest.mark.parametrize("variable", ["OPENPOKE_MODEL", "OPENPOKE_INTERACTION_MODEL"])
def test_empty_environment_choice_does_not_silently_select_another_model(monkeypatch, variable):
    monkeypatch.setenv(variable, "   ")
    with pytest.raises(ValueError, match="nonempty model identifier"):
        Settings()
