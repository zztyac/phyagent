"""Regenerate a runnable mock config, deliberately incomplete real draft, and JSON schema."""

import json
from pathlib import Path

from pydantic_core import PydanticUndefined

from g1_tabletop.config import Hardware, export_schema, mock_config

ROOT = Path(__file__).resolve().parents[1] / "config"


def generate():
    ROOT.mkdir(exist_ok=True)
    mock = mock_config()
    (ROOT / "mock.json").write_text(mock.model_dump_json(indent=2) + "\n", encoding="utf-8")
    draft = mock.model_dump(mode="json")
    draft.update(mode="real", commissioned=False, calibration_id="REPLACE_AFTER_CALIBRATION")
    pose = {"position": [None] * 3, "quaternion_xyzw": [None] * 4}
    draft["limits"]["workspace"] = {"lower": [None] * 3, "upper": [None] * 3}
    draft["panel_marker"].update(size_m=None, marker_to_target=pose)
    button = draft["buttons"]["button_01"]
    button.update(
        label="REPLACE_WITH_PHYSICAL_BUTTON_NAME",
        pose_in_panel=pose,
        press_axis_panel=[None] * 3,
        precontact_m=None,
        travel_m=None,
        half_extent_m=[None] * 2,
    )
    obj = draft["objects"]["block_01"]
    obj.update(
        label="REPLACE_WITH_OBJECT_NAME",
        min_current_a=None,
        payload_radius_m=None,
        payload_mass_kg=None,
    )
    obj["marker"].update(size_m=None, marker_to_target=pose)
    draft["placements"]["zone_01"]["marker"].update(size_m=None, marker_to_target=pose)
    for posture in draft["postures"].values():
        posture["q"] = [None] * 6
    draft["cameras"] = {
        name: {
            "serial": None,
            "parent": parent,
            "parent_to_camera": pose,
            "width": 640,
            "height": 480,
            "fps": 30,
            "max_timestamp_skew_s": 0.04,
            "reprojection_error_px": 2,
        }
        for name, parent in (
            ("head", "base"),
            ("left_wrist", "left_wrist"),
            ("right_wrist", "right_wrist"),
        )
    }
    hardware = {
        key: None if field.default is PydanticUndefined else field.default
        for key, field in Hardware.model_fields.items()
    }
    hardware.update(
        brainco_model="revo2",
        reference_body_q=[None] * 15,
        reference_imu_wxyz=[None] * 4,
        wrist_frames={side: None for side in ("left", "right")},
        wrist_to_tcp={
            side: {tool: pose for tool in ("point", "grasp")} for side in ("left", "right")
        },
        wrist_envelopes={side: [[None] * 4] for side in ("left", "right")},
        environment=[
            {"name": name, "pose": pose, "size_m": [None] * 3} for name in ("console", "table")
        ],
    )
    for key in ("kp", "kd", "torque_limit_nm", "joint_lower_rad", "joint_upper_rad"):
        hardware[key] = [None] * 14
    draft["hardware"] = hardware
    (ROOT / "real.site.template.json").write_text(
        json.dumps(draft, indent=2) + "\n", encoding="utf-8"
    )
    export_schema(ROOT / "runtime.schema.json")


if __name__ == "__main__":
    generate()
