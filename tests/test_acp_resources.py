"""Offline contracts for the fixed ACP resource-profile catalogue."""

from __future__ import annotations

import re
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from slaigpus.acp_resources import (  # noqa: E402
    CPU_TYPE_8468,
    DEFAULT_RESOURCE_PROFILE,
    DEFAULT_RESOURCE_PROFILE_KEY,
    GPU_TYPE_N6LS,
    RESOURCE_CLASSES,
    RESOURCE_PROFILE_KEYS,
    RESOURCE_PROFILES,
    RESOURCE_PROFILES_BY_KEY,
    ResourceProfileError,
    resolve_resource_profile,
)


EXPECTED = {
    "n6ls-80g-sxm5-1x-22c-230g": ("N6lS.Iu.I10.1", 1, 22, 230),
    "n6ls-80g-sxm5-2x-44c-460g": ("N6lS.Iu.I10.2", 2, 44, 460),
    "n6ls-80g-sxm5-4x-88c-920g": ("N6lS.Iu.I10.4", 4, 88, 920),
    "n6ls-80g-sxm5-8x-176c-1840g": ("N6lS.Iu.I10.8", 8, 176, 1840),
    "n6ls-80g-sxm5-1x-8c-128g": ("N6lS.Iu.I10.1.8c128g", 1, 8, 128),
    "n6ls-80g-sxm5-1x-14c-198g": ("N6lS.Iu.I10.1.14c198g", 1, 14, 198),
    "n6ls-80g-sxm5-2x-28c-396g": ("N6lS.Iu.I10.2.28c396g", 2, 28, 396),
    "n6ls-80g-sxm5-4x-56c-792g": ("N6lS.Iu.I10.4.56c792g", 4, 56, 792),
    "n6ls-80g-sxm5-6x-84c-1188g": ("N6lS.Iu.I10.6.84c1188g", 6, 84, 1188),
    "n6ls-80g-sxm5-8x-64c-1024g": ("N6lS.Iu.I10.8.64c1024g", 8, 64, 1024),
    "n6ls-80g-sxm5-8x-112c-1584g": ("N6lS.Iu.I10.8.112c1584g", 8, 112, 1584),
}


def test_catalogue_contains_exactly_the_confirmed_atomic_profiles():
    assert len(RESOURCE_PROFILES) == len(EXPECTED) == 11
    assert set(RESOURCE_PROFILES_BY_KEY) == set(EXPECTED)

    for key, (spec_name, gpu_cards, vcpus, memory_gib) in EXPECTED.items():
        profile = RESOURCE_PROFILES_BY_KEY[key]
        assert profile.key == key
        assert profile.spec_name == spec_name
        assert profile.gpu_type == GPU_TYPE_N6LS == "NVIDIA N6lS-80G-SXM5"
        assert profile.gpu_cards == gpu_cards
        assert profile.cpu_type == CPU_TYPE_8468 == "Intel 8468-2.1GHz"
        assert profile.vcpus == vcpus
        assert profile.memory_gib == memory_gib
        assert profile.classes == frozenset({"standard", "spot"})
        assert re.fullmatch(
            r"n6ls-80g-sxm5-\d+x-\d+c-\d+g",
            profile.key,
        )


def test_catalogue_keys_and_api_ids_are_unique_and_keys_are_stably_sorted():
    assert isinstance(RESOURCE_PROFILES, tuple)
    assert len({profile.key for profile in RESOURCE_PROFILES}) == len(RESOURCE_PROFILES)
    assert len({profile.spec_name for profile in RESOURCE_PROFILES}) == len(
        RESOURCE_PROFILES
    )
    assert RESOURCE_PROFILE_KEYS == tuple(sorted(EXPECTED))
    assert RESOURCE_CLASSES == frozenset({"standard", "spot"})


def test_default_profile_is_the_confirmed_two_card_28_vcpu_shape():
    assert DEFAULT_RESOURCE_PROFILE_KEY == "n6ls-80g-sxm5-2x-28c-396g"
    assert DEFAULT_RESOURCE_PROFILE is RESOURCE_PROFILES_BY_KEY[
        DEFAULT_RESOURCE_PROFILE_KEY
    ]
    assert (
        DEFAULT_RESOURCE_PROFILE.spec_name,
        DEFAULT_RESOURCE_PROFILE.gpu_cards,
        DEFAULT_RESOURCE_PROFILE.vcpus,
        DEFAULT_RESOURCE_PROFILE.memory_gib,
    ) == ("N6lS.Iu.I10.2.28c396g", 2, 28, 396)


def test_catalogue_profiles_mapping_and_classes_are_immutable():
    with pytest.raises(FrozenInstanceError):
        DEFAULT_RESOURCE_PROFILE.vcpus = 99  # type: ignore[misc]
    with pytest.raises(TypeError):
        RESOURCE_PROFILES_BY_KEY["replacement"] = DEFAULT_RESOURCE_PROFILE  # type: ignore[index]
    with pytest.raises(AttributeError):
        DEFAULT_RESOURCE_PROFILE.classes.add("debug")  # type: ignore[attr-defined]
    with pytest.raises(TypeError):
        RESOURCE_PROFILES[0] = DEFAULT_RESOURCE_PROFILE  # type: ignore[index]


@pytest.mark.parametrize("resource_class", ["standard", "spot"])
def test_strict_resolver_accepts_only_a_catalogued_key_for_supported_classes(
    resource_class,
):
    assert (
        resolve_resource_profile(
            DEFAULT_RESOURCE_PROFILE_KEY,
            resource_class=resource_class,
        )
        is DEFAULT_RESOURCE_PROFILE
    )


@pytest.mark.parametrize(
    "key",
    [
        "",
        " n6ls-80g-sxm5-2x-28c-396g",
        "n6ls-80g-sxm5-2x-28c-396g ",
        "N6LS-80G-SXM5-2X-28C-396G",
        "n6ls-80g-sxm5-3x-28c-396g",
        None,
    ],
)
def test_strict_resolver_rejects_unknown_or_noncanonical_keys(key):
    with pytest.raises(ResourceProfileError, match="unknown ACP resource profile"):
        resolve_resource_profile(key, resource_class="spot")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "resource_class",
    ["", "idle", "debug", "SPOT", " standard", "standard ", None],
)
def test_strict_resolver_rejects_unknown_or_noncanonical_resource_classes(
    resource_class,
):
    with pytest.raises(ResourceProfileError, match="unknown ACP resource class"):
        resolve_resource_profile(
            DEFAULT_RESOURCE_PROFILE_KEY,
            resource_class=resource_class,  # type: ignore[arg-type]
        )


def test_strict_resolver_requires_an_explicit_resource_class():
    with pytest.raises(TypeError):
        resolve_resource_profile(DEFAULT_RESOURCE_PROFILE_KEY)  # type: ignore[call-arg]
