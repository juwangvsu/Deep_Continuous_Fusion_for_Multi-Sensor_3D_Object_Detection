import numpy as np
from numpy import random
from numpy.core.fromnumeric import argmax

import torch
import torch.nn as nn
import torch.optim as optim
from torchvision.utils import save_image
from torch.nn.parallel import DistributedDataParallel as DDP

import os
import argparse
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import time

from data_import_carla import CarlaDataset
from loss import LossTotal
from model import LidarBackboneNetwork, ObjectDetection_DCF
from data_import import putBoundingBox
from IOU import get_3d_box, box3d_iou
from separation_axis_theorem import get_vertice_rect, separating_axis_theorem

import yaml


class Test:
    def __init__(self, pre_trained_net, config):
        """
        configuration
        nms_iou_score_theshold (0.01)
        plot_AP_graph (False)
        """
        self.net = pre_trained_net
        self.config = config
        self.net.eval()
        self.num_TP_set = {}
        self.num_TP_set_per_predbox = []
        self.num_T = 0
        self.num_P = 0
        self.IOU_threshold = [0.5, 0.55, 0.6, 0.65, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95]
        self.loss_total = LossTotal(config)
        for iou_threshold in self.IOU_threshold:
            self.num_TP_set[iou_threshold] = 0

    def get_num_T(self):
        return self.num_T

    def get_num_P(self):
        return self.num_P

    def get_num_TP_set(self):
        return self.num_TP_set

    def save_feature_result(self,bev_image, ref_bboxes, num_ref_bboxes, i, epoch, dir="./result"):
        B = ref_bboxes.shape[0]
        file_list = os.listdir("./")
        if not "result" in file_list:
            os.mkdir(dir)
        file_list = os.listdir(dir)
        if not "epoch_{}".format(epoch) in file_list:
            os.mkdir(dir+"/epoch_{}".format(epoch))
        ref_bboxes = ref_bboxes.cpu().clone().numpy()
        num_ref_bboxes = num_ref_bboxes.cpu().clone().numpy()
        for b in range(B):
            save_image(self.pred_cls[b, 1, :, :], dir+"/epoch_{}/{}_in_{}_positive_image.png".format(epoch,i,b ))
            save_image(self.pred_cls[b, 0, :, :], dir+"/epoch_{}/{}_in_{}_negative_image.png".format(epoch,i,b))
            bev_image_ = 0.5*bev_image[b].permute(1,2,0)
            bev_image_with_bbox = putBoundingBox(bev_image_, self.refined_bbox[b], self.config, color="green").permute(2,0,1).type(torch.float)
            save_image(bev_image_with_bbox, dir+"/epoch_{}/{}_in_{}_bev_image_with_predbbox.png".format(epoch,i,b))
            
            bev_image_with_bbox = putBoundingBox(bev_image_, ref_bboxes[b,:num_ref_bboxes[b]], self.config, color="red").permute(2,0,1).type(torch.float)
            save_image(bev_image_with_bbox, dir+"/epoch_{}/{}_in_{}_bev_image_with_refbbox.png".format(epoch,i,b))

    def get_eval_value_onestep(self, lidar_voxel, camera_image, ref_bboxes, num_ref_bboxes):
        
        pred = self.net(lidar_voxel, camera_image)
        pred_cls, pred_reg, pred_bbox_f = torch.split(pred,[4, 14, 14], dim=1)
        self.pred_cls = pred_cls.cpu().clone().detach()
        pred_bbox_f = pred_bbox_f.cpu().clone().detach()
        self.loss_value = self.loss_total(ref_bboxes.cuda(), num_ref_bboxes, pred_cls, pred_reg)
        pred_bboxes = self.get_bboxes(self.pred_cls, pred_bbox_f, score_threshold=self.config["score_threshold"]) # shape: b * list[tensor(N * 7)]
        # self.refined_bbox = self.NMS_IOU(pred_bboxes, nms_iou_score_theshold=self.config["nms_iou_threshold"]) # shape: b * list[N *list[tensor(7)]]
        self.refined_bbox = self.NMS_SAT(pred_bboxes) # shape: b * list[N *list[tensor(7)]]
        self.precision_recall_singleshot(self.refined_bbox, ref_bboxes) # single batch
    
    def get_bboxes(self, pred_cls, pred_reg, score_threshold=0.8):
        """
        get bounding box score threshold instead of selecting bounding box
        """
        B, C_cls, W, H = pred_cls.shape
        B, C_reg, W, H = pred_reg.shape
        anchor_numb = int(C_cls/2)
        reg_channel_per_anc = int(C_reg/anchor_numb)
        selected_bboxes_batch =[]
        for b in range(B):
            selected_bboxes = []
            for a in range (anchor_numb):
                cls_pos = anchor_numb * a + 1
                reg_cha = reg_channel_per_anc * a
                pred_cls_= pred_cls[b,cls_pos].view(-1) > score_threshold
                indices = torch.nonzero(pred_cls_).view(-1)
                pred_reg_ = pred_reg[b, reg_cha:reg_cha+reg_channel_per_anc, :, :].view((reg_channel_per_anc,-1))
                selected_bboxes_ = pred_reg_[:,indices].permute(1,0)
                selected_bboxes += [selected_bboxes_]
            selected_bboxes_batch.append(torch.cat(selected_bboxes, dim=0))
        return selected_bboxes_batch

    def NMS_IOU(self, pred_bboxes, nms_iou_score_theshold=0.01):
        filtered_bboxes_batch = []
        B = len(pred_bboxes)
        for b in range(B):
            filtered_bboxes = []
            filtered_bboxes_index = []
            print("pred bbox: ", pred_bboxes[b].shape[0])
            for i in range(pred_bboxes[b].shape[0]):
                bbox = pred_bboxes[b][i]
                if len(filtered_bboxes) == 0:
                    filtered_bboxes.append(bbox)
                    continue
                center = bbox[:3].numpy()
                box_size = bbox[3:6].numpy()
                heading_angle = bbox[6].numpy()
                cand_bbox_corners = get_3d_box(center, box_size, heading_angle)
                j =0
                for selected_bbox in filtered_bboxes:
                    j +=1
                    center_ = selected_bbox[:3].numpy()+0.0001
                    box_size_ = selected_bbox[3:6].numpy()
                    heading_angle_ = selected_bbox[6].numpy()
                    selected_bbox_corners = get_3d_box(center_, box_size_, heading_angle_)
                    (IOU_3d, IOU_2d) = box3d_iou(cand_bbox_corners, selected_bbox_corners)
                    if IOU_3d > nms_iou_score_theshold:
                        break
                    else:
                        if j == len(filtered_bboxes):
                            filtered_bboxes.append(bbox)
            filtered_bboxes_batch.append(filtered_bboxes)
        return filtered_bboxes_batch
        
    def NMS_SAT(self, pred_bboxes):
        # IOU vs SAT(separate axis theorem)
        filtered_bboxes_batch = []
        B = len(pred_bboxes)
        for b in range(B):
            filtered_bboxes = []
            filtered_bboxes_index = []
            # if pred_bboxes[b].shape[0] == 0:
            #     filtered_bboxes_batch.append(None)
            #     continue
            for i in range(pred_bboxes[b].shape[0]):
                bbox = pred_bboxes[b][i]
                if len(filtered_bboxes) == 0:
                    filtered_bboxes.append(bbox)
                    continue
                center = bbox[:3].numpy()
                box_size = bbox[3:6].numpy()
                heading_angle = bbox[6].numpy()
                cand_bbox_corners = get_vertice_rect(center, box_size, heading_angle)
                j = 0
                for selected_bbox in filtered_bboxes:
                    j += 1
                    center_ = selected_bbox[:3].numpy()
                    box_size_ = selected_bbox[3:6].numpy()
                    heading_angle_ = selected_bbox[6].numpy()
                    selected_bbox_corners = get_vertice_rect(center_, box_size_, heading_angle_)
                    is_overlapped = separating_axis_theorem(cand_bbox_corners, selected_bbox_corners)
                    if is_overlapped:
                        break
                    else:
                        if j == len(filtered_bboxes):
                            filtered_bboxes.append(bbox)
            filtered_bboxes_batch.append(filtered_bboxes)
        return filtered_bboxes_batch
        
    def precision_recall_singleshot(self, pred_bboxes, ref_bboxes):
        B,_,_ = ref_bboxes.shape
        for b in range(B):
            pred_bboxes_sb = pred_bboxes[b]
            ref_bboxes_sb = ref_bboxes[b]
            if pred_bboxes_sb != None:
                for pred_bbox in pred_bboxes_sb:
                    self.num_P += 1
                    center = pred_bbox[:3].numpy()
                    box_size = pred_bbox[3:6].numpy()
                    heading_angle = pred_bbox[6].numpy()
                    pred_bbox_corners = get_3d_box(center, box_size, heading_angle)
                    true_positive_cand_score = {}
                    for ref_bbox in ref_bboxes_sb:
                        if ref_bbox[-1] == 1:
                            center_ = ref_bbox[:3].numpy()
                            box_size_ = ref_bbox[3:6].numpy()
                            heading_angle_ = ref_bbox[6].numpy()
                            ref_bbox_corners = get_3d_box(center_, box_size_, heading_angle_)
                            (IOU_3d, IOU_2d) = box3d_iou(pred_bbox_corners, ref_bbox_corners)
                            for iou_threshold in self.IOU_threshold:
                                if IOU_2d > iou_threshold:
                                    true_positive_cand_score[iou_threshold] = IOU_2d
                    for iou_threshold in self.IOU_threshold:
                        if iou_threshold in true_positive_cand_score:
                            self.num_TP_set[iou_threshold] += 1
                    self.num_TP_set_per_predbox.append(self.num_TP_set)
            for ref_bbox_ in ref_bboxes_sb:
                if ref_bbox_[-1] == 1:
                    self.num_T += 1
        
    def display_average_precision(self, plot_AP_graph=False):
        """
        need to IOU threshold varying 
        """
        total_precision = {}
        total_recall = {}
        for iou_threshold in self.IOU_threshold:
            total_precision[iou_threshold] = self.num_TP_set[iou_threshold] / (self.num_P + 0.01)
            total_recall[iou_threshold] = self.num_TP_set[iou_threshold] / (self.num_T + 0.01)
        # print("Total Precision: ", total_precision)
        # print("Total Recall: ", total_recall)
        precisions = {}
        recalls = {}
        num_P = 0
        for iou_threshold in self.IOU_threshold:
            precisions[iou_threshold] = [1]
            recalls[iou_threshold] = [0]
        for num_tp_set in self.num_TP_set_per_predbox:
            num_P+=1
            for iou_threshold in self.IOU_threshold:
                precisions[iou_threshold].append(num_tp_set[iou_threshold] / num_P)
                recalls[iou_threshold].append(num_tp_set[iou_threshold] / self.num_T)
        if plot_AP_graph:
            fig = plt.figure()
            ax = fig.add_subplot(111)
            lines = []
            for iou_threshold in self.IOU_threshold:
                line = 0
                if len(recalls[iou_threshold]) > 1: 
                    line = ax.plot(recalls[iou_threshold], precisions[iou_threshold])
                else:
                    line = ax.plot([0,0])
                lines.append(line)
            fig.legend(lines, labels=self.IOU_threshold, title="IOU threshold value")
            fig.savefig('ap_result/test.png')

    def initialize_ap(self):
        self.num_TP_set = {}
        self.num_T = 0
        self.num_P = 0
        self.num_TP_set_per_predbox = []
        for iou_threshold in self.IOU_threshold:
            self.num_TP_set[iou_threshold] = 0


if __name__ == '__main__':
    CONFIG_PATH = "./config/"
    config_name = "config_carla.yaml"
    with open(os.path.join(CONFIG_PATH, config_name)) as file:
        config = yaml.safe_load(file)

    parser = argparse.ArgumentParser(description='deep continuous fusion training')
    parser.add_argument('--data', type=str, default="carla", help='Data type, choose "carla" or "kitti"')
    parser.add_argument('--cuda', type=str, default="0", help="list of cuda visible device number. you can choose 0~7 in list. [EX] --cuda 0,3,4")
    parser.add_argument('--port', type=str, default='12233', help="master port number. defaut is 12233")
    args = parser.parse_args()
    dataset_category = args.data
    cuda_vis_dev_str = args.cuda
    master_port = args.port
    print(cuda_vis_dev_str)
    device_id_source = cuda_vis_dev_str.split(",")
    device_id = [i for i in range(len(device_id_source))]
    os.environ['CUDA_VISIBLE_DEVICES'] = cuda_vis_dev_str
    os.environ['MASTER_ADDR'] = 'localhost'
    os.environ['MASTER_PORT'] = master_port


    torch.distributed.init_process_group(backend='nccl', world_size=1, rank=0)
    # Focus on test dataset
    if dataset_category == "carla":
        dataset = CarlaDataset(config, mode="test",want_bev_image=True)
        print("carla dataset is used for training")
    elif dataset_category =="kitti":
        dataset = KittiDataset(mode="test")
        print("kitti dataset is used for training")
    print("dataset is ready")
    data_loader = torch.utils.data.DataLoader(dataset,
                                          batch_size=2,
                                          shuffle=True)
    # Load pre-trained model. you can use the model during training instead of test_model 
    test_model = ObjectDetection_DCF(config).cuda()
    test_model = DDP(test_model,device_ids=device_id, output_device=0, find_unused_parameters=True)
    test_model.load_state_dict(torch.load("./saved_model/model"))
    test = Test(test_model)
    data_length = len(dataset)
    loss_value = None

    for batch_ndx, sample in enumerate(data_loader):
        print("batch_ndx is ", batch_ndx)
        print("sample keys are ", sample.keys())
        print("bbox shape is ", sample["bboxes"].shape)
        print("image shape is ", sample["image"].shape)
        print("pointcloud shape is ", sample["pointcloud"].shape)
        test_index = np.random.randint(data_length)
        image_data = sample['image'].cuda()
        point_voxel = sample['pointcloud'].cuda()
        reference_bboxes = sample['bboxes'].cpu().clone().detach()
        num_ref_bboxes = sample['num_bboxes']
        bev_image = sample['lidar_bev_2Dimage']
        
        # evaluate AP in one image and voxel lidar
        test.get_eval_value_onestep(point_voxel, image_data, reference_bboxes, num_ref_bboxes)
        test.save_feature_result(bev_image, reference_bboxes, num_ref_bboxes, batch_ndx, 99)
        print("accumulated number of true data is ", test.get_num_T())
        print("accumulated number of positive data is ", test.get_num_P())
        print("accumulated number of true positive data is ", test.get_num_TP_set())
        print("="*50)
        if batch_ndx > 10:
            break

    # display average-precision plot and mAP
    test.display_average_precision(plot_AP_graph=True)
    # MUST DO WHEN U DISPLAY ALL OF RESULTS
    test.initialize_ap()
    
