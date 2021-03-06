#%%
import sys

from collections import OrderedDict

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, Dataset

from sklearn.preprocessing import StandardScaler
from imblearn.over_sampling import SMOTE
import numpy as np
import pandas as pd

import flwr as fl
sys.path.append(".")
from importlib import import_module
if len(sys.argv) > 2:
    config = import_module(sys.argv[2])
else: 
    import config

if len(sys.argv) < 2:
    print('Error: client number needed')
    exit()
    
DEVICE = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')
#%%
class FraudDataset(Dataset):
    def __init__(self, x, y):
        self.x = x
        self.y = y

    def __getitem__(self, index):
        return self.x[index], self.y[index]

    def __len__(self):
        return len(self.x)

def load_data(path, initial_split, train_split, test_split, initial_test, columns, batch_size=128, label=30): # 'data/creditcard.csv', 2000, 3000, 1:30
    df = pd.read_csv(path)
    df = df.sample(frac=1, random_state=config.random)
    x_train = df.iloc[initial_split:train_split, 0:-1].values.astype(np.float32)
    y_train = df.iloc[initial_split:train_split, -1].values.astype(np.float32)
    sc = StandardScaler()
    x_train = sc.fit_transform(x_train)
    if config.resample:
        x_train, y_train = SMOTE(random_state=config.random).fit_resample(x_train, y_train)
    x_test = df.iloc[initial_test:test_split, 0:-1].values.astype(np.float32)
    x_test = sc.transform(x_test)
    y_test = df.iloc[initial_test:test_split, -1].values.astype(np.float32)
    trainset = FraudDataset(x_train, y_train)
    testset = FraudDataset(x_test, y_test)
    trainloader = DataLoader(trainset, batch_size=batch_size, shuffle=False)
    testloader = DataLoader(testset, batch_size=batch_size)
    num_examples = {'trainset' : len(trainset), 'testset' : len(testset)}
    return trainloader, testloader, num_examples


shared_columns = [col for col in config.client1['columns'] if col in config.client2['columns']]

if sys.argv[1] == '1':
    trainloader, testloader, num_examples = load_data(config.path, **config.client1)
    ind_columns = [col for col in config.client1['columns'] if col not in shared_columns]

if sys.argv[1] == '2':
    trainloader, testloader, num_examples = load_data(config.path, **config.client2)
    ind_columns = [col for col in config.client2['columns'] if col not in shared_columns]
# %%

def fed_train(shared_model, shared_opt, trainloader, epochs):
    criterion = nn.BCELoss()
    loss = 0.0
    for _ in range(epochs):
        tp, fp, tn, fn = 0, 0, 0, 0
        for x, y in trainloader:
            x, y = x.to(DEVICE), y.to(DEVICE)
            shared_opt.zero_grad()
            outputs = shared_model(x[:,shared_columns])
            outputs = outputs.view(-1)
            loss = criterion(outputs, y)
            loss.backward()
            shared_opt.step()
            preds = np.round_(outputs.detach().numpy())
            for lab, pred in zip(y, preds):
                # Collect statistics
                tp += (pred and lab)
                fp += (pred and not lab)
                tn += (not pred and not lab)
                fn += (not pred and lab)
        f1_score = tp / (tp + (fp + fn)/2)
        print(f'\rTRAIN tp: {tp}, fp: {fp}, tn: {tn}, fn: {fn} | F1 score: {f1_score:.4f} \t Loss: {loss:.4f}', end='')

def fed_test(shared_model, testloader):
    criterion = nn.BCELoss()
    loss = 0.0
    tp, fp, tn, fn = 0, 0, 0, 0
    with torch.no_grad():
        for x, y in testloader:
            x, y = x.to(DEVICE), y.to(DEVICE)
            outputs = shared_model(x[:,shared_columns])
            outputs = outputs.view(-1)
            loss += criterion(outputs, y).item()

            preds = np.round_(outputs.numpy())
            for lab, pred in zip(y, preds):
                # Collect statistics
                tp += (pred and lab)
                fp += (pred and not lab)
                tn += (not pred and not lab)
                fn += (not pred and lab)
    f1_score = tp / (tp + (fp + fn)/2)
    # print(f'TEST tp: {tp}, fp: {fp}, tn: {tn}, fn: {fn} | F1 score: {f1_score:.4f} \t Loss: {loss:.4f}')
    return loss, f1_score

class Net(nn.Module):
    def __init__(self, sizes) -> None:
        super(Net, self).__init__()
        self.last = sizes[-1]
        self.sizes = sizes
        modules = []
        for i in range(len(sizes)-1):
            modules.append(nn.Linear(sizes[i], sizes[i+1]))
            if i < len(sizes)-2 or self.last > 1:
                modules.append(nn.ReLU())
                modules.append(nn.Dropout(config.dropout))
            else:
                modules.append(nn.Sigmoid())
        self.sequential = nn.Sequential(*modules)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x.float()
        return self.sequential(x)

    def predict(self, x):
        pred = torch.F.softmax(self.forward(x))
        return torch.tensor(pred)
class SplitNN(nn.Module):
    def __init__(self, models) -> None:
        super(SplitNN, self).__init__()
        self.ind_model = models['ind_model']
        self.shared_model = models['shared_model']
        self.agg_model = models['agg_model']
        
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        x = x.float()
        x_ind = x[:,ind_columns]
        x_shared = x[:,shared_columns]
        x_ind = self.ind_model(x_ind)
        x_shared = self.shared_model(x_shared)
        return self.agg_model(torch.cat((x_ind, x_shared), dim=1))

    def predict(self, x):
        pred = torch.F.softmax(self.forward(x))
        return torch.tensor(pred)

print(f'number of shared features: {len(shared_columns)}')
shared_model = Net([len(shared_columns), *config.shared_layers, 1])
shared_opt = torch.optim.Adam(shared_model.parameters(), lr=1e-3, weight_decay=config.weight_decay)

class FraudClient(fl.client.NumPyClient):
    def get_parameters(self):
        return [val.cpu().numpy() for _, val in shared_model.state_dict().items()]

    def set_parameters(self, parameters):
        params_dict = zip(shared_model.state_dict().keys(), parameters)
        state_dict = OrderedDict({k: torch.tensor(v) for k, v in params_dict})
        shared_model.load_state_dict(state_dict, strict=True)

    def fit(self, parameters, configuration):
        self.set_parameters(parameters)
        fed_train(shared_model, shared_opt, trainloader, epochs=config.epochs)
        return self.get_parameters(), num_examples['trainset'], {}

    def evaluate(self, parameters, configuration):
        self.set_parameters(parameters)
        loss, f1_score = fed_test(shared_model, testloader)
        return float(loss), num_examples['testset'], {'f1_score': float(f1_score)}

fl.client.start_numpy_client('[::]:8080', client=FraudClient())

# Transfer learning
shared_model.sequential = shared_model.sequential[0:-2]
if config.freeze:
    for param in shared_model.parameters(): # Freeze the shared model parameters
        param.requires_grad = False
ind_model = Net([len(ind_columns), *config.ind_layers])
agg_model = Net([shared_model.sizes[-2] + ind_model.last, *config.agg_layers])
splitNN = SplitNN({'ind_model': ind_model, 'shared_model': shared_model, 'agg_model': agg_model})

split_opt = torch.optim.Adam(splitNN.parameters(), lr=1e-3, weight_decay=config.weight_decay)

def train(splitNN, opts, trainloader, epochs):
    criterion = nn.BCELoss()
    splitNN.train()
    for _ in range(epochs):
        loss = 0.0
        tp, fp, tn, fn = 0, 0, 0, 0
        for x, y in trainloader:
            x, y = x.to(DEVICE), y.to(DEVICE)
            opts.zero_grad()
            outputs = splitNN(x)
            outputs = outputs.view(-1)
            loss = criterion(outputs, y)
            loss.backward()
            opts.step()
            preds = np.round_(outputs.detach().numpy())
            for lab, pred in zip(y, preds):
                # Collect statistics
                tp += (pred and lab)
                fp += (pred and not lab)
                tn += (not pred and not lab)
                fn += (not pred and lab)
        f1_score = tp / (tp + (fp + fn)/2)
        print(f'\rTRAIN tp: {tp}, fp: {fp}, tn: {tn}, fn: {fn} | F1 score: {f1_score:.4f} \t Loss: {loss:.4f}', end='')


def test(splitNN, testloader):
    criterion = nn.BCELoss()
    loss = 0.0
    tp, fp, tn, fn = 0, 0, 0, 0
    splitNN.eval()
    with torch.no_grad():
        for x, y in testloader:
            x, y = x.to(DEVICE), y.to(DEVICE)
            outputs = splitNN(x)
            outputs = outputs.view(-1)
            loss += criterion(outputs, y).item()

            preds = np.round_(outputs.numpy())
            for lab, pred in zip(y, preds):
                # Collect statistics
                tp += (pred and lab)
                fp += (pred and not lab)
                tn += (not pred and not lab)
                fn += (not pred and lab)
    f1_score = tp / (tp + (fp + fn)/2)
    print(f'TEST tp: {tp}, fp: {fp}, tn: {tn}, fn: {fn} | F1 score: {f1_score:.4f} \t Loss: {loss:.4f}')
    return loss, f1_score
print('starting local training')
train(splitNN, split_opt, trainloader, epochs=config.rounds)
print()
_, f1_score = test(splitNN, testloader)
print(f'Client {sys.argv[1]} DONE!')
if len(sys.argv) > 2:
    with open(f'results/transfer_client_{sys.argv[1]}-{sys.argv[2]}-{len(shared_columns)}.txt', 'a') as f:
        f.write(f'{f1_score}\n')
else:
    with open(f'results/transfer_client_{sys.argv[1]}.txt', 'a') as f:
        f.write(f'{f1_score}\n')