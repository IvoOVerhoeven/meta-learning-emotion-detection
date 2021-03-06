import re
from copy import deepcopy

from memory_profiler import profile
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from models.seqtransformer import SeqTransformer
from modules.mlp_clf import MLP

class ProtoMAMLSeqTransformer(nn.Module):

    def __init__(self, config):
        """A transformer for sequence classification with the ability to train via FO-MAML

        Args:
            config (dict): dictionary with corresponding args.
        """
        super().__init__()

        self.model_shared = SeqTransformer(config)
        self.model_task = None

        self.n_inner = config['n_inner']
        self.n_outer = config['n_outer']

        self.inner_lr = config['inner_lr']
        self.output_lr = config['output_lr']
        self.lossfn = config['lossfn']()

        self.task_name = None
        self.W_task = None
        self.b_task = None

        self.clip_val = config['clip_val']

    def train(self):
        """Set model(s) to train.
        """

        self.model_shared.train()
        if self.model_task != None:
            self.model_task.train()

    def eval(self):
        """Set model(s) to eval.
        """

        self.model_shared.eval()
        if self.model_task != None:
            self.model_task.eval()

    def get_device(self):
        """
        Hacky method for checking model device.
        Requires all parameters to be on same device.
        """
        #assert next(self.model_shared.parameters()).device == next(self.model_task.parameters()).device,\
        #    "Models' devices do not match"

        self.device = next(self.model_shared.parameters()).device

        return self.device

    def _get_updateable_parameters(self, model):
        return [param for param in model.parameters() if param.requires_grad]

    def forward(self, model_input):
        """
        Task-specific classification of a sequence.
        For safety, will not classify without initial adaptation, but otherwise will classify with whatever
        the current task-specific parameters are.

        Args:
            labels (LongTensor): batch labels
            model_input (dict of Tensors): model input from Huggingface tokenizer. Unrolled into model.


        Returns:
            Tensor: tensor with logits.
        """

        if self.W_task == None or self.b_task == None:
            raise ValueError('No task-specific model specified yet.')

        y = self.model_task(model_input)
        logits = F.linear(y, self.W_task, self.b_task)

        return logits

    def _generate_protoypes(self, labels, model_input):

        y = self.model_shared(model_input)

        prototypes = [torch.mean(y[labels == i], dim=0)
                      for i in torch.unique(labels)]
        prototypes = torch.stack(prototypes)

        return prototypes

    def generate_clf_weights(self, labels, model_input):
        """Generates the classification layer weights from prototypes derived from a single batch.

        Args:
            labels (LongTensor): batch labels
            model_input (dict of Tensors): model input from Huggingface tokenizer. Unrolled into model.

        Returns:
            tuple of tensors: first tensor are weights, second biases
        """

        prototypes = self._generate_protoypes(labels, model_input)

        W_init = 2 * prototypes
        b_init = -torch.norm(prototypes, p=2, dim=1)

        return W_init, b_init

    #@profile
    def adapt(self, labels, model_input, task_name=None, verbose=False):
        """Perform MAML adaption with Prototypical initialization of classification layer.

        Args:
            labels (LongTensor): batch labels
            model_input (dict of Tensors): model input from Huggingface tokenizer. Unrolled into model.
            task_name (str, optional): name of current task for administration within model. Defaults to None.
            verbose (bool, optional): whether or not to print inner loop updates. Defaults to False.
        """

        self.task_name = task_name

        # Clone model for task specific episode model
        del self.model_task, self.W_task, self.b_task
        self.model_task = deepcopy(self.model_shared)#.to(self.get_device())
        self.model_task.zero_grad()

        #task_optimizer = optim.SGD(self.model_task.parameters(),
        #                           lr=self.inner_lr)

        # Generate initial classification weights
        W_init, b_init = self.generate_clf_weights(labels, model_input)

        # Detach the initial weights from task-specific model
        self.W_task, self.b_task = W_init.detach(), b_init.detach()
        self.W_task.requires_grad, self.b_task.requires_grad = True, True

        #output_optimizer = optim.SGD([self.W_task, self.b_task], lr=self.output_lr)

        for i in range(self.n_inner):

            # Embed, encode, classify and compute loss
            logits = self.forward(model_input)
            loss = self.lossfn(logits, labels)

            # Calculate the gradients on output and task parameters here
            task_grads = torch.autograd.grad(loss,
                                             [self.W_task, self.b_task] +
                                             self._get_updateable_parameters(self.model_task))

            # Store task-specific gradients
            for param, grad in zip([self.W_task, self.b_task] +
                                   self._get_updateable_parameters(self.model_task),
                                   task_grads):
                param.grad = grad

            if self.clip_val > 0:
                torch.nn.utils.clip_grad_norm_(self._get_updateable_parameters(self.model_task),
                                               self.clip_val)

            # Update the parameters
            #output_optimizer.step()
            #task_optimizer.step()

            #output_optimizer.zero_grad()
            #task_optimizer.zero_grad()

            if verbose:
                print("\tInner {} | Loss {:.4E}".format(
                    i, loss.detach().item()))

            del task_grads, logits, loss

        self.W_task = W_init + (self.W_task - W_init).detach()
        self.b_task = b_init + (self.b_task - b_init).detach()

    #@profile
    def backward(self, loss):
        """Backpropagate a loss on the task-specific model to the shared model parameters

        Args:
            loss (Tensor)
        """

        # Calculate gradients for task-specific parameters
        task_grads = torch.autograd.grad(loss, self._get_updateable_parameters(self.model_task),
                                         retain_graph=True)

        # Calculate gradients for shared model parameters
        shared_grads = torch.autograd.grad(loss, self._get_updateable_parameters(self.model_shared))

        # Accumulate gradients
        for param, g_task, g_shared in zip(self._get_updateable_parameters(self.model_shared), task_grads, shared_grads):
            if param.grad == None:
                param.grad = (g_shared + g_task).detach() / self.n_outer
            else:
                param.grad += (g_shared + g_task).detach() / self.n_outer

        if self.clip_val > 0:
            torch.nn.utils.clip_grad_norm_(self._get_updateable_parameters(self.model_shared),
                                           self.clip_val)

        del task_grads, shared_grads
