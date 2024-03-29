#!/home/sebastian/miniconda3/bin/python

#COPYRIGHT
#
#Copyright (c) 2018 Kristof Schütt, Michael Gastegger, Pan Kessel, Kim Nicoli
#
#All rights reserved.
#Modified for use with shrinkschnet.py, Sebastian Dick 2019
#
#LICENSE
#
#The MIT License

import argparse
import logging
import os
import sys
from shutil import copyfile, rmtree
from types import SimpleNamespace as SN
import numpy as np
import torch
import torch.nn as nn
from ase.data import atomic_numbers
from torch.optim import Adam
from torch.utils.data.sampler import RandomSampler
import json 

import schnetpack as spk
from schnetpack.datasets import QM9
from schnetpack.utils import compute_params, to_json, read_from_json
from shrinkschnet import *

logging.basicConfig(level=os.environ.get("LOGLEVEL", "INFO"))


def get_parser():
    """ Setup parser for command line arguments """
    main_parser = argparse.ArgumentParser()

    ## command-specific
    cmd_parser = argparse.ArgumentParser(add_help=False)
    cmd_parser.add_argument('--cuda', help='Set flag to use GPU(s)', action='store_true')
    cmd_parser.add_argument('--parallel',
                            help='Run data-parallel on all available GPUs (specify with environment variable'
                                 + ' CUDA_VISIBLE_DEVICES)', action='store_true')
    cmd_parser.add_argument('--batch_size', type=int,
                            help='Mini-batch size for training and prediction (default: %(default)s)',
                            default=100)

    ## training
    train_parser = argparse.ArgumentParser(add_help=False, parents=[cmd_parser])
    train_parser.add_argument('datapath', help='Path / destination of QM9 dataset directory')
    train_parser.add_argument('modelpath', help='Destination for models and logs')
    train_parser.add_argument('--property', type=str, help='QM9 property to be predicted (default: %(default)s)',
                              default="energy_U0", choices=QM9.properties+['coupling'] + ['coupling_min_lgbm'])

    train_parser.add_argument('--remove_uncharacterized', help='Remove uncharacterized molecules from QM9',
                              type=bool, default='False')
    train_parser.add_argument('--seed', type=int, default=None, help='Set random seed for torch and numpy.')
    train_parser.add_argument('--overwrite', help='Remove previous model directory.', action='store_true')

    train_parser.add_argument('--split_path', help='Path / destination of npz with data splits',
                              default=None)
    train_parser.add_argument('--split', help='Split into [train] [validation] and use remaining for testing',
                              type=int, nargs=2, default=[None, None])
    train_parser.add_argument('--shrink_distances', help='Distance cutoff for shrinking layers',
                              type=float, nargs='+', default=[3,1.5,0])
    train_parser.add_argument('--max_epochs', type=int, help='Maximum number of training epochs (default: %(default)s)',
                              default=5000)
    train_parser.add_argument('--lr', type=float, help='Initial learning rate (default: %(default)s)',
                              default=1e-4)
    train_parser.add_argument('--lr_patience', type=int,
                              help='Epochs without improvement before reducing the learning rate (default: %(default)s)',
                              default=25)
    train_parser.add_argument('--lr_decay', type=float, help='Learning rate decay (default: %(default)s)',
                              default=0.5)
    train_parser.add_argument('--lr_min', type=float, help='Minimal learning rate (default: %(default)s)',
                              default=1e-6)

    train_parser.add_argument('--logger', help='Choose logger for training process (default: %(default)s)',
                            choices=['csv', 'tensorboard'], default='csv')
    train_parser.add_argument('--log_every_n_epochs', type=int,
                            help='Log metrics every given number of epochs (default: %(default)s)',
                            default=1)

    ## evaluation
    eval_parser = argparse.ArgumentParser(add_help=False, parents=[cmd_parser])
    eval_parser.add_argument('datapath', help='Path of QM9 dataset directory')
    eval_parser.add_argument('modelpath', help='Path of stored model')
    eval_parser.add_argument('--split', help='Evaluate trained model on given split',
                             choices=['train', 'validation', 'test'], default=['test'], nargs='+')

    # model-specific parsers
    model_parser = argparse.ArgumentParser(add_help=False)
    model_parser.add_argument('--aggregation_mode', type=str, default='sum', choices=['sum', 'avg'],
                              help=' (default: %(default)s)')

    #######  SchNet  #######
    schnet_parser = argparse.ArgumentParser(add_help=False, parents=[model_parser])
    schnet_parser.add_argument('--features', type=int, help='Size of atom-wise representation (default: %(default)s)',
                               default=256)
    schnet_parser.add_argument('--interactions', type=int, help='Number of interaction blocks (default: %(default)s)',
                               default=6)
    schnet_parser.add_argument('--cutoff', type=float, default=5.,
                               help='Cutoff radius of local environment (default: %(default)s)')
    schnet_parser.add_argument('--num_gaussians', type=int, default=25,
                               help='Number of Gaussians to expand distances (default: %(default)s)')

    schnet_parser.add_argument('--readout_layers', type=int, default=3,
                               help='Layers in fully connected part of readout function')
    
    schnet_parser.add_argument('--edge_updates', action='store_true',
                              help='Allow edges to be updated')

    schnet_parser.add_argument('--pretrained_model', type=str, default='',
                              help='Use pretrained model as embedding layer')
    
    #######  wACSF  ########
    wacsf_parser = argparse.ArgumentParser(add_help=False, parents=[model_parser])
    # wACSF parameters
    wacsf_parser.add_argument('--radial', type=int, default=22,
                              help='Number of radial symmetry functions (default: %(default)s)')
    wacsf_parser.add_argument('--angular', type=int, default=5,
                              help='Number of angular symmetry functions (default: %(default)s)')
    wacsf_parser.add_argument('--zetas', type=int, nargs='+', default=[1],
                              help='List of zeta exponents used for angle resolution (default: %(default)s)')
    wacsf_parser.add_argument('--standardize', action='store_true',
                              help='Standardize wACSF before atomistic network.')
    wacsf_parser.add_argument('--cutoff', type=float, default=5.0,
                              help='Cutoff radius of local environment (default: %(default)s)')
    # Atomistic network parameters
    wacsf_parser.add_argument('--n_nodes', type=int, default=100,
                              help='Number of nodes in atomic networks (default: %(default)s)')
    wacsf_parser.add_argument('--n_layers', type=int, default=2,
                              help='Number of layers in atomic networks (default: %(default)s)')
    # Advances wACSF settings
    wacsf_parser.add_argument('--centered', action='store_true', help='Use centered Gaussians for radial functions')
    wacsf_parser.add_argument('--crossterms', action='store_true', help='Use crossterms in angular functions')
    wacsf_parser.add_argument('--behler', action='store_true', help='Switch to conventional ACSF')
    wacsf_parser.add_argument('--elements', default=['H', 'C', 'N', 'O', 'F'], nargs='+',
                              help='List of elements to be used for symmetry functions (default: %(default)s).')

    ## setup subparser structure
    cmd_subparsers = main_parser.add_subparsers(dest='mode', help='Command-specific arguments')
    cmd_subparsers.required = True
    subparser_train = cmd_subparsers.add_parser('train', help='Training help')
    subparser_eval = cmd_subparsers.add_parser('eval', help='Eval help')

    subparser_export = cmd_subparsers.add_parser('export', help='Export help')
    subparser_export.add_argument('modelpath', help='Path of stored model')
    subparser_export.add_argument('destpath', help='Destination path for exported model')

    train_subparsers = subparser_train.add_subparsers(dest='model', help='Model-specific arguments')
    train_subparsers.required = True
    train_subparsers.add_parser('schnet', help='SchNet help', parents=[train_parser, schnet_parser])
    train_subparsers.add_parser('wacsf', help='wACSF help', parents=[train_parser, wacsf_parser])

    eval_subparsers = subparser_eval.add_subparsers(dest='model', help='Model-specific arguments')
    eval_subparsers.required = True
    eval_subparsers.add_parser('schnet', help='SchNet help', parents=[eval_parser, schnet_parser])
    eval_subparsers.add_parser('wacsf', help='wACSF help', parents=[eval_parser, wacsf_parser])

    return main_parser


def train(args, model, train_loader, val_loader, device):
    # setup hook and logging
    hooks = [
        spk.train.MaxEpochHook(args.max_epochs)
    ]

    # filter for trainable parameters (https://github.com/pytorch/pytorch/issues/679)
    trainable_params = filter(lambda p: p.requires_grad, model.parameters())
    optimizer = Adam(trainable_params, lr=args.lr)
    schedule = spk.train.ReduceLROnPlateauHook(optimizer, patience=args.lr_patience, factor=args.lr_decay,
                                               min_lr=args.lr_min,
                                               window_length=1, stop_after_min=True)
    hooks.append(schedule)

    if args.logger == 'csv':
        logger = spk.train.CSVHook(os.path.join(args.modelpath, 'log'),
                                   [spk.metrics.MeanAbsoluteError(args.property, "y"),
                                    spk.metrics.RootMeanSquaredError(args.property, "y")],
                                   every_n_epochs=args.log_every_n_epochs)
        hooks.append(logger)
    elif args.logger == 'tensorboard':
        logger = spk.train.TensorboardHook(os.path.join(args.modelpath, 'log'),
                                           [spk.metrics.MeanAbsoluteError(args.property, "y"),
                                            spk.metrics.RootMeanSquaredError(args.property, "y")],
                                           every_n_epochs=args.log_every_n_epochs)
        hooks.append(logger)

    # setup loss function
    def loss(batch, result):
        diff = batch[args.property] - result["y"]
        diff = diff ** 2
        err_sq = torch.mean(diff)
        return err_sq

    trainer = spk.train.Trainer(args.modelpath, model, loss, optimizer,
                                train_loader, val_loader, hooks=hooks)
    trainer.train(device)


def evaluate(args, model, property, train_loader, val_loader, test_loader, device):
    header = ['Subset', property + ' MAE', property + ' RMSE']

    metrics = [spk.metrics.MeanAbsoluteError(property, "y"),
               spk.metrics.RootMeanSquaredError(property, "y")
               ]

    results = []
    if 'train' in args.split:
        results.append(['training'] + ['%.5f' % i for i in evaluate_dataset(metrics, model, train_loader, device)])

    if 'validation' in args.split:
        results.append(['validation'] + ['%.5f' % i for i in evaluate_dataset(metrics, model, val_loader, device)])

    if 'test' in args.split:
        results.append(['test'] + ['%.5f' % i for i in evaluate_dataset(metrics, model, test_loader, device)])

    header = ','.join(header)
    results = np.array(results)

    np.savetxt(os.path.join(args.modelpath, 'evaluation.csv'), results, header=header, fmt='%s', delimiter=',')


def evaluate_dataset(metrics, model, loader, device):
    for metric in metrics:
        metric.reset()

    for batch in loader:
        batch = {
            k: v.to(device)
            for k, v in batch.items()
        }
        result = model(batch)

        for metric in metrics:
            metric.add_batch(batch, result)

    results = [
        metric.aggregate() for metric in metrics
    ]
    return results

def get_pretrained(modelpath, return_rep=True):  
    
    args = json.load(open(modelpath + '/args.json','r'))
    args = SN(**args)
  
    representation = ShrinkSchNet(args.features, args.features, args.interactions,
                                                   args.cutoff, args.num_gaussians, 
                                                      shrink_distances = args.shrink_distances,
                                                 edgeupdate_block = None)

    device = torch.device('cuda')

    atomwise_output = SCReadout(args.features, args.features, args.readout_layers, mean=None)


    model = spk.atomistic.AtomisticModel(representation, atomwise_output).to(device)

    model.load_state_dict(
                torch.load(os.path.join(modelpath, 'best_model')))


    return model.modules().__next__().representation

def get_model(args, atomref=None, mean=None, stddev=None, train_loader=None, parallelize=False, mode='train'):
    if args.model == 'schnet':
        
        if args.pretrained_model:
            
            pretrained_args = json.load(open(args.pretrained_model +'args.json','r'))
            if pretrained_args['interactions'] > args.interactions:
                raise ValueError('Please set number of interaction layers greater or equal to that of pretrained model')
            args.features = pretrained_args['features']
            args.cutoff = pretrained_args['cutoff']
            args.num_gaussians = pretrained_args['num_gaussians']
            pretrained = get_pretrained(args.pretrained_model)
            
               
        if args.edge_updates:
            representation = ShrinkSchNet(args.features, args.features, args.interactions,
                                                       args.cutoff, args.num_gaussians, coupled_interactions = False,
                                                          shrink_distances = args.shrink_distances)
        else:
            representation = ShrinkSchNet(args.features, args.features, args.interactions,
                                                       args.cutoff, args.num_gaussians, 
                                                          shrink_distances = args.shrink_distances,
                                                     edgeupdate_block = None, coupled_interactions=False)
        
        if args.pretrained_model:
            list_of_layers = list(pretrained.interactions.children())[:pretrained_args['interactions']]
            for child in list_of_layers:
                for param in child.parameters():
                    param.requires_grad = False
 
#            list_of_layers.extend(list(representation.interactions.children())[len(list_of_layers):])
#            new_interactions = nn.ModuleList(list_of_layers)
#            pretrained.interactions = new_interactions
            representation = pretrained
            
        atomwise_output = SCReadout(args.features, args.features, args.readout_layers, mean=mean)

        
        model = spk.atomistic.AtomisticModel(representation, atomwise_output)
        print(model)
    if parallelize:
        model = nn.DataParallel(model)

    logging.info("The model you built has: %d parameters" % compute_params(model))

    return model


def export_model(args):
    jsonpath = os.path.join(args.modelpath, 'args.json')
    train_args = read_from_json(jsonpath)
    model = get_model(train_args, atomref=np.zeros((100, 1)))
    model.load_state_dict(
        torch.load(os.path.join(args.modelpath, 'best_model')))

    torch.save(model, args.destpath)


if __name__ == '__main__':
    parser = get_parser()
    args = parser.parse_args()

    if args.mode == 'export':
        export_model(args)
        sys.exit(0)

    device = torch.device("cuda" if args.cuda else "cpu")
    argparse_dict = vars(args)
    jsonpath = os.path.join(args.modelpath, 'args.json')

    if args.mode == 'train':
        if args.overwrite and os.path.exists(args.modelpath):
            rmtree(args.modelpath)
            logging.info('existing model will be overwritten...')

        if not os.path.exists(args.modelpath):
            os.makedirs(args.modelpath)

        to_json(jsonpath, argparse_dict)

        spk.utils.set_random_seed(args.seed)
        train_args = args
    else:
        train_args = read_from_json(jsonpath)

    # will download qm9 if necessary, calculate_triples is required for wACSF angular functions
    logging.info('QM9 will be loaded...')
    qm9 = spk.data.AtomsData(args.datapath + '/coupling.db', properties=[train_args.property, '_atom_mask'],
            collect_triples=args.model == 'wacsf')
    atomref = None
    # splits the dataset in test, val, train sets
    split_path = os.path.join(args.modelpath, 'split.npz')
    if args.mode == 'train':
        if args.split_path is not None:
            copyfile(args.split_path, split_path)

    logging.info('create splits...')
    data_train, data_val, data_test = qm9.create_splits(*train_args.split, split_file=split_path)

    logging.info('load data...')
    train_loader = spk.data.AtomsLoader(data_train, batch_size=args.batch_size, sampler=RandomSampler(data_train),
                                        num_workers=4, pin_memory=True)
    val_loader = spk.data.AtomsLoader(data_val, batch_size=args.batch_size, num_workers=2, pin_memory=True)

    if args.mode == 'train':
        logging.info('calculate statistics...')
        mean, stddev = train_loader.get_statistics(train_args.property, True, atomref, split_file=split_path)
    else:
        mean, stddev = None, None

    # construct the model
    model = get_model(train_args, atomref=atomref, mean=mean, stddev=stddev,
                      train_loader=train_loader, parallelize=args.parallel, mode=args.mode).to(device)
    if args.mode == 'eval':
        if args.parallel:
            model.module.load_state_dict(
                torch.load(os.path.join(args.modelpath, 'best_model')))
        else:
            model.load_state_dict(
                torch.load(os.path.join(args.modelpath, 'best_model')))
    if args.mode == 'train':
        logging.info("training...")
        train(args, model, train_loader, val_loader, device)
        logging.info("...training done!")
    elif args.mode == 'eval':
        logging.info("evaluating...")
        test_loader = spk.data.AtomsLoader(data_test, batch_size=args.batch_size,
                                           num_workers=2, pin_memory=True)
        with torch.no_grad():
            evaluate(args, model, train_args.property, train_loader, val_loader, test_loader, device)
        logging.info("... done!")
    else:
        print('Unknown mode:', args.mode)


