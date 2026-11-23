"""Start the combat assistant and expose its existing entry points."""

from wow_assistant.combat.policy import AttackPolicy, AttackSkill, CombatPolicy
from wow_assistant.combat.skills import (ATTACK_SKILLS, TEMPLATE, _skill_capture_plan, classify_attack_icon,
                                         classify_icon, icon_similarity, validate_skill_regions)
from wow_assistant.combat.worker import CombatWorker
from wow_assistant.device.hotkeys import Hotkeys, LowLevelKeyboardEvent, LowLevelMouseEvent
from wow_assistant.device.windows import HARDWAREINPUT, INPUT, INPUTDATA, KEYBDINPUT, MOUSEINPUT, WindowsInput
from wow_assistant.paths import ROOT as BASE
from wow_assistant.ui.app import CONFIG, CombatApp, main


if __name__ == "__main__":
    raise SystemExit(main())
