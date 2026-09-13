import httpx
import pytest
from plugins.video_knowledge.backend.media_adapters.errors import UnsafeUrlError
from plugins.video_knowledge.backend.media_adapters.models import MediaProbe
from plugins.video_knowledge.backend.media_adapters.security import MessagingUrlGuard


@pytest.mark.asyncio
async def test_direct_bilibili_url_requires_only_global_dns_answers() -> None:
    seen: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> tuple[str, ...]:
        seen.append((host, port))
        return ("8.8.8.8", "2001:4860:4860::8888")

    guard = MessagingUrlGuard(resolver=resolver)
    value = await guard.validate_input(
        "https://www.bilibili.com/video/BV1GJ411x7h7/?p=1#fragment"
    )
    assert value == "https://www.bilibili.com/video/BV1GJ411x7h7/?p=1"
    assert seen == [("www.bilibili.com", 443)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "addresses",
    [
        ("127.0.0.1",),
        ("169.254.1.1",),
        ("10.0.0.2",),
        ("::1",),
        ("8.8.8.8", "192.168.1.2"),
    ],
)
async def test_dns_rebinding_or_non_global_answer_is_rejected(addresses) -> None:
    guard = MessagingUrlGuard(resolver=lambda _host, _port: addresses)
    with pytest.raises(UnsafeUrlError, match="私有或本机"):
        await guard.validate_input("https://bilibili.com/video/BV1GJ411x7h7")


@pytest.mark.asyncio
async def test_short_link_rejects_redirect_before_contacting_unapproved_host() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    guard = MessagingUrlGuard(
        resolver=lambda _host, _port: ("8.8.8.8",),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(UnsafeUrlError):
        await guard.validate_input("https://b23.tv/AbCd123")
    assert requested == ["https://b23.tv/AbCd123"]


@pytest.mark.asyncio
async def test_short_link_and_probe_must_resolve_to_bilibili_video() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={
                "location": "https://www.bilibili.com/video/BV1GJ411x7h7/?share=1"
            },
        )

    guard = MessagingUrlGuard(
        resolver=lambda _host, _port: ("8.8.8.8",),
        transport=httpx.MockTransport(handler),
    )
    resolved = await guard.validate_input("https://b23.tv/AbCd123")
    assert resolved.startswith("https://www.bilibili.com/video/BV1GJ411x7h7/")
    probe_url = await guard.validate_probe(
        MediaProbe(
            external_id="BV1GJ411x7h7",
            title="fixture",
            webpage_url=resolved,
            platform="BiliBili",
        ),
        expected_platform="bilibili",
    )
    assert probe_url == resolved

    with pytest.raises(UnsafeUrlError):
        await guard.validate_probe(
            MediaProbe(
                external_id="fixture",
                title="fixture",
                webpage_url="https://www.bilibili.com/video/BV1GJ411x7h7/",
                platform="generic",
            ),
            expected_platform="bilibili",
        )


@pytest.mark.asyncio
async def test_douyin_short_link_and_probe_remain_on_douyin() -> None:
    direct = "https://www.douyin.com/video/7672313492216548651"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": direct})

    guard = MessagingUrlGuard(
        resolver=lambda _host, _port: ("8.8.8.8",),
        transport=httpx.MockTransport(handler),
    )
    assert await guard.validate_input("https://v.douyin.com/iRNBho6u/") == direct
    assert (
        await guard.validate_probe(
            MediaProbe(
                external_id="7672313492216548651",
                title="fixture",
                webpage_url=direct,
                platform="Douyin",
            ),
            expected_platform="douyin",
        )
        == direct
    )


@pytest.mark.asyncio
async def test_douyin_modal_url_is_canonicalized_before_network_access() -> None:
    seen: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> tuple[str, ...]:
        seen.append((host, port))
        return ("8.8.8.8",)

    guard = MessagingUrlGuard(resolver=resolver)
    value = await guard.validate_input(
        "https://www.douyin.com/jingxuan?modal_id=7672313492216548651&from=web"
    )
    assert value == "https://www.douyin.com/video/7672313492216548651"
    assert seen == [("www.douyin.com", 443)]


@pytest.mark.asyncio
async def test_short_link_cannot_redirect_between_allowlisted_platforms() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://www.bilibili.com/video/BV1GJ411x7h7"},
        )

    guard = MessagingUrlGuard(
        resolver=lambda _host, _port: ("8.8.8.8",),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(UnsafeUrlError):
        await guard.validate_input("https://v.douyin.com/iRNBho6u/")


@pytest.mark.asyncio
async def test_xiaohongshu_short_link_and_probe_preserve_access_token() -> None:
    direct = (
        "https://www.xiaohongshu.com/discovery/item/674051740000000007027a15"
        "?xsec_token=required-token%3D"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        return httpx.Response(302, headers={"location": direct})

    guard = MessagingUrlGuard(
        resolver=lambda _host, _port: ("8.8.8.8",),
        transport=httpx.MockTransport(handler),
    )
    resolved = await guard.validate_input("https://xhslink.com/a/AbCd_123-xy/")
    assert resolved == direct
    assert (
        await guard.validate_probe(
            MediaProbe(
                external_id="674051740000000007027a15",
                title="fixture",
                webpage_url=direct,
                platform="XiaoHongShu",
            ),
            expected_platform="xiaohongshu",
        )
        == direct
    )


@pytest.mark.asyncio
async def test_xiaohongshu_short_link_falls_back_to_streaming_get() -> None:
    direct = "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(302, headers={"location": direct})

    guard = MessagingUrlGuard(
        resolver=lambda _host, _port: ("8.8.8.8",),
        transport=httpx.MockTransport(handler),
    )
    assert await guard.validate_input("https://xhslink.com/a/Share123") == direct
    assert methods == ["HEAD", "GET"]


@pytest.mark.asyncio
async def test_xiaohongshu_cn_share_link_resolves_without_agent_browser() -> None:
    direct = "https://www.xiaohongshu.com/explore/6411cf99000000001300b6d9"

    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        assert request.url.host == "xhslink.cn"
        if request.method == "HEAD":
            return httpx.Response(404)
        return httpx.Response(302, headers={"location": direct})

    guard = MessagingUrlGuard(
        resolver=lambda _host, _port: ("8.8.8.8",),
        transport=httpx.MockTransport(handler),
    )
    assert await guard.validate_input("https://xhslink.cn/o/7PdV8Tkf8gw") == direct
    assert methods == ["HEAD", "GET"]


@pytest.mark.asyncio
async def test_xiaohongshu_short_link_cannot_redirect_to_douyin() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://www.douyin.com/video/7672313492216548651"},
        )

    guard = MessagingUrlGuard(
        resolver=lambda _host, _port: ("8.8.8.8",),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(UnsafeUrlError):
        await guard.validate_input("https://xhslink.com/a/Share123")
