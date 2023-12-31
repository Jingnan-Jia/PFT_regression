# -*- coding: utf-8 -*-
# @Time    : 7/5/21 9:27 AM
# @Author  : Jingnan
# @Email   : jiajingnan2222@gmail.com

from lung_function.modules.networks.cnn_fc3d import Cnn3fc1, Cnn3fc2, Cnn4fc2, Cnn5fc2, Cnn6fc2, Vgg11_3d, Vgg16_3d, Vgg19_3d
from lung_function.modules.networks.cnn_fc3d_enc import Cnn3fc1Enc, Cnn3fc2Enc, Cnn4fc2Enc, Cnn5fc2Enc, Cnn6fc2Enc, Vgg11_3dEnc
from lung_function.modules.networks.vit3 import ViT3
from lung_function.modules.networks.combined_net import CombinedNet
from torch import nn
import torch
import torchvision
import importlib
import sys
from torchsummary import summary

sys.path.append("/home/jjia/data/lung_function/lung_function/modules/networks/models_pcd")
# from openpoints.models import build_model_from_cfg
# from openpoints.utils import EasyConfig
from mlflow import log_params
# from lung_function.modules.ulip.models import ULIP_models 
# from lung_function.modules.ulip.models.pointmlp.pointMLP import pointMLP
# from lung_function.modules.ulip.models.pointnet2.pointnet2 import Pointnet2_Ssg
# from lung_function.modules.ulip.models.pointbert.point_encoder import PointTransformer
# from lung_function.modules.ulip.models.pointnext.pointnext import PointNEXT
import torch.nn.functional as F

class MLP_reg(nn.Module):
    def __init__(self, num_classes,  fc_ls=[1024, 512, 256]):
        super().__init__()
        self.num_classes = num_classes
        self.fc_ls = fc_ls
        
        self.fc1 = nn.Linear(fc_ls[0], fc_ls[1])
        self.dp_fc1 = nn.Dropout(p=0.4)
        self.bn1 = nn.BatchNorm1d(fc_ls[1])
        if len(fc_ls) == 2:
            self.fc2 = nn.Linear(fc_ls[1], num_classes)
        elif len(fc_ls) == 3:            
            self.fc2 = nn.Linear(fc_ls[1], fc_ls[2])
            self.bn2 = nn.BatchNorm1d(fc_ls[2])
            self.dropout = nn.Dropout(p=0.4)
            self.fc3 = nn.Linear(fc_ls[2], num_classes)
        else:      
            raise Exception(f"wrong fc_ls: {fc_ls}, the length should be 2 or 3")
        

    def forward(self, x):  # x shape: (B,N)

        x = F.relu(self.bn1(self.dp_fc1(self.fc1(x))))
        if len(self.fc_ls) == 2:
            x = self.fc2(x)
        elif len(self.fc_ls) == 3:
            x = F.relu(self.bn2(self.dropout(self.fc2(x))))
            x = self.fc3(x)
        
        else:
            raise Exception(f"wrong fc_ls: {self.fc_ls}, the length should be 2 or 3")

        return x


def modify_stride_inplace(net, old_stride=(1,2,2), new_stride=(2,2,2)):
    for name, module in net.named_children():
        # 检查是否是卷积层以及步长是否匹配
        if isinstance(module, nn.Conv3d) and module.stride == old_stride:
            # 创建一个具有相同参数但不同步长的新卷积层
            new_module = nn.Conv3d(
                in_channels=module.in_channels, 
                out_channels=module.out_channels, 
                kernel_size=module.kernel_size, 
                stride=new_stride, 
                padding=module.padding, 
                dilation=module.dilation, 
                groups=module.groups, 
                bias=(module.bias is not None)
            )
            # 复制权重和偏置
            new_module.weight = module.weight
            new_module.bias = module.bias

            # 用新的卷积层替换原有层
            setattr(net, name, new_module)
        else:
            # 递归处理子模块
            modify_stride_inplace(module, old_stride, new_stride)


def get_net_3d(name: str,
               nb_cls: int,
               fc1_nodes=1024, 
               fc2_nodes=1024, 
               image_size=240, 
               pretrained=True, 
               pointnet_fc_ls=None,
               loss=None,
               dp_fc1_flag=False, 
               args=None):
    level_node = 0
    # if 'ulip' in name or 'ULIP' in name:  # the code is from the point-ulip author
    #     if 'PointMLP' in name:
    #         net = pointMLP()
    #     elif 'Pointnet2_SSG' in name:
    #         net = Pointnet2_Ssg()
    #     elif 'PointBERT' in name:
    #         net = PointTransformer()
    #     elif 'PointNext' in name:
    #         net = PointNEXT()
    #     else:
    #         raise Exception('Unknown point network')
            
        # net = getattr(ULIP_models, args.net)(args=args)
        # args.model: 'ULIP_PN_SSG', 'ULIP_PN_NEXT', 'ULIP_PN_MLP', 'ULIP_PointBERT'
    if '-' in args.net: # two networks
        net = CombinedNet(args)

    elif name == 'mlp_reg':
        net = MLP_reg(num_classes=nb_cls, fc_ls=[args.PNB, 512, 256])
    elif name == 'pointmlp_reg':
        from pointmlp_reg import pointMLP
        net = pointMLP(num_classes=nb_cls, points=1024)
    # elif name == 'pointbert_reg':
    #     from pointbert_reg import pointMLP
            
    elif 'point' in name:  # the code is from the pointnet++ author
        # if name=='pointnet_reg':
        def inplace_relu(m):
            classname = m.__class__.__name__
            if classname.find('ReLU') != -1:
                m.inplace = True
        pcd_model = importlib.import_module(name)
        
        
            
        if name=='pointnet_reg':  

            net = pcd_model.get_model(nb_cls, pointnet_fc_ls, loss, dp_fc1_flag)
        else:  # pointnet++=pointnet2
            
            net = pcd_model.get_model(nb_cls, args.in_channel,
                                      npoint_base=args.npoint_base, 
                                      radius_base=args.radius_base, 
                                      nsample_base=args.nsample_base)
        net.apply(inplace_relu)
        # elif name=='pointnet2_reg':
    # elif 'pointnext' in name:  # the code is from the pointnext author
    #     cfg = EasyConfig()
    #     cfg_fpath = "/home/jjia/data/lung_function/lung_function/modules/cfgs/" + args.cfg
    #     cfg.load(cfg_fpath, recursive=True)  # args.cfs is the path of the cfg file
    #     cfg.radius = args.radius_base
    #     cfg.radius_scaling = args.radius_scaling
    #     cfg.sa_layers = args.sa_layers
    #     cfg.nsample = args.nsample_base
    #     cfg.num_classes = nb_cls
    #     cfg.width = args.width
    #     net = build_model_from_cfg(cfg.model)  # pass a config set to this function to build a model
    

    elif name == 'cnn3fc1':
        net = Cnn3fc1(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                      num_classes=nb_cls, level_node=level_node)
    elif name == 'cnn3fc2':
        net = Cnn3fc2(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                      num_classes=nb_cls, level_node=level_node)
    elif name == 'cnn4fc2':
        net = Cnn4fc2(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                      num_classes=nb_cls, level_node=level_node)
    elif name == 'cnn5fc2':
        net = Cnn5fc2(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                      num_classes=nb_cls, level_node=level_node)
    elif name == 'cnn6fc2':
        net = Cnn6fc2(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                      num_classes=nb_cls, level_node=level_node)
    elif name == "vgg11_3d":
        net = Vgg11_3d(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                       num_classes=nb_cls, level_node=level_node)
    elif name == "vgg16_3d":
        net = Vgg16_3d(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                       num_classes=nb_cls, level_node=level_node)
    elif name == "vgg19_3d":
        net = Vgg19_3d(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                       num_classes=nb_cls, level_node=level_node)
    elif name == "vit3":
        net = ViT3(dim=1024, image_size=image_size, patch_size=20,
                   num_classes=nb_cls, depth=6, heads=8, mlp_dim=2048, channels=1)
    elif name in ("r3d_18", "r2plus1d_18"):
        # class NewStem(nn.Sequential):
        #     """The new conv-batchnorm-relu stem"""
        #     def __init__(self) -> None:
        #         super().__init__(
        #             nn.Conv3d(1, 64, kernel_size=(3, 7, 7), stride=(1, 2, 2), padding=(1, 3, 3), bias=False),
        #             nn.BatchNorm3d(64),
        #             nn.ReLU(inplace=True),
        #         )
        if name == "r3d_18":
            net = torchvision.models.video.r3d_18(
                pretrained=pretrained, progress=True)
        else:
            net = torchvision.models.video.r2plus1d_18(
                pretrained=pretrained, progress=True)
        # net.stem = NewStem()
        net.stem[0] = nn.Conv3d(1, 64, kernel_size=(
            3, 7, 7), stride=(1, 2, 2), padding=(1, 3, 3), bias=False)
        net.fc = torch.nn.Linear(512 * 1, nb_cls)
    elif name in ('slow_r50', 'slowfast_r50', "x3d_xs", "x3d_s", "x3d_m", "x3d_l"):
        if name == "slow_r50":
            # Christoph et al, “SlowFast Networks for Video Recognition” https://arxiv.org/pdf/1812.03982.pdf
            net = torch.hub.load(
                'facebookresearch/pytorchvideo', name, pretrained=pretrained)
            net.blocks[0].conv = nn.Conv3d(1, 64, kernel_size=(
                1, 7, 7), stride=(1, 2, 2), padding=(0, 3, 3), bias=False)
            if '-' not in args.input_mode:  # combined_run
                net.blocks[-1].proj = nn.Linear(in_features=2048, out_features=nb_cls, bias=True)
        elif name == "slowfast_r50":
            net = torch.hub.load(
                'facebookresearch/pytorchvideo', name, pretrained=pretrained)
            net.blocks[0].multipathway_blocks[0].conv = nn.Conv3d(
                1, 8, kernel_size=(5, 7, 7), stride=(1, 2, 2), padding=(2, 3, 3), bias=False)
            net.blocks[0].multipathway_blocks[1].conv = nn.Conv3d(
                1, 8, kernel_size=(5, 7, 7), stride=(1, 2, 2), padding=(2, 3, 3), bias=False)
            if '-' not in args.input_mode:  # combined_run
                net.blocks[-1].proj = nn.Linear(in_features=2048, out_features=nb_cls, bias=True)
            
        else:          
            net = torch.hub.load( 'facebookresearch/pytorchvideo', name, pretrained=pretrained, head_activation=None, head_output_with_global_average=True)
            modify_stride_inplace(net)
            
            net.blocks[0].conv.conv_t = nn.Conv3d(1, 24, kernel_size=( 3, 3, 3), stride=(2, 2, 2), padding=(1, 1, 1), bias=False)

            # net.blocks[-1].pool.pool = nn.AdaptiveAvgPool3d(1)  # reinitialize the weights
            net.blocks[-1].pool.pool = nn.AvgPool3d(kernel_size=(7, 7, 7), stride=1, padding=0)  # change from 16, 7, 7 to 7, 7, 7
            # net.blocks[-1].pool.post_conv = nn.Conv3d(432, 8192, kernel_size=( 1, 1, 1), stride=(1, 1, 1), bias=False) 
            # net.blocks[-1].pool.post_conv = nn.Sequential(nn.Flatten(1),nn.Linear(in_features=432, out_features=8192, bias=True))
            # net.blocks[-1].dropout = nn.Dropout(0)
            net.blocks[-1].proj = nn.Linear(in_features=2048, out_features=nb_cls, bias=True)  # only command previously
            
            # net.blocks[-1].output_pool = nn.AdaptiveAvgPool3d(1)
            # del net.blocks[-1].activation
            # net.blocks[-1].activation = nn.ReLU()
                
               
        # net.blocks[-1].output_pool = nn.Linear(in_features=400, out_features=nb_cls, bias=True)

    else:
        raise Exception('wrong net name', name)

    # device = torch.device("cuda")
    # net = net.to(device)
    summary(net, (1, 240, 240, 240))
    
    return net


def get_net_pos_enc(name: str, nb_cls: int, fc1_nodes=1024, fc2_nodes=1024, level_node=0):
    if name == 'cnn3fc1':
        net = Cnn3fc1Enc(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                         num_classes=nb_cls, level_node=level_node)
    elif name == 'cnn3fc2':
        net = Cnn3fc2Enc(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                         num_classes=nb_cls, level_node=level_node)
    elif name == 'cnn4fc2':
        net = Cnn4fc2Enc(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                         num_classes=nb_cls, level_node=level_node)
    elif name == 'cnn5fc2':
        net = Cnn5fc2Enc(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                         num_classes=nb_cls, level_node=level_node)
    elif name == 'cnn6fc2':
        net = Cnn6fc2Enc(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                         num_classes=nb_cls, level_node=level_node)
    elif name == "vgg11_3d":
        net = Vgg11_3dEnc(fc1_nodes=fc1_nodes, fc2_nodes=fc2_nodes,
                          num_classes=nb_cls, level_node=level_node)
    else:
        raise Exception('wrong net name', name)

    return net
