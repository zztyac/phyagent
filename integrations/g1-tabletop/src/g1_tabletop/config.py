"""Validated site configuration. Example geometry is never accepted by the real backend."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Positive = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Unit = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Arm = Literal["left", "right"]
Vector3 = tuple[float, float, float]


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", frozen=True, allow_inf_nan=False, validate_default=True
    )


class Pose(StrictModel):
    position: Vector3 = (0, 0, 0)
    quaternion_xyzw: tuple[float, float, float, float] = (0, 0, 0, 1)

    @model_validator(mode="after")
    def unit_quaternion(self):
        if abs(sum(x * x for x in self.quaternion_xyzw) - 1) > 1e-6:
            raise ValueError("quaternion_xyzw must have unit norm")
        return self


class Bounds(StrictModel):
    lower: Vector3
    upper: Vector3

    @model_validator(mode="after")
    def ordered(self):
        if any(lo >= hi for lo, hi in zip(self.lower, self.upper, strict=True)):
            raise ValueError("workspace lower bounds must be smaller than upper bounds")
        return self


class Limits(StrictModel):
    workspace: Bounds
    observation_ttl_s: Positive = 2.0
    action_timeout_s: Positive = 90
    cleanup_timeout_s: Positive = 30
    state_max_age_s: Positive = 0.1
    feedback_max_age_s: Positive = 0.5
    max_translation_m: Positive = 0.8
    max_speed_m_s: Positive = 0.08
    joint_speed_rad_s: Positive = 0.4
    joint_acceleration_rad_s2: Positive = 0.8
    joint_step_rad: Positive = 0.008
    joint_tracking_rad: Positive = 0.12
    position_tolerance_m: Positive = 0.003
    orientation_tolerance_rad: Positive = 0.04
    max_local_correction_m: Positive = 0.015
    max_local_rotation_rad: Positive = 0.1
    body_drift_rad: Positive = 0.03
    collision_margin_m: Positive = 0.005


class HandPosture(StrictModel):
    q: tuple[Unit, Unit, Unit, Unit, Unit, Unit]
    speed: Annotated[float, Field(gt=0, le=1)]
    tolerance: Annotated[float, Field(gt=0, le=0.05)] = 0.01


class Marker(StrictModel):
    marker_id: Annotated[int, Field(ge=0, lt=50)]
    size_m: Positive
    marker_to_target: Pose = Field(default_factory=Pose)


class Button(StrictModel):
    label: str = Field(min_length=1)
    pose_in_panel: Pose
    press_axis_panel: Vector3
    precontact_m: Positive
    travel_m: Positive
    speed_m_s: Positive
    dwell_s: Annotated[float, Field(ge=0, le=2)] = 0.15
    settle_s: Positive = 0.5
    pointing_posture: str = "point"
    feedback_key: str = Field(min_length=1)
    contact_surface: str = "console"
    half_extent_m: tuple[Positive, Positive] = (0.008, 0.008)

    @model_validator(mode="after")
    def direction(self):
        if abs(math.sqrt(sum(v * v for v in self.press_axis_panel)) - 1) > 1e-6:
            raise ValueError("press_axis_panel must have unit norm")
        if self.travel_m > 0.03 or self.precontact_m > 0.2:
            raise ValueError("button travel/precontact exceeds supported bounded pressing range")
        return self


class ObjectProfile(StrictModel):
    label: str = Field(min_length=1)
    marker: Marker
    mock_pose: Pose
    approach_axis: Vector3 = (0, 0, -1)
    pregrasp_m: Positive = 0.05
    lift_m: Annotated[float, Field(gt=0, le=0.2)] = 0.05
    hold_s: Positive = 0.3
    open_posture: str = "open"
    close_posture: str = "pinch"
    min_current_a: Annotated[float, Field(ge=0)] = 0.01
    payload_radius_m: Positive = 0.025
    payload_mass_kg: Annotated[float, Field(gt=0, le=0.5)] = 0.05
    support_surface: str = "table"

    @model_validator(mode="after")
    def direction(self):
        if abs(sum(v * v for v in self.approach_axis) - 1) > 1e-6:
            raise ValueError("approach_axis must have unit norm")
        return self


class Camera(StrictModel):
    serial: str = Field(min_length=1)
    parent: Literal["base", "left_wrist", "right_wrist"]
    parent_to_camera: Pose
    width: Annotated[int, Field(ge=320, le=1920)] = 640
    height: Annotated[int, Field(ge=240, le=1080)] = 480
    fps: Literal[15, 30] = 30
    max_timestamp_skew_s: Positive = 0.04
    reprojection_error_px: Positive = 2


class Placement(StrictModel):
    marker: Marker
    mock_pose: Pose
    preplace_m: Positive = 0.08
    support_surface: str = "table"


class CollisionBox(StrictModel):
    name: str = Field(min_length=1)
    pose: Pose
    size_m: tuple[Positive, Positive, Positive]


class Hardware(StrictModel):
    interface: str = Field(min_length=1)
    domain_id: Annotated[int, Field(ge=0, le=232)] = 0
    arm_topic: Literal["rt/arm_sdk"] = "rt/arm_sdk"
    firmware_id: str = Field(min_length=1)
    brainco_model: Literal["revo2"]
    urdf: str = Field(min_length=1)
    urdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    mesh_directory: str = Field(min_length=1)
    srdf: str = Field(min_length=1)
    srdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    wrist_frames: dict[Arm, str]
    wrist_to_tcp: dict[Arm, dict[Literal["point", "grasp"], Pose]]
    reference_body_q: tuple[float, ...] = Field(min_length=15, max_length=15)
    reference_imu_wxyz: tuple[float, float, float, float]
    kp: tuple[Positive, ...] = Field(min_length=14, max_length=14)
    kd: tuple[Positive, ...] = Field(min_length=14, max_length=14)
    torque_limit_nm: tuple[Positive, ...] = Field(min_length=14, max_length=14)
    joint_lower_rad: tuple[float, ...] = Field(min_length=14, max_length=14)
    joint_upper_rad: tuple[float, ...] = Field(min_length=14, max_length=14)
    control_hz: Annotated[int, Field(ge=50, le=250)] = 250
    hand_current_limit_a: Positive
    # Read-only feedback service supplied by the test fixture/vehicle interface.
    feedback_url: str = Field(pattern=r"^https?://")
    feedback_source: str = Field(min_length=1)
    environment: tuple[CollisionBox, ...] = Field(min_length=1)
    # Collision spheres mounted in wrist frames cover hand and wrist camera envelopes.
    wrist_envelopes: dict[Arm, tuple[tuple[float, float, float, Positive], ...]]
    contact_tip_radius_m: Positive

    @model_validator(mode="after")
    def arms_complete(self):
        if abs(sum(v * v for v in self.reference_imu_wxyz) - 1) > 1e-6:
            raise ValueError("reference_imu_wxyz must have unit norm")
        if self.feedback_source == "synthetic-fixture":
            raise ValueError("synthetic feedback cannot commission a real runtime")
        for value in (self.wrist_frames, self.wrist_to_tcp, self.wrist_envelopes):
            if set(value) != {"left", "right"}:
                raise ValueError("both arms must be configured")
        if any(not spheres for spheres in self.wrist_envelopes.values()):
            raise ValueError("hand and camera collision envelopes must not be empty")
        if any(set(frames) != {"point", "grasp"} for frames in self.wrist_to_tcp.values()):
            raise ValueError("each wrist requires calibrated point and grasp TCP frames")
        if any(a >= b for a, b in zip(self.joint_lower_rad, self.joint_upper_rad, strict=True)):
            raise ValueError("invalid joint limits")
        return self


class RuntimeConfig(StrictModel):
    schema_version: Literal[1] = 1
    mode: Literal["mock", "real"] = "mock"
    calibration_id: str = Field(min_length=1)
    commissioned: bool = False
    limits: Limits
    panel_marker: Marker
    mock_panel_pose: Pose = Field(default_factory=Pose)
    buttons: dict[str, Button]
    objects: dict[str, ObjectProfile]
    placements: dict[str, Placement] = Field(default_factory=dict)
    postures: dict[str, HandPosture]
    maintenance_open_posture: str = "open"
    cameras: dict[str, Camera] = Field(default_factory=dict)
    hardware: Hardware | None = None

    @model_validator(mode="after")
    def references(self):
        if self.maintenance_open_posture not in self.postures:
            raise ValueError("maintenance_open_posture must name a calibrated open-hand posture")
        if not self.buttons:
            raise ValueError("at least one button is required")
        ids = [
            self.panel_marker.marker_id,
            *(o.marker.marker_id for o in self.objects.values()),
            *(p.marker.marker_id for p in self.placements.values()),
        ]
        if len(set(ids)) != len(ids):
            raise ValueError("marker IDs must be unique")
        for button in self.buttons.values():
            if button.pointing_posture not in self.postures:
                raise ValueError("undefined button posture")
            if button.speed_m_s > self.limits.max_speed_m_s:
                raise ValueError("button speed exceeds runtime limit")
        for obj in self.objects.values():
            if {obj.open_posture, obj.close_posture} - self.postures.keys():
                raise ValueError("undefined grasp posture")
        if self.mode == "real":
            if not self.commissioned or self.hardware is None:
                raise ValueError(
                    "real mode requires commissioned hardware/calibration configuration"
                )
            if set(self.cameras) != {"head", "left_wrist", "right_wrist"}:
                raise ValueError("real mode requires head and both wrist cameras")
            if len({c.serial for c in self.cameras.values()}) != 3:
                raise ValueError("camera serials must be distinct")
            for name, parent in (
                ("head", "base"),
                ("left_wrist", "left_wrist"),
                ("right_wrist", "right_wrist"),
            ):
                if self.cameras[name].parent != parent:
                    raise ValueError(f"incorrect camera parent for {name}")
            surfaces = {box.name for box in self.hardware.environment}
            if any(obj.support_surface not in surfaces for obj in self.objects.values()):
                raise ValueError("object support_surface must name a configured collision box")
            if any(place.support_surface not in surfaces for place in self.placements.values()):
                raise ValueError("placement support_surface must name a configured collision box")
            for button in self.buttons.values():
                if button.contact_surface not in surfaces:
                    raise ValueError("button contact_surface must name a configured collision box")
                if (
                    min(button.half_extent_m)
                    <= self.hardware.contact_tip_radius_m + self.limits.position_tolerance_m
                ):
                    raise ValueError(
                        "button usable area does not contain tip plus positioning tolerance"
                    )
                from g1_tabletop.geometry import matrix

                axis = matrix(button.pose_in_panel)[:3, 2]
                if sum(a * b for a, b in zip(axis, button.press_axis_panel, strict=True)) < 0.999:
                    raise ValueError("point TCP +Z must align with the calibrated press axis")
        return self

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def load_config(path: str | Path) -> RuntimeConfig:
    return RuntimeConfig.model_validate_json(Path(path).read_text(encoding="utf-8"))


def mock_config() -> RuntimeConfig:
    """Synthetic fixture geometry, not measurements of the user's console or robot."""
    return RuntimeConfig.model_validate(
        {
            "calibration_id": "synthetic-v1",
            "mode": "mock",
            "limits": {"workspace": {"lower": [0.1, -0.5, 0.05], "upper": [0.8, 0.5, 0.9]}},
            "panel_marker": {"marker_id": 0, "size_m": 0.04},
            "mock_panel_pose": {"position": [0.4, 0, 0.4]},
            "buttons": {
                "button_01": {
                    "label": "Synthetic button 1",
                    "pose_in_panel": {},
                    "press_axis_panel": [1, 0, 0],
                    "precontact_m": 0.03,
                    "travel_m": 0.003,
                    "speed_m_s": 0.01,
                    "dwell_s": 0,
                    "settle_s": 0.01,
                    "feedback_key": "button_01",
                }
            },
            "objects": {
                "block_01": {
                    "label": "Synthetic block",
                    "marker": {"marker_id": 1, "size_m": 0.025},
                    "mock_pose": {"position": [0.4, 0.12, 0.25]},
                    "hold_s": 0.01,
                }
            },
            "postures": {
                "point": {"q": [0.5, 0.5, 0, 1, 1, 1], "speed": 0.2},
                "open": {"q": [0, 0, 0, 0, 0, 0], "speed": 0.2},
                "pinch": {"q": [0.5, 0.5, 0.5, 0, 0, 0], "speed": 0.2},
            },
            "placements": {
                "zone_01": {
                    "marker": {"marker_id": 2, "size_m": 0.04},
                    "mock_pose": {"position": [0.45, 0.15, 0.25]},
                }
            },
        }
    )


def export_schema(path: Path) -> None:
    path.write_text(json.dumps(RuntimeConfig.model_json_schema(), indent=2), encoding="utf-8")
