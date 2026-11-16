import pytest

from chomik_core.names import (
    chomik_path,
    decode_segment,
    encode_segment,
    local_filename,
    path_from_url,
    remote_dirname,
    remote_filename,
    split_path,
)


def test_encode_matches_chomikuj_url_style():
    assert encode_segment("Audi Instrukcja Obsługi") == "Audi+Instrukcja+Obs*c5*82ugi"


@pytest.mark.parametrize("name", ["Zażółć gęślą jaźń", "a+b & c", "100% *real*", "plain"])
def test_segment_roundtrip(name):
    assert decode_segment(encode_segment(name)) == name


def test_decode_accepts_percent():
    assert decode_segment("Obs%C5%82ugi") == "Obsługi"


def test_paths():
    assert chomik_path("me", ["Books", "Sci Fi"]) == "/me/Books/Sci+Fi"
    assert path_from_url("https://chomikuj.pl/user/Folder+X/") == "/user/Folder+X"
    assert path_from_url("user/Folder") == "/user/Folder"
    assert split_path("/user/Obs*c5*82ugi/x") == ("user", ["Obsługi", "x"])


def test_sanitisers():
    assert remote_dirname(' a:b*c. ') == "abc"
    assert remote_filename('a:b?c.pdf') == "a b c.pdf"
    assert local_filename("../x:y") == "_x_y"
