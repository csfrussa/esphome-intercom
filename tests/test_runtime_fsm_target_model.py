#!/usr/bin/env python3
"""Executable target model for the next runtime FSM refactor.

These tests do not mirror today's YAML implementation line-by-line. They define
the behavioral contract the refactor must satisfy before it replaces the
current generic policy soup in full voip profiles.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import unittest


class CallState(str, Enum):
    IDLE = "idle"
    CALLING = "calling"
    RINGING = "ringing"
    IN_CALL = "in_call"


class VaState(str, Enum):
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    RESPONDING = "responding"


class MediaOwner(str, Enum):
    NONE = "none"
    USER = "user"
    VA_TTS = "va_tts"
    VOIP_RINGTONE = "voip_ringtone"
    TIMER = "timer"


@dataclass(frozen=True, slots=True)
class RuntimeFacts:
    boot: bool = False
    wifi_connected: bool = True
    ha_connected: bool = True
    va_ready: bool = True
    mic_muted: bool = False
    speaker_muted: bool = False
    dnd: bool = False
    call_state: CallState = CallState.IDLE
    call_generation: int = 0
    va_state: VaState = VaState.IDLE
    va_generation: int = 0
    media_playing: bool = False
    media_owner: MediaOwner = MediaOwner.NONE
    media_generation: int = 0
    ringtone_stopping_generation: int = 0


@dataclass(frozen=True, slots=True)
class LedSnapshot:
    mode: str
    rgb: tuple[int, int, int]
    effect: str | None
    owner: str


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    public_state: str
    led: LedSnapshot
    audio_policy: str
    ringtone: str
    mww_allowed: bool


def reduce_runtime(facts: RuntimeFacts) -> RuntimeSnapshot:
    if facts.boot:
        return RuntimeSnapshot("boot", LedSnapshot("boot", (0, 0, 255), "Slow Pulse", "system"), "normal", "stop", False)
    if not facts.wifi_connected:
        return RuntimeSnapshot("no_wifi", LedSnapshot("no_wifi", (255, 120, 0), "Slow Pulse", "system"), "normal", "stop", False)
    if not facts.ha_connected:
        return RuntimeSnapshot("no_ha", LedSnapshot("no_ha", (255, 120, 0), "Blink", "system"), "normal", "stop", False)

    if facts.call_state == CallState.RINGING:
        return RuntimeSnapshot(
            "voip_ringing",
            LedSnapshot("voip_ringing", (255, 0, 0), "Ringing", "voip"),
            "duck",
            "play",
            False,
        )
    if facts.call_state == CallState.CALLING:
        return RuntimeSnapshot(
            "voip_calling",
            LedSnapshot("voip_calling", (255, 150, 0), "Calling", "voip"),
            "duck",
            "stop",
            False,
        )
    if facts.call_state == CallState.IN_CALL:
        return RuntimeSnapshot(
            "voip_in_call",
            LedSnapshot("voip_in_call", (111, 255, 115), None, "voip"),
            "duck",
            "stop",
            False,
        )

    voip_media_residue = (
        facts.media_owner == MediaOwner.VOIP_RINGTONE
        or (
            facts.ringtone_stopping_generation
            and facts.ringtone_stopping_generation == facts.media_generation
        )
    )
    if facts.va_state == VaState.RESPONDING:
        return RuntimeSnapshot("va_responding", LedSnapshot("va_responding", (0, 255, 0), "Spin", "va"), "duck", "stop", False)
    if facts.va_state == VaState.THINKING:
        return RuntimeSnapshot("va_thinking", LedSnapshot("va_thinking", (255, 180, 0), "Spin", "va"), "duck", "stop", False)
    if facts.va_state == VaState.LISTENING:
        return RuntimeSnapshot("va_listening", LedSnapshot("va_listening", (0, 0, 255), "Slow Pulse", "va"), "duck", "stop", False)
    if facts.media_playing and not voip_media_residue:
        return RuntimeSnapshot("media", LedSnapshot("media", (0, 255, 0), "Spin", "media"), "normal", "stop", False)
    if facts.mic_muted and facts.speaker_muted:
        return RuntimeSnapshot("muted", LedSnapshot("muted", (255, 80, 0), "Slow Pulse", "audio_policy"), "normal", "stop", False)
    if facts.mic_muted:
        return RuntimeSnapshot("mic_muted", LedSnapshot("mic_muted", (255, 80, 0), None, "audio_policy"), "normal", "stop", False)
    if facts.speaker_muted:
        return RuntimeSnapshot("speaker_muted", LedSnapshot("speaker_muted", (255, 80, 0), None, "audio_policy"), "normal", "stop", True)
    return RuntimeSnapshot("idle", LedSnapshot("idle", (0, 0, 0), None, "idle"), "normal", "stop", True)


class RuntimeFsmTargetModelTest(unittest.TestCase):
    def assertNoMediaSpin(self, snapshot: RuntimeSnapshot) -> None:
        self.assertFalse(
            snapshot.led.owner == "media" and snapshot.led.effect == "Spin",
            snapshot,
        )

    def test_voip_ringing_masks_media_and_va(self) -> None:
        snap = reduce_runtime(
            RuntimeFacts(
                call_state=CallState.RINGING,
                va_state=VaState.RESPONDING,
                media_playing=True,
                media_owner=MediaOwner.USER,
            )
        )
        self.assertEqual(snap.public_state, "voip_ringing")
        self.assertEqual(snap.led.effect, "Ringing")
        self.assertEqual(snap.led.rgb, (255, 0, 0))
        self.assertEqual(snap.ringtone, "play")
        self.assertNoMediaSpin(snap)

    def test_voip_in_call_is_fixed_green_even_if_ringtone_media_is_still_draining(self) -> None:
        snap = reduce_runtime(
            RuntimeFacts(
                call_state=CallState.IN_CALL,
                call_generation=7,
                media_playing=True,
                media_owner=MediaOwner.VOIP_RINGTONE,
                media_generation=7,
                ringtone_stopping_generation=7,
            )
        )
        self.assertEqual(snap.public_state, "voip_in_call")
        self.assertEqual(snap.led.rgb, (111, 255, 115))
        self.assertIsNone(snap.led.effect)
        self.assertNoMediaSpin(snap)

    def test_voip_ringtone_residue_does_not_become_media_after_hangup(self) -> None:
        snap = reduce_runtime(
            RuntimeFacts(
                call_state=CallState.IDLE,
                media_playing=True,
                media_owner=MediaOwner.VOIP_RINGTONE,
                media_generation=11,
                ringtone_stopping_generation=11,
            )
        )
        self.assertEqual(snap.public_state, "idle")
        self.assertNoMediaSpin(snap)

    def test_real_user_media_still_gets_media_led_when_no_call_owns_it(self) -> None:
        snap = reduce_runtime(
            RuntimeFacts(
                media_playing=True,
                media_owner=MediaOwner.USER,
                media_generation=12,
            )
        )
        self.assertEqual(snap.public_state, "media")
        self.assertEqual(snap.led.owner, "media")
        self.assertEqual(snap.led.effect, "Spin")

    def test_intercom_led_contract_wins_over_mute_policy(self) -> None:
        snap = reduce_runtime(RuntimeFacts(call_state=CallState.RINGING, mic_muted=True, speaker_muted=True))
        self.assertEqual(snap.public_state, "voip_ringing")
        self.assertEqual(snap.led.effect, "Ringing")
        self.assertEqual(snap.led.owner, "voip")

    def test_mww_is_disabled_during_call_and_reenabled_when_idle(self) -> None:
        self.assertFalse(reduce_runtime(RuntimeFacts(call_state=CallState.CALLING)).mww_allowed)
        self.assertFalse(reduce_runtime(RuntimeFacts(call_state=CallState.RINGING)).mww_allowed)
        self.assertFalse(reduce_runtime(RuntimeFacts(call_state=CallState.IN_CALL)).mww_allowed)
        self.assertTrue(reduce_runtime(RuntimeFacts()).mww_allowed)


if __name__ == "__main__":
    unittest.main()
