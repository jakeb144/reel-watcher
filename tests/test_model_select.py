"""Model selection logic (no downloads, no model loading)."""
import pytest

from reel_watcher.model import SIZES, select_models

GB = 1024**3


def test_auto_small_below_32gb():
    assert select_models(env={}, ram_bytes=16 * GB)[2] == "small"
    assert select_models(env={}, ram_bytes=24 * GB)[2] == "small"


def test_auto_large_at_32gb_and_up():
    assert select_models(env={}, ram_bytes=32 * GB)[2] == "large"
    assert select_models(env={}, ram_bytes=64 * GB)[0] == SIZES["large"][0]


def test_explicit_size_beats_ram():
    assert select_models("large", env={}, ram_bytes=8 * GB)[2] == "large"
    assert select_models("small", env={}, ram_bytes=128 * GB) == (*SIZES["small"], "small")


def test_env_size_and_flag_precedence():
    assert select_models(env={"REEL_WATCHER_MODEL": "small"}, ram_bytes=64 * GB)[2] == "small"
    assert select_models("large", env={"REEL_WATCHER_MODEL": "small"})[2] == "large"


def test_model_override_keeps_whisper_of_size():
    vlm, whisper, size = select_models("small", model="org/custom", env={})
    assert (vlm, whisper, size) == ("org/custom", SIZES["small"][1], "small")


def test_env_repo_overrides():
    vlm, whisper, _ = select_models("small", env={"REEL_WATCHER_VLM": "a/b", "REEL_WATCHER_WHISPER": "c/d"})
    assert (vlm, whisper) == ("a/b", "c/d")
    assert select_models("small", model="x/y", env={"REEL_WATCHER_VLM": "a/b"})[0] == "x/y"


def test_bad_size():
    with pytest.raises(ValueError):
        select_models("huge", env={})
