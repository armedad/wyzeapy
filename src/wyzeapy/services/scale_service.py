#  Copyright (c) 2021. Mulliken, LLC - All Rights Reserved
#  You may use, distribute and modify this code under the terms
#  of the attached license. You should have received a copy of
#  the license with this file. If not, please write to:
#  katie@mulliken.net to receive a copy
"""Wyze Scale cloud service (classic JA.SC* and pluto WL_SC*)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List

from .base_service import BaseService
from ..types import Device, DeviceTypes

_LOGGER = logging.getLogger(__name__)

SCALE_CLASSIC_MODELS = {"JA.SC", "JA.SC2"}
SCALE_PLUTO_MODELS = {"WL_SC2", "WL_SC3", "WL_SCU", "WL_SCLET"}
SCALE_MODELS = SCALE_CLASSIC_MODELS | SCALE_PLUTO_MODELS

SCALE_SERVICE_BASE = "https://wyze-scale-service.wyzecam.com"
PLUTO_SERVICE_BASE = "https://wyze-pluto-service.wyzecam.com"


def _parse_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    # Wyze uses -1 as a sentinel for "not measured"
    if parsed < 0:
        return None
    return parsed


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class ScaleRecord:
    """A single weight / body-composition measurement from the Wyze cloud."""

    def __init__(self, dictionary: Dict[Any, Any] | None = None) -> None:
        data = dictionary or {}
        self.data_id: str | None = (
            str(data["data_id"]) if data.get("data_id") is not None else None
        )
        self.measure_ts: int | None = _parse_int(data.get("measure_ts"))
        # API stores weight in kilograms
        self.weight_kg: float | None = _parse_float(data.get("weight"))
        self.bmi: float | None = _parse_float(data.get("bmi"))
        self.body_fat: float | None = _parse_float(data.get("body_fat"))
        self.muscle: float | None = _parse_float(data.get("muscle"))
        self.body_water: float | None = _parse_float(data.get("body_water"))
        self.bone_mineral: float | None = _parse_float(data.get("bone_mineral"))
        self.protein: float | None = _parse_float(data.get("protein"))
        # Visceral fat rating (dimensionless index)
        self.body_vfr: float | None = _parse_float(data.get("body_vfr"))
        self.bmr: float | None = _parse_float(data.get("bmr"))
        self.heart_rate: int | None = _parse_int(data.get("heart_rate"))
        self.metabolic_age: int | None = _parse_int(data.get("metabolic_age"))
        family_member_id = data.get("family_member_id")
        self.family_member_id: str | None = (
            str(family_member_id) if family_member_id is not None else None
        )
        user_id = data.get("user_id")
        self.user_id: str | None = str(user_id) if user_id is not None else None
        self.mac: str | None = data.get("mac")


class ScaleFamilyMember:
    """A user profile associated with a Wyze scale."""

    def __init__(self, dictionary: Dict[Any, Any]) -> None:
        member_id = dictionary.get("id") or dictionary.get("family_member_id")
        self.id: str = str(member_id) if member_id is not None else ""
        self.nickname: str = str(dictionary.get("nickname") or "Unknown")
        self.height: float | None = _parse_float(dictionary.get("height"))
        self.goal_weight: float | None = _parse_float(dictionary.get("goal_weight"))


class Scale(Device):
    """Wyze Scale device with the latest cloud measurement."""

    def __init__(self, dictionary: Dict[Any, Any]):
        super().__init__(dictionary)
        self.available: bool = True
        self.unit: str | None = None
        self.firmware_ver: str | None = dictionary.get("firmware_ver")
        self.family_members: list[ScaleFamilyMember] = []
        self.latest_record: ScaleRecord | None = None


class ScaleService(BaseService):
    """Service for listing Wyze scales and fetching measurement records."""

    @staticmethod
    def _is_pluto(scale: Scale) -> bool:
        return scale.product_model in SCALE_PLUTO_MODELS

    @classmethod
    def _plugin_base(cls, scale: Scale) -> tuple[str, str]:
        if cls._is_pluto(scale):
            return PLUTO_SERVICE_BASE, "pluto"
        return SCALE_SERVICE_BASE, "scale"

    async def get_scales(self) -> List[Scale]:
        """Return all Wyze scales on the account."""
        if self._devices is None:
            self._devices = await self.get_object_list()

        scales = [
            device
            for device in self._devices
            if device.type is DeviceTypes.SCALE
            or device.product_model in SCALE_MODELS
        ]
        return [Scale(scale.raw_dict) for scale in scales]

    async def update(self, scale: Scale) -> Scale:
        """Refresh family members and the latest measurement for a scale."""
        try:
            scale.family_members = await self.get_family_members(scale)
        except Exception:
            _LOGGER.debug(
                "Unable to fetch family members for %s", scale.nickname, exc_info=True
            )

        try:
            scale.latest_record = await self.get_latest_record(scale)
            scale.available = True
        except Exception:
            _LOGGER.warning(
                "Unable to fetch latest record for %s", scale.nickname, exc_info=True
            )
            scale.available = False

        return scale

    async def get_family_members(self, scale: Scale) -> list[ScaleFamilyMember]:
        """Fetch family member profiles linked to the scale."""
        base, plugin = self._plugin_base(scale)
        response = await self._olive_get(
            f"{base}/plugin/{plugin}/get_family_member",
            device_id=scale.mac,
        )
        data = response.get("data")
        if not data:
            return []
        if isinstance(data, dict):
            data = data.get("family_member_list") or data.get("list") or []
        if not isinstance(data, list):
            return []
        return [ScaleFamilyMember(member) for member in data if isinstance(member, dict)]

    async def get_latest_record(
        self, scale: Scale, family_member_id: str | None = None
    ) -> ScaleRecord | None:
        """Fetch the most recent measurement for the account or a family member."""
        base, plugin = self._plugin_base(scale)
        params: Dict[str, Any] = {}
        if family_member_id:
            params["family_member_id"] = family_member_id

        response = await self._olive_get(
            f"{base}/plugin/{plugin}/get_latest_record",
            **params,
        )
        return self._first_record(response.get("data"))

    async def get_records(
        self,
        scale: Scale,
        start_time: datetime,
        end_time: datetime | None = None,
        family_member_id: str | None = None,
    ) -> list[ScaleRecord]:
        """Fetch measurement history for a time range (timestamps in ms)."""
        if end_time is None:
            end_time = datetime.now()

        base, plugin = self._plugin_base(scale)
        params: Dict[str, Any] = {
            "start_time": int(start_time.timestamp() * 1000),
            "end_time": int(end_time.timestamp() * 1000),
        }
        if family_member_id:
            params["family_member_id"] = family_member_id
        if self._is_pluto(scale):
            params["forward"] = 0

        response = await self._olive_get(
            f"{base}/plugin/{plugin}/get_record_range",
            **params,
        )
        data = response.get("data")
        if not data:
            return []
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list):
            return []
        return [ScaleRecord(item) for item in data if isinstance(item, dict)]

    @staticmethod
    def _first_record(data: Any) -> ScaleRecord | None:
        if data is None:
            return None
        if isinstance(data, list):
            if not data:
                return None
            first = data[0]
            return ScaleRecord(first) if isinstance(first, dict) else None
        if isinstance(data, dict):
            return ScaleRecord(data)
        return None
