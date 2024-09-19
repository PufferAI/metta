
import gymnasium as gym
import hydra
import numpy as np
import pufferlib
import pufferlib.frameworks.cleanrl
import pufferlib.models
import pufferlib.pytorch
import torch
from omegaconf import OmegaConf
from pufferlib.emulation import PettingZooPufferEnv
from pufferlib.environment import PufferEnv
from tensordict import TensorDict
from torch import nn

from agent.metta_agent import MettaAgent


class Recurrent(pufferlib.models.LSTMWrapper):
    def __init__(self, env, policy, input_size=512, hidden_size=512, num_layers=1):
        super().__init__(env, policy, input_size, hidden_size, num_layers)

class PufferAgentWrapper(nn.Module):
    def __init__(self, agent: MettaAgent, env: PettingZooPufferEnv):
        super().__init__()
        # self.dtype = pufferlib.pytorch.nativize_dtype(env.emulated)
        # xcxc
        self.atn_type = nn.Linear(agent.decoder_out_size(), env.action_space[0].n)
        self.atn_param = nn.Linear(agent.decoder_out_size(), env.action_space[1].n)
        self._agent = agent
        print(self)

    def forward(self, obs):
        x, _ = self.encode_observations(obs)
        return self.decode_actions(x, None)

    def encode_observations(self, flat_obs):
        obs = {
            "grid_obs": flat_obs.float(),
            "global_vars": torch.zeros(flat_obs.shape[0], dtype=float).to(flat_obs.device)
        }
        td = TensorDict({"obs": obs})
        self._agent.encode_observations(td)
        return td["encoded_obs"], None

    def decode_actions(self, flat_hidden, lookup, concat=None):
        action = [self.atn_type(flat_hidden), self.atn_param(flat_hidden)]
        value = self._agent._critic_linear(flat_hidden)
        return action, value

def make_policy(env: PufferEnv, cfg: OmegaConf):
    if "grid_obs" in cfg.agent.observation_encoder:
        cfg.agent.observation_encoder.grid_obs.feature_names = env._grid_env.grid_features()
        cfg.agent.observation_encoder.global_vars.feature_names = []
    obs_space = gym.spaces.Dict({
        "grid_obs": env.single_observation_space,
        "global_vars": gym.spaces.Box(
            low=-np.inf, high=np.inf,
            shape=[ 0 ],
            dtype=np.int32)
    })
    agent = hydra.utils.instantiate(
        cfg.agent, obs_space,
        env.single_action_space, _recursive_=False)
    puffer_agent = PufferAgentWrapper(agent, env)

    if cfg.agent.core.rnn_num_layers > 0:
        puffer_agent = Recurrent(
            env, puffer_agent, input_size=cfg.agent.fc.output_dim,
            hidden_size=cfg.agent.core.rnn_size,
            num_layers=cfg.agent.core.rnn_num_layers
        )
        puffer_agent = pufferlib.frameworks.cleanrl.RecurrentPolicy(puffer_agent)
    else:
        puffer_agent = pufferlib.frameworks.cleanrl.Policy(puffer_agent)

    return puffer_agent.to(cfg.framework.pufferlib.device)
