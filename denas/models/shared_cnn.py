import torch
import torch.nn.functional as F

"""
Notes
-----

This implementation uses Instancenorm instead of Batchnorm to enable small 
batchsizes
"""


class FactorizedReduction(torch.nn.Module):
    """
    Reduce both spatial dimensions (width and height) by a factor of 2, and
    potentially to change the number of output filters

    """

    def __init__(self, in_planes, out_planes, stride=2):
        """

        Parameters
        ----------
        in_planes : int
            number of input planes
        out_planes : int
            number of output planes
        stride : int
            reduction stride

        """
        super().__init__()

        assert out_planes % 2 == 0, (
            "Need even number of filters when using this factorized reduction.")

        self.in_planes = in_planes
        self.out_planes = out_planes
        self.stride = stride

        if stride == 1:
            self.fr = torch.nn.Sequential(
                torch.nn.Conv2d(in_planes, out_planes, kernel_size=1,
                                bias=False),
                torch.nn.InstanceNorm2d(out_planes))
        else:
            self.path1 = torch.nn.Sequential(
                torch.nn.AvgPool2d(1, stride=stride),
                torch.nn.Conv2d(in_planes, out_planes // 2, kernel_size=1,
                                bias=False))

            self.path2 = torch.nn.Sequential(
                torch.nn.AvgPool2d(1, stride=stride),
                torch.nn.Conv2d(in_planes, out_planes // 2, kernel_size=1,
                                bias=False))
            self.bn = torch.nn.InstanceNorm2d(out_planes)

    def forward(self, x):
        """
        Feed tensor through layer

        Parameters
        ----------
        x : :class:`torch.Tensor`
            input tensor

        Returns
        -------
        :class:`torch.Tensor`
            result tensor

        """
        if self.stride == 1:
            return self.fr(x)
        else:
            path1 = self.path1(x)

            # pad the right and the bottom, then crop to include those pixels
            path2 = F.pad(x, pad=(0, 1, 0, 1), mode='constant', value=0.)
            path2 = path2[:, :, 1:, 1:]
            path2 = self.path2(path2)

            out = torch.cat([path1, path2], dim=1)
            out = self.bn(out)
            return out


class ENASLayer(torch.nn.Module):

    def __init__(self, layer_id, in_planes, out_planes):
        """

        Parameters
        ----------
        layer_id :
            current layer
        in_planes : int
            number of input planes
        out_planes : int
            number of output planes

        """
        super().__init__()

        self.layer_id = layer_id
        self.in_planes = in_planes
        self.out_planes = out_planes

        self.branch_0 = ConvBranch(in_planes, out_planes, kernel_size=3)
        self.branch_1 = ConvBranch(in_planes, out_planes, kernel_size=3,
                                   separable=True)
        self.branch_2 = ConvBranch(in_planes, out_planes, kernel_size=5)
        self.branch_3 = ConvBranch(in_planes, out_planes, kernel_size=5,
                                   separable=True)
        self.branch_4 = PoolBranch(in_planes, out_planes, 'avg')
        self.branch_5 = PoolBranch(in_planes, out_planes, 'max')

        self.bn = torch.nn.InstanceNorm2d(out_planes)

    def forward(self, x, prev_layers, sample_arc):
        layer_type = sample_arc[0]
        if self.layer_id > 0:
            skip_indices = sample_arc[1]
        else:
            skip_indices = []

        if layer_type == 0:
            out = self.branch_0(x)
        elif layer_type == 1:
            out = self.branch_1(x)
        elif layer_type == 2:
            out = self.branch_2(x)
        elif layer_type == 3:
            out = self.branch_3(x)
        elif layer_type == 4:
            out = self.branch_4(x)
        elif layer_type == 5:
            out = self.branch_5(x)
        else:
            raise ValueError("Unknown layer_type {}".format(layer_type))

        for i, skip in enumerate(skip_indices):
            if skip == 1:
                out += prev_layers[i]

        out = self.bn(out)
        return out


class FixedLayer(torch.nn.Module):

    def __init__(self, layer_id, in_planes, out_planes, sample_arc):
        """

        Parameters
        ----------
        layer_id :
            current layer
        in_planes : int
            number of input planes
        out_planes : int
            number of output planes
        sample_arc : list
            sampling sequence
        """
        super().__init__()

        self.layer_id = layer_id
        self.in_planes = in_planes
        self.out_planes = out_planes
        self.sample_arc = sample_arc

        self.layer_type = sample_arc[0]
        if self.layer_id > 0:
            self.skip_indices = sample_arc[1]
        else:
            self.skip_indices = torch.zeros(1)

        if self.layer_type == 0:
            self.branch = ConvBranch(in_planes, out_planes, kernel_size=3)
        elif self.layer_type == 1:
            self.branch = ConvBranch(in_planes, out_planes, kernel_size=3,
                                     separable=True)
        elif self.layer_type == 2:
            self.branch = ConvBranch(in_planes, out_planes, kernel_size=5)
        elif self.layer_type == 3:
            self.branch = ConvBranch(in_planes, out_planes, kernel_size=5,
                                     separable=True)
        elif self.layer_type == 4:
            self.branch = PoolBranch(in_planes, out_planes, 'avg')
        elif self.layer_type == 5:
            self.branch = PoolBranch(in_planes, out_planes, 'max')
        else:
            raise ValueError("Unknown layer_type {}".format(self.layer_type))

        # Use concatentation instead of addition in the fixed layer for some
        # reason
        in_planes = int((torch.sum(self.skip_indices).item() + 1) * in_planes)
        self.dim_reduc = torch.nn.Sequential(
            torch.nn.Conv2d(in_planes, out_planes, kernel_size=1, bias=False),
            torch.nn.ReLU(),
            torch.nn.InstanceNorm2d(out_planes))

    def forward(self, x, prev_layers, sample_arc):
        out = self.branch(x)

        res_layers = []
        for i, skip in enumerate(self.skip_indices):
            if skip == 1:
                res_layers.append(prev_layers[i])
        prev = res_layers + [out]
        prev = torch.cat(prev, dim=1)

        out = self.dim_reduc(prev)
        return out


class SeparableConv(torch.nn.Module):
    def __init__(self, in_planes, out_planes, kernel_size, bias):
        super(SeparableConv, self).__init__()
        padding = (kernel_size - 1) // 2
        self.depthwise = torch.nn.Conv2d(in_planes, in_planes,
                                         kernel_size=kernel_size,
                                         padding=padding, groups=in_planes,
                                         bias=bias)
        self.pointwise = torch.nn.Conv2d(in_planes, out_planes, kernel_size=1,
                                         bias=bias)

    def forward(self, x):
        out = self.depthwise(x)
        out = self.pointwise(out)
        return out


class ConvBranch(torch.nn.Module):

    def __init__(self, in_planes, out_planes, kernel_size, separable=False):
        """
        Parameters
        ----------
        in_planes : int
            number of input planes
        out_planes : int
            number of output planes
        kernel_size : int
            kernel size
        separable : bool
            whether to use a separable conv or not

        """
        super().__init__()
        assert kernel_size in [3, 5], "Kernel size must be either 3 or 5"

        self.in_planes = in_planes
        self.out_planes = out_planes
        self.kernel_size = kernel_size
        self.separable = separable

        self.inp_conv1 = torch.nn.Sequential(
            torch.nn.Conv2d(in_planes, out_planes, kernel_size=1, bias=False),
            torch.nn.BatchNorm2d(out_planes),
            torch.nn.ReLU())

        if separable:
            self.out_conv = torch.nn.Sequential(
                SeparableConv(in_planes, out_planes, kernel_size=kernel_size,
                              bias=False),
                torch.nn.BatchNorm2d(out_planes),
                torch.nn.ReLU())
        else:
            padding = (kernel_size - 1) // 2
            self.out_conv = torch.nn.Sequential(
                torch.nn.Conv2d(in_planes, out_planes, kernel_size=kernel_size,
                                padding=padding, bias=False),
                torch.nn.BatchNorm2d(out_planes),
                torch.nn.ReLU())

    def forward(self, x):
        out = self.inp_conv1(x)
        out = self.out_conv(out)
        return out


class PoolBranch(torch.nn.Module):

    def __init__(self, in_planes, out_planes, avg_or_max):
        """

        Parameters
        ----------
        in_planes : int
            number of input planes
        out_planes : int
            number of output planes
        avg_or_max : str
            whether to use avg or max pool
        """
        super().__init__()

        self.in_planes = in_planes
        self.out_planes = out_planes
        self.avg_or_max = avg_or_max

        self.conv1 = torch.nn.Sequential(
            torch.nn.Conv2d(in_planes, out_planes, kernel_size=1, bias=False),
            torch.nn.InstanceNorm2d(out_planes),
            torch.nn.ReLU())

        if avg_or_max == 'avg':
            self.pool = torch.torch.nn.AvgPool2d(kernel_size=3, stride=1,
                                                 padding=1)
        elif avg_or_max == 'max':
            self.pool = torch.torch.nn.MaxPool2d(kernel_size=3, stride=1,
                                                 padding=1)
        else:
            raise ValueError("Unknown pool {}".format(avg_or_max))

    def forward(self, x):
        out = self.conv1(x)
        out = self.pool(out)
        return out


class SharedCNN(torch.nn.Module):
    def __init__(self,
                 num_layers=12,
                 num_branches=6,
                 out_filters=24,
                 keep_prob=1.0,
                 fixed_arc=None
                 ):
        super(SharedCNN, self).__init__()

        self.num_layers = num_layers
        self.num_branches = num_branches
        self.out_filters = out_filters
        self.keep_prob = keep_prob
        self.fixed_arc = fixed_arc

        pool_distance = self.num_layers // 3
        self.pool_layers = [pool_distance - 1, 2 * pool_distance - 1]

        self.stem_conv = torch.nn.Sequential(
            torch.nn.Conv2d(3, out_filters, kernel_size=3, padding=1, bias=False),
            torch.nn.InstanceNorm2d(out_filters))

        self.layers = torch.nn.ModuleList([])
        self.pooled_layers = torch.nn.ModuleList([])

        for layer_id in range(self.num_layers):
            if self.fixed_arc is None:
                layer = ENASLayer(layer_id, self.out_filters, self.out_filters)
            else:
                layer = FixedLayer(layer_id, self.out_filters, self.out_filters,
                                   self.fixed_arc[str(layer_id)])
            self.layers.append(layer)

            if layer_id in self.pool_layers:
                for i in range(len(self.layers)):
                    if self.fixed_arc is None:
                        self.pooled_layers.append(FactorizedReduction(
                            self.out_filters, self.out_filters))
                    else:
                        self.pooled_layers.append(FactorizedReduction(
                            self.out_filters, self.out_filters * 2))
                if self.fixed_arc is not None:
                    self.out_filters *= 2

        self.global_avg_pool = torch.nn.AdaptiveAvgPool2d((1, 1))
        self.dropout = torch.nn.Dropout(p=1. - self.keep_prob)
        self.classify = torch.nn.Linear(self.out_filters, 10)

        for m in self.modules():
            if isinstance(m, torch.nn.Conv2d):
                torch.nn.init.kaiming_uniform_(m.weight, mode='fan_in',
                                               nonlinearity='relu')

    def forward(self, x, sample_arc):

        x = self.stem_conv(x)

        prev_layers = []
        pool_count = 0
        for layer_id in range(self.num_layers):
            x = self.layers[layer_id](x, prev_layers,
                                      sample_arc[str(layer_id)])
            prev_layers.append(x)
            if layer_id in self.pool_layers:
                for i, prev_layer in enumerate(prev_layers):
                    # Go through the outputs of all previous layers
                    # and downsample them
                    prev_layers[i] = self.pooled_layers[pool_count](prev_layer)
                    pool_count += 1
                x = prev_layers[-1]

        x = self.global_avg_pool(x)
        x = x.view(x.shape[0], -1)
        x = self.dropout(x)
        out = self.classify(x)

        return {"pred": out}
