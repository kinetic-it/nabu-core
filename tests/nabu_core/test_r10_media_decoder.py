from itertools import product
from urllib.parse import unquote

import pytest

from nabu_core.player import _decode_media_escapes


@pytest.mark.parametrize(
    "text",
    [
        "",
        "normal",
        "%20",
        "%252F",
        "%E2%82%AC",
        "%FF",
        "%E2x%82",
        "%",
        "%2",
        "%G0",
        "ä%20猫",
        "%00",
        "+",
        "%2b",
        "abc%2Fdef",
    ],
)
def test_media_percent_decoder_matches_stdlib(text):
    assert _decode_media_escapes(text) == unquote(text)


def test_media_percent_decoder_matches_stdlib_mixed_inputs():
    atoms = ["abc", "%", "%00", "%ff", "%E2", "%82", "%AC", "%2f", "%gg", "ä", "+", "/"]
    for items in product(atoms, repeat=3):
        text = "".join(items)
        assert _decode_media_escapes(text) == unquote(text)
