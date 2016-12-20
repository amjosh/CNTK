# Copyright (c) Microsoft. All rights reserved.

# Licensed under the MIT license. See LICENSE.md file in the project root
# for full license information.
# ==============================================================================

from __future__ import print_function
import os
import argparse
import math
import numpy as np

from cntk.utils import *
from cntk.ops import input_variable, cross_entropy_with_softmax, classification_error
from cntk.io import MinibatchSource, ImageDeserializer, StreamDef, StreamDefs, INFINITE_SAMPLES, FULL_DATA_SWEEP
from cntk import Trainer, cntk_py, distributed
from cntk.learner import momentum_sgd, learning_rate_schedule, momentum_as_time_constant_schedule, UnitType
from _cntk_py import set_computation_network_trace_level

from resnet_models import *

# Paths relative to current python file.
abs_path   = os.path.dirname(os.path.abspath(__file__))
data_path  = os.path.join(abs_path, "..", "..", "..", "DataSets", "CIFAR-10")
model_path = os.path.join(abs_path, "Models")

# model dimensions
image_height = 32
image_width  = 32
num_channels = 3  # RGB
num_classes  = 10

# Define the reader for both training and evaluation action.
def create_reader(map_file, mean_file, train, total_data_size, distributed_after=INFINITE_SAMPLES):
    if not os.path.exists(map_file) or not os.path.exists(mean_file):
        raise RuntimeError("File '%s' or '%s' does not exist. Please run install_cifar10.py from DataSets/CIFAR-10 to fetch them" %
                           (map_file, mean_file))

    # transformation pipeline for the features has jitter/crop only when training
    transforms = []
    if train:
        transforms += [
            ImageDeserializer.crop(crop_type='Random', ratio=0.8, jitter_type='uniRatio') # train uses jitter
        ]
    transforms += [
        ImageDeserializer.scale(width=image_width, height=image_height, channels=num_channels, interpolations='linear'),
        ImageDeserializer.mean(mean_file)
    ]
    # deserializer
    return MinibatchSource(
        ImageDeserializer(map_file, StreamDefs(
            features = StreamDef(field='image', transforms=transforms), # first column in map file is referred to as 'image'
            labels   = StreamDef(field='label', shape=num_classes))),   # and second as 'label'
        epoch_size=total_data_size,
        multithreaded_deserializer = False,  # turn off omp as CIFAR-10 is not heavy for deserializer
        distributed_after = distributed_after)


# Train and evaluate the network.
def train_and_evaluate(create_train_reader, test_reader, network_name, max_epochs, create_dist_learner, scale_up=False):

    set_computation_network_trace_level(0)

    # Input variables denoting the features and label data
    input_var = input_variable((num_channels, image_height, image_width))
    label_var = input_variable((num_classes))

    # create model, and configure learning parameters 
    if network_name == 'resnet20': 
        z = create_cifar10_model(input_var, 3, num_classes)
        lr_per_mb = [1.0]*80+[0.1]*40+[0.01]
    elif network_name == 'resnet110': 
        z = create_cifar10_model(input_var, 18, num_classes)
        lr_per_mb = [0.1]*1+[1.0]*80+[0.1]*40+[0.01]
    else: 
        return RuntimeError("Unknown model name!")

    # loss and metric
    ce = cross_entropy_with_softmax(z, label_var)
    pe = classification_error(z, label_var)

    # shared training parameters 
    epoch_size = 50000                    # for now we manually specify epoch size
    
    # NOTE: scaling up minibatch_size increases sample throughput. In 8-GPU machine,
    # ResNet110 samples-per-second is ~7x of single GPU, comparing to ~3x without scaling
    # up. However, bigger minimatch size on the same number of samples means less updates, 
    # thus leads to higher training error. This is a trade-off of speed and accuracy
    minibatch_size = 128 * (distributed.Communicator.num_workers() if scale_up else 1)

    momentum_time_constant = -minibatch_size/np.log(0.9)
    l2_reg_weight = 0.0001

    # Set learning parameters
    lr_per_sample = [lr/minibatch_size for lr in lr_per_mb]
    lr_schedule = learning_rate_schedule(lr_per_sample, epoch_size=epoch_size, unit=UnitType.sample)
    mm_schedule = momentum_as_time_constant_schedule(momentum_time_constant)
    
    # trainer object
    learner     = create_dist_learner(momentum_sgd(z.parameters, lr_schedule, mm_schedule,
                                                   l2_regularization_weight = l2_reg_weight))
    trainer     = Trainer(z, ce, pe, learner)

    total_number_of_samples = max_epochs * epoch_size
    train_reader=create_train_reader(total_number_of_samples)

    # define mapping from reader streams to network inputs
    input_map = {
        input_var: train_reader.streams.features,
        label_var: train_reader.streams.labels
    }

    log_number_of_parameters(z) ; print()
    progress_printer = ProgressPrinter(tag='Training')

    # perform model training
    current_epoch=0
    updated=True
    while updated:
        data=train_reader.next_minibatch(minibatch_size, input_map=input_map) # fetch minibatch.
        updated=trainer.train_minibatch(data)                                 # update model with it
        progress_printer.update_with_trainer(trainer, with_metric=True)       # log progress
        epoch_index = int(trainer.total_number_of_samples_seen/epoch_size)
        if current_epoch != epoch_index:                                      # new epoch reached
            progress_printer.epoch_summary(with_metric=True)
            current_epoch=epoch_index
            trainer.save_checkpoint(os.path.join(model_path, network_name + "_{}.dnn".format(current_epoch)))

    # Evaluation parameters
    epoch_size     = 10000
    minibatch_size = 16

    # process minibatches and evaluate the model
    metric_numer    = 0
    metric_denom    = 0
    sample_count    = 0
    minibatch_index = 0

    while True:
        data = test_reader.next_minibatch(minibatch_size, input_map=input_map)
        if not data: break;

        local_mb_samples=data[label_var].num_samples
        metric_numer += trainer.test_minibatch(data) * local_mb_samples
        metric_denom += local_mb_samples
        minibatch_index += 1

    print("")
    print("Final Results: Minibatch[1-{}]: errs = {:0.2f}% * {}".format(minibatch_index+1, (metric_numer*100.0)/metric_denom, metric_denom))
    print("")

    return metric_numer/metric_denom

if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('-n', '--network', help='network type, resnet20 or resnet110', required=False, default='resnet20')
    parser.add_argument('-e', '--epochs', help='total epochs', type=int, required=False, default='160')
    parser.add_argument('-q', '--quantize_bit', help='quantized bit', type=int, required=False, default='32')
    parser.add_argument('-s', '--scale_up', help='scale up minibatch size with #workers for better parallelism', type=bool, required=False, default='False')
    parser.add_argument('-a', '--distributed_after', help='number of samples to train with before running distributed', type=int, required=False, default='0')

    args = vars(parser.parse_args())
    num_quantization_bits = int(args['quantize_bit'])
    epochs = int(args['epochs'])
    distributed_after_samples = int(args['distributed_after'])
    network_name = args['network']
    scale_up = bool(args['scale_up'])

    # Create distributed trainer factory
    print("Start training: quantize_bit = {}, epochs = {}, distributed_after = {}".format(num_quantization_bits, epochs, distributed_after_samples))
    create_dist_learner = lambda learner: distributed.data_parallel_distributed_learner(learner=learner,
                                                                                        num_quantization_bits=num_quantization_bits,
                                                                                        distributed_after=distributed_after_samples)
    train_data=os.path.join(data_path, 'train_map.txt')
    test_data=os.path.join(data_path, 'test_map.txt')
    mean=os.path.join(data_path, 'CIFAR-10_mean.xml')

    create_train_reader=lambda data_size: create_reader(train_data, mean, True, data_size, distributed_after_samples)
    test_reader=create_reader(test_data, mean, False, FULL_DATA_SWEEP)

    train_and_evaluate(create_train_reader, test_reader, network_name, epochs, create_dist_learner, scale_up)

    # Must call MPI finalize when process exit
    distributed.Communicator.finalize()
