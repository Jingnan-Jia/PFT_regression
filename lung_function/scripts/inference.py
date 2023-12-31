# -*- coding: utf-8 -*-
# @Time    : 4/5/22 12:25 PM
# @Author  : Jingnan
# @Email   : jiajingnan2222@gmail.com
# log_dict is used to record super parameters and metrics

import sys
import random
import statistics
import threading
import time
from pathlib import Path

import mlflow
import numpy as np
import torch
import torch.nn as nn
from medutils import medutils
from medutils.medutils import count_parameters
from mlflow import log_metric, log_metrics, log_param, log_params
from mlflow.tracking import MlflowClient
from monai.utils import set_determinism
from typing import List, Sequence
from argparse import Namespace
import functools
import thop
import os
import copy
import pandas as pd
from glob import glob

from lung_function.modules import provider
from lung_function.modules.compute_metrics import icc, metrics
from lung_function.modules.datasets import all_loaders
from lung_function.modules.loss import get_loss
from lung_function.modules.networks import get_net_3d
from lung_function.modules.path import PFTPath
from lung_function.modules.set_args import get_args
from lung_function.modules.tool import record_1st, dec_record_cgpu, retrive_run
from lung_function.modules.trans import batch_bbox2_3D
import sys
# sys.path.append("/home/jjia/data/lung_function/lung_function/modules/networks/models_pcd")
sys.path.append("/home/jjia/data/lung_function/lung_function/modules")

args = get_args()
args.pretrained_id = '3006-3007-3008-3009'
global_lock = threading.Lock()


def thread_safe(func):
    def thread_safe_fun(*args, **kwargs):
        with global_lock:
            print('get lock by main thread')
            func(*args, **kwargs)
            print('release lock by main thread')
    return thread_safe_fun


def try_func(func):
    def _try_fun(*args, **kwargs):
        try:
            func(*args, **kwargs)
        except Exception as err:
            print(err, file=sys.stderr)
            pass
    return _try_fun


log_metric = try_func(log_metric)
log_metrics = try_func(log_metrics)


def reinit_fc(net, nb_fc0, fc1_nodes, fc2_nodes, num_classes):
    net.ln1 = nn.Linear(nb_fc0, fc1_nodes)
    net.rl1 = nn.ReLU(inplace=True)
    net.dp1 = nn.Dropout()
    net.ln2 = nn.Linear(fc1_nodes, fc2_nodes)
    net.rl2 = nn.ReLU(inplace=True)
    net.dp2 = nn.Dropout()
    net.ln3 = nn.Linear(fc2_nodes, num_classes)
    return net


def int2str(batch_id: np.ndarray) -> np.ndarray:
    """_summary_

    Args:
        batch_id (np.ndarray): _description_

    Raises:
        Exception: _description_

    Returns:
        np.ndarray: _description_
    """
    tmp = batch_id.shape
    id_str_ls = []
    for id in batch_id:
        if isinstance(id, np.ndarray):
            id = id[0]
        id = str(id)
        while len(id) < 7:  # the pat id should be 7 digits
            id = '0' + id
        if len(tmp) == 2:
            id_str_ls.append([id])
        elif len(tmp) == 1:
            id_str_ls.append(id)
        else:
            raise Exception(
                f"the shape of batch_id is {tmp}, but it should be 1-dim or 2-dim")

    return np.array(id_str_ls)


class Run:
    """A class which has its dataloader and step_iteration. It is like Lighting. 
    """

    def __init__(self, args: Namespace, dataloader_flag=True):
        self.args = args
        self.mypath = PFTPath(args.id, check_id_dir=False, space=args.ct_sp)
        self.device = torch.device("cuda")  # 'cuda'
        self.target = [i.lstrip() for i in args.target.split('-')]

        self.pointnet_fc_ls = [int(i) for i in args.pointnet_fc_ls.split('-')]

        self.net = get_net_3d(name=args.net, nb_cls=len(self.target), image_size=args.x_size,
                              pretrained=args.pretrained_imgnet, pointnet_fc_ls=self.pointnet_fc_ls, loss=args.loss,
                              dp_fc1_flag=args.dp_fc1_flag, args=args)  # output FVC and FEV1
        self.fold = args.fold
        self.flops_done = False

        print('net:', self.net)

        net_parameters = count_parameters(self.net)
        net_parameters = str(round(net_parameters / 1e6, 2))
        log_param('net_parameters_M', net_parameters)

        self.loss_fun = get_loss(
            args.loss, mat_diff_loss_scale=args.mat_diff_loss_scale)
        if args.adamw:
            self.opt = torch.optim.AdamW(self.net.parameters(), lr=args.lr, weight_decay=args.weight_decay)

        else:
            self.opt = torch.optim.Adam( self.net.parameters(), lr=args.lr, weight_decay=args.weight_decay)
        if args.cosine_decay:
            self.scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(self.opt, T_max=10, eta_min=0, last_epoch=-1, verbose=False)

        self.net = self.net.to(self.device)

        validMAEEpoch_AllBest = 1000
        args.pretrained_id = str(args.pretrained_id)
        if args.pretrained_id != '0':
            if 'SSc' in args.pretrained_id:  # pretrained by ssc_pos L-Net weights
                pretrained_id = args.pretrained_id.split(
                    '-')[self.fold]  # [852] [853] [854] [855]
                pretrained_model_path = f"/home/jjia/data/ssc_scoring/ssc_scoring/results/models_pos/{pretrained_id}/model.pt"
                print(f"pretrained_model_path: {pretrained_model_path}")
                ckpt = torch.load(pretrained_model_path,
                                  map_location=self.device)
                print(f"model is loaded arom {pretrained_model_path}")

                del ckpt['ln3.weight']
                del ckpt['ln3.bias']
                del self.net.ln3  # remove the last layer because they do not match

                # model_fpath need to exist
                self.net.load_state_dict(ckpt, strict=False)
                self.net = reinit_fc(self.net, nb_fc0=8 * 16 * 6 * 6 * 6, fc1_nodes=1024, fc2_nodes=1024,
                                     num_classes=len(self.target))
                # move the new initialized layers to GPU
                self.net = self.net.to(self.device)
                print(f"use the pretrained model from {pretrained_model_path}")

            else:
                if '-' in args.pretrained_id:
                    pretrained_ids = args.pretrained_id.split('-')
                    args.pretrained_id = pretrained_ids[self.fold-1]

                pretrained_path = PFTPath(args.pretrained_id, check_id_dir=False, space=args.ct_sp)
                ckpt = torch.load(pretrained_path.model_fpath, map_location=self.device)
                print(f"model is loaded arom {pretrained_path.model_fpath}")

                if type(ckpt) is dict and 'model' in ckpt:
                    model = ckpt['model']
                    # if 'metric_name' in ckpt:  # not applicable if the pre-trained model is from ModelNet40
                    #     if 'validMAEEpoch_AllBest' == ckpt['metric_name']:
                    #         validMAEEpoch_AllBest = ckpt['current_metric_value']
                    client = mlflow.MlflowClient()
                    experiment = mlflow.get_experiment_by_name("lung_fun_db15")
                    pre_run =  client.search_runs(experiment_ids=[experiment.experiment_id], filter_string=f"params.id = '{str(args.pretrained_id)}'")[0]
                    if ('dataset' in pre_run.data.params) and (pre_run.data.params['dataset'] in ['modelnet40']):   # pre-trained by an classification dataset
                        assert pre_run.data.params['net'] == self.args.net
                        if 'pointmlp_reg' == self.args.net:
                            model = {k:v for k,v in model.items() if 'classifier' != k.split('.')[0]}
                        elif 'pointnet2_reg' == self.args.net:
                            # model = {k:v for k,v in model.items() if 'fc1' in k }
                            excluded_keys = ['fc1', 'bn1', 'drop1', 'fc2', 'bn2', 'drop2', 'fc3']  # FC layers
                            model = {key: value for key, value in model.items() if all(excluded_key not in key for excluded_key in excluded_keys)}

                              
                else:
                    model = ckpt
                # model_fpath need to exist
                # strict=false due to the calculation of FLOPs and params. In addition, the pre-trained model may be a 
                # classification model with different output nodes
                self.net.load_state_dict(model, strict=False)  
                # move the new initialized layers to GPU
                self.net = self.net.to(self.device)
        if dataloader_flag:
            self.data_dt = all_loaders(self.mypath.data_dir, self.mypath.label_fpath, args)

        self.BestMetricDt = {'trainLossEpochBest': 1000,
                             # 'trainnoaugLossEpochBest': 1000,
                             'validLossEpochBest': 1000,
                             'testLossEpochBest': 1000,

                             'trainMAEEpoch_AllBest': 1000,
                             # 'trainnoaugMAEEpoch_AllBest': 1000,
                             'validMAEEpoch_AllBest': validMAEEpoch_AllBest,
                             'testMAEEpoch_AllBest': 1000,
                             }

    def step(self, mode, epoch_idx, save_pred=False, suffix=None):
        dataloader = self.data_dt[mode]
        loss_fun_mae = nn.L1Loss()

        scaler = torch.cuda.amp.GradScaler()
        print(mode + "ing ......")
        self.net.eval()

        t0 = time.time()
        data_idx = 0
        loss_accu = 0
        mae_accu_ls = [0]
        mae_accu_all = 0
        for data in dataloader:
            if 8365740 not in data['pat_id']:
                continue
            torch.cuda.empty_cache()  # avoid memory leak
            data_idx += 1
            if epoch_idx < 3:  # only show first 3 epochs' data loading time
                t1 = time.time()
                log_metric('TLoad', t1 - t0, data_idx +
                           epoch_idx * len(dataloader))
            key = args.input_mode

 
            if args.input_mode in ['vessel_skeleton_pcd', 'lung_mask_pcd']:  # first 3 columns are xyz, last 1 is value
                points = data[key].data.numpy()
                points = provider.random_point_dropout(points)
             
                points[:, :, 0:3] = provider.shift_point_cloud(
                    points[:, :, 0:3], shift_range=args.shift_range)
                points = torch.Tensor(points)
                
                if 'pointnext' in args.net:  # data input for pointnext shoudl be split to two parts
                    # 'pos' shape: Batch, N, 3;  'x' shape: Batch, 3+1, N
                    data[key] = {'pos': points[:, :, :3], 'x': points.transpose(2, 1)}

            if args.input_mode in ['vessel_skeleton_pcd', 'lung_mask_pcd']:
                batch_x = data[key]  # n, c, z, y, x
            elif args.input_mode == 'modelnet40_pcd':  # ModelNet, ShapeNet
                batch_x = data[0]
            else:
                pass
            
            
            if 'pointnext' in args.net:  # data input for pointnext shoudl be split to two parts
                batch_x['pos'] = batch_x['pos'].to(self.device)
                batch_x['x'] = batch_x['x'].to(self.device)  # n, z, y, x
            else:
                batch_x = batch_x.to(self.device)  # n, z, y, x
                
            if args.input_mode in ['vessel_skeleton_pcd', 'lung_mask_pcd']:
                batch_y = data['label'].to(self.device)
            else:  # ModelNet, ShapeNet
                batch_y = data[1].to(self.device)
            
            if 'pcd' == args.input_mode[-3:]:  #TODO: 
                batch_x = batch_x.permute(0, 2, 1) # from b, n, d to b, d, n	

            with torch.cuda.amp.autocast():
                with torch.no_grad():
                    print('batch_x ori', batch_x[:, -1, :])
                    batch_min = 0 # torch.min(batch_x[:, -1, :])
                    batch_x[:, -1, :] = batch_x[:, -1, :] - (0.5 * float(suffix))
                    batch_x[:, -1, :][batch_x[:, -1, :]<batch_min] = batch_min
                    print('batch_x after', batch_x[:, -1, :])

                    pred = self.net(batch_x)
                    print(f"pred: {pred}")
                
            if save_pred:
                head = ['pat_id']
                head.extend(self.target)

                batch_pat_id = data['pat_id'].cpu(
                ).detach().numpy()  # shape (N,1)
                batch_pat_id = int2str(batch_pat_id)  # shape (N,1)

                batch_y_np = batch_y.cpu().detach().numpy()  # shape (N, out_nb)
                pred_np = pred.cpu().detach().numpy()  # shape (N, out_nb)
                

                saved_label = np.hstack((batch_pat_id, batch_y_np))
                saved_pred = np.hstack((batch_pat_id, pred_np))
                
                if suffix not in (None, 0, '0'):
                    pred_fpath = self.mypath.save_pred_fpath(mode).replace('.csv', '_increase_r'+ suffix + 'mm.csv')
                    label_fpath = self.mypath.save_label_fpath(mode).replace('.csv', '_increase_r'+ suffix + 'mm.csv')
                else:
                    pred_fpath = self.mypath.save_pred_fpath(mode)
                    label_fpath = self.mypath.save_label_fpath(mode)
                    
                medutils.appendrows_to(label_fpath, saved_label, head=head)
                medutils.appendrows_to(pred_fpath, saved_pred, head=head)

            loss = self.loss_fun(pred, batch_y)
            
            with torch.no_grad():
                mae_ls = [loss]
                mae_all = loss.item()

                    
            loss_cpu = loss.item()
            print('loss:', loss_cpu)
            loss_accu += loss_cpu
            for i, mae in enumerate(mae_ls):
                mae_accu_ls[i] += mae
            mae_accu_all += mae_all

            if epoch_idx < 3:
                t2 = time.time()
                log_metric('TUpdateWBatch', t2-t1, data_idx +
                           epoch_idx*len(dataloader))
                t0 = t2  # reset the t0

        log_metric(mode+'LossEpoch', loss_accu/len(dataloader), epoch_idx)
        log_metric(mode+'MAEEpoch_All', mae_accu_all / len(dataloader), epoch_idx)
        for t, i in zip(self.target, mae_accu_ls):
            log_metric(mode + 'MAEEpoch_' + t, i / len(dataloader), epoch_idx)

        self.BestMetricDt[mode + 'LossEpochBest'] = min( self.BestMetricDt[mode+'LossEpochBest'], loss_accu/len(dataloader))
        tmp = self.BestMetricDt[mode+'MAEEpoch_AllBest']
        self.BestMetricDt[mode + 'MAEEpoch_AllBest'] = min( self.BestMetricDt[mode+'MAEEpoch_AllBest'], mae_accu_all/len(dataloader))

        log_metric(mode+'LossEpochBest', self.BestMetricDt[mode + 'LossEpochBest'], epoch_idx)
        log_metric(mode+'MAEEpoch_AllBest', self.BestMetricDt[mode + 'MAEEpoch_AllBest'], epoch_idx)

        if self.BestMetricDt[mode+'MAEEpoch_AllBest'] == mae_accu_all/len(dataloader):
            for t, i in zip(self.target, mae_accu_ls):
                log_metric(mode + 'MAEEpoch_' + t + 'Best',
                           i / len(dataloader), epoch_idx)


@dec_record_cgpu(args.outfile)
def run(args: Namespace):
    """
    Run the whole  experiment using this args.
    """
    myrun = Run(args)
    for mode in ['test']:
        args.mode = mode
        for i in range(10):
            myrun.step(mode,  0,  save_pred=True, suffix=str(i))
    
    
    print('Finish all things!')

        
def main():
    SEED = 4
    set_determinism(SEED)  # set seed for this run

    torch.manual_seed(SEED)
    torch.cuda.manual_seed_all(SEED)
    torch.cuda.manual_seed(SEED)

    random.seed(SEED)
    np.random.seed(SEED)

    mlflow.set_tracking_uri("http://nodelogin02:5000")
    experiment = mlflow.set_experiment("lung_fun_db15")
    
    RECORD_FPATH = f"{Path(__file__).absolute().parent}/results/record.log"
    # write super parameters from set_args.py to record file.
    id = record_1st(RECORD_FPATH)

    with mlflow.start_run(run_name=str(id), tags={"mlflow.note.content": args.remark}):
        args.id = id  # do not need to pass id seperately to the latter function
        args.set_all_r_to_1 = False
        

        current_id = id
        tmp_args_dt = vars(args)
        tmp_args_dt['fold'] = 'all'
        log_params(tmp_args_dt)

        all_folds_id_ls = []
        for fold in [1, 2, 3, 4]:
            # write super parameters from set_args.py to record file.

            id = record_1st(RECORD_FPATH)
            all_folds_id_ls.append(id)
            with mlflow.start_run(run_name=str(id) + '_fold_' + str(fold), tags={"mlflow.note.content": f"fold: {fold}"}, nested=True):
                args.fold = fold
                args.pretrained_id = get_args().pretrained_id
                args.id = id  # do not need to pass id seperately to the latter function
                tmp_args_dt = vars(args)
                log_params(tmp_args_dt)
                run(args)
        

if __name__ == "__main__":
    main()
