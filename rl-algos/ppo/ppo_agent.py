import numpy as np
import torch
import torch.nn as nn

# def layer_init(layer, std=np.sqrt(2), bias_const=0.0):
#     torch.nn.init.orthogonal_(layer.weight, std)
#     torch.nn.init.constant_(layer.bias, bias_const)
#     return layer


class Agent(nn.Module):
    def __init__(self, envs, actor_layer_size, critic_layer_size):
        super().__init__()
        self.critic = nn.Sequential(
            nn.Linear(
                np.array(envs.single_observation_space.shape).prod(),
                critic_layer_size,
            ),
            nn.LeakyReLU(),
            nn.Linear(critic_layer_size, critic_layer_size),
            nn.LeakyReLU(),
            nn.Linear(critic_layer_size, 1),
        )
        self.actor_mean = nn.Sequential(
            nn.Linear(
                np.array(envs.single_observation_space.shape).prod(),
                actor_layer_size,
            ),
            nn.LeakyReLU(),
            nn.Linear(actor_layer_size, actor_layer_size),
            nn.LeakyReLU(),
            nn.Linear(
                actor_layer_size, np.prod(envs.single_action_space.shape)
            ),
        )
        self.actor_logstd = nn.Parameter(
            torch.zeros(1, np.prod(envs.single_action_space.shape))
        )

    def get_value(self, x):
        return self.critic(x)

    def get_action_and_value(self, x, action=None):
        action_mean = self.actor_mean(x)
        action_logstd = self.actor_logstd.expand_as(action_mean)
        action_std = torch.exp(action_logstd)
        probs = torch.distributions.Normal(action_mean, action_std)
        if action is None:
            action = probs.sample()
        return (
            action,
            probs.log_prob(action).sum(1),
            probs.entropy().sum(1),
            self.critic(x),
        )
