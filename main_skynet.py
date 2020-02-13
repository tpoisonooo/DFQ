import torch
from modeling.detection.skynet import *
from torch.utils.data import Dataset
import torchvision.transforms as transforms
from PIL import Image
import argparse
import pathlib
import numpy as np
import logging
import sys
import time
import os

parser = argparse.ArgumentParser(description="SkyNet Evaluation on DAC dataset.")
parser.add_argument("--quantize", action='store_true')
parser.add_argument("--equalize", action='store_true')
parser.add_argument("--correction", action='store_true')
parser.add_argument("--absorption", action='store_true')
parser.add_argument("--distill_range", action='store_true')
parser.add_argument("--log", action='store_true')
parser.add_argument("--relu", action='store_true')
parser.add_argument("--clip_weight", action='store_true')
parser.add_argument("--trainable", action='store_true')
parser.add_argument("--equal_range", type=float, default=1e8)
parser.add_argument("--bits_weight", type=int, default=8)
parser.add_argument("--bits_activation", type=int, default=8)
parser.add_argument("--bits_bias", type=int, default=16)
parser.add_argument("--batch_size", type=int, default=32)
parser.add_argument('--workers', type=int, default=10, metavar='N')
args = parser.parse_args()
DEVICE = torch.device("cuda:0")


def convert2cpu(gpu_matrix):
    return torch.FloatTensor(gpu_matrix.size()).copy_(gpu_matrix)

class DACDataset(Dataset):
    def __init__(self, root, shape=None, transform=None, batch_size=32, num_workers=4):
        self.files = [file for file in os.listdir(root) if os.path.isfile(os.path.join(root, file))]
        self.imageNames = [file.split('.')[0] for file in self.files]
        self.files = [os.path.join(root, file) for file in self.files]
        self.nSamples = len(self.files)
        self.transform = transform
        self.shape = shape
        self.batch_size = batch_size
        self.num_workers = num_workers
       
    def __len__(self):
        return self.nSamples

    def __getitem__(self, index):
        imgpath = self.files[index]
        img = Image.open(imgpath).convert('RGB')
        if self.shape:
            img = img.resize(self.shape)

        if self.transform is not None:
            img = self.transform(img)

        return img

imgDir = '/media/DATASET/DAC/val/jpg'
batch_size = args.batch_size

if __name__ == '__main__':
    assert args.relu or args.relu == args.equalize, 'must replace relu6 to relu while equalization'
    assert args.equalize or args.absorption == args.equalize, 'must use absorption with equalize'

    model = SkyNet()
    model.load_state_dict(torch.load('./modeling/detection/SkyNet.pth'))
    model = model.to(DEVICE)

    init_width = model.width
    init_height = model.height
    dataset = DACDataset(imgDir, shape=(init_width, init_height),
                         transform=transforms.Compose([
                                 transforms.ToTensor(),
                                 transforms.Normalize(mean=[0.5, 0.5, 0.5], std=[0.25, 0.25, 0.25]),
                        ]))
    test_loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
        num_workers=args.workers, pin_memory=True)

    # model = model.cuda()
    model.eval()
    print("Start Inference")

    anchors = model.anchors
    num_anchors = model.num_anchors
    anchor_step = len(anchors) // num_anchors
    h = 20
    w = 40
    total = 0
    imageNum = dataset.__len__()
    results = np.zeros((imageNum, 5))

    grid_x = torch.linspace(0, w - 1, w).repeat(h, 1).repeat(batch_size * num_anchors, 1, 1).view(batch_size * num_anchors * h * w).cuda()
    grid_y = torch.linspace(0, h - 1, h).repeat(w, 1).t().repeat(batch_size * num_anchors, 1, 1).view(batch_size * num_anchors * h * w).cuda()
    anchor_w = torch.Tensor(anchors).view(num_anchors, anchor_step).index_select(1, torch.LongTensor([0]))
    anchor_h = torch.Tensor(anchors).view(num_anchors, anchor_step).index_select(1, torch.LongTensor([1]))
    anchor_w = anchor_w.repeat(batch_size, 1).repeat(1, 1, h * w).view(batch_size * num_anchors * h * w).cuda()
    anchor_h = anchor_h.repeat(batch_size, 1).repeat(1, 1, h * w).view(batch_size * num_anchors * h * w).cuda()
    sz_hw = h * w
    sz_hwa = sz_hw * num_anchors

    stime = time.time()
    for batch_idx, data in enumerate(test_loader):
        data = data.cuda()
        output = model(data).data
        batch = output.size(0)
        output = output.view(batch * num_anchors, 5, h * w).transpose(0, 1).contiguous().view(5, batch * num_anchors * h * w)

        if batch != batch_size:
            # print("Last batch")
            grid_x = torch.linspace(0, w - 1, w).repeat(h, 1).repeat(batch * num_anchors, 1, 1).view(batch * num_anchors * h * w).cuda()
            grid_y = torch.linspace(0, h - 1, h).repeat(w, 1).t().repeat(batch * num_anchors, 1, 1).view(batch * num_anchors * h * w).cuda()
            anchor_w = torch.Tensor(anchors).view(num_anchors, anchor_step).index_select(1, torch.LongTensor([0]))
            anchor_h = torch.Tensor(anchors).view(num_anchors, anchor_step).index_select(1, torch.LongTensor([1]))
            anchor_w = anchor_w.repeat(batch, 1).repeat(1, 1, h * w).view(batch * num_anchors * h * w).cuda()
            anchor_h = anchor_h.repeat(batch, 1).repeat(1, 1, h * w).view(batch * num_anchors * h * w).cuda()

        det_confs = torch.sigmoid(output[4])
        det_confs = convert2cpu(det_confs)

        for b in range(batch):
            det_confs_inb = det_confs[b * sz_hwa:(b + 1) * sz_hwa].numpy()
            ind = np.argmax(det_confs_inb)

            xs_inb = torch.sigmoid(output[0, b * sz_hwa + ind]) + grid_x[b * sz_hwa + ind]
            ys_inb = torch.sigmoid(output[1, b * sz_hwa + ind]) + grid_y[b * sz_hwa + ind]
            ws_inb = torch.exp(output[2, b * sz_hwa + ind]) * anchor_w[b * sz_hwa + ind]
            hs_inb = torch.exp(output[3, b * sz_hwa + ind]) * anchor_h[b * sz_hwa + ind]

            bcx = xs_inb.item() / w
            bcy = ys_inb.item() / h
            bw = ws_inb.item() / w
            bh = hs_inb.item() / h

            xmin = bcx - bw / 2.0
            ymin = bcy - bh / 2.0
            xmax = xmin + bw
            ymax = ymin + bh

            results[total + b, 1:] = np.asarray([xmin * 640, xmax * 640, ymin * 360, ymax * 360])

        total += batch
    etime = time.time()

    results[:, 0] = dataset.imageNames
    index = np.argsort(results[:, 0])

    file = open('./result/result.txt','w+')
    for i in range(len(index)):
        name = '%03d'%int(results[index[i]][0])+'.jpg '
        bbox = '['+str(int(round(results[index[i]][1])))+', '+str(int(round(results[index[i]][2])))+', '+str(int(round(results[index[i]][3])))+', '+str(int(round(results[index[i]][4])))+']\n'
        file.write(name+bbox)
    file.close()