from datetime import UTC, datetime

import httpx
import pytest

from ai_daily.models import SourceConfig, SourceTier
from ai_daily.sources import Collector


@pytest.fixture(autouse=True)
def public_dns_for_mocked_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    async def resolve_public_dns(_value: str) -> None:
        return None

    monkeypatch.setattr("ai_daily.sources._validate_public_dns", resolve_public_dns)


def _message(post_id: int, body: str, stamp: str) -> str:
    return (
        '<div class="tgme_widget_message_wrap">'
        f'<div class="tgme_widget_message js-widget_message" data-post="zaihuapd/{post_id}">'
        '<div class="tgme_widget_message_bubble">'
        f"{body}"
        '<div class="tgme_widget_message_footer">'
        f'<a class="tgme_widget_message_date" href="https://t.me/zaihuapd/{post_id}">'
        f'<time datetime="{stamp}">00:00</time></a></div>'
        "</div></div></div>"
    )


def _page(*messages: str) -> bytes:
    history = "".join(messages)
    return (
        f'<html><body><section class="tgme_channel_history">{history}</section></body></html>'
    ).encode()


REPLY_QUOTE = (
    '<a class="tgme_widget_message_reply" href="https://t.me/zaihuapd/43700">'
    '<div class="tgme_widget_message_text js-message_reply_text">'
    "GLM 5.3 Flash 在 OpenCode Go 额度翻倍</div></a>"
)
PRICE_POST = (
    '<div class="tgme_widget_message_text js-message_text" dir="auto">'
    '<tg-emoji><i class="emoji"><b>🤖</b></i></tg-emoji>'
    "<b>DeepSeek 下调 Flash 模型价格</b><br/><br/>"
    "我们将于北京时间 2026 年 9 月 10 日 12:00 起，调整 flash 系列定价。<br/><br/>"
    '<a href="https://www.ithome.com/0/999/967.htm" target="_blank">IT之家</a><br/><br/>'
    '<i class="emoji"><b>🌸</b></i> <a href="http://t.me/ZaiHuaPd">在花频道</a> · '
    '<a href="https://t.me/zaihuachat">茶馆水群</a> · '
    '<a href="http://t.me/ZaiHuabot">投稿通道</a></div>'
)
PLAIN_POST = (
    '<div class="tgme_widget_message_text js-message_text" dir="auto">'
    "苹果昨晚的发布会，可以往上翻看几条历史消息。</div>"
)
PHOTO_ONLY = '<div class="tgme_widget_message_photo_wrap"></div>'

TELEGRAM_PREVIEW = _page(
    _message(43730, REPLY_QUOTE + PRICE_POST, "2026-09-10T02:16:03+00:00"),
    _message(43729, PLAIN_POST, "2026-09-10T01:20:27+00:00"),
    _message(43728, PHOTO_ONLY, "2026-09-10T00:20:21+00:00"),
)


def _telegram_source(limit: int = 40) -> SourceConfig:
    return SourceConfig(
        name="telegram-zaihua",
        display_name="在花频道",
        kind="telegram_channel",
        url="https://t.me/s/zaihuapd",
        tier=SourceTier.C,
        region="china",
        ai_focused=False,
        limit=limit,
    )


async def test_reads_post_body_not_reply_quote_and_keeps_url_on_t_me() -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, content=TELEGRAM_PREVIEW))
    items, health = await Collector(transport).collect([_telegram_source()])

    assert health[0].status == "ok"
    assert [item.title for item in items] == [
        "DeepSeek 下调 Flash 模型价格",
        "苹果昨晚的发布会，可以往上翻看几条历史消息。",
    ]
    priced = items[0]
    assert str(priced.url) == "https://t.me/zaihuapd/43730"
    assert priced.source_item_id == "https://t.me/zaihuapd/43730"
    assert priced.published_at == datetime(2026, 9, 10, 2, 16, 3, tzinfo=UTC)
    assert "GLM 5.3 Flash" not in priced.summary
    assert "在花频道" not in priced.summary
    assert priced.summary == (
        "我们将于北京时间 2026 年 9 月 10 日 12:00 起，调整 flash 系列定价。 IT之家 "
        "来源：IT之家 https://www.ithome.com/0/999/967.htm"
    )
    assert priced.metrics == {
        "cited_url": "https://www.ithome.com/0/999/967.htm",
        "cited_source": "IT之家",
    }
    assert items[1].metrics == {}
    assert items[1].summary == ""


async def test_pages_back_until_limit_is_met() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        before = request.url.params.get("before")
        newest = int(before) - 1 if before else 100
        page = _page(
            *(
                _message(
                    post_id,
                    f'<div class="tgme_widget_message_text js-message_text">Post {post_id}</div>',
                    "2026-09-10T00:00:00+00:00",
                )
                for post_id in range(newest - 19, newest + 1)
            )
        )
        return httpx.Response(200, content=page)

    items, health = await Collector(httpx.MockTransport(handler)).collect(
        [_telegram_source(limit=30)]
    )

    assert requested == ["https://t.me/s/zaihuapd", "https://t.me/s/zaihuapd?before=81"]
    assert health[0].item_count == 30
    assert [item.title for item in items[:2]] == ["Post 100", "Post 99"]
    assert items[-1].title == "Post 71"


async def test_preview_with_no_posts_fails_loudly() -> None:
    transport = httpx.MockTransport(
        lambda request: httpx.Response(200, content=b"<html><body>nothing here</body></html>")
    )
    items, health = await Collector(transport).collect([_telegram_source()])

    assert items == []
    assert health[0].status == "failed"
    assert health[0].error == "SourceCollectionError"
