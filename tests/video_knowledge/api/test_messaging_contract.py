import pytest
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.schemas.messaging import (
    CancelCollectionArguments,
    CollectionStatusArguments,
    CollectVideoArguments,
    RetryCollectionArguments,
)
from pydantic import ValidationError


def test_messaging_gate_is_disabled_and_does_not_change_desktop_defaults(monkeypatch):
    monkeypatch.delenv("VKC_MESSAGING_INGEST_ENABLED", raising=False)
    settings = Settings(_env_file=None)
    assert not settings.messaging_ingest_allowed("feishu")
    assert not settings.messaging_ingest_allowed("desktop")
    assert settings.auto_analyze is True


def test_messaging_policy_loads_from_operator_environment(monkeypatch):
    monkeypatch.setenv("VKC_MESSAGING_INGEST_ENABLED", "true")
    monkeypatch.setenv("VKC_MESSAGING_ALLOWED_PLATFORMS", '["feishu"]')
    monkeypatch.setenv("VKC_MESSAGING_MAX_ACTIVE_PER_USER", "2")
    monkeypatch.setenv("VKC_MESSAGING_MAX_SUBMISSIONS_PER_USER_PER_DAY", "15")
    monkeypatch.setenv("VKC_MESSAGING_MAX_VIDEO_DURATION_SECONDS", "900")
    monkeypatch.setenv("VKC_MESSAGING_MAX_VIDEO_HEIGHT", "480")
    settings = Settings(_env_file=None)
    assert settings.messaging_ingest_allowed("feishu")
    assert not settings.messaging_ingest_allowed("desktop")
    assert not settings.messaging_ingest_allowed("telegram")
    assert settings.messaging_max_active_per_user == 2
    assert settings.messaging_max_submissions_per_user_per_day == 15
    assert settings.messaging_max_video_duration_seconds == 900
    assert settings.messaging_max_video_height == 480
    denied = Settings(_env_file=None, messaging_allowed_platforms=[])
    assert not denied.messaging_ingest_allowed("feishu")


@pytest.mark.parametrize(
    "values",
    [
        {"messaging_max_active_per_user": 0},
        {"messaging_max_submissions_per_user_per_day": -1},
        {"messaging_max_video_duration_seconds": 0},
        {"messaging_max_video_height": 2160},
        {"messaging_allowed_platforms": ["telegram"]},
    ],
)
def test_invalid_messaging_policy_fails_closed(values):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **values)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.bilibili.com/video/BV1GJ411x7h7/",
        "https://bilibili.com/video/av170001?p=1",
        "https://m.bilibili.com/video/BV1GJ411x7h7",
        "https://b23.tv/AbCd123",
    ],
)
def test_collect_contract_accepts_only_video_link_shape(url):
    assert CollectVideoArguments(url=url).url == url


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/secret.txt",
        "https://127.0.0.1/video/BV1GJ411x7h7",
        "https://bilibili.com.evil.test/video/BV1GJ411x7h7",
        "https://evilbilibili.com/video/BV1GJ411x7h7",
        "https://www.bilibili.com@evil.test/video/BV1GJ411x7h7",
        "https://user:secret@www.bilibili.com/video/BV1GJ411x7h7",
        "https://www.bilibili.com:8443/video/BV1GJ411x7h7",
        "https://live.bilibili.com/123",
        "https://www.bilibili.com/bangumi/play/ss123",
        "https://b23.tv/",
        "https://b23.tv.evil.test/AbCd123",
        "https://b23.tv/AbCd123\n",
        "https://[invalid/",
    ],
)
def test_collect_contract_rejects_unapproved_url_shapes(url):
    with pytest.raises(ValidationError):
        CollectVideoArguments(url=url)


@pytest.mark.parametrize(
    "field",
    [
        "platform",
        "chat_id",
        "thread_id",
        "message_id",
        "user_id",
        "profile",
        "cookies_file",
        "callback_url",
        "system_prompt",
        "force",
        "auto_analyze",
    ],
)
def test_model_arguments_cannot_supply_authority_or_execution_options(field):
    with pytest.raises(ValidationError):
        CollectVideoArguments.model_validate({
            "url": "https://b23.tv/AbCd123",
            field: "forged",
        })


@pytest.mark.parametrize(
    "contract",
    [
        CollectionStatusArguments,
        CancelCollectionArguments,
        RetryCollectionArguments,
    ],
)
def test_conversation_actions_accept_optional_workflow_identity_only(contract):
    assert contract().workflow_id is None
    assert contract(workflow_id="workflow_fixture").workflow_id == "workflow_fixture"
    for payload in (
        {"workflow_id": "../state.db"},
        {"workflow_id": "workflow_fixture", "user_id": "forged"},
    ):
        with pytest.raises(ValidationError):
            contract.model_validate(payload)
    assert set(contract.model_json_schema()["properties"]) == {"workflow_id"}
