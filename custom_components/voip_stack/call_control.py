"""Session-level hold and RFC 3515 transfer coordination."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from .endpoint_session import EndpointCallSession, SessionPhase
from .session_cleanup import async_wait_for_cleanup


class ControlDisposition(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"


class ReferSubscription(Protocol):
    """Accepted REFER subscription whose final NOTIFY carries sipfrag status."""

    async def wait_final(self) -> int: ...

    async def close(self) -> None: ...


class ControllableLeg(Protocol):
    leg_id: str

    async def set_hold(self, held: bool) -> bool: ...

    async def refer(
        self,
        target_uri: str,
        *,
        replaces: str = "",
    ) -> ReferSubscription: ...


@dataclass(frozen=True, slots=True)
class HoldResult:
    disposition: ControlDisposition
    held: bool
    changed_legs: tuple[str, ...] = ()
    failed_legs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TransferResult:
    disposition: ControlDisposition
    notify_status: int = 0
    target_uri: str = ""


class SessionCallController:
    """Serialize call control while the original session remains authoritative."""

    def __init__(self, session: EndpointCallSession) -> None:
        self.session = session
        self.token = session.token
        self._lock = asyncio.Lock()

    async def _set_hold(
        self,
        legs: tuple[ControllableLeg, ...],
        held: bool,
    ) -> HoldResult:
        async with self._lock:
            expected = SessionPhase.ESTABLISHED if held else SessionPhase.HELD
            if not self.session.owns(self.token) or self.session.phase is not expected:
                return HoldResult(ControlDisposition.CANCELLED, held)
            results = await asyncio.gather(
                *(leg.set_hold(held) for leg in legs),
                return_exceptions=True,
            )
            changed = tuple(
                leg
                for leg, result in zip(legs, results, strict=True)
                if result is True
            )
            failed = tuple(
                leg
                for leg, result in zip(legs, results, strict=True)
                if result is not True
            )
            if failed or not self.session.owns(self.token):
                if changed:
                    async def _rollback() -> None:
                        await asyncio.gather(
                            *(leg.set_hold(not held) for leg in changed),
                            return_exceptions=True,
                        )

                    rollback = asyncio.create_task(
                        _rollback(),
                        name=f"voip-call-hold-rollback-{self.session.call_id}",
                    )
                    await async_wait_for_cleanup(rollback)
                return HoldResult(
                    ControlDisposition.FAILED,
                    held,
                    changed_legs=tuple(leg.leg_id for leg in changed),
                    failed_legs=tuple(leg.leg_id for leg in failed),
                )
            self.session.transition(
                SessionPhase.HELD if held else SessionPhase.ESTABLISHED,
                expected={expected},
            )
            return HoldResult(
                ControlDisposition.SUCCEEDED,
                held,
                changed_legs=tuple(leg.leg_id for leg in changed),
            )

    async def set_hold(
        self,
        legs: tuple[ControllableLeg, ...],
        held: bool,
    ) -> HoldResult:
        """Apply hold atomically; partial success is explicitly rolled back."""

        if not legs:
            raise ValueError("hold requires at least one call leg")
        operation = asyncio.create_task(
            self._set_hold(legs, bool(held)),
            name=f"voip-call-{'hold' if held else 'resume'}-{self.session.call_id}",
        )
        return await async_wait_for_cleanup(operation)

    async def _transfer(
        self,
        leg: ControllableLeg,
        target_uri: str,
        *,
        replaces: str,
        timeout: float,
    ) -> TransferResult:
        async with self._lock:
            if (
                not self.session.owns(self.token)
                or self.session.phase is not SessionPhase.ESTABLISHED
            ):
                return TransferResult(ControlDisposition.CANCELLED, target_uri=target_uri)
            self.session.transition(
                SessionPhase.TRANSFERRING,
                expected={SessionPhase.ESTABLISHED},
            )
            subscription: ReferSubscription | None = None
            try:
                subscription = await leg.refer(target_uri, replaces=replaces)
                notify = asyncio.create_task(
                    subscription.wait_final(),
                    name=f"voip-call-transfer-notify-{self.session.call_id}",
                )
                terminated = asyncio.create_task(
                    self.session.termination_started.wait(),
                    name=f"voip-call-transfer-termination-{self.session.call_id}",
                )
                done, _pending = await asyncio.wait(
                    {notify, terminated},
                    timeout=float(timeout),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if terminated in done and terminated.result():
                    disposition = ControlDisposition.CANCELLED
                    status = 0
                elif notify in done:
                    try:
                        status = int(notify.result())
                    except Exception:
                        status = 0
                    disposition = (
                        ControlDisposition.SUCCEEDED
                        if 200 <= status < 300
                        else ControlDisposition.FAILED
                    )
                else:
                    disposition = ControlDisposition.TIMEOUT
                    status = 0
                for task in (notify, terminated):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(notify, terminated, return_exceptions=True)
            except Exception:
                disposition = ControlDisposition.FAILED
                status = 0
            finally:
                if subscription is not None:
                    await subscription.close()

            if disposition is ControlDisposition.SUCCEEDED:
                await self.session.terminate("transferred")
            elif self.session.owns(self.token):
                self.session.transition(
                    SessionPhase.ESTABLISHED,
                    expected={SessionPhase.TRANSFERRING},
                )
            return TransferResult(disposition, status, target_uri)

    async def transfer(
        self,
        leg: ControllableLeg,
        target_uri: str,
        *,
        replaces: str = "",
        timeout: float = 30.0,
    ) -> TransferResult:
        """REFER a confirmed dialog and commit only after a successful NOTIFY."""

        clean_target = str(target_uri or "").strip()
        if not clean_target:
            raise ValueError("transfer target must not be empty")
        if not 0.0 < float(timeout) <= 300.0:
            raise ValueError("transfer timeout must be between 0 and 300 seconds")
        operation: Awaitable[TransferResult] = self._transfer(
            leg,
            clean_target,
            replaces=str(replaces or "").strip(),
            timeout=float(timeout),
        )
        task = asyncio.create_task(
            operation,
            name=f"voip-call-transfer-{self.session.call_id}",
        )
        return await async_wait_for_cleanup(task)
