#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import json
import logging
import math
import os
from os.path import exists, join, split
import threading

import time

import numpy as np
import shutil
import tqdm

import sys
from PIL import Image
import torch
from torch import nn
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data as data
from torchvision import datasets
from torch.autograd import Variable

import drn
import data_transforms

sys.path.append(os.path.abspath('../../active_learning'))
from active_learning import ActiveLearning
from active_loss import LossPredictionLoss
from active_learning_utils import *
from discriminative_learning import *


try:
    from modules import batchnormsync
except ImportError:
    pass

FORMAT = "[%(asctime)-15s %(filename)s:%(lineno)d %(funcName)s] %(message)s"
logging.basicConfig(format=FORMAT)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


CITYSCAPE_PALETTE = np.asarray([
    [128, 64, 128],
    [244, 35, 232],
    [70, 70, 70],
    [102, 102, 156],
    [190, 153, 153],
    [153, 153, 153],
    [250, 170, 30],
    [220, 220, 0],
    [107, 142, 35],
    [152, 251, 152],
    [70, 130, 180],
    [220, 20, 60],
    [255, 0, 0],
    [0, 0, 142],
    [0, 0, 70],
    [0, 60, 100],
    [0, 80, 100],
    [0, 0, 230],
    [119, 11, 32],
    [0, 0, 0]], dtype=np.uint8)


TRIPLET_PALETTE = np.asarray([
    [0, 0, 0, 255],
    [217, 83, 79, 255],
    [91, 192, 222, 255]], dtype=np.uint8)


def fill_up_weights(up):
    w = up.weight.data
    f = math.ceil(w.size(2) / 2)
    c = (2 * f - 1 - f % 2) / (2. * f)
    for i in range(w.size(2)):
        for j in range(w.size(3)):
            w[0, 0, i, j] = \
                (1 - math.fabs(i / f - c)) * (1 - math.fabs(j / f - c))
    for c in range(1, w.size(0)):
        w[c, 0, :, :] = w[0, 0, :, :]


class DRNSeg(nn.Module):
    def __init__(self, model_name, classes, pretrained_model=None,
                 pretrained=True, use_torch_up=False):
        super(DRNSeg, self).__init__()
        model = drn.__dict__.get(model_name)(
            pretrained=pretrained, num_classes=1000, remove_last_2_layers=True)

        # Remember channel sizes for the active learning.
        self.channels = list(model.get_active_learning_feature_channel_counts())
        # Adding 2 more layers for the active learning.
        self.channels.append(classes)
        self.channels.append(classes)

        pmodel = nn.DataParallel(model)
        if pretrained_model is not None:
            pmodel.load_state_dict(pretrained_model)
        self.base = model
        self.seg = nn.Conv2d(model.out_dim, classes,
                             kernel_size=1, bias=True)
        self.softmax = nn.LogSoftmax()
        m = self.seg
        n = m.kernel_size[0] * m.kernel_size[1] * m.out_channels
        m.weight.data.normal_(0, math.sqrt(2. / n))
        m.bias.data.zero_()
        if use_torch_up:
            self.up = nn.UpsamplingBilinear2d(scale_factor=8)
        else:
            up = nn.ConvTranspose2d(classes, classes, 16, stride=8, padding=4,
                                    output_padding=0, groups=classes,
                                    bias=False)
            fill_up_weights(up)
            up.weight.requires_grad = False
            self.up = up

    def forward(self, x):
        x = self.base(x)
        self.active_learning_features = self.base.get_active_learning_features()
        x = self.seg(x)
        self.active_learning_features.append(x)
        y = self.up(x)
        self.active_learning_features.append(self.softmax(y))
        return self.softmax(y), x

    def get_active_learning_feature_channel_counts(self):
        return self.channels

    def get_active_learning_features(self):
        #print("Active learning feature Shapes are ================")
        #for f in self.active_learning_features:
        #    print(f.size())
        return self.active_learning_features

    def get_discriminative_al_layer_shapes(self):
        # All we have is one flat tensor of size 512.
        return self.base.get_discriminative_al_layer_shapes()

    def get_discriminative_al_features(self):
        return self.base.get_discriminative_al_features()

    def optim_parameters(self, memo=None):
        for param in self.base.parameters():
            yield param
        for param in self.seg.parameters():
            yield param


class SegList(torch.utils.data.Dataset):
    def __init__(self, data_dir, phase, transforms, list_dir=None,
                 out_name=False):
        self.list_dir = data_dir if list_dir is None else list_dir
        self.data_dir = data_dir
        self.out_name = out_name
        self.phase = phase
        self.transforms = transforms
        self.image_list = None
        self.label_list = None
        self.bbox_list = None
        self.read_lists()

    def __getitem__(self, index):
        data = [Image.open(join(self.data_dir, self.image_list[index]))]
        if self.label_list is not None:
            mask_path = join(self.data_dir, self.label_list[index])
            data.append(Image.open(mask_path))
        data = list(self.transforms(*data))
        if self.out_name:
            if self.label_list is None:
                data.append(item()[0, :, :])
            data.append(self.image_list[index])
        return tuple(data)

    def __len__(self):
        return len(self.image_list)

    def read_lists(self):
        image_path = join(self.list_dir, self.phase + '_images.txt')
        label_path = join(self.list_dir, self.phase + '_labels.txt')
        assert exists(image_path)
        self.image_list = [line.strip() for line in open(image_path, 'r')]
        if exists(label_path):
            self.label_list = [line.strip() for line in open(label_path, 'r')]
            assert len(self.image_list) == len(self.label_list)

    # Needed for writing csv files to be uploaded to annotate.online.
    def get_image_path(self, index):
        return self.image_list[index]


class SegListMS(torch.utils.data.Dataset):
    def __init__(self, data_dir, phase, transforms, scales, list_dir=None):
        self.list_dir = data_dir if list_dir is None else list_dir
        self.data_dir = data_dir
        self.phase = phase
        self.transforms = transforms
        self.image_list = None
        self.label_list = None
        self.bbox_list = None
        self.read_lists()
        self.scales = scales

    def __getitem__(self, index):
        data = [Image.open(join(self.data_dir, self.image_list[index]))]
        w, h = item().size
        if self.label_list is not None:
            data.append(Image.open(
                join(self.data_dir, self.label_list[index])))
        out_data = list(self.transforms(*data))
        ms_images = [self.transforms(item().resize((int(w * s), int(h * s)),
                                                    Image.BICUBIC))[0]
                     for s in self.scales]
        out_data.append(self.image_list[index])
        out_data.extend(ms_images)
        return tuple(out_data)

    def __len__(self):
        return len(self.image_list)

    def read_lists(self):
        image_path = join(self.list_dir, self.phase + '_images.txt')
        label_path = join(self.list_dir, self.phase + '_labels.txt')
        assert exists(image_path)
        self.image_list = [line.strip() for line in open(image_path, 'r')]
        if exists(label_path):
            self.label_list = [line.strip() for line in open(label_path, 'r')]
            assert len(self.image_list) == len(self.label_list)


def validate(val_loader, model, criterion, eval_score=None, print_freq=40, num_classes=1000,
             use_loss_prediction_al=False, use_discriminative_al=False):
    batch_time = AverageMeter()
    losses = AverageMeter()
    score = AverageMeter()
    mAP = AverageMeter()

    # switch to evaluate mode
    model.eval()

    end = time.time()
    hist = np.zeros((num_classes, num_classes))
    for i, (input, target) in enumerate(val_loader):
        if type(criterion) in [torch.nn.modules.loss.L1Loss,
                               torch.nn.modules.loss.MSELoss]:
            target = target.float()
        input = input.cuda()
        target = target.cuda(non_blocking=True)
        input_var = torch.autograd.Variable(input, volatile=True)
        target_var = torch.autograd.Variable(target, volatile=True)

        # compute output
        if use_loss_prediction_al or use_discriminative_al:
            output = model(input_var)[0][0]
        else:
            output = model(input_var)[0]
        loss = criterion(output, target_var).mean()

        # measure accuracy and record loss
        # prec1, prec5 = accuracy(output.data, target, topk=(1, 5))
        losses.update(loss.item(), input.size(0))
        if eval_score is not None:
            score.update(eval_score(output, target_var), input.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        _, pred = torch.max(output, 1)
        pred = pred.cpu().data.numpy()
        label = target.cpu().numpy()
        # Remove the 'background' class and compute the matrix hist, where
        # hist[i][j] is the number of pixels for which ground truth class
        # was i, but predicted j.
        hist += fast_hist(pred.flatten(), label.flatten(), num_classes)
        current_mAP = round(np.nanmean(per_class_iu(hist)) * 100, 2)
        mAP.update(current_mAP)
        if i % print_freq == 0:
            logger.info('Test: [{0}/{1}]\t'
                        'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                        'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                        'Score {score.val:.3f} ({score.avg:.3f})\t'
                        'mAP {mAP.val:.3f} ({mAP.avg:.3f})'.format(
                i, len(val_loader), batch_time=batch_time, loss=losses,
                score=score, mAP=mAP))

    logger.info(' * Score {top1.avg:.3f}'.format(top1=score))

    return score.avg, mAP.avg


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def accuracy(output, target):
    """Computes the precision@k for the specified values of k"""
    # batch_size = target.size(0) * target.size(1) * target.size(2)
    _, pred = output.max(1)
    pred = pred.view(1, -1)
    target = target.view(1, -1)
    correct = pred.eq(target)
    correct = correct[target != 255]
    correct = correct.view(-1)
    score = correct.float().sum(0).mul(100.0 / correct.size(0))
    return score.item()


def train(train_loader, model, criterion, optimizer, epoch,
          eval_score=None, print_freq=100, use_loss_prediction_al=False, active_learning_lamda=1, 
          use_discriminative_al=False):
    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    scores = AverageMeter()

    # switch to train mode
    model.train()

    end = time.time()

    # Values used for Loss Prediction Active Learning.
    total_ranked = 0
    correctly_ranked = 0
    criterion_lp = LossPredictionLoss()

    for i, (input, target) in enumerate(train_loader):
        # measure data loading time
        data_time.update(time.time() - end)

        if type(criterion) in [torch.nn.modules.loss.L1Loss,
                               torch.nn.modules.loss.MSELoss]:
            target = target.float()

        input = input.cuda()
        target = target.cuda(non_blocking=True)
        input_var = torch.autograd.Variable(input)
        target_var = torch.autograd.Variable(target)

        # compute output
        if use_loss_prediction_al:
            if epoch < 150:
                output, loss_pred = model(input_var)
            else:
                output, loss_pred = model(input_var, detach_lp=True)
            output = output[0]
        elif use_discriminative_al:
            output, labeled_unlabeled_predictions = model(input_var)
        else:
            output = model(input_var)[0]

        loss = criterion(output, target_var)

        # Compute means from [N, W, H] to [N].
        loss = loss.mean([1, 2])
        # Let the main model "warm-up" for a while, loss prediction does not
        # work well otherwise.
        if use_loss_prediction_al and epoch > 1:
            loss_prediction_loss = criterion_lp(loss_pred, loss)
            # Also compute (an estimate) of the ranking accuracy for the training set.
            batch_size = loss.shape[0]
            for l1 in range(batch_size):
                for l2 in range(l1):
                    total_ranked += 1
                    if (loss[l1] - loss[l2]) * (loss_pred[l1] - loss_pred[l2]) > 0:
                        correctly_ranked += 1
            if i % print_freq == 0:
                logger.info(
                    "loss.mean() = {} active_learning_lamda = {}, loss_prediction_loss = {}".format(
                        loss.mean(), active_learning_lamda, loss_prediction_loss));
            loss = loss.mean() + active_learning_lamda * loss_prediction_loss
        else:
            loss = loss.mean()

        # measure accuracy and record loss
        # prec1, prec5 = accuracy(output.data, target, topk=(1, 5))
        losses.update(loss.item(), input.size(0))
        if eval_score is not None:
            scores.update(eval_score(output, target_var), input.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        if i % print_freq == 0:
            logger.info('{0} Epoch: [{1}][{2}/{3}]\t'
                        'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                        'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                        'Loss {loss.val:.4f} ({loss.avg:.4f})\t'
                        'Score {top1.val:.3f} ({top1.avg:.3f})'
                        'Ranking accuracy estimate ({ranking_accuracy})'.format(
                get_algorithm_name(use_loss_prediction_al, use_discriminative_al, None),
                epoch, i, len(train_loader), batch_time=batch_time,
                data_time=data_time, loss=losses, top1=scores,
                ranking_accuracy=correctly_ranked/(total_ranked+0.00001)))


def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, 'model_best.pth.tar')


def train_seg(args):
    rand_state = np.random.RandomState(1311)
    torch.manual_seed(1311)
    device = 'cuda' if (torch.cuda.is_available()) else 'cpu'

    # We have 2975 images total in the training set, so let's choose 500 for 3 cycles,
    # 1500 images total (~1/2 of total)
    images_per_cycle = 150

    batch_size = args.batch_size
    num_workers = args.workers
    crop_size = args.crop_size

    print(' '.join(sys.argv))

    for k, v in args.__dict__.items():
        print(k, ':', v)

    # Data loading code
    data_dir = args.data_dir
    info = json.load(open(join(data_dir, 'info.json'), 'r'))
    normalize = data_transforms.Normalize(mean=info['mean'],
                                     std=info['std'])
    t = []
    if args.random_rotate > 0:
        t.append(data_transforms.RandomRotate(args.random_rotate))
    if args.random_scale > 0:
        t.append(data_transforms.RandomScale(args.random_scale))
    t.extend([data_transforms.RandomCrop(crop_size),
              data_transforms.RandomHorizontalFlip(),
              data_transforms.ToTensor(),
              normalize])
    dataset = SegList(data_dir, 'train', data_transforms.Compose(t),
                list_dir=args.list_dir)
    training_dataset_no_augmentation = SegList(
        data_dir, 'train',
        data_transforms.Compose([data_transforms.ToTensor(), normalize]),
        list_dir=args.list_dir
    )

    unlabeled_idx = list(range(len(dataset)))
    labeled_idx = []
    validation_accuracies = list()
    validation_mAPs = list()
    progress = tqdm.tqdm(range(10))
    for cycle in progress:
        single_model = DRNSeg(args.arch, args.classes, None,
                              pretrained=True)
        if args.pretrained:
            single_model.load_state_dict(torch.load(args.pretrained))

        # Wrap our model in Active Learning Model.
        if args.use_loss_prediction_al:
            single_model = ActiveLearning(
                single_model, global_avg_pool_size=6, fc_width=256)
        elif args.use_discriminative_al:
            single_model = DiscriminativeActiveLearning(single_model)
        optim_parameters = single_model.optim_parameters()

        model = torch.nn.DataParallel(single_model).cuda()

        # Don't apply a 'mean' reduction, we need the whole loss vector.
        criterion = nn.NLLLoss(ignore_index=255, reduction='none')

        criterion.cuda()

        if args.choose_images_with_highest_loss:
            # Choosing images based on the ground truth labels. 
            # We want to check if predicting loss with 100% accuracy would result to
            # a good active learning algorithm.
            new_indices, entropies = choose_new_labeled_indices_using_gt(
                    model, cycle, rand_state, unlabeled_idx, training_dataset_no_augmentation,
                    device, criterion, images_per_cycle)
        else:
            new_indices, entropies = choose_new_labeled_indices(
                model, training_dataset_no_augmentation, cycle, rand_state,
                labeled_idx, unlabeled_idx, device, images_per_cycle,
                args.use_loss_prediction_al, args.use_discriminative_al, input_pickle_file=None)
        labeled_idx.extend(new_indices)
        print("Running on {} labeled images.".format(len(labeled_idx)));
        if args.output_superannotate_csv_file is not None:
            # Write image paths to csv file which can be uploaded to annotate.online.
            write_entropies_csv(
                training_dataset_no_augmentation, new_indices,
                entropies, args.output_superannotate_csv_file)

        train_loader = torch.utils.data.DataLoader(
            data.Subset(dataset, labeled_idx),
            batch_size=batch_size, shuffle=True, num_workers=num_workers,
            pin_memory=True, drop_last=True
        )
        val_loader = torch.utils.data.DataLoader(
            SegList(data_dir, 'val', data_transforms.Compose([
                data_transforms.RandomCrop(crop_size),
                data_transforms.ToTensor(),
                normalize,
            ]), list_dir=args.list_dir),
            batch_size=batch_size, shuffle=False, num_workers=num_workers,
            pin_memory=True, drop_last=True
        )

        # define loss function (criterion) and optimizer.
        optimizer = torch.optim.SGD(optim_parameters,
                                    args.lr,
                                    momentum=args.momentum,
                                    weight_decay=args.weight_decay)

        cudnn.benchmark = True
        best_prec1 = 0
        best_mAP = 0
        start_epoch = 0

        # optionally resume from a checkpoint
        if args.resume:
            if os.path.isfile(args.resume):
                print("=> loading checkpoint '{}'".format(args.resume))
                checkpoint = torch.load(args.resume)
                start_epoch = checkpoint['epoch']
                best_prec1 = checkpoint['best_prec1']
                model.load_state_dict(checkpoint['state_dict'])
                print("=> loaded checkpoint '{}' (epoch {})"
                      .format(args.resume, checkpoint['epoch']))
            else:
                print("=> no checkpoint found at '{}'".format(args.resume))

        if args.evaluate:
            validate(val_loader, model, criterion, eval_score=accuracy,
                     num_classes=args.classes,
                     use_loss_prediction_al=args.use_loss_prediction_al)
            return

        progress_epoch = tqdm.tqdm(range(start_epoch, args.epochs))
        for epoch in progress_epoch:
            lr = adjust_learning_rate(args, optimizer, epoch)
            logger.info('Cycle {0} Epoch: [{1}]\tlr {2:.06f}'.format(cycle, epoch, lr))
            # train for one epoch
            train(train_loader, model, criterion, optimizer, epoch,
                  eval_score=accuracy, use_loss_prediction_al=args.use_loss_prediction_al, 
                  active_learning_lamda=args.lamda)

            # evaluate on validation set
            prec1, mAP1 = validate(val_loader, model, criterion, eval_score=accuracy,
                             num_classes=args.classes,
                             use_loss_prediction_al=args.use_loss_prediction_al)

            is_best = prec1 > best_prec1
            best_prec1 = max(prec1, best_prec1)
            best_mAP = max(mAP1, best_mAP)
            checkpoint_path = os.path.join(args.save_path, 'checkpoint_latest.pth.tar')
            save_checkpoint({
                'epoch': epoch + 1,
                'arch': args.arch,
                'state_dict': model.state_dict(),
                'best_prec1': best_prec1,
                'best_mAP': best_mAP,
            }, is_best, filename=checkpoint_path)
            if (epoch + 1) % args.save_iter == 0:
                history_path = os.path.join(args.save_path, 'checkpoint_{:03d}.pth.tar'.format(epoch + 1))
                shutil.copyfile(checkpoint_path, history_path)
        validation_accuracies.append(best_prec1)
        validation_mAPs.append(best_mAP)
        print("{} accuracies: {} mAPs {}".format(
            "Active Learning" if args.use_loss_prediction_al else "Random",
            str(validation_accuracies),
            str(validation_mAPs)))
        # Compute histogram of loss values for the unlabeled part of training dataset.
        # Uncomment next lines if you want to check the loss distribution.
        # loss_value_histogram(
        #     model, cycle, rand_state, unlabeled_idx,
        #     training_dataset_no_augmentation, device, criterion)
        # loss_value_min_max_average(
        #     model, cycle, rand_state, unlabeled_idx,
        #     dataset, device, criterion)


def adjust_learning_rate(args, optimizer, epoch):
    """
    Sets the learning rate to the initial LR decayed by 10 every 30 epochs
    """
    if args.lr_mode == 'step':
        lr = args.lr * (0.1 ** (epoch // args.step))
    elif args.lr_mode == 'poly':
        lr = args.lr * (1 - epoch / args.epochs) ** 0.9
    else:
        raise ValueError('Unknown lr mode {}'.format(args.lr_mode))

    for param_group in optimizer.param_groups:
        param_group['lr'] = lr
    return lr


def fast_hist(pred, label, n):
    k = (label >= 0) & (label < n)
    return np.bincount(
        n * label[k].astype(int) + pred[k], minlength=n ** 2).reshape(n, n)


def per_class_iu(hist):
    return np.diag(hist) / (hist.sum(1) + hist.sum(0) - np.diag(hist))


def save_output_images(predictions, filenames, output_dir):
    """
    Saves a given (B x C x H x W) into an image file.
    If given a mini-batch tensor, will save the tensor as a grid of images.
    """
    # pdb.set_trace()
    for ind in range(len(filenames)):
        im = Image.fromarray(predictions[ind].astype(np.uint8))
        fn = os.path.join(output_dir, filenames[ind][:-4] + '.png')
        out_dir = split(fn)[0]
        if not exists(out_dir):
            os.makedirs(out_dir)
        im.save(fn)


def save_colorful_images(predictions, filenames, output_dir, palettes):
   """
   Saves a given (B x C x H x W) into an image file.
   If given a mini-batch tensor, will save the tensor as a grid of images.
   """
   for ind in range(len(filenames)):
       im = Image.fromarray(palettes[predictions[ind].squeeze()])
       fn = os.path.join(output_dir, filenames[ind][:-4] + '.png')
       out_dir = split(fn)[0]
       if not exists(out_dir):
           os.makedirs(out_dir)
       im.save(fn)


def test(eval_data_loader, model, num_classes,
         output_dir='pred', has_gt=True, save_vis=False):
    model.eval()
    batch_time = AverageMeter()
    data_time = AverageMeter()
    end = time.time()
    hist = np.zeros((num_classes, num_classes))
    for iter, (image, label, name) in enumerate(eval_data_loader):
        data_time.update(time.time() - end)
        image_var = Variable(image, requires_grad=False, volatile=True)
        final = model(image_var)[0]
        _, pred = torch.max(final, 1)
        pred = pred.cpu().data.numpy()
        batch_time.update(time.time() - end)
        if save_vis:
            save_output_images(pred, name, output_dir)
            save_colorful_images(
                pred, name, output_dir + '_color',
                TRIPLET_PALETTE if num_classes == 3 else CITYSCAPE_PALETTE)
        if has_gt:
            label = label.numpy()
            hist += fast_hist(pred.flatten(), label.flatten(), num_classes)
            logger.info('===> mAP {mAP:.3f}'.format(
                mAP=round(np.nanmean(per_class_iu(hist)) * 100, 2)))
        end = time.time()
        logger.info('Eval: [{0}/{1}]\t'
                    'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                    'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                    .format(iter, len(eval_data_loader), batch_time=batch_time,
                            data_time=data_time))
    if has_gt: #val
        ious = per_class_iu(hist) * 100
        logger.info(' '.join('{:.03f}'.format(i) for i in ious))
        return round(np.nanmean(ious), 2)


def resize_4d_tensor(tensor, width, height):
    tensor_cpu = tensor.cpu().numpy()
    if tensor.size(2) == height and tensor.size(3) == width:
        return tensor_cpu
    out_size = (tensor.size(0), tensor.size(1), height, width)
    out = np.empty(out_size, dtype=np.float32)

    def resize_one(i, j):
        out[i, j] = np.array(
            Image.fromarray(tensor_cpu[i, j]).resize(
                (width, height), Image.BILINEAR))

    def resize_channel(j):
        for i in range(tensor.size(0)):
            out[i, j] = np.array(
                Image.fromarray(tensor_cpu[i, j]).resize(
                    (width, height), Image.BILINEAR))

    # workers = [threading.Thread(target=resize_one, args=(i, j))
    #            for i in range(tensor.size(0)) for j in range(tensor.size(1))]

    workers = [threading.Thread(target=resize_channel, args=(j,))
               for j in range(tensor.size(1))]
    for w in workers:
        w.start()
    for w in workers:
        w.join()
    # for i in range(tensor.size(0)):
    #     for j in range(tensor.size(1)):
    #         out[i, j] = np.array(
    #             Image.fromarray(tensor_cpu[i, j]).resize(
    #                 (w, h), Image.BILINEAR))
    # out = tensor.new().resize_(*out.shape).copy_(torch.from_numpy(out))
    return out


def test_ms(eval_data_loader, model, num_classes, scales,
            output_dir='pred', has_gt=True, save_vis=False):
    model.eval()
    batch_time = AverageMeter()
    data_time = AverageMeter()
    end = time.time()
    hist = np.zeros((num_classes, num_classes))
    num_scales = len(scales)
    for iter, input_data in enumerate(eval_data_loader):
        data_time.update(time.time() - end)
        if has_gt:
            name = input_data[2]
            label = input_data[1]
        else:
            name = input_data[1]
        h, w = input_item().size()[2:4]
        images = [input_item()]
        images.extend(input_data[-num_scales:])
        # pdb.set_trace()
        outputs = []
        for image in images:
            image_var = Variable(image, requires_grad=False, volatile=True)
            final = model(image_var)[0]
            outputs.append(final.data)
        final = sum([resize_4d_tensor(out, w, h) for out in outputs])
        # _, pred = torch.max(torch.from_numpy(final), 1)
        # pred = pred.cpu().numpy()
        pred = final.argmax(axis=1)
        batch_time.update(time.time() - end)
        if save_vis:
            save_output_images(pred, name, output_dir)
            save_colorful_images(pred, name, output_dir + '_color',
                                 CITYSCAPE_PALETTE)
        if has_gt:
            label = label.numpy()
            hist += fast_hist(pred.flatten(), label.flatten(), num_classes)
            logger.info('===> mAP {mAP:.3f}'.format(
                mAP=round(np.nanmean(per_class_iu(hist)) * 100, 2)))
        end = time.time()
        logger.info('Eval: [{0}/{1}]\t'
                    'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                    'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                    .format(iter, len(eval_data_loader), batch_time=batch_time,
                            data_time=data_time))
    if has_gt: #val
        ious = per_class_iu(hist) * 100
        logger.info(' '.join('{:.03f}'.format(i) for i in ious))
        return round(np.nanmean(ious), 2)


def test_seg(args):
    batch_size = args.batch_size
    num_workers = args.workers
    phase = args.phase

    for k, v in args.__dict__.items():
        print(k, ':', v)

    single_model = DRNSeg(args.arch, args.classes, pretrained_model=None,
                          pretrained=False)
    if args.pretrained:
        single_model.load_state_dict(torch.load(args.pretrained))
    model = torch.nn.DataParallel(single_model).cuda()

    data_dir = args.data_dir
    info = json.load(open(join(data_dir, 'info.json'), 'r'))
    normalize = data_transforms.Normalize(mean=info['mean'], std=info['std'])
    scales = [0.5, 0.75, 1.25, 1.5, 1.75]
    if args.ms:
        dataset = SegListMS(data_dir, phase, data_transforms.Compose([
            data_transforms.ToTensor(),
            normalize,
        ]), scales, list_dir=args.list_dir)
    else:
        dataset = SegList(data_dir, phase, data_transforms.Compose([
            data_transforms.ToTensor(),
            normalize,
        ]), list_dir=args.list_dir, out_name=True)
    test_loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size, shuffle=False, num_workers=num_workers,
        pin_memory=False
    )

    cudnn.benchmark = True

    # optionally resume from a checkpoint
    start_epoch = 0
    if args.resume:
        if os.path.isfile(args.resume):
            logger.info("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            start_epoch = checkpoint['epoch']
            best_prec1 = checkpoint['best_prec1']
            model.load_state_dict(checkpoint['state_dict'])
            logger.info("=> loaded checkpoint '{}' (epoch {})"
                  .format(args.resume, checkpoint['epoch']))
        else:
            logger.info("=> no checkpoint found at '{}'".format(args.resume))

    out_dir = '{}_{:03d}_{}'.format(args.arch, start_epoch, phase)
    if len(args.test_suffix) > 0:
        out_dir += '_' + args.test_suffix
    if args.ms:
        out_dir += '_ms'

    if args.ms:
        mAP = test_ms(test_loader, model, args.classes, save_vis=True,
                      has_gt=phase != 'test' or args.with_gt,
                      output_dir=out_dir,
                      scales=scales)
    else:
        mAP = test(test_loader, model, args.classes, save_vis=True,
                   has_gt=phase != 'test' or args.with_gt, output_dir=out_dir)
    logger.info('mAP: %f', mAP)


def parse_args():
    # Training settings
    parser = argparse.ArgumentParser(description='')
    parser.add_argument('cmd', choices=['train', 'test'])
    parser.add_argument('-d', '--data-dir', default=None, required=True)
    parser.add_argument('-l', '--list-dir', default=None,
                        help='List dir to look for train_images.txt etc. '
                             'It is the same with --data-dir if not set.')
    parser.add_argument('-c', '--classes', default=0, type=int)
    parser.add_argument('-s', '--crop-size', default=0, type=int)
    parser.add_argument('--step', type=int, default=200)
    parser.add_argument('--arch')
    parser.add_argument('--batch-size', type=int, default=64, metavar='N',
                        help='input batch size for training (default: 64)')
    parser.add_argument('--epochs', type=int, default=10, metavar='N',
                        help='number of epochs to train (default: 10)')
    parser.add_argument('--lr', type=float, default=0.01, metavar='LR',
                        help='learning rate (default: 0.01)')
    parser.add_argument('--lr-mode', type=str, default='step')
    parser.add_argument('--momentum', type=float, default=0.9, metavar='M',
                        help='SGD momentum (default: 0.9)')
    parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                        metavar='W', help='weight decay (default: 1e-4)')
    parser.add_argument('-e', '--evaluate', dest='evaluate',
                        action='store_true',
                        help='evaluate model on validation set')
    parser.add_argument('--resume', default='', type=str, metavar='PATH',
                        help='path to latest checkpoint (default: none)')
    parser.add_argument('--pretrained', dest='pretrained',
                        default='', type=str, metavar='PATH',
                        help='use pre-trained model')
    parser.add_argument('--save_path', default='', type=str, metavar='PATH',
                        help='output path for training checkpoints')
    parser.add_argument('--save_iter', default=1, type=int,
                        help='number of training iterations between'
                             'checkpoint history saves')
    parser.add_argument('-j', '--workers', type=int, default=8)
    parser.add_argument('--load-release', dest='load_rel', default=None)
    parser.add_argument('--phase', default='val')
    parser.add_argument('--random-scale', default=0, type=float)
    parser.add_argument('--random-rotate', default=0, type=int)
    parser.add_argument('--bn-sync', action='store_true')
    parser.add_argument('--ms', action='store_true',
                        help='Turn on multi-scale testing')
    parser.add_argument('--with-gt', action='store_true')
    parser.add_argument('--test-suffix', default='', type=str)
    parser.add_argument('--use-loss-prediction-al',
                        dest='use_loss_prediction_al',
                        default=False, type=bool,
                        help='If True, will use loss prediction active learning algorithm.')
    parser.add_argument('--choose_images_with_highest_loss',
                        dest='choose_images_with_highest_loss',
                        default=False, type=bool,
                        help='If True, will use ground truth labels to select the images with highest loss.')
    parser.add_argument('--lamda', default=1, type=float,
                        help='Loss prediction active learning loss weight')
    parser.add_argument('--use-discriminative-al',
                        dest='use_discriminative_al',
                        default=False, type=bool,
                        help='If True, will use discriminative active learning algorithm.')
    parser.add_argument('--output_superannotate_csv_file',
                        required=False,
                        type=str,
                        default=None,
                        help='Path to the output csv file with the selected indices. Can be uploaded to annotate.online.')

    args = parser.parse_args()

    assert args.classes > 0

    print(' '.join(sys.argv))
    print(args)

    if args.bn_sync:
        drn.BatchNorm = batchnormsync.BatchNormSync

    return args


def main():
    args = parse_args()
    if args.cmd == 'train':
        train_seg(args)
    elif args.cmd == 'test':
        test_seg(args)


if __name__ == '__main__':
    main()
