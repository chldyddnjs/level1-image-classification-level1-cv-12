import argparse
import glob
import json
import multiprocessing
import os
import random
import re
from importlib import import_module
from pathlib import Path

import matplotlib.pyplot as plt
from PIL import Image
import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torchvision.models as models
from torch.optim.lr_scheduler import StepLR
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter
from efficientnet_pytorch import EfficientNet
from sklearn.metrics import f1_score, classification_report

import torchvision
from torchvision import transforms
from torchvision.transforms import *

import albumentations as A
from albumentations.pytorch import ToTensorV2

from dataset_ensemble import MaskBaseDataset, MaskSplitByProfileDataset
from loss import create_criterion

# ========== Error analysis code ==========
def save_best_val_pred(y_true:np.array, y_pred:np.array, dir_model:str):
    df_val_pred = pd.DataFrame({'true': y_true,
                                'pred': y_pred}
                              )
    
    df_val_pred.to_csv(dir_model + '/pred_result.csv', index=False)   


def save_f1_result(epoch:int, report:dict, dir_model:str, save_best=True):
    df_f1_rslt = pd.DataFrame(columns=['precision', 'recall', 'f1'],
                              index=np.arange(0, 18),
                              dtype=np.float32
                              )
    
    for idx in df_f1_rslt.index:
        k = str(idx)
        df_f1_rslt.loc[idx, :] = report[k]['precision'], report[k]['recall'], report[k]['f1-score']
    
    if save_best:
        df_f1_rslt.to_csv(dir_model + '/f1_result.csv', index=False) 
    else:
        df_f1_rslt.to_csv(dir_model + f'/f1_result_epoch_{epoch}.csv', index=False) 
# ========== Error analysis code ==========

def seed_everything(seed):
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)  # if use multi-GPU
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
    np.random.seed(seed)
    random.seed(seed)


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']


def grid_image(np_images, gts, preds, n=16, shuffle=False):
    batch_size = np_images.shape[0]
    assert n <= batch_size

    choices = random.choices(range(batch_size), k=n) if shuffle else list(range(n))
    figure = plt.figure(figsize=(12, 18 + 2))  # cautions: hardcoded, 이미지 크기에 따라 figsize 를 조정해야 할 수 있습니다. T.T
    plt.subplots_adjust(top=0.8)               # cautions: hardcoded, 이미지 크기에 따라 top 를 조정해야 할 수 있습니다. T.T
    n_grid = np.ceil(n ** 0.5)
    tasks = ["mask", "gender", "age"]
    for idx, choice in enumerate(choices):
        gt = gts[choice].item()
        pred = preds[choice].item()
        image = np_images[choice]
        # title = f"gt: {gt}, pred: {pred}"
        gt_decoded_labels = MaskBaseDataset.decode_multi_class(gt)
        pred_decoded_labels = MaskBaseDataset.decode_multi_class(pred)
        title = "\n".join([
            f"{task} - gt: {gt_label}, pred: {pred_label}"
            for gt_label, pred_label, task
            in zip(gt_decoded_labels, pred_decoded_labels, tasks)
        ])

        plt.subplot(n_grid, n_grid, idx + 1, title=title)
        plt.xticks([])
        plt.yticks([])
        plt.grid(False)
        plt.imshow(image, cmap=plt.cm.binary)

    return figure

def select_model(model, num_classes=18):
    if model == 'resnet18':
        model_ = models.resnet18(pretrained=True)
        model_.classifier = nn.Linear(1024, num_classes)
    elif model == 'densenet161':
        model_ = models.densenet161(pretrained=True)
        model_.classifier = nn.Linear(2208, num_classes)
    elif model == 'shufflenet':
        model_ = torch.hub.load('pytorch/vision:v0.10.0', 'shufflenet_v2_x1_0', pretrained=True)
        model_.fc = nn.Linear(1024, num_classes)
    elif model == 'efficientnet':
        model_ = EfficientNet.from_pretrained('efficientnet-b0',num_classes=num_classes)
    return model_


def increment_path(path, exist_ok=False):
    """ Automatically increment path, i.e. runs/exp --> runs/exp0, runs/exp1 etc.

    Args:
        path (str or pathlib.Path): f"{model_dir}/{args.name}".
        exist_ok (bool): whether increment path (increment if False).
    """
    path = Path(path)
    if (path.exists() and exist_ok) or (not path.exists()):
        return str(path)
    else:
        dirs = glob.glob(f"{path}*")
        matches = [re.search(rf"%s(\d+)" % path.stem, d) for d in dirs]
        i = [int(m.groups()[0]) for m in matches if m]
        n = max(i) + 1 if i else 2
        return f"{path}{n}"

# torchvision transform
# def get_transforms(need=('train', 'val'), img_size=(512, 384)):
#     transformations = {}
#     if 'train' in need:
#         transformations['train'] = torchvision.transforms.Compose([
#             Resize((224,224),Image.BILINEAR),
#             RandomRotation([-8, +8]),
#             ColorJitter(brightness=0.5, saturation=0.5, hue=0.5),  # todo : param
#             ToTensor(),
#             Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
#         ])
#     if 'val' in need:
#         transformations['val'] = torchvision.transforms.Compose([
#             Resize((224,224),Image.BILINEAR),
#             ToTensor(),
#             Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225]),
#         ])
#     return transformations    
    
# albumentations    
def get_transforms(need=('train', 'val'), img_size=(512, 384)):
    transformations = {}
    if 'train' in need:
        transformations['train'] = A.Compose([
            A.Resize(224,224, p=1.0),
            A.HorizontalFlip(p=0.5),
            A.ShiftScaleRotate(p=0.5,rotate_limit=15),
#             A.HueSaturationValue(hue_shift_limit=0.2, sat_shift_limit=0.2, val_shift_limit=0.2, p=0.5),
#             A.RandomBrightnessContrast(brightness_limit=(-0.1, 0.1), contrast_limit=(-0.1, 0.1), p=0.5),
#             A.GaussNoise(p=0.5),
            A.CoarseDropout(max_holes=20,p=1),
            A.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225], max_pixel_value=255.0, p=1.0),
            ToTensorV2(p=1.0),
        ], p=1.0)
    if 'val' in need:
        transformations['val'] = A.Compose([
            A.Resize(224,224, p=1.0),
            A.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225], max_pixel_value=255.0, p=1.0),
            ToTensorV2(p=1.0),
        ], p=1.0)
    return transformations    

def train(data_dir, model_dir, args):
    seed_everything(args.seed)

    # -- settings
    use_cuda = torch.cuda.is_available()
    device = torch.device("cuda" if use_cuda else "cpu")

    # -- start K-fold cross-validation
    for i in range(5):
        save_dir = increment_path(os.path.join(model_dir, args.name))
        dataset = MaskSplitByProfileDataset(data_dir=data_dir,cv=i)
        num_classes = dataset.num_classes  # 18

        # -- data_loader
        transform = get_transforms()
        train_set, val_set = dataset.split_dataset()

        train_loader = DataLoader(
            train_set,
            batch_size=args.batch_size,
            num_workers=multiprocessing.cpu_count()//2,
            shuffle=True,
            pin_memory=use_cuda,
            drop_last=True,
        )

        val_loader = DataLoader(
            val_set,
            batch_size=args.valid_batch_size,
            num_workers=multiprocessing.cpu_count()//2,
            shuffle=False,
            pin_memory=use_cuda,
            drop_last=True,
        )

        model = select_model('efficientnet', 18).to(device)
        scaler = torch.cuda.amp.GradScaler()

        # -- loss & metric
        criterion = create_criterion(args.criterion)  # default: cross_entropy
        opt_module = getattr(import_module("torch.optim"), args.optimizer)  # default: SGD
        optimizer = opt_module(
            filter(lambda p: p.requires_grad, model.parameters()),
            lr=args.lr,
            weight_decay=5e-4
        )
        scheduler = StepLR(optimizer, args.lr_decay_step, gamma=0.5)

        # -- logging
        logger = SummaryWriter(log_dir=save_dir)
        with open(os.path.join(save_dir, 'config.json'), 'w', encoding='utf-8') as f:
            json.dump(vars(args), f, ensure_ascii=False, indent=4)

        best_val_acc = 0
        best_val_loss = np.inf
        best_f1 = 0
        print('-------------------CV {} started-------------------'.format(i))
        for epoch in range(args.epochs):
            # train loop
            model.train()
            loss_value = 0
            matches = 0
            list_labels = []
            list_preds = []
            train_set.dataset.set_transform(transform['train']) # 
            for idx, train_batch in enumerate(train_loader):
                inputs, labels = train_batch
                inputs = inputs.to(device)
                labels = labels.to(device)

                optimizer.zero_grad()

                with torch.cuda.amp.autocast():
                    outs = model(inputs)
                    preds = torch.argmax(outs, dim=-1)
                    loss = criterion(outs, labels)

                list_labels.append(labels.detach().cpu().numpy())
                list_preds.append(preds.detach().cpu().numpy())

                scaler.scale(loss).backward()
                scaler.step(optimizer)
                scaler.update()

                loss_value += loss.item()
                matches += (preds == labels).sum().item()
                if (idx + 1) % args.log_interval == 0:
                    train_loss = loss_value / args.log_interval
                    train_acc = matches / args.batch_size / args.log_interval
                    tot_labels = np.concatenate(list_labels)
                    tot_preds = np.concatenate(list_preds)
                    train_f1 = f1_score(tot_labels, tot_preds, average='macro')
                    current_lr = get_lr(optimizer)
                    print(
                        f"CV {i} || Epoch[{epoch}/{args.epochs}]({idx + 1}/{len(train_loader)}) || "
                        f"train_loss {train_loss:4.4} || train_acc {train_acc:4.2%} || train_f1 {train_f1:4.2%} || lr {current_lr}"
                    )
                    logger.add_scalar("Train/loss", train_loss, epoch * len(train_loader) + idx)
                    logger.add_scalar("Train/accuracy", train_acc, epoch * len(train_loader) + idx)

                    loss_value = 0
                    matches = 0

            scheduler.step()

            # val loop
            with torch.no_grad():
                print("Calculating validation results...")
                model.eval()
                val_loss_items = []
                val_acc_items = []
                list_labels = []
                list_preds = []

                figure = None
                val_set.dataset.set_transform(transform['val'])
                for val_batch in val_loader:
                    inputs, labels = val_batch
                    inputs = inputs.to(device)
                    labels = labels.to(device)

                    outs = model(inputs)
                    preds = torch.argmax(outs, dim=-1)

                    list_labels.append(labels.detach().cpu().numpy())
                    list_preds.append(preds.detach().cpu().numpy())

                    loss_item = criterion(outs, labels).item()
                    acc_item = (labels == preds).sum().item()
                    val_loss_items.append(loss_item)
                    val_acc_items.append(acc_item)

                    if figure is None:
                        inputs_np = torch.clone(inputs).detach().cpu().permute(0, 2, 3, 1).numpy()
                        inputs_np = MaskSplitByProfileDataset.denormalize_image(inputs_np, dataset.mean, dataset.std)
                        figure = grid_image(
                            inputs_np, labels, preds, n=16, shuffle=args.dataset != "MaskSplitByProfileDataset"
                        )

                val_loss = np.sum(val_loss_items) / len(val_loader)
                val_acc = np.sum(val_acc_items) / len(val_set)
                tot_val_labels = np.concatenate(list_labels)
                tot_val_preds = np.concatenate(list_preds)
                val_f1 = f1_score(tot_val_labels, tot_val_preds, average='macro')

                if val_f1 > best_f1:
                    print(f"New best model for val f1 : {val_f1:4.2%}! saving the best model..")
                    torch.save(model.state_dict(), f"{save_dir}/best.pth")
                    best_val_loss = val_loss
                    best_val_acc = val_acc
                    best_f1 = val_f1
                # ========== Error analysis code ==========
                    print(classification_report(tot_val_labels, tot_val_preds)) # For print in terminal
                    rslt_f1_by_class = classification_report(tot_val_labels, tot_val_preds, output_dict=True) # For save as csv
                    save_f1_result(epoch, rslt_f1_by_class, save_dir, save_best=True) # Save validation classification report
                    save_best_val_pred(tot_val_labels, tot_val_preds, save_dir) # Save best validation prediction result
                # ========== Error analysis code ==========
                torch.save(model.state_dict(), f"{save_dir}/last.pth")
                print(
                    f"[Val] acc : {val_acc:4.2%}, loss: {val_loss:4.2}, f1: {val_f1:4.2%} || "
                    f"best acc : {best_val_acc:4.2%}, best loss: {best_val_loss:4.2}, f1: {best_f1:4.2%} || " 
                )
                logger.add_scalar("Val/loss", val_loss, epoch)
                logger.add_scalar("Val/accuracy", val_acc, epoch)
                logger.add_figure("results", figure, epoch)
                print()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    from dotenv import load_dotenv
    import os
    load_dotenv(verbose=True)

    parser.add_argument('--seed', type=int, default=42, help='random seed (default: 42)')
    parser.add_argument('--epochs', type=int, default=15, help='number of epochs to train (default: 1)')
    parser.add_argument('--dataset', type=str, default='MaskSplitByProfileDataset', help='dataset augmentation type (default: MaskBaseDataset)')
#     parser.add_argument('--augmentation', type=str, default='BaseAugmentation', help='data augmentation type (default: BaseAugmentation)')
#     parser.add_argument("--resize", nargs="+", type=list, default=[224, 224], help='resize size for image when training')
    parser.add_argument('--batch_size', type=int, default=64, help='input batch size for training (default: 64)')
    parser.add_argument('--valid_batch_size', type=int, default=64, help='input batch size for validing (default: 1000)')
#     parser.add_argument('--model', type=str, default='BaseModel', help='model type (default: BaseModel)')
    parser.add_argument('--optimizer', type=str, default='Adam', help='optimizer type (default: SGD)')
    parser.add_argument('--lr', type=float, default=1e-4, help='learning rate (default: 1e-3)')
    parser.add_argument('--val_ratio', type=float, default=0.2, help='ratio for validaton (default: 0.2)')
    parser.add_argument('--criterion', type=str, default='label_smoothing', help='criterion type (default: cross_entropy)')
    parser.add_argument('--lr_decay_step', type=int, default=10, help='learning rate scheduler decay step (default: 20)')
    parser.add_argument('--log_interval', type=int, default=50, help='how many batches to wait before logging training status')
    parser.add_argument('--name', default='ensem', help='model save at {SM_MODEL_DIR}/{name}')

    # Container environment
    parser.add_argument('--data_dir', type=str, default=os.environ.get('SM_CHANNEL_TRAIN', '/opt/ml/input/data/train/images'))
    parser.add_argument('--model_dir', type=str, default=os.environ.get('SM_MODEL_DIR', './model'))

    args = parser.parse_args()
    print(args)

    data_dir = args.data_dir
    model_dir = args.model_dir

    train(data_dir, model_dir, args)
