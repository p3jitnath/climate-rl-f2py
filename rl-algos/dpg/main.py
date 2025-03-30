import json
import os
import pickle
import random
import sys
import time
from dataclasses import dataclass
from typing import Optional

import fedrl_climate_envs
import gymnasium as gym
import numpy as np
import torch
import torch.nn.functional as F
import torch.optim as optim
import tyro
from dpg_actor import Actor
from dpg_critic import Critic
from stable_baselines3.common.buffers import ReplayBuffer
from torch.utils.tensorboard import SummaryWriter

BASE_DIR = "/gws/nopw/j04/ai4er/users/pn341/climate-rl-fedrl"
sys.path.append(BASE_DIR)

from fedrl.fedrl import FedRL
from param_tune.utils.no_op_summary_writer import NoOpSummaryWriter

os.environ["WANDB__SERVICE_WAIT"] = "600"
os.environ["MUJOCO_GL"] = "egl"
date = time.strftime("%Y-%m-%d", time.gmtime(time.time()))


@dataclass
class Args:
    exp_name: str = "dpg_torch"
    """the name of this experiment"""
    seed: int = 1
    """seed of the experiment"""
    torch_deterministic: bool = True
    """if toggled, `torch.backends.cudnn.deterministic=False`"""
    cuda: bool = True
    """if toggled, cuda will be enabled by default"""
    track: bool = True
    """if toggled, this experiment will be tracked with Weights and Biases"""
    wandb_project_name: str = "cleanRL"
    """the wandb's project name"""
    wandb_entity: str = "personal-p3jitnath"
    """the entity (team) of wandb's project"""
    wandb_group: str = date
    """the group name under wandb's project"""
    capture_video: bool = True
    """whether to capture videos of the agent performances (check out `videos` folder)"""
    capture_video_freq: int = 100
    """episode frequency at which to capture video"""

    env_id: str = "SimpleClimateBiasCorrection-v0"
    """the environment id of the environment"""
    total_timesteps: int = 60000
    """total timesteps of the experiments"""
    learning_rate: float = 3e-4
    """the learning rate of the optimizer"""
    buffer_size: int = int(1e6)
    """the replay memory buffer size"""
    gamma: float = 0.99
    """the discount factor gamma"""
    tau: float = 0.005
    """target smoothing coefficient (default: 0.005)"""
    batch_size: int = 256
    """the batch size of sample from the replay memory"""
    exploration_noise: float = 0.1
    """the scale of exploration noise"""
    learning_starts: int = 1000
    """timestep to start learning"""
    policy_frequency: int = 2
    """the frequency of training policy (delayed)"""
    # noise_clip: float = 0.5
    # """noise clip parameter of the Target Policy Smoothing Regularization"""

    optimise: bool = False
    """whether to modify output for hyperparameter optimisation"""
    write_to_file: str = ""
    """filename to write last episode return"""
    optim_group: str = ""
    """folder name under results to load optimised set of params"""
    opt_timesteps: Optional[int] = None
    """timestep duration for one single optimisation run"""

    actor_layer_size: int = 256
    """layer size for the actor network"""
    critic_layer_size: int = 256
    """layer size for the critic network"""

    num_steps: int = 200
    """the number of steps to run in each environment per policy rollout"""

    flwr_client: Optional[int] = None
    """flwr client id for Federated Learning"""
    flwr_episodes: int = 5
    """the number of episodes after each flwr update"""
    flwr_actor: bool = True
    """whether to exchange actor network weights"""
    flwr_critics: bool = False
    """whether to exchange critic network(s) weights"""

    def __post_init__(self):
        if self.flwr_client is not None:
            self.track = False
            # self.capture_video = False

        if self.optimise:
            self.track = False
            self.capture_video = False
            self.total_timesteps = self.opt_timesteps

        if self.optim_group:
            algo = self.exp_name.split("_")[0]
            with open(
                f"{BASE_DIR}/param_tune/results/{self.optim_group}/best_results.json",
                "r",
            ) as file:
                opt_params = {
                    k: v
                    for k, v in json.load(file)[algo].items()
                    if k not in {"algo", "episodic_return", "date"}
                }
                for key, value in opt_params.items():
                    if key == "actor_critic_layer_size":
                        setattr(self, "actor_layer_size", value)
                        setattr(self, "critic_layer_size", value)
                    elif hasattr(self, key):
                        setattr(self, key, value)


def make_env(env_id, seed, idx, capture_video, run_name, capture_video_freq):
    def thunk():
        if capture_video and idx == 0:
            try:
                env = gym.make(env_id, seed=seed, render_mode="rgb_array")
            except TypeError:
                env = gym.make(env_id, render_mode="rgb_array")
            env = gym.wrappers.RecordVideo(
                env,
                f"{BASE_DIR}/videos/{run_name}",
                episode_trigger=lambda x: (x == 0)
                or (
                    x % capture_video_freq == (capture_video_freq - 1)
                ),  # add 1 to the episode count generated by gym
            )
        else:
            try:
                env = gym.make(env_id, seed=seed)
            except TypeError:
                env = gym.make(env_id)
        env = gym.wrappers.RecordEpisodeStatistics(env)
        env.action_space.seed(seed)
        return env

    return thunk


args = tyro.cli(Args)
run_name = f"{args.wandb_group}/{args.env_id}__{args.exp_name}__{args.seed}__{int(time.time())}"

if args.track:
    import wandb

    wandb.init(
        project=args.wandb_project_name,
        entity=args.wandb_entity,
        group=args.wandb_group,
        sync_tensorboard=True,
        config=vars(args),
        name=run_name,
        monitor_gym=True,
        save_code=True,
    )

if args.optimise:
    writer = NoOpSummaryWriter()
else:
    writer = SummaryWriter(f"{BASE_DIR}/runs/{run_name}")

writer.add_text(
    "hyperparameters",
    "|param|value|\n|-|-|\n%s"
    % ("\n".join([f"|{key}|{value}|" for key, value in vars(args).items()])),
)

random.seed(args.seed)
np.random.seed(args.seed)
torch.manual_seed(args.seed)
torch.backends.cudnn.deterministic = args.torch_deterministic

device = torch.device(
    "cuda" if torch.cuda.is_available() and args.cuda else "cpu"
)
print(f"device: {device}", flush=True)
print(f"actor layer size: {args.actor_layer_size}", flush=True)
print(f"critic layer size: {args.critic_layer_size}", flush=True)

# 0. env setup
envs = gym.vector.SyncVectorEnv(
    [
        make_env(
            args.env_id,
            args.seed,
            0,
            args.capture_video,
            run_name,
            args.capture_video_freq,
        )
    ]
)
assert isinstance(
    envs.single_action_space, gym.spaces.Box
), "only continuous action space is supported"

actor = Actor(envs, args.actor_layer_size).to(device)
qf1 = Critic(envs, args.critic_layer_size).to(device)
# qf1_target = Critic(envs, args.critic_layer_size).to(device)
# target_actor = Actor(envs, args.actor_layer_size).to(device)

# target_actor.load_state_dict(actor.state_dict())
# qf1_target.load_state_dict(qf1.state_dict())

q_optimizer = optim.Adam(list(qf1.parameters()), lr=args.learning_rate)
actor_optimizer = optim.Adam(list(actor.parameters()), lr=args.learning_rate)

envs.single_observation_space.dtype = np.float32
# rb = ReplayBuffer(
#     args.buffer_size,
#     envs.single_observation_space,
#     envs.single_action_space,
#     device,
#     handle_timeout_termination=False,
# )

start_time = time.time()

# initialise for federated learning
if args.flwr_client is not None:

    weights_folder = f"{BASE_DIR}/weights/{run_name}"
    os.makedirs(f"{weights_folder}/actor", exist_ok=True)
    os.makedirs(f"{weights_folder}/critic", exist_ok=True)

    fedRL = FedRL(
        args.seed,
        None,
        actor,
        [qf1],
        args.flwr_client,
        None,
        args.flwr_actor,
        args.flwr_critics,
        weights_folder,
    )

    # save the current initial actor weights when using federated learning
    # print('[RL Agent]', args.seed, "saving local actor weights", flush=True)
    fedRL.save_weights(0)

    # load the new actor weights from the global server
    # print('[RL Agent]', args.seed, "loading global actor weights", flush=True)
    fedRL.load_weights(0)

# 1. start the game
obs, _ = envs.reset(seed=args.seed)
for global_step in range(1, args.total_timesteps + 1):
    # 2. retrieve action(s)
    # if global_step < args.learning_starts:
    #     actions = np.array(
    #         [envs.single_action_space.sample() for _ in range(envs.num_envs)]
    #     )
    # else:
    with torch.no_grad():
        actions = actor(torch.Tensor(obs).to(device))
        actions += torch.normal(0, actor.action_scale * args.exploration_noise)
        actions = (
            actions.cpu()
            .numpy()
            .clip(envs.single_action_space.low, envs.single_action_space.high)
        )

    # 3. execute the game and log data
    next_obs, rewards, terminations, truncations, infos = envs.step(actions)

    if "final_info" in infos:
        for info in infos["final_info"]:
            print(
                f"seed={args.seed}, global_step={global_step}, episodic_return={info['episode']['r']}",
                flush=True,
            )
            writer.add_scalar(
                "charts/episodic_return", info["episode"]["r"], global_step
            )
            writer.add_scalar(
                "charts/episodic_length", info["episode"]["l"], global_step
            )
            break

    # # 4. [skip] save data to replay buffer
    # real_next_obs = next_obs.copy()
    # for idx, trunc in enumerate(truncations):
    #     if trunc:
    #         real_next_obs[idx] = infos["final_observation"][idx]
    # rb.add(obs, real_next_obs, actions, rewards, terminations, infos)
    # if args.flwr_client is not None:
    #     fedRL.new_rb_entries.append(
    #         [obs, real_next_obs, actions, rewards, terminations, infos]
    #     )

    # 5. training
    # if global_step > args.learning_starts:

    # # 5a. [skip] calculate the target_q_values to compare with
    # data = rb.sample(args.batch_size)
    # with torch.no_grad():
    #     next_state_actions = target_actor(data.next_observations)
    #     qf1_next_target = qf1_target(
    #         data.next_observations, next_state_actions
    #     )
    #     target_q_values = data.rewards.flatten() + (
    #         1 - data.dones.flatten()
    #     ) * args.gamma * (qf1_next_target).view(-1)

    # qf1_a_values = qf1(data.observations, data.actions).view(-1)
    # qf1_loss = F.mse_loss(qf1_a_values, target_q_values)

    # 5a. calculate the q_values to compare with
    to_device = lambda *x: [torch.Tensor(t).to(device) for t in x]
    obs, next_obs, actions, rewards, terminations = to_device(
        obs, next_obs, actions, rewards, terminations
    )

    with torch.no_grad():
        next_state_actions = actor(next_obs)
        qf1_next = qf1(next_obs, next_state_actions)
        q_values = rewards.flatten() + (
            1 - terminations.flatten()
        ) * args.gamma * (qf1_next).view(-1)

    qf1_a_values = qf1(obs, actions).view(-1)
    qf1_loss = F.mse_loss(qf1_a_values, q_values)

    # 5b. update the critic
    q_optimizer.zero_grad()
    qf1_loss.backward()
    q_optimizer.step()

    # 5c. update the actor network
    if global_step % args.policy_frequency == 0:
        actor_loss = -qf1(obs, actor(obs)).mean()
        actor_optimizer.zero_grad()
        actor_loss.backward()
        actor_optimizer.step()

    # # 5d. [skip] soft-update the target networks
    # if global_step % args.policy_frequency == 0:
    #     for param, target_param in zip(
    #         actor.parameters(), target_actor.parameters()
    #     ):
    #         target_param.data.copy_(
    #             args.tau * param.data + (1 - args.tau) * target_param.data
    #         )
    #     for param, target_param in zip(
    #         qf1.parameters(), qf1_target.parameters()
    #     ):
    #         target_param.data.copy_(
    #             args.tau * param.data + (1 - args.tau) * target_param.data
    #         )

    if global_step % 100 == 0:
        writer.add_scalar(
            "losses/qf1_values", qf1_a_values.mean().item(), global_step
        )
        writer.add_scalar("losses/qf1_loss", qf1_loss.item(), global_step)
        writer.add_scalar("losses/actor_loss", actor_loss.item(), global_step)
        # print("SPS:", int(global_step / (time.time() - start_time)), flush=True)
        writer.add_scalar(
            "charts/SPS",
            int(global_step / (time.time() - start_time)),
            global_step,
        )

    obs = next_obs

    if args.flwr_client is not None and "final_info" in infos:
        if global_step % (args.flwr_episodes * args.num_steps) == 0:
            for info in infos["final_info"]:
                # save the current actor and/or critic weights when using federated learning
                # print('[RL Agent]', args.seed, "saving local weights", flush=True)
                fedRL.save_weights(global_step)

                # send the new replay buffer additions to the global server
                # print('[RL Agent]', args.seed, "saving new local replay buffer entries", flush=True)
                # fedRL.save_new_replay_buffer_entries()

                # load the new actor and/or critic weights from the global server
                # print('[RL Agent]', args.seed, "loading global weights", flush=True)
                fedRL.load_weights(global_step)

                # load the aggregated new replay buffer
                # print('[RL Agent]', args.seed, "loading global replay buffer", flush=True)
                # fedRL.load_replay_buffer()

                # add the samples to the local replay buffer
                # rb.reset()
                # for sample in fedRL.global_rb:
                #     rb.add(*sample)
                # print('[RL Agent]', args.seed, "rb size:", rb.size())

                break

    if global_step == args.total_timesteps:
        if args.write_to_file:
            episodic_return = info["episode"]["r"][0]
            with open(args.write_to_file, "wb") as file:
                pickle.dump(
                    {
                        "timesteps": args.total_timesteps,
                        "last_episodic_return": episodic_return,
                    },
                    file,
                )

envs.close()
writer.close()
