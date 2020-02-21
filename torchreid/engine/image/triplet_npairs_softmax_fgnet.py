from __future__ import absolute_import
from __future__ import print_function
from __future__ import division

import time
import datetime

import torch

import torchreid
from torchreid.engine import engine
from torchreid.losses import CrossEntropyLoss, TripletLoss, NPairsLoss
from torchreid.utils import AverageMeter, open_specified_layers, open_all_layers
from torchreid import metrics


class ImageTripletNpairsSoftmaxFGnetEngine(engine.Engine):
    r"""Triplet-loss engine for image-reid.

    Args:
        datamanager (DataManager): an instance of ``torchreid.data.ImageDataManager``
            or ``torchreid.data.VideoDataManager``.
        model (nn.Module): model instance.
        optimizer (Optimizer): an Optimizer.
        margin (float, optional): margin for triplet loss. Default is 0.3.
        weight_t (float, optional): weight for triplet loss. Default is 1.
        weight_x (float, optional): weight for softmax loss. Default is 1.
        scheduler (LRScheduler, optional): if None, no learning rate decay will be performed.
        use_gpu (bool, optional): use gpu. Default is True.
        label_smooth (bool, optional): use label smoothing regularizer. Default is True.

    Examples::
        
        import torch
        import torchreid
        datamanager = torchreid.data.ImageDataManager(
            root='path/to/reid-data',
            sources='market1501',
            height=256,
            width=128,
            combineall=False,
            batch_size=32,
            num_instances=4,
            train_sampler='RandomIdentitySampler' # this is important
        )
        model = torchreid.models.build_model(
            name='resnet50',
            num_classes=datamanager.num_train_pids,
            loss='triplet'
        )
        model = model.cuda()
        optimizer = torchreid.optim.build_optimizer(
            model, optim='adam', lr=0.0003
        )
        scheduler = torchreid.optim.build_lr_scheduler(
            optimizer,
            lr_scheduler='single_step',
            stepsize=20
        )
        engine = torchreid.engine.ImageTripletEngine(
            datamanager, model, optimizer, margin=0.3,
            weight_t=0.7, weight_x=1, scheduler=scheduler
        )
        engine.run(
            max_epoch=60,
            save_dir='log/resnet50-triplet-market1501',
            print_freq=10
        )
    """   

    def __init__(self, datamanager, model, optimizer, margin=0.3, margin_sasc=0.3, margin_sadc=0.2, margin_dasc=0.4,
                 weight_t=1, weight_x=1, weight_n=1, weight_x_parts=1, scheduler=None, use_gpu=True,
                 label_smooth=True):
        super(ImageTripletNpairsSoftmaxFGnetEngine, self).__init__(datamanager, model, optimizer, scheduler, use_gpu)

        self.weight_t = weight_t
        self.weight_x = weight_x
        self.weight_n = weight_n
        self.weight_x_parts = weight_x_parts
        
        self.criterion_t = TripletLoss(margin=margin)
        self.criterion_x = CrossEntropyLoss(
            num_classes=self.datamanager.num_train_pids,
            use_gpu=self.use_gpu,
            label_smooth=label_smooth
        )
        self.criterion_n = NPairsLoss(
            use_gpu=self.use_gpu,
            margin_sasc = margin_sasc,
            margin_sadc = margin_sadc,
            margin_dasc = margin_dasc
        )
        self.criterion_x_parts = CrossEntropyLoss(
            num_classes=self.datamanager.num_train_pids,
            use_gpu=self.use_gpu,
            label_smooth=label_smooth
        )

    def train(self, epoch, max_epoch, trainloader, fixbase_epoch=0, open_layers=None, print_freq=10):
        losses_t = AverageMeter()
        losses_x = AverageMeter()
        losses_n = AverageMeter()
        losses_x_parts = AverageMeter()
        accs = AverageMeter()
        batch_time = AverageMeter()
        data_time = AverageMeter()

        self.model.train()
        if (epoch+1)<=fixbase_epoch and open_layers is not None:
            print('* Only train {} (epoch: {}/{})'.format(open_layers, epoch+1, fixbase_epoch))
            open_specified_layers(self.model, open_layers)
        else:
            open_all_layers(self.model)

        num_batches = len(trainloader)
        end = time.time()
        for batch_idx, data in enumerate(trainloader):
            data_time.update(time.time() - end)

            imgs, pids = self._parse_data_for_train(data)
            if self.use_gpu:
                imgs = imgs.cuda()
                pids = pids.cuda()
            
            self.optimizer.zero_grad()
            outputs, features, parts, part_weights, outputs_parts = self.model(imgs)
            loss_t = self._compute_loss(self.criterion_t, features, pids)
            loss_x = self._compute_loss(self.criterion_x, outputs, pids)
            loss_n = self._compute_loss(self.criterion_n, parts, pids)
            loss_x_parts = self._compute_loss(self.criterion_x_parts, outputs_parts, pids)
            loss = self.weight_t * loss_t + self.weight_x * loss_x + self.weight_n * loss_n + self.weight_x_parts*loss_x_parts

            #print(self.weight_t, loss_t, self.weight_x, loss_x, self.weight_n, loss_n)
            loss.backward()
            self.optimizer.step()

            batch_time.update(time.time() - end)

            losses_t.update(loss_t.item(), pids.size(0))
            losses_x.update(loss_x.item(), pids.size(0))
            losses_n.update(loss_n.item(), pids.size(0))
            losses_x_parts.update(loss_x_parts.item(), pids.size(0))
            accs.update(metrics.accuracy(outputs, pids)[0].item())

            if (batch_idx+1) % print_freq == 0:
                # estimate remaining time
                eta_seconds = batch_time.avg * (num_batches-(batch_idx+1) + (max_epoch-(epoch+1))*num_batches)
                eta_str = str(datetime.timedelta(seconds=int(eta_seconds)))
                print('Epoch: [{0}/{1}][{2}/{3}]\t'
                      'Time {batch_time.val:.3f} ({batch_time.avg:.3f})\t'
                      'Data {data_time.val:.3f} ({data_time.avg:.3f})\t'
                      'Loss_t {loss_t.val:.4f} ({loss_t.avg:.4f})\t'
                      'Loss_x {loss_x.val:.4f} ({loss_x.avg:.4f})\t'
                      'Loss_n {loss_n.val:.4f} ({loss_n.avg:.4f})\t'
                      'Loss_x_parts {loss_x_parts.val:.4f} ({loss_x_parts.avg:.4f})\t'
                      'Acc glob{acc.val:.2f} ({acc.avg:.2f})\t'
                      'Lr {lr:.6f}\t'
                      'eta {eta}'.format(
                      epoch+1, max_epoch, batch_idx+1, num_batches,
                      batch_time=batch_time,
                      data_time=data_time,
                      loss_t=losses_t,
                      loss_x=losses_x,
                      loss_n=losses_n,
                      loss_x_parts=losses_x_parts,
                      acc=accs,
                      lr=self.optimizer.param_groups[0]['lr'],
                      eta=eta_str
                    )
                )

            if self.writer is not None:
                n_iter = epoch * num_batches + batch_idx
                self.writer.add_scalar('Train/Time', batch_time.avg, n_iter)
                self.writer.add_scalar('Train/Data', data_time.avg, n_iter)
                self.writer.add_scalar('Train/Loss_t', losses_t.avg, n_iter)
                self.writer.add_scalar('Train/Loss_x', losses_x.avg, n_iter)
                self.writer.add_scalar('Train/Loss_n', losses_n.avg, n_iter)
                self.writer.add_scalar('Train/Loss_x_parts', losses_x_parts.avg, n_iter)
                self.writer.add_scalar('Train/Acc glob', accs.avg, n_iter)
                self.writer.add_scalar('Train/Lr', self.optimizer.param_groups[0]['lr'], n_iter)

                #log part weights
                for i, (_, weights) in enumerate(part_weights):#parts
                    for j in range(min(weights.size(0), 3)):#batch
                        self.writer.add_histogram('Train/Part{}/Batch{}'.format(i, j), weights[j, ...], n_iter)

            end = time.time()

        if self.scheduler is not None:
            self.scheduler.step()
