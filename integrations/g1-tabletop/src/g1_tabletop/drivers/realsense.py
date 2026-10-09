"""Three explicitly selected RealSense cameras with exposure-time checks."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import numpy as np

from g1_tabletop.errors import ExecutionError


@dataclass(frozen=True)
class Frame:
    rgb: np.ndarray
    depth_m: np.ndarray
    camera_matrix: np.ndarray
    distortion: np.ndarray
    timestamp: float
    received_monotonic: float


class RealSenseCameras:
    def __init__(self, config):
        self.config = config
        self.pipelines, self.threads = {}, []
        self.frames: dict[str, Frame] = {}
        self.errors = {}
        self.lock = threading.Lock()
        self.exit = threading.Event()

    def connect(self):
        import pyrealsense2 as rs

        for name, camera in self.config.cameras.items():
            pipeline, settings = rs.pipeline(), rs.config()
            settings.enable_device(camera.serial)
            settings.enable_stream(
                rs.stream.color, camera.width, camera.height, rs.format.bgr8, camera.fps
            )
            settings.enable_stream(
                rs.stream.depth, camera.width, camera.height, rs.format.z16, camera.fps
            )
            profile = pipeline.start(settings)
            self.pipelines[name] = pipeline
            for sensor in profile.get_device().query_sensors():
                if sensor.supports(rs.option.global_time_enabled):
                    sensor.set_option(rs.option.global_time_enabled, 1)
            scale = profile.get_device().first_depth_sensor().get_depth_scale()
            worker = threading.Thread(
                target=self._loop, args=(name, pipeline, scale, rs), daemon=True
            )
            worker.start()
            self.threads.append(worker)

    def _loop(self, name, pipeline, scale, rs):
        align = rs.align(rs.stream.color)
        while not self.exit.is_set():
            try:
                frames = align.process(pipeline.wait_for_frames(1000))
                color, depth = frames.get_color_frame(), frames.get_depth_frame()
                if not color or not depth:
                    continue
                timestamp = color.get_timestamp() / 1000
                if color.get_frame_timestamp_domain() not in {
                    rs.timestamp_domain.system_time,
                    rs.timestamp_domain.global_time,
                }:
                    raise ExecutionError(
                        "CAMERA_CLOCK",
                        "enable RealSense global timestamps before moving wrist cameras",
                    )
                if abs(time.time() - timestamp) > 0.5:
                    raise ExecutionError(
                        "CAMERA_CLOCK", "camera/global timestamp differs from host clock"
                    )
                intrinsics = color.profile.as_video_stream_profile().get_intrinsics()
                if intrinsics.model not in {
                    rs.distortion.none,
                    rs.distortion.brown_conrady,
                    rs.distortion.modified_brown_conrady,
                }:
                    raise ExecutionError(
                        "CAMERA_DISTORTION", "unsupported OpenCV camera distortion model"
                    )
                camera_matrix = np.array(
                    [
                        [intrinsics.fx, 0, intrinsics.ppx],
                        [0, intrinsics.fy, intrinsics.ppy],
                        [0, 0, 1],
                    ]
                )
                frame = Frame(
                    np.asanyarray(color.get_data()).copy(),
                    np.asanyarray(depth.get_data()).astype(float) * scale,
                    camera_matrix,
                    np.array(intrinsics.coeffs),
                    timestamp,
                    time.monotonic(),
                )
                with self.lock:
                    self.frames[name] = frame
                    self.errors.pop(name, None)
            except Exception as exc:
                with self.lock:
                    self.errors[name] = str(exc)
                self.exit.wait(0.05)

    def frame(self, source):
        with self.lock:
            frame = self.frames.get(source)
            if (
                frame is None
                or time.monotonic() - frame.received_monotonic > 0.3
                or source in self.errors
            ):
                raise ExecutionError(
                    "CAMERA_STALE", f"{source}: {self.errors.get(source, 'no fresh image')}"
                )
            return frame

    def close(self):
        self.exit.set()
        for worker in self.threads:
            worker.join(timeout=2)
        for pipeline in self.pipelines.values():
            pipeline.stop()
