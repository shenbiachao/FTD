import math

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F


def _get_out_shape_cuda(in_shape, layers):
    x = torch.randn(*in_shape).cuda().unsqueeze(0)
    return layers(x).squeeze(0).shape


def _get_out_shape(in_shape, layers):
    x = torch.randn(*in_shape).unsqueeze(0)
    return layers(x).squeeze(0).shape


def gaussian_logprob(noise, log_std):
    """Compute Gaussian log probability"""
    residual = (-0.5 * noise.pow(2) - log_std).sum(-1, keepdim=True)
    return residual - 0.5 * np.log(2 * np.pi) * noise.size(-1)


def squash(mu, pi, log_pi):
    """Apply squashing function, see appendix C from https://arxiv.org/pdf/1812.05905.pdf"""
    mu = torch.tanh(mu)
    if pi is not None:
        pi = torch.tanh(pi)
    if log_pi is not None:
        log_pi -= torch.log(F.relu(1 - pi.pow(2)) + 1e-6).sum(-1, keepdim=True)
    return mu, pi, log_pi


def trunc_normal_(tensor, mean=0., std=1., a=-2., b=2.):
    """Truncated normal distribution, see https://people.sc.fsu.edu/~jburkardt/presentations/truncated_normal.pdf"""

    def norm_cdf(x):
        return (1. + math.erf(x / math.sqrt(2.))) / 2.

    with torch.no_grad():
        l = norm_cdf((a - mean) / std)
        u = norm_cdf((b - mean) / std)
        tensor.uniform_(2 * l - 1, 2 * u - 1)
        tensor.erfinv_()
        tensor.mul_(std * math.sqrt(2.))
        tensor.add_(mean)
        tensor.clamp_(min=a, max=b)
        return tensor


def weight_init(m):
    """Custom weight init for Conv2D and Linear layers"""
    if isinstance(m, nn.Linear):
        nn.init.orthogonal_(m.weight.data)
        if hasattr(m.bias, 'data'):
            m.bias.data.fill_(0.0)
    elif isinstance(m, nn.Conv2d) or isinstance(m, nn.ConvTranspose2d):
        # delta-orthogonal init from https://arxiv.org/pdf/1806.05393.pdf
        assert m.weight.size(2) == m.weight.size(3)
        m.weight.data.fill_(0.0)
        if hasattr(m.bias, 'data'):
            m.bias.data.fill_(0.0)
        mid = m.weight.size(2) // 2
        gain = nn.init.calculate_gain('relu')
        nn.init.orthogonal_(m.weight.data[:, :, mid, mid], gain)


class CenterCrop(nn.Module):
    def __init__(self, size):
        super().__init__()
        assert size in {84, 100}, f'unexpected size: {size}'
        self.size = size

    def forward(self, x):
        assert x.ndim == 4, 'input must be a 4D tensor'
        if x.size(2) == self.size and x.size(3) == self.size:
            return x
        assert x.size(3) == 100, f'unexpected size: {x.size(3)}'
        if self.size == 84:
            p = 8
        return x[:, :, p:-p, p:-p]


class NormalizeImg(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x / 255.


class Flatten(nn.Module):
    def __init__(self):
        super().__init__()

    def forward(self, x):
        return x.view(x.size(0), -1)


class RLProjection(nn.Module):
    def __init__(self, in_shape, out_dim):
        super().__init__()
        self.out_dim = out_dim
        self.projection = nn.Sequential(
            nn.Linear(in_shape[0], out_dim),
            nn.LayerNorm(out_dim),
            nn.Tanh()
        )
        self.apply(weight_init)

    def forward(self, x):
        return self.projection(x)


class SharedCNN(nn.Module):
    """
    Obtain an intermediate embedding of the original image; 
    the output shape is (-1, out_channel=num_filters, W, H).
    """

    def __init__(self, obs_shape, num_layers=11, num_filters=32):
        super().__init__()
        assert len(obs_shape) == 3
        self.num_layers = num_layers
        self.num_filters = num_filters

        self.layers = [CenterCrop(size=84), NormalizeImg(), nn.Conv2d(
            obs_shape[0], num_filters, 3, stride=2)]
        for _ in range(1, num_layers):
            self.layers.append(nn.ReLU())
            self.layers.append(
                nn.Conv2d(num_filters, num_filters, 3, stride=1))
        self.layers = nn.Sequential(*self.layers)
        self.out_shape = _get_out_shape(obs_shape, self.layers)
        self.apply(weight_init)

    def forward(self, x):
        return self.layers(x)


class HeadCNN(nn.Module):
    """
    Obtain the flattened final embedding based on the SharedCNN.
    """

    def __init__(self, in_shape, num_layers=0, num_filters=32):
        super().__init__()
        self.layers = []
        for _ in range(0, num_layers):
            self.layers.append(nn.ReLU())
            self.layers.append(
                nn.Conv2d(num_filters, num_filters, 3, stride=1))
        self.layers.append(Flatten())
        self.layers = nn.Sequential(*self.layers)
        self.out_shape = _get_out_shape(in_shape, self.layers)
        self.apply(weight_init)

    def forward(self, x):
        return self.layers(x)


class SelectorCNN(nn.Module):
    def __init__(self, selector_layers, obs_shape, region_num=5, in_channels=3, stack_num=3, num_shared_layers=11,
                 num_filters=32):
        super().__init__()
        assert len(obs_shape) == 3
        # assert region_num * in_channels * stack_num == obs_shape[0]
        self.obs_shape = obs_shape
        self.in_channels = in_channels
        self.stack_num = stack_num
        self.num_filters = num_filters

        self.selector_layers = selector_layers

        self.shared_layers = [
            nn.Conv2d(self.stack_num * self.in_channels, num_filters, 3, stride=2)]
        for _ in range(1, num_shared_layers):
            self.shared_layers.append(nn.ReLU())
            self.shared_layers.append(
                nn.Conv2d(num_filters, num_filters, 3, stride=1))
        self.shared_layers = nn.Sequential(*self.shared_layers)

        self.out_shape = _get_out_shape([self.stack_num * self.in_channels, self.obs_shape[-2], self.obs_shape[-1]],
                                        self.shared_layers)
        self.shared_layers.apply(weight_init)

    def forward(self, x):
        x = self.selector_layers(x)
        x = self.shared_layers(x)

        return x


class Encoder(nn.Module):
    def __init__(self, shared_cnn, head_cnn, projection):
        super().__init__()
        self.shared_cnn = shared_cnn
        self.head_cnn = head_cnn
        self.projection = projection
        self.out_dim = projection.out_dim

    def forward(self, x, detach=False):
        x = self.shared_cnn(x)
        x = self.head_cnn(x)
        if detach:
            x = x.detach()
        return self.projection(x)


class Actor(nn.Module):
    def __init__(self, encoder, action_shape, hidden_dim, log_std_min, log_std_max):
        super().__init__()
        self.encoder = encoder
        self.log_std_min = log_std_min
        self.log_std_max = log_std_max
        self.mlp = nn.Sequential(
            nn.Linear(self.encoder.out_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, 2 * action_shape[0])
        )
        self.mlp.apply(weight_init)

    def forward(self, x, compute_pi=True, compute_log_pi=True, detach=False):
        x = self.encoder(x, detach)
        mu, log_std = self.mlp(x).chunk(2, dim=-1)
        log_std = torch.tanh(log_std)
        log_std = self.log_std_min + 0.5 * (
                self.log_std_max - self.log_std_min
        ) * (log_std + 1)

        if compute_pi:
            std = log_std.exp()
            noise = torch.randn_like(mu)
            pi = mu + noise * std
        else:
            pi = None
            entropy = None

        if compute_log_pi:
            log_pi = gaussian_logprob(noise, log_std)
        else:
            log_pi = None

        mu, pi, log_pi = squash(mu, pi, log_pi)

        return mu, pi, log_pi, log_std


class QFunction(nn.Module):
    def __init__(self, obs_dim, action_dim, hidden_dim):
        super().__init__()
        self.trunk = nn.Sequential(
            nn.Linear(obs_dim + action_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, hidden_dim), nn.ReLU(),
            nn.Linear(hidden_dim, 1)
        )
        self.apply(weight_init)

    def forward(self, obs, action):
        assert obs.size(0) == action.size(0)
        return self.trunk(torch.cat([obs, action], dim=1))


class Critic(nn.Module):
    def __init__(self, encoder, action_shape, hidden_dim):
        super().__init__()
        self.encoder = encoder
        self.Q1 = QFunction(
            self.encoder.out_dim, action_shape[0], hidden_dim
        )
        self.Q2 = QFunction(
            self.encoder.out_dim, action_shape[0], hidden_dim
        )

    def forward(self, x, action, detach=False):
        x = self.encoder(x, detach)
        return self.Q1(x, action), self.Q2(x, action)


class SiameseNet(nn.Module):
    def __init__(self, input_dim, hidden_sizes, noise_scale=0.05):
        super(SiameseNet, self).__init__()
        assert len(hidden_sizes) > 1
        temp = input_dim
        output_dim = hidden_sizes[-1]

        fc_layers = []
        for hidden_size in hidden_sizes[:-1]:
            fc_layer = nn.Linear(input_dim, hidden_size)
            nn.init.orthogonal_(fc_layer.weight.data)
            fc_layer.bias.data.fill_(0.)

            fc_layers.append(fc_layer)
            fc_layers.append(nn.ReLU())

            input_dim = hidden_size

        last_layer = nn.Linear(hidden_sizes[-2], output_dim)
        nn.init.orthogonal_(fc_layer.weight.data)
        last_layer.bias.data.fill_(0.)

        fc_layers.append(last_layer)

        self.fc_layers = nn.Sequential(*fc_layers)

        self.noise_scale = noise_scale

    def forward(self, h, a):
        noise = torch.randn_like(h) * self.noise_scale
        h = h + noise
        h = h.clamp(-1.0, 1.0)
        noise = torch.randn_like(a) * self.noise_scale
        a = (a + noise).clamp(-1.0, 1.0)
        x = torch.cat((h, a), dim=1)
        return self.fc_layers(x)


class SiameseNet_a(nn.Module):
    def __init__(self, input_dim, action_dim, hidden_sizes, max_action):
        super(SiameseNet_a, self).__init__()
        output_dim = hidden_sizes[-1]

        fc_layers = []
        for hidden_size in hidden_sizes:
            fc_layer = nn.Linear(input_dim, hidden_size)
            nn.init.orthogonal_(fc_layer.weight.data)
            fc_layer.bias.data.fill_(0.)

            fc_layers.append(fc_layer)
            fc_layers.append(nn.ReLU())

            input_dim = hidden_size

        last_layer = nn.Linear(hidden_sizes[-1], action_dim)
        nn.init.orthogonal_(fc_layer.weight.data)
        last_layer.bias.data.fill_(0.)

        fc_layers.append(last_layer)

        self.fc_layers = nn.Sequential(*fc_layers)

        self.max_action = max_action

    def forward(self, h1, h2):
        h = torch.cat([h1 + h2, torch.abs(h1 - h2)], dim=1)
        h = self.fc_layers(h)
        return self.max_action * torch.tanh(h)


def make_siamese_net(type, feature_dim, action_dim, hidden_sizes, noise_scale=0.0, max_action=1.0):
    if type == 'critic':
        return SiameseNet(feature_dim + action_dim, hidden_sizes, noise_scale=noise_scale)
    else:
        return SiameseNet_a(feature_dim * 2, action_dim, hidden_sizes, max_action)
