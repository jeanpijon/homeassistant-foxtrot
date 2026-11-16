"""Keeps the weekly programs of the thermostats equal to Home Assistant schedule helpers.

The helper is the master: whenever one that is linked (options of the integration) changes, and now
and then in between, its blocks are turned into the thermostat's weekly program and, if that differs
from what the PLC runs, written into the PLC. The PLC keeps running the program by itself.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import (
    async_call_later,
    async_track_state_change_event,
    async_track_state_report_event,
    async_track_time_interval,
)
from homeassistant.helpers.start import async_at_started

from . import const
from .const import CONF_SCHEDULES, DOMAIN
from .coordinator import FoxtrotCoordinator
from .schedule import ScheduleError, week_from_helper

_LOGGER = logging.getLogger(__name__)


class ScheduleSync:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, coordinator: FoxtrotCoordinator):
        self.hass, self.entry, self.coordinator = hass, entry, coordinator
        self.links: dict[str, dict[str, str]] = dict(entry.options.get(CONF_SCHEDULES) or {})
        self._lock = asyncio.Lock()
        self._unsubscribe: list = []
        self._debounce = None
        self._stopped = False

    def start(self) -> None:
        if not self.links:
            return
        entities = {link["entity_id"] for link in self.links.values()}
        # A helper edited in the UI may keep its state, then only a "state reported" event tells.
        self._unsubscribe = [
            async_track_state_change_event(self.hass, list(entities), self._changed),
            async_track_state_report_event(self.hass, list(entities), self._changed),
            async_track_time_interval(self.hass, self._tick, timedelta(seconds=const.RESYNC_INTERVAL)),
            async_at_started(self.hass, self._started),
        ]

    def stop(self) -> None:
        self._stopped = True
        if self._debounce is not None:
            self._debounce()
            self._debounce = None
        for unsubscribe in self._unsubscribe:
            if callable(unsubscribe):
                unsubscribe()
        self._unsubscribe = []

    # ----- triggers ------------------------------------------------------------------------

    @callback
    def _started(self, _hass: HomeAssistant) -> None:
        self._later()

    @callback
    def _changed(self, _event: Event) -> None:
        self._later()

    @callback
    def _tick(self, _now) -> None:
        self._later()

    def _later(self) -> None:
        """Send once, a moment after the last change (an edit in the UI is several changes)."""
        if self._stopped:
            return
        if self._debounce is not None:
            self._debounce()
        self._debounce = async_call_later(self.hass, const.SYNC_DEBOUNCE, self._run)

    async def _run(self, _now=None) -> None:
        self._debounce = None
        if self._stopped:
            return
        async with self._lock:
            for prog, link in self.links.items():
                await self._sync_room(prog, link)

    # ----- one room ------------------------------------------------------------------------

    def _block(self, prog: str) -> dict[str, Any] | None:
        for face in (self.coordinator.data or {}).values():
            if face.get("prog") == prog and face.get("_tpw"):
                return face["_tpw"]
        return None

    async def _sync_room(self, prog: str, link: dict[str, str]) -> None:
        issue = f"schedule_not_sent_{prog}"
        try:
            block = self._block(prog)
            if block is None:
                raise HomeAssistantError(f"the PLC does not publish a time program named {prog!r}")
            entity_id = link["entity_id"]
            answer = await self.hass.services.async_call(
                "schedule", "get_schedule", {"entity_id": entity_id}, blocking=True, return_response=True)
            week = week_from_helper(answer[entity_id], link["base_mode"])
            current = await self.coordinator.async_read_program(block["_prefix"])
            if current.week() != week:
                _LOGGER.info("Sending the schedule %s to the thermostat %s", entity_id, prog)
                await self.coordinator.async_write_program(block["_prefix"], current.with_week(week))
        except (ScheduleError, HomeAssistantError, KeyError) as err:
            _LOGGER.warning("The schedule of %s was not sent to the PLC: %s", prog, err)
            ir.async_create_issue(
                self.hass, DOMAIN, issue, is_fixable=False, severity=ir.IssueSeverity.WARNING,
                translation_key="schedule_not_sent",
                translation_placeholders={"room": prog, "schedule": link["entity_id"], "reason": str(err)})
        else:
            ir.async_delete_issue(self.hass, DOMAIN, issue)
