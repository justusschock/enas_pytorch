import pdb
import visdom
import argparse
import numpy as np
from tqdm import tqdm, trange

import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.backends.cudnn as cudnn
from torchvision import datasets, transforms
from torch.utils.data.dataset import Subset
from torch.optim.lr_scheduler import CosineAnnealingLR

from utils.utils import CSVLogger, AverageMeter
from utils.cutout import Cutout
from models.controller import Controller
from models.shared_cnn import SharedCNN

parser = argparse.ArgumentParser(description='ENAS')

parser.add_argument('--search_for', default='macro', choices=['macro'])
parser.add_argument('--data_path', default='/export/mlrg/terrance/Projects/data/', type=str)
parser.add_argument('--output_filename', default='ENAS2', type=str)
parser.add_argument('--batch_size', type=int, default=128)
parser.add_argument('--num_epochs', type=int, default=310)
parser.add_argument('--log_every', type=int, default=50)
parser.add_argument('--eval_every_epochs', type=int, default=1)
parser.add_argument('--seed', type=int, default=0)
parser.add_argument('--cutout', type=int, default=0)

parser.add_argument('--child_num_layers', type=int, default=12)
parser.add_argument('--child_out_filters', type=int, default=36)
parser.add_argument('--child_grad_bound', type=float, default=5.0)
parser.add_argument('--child_l2_reg', type=float, default=0.00025)
parser.add_argument('--child_num_branches', type=int, default=6)
parser.add_argument('--child_keep_prob', type=float, default=0.9)
parser.add_argument('--child_lr_max', type=float, default=0.05)
parser.add_argument('--child_lr_min', type=float, default=0.0005)
parser.add_argument('--child_lr_T', type=float, default=10)

parser.add_argument('--controller_lstm_size', type=int, default=64)
parser.add_argument('--controller_lstm_num_layers', type=int, default=1)
parser.add_argument('--controller_entropy_weight', type=float, default=0.0001)
parser.add_argument('--controller_train_every', type=int, default=1)
parser.add_argument('--controller_num_aggregate', type=int, default=20)
parser.add_argument('--controller_train_steps', type=int, default=50)
parser.add_argument('--controller_lr', type=float, default=0.001)
parser.add_argument('--controller_tanh_constant', type=float, default=1.5)
parser.add_argument('--controller_op_tanh_reduce', type=float, default=2.5)
parser.add_argument('--controller_skip_target', type=float, default=0.4)
parser.add_argument('--controller_skip_weight', type=float, default=0.8)
parser.add_argument('--controller_bl_dec', type=float, default=0.99)

args = parser.parse_args()

vis = visdom.Visdom()
vis.env = 'ENAS_' + args.output_filename
vis_win = {'shared_cnn_acc': None, 'shared_cnn_loss': None, 'controller_reward': None,
           'controller_acc': None, 'controller_loss': None}

# cudnn.benchmark = True  # TODO: test to see if this actually makes things go faster


def load_datasets():
    normalize = transforms.Normalize(mean=[x / 255.0 for x in [125.3, 123.0, 113.9]],
                                     std=[x / 255.0 for x in [63.0, 62.1, 66.7]])

    train_transform = transforms.Compose([
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        normalize])

    if args.cutout > 0:
        train_transform.transforms.append(Cutout(length=args.cutout))

    valid_transform = transforms.Compose([
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        normalize])

    test_transform = transforms.Compose([
        transforms.ToTensor(),
        normalize])

    train_dataset = datasets.CIFAR10(root=args.data_path,
                                     train=True,
                                     transform=train_transform,
                                     download=True)

    valid_dataset = datasets.CIFAR10(root=args.data_path,
                                     train=True,
                                     transform=valid_transform,
                                     download=True)

    test_dataset = datasets.CIFAR10(root=args.data_path,
                                    train=False,
                                    transform=test_transform,
                                    download=True)

    train_indices = list(range(0, 45000))
    valid_indices = list(range(45000, 50000))
    train_subset = Subset(train_dataset, train_indices)
    valid_subset = Subset(valid_dataset, valid_indices)

    train_loader = torch.utils.data.DataLoader(dataset=train_subset,
                                               batch_size=args.batch_size,
                                               shuffle=True,
                                               pin_memory=True,
                                               num_workers=2)

    valid_loader = torch.utils.data.DataLoader(dataset=valid_subset,
                                               batch_size=args.batch_size,
                                               shuffle=True,
                                               pin_memory=True,
                                               num_workers=2,
                                               drop_last=True)

    test_loader = torch.utils.data.DataLoader(dataset=test_dataset,
                                              batch_size=args.batch_size,
                                              shuffle=False,
                                              pin_memory=True,
                                              num_workers=2)

    return train_loader, valid_loader, test_loader


def train_shared_cnn(epoch,
                     controller,
                     shared_cnn,
                     data_loaders,
                     shared_cnn_optimizer):

    global vis_win

    controller.eval()
    train_loader, _, _ = data_loaders

    train_acc_meter = AverageMeter()
    loss_meter = AverageMeter()

    progress_bar = tqdm(train_loader)
    for (images, labels) in progress_bar:
        progress_bar.set_description('Epoch ' + str(epoch))

        images = images.cuda()
        labels = labels.cuda()

        with torch.no_grad():
            controller()  # perform forward pass to generate a new architecture

        sample_arc = controller.sample_arc

        '''
        # Test architecture
        sample_arc = {}
        arc = [
        [4],
        [3, 1],
        [2, 0, 0],
        [4, 1, 0, 1],
        [0, 1, 1, 0, 1],
        [1, 1, 1, 0, 1, 1],
        [3, 1, 0, 1, 1, 1, 0],
        [5, 0, 0, 0, 0, 0, 0, 0],
        [5, 1, 0, 1, 1, 0, 0, 0, 0],
        [4, 1, 1, 0, 0, 0, 1, 0, 1, 0],
        [4, 1, 1, 1, 0, 1, 0, 0, 0, 0, 1],
        [4, 0, 0, 0, 1, 1, 1, 1, 0, 0, 1, 1]]
        
        for i in range(len(arc)):
            sample_arc[str(i)] = [torch.tensor(arc[i][0]).cuda()]
            if len(arc[i]) > 1:
                sample_arc[str(i)].append(torch.tensor(arc[i][1:]).cuda())
        '''
        shared_cnn.zero_grad()
        pred = shared_cnn(images, sample_arc)
        loss = nn.CrossEntropyLoss()(pred, labels)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(shared_cnn.parameters(), args.child_grad_bound)
        shared_cnn_optimizer.step()

        train_acc = torch.mean((torch.max(pred, 1)[1] == labels).type(torch.float))

        train_acc_meter.update(train_acc.item())
        loss_meter.update(loss.item())

        progress_bar.set_postfix(
            train_acc='%.3f' % train_acc_meter.avg,
            loss='%.3f' % loss_meter.avg)

    vis_win['shared_cnn_acc'] = vis.line(
        X=np.array([epoch]),
        Y=np.array([train_acc_meter.avg]),
        win=vis_win['shared_cnn_acc'],
        opts=dict(title='shared_cnn_acc', xlabel='Iteration', ylabel='Accuracy'),
        update='append' if epoch > 0 else None)

    vis_win['shared_cnn_loss'] = vis.line(
        X=np.array([epoch]),
        Y=np.array([loss_meter.avg]),
        win=vis_win['shared_cnn_loss'],
        opts=dict(title='shared_cnn_loss', xlabel='Iteration', ylabel='Loss'),
        update='append' if epoch > 0 else None)

    controller.train()


def train_controller(epoch,
                     controller,
                     shared_cnn,
                     data_loaders,
                     controller_optimizer,
                     baseline=None):
    '''
    https://github.com/melodyguan/enas/blob/master/src/cifar10/general_controller.py#L270
    '''

    global vis_win

    shared_cnn.eval()
    _, valid_loader, _ = data_loaders

    reward_meter = AverageMeter()
    baseline_meter = AverageMeter()
    val_acc_meter = AverageMeter()
    loss_meter = AverageMeter()

    progress_bar = trange(args.controller_train_steps * args.controller_num_aggregate)
    controller.zero_grad()
    for i in progress_bar:
        progress_bar.set_description('Epoch ' + str(epoch))
        images, labels = next(iter(valid_loader))

        images = images.cuda()
        labels = labels.cuda()

        controller()  # perform forward pass to generate a new architecture
        sample_arc = controller.sample_arc

        with torch.no_grad():
            pred = shared_cnn(images, sample_arc)
        batch_val_acc = torch.mean((torch.max(pred, 1)[1] == labels).type(torch.float))

        reward = torch.tensor(batch_val_acc.detach())  # make sure that gradients aren't backpropped through the reward
        reward += args.controller_entropy_weight * controller.sample_entropy

        sample_log_prob = controller.sample_log_prob

        if baseline is None:
            baseline = batch_val_acc
        else:
            baseline -= (1 - args.controller_bl_dec) * (baseline - reward)
            baseline = baseline.detach()  # to make sure that gradients are not backpropped through the baseline

        # Might need to multiply by -1
        loss = -1 * sample_log_prob * (reward - baseline)

        if args.controller_skip_weight is not None:
            loss += args.controller_skip_weight * controller.skip_penaltys

        loss.backward(retain_graph=True)

        # Aggregate gradients for controller_num_aggregate iterationa, then update weights
        if (i + 1) % args.controller_num_aggregate == 0:
            controller_optimizer.step()
            controller.zero_grad()

        reward_meter.update(reward.item())
        baseline_meter.update(baseline.item())
        val_acc_meter.update(batch_val_acc.item())
        loss_meter.update(loss.item())

        progress_bar.set_postfix(
            reward='%.3f' % reward_meter.avg,
            baseline='%.3f' % baseline_meter.avg,
            val_acc='%.3f' % val_acc_meter.avg,
            loss='%.3f' % loss_meter.avg)

    vis_win['controller_reward'] = vis.line(
        X=np.column_stack([epoch] * 2),
        Y=np.column_stack([reward_meter.avg, baseline_meter.avg]),
        win=vis_win['controller_reward'],
        opts=dict(title='controller_reward', xlabel='Iteration', ylabel='Reward'),
        update='append' if epoch > 0 else None)

    vis_win['controller_acc'] = vis.line(
        X=np.array([epoch]),
        Y=np.array([val_acc_meter.avg]),
        win=vis_win['controller_acc'],
        opts=dict(title='controller_acc', xlabel='Iteration', ylabel='Accuracy'),
        update='append' if epoch > 0 else None)

    vis_win['controller_loss'] = vis.line(
        X=np.array([epoch]),
        Y=np.array([loss_meter.avg]),
        win=vis_win['controller_loss'],
        opts=dict(title='controller_loss', xlabel='Iteration', ylabel='Loss'),
        update='append' if epoch > 0 else None)

    shared_cnn.train()
    return baseline


def get_eval_accuracy(loader, shared_cnn, sample_arc):
    total = 0.
    acc_sum = 0.
    for (images, labels) in loader:
        images = images.cuda()
        labels = labels.cuda()

        pred = shared_cnn(images, sample_arc)
        acc_sum += torch.sum((torch.max(pred, 1)[1] == labels).type(torch.float))
        total += pred.shape[0]

    acc = acc_sum / total
    return acc.item()


def evaluate_model(epoch, controller, shared_cnn, data_loaders, n_samples=10):
    controller.eval()
    shared_cnn.eval()

    _, valid_loader, test_loader = data_loaders

    images, labels = next(iter(valid_loader))
    images = images.cuda()
    labels = labels.cuda()

    print('Here are 10 architectures:')

    arcs = []
    val_accs = []
    for i in range(n_samples):
        controller()  # perform forward pass to generate a new architecture
        sample_arc = controller.sample_arc
        arcs.append(sample_arc)

        with torch.no_grad():
            pred = shared_cnn(images, sample_arc)
        val_acc = torch.mean((torch.max(pred, 1)[1] == labels).type(torch.float))
        val_accs.append(val_acc.item())

        print_arc(sample_arc)
        print('val_acc=' + str(val_acc.item()))
        print('-' * 80)

    best_iter = np.argmax(val_accs)
    best_arc = arcs[best_iter]

    valid_acc = get_eval_accuracy(valid_loader, shared_cnn, best_arc)
    test_acc = get_eval_accuracy(test_loader, shared_cnn, best_arc)

    print('Epoch ' + str(epoch) + ': Eval')
    print('valid_accuracy: %.4f' % (valid_acc))
    print('test_accuracy: %.4f' % (test_acc))

    controller.train()
    shared_cnn.train()


def print_arc(sample_arc):
    for key, value in sample_arc.items():
        if len(value) == 1:
            branch_type = value[0].cpu().numpy().tolist()
            print('[' + ' '.join(str(n) for n in branch_type) + ']')
        else:
            branch_type = value[0].cpu().numpy().tolist()
            skips = value[1].cpu().numpy().tolist()
            print('[' + ' '.join(str(n) for n in (branch_type + skips)) + ']')


def main():
    np.random.seed(args.seed)
    torch.cuda.manual_seed(args.seed)

    print(args)

    data_loaders = load_datasets()

    controller = Controller(search_for=args.search_for,
                            search_whole_channels=True,
                            num_layers=args.child_num_layers,
                            num_branches=args.child_num_branches,
                            out_filters=args.child_out_filters,
                            lstm_size=args.controller_lstm_size,
                            lstm_num_layers=args.controller_lstm_num_layers,
                            tanh_constant=args.controller_tanh_constant,
                            temperature=None,
                            skip_target=args.controller_skip_target,
                            skip_weight=args.controller_skip_weight)
    controller = controller.cuda()

    shared_cnn = SharedCNN(num_layers=args.child_num_layers,
                           num_branches=args.child_num_branches,
                           out_filters=args.child_out_filters,
                           keep_prob=args.child_keep_prob)
    shared_cnn = shared_cnn.cuda()

    # https://github.com/melodyguan/enas/blob/master/src/utils.py#L218
    controller_optimizer = torch.optim.Adam(params=controller.parameters(),
                                            lr=args.controller_lr,
                                            betas=(0.0, 0.999),
                                            eps=1e-3)

    # https://github.com/melodyguan/enas/blob/master/src/utils.py#L213
    shared_cnn_optimizer = torch.optim.SGD(params=shared_cnn.parameters(),
                                           lr=args.child_lr_max,
                                           momentum=0.9,
                                           nesterov=True,
                                           weight_decay=args.child_l2_reg)

    # PyTorch has slightly different implementation than TensorFlow
    # https://github.com/melodyguan/enas/blob/master/src/utils.py#L154
    # https://pytorch.org/docs/master/optim.html#torch.optim.lr_scheduler.CosineAnnealingLR
    shared_cnn_scheduler = CosineAnnealingLR(optimizer=shared_cnn_optimizer,
                                             T_max=args.child_lr_T,
                                             eta_min=args.child_lr_min)

    baseline = None
    for epoch in range(args.num_epochs):

        train_shared_cnn(epoch,
                         controller,
                         shared_cnn,
                         data_loaders,
                         shared_cnn_optimizer)

        baseline = train_controller(epoch,
                                    controller,
                                    shared_cnn,
                                    data_loaders,
                                    controller_optimizer,
                                    baseline)

        evaluate_model(epoch, controller, shared_cnn, data_loaders)

        shared_cnn_scheduler.step(epoch)

        state = {'epoch': epoch + 1,
                 'args': args,
                 'shared_cnn_state_dict': shared_cnn.state_dict(),
                 'controller_state_dict': controller.state_dict(),
                 'shared_cnn_optimizer': shared_cnn_optimizer.state_dict(),
                 'controller_optimizer': controller_optimizer.state_dict()}
        filename = 'checkpoints/' + args.output_filename + '.pth.tar'
        torch.save(state, filename)


if __name__ == "__main__":
    main()