"""Shared constants for Wisconsin State Patrol — Lakeville Roleplay."""

from enum import IntEnum

DEPARTMENT = "Wisconsin State Patrol"
COMMUNITY = "Lakeville Roleplay"
SHORT_NAME = "WSP"
FOOTER = "Wisconsin State Patrol  •  Lakeville Roleplay"

# Embed colors — navy / gold command palette
COLOR_NAVY = 0x0D2137
COLOR_GOLD = 0xC9A227
COLOR_STEEL = 0x3D5A80
COLOR_SUCCESS = 0x1F6B4A
COLOR_WARNING = 0xC9782A
COLOR_DANGER = 0x8B1E3F
COLOR_INFO = 0x2E5A88
COLOR_MUTED = 0x6B7280

DEFAULT_RANKS = [
    ("Probationary Trooper", 1, 1),
    ("Trooper", 2, 1),
    ("Senior Trooper", 3, 1),
    ("Master Trooper", 4, 1),
    ("Sergeant", 5, 2),
    ("Lieutenant", 6, 3),
    ("Captain", 7, 4),
    ("Major", 8, 4),
    ("Colonel", 9, 4),
    ("Superintendent", 10, 5),
]

HIGH_RANKS = frozenset({"Lieutenant", "Captain", "Major", "Colonel", "Superintendent"})
MIDDLE_RANKS = frozenset({"Sergeant"})
LOW_RANKS = frozenset({"Master Trooper", "Senior Trooper", "Trooper", "Probationary Trooper"})
BAND_ROLE_KEYS = {"high": "high_rank", "middle": "middle_rank", "low": "low_rank"}


def rank_band(name: str | None) -> str | None:
    if name in HIGH_RANKS:
        return "high"
    if name in MIDDLE_RANKS:
        return "middle"
    if name in LOW_RANKS:
        return "low"
    return None


class PermissionLevel(IntEnum):
    TROOPER = 1
    SUPERVISOR = 2
    HR = 3
    COMMAND = 4
    SUPERINTENDENT = 5
    OWNER = 6


LEVEL_LABELS = {
    PermissionLevel.TROOPER: "Trooper",
    PermissionLevel.SUPERVISOR: "Supervisor",
    PermissionLevel.HR: "HR",
    PermissionLevel.COMMAND: "Command",
    PermissionLevel.SUPERINTENDENT: "Superintendent",
    PermissionLevel.OWNER: "Owner",
}

SENSITIVE_PROFILE_LEVEL = PermissionLevel.HR
