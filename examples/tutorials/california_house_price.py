import argparse
import os.path as osp
from typing import List

import pandas as pd
import torch
import torch.nn.functional as F
from torch import Tensor
from tqdm import tqdm
# Please run `pip install transformers` to install the package
from transformers import AutoModel, AutoTokenizer

import torch_frame
from torch_frame import stype
from torch_frame.config.text_embedder import TextEmbedderConfig
from torch_frame.config.text_tokenizer import TextTokenizerConfig
from torch_frame.data import DataLoader
from torch_frame.data.mapper import TextEmbeddingTensorMapper
from torch_frame.datasets import MultimodalTextBenchmark
from torch_frame.nn import (
    EmbeddingEncoder,
    FTTransformer,
    LinearEncoder,
    MultiCategoricalEmbeddingEncoder,
)
from torch_frame.nn.encoder.stype_encoder import LinearEmbeddingEncoder, LinearModelEncoder, TimestampEncoder
from torch_frame.typing import NAStrategy, TensorData, TextTokenizationOutputs

parser = argparse.ArgumentParser()
parser.add_argument("--dataset", type=str, default="california_house_price")
parser.add_argument("--channels", type=int, default=256)
parser.add_argument("--num_layers", type=int, default=4)
parser.add_argument("--batch_size", type=int, default=512)
parser.add_argument("--lr", type=float, default=0.0001)
parser.add_argument("--epochs", type=int, default=100)
parser.add_argument("--seed", type=int, default=0)
parser.add_argument("--model", type=str,
                    default="sentence-transformers/all-distilroberta-v1")
parser.add_argument("--pooling", type=str, default="mean",
                    choices=["mean", "cls"])
parser.add_argument("--compile", action="store_true")
args = parser.parse_args()

torch.manual_seed(args.seed)
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

class SimpleTextToEmbedding:
    def __init__(self, model: str, device: torch.device):
        self.tokenizer = AutoTokenizer.from_pretrained(model)
        self.model = AutoModel.from_pretrained(model).to(device)
        self.device = device

    def __call__(self, sentences) -> Tensor:
        inputs = self.tokenizer(
            sentences,
            truncation=True,
            padding="max_length",
            return_tensors="pt",
        )
        for key in inputs:
            if isinstance(inputs[key], Tensor):
                inputs[key] = inputs[key].to(self.device)
        out = self.model(**inputs)
        return out.last_hidden_state[:, 0, :].detach().cpu()


text_encoder = SimpleTextToEmbedding(model=args.model, device=device)

kwargs = {
        "text_stype": stype.text_embedded,
        "col_to_text_embedder_cfg": TextEmbedderConfig(
            text_embedder=text_encoder,
            batch_size=5,
        ),
    }

# Prepare datasets
path = osp.join(osp.dirname(osp.realpath(__file__)), "..", "..", "data",
                args.dataset)

# Load Dataset
dataset = MultimodalTextBenchmark(root=path, name=args.dataset, num_rows=5000, **kwargs)

model_name = args.model.replace('/', '')
filename = f"{model_name}_data.pt"
dataset.materialize(path=osp.join(path, filename))
dataset.tensor_frame.col_names_dict[stype.embedding] = dataset.tensor_frame.col_names_dict.pop(stype.text_embedded)
dataset.tensor_frame.feat_dict[stype.embedding] = dataset.tensor_frame.feat_dict.pop(stype.text_embedded)

is_classification = dataset.task_type.is_classification

train_dataset, val_dataset, test_dataset = dataset[:0.8], dataset[0.8:0.9], dataset[0.9:]
# Set up data loaders
train_tensor_frame = train_dataset.tensor_frame

val_tensor_frame = val_dataset.tensor_frame
test_tensor_frame = test_dataset.tensor_frame
train_loader = DataLoader(train_tensor_frame, batch_size=args.batch_size,
                          shuffle=True)
val_loader = DataLoader(val_tensor_frame, batch_size=args.batch_size)
test_loader = DataLoader(test_tensor_frame, batch_size=args.batch_size)

stype_encoder_dict = {
    stype.categorical: EmbeddingEncoder(),
    stype.numerical: LinearEncoder(),
    stype.embedding: LinearEmbeddingEncoder(),
    stype.multicategorical: MultiCategoricalEmbeddingEncoder(),
    stype.timestamp: TimestampEncoder(na_strategy=NAStrategy.MEDIAN_TIMESTAMP)
}

if is_classification:
    output_channels = dataset.num_classes
else:
    output_channels = 1

model = FTTransformer(
    channels=args.channels,
    out_channels=output_channels,
    num_layers=args.num_layers,
    col_stats=dataset.col_stats,
    col_names_dict=train_tensor_frame.col_names_dict,
    stype_encoder_dict=stype_encoder_dict,
).to(device)
model = torch.compile(model, dynamic=True) if args.compile else model
optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)


def train(epoch: int) -> float:
    model.train()
    loss_accum = total_count = 0

    for tf in tqdm(train_loader, desc=f"Epoch: {epoch}"):
        tf = tf.to(device)
        pred = model(tf)
        if is_classification:
            loss = F.cross_entropy(pred, tf.y)
        else:
            loss = F.mse_loss(pred.view(-1), tf.y.view(-1))
        optimizer.zero_grad()
        loss.backward()
        loss_accum += float(loss) * len(tf.y)
        total_count += len(tf.y)
        optimizer.step()
    return loss_accum / total_count


@torch.no_grad()
def test(loader: DataLoader) -> float:
    model.eval()
    accum = total_count = 0

    for tf in loader:
        tf = tf.to(device)
        pred = model(tf)
        if is_classification:
            pred_class = pred.argmax(dim=-1)
            accum += float((tf.y == pred_class).sum())
        else:
            accum += float(
                F.mse_loss(pred.view(-1), tf.y.view(-1), reduction="sum"))
        total_count += len(tf.y)

    if is_classification:
        accuracy = accum / total_count
        return accuracy
    else:
        rmse = (accum / total_count)**0.5
        return rmse


if is_classification:
    metric = "Acc"
    best_val_metric = 0
    best_test_metric = 0
else:
    metric = "RMSE"
    best_val_metric = float("inf")
    best_test_metric = float("inf")

for epoch in range(1, args.epochs + 1):
    train_loss = train(epoch)
    train_metric = test(train_loader)
    val_metric = test(val_loader)
    test_metric = test(test_loader)

    if is_classification and val_metric > best_val_metric:
        best_val_metric = val_metric
        best_test_metric = test_metric
    elif not is_classification and val_metric < best_val_metric:
        best_val_metric = val_metric
        best_test_metric = test_metric

    print(f"Train Loss: {train_loss:.4f}, Train {metric}: {train_metric:.4f}, "
          f"Val {metric}: {val_metric:.4f}, Test {metric}: {test_metric:.4f}")

print(f"Best Val {metric}: {best_val_metric:.4f}, "
      f"Best Test {metric}: {best_test_metric:.4f}")
