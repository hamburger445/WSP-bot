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
    ("Corporal", 5, 2),
    ("Sergeant", 6, 2),
    ("Lieutenant", 7, 3),
    ("Captain", 8, 4),
    ("Major", 9, 4),
    ("Lieutenant Colonel", 10, 4),
    ("Colonel", 11, 4),
    ("Superintendent", 12, 5),
]

FASTPASS_CATEGORIES = [
    "Traffic Stops",
    "Radio Communications",
    "Traffic Laws & Violations",
    "Probable Cause / Reasonable Suspicion",
    "De-escalation",
    "Pursuit Procedures",
    "Use of Force",
    "Incident / Arrest Reports",
    "General Law Enforcement Procedures",
    "Overall Law Enforcement Knowledge",
]

FASTPASS_SCALE = {
    1: "Very unfamiliar",
    2: "Limited knowledge",
    3: "Somewhat familiar",
    4: "Confident",
    5: "Very confident",
}

FASTPASS_RECOMMENDATIONS = {
    "full_training": "Full Training Required",
    "partial_training": "Partial Training Required",
    "waived": "Training Waived",
    "additional_eval": "Additional Evaluation Required",
}

DISCIPLINE_ACTIONS = [
    "Verbal Warning",
    "Written Warning",
    "Strike",
    "Suspension",
    "Demotion",
    "Removal",
]

TICKET_TYPES = [
    ("resignation", "Resignation"),
    ("loa", "LOA Request"),
    ("hr_question", "HR Question"),
    ("complaint", "Complaint"),
    ("transfer", "Transfer"),
    ("appeal", "Appeal"),
    ("general", "General Assistance"),
]

SUPERVISION_SCORE_FIELDS = [
    ("radio_score", "Radio performance"),
    ("driving_score", "Driving"),
    ("scene_score", "Scene management"),
    ("communication_score", "Communication"),
    ("policy_score", "Policy knowledge"),
    ("overall_score", "Overall performance"),
]


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
