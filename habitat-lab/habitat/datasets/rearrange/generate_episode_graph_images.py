import os
import json
import numpy as np
from scipy.spatial.transform import Rotation as R
from typing import List, Tuple
import habitat
import random
from habitat.config.default import get_config
from habitat.config import read_write
from habitat.core.env import Env
from habitat.tasks.rearrange.rearrange_sim import RearrangeSim
from habitat.datasets.rearrange.rearrange_dataset import (
    RearrangeDatasetV0,
    RearrangeEpisode
)
from habitat.datasets.rearrange.navmesh_utils import (
    get_largest_island_index,
)
from habitat.utils.visualizations.utils import observations_to_image


def save_image(image, file_path):
    from PIL import Image
    img = Image.fromarray(image)
    img.save(file_path)
def inrange(s,min_range,max_range):
    return min_range <s <max_range
def get_hssd_single_agent_config(cfg_path,overrides=None):
    if overrides:
        config = get_config(cfg_path,overrides)
        
    else:
        config = get_config(cfg_path)
    # print("config:",config)
    # with read_write(config):
        # config.habitat.task.lab_sensors = {}
        # agent_config = get_agent_config(config.habitat.simulator)
        # agent_config.sim_sensors = {
        #     "rgb_sensor": HabitatSimRGBSensorConfig(),
        #     "depth_sensor": HabitatSimDepthSensorConfig(),
        # }
    return config

# Generate a random quaternion (x, y, z, w) restricted to the x-z plane
def random_quaternion_xz_plane():
    # Restrict the orientation to the x-z plane (no y component)
    theta = np.random.uniform(0, 2 * np.pi)  # random angle in the x-z plane
    qx = 0
    qy = np.sin(theta / 2)  # No rotation around the y-axis
    qz = 0
    qw = np.cos(theta / 2)
    # qz = np.cos(theta / 2)
    # qw = 0  # Unit quaternion constraint
    
    return [qx, qy, qz, qw]

def calculate_orientation_xz_plane(position, goal_position):
    """
    Calculate the orientation of the agent facing towards the goal position.
    """
    dx = goal_position[0] - position[0]
    dz = goal_position[2] - position[2]
    
    # Calculate the angle between the x-axis and the vector from the agent's position to the goal position
    theta = np.arctan2(dz, dx)
    
    # Convert the angle to a quaternion
    qx = 0
    qy = np.sin(theta / 2)  # No rotation around the y-axis
    qz = 0
    qw = np.cos(theta / 2)
    # qz = np.cos(theta / 2)
    # qw = 0  # Unit quaternion constraint
    
    return [qx, qy, qz, qw]

def set_articulated_agent_base_state(sim: RearrangeSim, base_pos, base_rot, agent_id=0):
    """
    Set the base position and rotation of the articulated agent.
    
    Args:
        sim: RearrangeSim object
        base_pos: np.ndarray of shape (3,) representing the base position of the agent
        base_rot: Optional[(4,), (1)] representing the base rotation of the agent, can be quaternion or rotation_y_rad
    """
    agent = sim.agents_mgr[agent_id].articulated_agent
    agent.base_pos = base_pos
    if len(base_rot) == 1:
        agent.base_rot = base_rot
    elif len(base_rot) == 4:
        # convert quaternion to rotation_y_rad with scipy 
        r = R.from_quat(base_rot)
        agent.base_rot = r.as_euler('xyz', degrees=False)[1]
    else:
        raise ValueError("base_rot should be either quaternion or rotation_y_rad")

def get_target_objects_info(sim: RearrangeSim) -> Tuple[List[int], List[np.ndarray], List[np.ndarray]]:
    """
    Get the target objects' information: object ids, start positions, and goal positions from the rearrange episode.
    """
    target_idxs = []
    target_handles = []
    start_positions = []
    goal_positions = []
    rom = sim.get_rigid_object_manager()

    for target_handle, trans in sim._targets.items():
        target_idx = sim._scene_obj_ids.index(rom.get_object_by_handle(target_handle).object_id)
        obj_id = rom.get_object_by_handle(target_handle).object_id
        start_pos = sim.get_scene_pos()[sim._scene_obj_ids.index(obj_id)]
        goal_pos = np.array(trans.translation)

        target_idxs.append(target_idx)
        target_handles.append(target_handle)
        start_positions.append(start_pos)
        goal_positions.append(goal_pos)

    return target_idxs, target_handles, start_positions, goal_positions

def generate_scene_graph(
    dataset_path,
    config_path="benchmark/single_agent/zxz_fetch.yaml",
    gpu_id = None,
    bbox_min= 2500,
    bbox_max = 5000,
    dist_to_target=6.0,
    max_trials = 60,
    max_images= 10,
    random_min_bbox = 400,
    min_dis = 3,
    output_dir = 'data/sparse_slam/rearrange/hssd'
    ):
    override = []
    if gpu_id:
        override.append(f"++habitat.simulator.habitat_sim_v0.gpu_device_id={gpu_id}")
        override.append(f"++habitat.simulator.habitat_sim_v0.gpu_gpu=True")
    override.append(f"++habitat.simulator.dataset.data_path={dataset_path}")
    config = get_hssd_single_agent_config(config_path,override)
    print("config:",config)
    # config['habitat']['simulator']['habitat_sim_v0']['gpu_device_id'] = gpu_id
    # config['habitat']['simulator']['dataset']['data_path'] = dataset_path
    env = Env(config=config)
    dataset = env._dataset
    metadata = []
    for episode in dataset.episodes:
        # reset automatically calls next episode
        print("episode_info:",episode)
        env.reset()
        sim: RearrangeSim = env.sim
        # get the largest island index
        largest_island_idx = get_largest_island_index(
            sim.pathfinder, sim, allow_outdoor=True
        )
        graph_positions = []
        graph_orientations = []
        graph_annotations = []
        # Get scene objects ids
        scene_obj_ids = sim.scene_obj_ids
        
        # Get target objects' information
        object_ids, object_handles, start_positions, goal_positions = get_target_objects_info(sim)
        # Sample navigable points near the target objects start positions and goal positions
        # And calculate the orientation of the agent facing towards the position 
        bbox_range_min = bbox_min
        bbox_range_max = bbox_max
        for idx, (start_pos, goal_pos) in enumerate(zip(start_positions, goal_positions)):
            # Sample navigable point near the start and goal positions
            n_trial = 0
            radius = dist_to_target
            while n_trial < max_trials: #find start point      
                start_navigable_point = sim.pathfinder.get_random_navigable_point_near(
                    circle_center=start_pos, radius=radius, island_index=largest_island_idx
                )
                start_orientation = calculate_orientation_xz_plane(start_navigable_point, start_pos)
                set_articulated_agent_base_state(sim, start_navigable_point, start_orientation, agent_id=0)
                observations = env.step({"action": (), "action_args": {}})
                env._episode_over = False
                _,_,w,h = observations["rec_bounding_box"][0]
                if not np.isnan(start_navigable_point[0]) and inrange(w*h,bbox_range_min,bbox_range_max):
                    break
                # else increase the radius limit and try again
                else:
                    radius += 0.1
                    n_trial += 1
            # print("trail_time_rec:",n_trial)
            n_trial = 0
            radius = dist_to_target
            while n_trial < max_trials: #find goal point      
                goal_navigable_point = sim.pathfinder.get_random_navigable_point_near(
                    circle_center=goal_pos, radius=radius, island_index=largest_island_idx
                )
                goal_orientation = calculate_orientation_xz_plane(goal_navigable_point, goal_pos)
                set_articulated_agent_base_state(sim, goal_navigable_point, goal_orientation, agent_id=0)
                observations = env.step({"action": (), "action_args": {}})
                env._episode_over = False
                _,_,w,h = observations["target_bounding_box"][0]
                if not np.isnan(start_navigable_point[0]) and inrange(w*h,bbox_range_min,bbox_range_max):
                    break
                # else increase the radius limit and try again
                else:
                    radius += 0.1
                    n_trial += 1
            # print("trail_time_target:",n_trial)
            graph_positions.extend([start_navigable_point, goal_navigable_point])
            graph_orientations.extend([start_orientation, goal_orientation])
            graph_annotations.extend(["start_rec", "goal_rec"])
        # print("graph_positions:",graph_positions)
        # print("graph_orientations:",graph_orientations)

        # Sample navigable points if images are less than the maximum number of images
        if len(graph_positions) < max_images:
            i = 0
            min_bbox = random_min_bbox
            while(max_images>len(graph_positions)):
                # Sample a random navigable point in the scene
                point = sim.pathfinder.get_random_navigable_point(island_index=largest_island_idx)
                orientation = random_quaternion_xz_plane()
                min_dis_flag = 0
                min_point_dis = min_dis
                for item in graph_positions[:2]:
                    x1 = point[0]
                    y1 = point[2]
                    x2 = item[0]
                    y2 = item[2]
                    # print("dis:",int(np.sqrt(((x1-x2) ** 2)+((y1-y2) ** 2))))
                    if int(np.sqrt(((x1-x2) ** 2)+((y1-y2) ** 2))) < min_point_dis:
                        min_dis_flag = 1
                        break
                if min_dis_flag == 0:
                    set_articulated_agent_base_state(sim, point, orientation, agent_id=0)
                    observations = env.step({"action": (), "action_args": {}})
                    env._episode_over = False
                    _,_,w_tar,h_tar = observations["target_bounding_box"][0]
                    _,_,w_rec,h_rec = observations["rec_bounding_box"][0]
                    if w_tar*h_tar<min_bbox and w_rec*h_rec<min_bbox:
                        i+=1
                        graph_positions.append(point)
                        graph_orientations.append(orientation)
                        graph_annotations.append("random")
        # print("graph_positions:",graph_positions)
        # print("graph_orientations:",graph_orientations)
        # print("graph_annotations:",graph_annotations)  
        # Create a folder for this episode
        episode_dir = os.path.join(output_dir, f"episode_{episode.episode_id}")
        if not os.path.exists(episode_dir):
            os.makedirs(episode_dir)
        # print("graph_positions:",graph_positions)
        # print("graph_orientations:",graph_orientations)
        for idx, (position, orientation) in enumerate(zip(graph_positions, graph_orientations)):
            # observations = env.sim.get_observations_at(position=position, rotation=orientation)
            set_articulated_agent_base_state(sim, position, orientation, agent_id=0)
            observations = env.step({"action": (), "action_args": {}})
            
            # Force the episode to be active
            env._episode_over = False
            
            obs_file_list = []
            for key in observations.keys():
                print(key)
            for obs_key in obs_keys:
                if obs_key in observations:
                    image = observations[obs_key]
                    image_file_name = f"episode_{episode.episode_id}_{obs_key}_{idx}.png"
                    image_file_path = os.path.join(episode_dir, image_file_name)
                    save_image(image, image_file_path)
                    obs_file_list.append(image_file_name)
            # for bbox in args.bbox:
            #     if bbox in observations:
            #         print(f"{bbox}:{observations[bbox]}")
            metadata.append({
                "episode_id": episode.episode_id,
                "obs_files": obs_file_list,
                "position": position,
                "rotation": orientation,
                # "detected_objects": observations["objectgoal"],
            })
        
        env._episode_over = True
        
    # Save metadata to JSON
    metadata_file_path = os.path.join(output_dir, "metadata.json")
    class NumpyEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return super().default(obj)
    with open(metadata_file_path, "w") as f:
        json.dump(metadata, f, indent=4, cls=NumpyEncoder)
def main(args):
    override = []
    # if args.gpu_id and args.dataset_path:
    #     override.append(f"++habitat.simulator.habitat_sim_v0.gpu_device_id={args.gpu_id}")
    #     override.append(f"++habitat.simulator.dataset.data_path={args.dataset_path}")
    #     config = get_hssd_single_agent_config(args.config,override)
    # else:
    config = get_hssd_single_agent_config(args.config)
    env = Env(config=config)
    dataset = env._dataset

    metadata = []

    for episode in dataset.episodes:
        # reset automatically calls next episode
        # print("episode:",episode)
        env.reset()
        sim: RearrangeSim = env.sim
        # get the largest island index
        largest_island_idx = get_largest_island_index(
            sim.pathfinder, sim, allow_outdoor=True
        )
        graph_positions = []
        graph_orientations = []
        graph_annotations = []
        # Get scene objects ids
        scene_obj_ids = sim.scene_obj_ids
        
        # Get target objects' information
        object_ids, object_handles, start_positions, goal_positions = get_target_objects_info(sim)
        # Sample navigable points near the target objects start positions and goal positions
        # And calculate the orientation of the agent facing towards the position 
        bbox_range_min = args.bbox_range_min
        bbox_range_max = args.bbox_range_max
        # print("graph_positions:",graph_positions)
        # print("graph_orientations:",graph_orientations)
        for idx, (start_pos, goal_pos) in enumerate(zip(start_positions, goal_positions)):
            # Sample navigable point near the start and goal positions
            n_trial = 0
            radius = args.dist_to_target
            while n_trial < args.max_trials: #find start point      
                start_navigable_point = sim.pathfinder.get_random_navigable_point_near(
                    circle_center=start_pos, radius=radius, island_index=largest_island_idx
                )
                start_orientation = calculate_orientation_xz_plane(start_navigable_point, start_pos)
                set_articulated_agent_base_state(sim, start_navigable_point, start_orientation, agent_id=0)
                observations = env.step({"action": (), "action_args": {}})
                env._episode_over = False
                _,_,w,h = observations["rec_bounding_box"][0]
                if not np.isnan(start_navigable_point[0]) and inrange(w*h,bbox_range_min,bbox_range_max):
                    break
                # else increase the radius limit and try again
                else:
                    radius += 0.1
                    n_trial += 1
            print("trail_time_rec:",n_trial)
            n_trial = 0
            radius = args.dist_to_target
            while n_trial < args.max_trials: #find goal point      
                goal_navigable_point = sim.pathfinder.get_random_navigable_point_near(
                    circle_center=goal_pos, radius=radius, island_index=largest_island_idx
                )
                goal_orientation = calculate_orientation_xz_plane(goal_navigable_point, goal_pos)
                set_articulated_agent_base_state(sim, goal_navigable_point, goal_orientation, agent_id=0)
                observations = env.step({"action": (), "action_args": {}})
                env._episode_over = False
                _,_,w,h = observations["target_bounding_box"][0]
                if not np.isnan(start_navigable_point[0]) and inrange(w*h,bbox_range_min,bbox_range_max):
                    break
                # else increase the radius limit and try again
                else:
                    radius += 0.1
                    n_trial += 1
            print("trail_time_target:",n_trial)
            try:
                graph_positions.extend([start_navigable_point, goal_navigable_point])
                graph_orientations.extend([start_orientation, goal_orientation])
                graph_annotations.extend([{"obj_start_rec":episode.target_receptacles}, {"obj_goal_rec":episode.goal_receptacles}])
            except:
                continue
        # print("graph_positions:",graph_positions)
        # print("graph_orientations:",graph_orientations)

        # Sample navigable points if images are less than the maximum number of images
        if len(graph_positions) < args.max_images:
            i = 0
            min_bbox = args.random_min_bbox
            while(args.max_images>len(graph_positions)):
                # Sample a random navigable point in the scene
                point = sim.pathfinder.get_random_navigable_point(island_index=largest_island_idx)
                orientation = random_quaternion_xz_plane()
                min_dis_flag = 0
                min_point_dis = args.min_point_dis
                if len(graph_positions)!=0:
                    for item in graph_positions[:2]:
                        x1 = point[0]
                        y1 = point[2]
                        x2 = item[0]
                        y2 = item[2]
                        # print("dis:",int(np.sqrt(((x1-x2) ** 2)+((y1-y2) ** 2))))
                        if np.sqrt(((x1-x2) ** 2)+((y1-y2) ** 2)) < min_point_dis:
                            min_dis_flag = 1
                            break
                if min_dis_flag == 0:
                    set_articulated_agent_base_state(sim, point, orientation, agent_id=0)
                    observations = env.step({"action": (), "action_args": {}})
                    env._episode_over = False
                    _,_,w_tar,h_tar = observations["target_bounding_box"][0]
                    _,_,w_rec,h_rec = observations["rec_bounding_box"][0]
                    if w_tar*h_tar<min_bbox and w_rec*h_rec<min_bbox:
                        i+=1
                        graph_positions.append(point)
                        graph_orientations.append(orientation)
                        graph_annotations.append({"item":None})
        # print("graph_positions:",graph_positions)
        # print("graph_orientations:",graph_orientations)
        # print("graph_annotations:",graph_annotations)  
        # Create a folder for this episode
        episode_dir = os.path.join(args.output_dir, f"episode_{episode.episode_id}")
        if not os.path.exists(episode_dir):
            os.makedirs(episode_dir)
        # print("graph_positions:",graph_positions)
        # print("graph_orientations:",graph_orientations)
        for idx, (position, orientation, annotation) in enumerate(zip(graph_positions, graph_orientations,graph_annotations)):
            # observations = env.sim.get_observations_at(position=position, rotation=orientation)
            set_articulated_agent_base_state(sim, position, orientation, agent_id=0)
            observations = env.step({"action": (), "action_args": {}})
            
            # Force the episode to be active
            env._episode_over = False
            
            obs_file_list = []
            loc_sensor = observations['localization_sensor']
            for obs_key in args.obs_keys:
                if obs_key in observations:
                    image = observations[obs_key]
                    image_file_name = f"episode_{episode.episode_id}_{obs_key}_{idx}.png"
                    image_file_path = os.path.join(episode_dir, image_file_name)
                    save_image(image, image_file_path)
                    obs_file_list.append(image_file_name)
            # for bbox in args.bbox:
            #     if bbox in observations:
            #         print(f"{bbox}:{observations[bbox]}")
            metadata.append({
                "episode_id": episode.episode_id,
                "obs_files": obs_file_list,
                "localization_sensor":loc_sensor,
                # "position": position,
                # "rotation": orientation,
                "annotation": annotation,
                # "detected_objects": observations["objectgoal"],
            })
        
        env._episode_over = True
        
    # Save metadata to JSON
    metadata_file_path = os.path.join(args.output_dir, "metadata.json")
    class NumpyEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return super().default(obj)
    with open(metadata_file_path, "w") as f:
        json.dump(metadata, f, indent=4, cls=NumpyEncoder)
def parse_args():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=str, default="single_rearrange/zxz_llm_fetch.yaml")
    parser.add_argument("--output_dir", type=str, default="data/sparse_slam/rearrange/hssd")
    parser.add_argument("--obs_keys", nargs="+", default=["head_rgb"])
    parser.add_argument("--dist_to_target", type=float, default=6.0)
    parser.add_argument("--max_trials", type=int, default=60)
    parser.add_argument("--max_images", type=int, default=12)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--bbox_range_min", type=int, default=2700)
    parser.add_argument("--bbox_range_max", type=int, default=4500)
    parser.add_argument("--min_point_dis", type=float, default=3.2)
    parser.add_argument("--random_min_bbox", type=int, default=400)
    parser.add_argument("--gpu_id", type=int, default=4)
    parser.add_argument("--dataset_path", type=str, default="data/datasets/hssd_scene_new/104348463_171513588/data_0.json.gz")


    
    # parser.add_argument("--output_dir", type=str, default="data/sparse_slam/rearrange/mp3d")
    args = parser.parse_args()

    # Create output directory if it does not exist
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)
    
    # Set seed
    np.random.seed(args.seed)
    return args

if __name__ == "__main__":
    args = parse_args()
    
    main(args)