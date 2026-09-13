import pytest
from plugins.video_knowledge.backend.app.core.config import Settings
from plugins.video_knowledge.backend.app.schemas.messaging import (
    CancelCollectionArguments,
    CollectionStatusArguments,
    CollectVideoArguments,
    RetryCollectionArguments,
)
from plugins.video_knowledge.messaging_tools import fast_collect_url
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
    monkeypatch.setenv("VKC_MESSAGING_MAX_ACTIVE_PER_CHAT", "4")
    monkeypatch.setenv("VKC_MESSAGING_MAX_SUBMISSIONS_PER_USER_PER_DAY", "15")
    monkeypatch.setenv("VKC_MESSAGING_MAX_SUBMISSIONS_PER_CHAT_PER_DAY", "40")
    monkeypatch.setenv("VKC_MESSAGING_MAX_VIDEO_DURATION_SECONDS", "900")
    monkeypatch.setenv("VKC_MESSAGING_MAX_VIDEO_HEIGHT", "480")
    settings = Settings(_env_file=None)
    assert settings.messaging_ingest_allowed("feishu")
    assert not settings.messaging_ingest_allowed("desktop")
    assert not settings.messaging_ingest_allowed("telegram")
    assert settings.messaging_max_active_per_user == 2
    assert settings.messaging_max_active_per_chat == 4
    assert settings.messaging_max_submissions_per_user_per_day == 15
    assert settings.messaging_max_submissions_per_chat_per_day == 40
    assert settings.messaging_max_video_duration_seconds == 900
    assert settings.messaging_max_video_height == 480
    denied = Settings(_env_file=None, messaging_allowed_platforms=[])
    assert not denied.messaging_ingest_allowed("feishu")


@pytest.mark.parametrize(
    "values",
    [
        {"messaging_max_active_per_user": 0},
        {"messaging_max_active_per_chat": 0},
        {"messaging_max_submissions_per_user_per_day": -1},
        {"messaging_max_submissions_per_chat_per_day": -1},
        {"messaging_min_free_bytes": 1},
        {"messaging_personal_data_retention_days": 0},
        {"messaging_max_video_duration_seconds": -1},
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
        "http://www.bilibili.com/video/BV1GJ411x7h7",
        "https://www.douyin.com/video/7672313492216548651",
        "http://www.douyin.com/video/7672313492216548651?from=share",
        "https://v.douyin.com/iRNBho6u/",
        "https://www.iesdouyin.com/share/video/7672313492216548651/",
        "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9",
        (
            "https://www.xiaohongshu.com/discovery/item/674051740000000007027a15"
            "?xsec_token=required-token%3D"
        ),
        "https://xhslink.com/a/AbCd_123-xy/",
        "http://www.xhslink.com/m/Share123",
        "https://xhslink.cn/o/7PdV8Tkf8gw",
    ],
)
def test_collect_contract_accepts_only_video_link_shape(url):
    expected = "https://" + url.split("://", 1)[1]
    assert CollectVideoArguments(url=url).url == expected


def test_collect_contract_canonicalizes_douyin_modal_video_url():
    assert (
        CollectVideoArguments(
            url="https://www.douyin.com/jingxuan?modal_id=7672313492216548651&from=web"
        ).url
        == "https://www.douyin.com/video/7672313492216548651"
    )


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
        "https://live.bilibili.com/not-a-room",
        "https://live.bilibili.com/0",
        "https://live.bilibili.com.evil.test/123",
        "https://space.bilibili.com/video/BV1GJ411x7h7",
        "https://www.bilibili.com/bangumi/play/ss123",
        "https://b23.tv/",
        "https://b23.tv.evil.test/AbCd123",
        "https://b23.tv/AbCd123\n",
        "https://live.douyin.com/123456",
        "https://www.douyin.com/user/MS4wLjABAAAA",
        "https://www.douyin.com/video/not-numeric",
        "https://v.douyin.com/",
        "https://v.douyin.com.evil.test/iRNBho6u/",
        "https://www.iesdouyin.com/share/user/123",
        "https://xiaohongshu.com/explore/6411cf99000000001300b6d9",
        "https://www.xiaohongshu.com/user/profile/6411cf99000000001300b6d9",
        "https://www.xiaohongshu.com/explore/not-a-note-id",
        "https://xhslink.com/",
        "https://xhslink.com/a/Share123/extra",
        "https://evilxhslink.com/a/Share123",
        "https://xhslink.cn/",
        "https://xhslink.cn/o/Share123/extra",
        "https://xhslink.cn.evil.test/o/Share123",
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


def test_fast_collect_url_requires_one_explicit_intent_and_allowed_url():
    url = "https://www.bilibili.com/video/BV1Sxbp6REnB"
    assert fast_collect_url(f"采集并分析这个视频：{url}。") == url
    assert fast_collect_url(url) is None
    assert fast_collect_url(f"分析 {url} 和 https://b23.tv/AbCd123") is None
    assert fast_collect_url("分析 https://example.com/video/BV1Sxbp6REnB") is None
    douyin = "https://www.douyin.com/video/7672313492216548651"
    assert fast_collect_url(f"采集并分析这个抖音视频：{douyin}") == douyin
    xiaohongshu = "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"
    assert fast_collect_url(f"采集并分析这个小红书视频：{xiaohongshu}") == xiaohongshu
    short_xiaohongshu = "https://xhslink.cn/o/7PdV8Tkf8gw"
    assert (
        fast_collect_url(f"采集并分析小红书视频：{short_xiaohongshu}")
        == short_xiaohongshu
    )
