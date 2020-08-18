import bisect
from functools import partial
from typing import Optional, Tuple, cast

import numpy as np
from torch.utils.data import Dataset

from ..data import ChunkedDataset
from ..kinematic import Perturbation
from ..rasterization import Rasterizer
from ..sampling import generate_agent_sample


class EgoDataset(Dataset):
    def __init__(
        self,
        cfg: dict,
        zarr_dataset: ChunkedDataset,
        rasterizer: Rasterizer,
        perturbation: Optional[Perturbation] = None,
    ):
        """
        Get a PyTorch dataset object that can be used to train DNN

        Args:
            cfg (dict): configuration file
            zarr_dataset (ChunkedDataset): the raw zarr dataset
            rasterizer (Rasterizer): an object that support rasterisation around an agent (AV or not)
            perturbation (Optional[Perturbation]): an object that takes care of applying trajectory perturbations.
None if not desired
        """
        self.perturbation = perturbation
        self.cfg = cfg
        self.dataset = zarr_dataset
        self.rasterizer = rasterizer

        self.cumulative_sizes = self.dataset.scenes["frame_index_interval"][:, 1]

        # build a partial so we don't have to access cfg each time
        self.sample_function = partial(
            generate_agent_sample,
            raster_size=cast(Tuple[int, int], tuple(cfg["raster_params"]["raster_size"])),
            pixel_size=np.array(cfg["raster_params"]["pixel_size"]),
            ego_center=np.array(cfg["raster_params"]["ego_center"]),
            history_num_frames=cfg["model_params"]["history_num_frames"],
            history_step_size=cfg["model_params"]["history_step_size"],
            future_num_frames=cfg["model_params"]["future_num_frames"],
            future_step_size=cfg["model_params"]["future_step_size"],
            filter_agents_threshold=cfg["raster_params"]["filter_agents_threshold"],
            rasterizer=rasterizer,
            perturbation=perturbation,
        )

    def __len__(self) -> int:
        """
        Get the number of available AV frames

        Returns:
            int: the number of elements in the dataset
        """
        return len(self.dataset.frames)

    def get_frame(self, scene_index: int, state_index: int, track_id: Optional[int] = None) -> dict:
        """
        A utility function to get the rasterisation and trajectory target for a given agent in a given frame

        Args:
            scene_index (int): the index of the scene in the zarr
            state_index (int): a relative frame index in the scene
            track_id (Optional[int]): the agent to rasterize or None for the AV
        Returns:
            dict: the rasterised image, the target trajectory (position and yaw) along with their availability,
            the 2D matrix to center that agent, the agent track (-1 if ego) and the timestamp

        """
        frame_interval = self.dataset.scenes[scene_index]["frame_index_interval"]
        frames = self.dataset.frames[frame_interval[0] : frame_interval[1]]
        data = self.sample_function(state_index, frames, self.dataset.agents, self.dataset.tl_faces, track_id)
        # 0,1,C -> C,0,1
        image = data["image"].transpose(2, 0, 1)

        target_positions = np.array(data["target_positions"], dtype=np.float32)
        target_yaws = np.array(data["target_yaws"], dtype=np.float32)

        history_positions = np.array(data["history_positions"], dtype=np.float32)
        history_yaws = np.array(data["history_yaws"], dtype=np.float32)

        if self.cfg.get("use_frenet", True):  # DO NOT SUBMIT, plumb a config setting for this first
            # Convert from ego-relative coordinates to geomap coordinates
            image_to_world = np.inverse(np.array(data["world_to_image"]))
            assert image_to_world.shape == (3,3)
            def world_from_image(image_coords):
                return np.matmul(image_to_world[0:2, 0:2], image_coords) + image_to_world[0:2, 2:3]

            target_positions_geomap = world_from_image(target_positions)

            # may have to also add Ego's initial yaw to target_yaws
            initial_ego_yaw = np.arctan2(image_to_world[0, 1], image_to_world[0,0])  # not 100% sure about taking first row
            target_yaws += initial_ego_yaw

            target_frenet_coordinates = [self.rasterizer.route_frenet_coordinates_from_xy_heading(xyh) for xyh in zip(target_positions_geomap, target_yaws)]
            target_frenet_positions = np.array([f[0] for f in target_frenet_coordinates]) # list of (s,d)'s
            target_relative_headings = np.array([f[1] for f in target_frenet_coordinates]) # list of headings (yaws)
            # Make the Frenet "along" coordinate relative to the initial ego position, rather than the origin of the whole route
            ego_in_frenet, _ = self.rasterizer.route_frenet_coordinates_from_xy_heading(world_from_image(np.array(data["ego_center"])), 0)  # (s0,d0)
            # Drop the "d0"/"across" coordinate here, to encourage the network to learn "0", i.e try to come back to the middle of the lane (path prior)
            # even when the ego is currently off, rather than try to maintain the ego's current offset, which we would get by using d0.
            target_frenet_positions -= np.array([[ego_in_frenet[0]], [0]])

            target_positions = target_frenet_positions
            target_yaws = target_relative_headings

        timestamp = self.dataset.frames[frame_interval[0] + state_index]["timestamp"]
        track_id = np.int64(-1 if track_id is None else track_id)  # always a number to avoid crashing torch

        return {
            "image": image,
            "target_positions": target_positions,
            "target_yaws": target_yaws,
            "target_availabilities": data["target_availabilities"],
            "history_positions": history_positions,
            "history_yaws": history_yaws,
            "history_availabilities": data["history_availabilities"],
            "world_to_image": data["world_to_image"],
            "track_id": track_id,
            "timestamp": timestamp,
            "centroid": data["centroid"],
            "yaw": data["yaw"],
            "extent": data["extent"],
        }

    def __getitem__(self, index: int) -> dict:
        """
        Function called by Torch to get an element

        Args:
            index (int): index of the element to retrieve

        Returns: please look get_frame signature and docstring

        """
        if index < 0:
            if -index > len(self):
                raise ValueError("absolute value of index should not exceed dataset length")
            index = len(self) + index

        scene_index = bisect.bisect_right(self.cumulative_sizes, index)

        if scene_index == 0:
            state_index = index
        else:
            state_index = index - self.cumulative_sizes[scene_index - 1]
        return self.get_frame(scene_index, state_index)

    def get_scene_dataset(self, scene_index: int) -> "EgoDataset":
        """
        Returns another EgoDataset dataset where the underlying data can be modified.
        This is possible because, even if it supports the same interface, this dataset is np.ndarray based.

        Args:
            scene_index (int): the scene index of the new dataset

        Returns:
            EgoDataset: A valid EgoDataset dataset with a copy of the data

        """
        # copy everything to avoid references (scene is already detached from zarr if get_combined_scene was called)
        scenes = self.dataset.scenes[scene_index : scene_index + 1].copy()
        frame_interval = scenes[0]["frame_index_interval"]
        frames = self.dataset.frames[frame_interval[0] : frame_interval[1]].copy()
        # ASSUMPTION: all agents_index are consecutive
        agents_start_index = frames[0]["agent_index_interval"][0]
        agents_end_index = frames[-1]["agent_index_interval"][1]
        agents = self.dataset.agents[agents_start_index:agents_end_index].copy()

        tl_start_index = frames[0]["traffic_light_faces_index_interval"][0]
        tl_end_index = frames[-1]["traffic_light_faces_index_interval"][1]
        tl_faces = self.dataset.tl_faces[tl_start_index:tl_end_index].copy()

        frames["agent_index_interval"] -= agents_start_index
        frames["traffic_light_faces_index_interval"] -= tl_start_index
        scenes["frame_index_interval"] -= frame_interval[0]

        dataset = ChunkedDataset("")
        dataset.agents = agents
        dataset.tl_faces = tl_faces
        dataset.frames = frames
        dataset.scenes = scenes

        return EgoDataset(self.cfg, dataset, self.rasterizer, self.perturbation)

    def get_scene_indices(self, scene_idx: int) -> np.ndarray:
        """
        Get indices for the given scene. EgoDataset iterates over frames, so this is just a matter
        of finding the scene boundaries.
        Args:
            scene_idx (int): index of the scene

        Returns:
            np.ndarray: indices that can be used for indexing with __getitem__
        """
        scenes = self.dataset.scenes
        assert scene_idx < len(scenes), f"scene_idx {scene_idx} is over len {len(scenes)}"
        return np.arange(*scenes[scene_idx]["frame_index_interval"])

    def get_frame_indices(self, frame_idx: int) -> np.ndarray:
        """
        Get indices for the given frame. EgoDataset iterates over frames, so this will be a single element
        Args:
            frame_idx (int): index of the scene

        Returns:
            np.ndarray: indices that can be used for indexing with __getitem__
        """
        frames = self.dataset.frames
        assert frame_idx < len(frames), f"frame_idx {frame_idx} is over len {len(frames)}"
        return np.asarray((frame_idx,), dtype=np.int64)

    def __str__(self) -> str:
        return self.dataset.__str__()
