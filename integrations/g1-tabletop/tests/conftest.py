"""Synthetic fixtures: deliberately not commissioning values for any physical robot."""

import hashlib

import pytest

from g1_tabletop.config import RuntimeConfig, mock_config
from g1_tabletop.drivers.unitree import JOINT_NAMES


@pytest.fixture
def hardware_config(tmp_path):
    links = ['<link name="base"/>']
    joints = []
    for index, name in enumerate(JOINT_NAMES):
        link = name.removesuffix("_joint") + "_link"
        links.append(f'''<link name="{link}">
          <inertial><mass value="0.1"/><inertia ixx="0.001" ixy="0" ixz="0" iyy="0.001" iyz="0" izz="0.001"/></inertial>
          <collision><geometry><sphere radius="0.008"/></geometry></collision></link>''')
        if index < 15:
            parent, xyz, axis = "base", f"0 0 {-1 - index * 0.1}", "0 1 0"
        else:
            local = (index - 15) % 7
            parent = (
                "base" if local == 0 else JOINT_NAMES[index - 1].removesuffix("_joint") + "_link"
            )
            xyz = f"0.2 {0.22 if index < 22 else -0.22} 0.4" if local == 0 else "0.055 0 0"
            axis = ["0 1 0", "1 0 0", "0 0 1", "0 1 0", "1 0 0", "0 1 0", "0 0 1"][local]
        joints.append(f'''<joint name="{name}" type="revolute"><parent link="{parent}"/>
          <child link="{link}"/><origin xyz="{xyz}"/><axis xyz="{axis}"/>
          <limit lower="-2.8" upper="2.8" effort="20" velocity="1"/></joint>''')
    urdf = tmp_path / "synthetic.urdf"
    urdf.write_text('<robot name="synthetic_g1">' + "".join(links + joints) + "</robot>")
    srdf = tmp_path / "synthetic.srdf"
    srdf.write_text('<robot name="synthetic_g1"/>')
    data = mock_config().model_dump(mode="json")
    data.update(mode="real", commissioned=True)
    data["limits"]["workspace"] = {"lower": [-2, -2, -3], "upper": [2, 2, 2]}
    data["buttons"]["button_01"]["press_axis_panel"] = [0, 0, 1]
    data["buttons"]["button_01"]["half_extent_m"] = [0.02, 0.02]
    data["cameras"] = {
        name: {"serial": name, "parent": parent, "parent_to_camera": {}}
        for name, parent in (
            ("head", "base"),
            ("left_wrist", "left_wrist"),
            ("right_wrist", "right_wrist"),
        )
    }
    data["hardware"] = {
        "interface": "synthetic-do-not-connect",
        "firmware_id": "synthetic",
        "brainco_model": "revo2",
        "urdf": str(urdf),
        "urdf_sha256": hashlib.sha256(urdf.read_bytes()).hexdigest(),
        "srdf": str(srdf),
        "srdf_sha256": hashlib.sha256(srdf.read_bytes()).hexdigest(),
        "mesh_directory": str(tmp_path),
        "wrist_frames": {arm: f"{arm}_wrist_yaw_link" for arm in ("left", "right")},
        "wrist_to_tcp": {
            arm: {kind: {"position": [0.07, 0, 0.015]} for kind in ("point", "grasp")}
            for arm in ("left", "right")
        },
        "reference_body_q": [0] * 15,
        "reference_imu_wxyz": [1, 0, 0, 0],
        "kp": [10] * 14,
        "kd": [1] * 14,
        "torque_limit_nm": [20] * 14,
        "joint_lower_rad": [-2.8] * 14,
        "joint_upper_rad": [2.8] * 14,
        "hand_current_limit_a": 0.5,
        "feedback_url": "http://127.0.0.1:19003",
        "feedback_source": "test-harness",
        "environment": [
            {"name": "console", "pose": {"position": [5, 0, 0]}, "size_m": [0.1, 0.1, 0.1]},
            {"name": "table", "pose": {"position": [0, 0, -5]}, "size_m": [1, 1, 0.1]},
        ],
        "wrist_envelopes": {arm: [[0.025, 0, 0, 0.009]] for arm in ("left", "right")},
        "contact_tip_radius_m": 0.004,
    }
    return RuntimeConfig.model_validate(data)
