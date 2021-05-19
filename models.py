"""
This script defines the NeRF architecture
"""


import torch
from torch import nn


class Siren(nn.Module):
    """
    Siren layer
    """
    def __init__(self):
        super().__init__()

    def forward(self, input):
        return torch.sin(30 * input)


class Mapping(nn.Module):
    def __init__(self, mapping_size, in_size, logscale=True):
        """
        Defines a function that embeds x to (x, sin(2^k x), cos(2^k x), ...)
        in_channels: number of input channels (3 for both xyz and direction)
        """
        super(Mapping, self).__init__()
        self.N_freqs = mapping_size
        self.in_channels = in_size
        self.funcs = [torch.sin, torch.cos]
        self.out_channels = self.in_channels*(len(self.funcs)*self.N_freqs+1)

        if logscale:
            self.freq_bands = 2**torch.linspace(0, self.N_freqs-1, self.N_freqs)
        else:
            self.freq_bands = torch.linspace(1, 2**(self.N_freqs-1), self.N_freqs)

    def forward(self, x):
        """
        Embeds x to (x, sin(2^k x), cos(2^k x), ...)
        Different from the paper, "x" is also in the output
        See https://github.com/bmild/nerf/issues/12
        Inputs:
            x: (B, self.in_channels)
        Outputs:
            out: (B, self.out_channels)
        """
        #out = [x]
        out = []
        for freq in self.freq_bands:
            for func in self.funcs:
                out += [func(freq*x)]

        return torch.cat(out, -1)


class NeRF(nn.Module):
    def __init__(self,
                 layers=8, feat=100,
                 input_sizes=[3, 3],
                 skips=[4], siren=False,
                 mapping=True,
                 mapping_sizes=[10, 4]):
        """
        layers: integer, number of layers for density (sigma) encoder
        feat: integer, number of hidden units in each layer
        input_sizes: tuple [a, b] where a is the number of input channels for xyz (3*10*2=60 by default)
                                        b is the number of input channels for dir (3*4*2=24 by default)
        skips: list of layer indices, e.g. [i] means add skip connection in the i-th layer
        siren: boolean, use Siren where possible instead of ReLU if True
        mapping: boolean, use positional encoding if True
        mapping_sizes: tuple [a, b] where a and b are the number of freqs for the positional encoding of xyz and dir
        """
        super(NeRF, self).__init__()
        self.layers = layers
        self.skips = skips
        self.mapping = mapping
        self.input_sizes = input_sizes

        # activation function
        nl = Siren() if siren else torch.nn.ReLU()

        # use positional encoding if specified, otherwise Siren initialization is used
        in_size = input_sizes.copy()
        if mapping:
            self.mapping = [Mapping(map_sz, in_sz) for map_sz, in_sz in zip(mapping_sizes, input_sizes)]
            in_size = [2 * map_sz * in_sz for map_sz, in_sz in zip(mapping_sizes, input_sizes)]
        else:
            self.mapping = [Siren(), Siren()]

        # define the main network of fully connected layers, i.e. FC_NET
        fc_layers = []
        fc_layers.append(torch.nn.Linear(in_size[0], feat))
        fc_layers.append(nl)
        for i in range(1, layers):
            if i in skips:
                fc_layers.append(torch.nn.Linear(feat + in_size[0], feat))
            else:
                fc_layers.append(torch.nn.Linear(feat, feat))
            fc_layers.append(nl)
        self.fc_net = torch.nn.Sequential(*fc_layers)  # shared 8-layer structure that takes the encoded xyz vector

        # FC_NET output 1: volume density
        self.sigma_from_xyz = torch.nn.Sequential(torch.nn.Linear(feat, 1), nn.Softplus())

        # FC_NET output 2: vector of features from the spatial coordinates
        self.feats_from_xyz = torch.nn.Sequential(torch.nn.Linear(feat, feat), nl)

        # the FC_NET output 2 is concatenated to the encoded viewing direction input
        # and the resulting vector of features is used to predict the rgb color
        self.rgb_from_xyzdir = torch.nn.Sequential(torch.nn.Linear(feat + in_size[1], feat // 2), nl,
                                                   torch.nn.Linear(feat // 2, 3), torch.nn.Sigmoid())

    def forward(self, x, input_dir=None, sigma_only=False):
        """
        Predicts the values rgb, sigma from a batch of input rays
        the input rays are represented as a set of 3d points xyz

        Args:
            input_xyz: (B, 3) input tensor, with the 3d spatial coordinates, B is batch size
            sigma_only: boolean, infer sigma only if True, otherwise infer both sigma and color

        Returns:
            if sigma_ony:
                sigma: (B, 1) volume density
            else:
                out: (B, 4) first 3 columns are rgb color, last column is volume density
        """
        if not sigma_only and self.input_sizes[1] > 0:
            input_xyz, input_dir = torch.split(x, [self.input_sizes[0], self.input_sizes[1]], dim=-1)
        else:
            input_xyz = x

        # compute shared features
        input_xyz = self.mapping[0](input_xyz)
        xyz_ = input_xyz
        for i in range(self.layers):
            if i in self.skips:
                xyz_ = torch.cat([input_xyz, xyz_], -1)
            xyz_ = self.fc_net[2*i](xyz_)
            xyz_ = self.fc_net[2*i + 1](xyz_)
        shared_features = xyz_

        # compute volume density
        sigma = self.sigma_from_xyz(shared_features)
        if sigma_only:
            return sigma

        # compute color
        xyz_features = self.feats_from_xyz(shared_features)
        if self.input_sizes[1] > 0:
            input_xyzdir = torch.cat([xyz_features, self.mapping[1](input_dir)], -1)
        else:
            input_xyzdir = xyz_features
        rgb = self.rgb_from_xyzdir(input_xyzdir)

        out = torch.cat([rgb, sigma], -1)

        return out