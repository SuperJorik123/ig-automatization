"""The BulkFollows ordering helpers, exercised without a network."""

from modules.telegram import reactions


def test_channel_link_drops_the_message_id():
    assert reactions.channel_link("@c", "https://t.me/c/412") == "https://t.me/c"
    assert reactions.channel_link("@c", None) == "https://t.me/c"
    assert reactions.channel_link("-100123", None) == ""
