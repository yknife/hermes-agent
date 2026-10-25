import httpx
import pytest
from plugins.video_knowledge.backend.media_adapters.errors import UnsafeUrlError
from plugins.video_knowledge.backend.media_adapters.models import MediaProbe
from plugins.video_knowledge.backend.media_adapters.security import MessagingUrlGuard


@pytest.mark.asyncio
@pytest.mark.parametrize("proxy", ["http://127.0.0.1:7890", "socks5://127.0.0.1:7890"])
async def test_proxy_resolves_allowlisted_youtube_instead_of_polluted_local_dns(
    monkeypatch, proxy
):
    monkeypatch.setattr(
        "plugins.video_knowledge.backend.media_adapters.security.getproxies",
        lambda: {"https": proxy},
    )

    def resolver(*_args):
        raise AssertionError("A remote-DNS proxy must not use local destination DNS")

    guard = MessagingUrlGuard(resolver=resolver)
    url = "https://www.youtube.com/watch?v=06rHoEpiuYY"
    assert await guard.validate_input(url) == url
    assert (
        await guard.validate_probe(
            MediaProbe(
                external_id="06rHoEpiuYY",
                title="fixture",
                webpage_url=url,
                platform="youtube",
            ),
            expected_platform="youtube",
        )
        == url
    )
    assert guard.proxy_for_url(url) == proxy.replace("socks5:", "socks5h:")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["direct", "bypass", "no_proxy"])
async def test_polluted_dns_still_rejected_without_proxy_route(monkeypatch, mode):
    monkeypatch.setattr(
        "plugins.video_knowledge.backend.media_adapters.security.getproxies",
        lambda: (
            {"https": "http://127.0.0.1:7890", "no": "youtube.com"}
            if mode == "bypass"
            else {}
        ),
    )
    guard = MessagingUrlGuard(
        resolver=lambda *_: ("69.171.235.22", "2001::1"),
        proxy="" if mode == "direct" else None,
    )
    url = "https://www.youtube.com/watch?v=06rHoEpiuYY"
    assert guard.proxy_for_url(url) == ""
    with pytest.raises(UnsafeUrlError, match="私有或本机"):
        await guard.validate_input(url)


@pytest.mark.asyncio
async def test_proxy_does_not_allow_arbitrary_hosts_ips_or_private_streams():
    guard = MessagingUrlGuard(
        proxy="http://127.0.0.1:7890", resolver=lambda *_: ("127.0.0.1",)
    )
    for url in [
        "http://127.0.0.1/private",
        "https://youtube.com.evil.test/watch?v=06rHoEpiuYY",
    ]:
        with pytest.raises(UnsafeUrlError):
            await guard.validate_input(url)
    with pytest.raises(UnsafeUrlError):
        await guard.validate_live_stream("https://cdn.example.test/stream.m3u8")


@pytest.mark.asyncio
async def test_short_link_http_client_uses_selected_proxy_and_checks_redirect(
    monkeypatch,
):
    real_client = httpx.AsyncClient
    proxies = []

    def client(**kwargs):
        proxies.append(kwargs.pop("proxy"))
        return real_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    guard = MessagingUrlGuard(
        proxy="http://127.0.0.1:7890",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                302, headers={"location": "http://127.0.0.1/private"}
            )
        ),
    )
    with pytest.raises(UnsafeUrlError):
        await guard.validate_input("https://b23.tv/AbCd123")
    assert proxies == ["http://127.0.0.1:7890"]


@pytest.mark.asyncio
async def test_youtube_alias_is_normalized_before_dns_and_probe_validation():
    seen = []

    def resolver(host, port):
        seen.append((host, port))
        return ("8.8.8.8",)

    guard = MessagingUrlGuard(resolver=resolver)
    canonical = "https://www.youtube.com/watch?v=06rHoEpiuYY"
    assert (
        await guard.validate_input("https://youtu.be/06rHoEpiuYY?si=token") == canonical
    )
    assert seen == [("www.youtube.com", 443)]
    assert (
        await guard.validate_probe(
            MediaProbe(
                external_id="06rHoEpiuYY",
                title="fixture",
                webpage_url=canonical,
                platform="Youtube",
            ),
            expected_platform="youtube",
        )
        == canonical
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "platform,url",
    [
        ("generic", "https://www.youtube.com/watch?v=06rHoEpiuYY"),
        ("YoutubeTab", "https://www.youtube.com/watch?v=06rHoEpiuYY"),
        ("Youtube", "https://www.bilibili.com/video/BV1GJ411x7h7"),
        ("Youtube", "https://127.0.0.1/watch?v=06rHoEpiuYY"),
    ],
)
async def test_youtube_probe_rejects_wrong_extractor_or_destination(platform, url):
    guard = MessagingUrlGuard(resolver=lambda _host, _port: ("8.8.8.8",))
    with pytest.raises(UnsafeUrlError):
        await guard.validate_probe(
            MediaProbe(
                external_id="06rHoEpiuYY",
                title="fixture",
                webpage_url=url,
                platform=platform,
            ),
            expected_platform="youtube",
        )


@pytest.mark.asyncio
async def test_youtube_canonical_destination_rejects_private_dns():
    guard = MessagingUrlGuard(resolver=lambda _host, _port: ("127.0.0.1",))
    with pytest.raises(UnsafeUrlError, match="私有或本机"):
        await guard.validate_input("https://youtu.be/06rHoEpiuYY")


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
@pytest.mark.parametrize(
    "direct",
    [
        "https://weibo.com/7827771738/N4xlMvjhI",
        "https://weibo.com/l/wblive/p/show/1022:2321325026370190442592",
    ],
)
async def test_weibo_short_link_stays_on_weibo_and_preserves_content_type(
    direct,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": direct})

    guard = MessagingUrlGuard(
        resolver=lambda _host, _port: ("8.8.8.8",),
        transport=httpx.MockTransport(handler),
    )
    assert await guard.validate_input("https://t.cn/A6Example") == direct

    probe_url = "https://weibo.com/7827771738/N4xlMvjhI"
    assert (
        await guard.validate_probe(
            MediaProbe(
                external_id="fixture",
                title="fixture",
                webpage_url=probe_url,
                platform="Weibo",
            ),
            expected_platform="weibo",
        )
        == probe_url
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
