# ******************************************************************************
# Copyright 2017-2018 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ******************************************************************************
from __future__ import division, print_function, absolute_import

import h5py
import os
import logging
import time
import numpy as np
from tqdm import tqdm
from enum import Enum
from timeit import default_timer

logger = logging.getLogger(__name__)


class CallbackPhase(Enum):
    train_pre_ = 0
    train_post = 1
    interval_pre_ = 2
    interval_post = 3
    minibatch_pre_ = 4
    minibatch_post = 5


def make_default_callbacks(transformer, output_file, frequency, train_computation,
                           total_iterations, eval_set=None,
                           eval_feed_wrapper=None, loss_computation=None,
                           enable_top5=False, use_progress_bar=True):

    cbs = CallbackContainer(transformer, output_file, total_iterations)

    cbs.append(TrainCostCallback(train_computation))

    cbs.append(TrainLoggerCallback(frequency))

    if eval_set is not None:
        cbs.append(LossCallback(frequency,
                                eval_set,
                                eval_feed_wrapper,
                                loss_computation,
                                enable_top5))

    if use_progress_bar:
        cbs.append(ProgressCallback())

    return cbs


class CallbackContainer(object):
    def __init__(self, transformer, output_file, total_iterations, callback_list=[]):
        self.transformer = transformer
        '''
        just store a list of callbacks
        '''
        self._callbacks = callback_list
        if output_file is None:
            if hasattr(self, 'callback_data'):
                del self.callback_data
            # self.name sould give a unique filename
            self.callback_data = h5py.File(self.name, driver='core', backing_store=False)
        else:
            if os.path.isfile(output_file):
                logger.warn("Overwriting output file %s", output_file)
                os.remove(output_file)
            self.callback_data = h5py.File(output_file, "w")

        config = self.callback_data.create_group('config')
        config.attrs['total_iterations'] = total_iterations

    def __del__(self):
        try:
            self.callback_data.close()
        except Exception:
            pass

    def __iter__(self):
        return self._callbacks.__iter__()

    def append(self, cb):
        """
        Appends a callback

        Arguments:
            cb: The callback object to append.
        """
        self._callbacks.append(cb)

    def insert(self, index, cb):
        """
        Inserts a callback
        Arguments:
            index : Index to insert at
            cb    : The callback object to insert
        """
        self._callbacks.insert(index, cb)

    def __call__(self, phase, data=None, idx=None):
        for c in self._callbacks:
            c(self.transformer, self.callback_data, phase, data, idx)


class Callback(object):
    def __call__(self, transformer, callback_data, phase, data, idx):
        pass


class TrainCostCallback(Callback):
    """
    Callback for computing average training cost periodically during training.
    """

    def __init__(self, computation):
        self.computation = computation

    def __call__(self, transformer, callback_data, phase, data, idx):
        if phase == CallbackPhase.train_pre_:
            transformer.set_output_statistics_file(callback_data)
            iterations = callback_data['config'].attrs['total_iterations']
            callback_data.create_dataset("cost/train", (iterations,))
            # clue in the data reader to use the 'minibatch' time_markers
            callback_data['cost/train'].attrs['time_markers'] = 'minibatch'
        elif phase == CallbackPhase.minibatch_post:
            # This is where the training function is actually called
            callback_data['cost/train'][idx] = self.computation(data)['batch_cost']
        elif phase == CallbackPhase.train_post:
            transformer.save_output_statistics_file()


class TrainSaverCallback(Callback):

    def __init__(self, saver, filename, frequency):
        self.saver = saver
        self.filename = filename
        self.frequency = frequency

    def __call__(self, transformer, callback_data, phase, data, idx):
        if phase == CallbackPhase.minibatch_post:
            if ((idx + 1) % self.frequency == 0):
                self.saver.save(filename=self.filename + "_" + str(idx))


class FeedAddWrapper:

    def __init__(self, wrapper=None, holder=None, wrapper_kwargs=None, clear_feed=False):
        self.wrapper = wrapper
        self.holder = holder
        self.wrapper_kwargs = wrapper_kwargs
        self.clear_feed = clear_feed

    def __call__(self, data, step):
        if self.clear_feed:
            data.clear()
        if self.wrapper is not None:
            data[self.holder] = self.wrapper(step=step, **self.wrapper_kwargs)


class RunTimerCallback(Callback):
    """
    Callback which tracks the total training time.
    """
    def __call__(self, transformer, callback_data, phase, data, idx):
        if phase == CallbackPhase.train_pre_:
            self.timing = callback_data.create_group("time/train")
            self.timing.create_dataset("start_time", (1,), dtype='float64')
            self.timing.create_dataset("end_time", (1,), dtype='float64')
            self.timing['start_time'][0] = time.time()
            self.timing['start_time'].attrs['units'] = 'seconds'
        elif phase == CallbackPhase.train_post:
            self.timing['end_time'][0] = time.time()
            self.timing['end_time'].attrs['units'] = 'seconds'


class ProgressCallback(Callback):
    """
    Callback shows overall progress
    """
    def __call__(self, transformer, callback_data, phase, data, idx):
        if phase == CallbackPhase.train_pre_:
            self.tpbar = tqdm(desc="Overall",
                              unit="minibatches",
                              ncols=80,
                              total=callback_data['config'].attrs['total_iterations'])
        elif phase == CallbackPhase.train_post:
            self.tpbar.close()
        elif phase == CallbackPhase.minibatch_post:
            self.tpbar.update(1)


class TrainLoggerCallback(Callback):
    """
    Callback for logging training progress.

    Arguments:
        frequency (int, optional): how often (in minibatches) to log training info.
    """
    def __init__(self, frequency):
        self.frequency = frequency

    def __call__(self, transformer, callback_data, phase, data, idx):
        if phase == CallbackPhase.minibatch_post:
            if ((idx + 1) % self.frequency == 0):
                interval = slice(idx + 1 - self.frequency, idx)
                train_cost = callback_data["cost/train"][interval].mean()
                tqdm.write("Interval {} Iteration {} complete.  Avg Train cost: {}".format(
                    idx // self.frequency + 1, idx + 1, train_cost))


class LossCallback(Callback):
    """
    Callback for calculating the loss on a given dataset periodically during training.

    Arguments:
        eval_set (NervanaDataIterator): dataset to evaluate
        interval_freq (int, optional): how often (in iterations) to log info.
    """

    def __init__(self, frequency, dataset, eval_feed_wrapper, interval_loss_comp, enable_top5):
        self.frequency = frequency
        self.dataset = dataset
        self.eval_feed_wrapper = eval_feed_wrapper
        self.interval_loss_comp = interval_loss_comp
        self.enable_top5 = enable_top5

    def __call__(self, transformer, callback_data, phase, data, idx):
        if phase == CallbackPhase.train_pre_:
            self.total_iterations = callback_data['config'].attrs['total_iterations']
            num_intervals = self.total_iterations // self.frequency
            for loss_name in self.interval_loss_comp.output_keys:
                callback_data.create_dataset("cost/{}".format(loss_name), (num_intervals,))
            if self.enable_top5:
                callback_data.create_dataset("cost/top_1_acc", (num_intervals,))
                callback_data.create_dataset("cost/top_5_acc", (num_intervals,))
            callback_data.create_dataset("time/loss", (num_intervals,))
        elif phase == CallbackPhase.train_post:
            losses = loop_eval(dataset=self.dataset,
                               computation=self.interval_loss_comp,
                               enable_top5=self.enable_top5,
                               eval_feed_wrapper=self.eval_feed_wrapper)
            tqdm.write("Training complete.  Avg losses: {}".format(losses))
        elif phase == CallbackPhase.minibatch_post and ((idx + 1) % self.frequency == 0):
            start_loss = default_timer()
            interval_idx = idx // self.frequency

            losses = loop_eval(dataset=self.dataset,
                               computation=self.interval_loss_comp,
                               enable_top5=self.enable_top5,
                               eval_feed_wrapper=self.eval_feed_wrapper)

            for loss_name, loss in losses.items():
                callback_data["cost/{}".format(loss_name)][interval_idx] = loss

            callback_data["time/loss"][interval_idx] = (default_timer() - start_loss)
            tqdm.write("Interval {} Iteration {} complete.  Avg losses: {}".format(
                interval_idx + 1, idx + 1, losses))


def loop_train(dataset, computation, callbacks, train_feed_wrapper=None):
    callbacks(CallbackPhase.train_pre_)
    for mb_idx, data in enumerate(dataset):
        if train_feed_wrapper is not None:
            train_feed_wrapper(data=data, step=mb_idx)
        data['iteration'] = mb_idx
        callbacks(CallbackPhase.minibatch_pre_, data, mb_idx)
        callbacks(CallbackPhase.minibatch_post, data, mb_idx)
    callbacks(CallbackPhase.train_post)


def loop_eval(dataset, computation, enable_top5, eval_feed_wrapper=None):
    dataset.reset()
    all_results = None

    def top_results(inference_prob, data):
        if inference_prob is not None:
            top5_sorted = np.argsort(inference_prob, axis=0)[-5:]
            data_tr = data['label'].T  # true labels
            top1_results = np.any(np.equal(data_tr, top5_sorted[-1:]), axis=0)
            top5_results = np.any(np.equal(data_tr, top5_sorted), axis=0)
            return {'top_1_acc': top1_results, 'top_5_acc': top5_results}

    for data in dataset:
        if eval_feed_wrapper is not None:
            eval_feed_wrapper(data=data, step=1)
        data['iteration'] = 1
        results = computation(data)
        if enable_top5:
            if 'results' in results.keys():
                inference_prob = results.pop('results')
                results.update(top_results(inference_prob, data))
        if all_results is None:
            all_results = {k: list(rs) for k, rs in results.items()}
        else:
            for k, rs in results.items():
                all_results[k].extend(list(rs))
    reduced_results = {k: np.mean(ar[:dataset.ndata]) for k, ar in all_results.items()}
    return reduced_results
