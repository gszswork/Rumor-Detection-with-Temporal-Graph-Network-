import math
import logging
from tqdm import tqdm
import time
import sys
import argparse
import torch
import numpy as np
import pickle
from pathlib import Path
from torch.utils.data import DataLoader
from evaluation.evaluation import eval_edge_prediction
from model.tgn import TGN
from utils.utils import EarlyStopMonitor, get_neighbor_finder
from utils.dataset import loadTree, loadUdData
from utils.rand5fold import load5foldData
from tqdm import tqdm
import os
from torch import nn
from sklearn import metrics
import random
from random import shuffle


class Net(nn.Module):
    def __init__(self, args, device):
        super(Net, self).__init__()
        self.device = device

        self.tgn = TGN(device=device,
            n_layers = args.n_layer,
            n_heads= args.n_head, dropout = args.drop_out, use_memory = args.use_memory,
            message_dimension=args.message_dim, memory_dimension = args.memory_dim,
            memory_update_at_start = not args.memory_update_at_end,
            embedding_module_type = args.embedding_module,
            message_function_type=args.message_function_type,
            aggregator_type = args.aggregator,
            memory_updater_type = args.memory_updater,
            n_neighbors = args.n_degree,
            use_destination_embedding_in_message=args.use_destination_embedding_in_message,
            use_source_embedding_in_message= args.use_source_embedding_in_message,
            dyrep=args.dyrep)

        self.fc2 = torch.nn.Linear(args.memory_dim, 2)

      

        self.act_tanh = torch.nn.Tanh()
        self.softmax =  torch.nn.Softmax(dim=1)

        self.batchsize = args.bs
        

    def forward(self, data, train_ngh_finder):
          updated_embedding = self.tgn(train_ngh_finder, self.batchsize, data.sources, data.destinations, data.timestamps, data.unique_features, data.edge_idxs, data.n_unique_nodes, data.adj_list)
          torch.cuda.empty_cache()
          out_feature = torch.mean(updated_embedding[1:].float(), 0).float()  # Our index is counting from 1, so neglect idx 0.
          out_feature = self.fc2(self.act_tanh(out_feature))
          class_outputs = self.softmax(out_feature.view(1,-1))
   

          return class_outputs




torch.manual_seed(0)
np.random.seed(0)
np.set_printoptions(suppress=True)
### Argument and global variables
parser = argparse.ArgumentParser('TGN self-supervised training')
parser.add_argument('--data', type=str, help='Dataset name',
                    default='Twitter')
parser.add_argument('--bs', type=int, default=10, help='Batch_size')
parser.add_argument('--prefix', type=str, default='tgn-attn-weibo-ma', help='Prefix to name the checkpoints')
parser.add_argument('--n_degree', type=int, default=10, help='Number of neighbors to sample')
parser.add_argument('--n_head', type=int, default=2, help='Number of heads used in attention layer')
parser.add_argument('--n_epoch', type=int, default=50, help='Number of epochs')
parser.add_argument('--n_layer', type=int, default=1, help='Number of network layers')
parser.add_argument('--lr', type=float, default=0.00001, help='Learning rate')
parser.add_argument('--patience', type=int, default=5, help='Patience for early stopping')
parser.add_argument('--n_runs', type=int, default=1, help='Number of runs')
parser.add_argument('--drop_out', type=float, default=0.1, help='Dropout probability')
parser.add_argument('--gpu', type=int, default=0, help='Idx for the gpu to use')
parser.add_argument('--node_dim', type=int, default=100, help='Dimensions of the node embedding')
parser.add_argument('--time_dim', type=int, default=100, help='Dimensions of the time embedding')
parser.add_argument('--backprop_every', type=int, default=2, help='Every how many batches to '
                                                                  'backprop')
parser.add_argument('--use_memory', action='store_true',
                    help='Whether to augment the model with a node memory')
parser.add_argument('--embedding_module', type=str, default="graph_attention", choices=[
  "graph_attention", "graph_sum", "identity", "time"], help='Type of embedding module')
parser.add_argument('--message_function_type', type=str, default="identity", choices=[
  "mlp", "identity"], help='Type of message function')
parser.add_argument('--memory_updater', type=str, default="gru", choices=[
  "gru", "rnn"], help='Type of memory updater')
parser.add_argument('--aggregator', type=str, default="last", help='Type of message '
                                                                        'aggregator')
parser.add_argument('--memory_update_at_end', action='store_true',
                    help='Whether to update memory at the end or at the start of the batch')
parser.add_argument('--message_dim', type=int, default=100, help='Dimensions of the messages')
parser.add_argument('--memory_dim', type=int, default=768, help='Dimensions of the memory for '
                                                                'each user')
parser.add_argument('--different_new_nodes', action='store_true',
                    help='Whether to use disjoint set of new nodes for train and val')
parser.add_argument('--uniform', action='store_true',
                    help='take uniform sampling from temporal neighbors')
parser.add_argument('--randomize_features', action='store_true',
                    help='Whether to randomize node features')


parser.add_argument('--use_destination_embedding_in_message', action='store_false',
                    help='Whether to use the embedding of the destination node as part of the message')
parser.add_argument('--use_source_embedding_in_message', action='store_false',
                    help='Whether to use the embedding of the source node as part of the message')
# Beacause I'm not sure which one I used (use_destination_embedding_in_message and use_source_embedding_in_message), I suggest you also can try the following setting. 
#parser.add_argument('--use_destination_embedding_in_message', action='store_false',
#                    help='Whether to use the embedding of the destination node as part of the message')
#parser.add_argument('--use_source_embedding_in_message', action='store_false',
#                    help='Whether to use the embedding of the source node as part of the message')




parser.add_argument('--dyrep', action='store_true',
                    help='Whether to run the dyrep model')
parser.add_argument('--use_gcn', action='store_true',
                    help='Whether to run the GCN model')
parser.add_argument('--opt', type=str, default="RMSprop", choices=[
  "RMSprop", "Adam"], help='Type of optimizer')
parser.add_argument('--fd', type=int, default= 0,choices=[0, 1, 2, 3, 4, 5], help='fold index')# 5 denotes all data


try:
  args = parser.parse_args()
except:
  parser.print_help()
  sys.exit(0)
  
print("Now using aggregator function is ",args.aggregator)
weight_decay=1e-4
patience=10

Path("./saved_models/{}/{}_fold/".format(args.aggregator, args.fd)).mkdir(parents=True, exist_ok=True)


get_model_path = lambda \
    epoch, train_accuracy, test_accuracy: f'./saved_models/{args.aggregator}/{args.fd}_fold/{epoch}-{train_accuracy}-{test_accuracy}.pth'


# Set device
device_string = 'cuda:{}'.format(args.gpu) if torch.cuda.is_available() else 'cpu'
device = torch.device(device_string)

treeDic = loadTree(args.data)
print("len(treeDic)",len(treeDic))

### Extract data for training and testing
'''
fold0_x_test, fold0_x_train,\
   fold1_x_test, fold1_x_train, \
    fold2_x_test, fold2_x_train,  \
    fold3_x_test, fold3_x_train,  \
    fold4_x_test, fold4_x_train = load5foldData(args.data, treeDic)

np.save('./5_fold_ids/fold0_x_test_id.npy',np.array(fold0_x_test))
np.save('./5_fold_ids/fold0_x_train_id.npy',np.array(fold0_x_train))

np.save('./5_fold_ids/fold1_x_test_id.npy',np.array(fold1_x_test))
np.save('./5_fold_ids/fold1_x_train_id.npy',np.array(fold1_x_train))

np.save('./5_fold_ids/fold2_x_test_id.npy',np.array(fold2_x_test))
np.save('./5_fold_ids/fold2_x_train_id.npy',np.array(fold2_x_train))

np.save('./5_fold_ids/fold3_x_test_id.npy',np.array(fold3_x_test))
np.save('./5_fold_ids/fold3_x_train_id.npy',np.array(fold3_x_train))

np.save('./5_fold_ids/fold4_x_test_id.npy',np.array(fold4_x_test))
np.save('./5_fold_ids/fold4_x_train_id.npy',np.array(fold4_x_train))

'''
fold0_x_test = np.load('./5_fold_ids/fold0_x_test_id.npy')
fold0_x_train = np.load('./5_fold_ids/fold0_x_train_id.npy')
fold1_x_test = np.load('./5_fold_ids/fold1_x_test_id.npy')
fold1_x_train = np.load('./5_fold_ids/fold1_x_train_id.npy')
fold2_x_test = np.load('./5_fold_ids/fold2_x_test_id.npy')
fold2_x_train = np.load('./5_fold_ids/fold2_x_train_id.npy')
fold3_x_test = np.load('./5_fold_ids/fold3_x_test_id.npy')
fold3_x_train = np.load('./5_fold_ids/fold3_x_train_id.npy')
fold4_x_test = np.load('./5_fold_ids/fold4_x_test_id.npy')
fold4_x_train = np.load('./5_fold_ids/fold4_x_train_id.npy')


#random.shuffle(treeDic)
fold5_x_train = [] # all data
fold5_x_test = [] # all data

for i in treeDic:
        fold5_x_train.append(i)
        fold5_x_test.append(i)
                                                                                             
                                                                                               
                                                                                               
def to_np(x):
    return x.cpu().detach().numpy()                                                                                              
     

def train_TGN (args, treeDic, x_test, x_train, weight_decay, patience, device):
    print('Training on device: ', device)
    model = Net(args, device)
    model = model.to(device)
  
    if args.opt == 'RMSprop':
          print("RMSprop")
          optimizer = torch.optim.RMSprop(model.parameters(), lr = args.lr, weight_decay=weight_decay)
    else:
          print("Adam")
          optimizer = torch.optim.Adam(model.parameters(), lr = args.lr, weight_decay=weight_decay)
        
    criterion = nn.CrossEntropyLoss() #nn.NLLLoss()#
    
    traindata_list, testdata_list = loadUdData(args.data, treeDic, x_train, x_test)
    print("len(traindata_list)",len(traindata_list))
    print("len(testdata_list)",len(testdata_list))
    
  
    for epoch in range(args.n_epoch):
                  start_epoch = time.time()
                  num_item = 0
                  total_train_loss = 0.0
                  avg_train_loss = 0.0
                  ok = 0
                  train_pred = []
                  train_true = []
                  model = model.train()

                  for item in tqdm(traindata_list):
                        num_item+=1
                        optimizer.zero_grad()
                        label = np.array([item.labels])
                        label = torch.from_numpy(label).to(device)
                        train_ngh_finder = get_neighbor_finder(item, uniform=False)
                        model.tgn.set_neighbor_finder(train_ngh_finder)
                        class_outputs = model(item, train_ngh_finder)
                        class_loss = criterion(class_outputs, label.long())
                        
                        class_loss.backward()
                        optimizer.step()
                        pred = torch.argmax(class_outputs, dim=1)

                        if num_item % 1000 == 0:
                              print("num_item",num_item)
                        total_train_loss+= class_loss
                        
                        if pred[0] == label[0]:
                                    ok+=1

                        if num_item-1 == 0:
                          train_pred = to_np(pred)
                          train_true = to_np(label)
                        else:
                          train_pred = np.concatenate((train_pred, to_np(pred)), axis=0)
                          train_true = np.concatenate((train_true, to_np(label)), axis=0)
                      
                  print(metrics.classification_report(train_true, train_pred, digits = 4))
                  avg_train_loss = total_train_loss/num_item
                  train_accuracy = round(ok /num_item,3)
                 

                  num_item = 0
                  total_test_loss = 0.0
                  avg_test_loss = 0.0
                  ok = 0
                  
                  test_accuracy = 0.000
                  test_pred = []
                  test_true = []
                  model = model.eval()      
                  for item in testdata_list:
                        num_item+=1
                        index = item.id
                        label = np.array([item.labels])
                        label = torch.from_numpy(label).to(device)
                        test_ngh_finder = get_neighbor_finder(item, uniform=False)
                        model.tgn.set_neighbor_finder(test_ngh_finder)
                        class_outputs = model(item, test_ngh_finder)
                        pred = torch.argmax(class_outputs, dim=1)
                        if pred[0] == label[0]:
                                   ok+=1
                  
                        if num_item-1 == 0:
                              test_pred = to_np(pred)
                              test_true = to_np(label)
                        else:
                              test_pred = np.concatenate((test_pred, to_np(pred)), axis=0)
                              test_true = np.concatenate((test_true, to_np(label)), axis=0)
                  
                  test_accuracy = round(ok /num_item, 3)   
                
                  print(metrics.classification_report(test_true, test_pred,  digits = 4)) 

                  epoch_time = (time.time() - start_epoch)/60
                 
                  print("Epoch id: {}, Epoch time: {:.3f} , avg_train_loss: {:.3f}, train_accuracy: {:.3f},  avg_test_loss: {:.3f}, test_accuracy: {:.3f}".format(epoch, epoch_time, avg_train_loss, train_accuracy, avg_test_loss, test_accuracy))
                  torch.save(model.state_dict(), get_model_path(epoch, round(train_accuracy,3), round(train_accuracy,3) ))
            
                  
        
    
        

if   args.fd == 0:
      train_TGN(args, treeDic, fold0_x_test, fold0_x_train, weight_decay, patience, device)
elif args.fd == 1:
      train_TGN(args, treeDic, fold1_x_test, fold1_x_train, weight_decay, patience, device)
elif args.fd == 2:
      train_TGN(args, treeDic, fold2_x_test, fold2_x_train, weight_decay, patience, device)
elif args.fd == 3:
      train_TGN(args, treeDic, fold3_x_test, fold3_x_train, weight_decay, patience, device)
elif args.fd == 4:
      train_TGN(args, treeDic, fold4_x_test, fold4_x_train, weight_decay, patience, device)
elif args.fd == 5:
      train_TGN(args, treeDic, fold5_x_test, fold5_x_train, weight_decay, patience, device)





