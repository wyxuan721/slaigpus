"""Fixed, non-debug ACP compute resource profiles.

The ACP create API accepts an opaque resource-specification id, while the
Console presents that id as one indivisible hardware choice.  Keeping those
values in a static catalogue prevents callers from combining a GPU count,
vCPU count, and memory size that SenseCore does not actually offer.

The entries below were confirmed from the live non-debug compute pools.  Live
API discovery may establish that a profile is unavailable in one workspace;
it must never add an unknown profile to this allow-list.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import FrozenSet, Mapping, Tuple


GPU_TYPE_N6LS = "NVIDIA N6lS-80G-SXM5"
CPU_TYPE_8468 = "Intel 8468-2.1GHz"

RESOURCE_CLASSES: FrozenSet[str] = frozenset({"standard", "spot"})
_ALL_RESOURCE_CLASSES: FrozenSet[str] = RESOURCE_CLASSES


class ResourceProfileError(ValueError):
    """A resource profile key or requested resource class is not allowed."""


@dataclass(frozen=True)
class ResourceProfile:
    """One atomic ACP hardware choice and its exact create-API identifier."""

    key: str
    spec_name: str
    gpu_type: str
    gpu_cards: int
    cpu_type: str
    vcpus: int
    memory_gib: int
    classes: FrozenSet[str]

    def __post_init__(self) -> None:
        for value, label in (
            (self.key, "profile key"),
            (self.spec_name, "resource specification id"),
            (self.gpu_type, "GPU type"),
            (self.cpu_type, "CPU type"),
        ):
            if type(value) is not str or not value or value.strip() != value:
                raise ValueError(f"invalid {label}")
        for value, label in (
            (self.gpu_cards, "GPU card count"),
            (self.vcpus, "vCPU count"),
            (self.memory_gib, "memory size"),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"invalid {label}")
        if type(self.classes) is not frozenset or not self.classes:
            raise ValueError("invalid resource classes")
        if not self.classes.issubset(RESOURCE_CLASSES):
            raise ValueError("invalid resource classes")
        expected_key = (
            f"n6ls-80g-sxm5-{self.gpu_cards}x-"
            f"{self.vcpus}c-{self.memory_gib}g"
        )
        if self.key != expected_key:
            raise ValueError("resource profile key does not match its hardware shape")


def _profile(
    spec_name: str,
    gpu_cards: int,
    vcpus: int,
    memory_gib: int,
) -> ResourceProfile:
    return ResourceProfile(
        key=f"n6ls-80g-sxm5-{gpu_cards}x-{vcpus}c-{memory_gib}g",
        spec_name=spec_name,
        gpu_type=GPU_TYPE_N6LS,
        gpu_cards=gpu_cards,
        cpu_type=CPU_TYPE_8468,
        vcpus=vcpus,
        memory_gib=memory_gib,
        classes=_ALL_RESOURCE_CLASSES,
    )


RESOURCE_PROFILES: Tuple[ResourceProfile, ...] = (
    _profile("N6lS.Iu.I10.1", 1, 22, 230),
    _profile("N6lS.Iu.I10.2", 2, 44, 460),
    _profile("N6lS.Iu.I10.4", 4, 88, 920),
    _profile("N6lS.Iu.I10.8", 8, 176, 1840),
    _profile("N6lS.Iu.I10.1.8c128g", 1, 8, 128),
    _profile("N6lS.Iu.I10.1.14c198g", 1, 14, 198),
    _profile("N6lS.Iu.I10.2.28c396g", 2, 28, 396),
    _profile("N6lS.Iu.I10.4.56c792g", 4, 56, 792),
    _profile("N6lS.Iu.I10.6.84c1188g", 6, 84, 1188),
    _profile("N6lS.Iu.I10.8.64c1024g", 8, 64, 1024),
    _profile("N6lS.Iu.I10.8.112c1584g", 8, 112, 1584),
)

RESOURCE_PROFILES_BY_KEY: Mapping[str, ResourceProfile] = MappingProxyType(
    {profile.key: profile for profile in RESOURCE_PROFILES}
)
if len(RESOURCE_PROFILES_BY_KEY) != len(RESOURCE_PROFILES):  # pragma: no cover
    raise RuntimeError("duplicate ACP resource profile key")
if len({profile.spec_name for profile in RESOURCE_PROFILES}) != len(RESOURCE_PROFILES):
    raise RuntimeError("duplicate ACP resource specification id")  # pragma: no cover

RESOURCE_PROFILE_KEYS: Tuple[str, ...] = tuple(sorted(RESOURCE_PROFILES_BY_KEY))

DEFAULT_RESOURCE_PROFILE_KEY = "n6ls-80g-sxm5-2x-28c-396g"
DEFAULT_RESOURCE_PROFILE = RESOURCE_PROFILES_BY_KEY[DEFAULT_RESOURCE_PROFILE_KEY]


def resolve_resource_profile(
    key: str,
    *,
    resource_class: str,
) -> ResourceProfile:
    """Resolve one exact catalogue key for ``standard`` or ``spot`` resources.

    Values are intentionally not trimmed, case-folded, or inferred.  CLI and
    library callers therefore share the same closed set of accepted inputs.
    """

    if type(resource_class) is not str or resource_class not in RESOURCE_CLASSES:
        raise ResourceProfileError("unknown ACP resource class")
    if type(key) is not str:
        raise ResourceProfileError("unknown ACP resource profile")
    profile = RESOURCE_PROFILES_BY_KEY.get(key)
    if profile is None:
        raise ResourceProfileError("unknown ACP resource profile")
    if resource_class not in profile.classes:
        raise ResourceProfileError(
            "ACP resource profile does not support the requested resource class"
        )
    return profile


__all__ = [
    "GPU_TYPE_N6LS",
    "CPU_TYPE_8468",
    "RESOURCE_CLASSES",
    "ResourceProfileError",
    "ResourceProfile",
    "RESOURCE_PROFILES",
    "RESOURCE_PROFILES_BY_KEY",
    "RESOURCE_PROFILE_KEYS",
    "DEFAULT_RESOURCE_PROFILE_KEY",
    "DEFAULT_RESOURCE_PROFILE",
    "resolve_resource_profile",
]
