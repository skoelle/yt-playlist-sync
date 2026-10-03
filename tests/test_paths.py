# Copyright (c) 2026 Stefan Koelle (https://stefankoelle.de)
# Licensed under the MIT License. See LICENSE file in project root for details.
from app.paths import is_oneshot, sanitize_folder_name


def test_is_oneshot_variants():
    assert is_oneshot("Setlist", "setlist")
    assert is_oneshot("SETLIST 2024", "setlist")
    assert is_oneshot("Meine setlist fuer Rock", "setlist")
    assert not is_oneshot("Sommer Mix", "setlist")
    assert not is_oneshot("anything", "")


def test_is_oneshot_multiple_keywords():
    kw = "setlist,concert"
    assert is_oneshot("Caliban Concert 2024", kw)
    assert is_oneshot("Band Setlist Night", kw)
    assert is_oneshot("open CONCERT air", kw)
    assert not is_oneshot("Sommer Mix", kw)
    assert is_oneshot("Concert for two", " setlist , concert ")
    assert not is_oneshot("plain title", " , ")
    assert not is_oneshot("", kw)


def test_sanitize_blocks_traversal():
    name = sanitize_folder_name("../../etc/passwd", "PL123")
    assert "/" not in name and ".." not in name
    assert name.endswith("[PL123]")


def test_sanitize_special_chars_and_empty():
    assert sanitize_folder_name('a:b*c?"d<e>f|g', "PLx") == "a b c d e f g [PLx]"
    assert sanitize_folder_name("...", "PLx") == "playlist [PLx]"


def test_sanitize_long_unicode_title():
    name = sanitize_folder_name("\u00e4" * 300, "PLx")
    assert len(name.encode()) <= 150 + len(" [PLx]")
    name.encode("utf-8")


def test_sanitize_playlist_id():
    assert sanitize_folder_name("t", "a/b").endswith("[a_b]")
