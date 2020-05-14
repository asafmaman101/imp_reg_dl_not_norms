from abc import ABC, abstractmethod
from collections import OrderedDict

import torch
import torch.utils.data

from .callbacks import ComposeCallback, Callback
from .fit_output import FitOutput
from .stop_fit_iteration import StopFitIteration
from .value_store import ValueStore
from ..evaluation.evaluators.evaluator import VoidEvaluator, TrainEvaluator, Evaluator
from ..serialization.torch_serializable import TorchSerializable
from ..utils import module as module_utils


class Trainer(TorchSerializable, ABC):
    """
    Model train functionality wrapper. Allows for configurable model training.
    """

    def __init__(self, model, optimizer, train_evaluator: TrainEvaluator = VoidEvaluator(), val_evaluator: Evaluator = VoidEvaluator(),
                 callback: Callback = None, device=module_utils.get_device()):
        self.model = model
        self.optimizer = optimizer
        self.train_evaluator = train_evaluator
        self.val_evaluator = val_evaluator
        self.value_store = ValueStore()
        self.callback = callback if callback is not None else ComposeCallback(OrderedDict())
        self.device = device
        self.epoch = -1

    @abstractmethod
    def batch_update(self, batch_num: int, batch) -> dict:
        """
        Runs a single train batch update.
        :param batch_num: current batch number (starts from 0).
        :param batch: batch loaded from the data loader.
        :return: output that will be given to the train evaluator. Usually the output will be a dictionary with the loss value and model outputs and
        labels if exists for the batch.
        """
        raise NotImplementedError

    def fit(self, data_loader: torch.utils.data.DataLoader, num_epochs: int = 1, validate_every: int = 1) -> FitOutput:
        """
        Trains model using the training data_loader for the specified number of epochs.
        :param data_loader: training data loader.
        :param num_epochs: number of training epochs.
        :param validate_every: run validation phase every this number of epochs.
        :return: FitOutput object with saved tracked values and information on the fitting process.
        """
        output = FitOutput(self.value_store)

        original_train_mode = self.model.training
        start_epoch = self.epoch
        try:
            self.model.to(self.device)
            self.callback.on_fit_start(self, num_epochs)

            for i in range(num_epochs):
                self.epoch += 1
                self.callback.on_epoch_start(self)

                self.__train(data_loader)
                if (self.epoch + 1) % validate_every == 0:
                    self.__validate()

                self.callback.on_epoch_end(self)
        except StopFitIteration as e:
            # If StopFitIteration was thrown (usually by a callback) should exit gracefully the fitting process.
            self.callback.on_exception(self, e)
            output.exception = e
        except Exception as e:
            self.callback.on_exception(self, e)
            raise
        finally:
            self.model.train(original_train_mode)

        output.update_train_tracked_values(self.train_evaluator.get_tracked_values())
        output.update_val_tracked_values(self.val_evaluator.get_tracked_values())
        self.callback.on_fit_end(self, self.epoch - start_epoch, output)
        return output

    def __train(self, data_loader: torch.utils.data.DataLoader):
        self.model.train()
        self.callback.on_epoch_train_start(self, len(data_loader))
        self.train_evaluator.epoch_start(self.epoch)

        for batch_num, batch in enumerate(data_loader):
            self.callback.on_train_batch_start(self, batch_num)

            output = self.batch_update(batch_num, batch)
            with torch.no_grad():
                metric_values = self.train_evaluator.evaluate_batch(output)

            self.callback.on_train_batch_end(self, batch_num, output, metric_values)

        self.train_evaluator.epoch_end(self.epoch)
        epoch_train_metric_values = {name: tracked_value.current_value
                                     for name, tracked_value in self.train_evaluator.get_tracked_values().items()}
        self.callback.on_epoch_train_end(self, epoch_train_metric_values)

    def __validate(self):
        self.model.eval()
        self.callback.on_epoch_validation_start(self)
        self.val_evaluator.epoch_start(self.epoch)

        with torch.no_grad():
            metric_values = self.val_evaluator.evaluate()

        self.val_evaluator.epoch_end(self.epoch)
        self.callback.on_epoch_validation_end(self, metric_values)

    def state_dict(self) -> dict:
        return {
            "model": self.model.state_dict(),
            "optimizer": self.optimizer.state_dict(),
            "value_store": self.value_store.state_dict(),
            "train_evaluator": self.train_evaluator.state_dict(),
            "val_evaluator": self.val_evaluator.state_dict(),
            "callback": self.callback.state_dict(),
            "epoch": self.epoch
        }

    def load_state_dict(self, state_dict: dict):
        self.model.load_state_dict(state_dict["model"])
        self.optimizer.load_state_dict(state_dict["optimizer"])
        self.value_store.load_state_dict(state_dict["value_store"])
        self.train_evaluator.load_state_dict(state_dict["train_evaluator"])
        self.val_evaluator.load_state_dict(state_dict["val_evaluator"])
        self.callback.load_state_dict(state_dict["callback"])
        self.epoch = state_dict["epoch"]
