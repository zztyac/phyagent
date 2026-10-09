"""Optional Pinocchio IK and coal collision checks for a fixed-base G1-29 model."""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np

from g1_tabletop.drivers.unitree import JOINT_NAMES
from g1_tabletop.errors import ExecutionError
from g1_tabletop.geometry import as_pose, matrix


def verified_file(path: str, expected: str) -> Path:
    result = Path(path).expanduser().resolve(strict=True)
    if hashlib.sha256(result.read_bytes()).hexdigest() != expected:
        raise ExecutionError("MODEL_DIGEST", f"model/calibration asset changed: {result.name}")
    return result


class Kinematics:
    def __init__(self, config):
        import coal
        import pinocchio as pin

        self.pin, self.coal, self.config = pin, coal, config
        hardware = config.hardware
        urdf = verified_file(hardware.urdf, hardware.urdf_sha256)
        srdf = verified_file(hardware.srdf, hardware.srdf_sha256)
        self.model = pin.buildModelFromUrdf(str(urdf))
        if self.model.nq != 29 or self.model.nv != 29:
            raise ExecutionError(
                "MODEL_MISMATCH", "requires fixed-base G1 model with exactly 29 one-DOF joints"
            )
        self.q_indices, self.v_indices = [], []
        for name in JOINT_NAMES:
            if not self.model.existJointName(name):
                raise ExecutionError("MODEL_MISMATCH", f"missing G1 joint {name}")
            joint = self.model.joints[self.model.getJointId(name)]
            if joint.nq != 1 or joint.nv != 1:
                raise ExecutionError("MODEL_MISMATCH", f"unexpected joint type: {name}")
            self.q_indices.append(joint.idx_q)
            self.v_indices.append(joint.idx_v)
        self.wrists, self.frames, self.selected = {}, {}, {"left": "point", "right": "point"}
        for arm in ("left", "right"):
            name = hardware.wrist_frames[arm]
            if not self.model.existFrame(name):
                raise ExecutionError("MODEL_MISMATCH", f"missing wrist frame {name}")
            wrist_id = self.model.getFrameId(name)
            self.wrists[arm] = wrist_id
            wrist = self.model.frames[wrist_id]
            self.frames[arm] = {}
            for tool, offset in hardware.wrist_to_tcp[arm].items():
                transform = matrix(offset)
                placement = wrist.placement * pin.SE3(transform[:3, :3], transform[:3, 3])
                self.frames[arm][tool] = self.model.addFrame(
                    pin.Frame(
                        f"paos_{arm}_{tool}",
                        wrist.parentJoint,
                        wrist_id,
                        placement,
                        pin.FrameType.OP_FRAME,
                    )
                )
        self.data = self.model.createData()
        self.geometry = pin.buildGeomFromUrdf(
            self.model,
            str(urdf),
            pin.GeometryType.COLLISION,
            package_dirs=[str(Path(hardware.mesh_directory).resolve())],
        )
        if self.geometry.ngeoms == 0:
            raise ExecutionError("COLLISION_MODEL_EMPTY", "URDF must supply collision geometry")
        self.tip_ids = {}
        for arm in ("left", "right"):
            wrist_id = self.wrists[arm]
            wrist = self.model.frames[wrist_id]
            for index, (x, y, z, radius) in enumerate(hardware.wrist_envelopes[arm]):
                self.geometry.addGeometryObject(
                    pin.GeometryObject(
                        f"paos_{arm}_envelope_{index}",
                        wrist.parentJoint,
                        wrist_id,
                        wrist.placement * pin.SE3(np.eye(3), np.array([x, y, z])),
                        coal.Sphere(radius),
                    )
                )
            self.tip_ids[arm] = self.geometry.addGeometryObject(
                pin.GeometryObject(
                    f"paos_{arm}_tip",
                    wrist.parentJoint,
                    wrist_id,
                    pin.SE3.Identity(),
                    coal.Sphere(hardware.contact_tip_radius_m),
                )
            )
        for box in hardware.environment:
            transform = matrix(box.pose)
            self.geometry.addGeometryObject(
                pin.GeometryObject(
                    f"env:{box.name}",
                    0,
                    0,
                    pin.SE3(transform[:3, :3], transform[:3, 3]),
                    coal.Box(*box.size_m),
                )
            )
        self.geometry.addAllCollisionPairs()
        pin.removeCollisionPairs(self.model, self.geometry, str(srdf), False)
        self.geometry_data = self.geometry.createData()
        self.payload_ids = {}
        self.payload_inertias = {}
        for arm in ("left", "right"):
            self.select_tcp(arm, "point")

    def to_model(self, motor_q):
        motor_q = np.asarray(motor_q, dtype=float)
        if motor_q.shape != (29,) or not np.isfinite(motor_q).all():
            raise ExecutionError("MODEL_STATE", "expected 29 finite motor positions")
        result = np.zeros(29)
        result[self.q_indices] = motor_q
        return result

    def to_motors(self, model_q):
        return np.asarray(model_q)[self.q_indices].copy()

    def select_tcp(self, arm, tool):
        self.selected[arm] = tool
        frame = self.model.frames[self.frames[arm][tool]]
        placement = frame.placement.copy()
        placement.translation -= (
            placement.rotation[:, 2] * self.config.hardware.contact_tip_radius_m
        )
        self.geometry.geometryObjects[self.tip_ids[arm]].placement = placement

    def fk(self, motor_q, arm, *, wrist=False):
        self.pin.framesForwardKinematics(self.model, self.data, self.to_model(motor_q))
        frame_id = self.wrists[arm] if wrist else self.frames[arm][self.selected[arm]]
        transform = self.data.oMf[frame_id]
        result = np.eye(4)
        result[:3, :3], result[:3, 3] = transform.rotation, transform.translation
        return as_pose(result)

    def gravity(self, motor_q):
        q = self.to_model(motor_q)
        tau = self.pin.rnea(self.model, self.data, q, np.zeros(29), np.zeros(29))
        return np.asarray(tau)[self.v_indices][15:29].copy()

    def solve(self, motor_q, arm, desired):
        pin, model = self.pin, self.model
        q = self.to_model(motor_q)
        transform = matrix(desired)
        goal = pin.SE3(transform[:3, :3], transform[:3, 3])
        frame = self.frames[arm][self.selected[arm]]
        group = slice(15, 22) if arm == "left" else slice(22, 29)
        indices = np.array(self.v_indices[group])
        for _ in range(160):
            pin.framesForwardKinematics(model, self.data, q)
            error = pin.log6(self.data.oMf[frame].actInv(goal)).vector
            if (
                np.linalg.norm(error[:3]) <= self.config.limits.position_tolerance_m / 2
                and np.linalg.norm(error[3:]) <= self.config.limits.orientation_tolerance_rad / 2
            ):
                motors = self.to_motors(q)
                if np.any(motors[15:29] < self.config.hardware.joint_lower_rad) or np.any(
                    motors[15:29] > self.config.hardware.joint_upper_rad
                ):
                    raise ExecutionError("IK_JOINT_LIMIT", "solution violates commissioned limits")
                return motors
            jacobian = pin.computeFrameJacobian(
                model, self.data, q, frame, pin.ReferenceFrame.LOCAL
            )[:, indices]
            step = jacobian.T @ np.linalg.solve(jacobian @ jacobian.T + 1e-4 * np.eye(6), error)
            step *= min(1, 0.05 / max(np.max(np.abs(step)), 1e-12))
            velocity = np.zeros(29)
            velocity[indices] = step
            q = pin.integrate(model, q, velocity)
            if np.any(q < model.lowerPositionLimit) or np.any(q > model.upperPositionLimit):
                raise ExecutionError("IK_JOINT_LIMIT", "IK crossed URDF joint limits")
        raise ExecutionError("IK_NO_SOLUTION", "bounded IK did not converge")

    def attach_payload(self, arm, profile):
        if arm in self.payload_ids:
            return
        frame_id = self.frames[arm]["grasp"]
        frame = self.model.frames[frame_id]
        self.payload_inertias[arm] = self.model.inertias[frame.parentJoint].copy()
        inertia = self.pin.Inertia(
            profile.payload_mass_kg,
            frame.placement.translation,
            np.eye(3) * (0.4 * profile.payload_mass_kg * profile.payload_radius_m**2),
        )
        self.model.inertias[frame.parentJoint] = self.model.inertias[frame.parentJoint] + inertia
        identity = self.geometry.addGeometryObject(
            self.pin.GeometryObject(
                f"payload:{arm}",
                frame.parentJoint,
                frame_id,
                frame.placement,
                self.coal.Sphere(profile.payload_radius_m),
            )
        )
        self.payload_ids[arm] = identity
        for other in range(identity):
            other_object = self.geometry.geometryObjects[other]
            if other_object.parentJoint != frame.parentJoint:
                self.geometry.addCollisionPair(self.pin.CollisionPair(other, identity))
        self.geometry_data = self.geometry.createData()

    def detach_payload(self, arm):
        identity = self.payload_ids.pop(arm, None)
        if identity is None:
            return
        joint = self.geometry.geometryObjects[identity].parentJoint
        self.model.inertias[joint] = self.payload_inertias.pop(arm)
        self.geometry.removeGeometryObject(f"payload:{arm}")
        # Removing geometry shifts later indices, including the other arm's payload.
        for side in self.payload_ids:
            self.payload_ids[side] = self.geometry.getGeometryId(f"payload:{side}")
        for side in self.tip_ids:
            self.tip_ids[side] = self.geometry.getGeometryId(f"paos_{side}_tip")
        self.geometry_data = self.geometry.createData()

    def check_collision(self, motor_q, *, contact=None, lifting=None):
        pin = self.pin
        q = self.to_model(motor_q)
        pin.updateGeometryPlacements(self.model, self.data, self.geometry, self.geometry_data, q)
        pin.computeDistances(self.geometry, self.geometry_data)
        for pair, result in zip(
            self.geometry.collisionPairs, self.geometry_data.distanceResults, strict=True
        ):
            if result.min_distance > self.config.limits.collision_margin_m:
                continue
            names = {
                self.geometry.geometryObjects[pair.first].name,
                self.geometry.geometryObjects[pair.second].name,
            }
            if contact:
                arm, target, profile = contact
                if names == {f"paos_{arm}_tip", f"env:{profile.contact_surface}"}:
                    tip = matrix(self.fk(motor_q, arm))[:3, 3]
                    local = np.linalg.inv(matrix(target)) @ np.r_[tip, 1]
                    radius = self.config.hardware.contact_tip_radius_m
                    if (
                        all(abs(local[i]) + radius <= profile.half_extent_m[i] for i in (0, 1))
                        and -profile.precontact_m
                        <= local[2]
                        <= profile.travel_m + self.config.limits.position_tolerance_m
                    ):
                        continue
            if lifting:
                arm, origin, profile = lifting
                if names == {f"payload:{arm}", f"env:{profile.support_surface}"}:
                    tcp = self.fk(motor_q, arm)
                    delta = np.array(tcp.position) - origin.position
                    if (
                        np.linalg.norm(delta[:2]) <= self.config.limits.position_tolerance_m
                        and delta[2] >= -self.config.limits.position_tolerance_m
                    ):
                        continue
            raise ExecutionError(
                "COLLISION", "collision/clearance violation: " + ", ".join(sorted(names))
            )
