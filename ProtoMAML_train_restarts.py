import os
import re
import gc
import argparse
from collections import defaultdict
from distutils.util import strtobool
import pickle

from pympler import tracker
from memory_profiler import profile
import psutil
import numpy as np
import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter
import torch.autograd.profiler as profiler
from transformers import AutoTokenizer, get_constant_schedule_with_warmup

from models.protomaml_seqtransformer import ProtoMAMLSeqTransformer
from data.utils.data_loader_numpy import StratifiedLoader, RandomTextLoader#, AdaptiveNKShotLoader
#from data.meta_dataset import MetaDataset
from data.unified_emotion_numpy import unified_emotion
from data.utils.sampling import dataset_sampler
from data.utils.tokenizer import manual_tokenizer, specials
from utils.metrics import logging_metrics
from utils.timing import Timer

#@profile
def meta_evaluate(model, dataset, tokenizer, device, config, timer, writer, episode):
    """
    Check model performance on all datasets.
    DO NOT CALL with torch.no_grad(). THIS IS HANDLED INSIDE.

    Args:
        model: model currently being trained
        dataset: current dataset
        tokenizer (AutoTokenizer): Huggingface's tokenizer to match the model
        config (dict): training config dictionary
        k (int, optional): size of the k-shot. Defaults to 16.

    Returns:
        dict: dictionary with metrics per task

    """

    model.eval()

    task_vals = defaultdict(dict)

    for task in dataset.lens.keys():

        task_loss, task_acc, task_f1 = [], [], []
        for i in range(config['n_eval_per_task']):

            sample_loss, sample_acc, sample_f1 = [], [], []

            datasubset = dataset.datasets[task]['test']
            dataloader = StratifiedLoader(datasubset, k=16,
                                          shuffle=True, max_batch_size=config['max_support_size'],
                                          tokenizer=tokenizer, device=device, classes_subset=False)

            # Inner loop
            # Support set
            support_labels, support_text, query_labels, query_text = next(dataloader)

            #model.train()
            model.adapt(support_labels, support_text, task_name=task)

            with torch.no_grad():
                for ii in range(config['n_eval_per_support']):

                    logits = model(query_text)
                    loss = model.lossfn(logits, query_labels)

                    mets = logging_metrics(logits.detach().cpu(),
                                           query_labels.detach().cpu())

                    sample_loss.append(loss.item())
                    sample_acc.append(mets['acc'] * 100)
                    sample_f1.append(mets['f1'] * 100)

            task_loss.append(np.mean(sample_loss))
            task_acc.append(np.mean(sample_acc))
            task_f1.append(np.mean(sample_f1))
            #print('Task {:}: {:}/{:} | Loss {:.4E}, Acc {:5.2f}, F1 {:5.2f}'.format(task, i+1, config['n_eval_per_task'], \
            #    task_loss[-1], task_acc[-1], task_f1[-1]))

        print(u"{:} | Eval | Task {:} | Loss {:.2E} \u00B1 {:.2E}, Acc {:5.2f} \u00B1 {:4.2f}, F1 {:5.2f} \u00B1 {:4.2f}".format(timer.dt(),\
            task, np.mean(task_loss), np.std(task_loss), np.mean(task_acc), np.std(task_acc), np.mean(task_f1), np.std(task_f1)))

        task_vals['loss'][task] = np.mean(task_loss)
        task_vals['acc'][task] = np.mean(task_acc)
        task_vals['f1'][task] = np.mean(task_f1)

    macro_f1 = np.mean(list(task_vals['f1'].values()))

    if config['logging']:
        writer.add_scalars('Loss/MetaEval', task_vals['loss'], episode)
        writer.add_scalars('Accuracy/MetaEval', task_vals['acc'], episode)
        writer.add_scalars('F1/MetaEval', task_vals['f1'], episode)
        writer.add_scalar('MacroF1/MetaEval', macro_f1, episode)

        writer.flush()

    return macro_f1, task_vals

#@profile
def train_episode(episode, dataset, tokenizer, config, model, timer, writer):

    task = dataset_sampler(dataset, sampling_method='sqrt')
    datasubset = dataset.datasets[task]['train']

    dataloader = StratifiedLoader(data_subset, k=16,
                                  shuffle=True, max_batch_size=config['max_support_size'],
                                  tokenizer=tokenizer, device=device)

    # Inner loop
    # Support set
    batch = next(dataloader)
    support_labels, support_input, query_labels, query_input = batch

    model.train()
    model.adapt(support_labels, support_input,
                task_name=task)

    # Outer loop
    # Query set
    model.eval()
    logits = model(query_input)
    loss = model.lossfn(logits, query_labels)

    model.backward(loss)

    logging(query_labels, logits, loss, task, dataloader.n_classes,
            support_labels.size(0), episode, timer, writer)

#@profile
def logging(labels, logits, loss, task, n_classes, batchsize, episode, timer, writer, print_ratios=True):
    with torch.no_grad():
        mets = logging_metrics(logits.detach(),
                                labels.detach())

        loss, acc, f1 = loss.detach().item(), mets['acc'], mets['f1']
        loss_ratio, acc_ratio, f1_ratio = loss /\
            np.log(n_classes), acc /(1/n_classes), f1/(1/n_classes)

        mem = psutil.Process(os.getpid()).memory_info().rss / 1024 ** 3

    print("{:} | Train | Episode {:>04d} | {:^20s}, N={:>02d}, k={:>02d} | {:} Loss {:.4f}, Acc {:.4f}, F1 {:.4f} | Memory: {:5.2f}Gb"\
        .format(timer.dt(),
                episode,
                task,
                n_classes,
                batchsize // n_classes,
                "Ratios" if print_ratios else "",
                loss_ratio if print_ratios else loss,
                acc_ratio if print_ratios else acc,
                f1_ratio if print_ratios else f1,
                mem))

    if config['logging']:
        writer.add_scalars('Loss/Train', {task: loss}, episode)
        writer.add_scalars('Accuracy/Train', {task: acc}, episode)
        writer.add_scalars('F1/Train', {task: f1}, episode)

        writer.add_scalars('LossRatio/Train',
                            {task: loss_ratio}, episode)
        writer.add_scalars('AccuracyRatio/Train',
                        {task: acc_ratio}, episode)
        writer.add_scalars('F1Ratio/Train', {task: f1_ratio}, episode)

        writer.add_scalar('Memory Usage', mem, episode)

        writer.flush()

def data_examples(dataset, tokenizer):
    print('\nExample data')
    for task in dataset.lens.keys():
        datasubset = dataset.datasets[task]['train']
        dataloader = StratifiedLoader(datasubset,
                                        device='cpu',
                                        k=1)
        support_labels, support_text, _, _ = next(dataloader)

        print(task)

        label_map = {v: k for k, v in dataset.label_map[task].items()}
        tokenized_texts = list(
            map(tokenizer.decode, tokenizer(list(support_text))['input_ids']))
        for txt, label in zip(tokenized_texts, support_labels):
            print(label_map[label], txt)
        print()

#@profile
def train(config):

    with profiler.profile(profile_memory=True) as prof, torch.autograd.set_detect_anomaly(True):
        # Set to debug in case of various weird tests
        if (not config['gpu']) and (not config['debug']):
            config['debug'] = True
            print(f"Setting debug mode to {config['debug']}.")
        if config['debug']:
            config['version'] = 'debug'

        #######################
        # Logging Directories #
        #######################
        log_dir = os.path.join(config['checkpoint_path'], config['version'])

        os.makedirs(log_dir, exist_ok=True)
        os.makedirs(os.path.join(log_dir, 'tensorboard'), exist_ok=True)
        os.makedirs(os.path.join(log_dir, 'checkpoint'), exist_ok=True)
        print(f"Saving models and logs to {log_dir}")

        with open(os.path.join(log_dir, 'checkpoint', 'hparams.pickle'), 'wb') as file:
            pickle.dump(config, file)

        ## Initialization
        # Device
        device = torch.device('cuda' if (torch.cuda.is_available() and config['gpu']) else 'cpu')

        # Build the tensorboard writer
        writer = SummaryWriter(os.path.join(log_dir, 'tensorboard'))

        #######################
        # Model and Data Init #
        #######################
        # Load in the data
        #dataset = MetaDataset(include=config['include'], verbose=False)
        #dataset.prep(text_tokenizer=manual_tokenizer)
        if not config['random_data']:
            dataset = unified_emotion(file_path="./data/datasets/unified-dataset.jsonl",
                                      include=config['include'], verbose=False)
            dataset.prep(text_tokenizer=manual_tokenizer)

        # Huggingface tokenizer
        tokenizer = AutoTokenizer.from_pretrained(config['encoder_name'] if config['encoder_name'].lower() != 'random' else 'bert-base-cased')
        tokenizer.add_special_tokens({'additional_special_tokens': specials()})

        # Initialization of model
        config['lossfn'] = nn.CrossEntropyLoss
        config['vocab_length'] = len(tokenizer.vocab)

        model = ProtoMAMLSeqTransformer(config).to(device)
        print(f"Model loaded succesfully on device: {model.get_device()}")

        if config['encoder_name'].lower() != 'random':
            model.model_shared.encoder.model.resize_token_embeddings(len(tokenizer.vocab))

        # Meta optimizers
        shared_optimizer = optim.SGD(model.model_shared.parameters(), lr=config['meta_lr'])
        shared_lr_schedule = get_constant_schedule_with_warmup(shared_optimizer, config['warmup_steps'])

        # Check the dataloader
        if not config['random_data']:
            data_examples(dataset, tokenizer)

        #####################
        # Trainer Variables #
        #####################
        best_macro_f1 = 0.0
        curr_patience = config['patience']
        init_episode = 1

        checkpoint_save_path = os.path.join(log_dir, "checkpoint")

        # Meta-evaluate prior to training for decent baseline
        if config['warm_restart']:

            checkpoints = os.listdir(checkpoint_save_path)
            try:
                latest_checkpoint = checkpoints[list(
                    map(lambda x: 'latest_model' in x, checkpoints)).index(True)]

                with open(os.path.join(checkpoint_save_path, latest_checkpoint), 'rb') as file:
                    model.load_state_dict(torch.load(
                        file, map_location=torch.device(model.get_device())))

                with open(os.path.join(checkpoint_save_path, "latest_trainer.pickle"), 'rb') as file:
                    trainer_state_dict = pickle.load(file)

                init_episode = trainer_state_dict['episode'] + 1
                macro_f1 = trainer_state_dict['macro_f1']
                best_macro_f1 = trainer_state_dict['best_macro_f1']
                curr_patience = trainer_state_dict['curr_patience']

                for _ in range(init_episode-1):
                    shared_lr_schedule.step()

            except ValueError:
                print("Latest checkpoint not found. Cannot perform warm restart.")

        if config['overfit_on_single_query']:
            assert len(config['include']) == 1, "Too many datasets for overfit test."
            dataloader = StratifiedLoader(dataset=dataset.datasets[list(dataset.lens.keys())[0]]['test'],
                                          device=device, k=16, tokenizer=tokenizer)
            overfit_labels, overfit_text, _, _ = next(dataloader)

        ##############
        # Train Loop #
        ##############

        # Set-up the timer
        timer = Timer()

        for episode in range(init_episode, min((init_episode + config['manual_crash']) if config['manual_crash']>0 else config['max_episodes']+1, config['max_episodes']+1)):

            if curr_patience < 0:
                print("Stopping early.")
                raise KeyboardInterrupt

            ############
            # Training #
            ############

            if not config['random_data']:
                task = dataset_sampler(dataset, sampling_method='sqrt')

                datasubset = dataset.datasets[task]['train']

                dataloader = StratifiedLoader(datasubset, k=16,
                                              shuffle=True, max_batch_size=config['max_support_size'],
                                              tokenizer=tokenizer, device=device, classes_subset=False)
            else:
                task = 'Random'

                dataloader = RandomTextLoader(tokenizer,
                                              batch_size=config['max_support_size'],
                                              device=device)

            for _ in range(config['n_outer']):

                # Inner loop
                # Support set
                batch = next(dataloader)
                support_labels, support_input, query_labels, query_input = batch

                model.train()
                model.adapt(support_labels, support_input,
                            task_name=task)

                # Outer loop
                # Query set
                model.eval()
                logits = model(query_input)
                loss = model.lossfn(logits, query_labels)

                model.backward(loss)

                logging(query_labels, logits, loss, task, dataloader.n_classes,
                        support_labels.size(0), episode, timer, writer)

            shared_optimizer.step()
            shared_lr_schedule.step()

            model.model_shared.zero_grad()
            model.model_task.zero_grad()

            ##############
            # Evaluation #
            ##############
            if (episode % config['eval_every_n']) == 0:

                print('')
                macro_f1, _ = meta_evaluate(model, dataset, tokenizer, device,
                                            config, timer, writer, episode)

                for file in os.listdir(checkpoint_save_path):

                    if 'best_model' in file:
                        ep = re.match(r".+macrof1_([0-9]+)", file)
                        if int(ep.group(1)) < macro_f1:
                            os.remove(os.path.join(checkpoint_save_path, file))

                    if 'latest_model' in file:
                        ep = re.match(r".+episode_([0-9]+).+", file)
                        if int(ep.group(1)) <= episode:
                            os.remove(os.path.join(checkpoint_save_path, file))


                if macro_f1 >= best_macro_f1:
                    save_name = "best_model-episode_{:}-macrof1_{:5.2f}".format(episode, macro_f1)

                    with open(os.path.join(checkpoint_save_path, save_name), 'wb') as f:
                        model.model_task, model.W_task, model.b = None, None, None

                        torch.save(model.state_dict(), f)

                    print(f"New best macro F1. Saving model as {save_name}")
                    best_macro_f1 = macro_f1
                    curr_patience = config['patience']
                else:
                    if episode > config['min_episodes'] or (config['manual_crash'] > 0):
                        curr_patience -= 1
                    print(f"Model did not improve with macrof1={macro_f1}. Patience is now {curr_patience}")

                save_name = "latest_model-episode_{:}-macrof1_{:5.2f}".format(episode, macro_f1)

                with open(os.path.join(checkpoint_save_path, save_name), 'wb') as f:

                    model.model_task, model.W_task, model.b_task = None, None, None

                    torch.save(model.state_dict(), f)

                with open(os.path.join(checkpoint_save_path, "latest_trainer.pickle"), 'wb') as f:

                    pickle.dump({'episode': episode,
                                'macro_f1': macro_f1,
                                'best_macro_f1': best_macro_f1,
                                'curr_patience': curr_patience},
                                f)

                print('')

            #if (config['manual_crash'] > 0) and ((episode - (init_episode-1)) >= config['manual_crash']):
            #    print("Manual break.")
            #    raise KeyboardInterrupt

if __name__ == '__main__':

    parser = argparse.ArgumentParser(formatter_class=argparse.ArgumentDefaultsHelpFormatter)

    ## Dataset Initialization Hyperparameters
    parser.add_argument('--include', type=str, nargs='+', default=['dailydialog', 'emoint', 'grounded_emotions'],  # ['crowdflower', 'dailydialog', 'electoraltweets', 'emoint', 'emotion-cause', 'grounded_emotions', 'go_emotions', 'ssec', 'tec'],
                        choices=['crowdflower', 'dailydialog', 'electoraltweets', 'emoint', 'emotion-cause', 'grounded_emotions', 'go_emotions', 'ssec', 'tec'],
                        help='Datasets to include.')

    parser.add_argument('--max_support_size', type=int, default=8,
                        help='Batch size during adaptation to support set.')

    ## Model Initialization Hyperparameters
    # Encoder
    parser.add_argument('--encoder_name', type=str, default='random',
                        help='Pretrained encoder model matching import from Hugginface, e.g. "bert-base-uncased", "vinai/bertweet-base".')

    parser.add_argument('--nu', type=int, default=5,
                        help='Max layer to keep frozen. 11 keeps enitre model frozen, -1 entirely trainable.')

    # Classifier
    parser.add_argument('--hidden_dims', type=int, nargs='+', default=[256, 128],
                        help='Hidden dimensions of the MLP. Pass a space separated list, e.g. "--hidden_dims 256 128".')

    parser.add_argument('--act_fn', type=str, default='Tanh',
                        help='Which activation to use. Currently either Tanh or ReLU.')

    ## Meta-training Hyperparameters
    # MAML

    parser.add_argument('--n_inner', type=int, default=5,
                        help='Number of inner loop (MAML) steps to take.')

    parser.add_argument('--n_outer', type=int, default=1,
                        help='Number of outer loop (MAML) steps to take. Samples a new task per steps. Values greater than 1 essentially mean accumulated gradients.')

    parser.add_argument('--max_episodes', type=int, default=15000,
                        help='Maximum number of episodes to take.')

    parser.add_argument('--min_episodes', type=int, default=0,
                        help='Minimum number of episodes to take. Starts checking early stopping after this is reached.')

    parser.add_argument('--patience', type=int, default=2,
                        help='Number of evaluations without improvement before stopping training.')

    # Optimizer
    parser.add_argument('--meta_lr', type=float, default=1e-4,
                        help='Learning rate for the shared model update.')

    parser.add_argument('--inner_lr', type=float, default=1e-3,
                        help='Learning rate for the task-specific model update.')

    parser.add_argument('--output_lr', type=float, default=1e-1,
                        help='Learning rate for the softmax classification layer update.')

    parser.add_argument('--warmup_steps', type=float, default=100,
                        help='Learning warm-up steps for the shared model update. Uses linear schedule to constant.')

    parser.add_argument('--clip_val', type=float, default=5,
                        help='Max norm of gradients to avoid exploding gradients.')

    ## Meta-eval Hyperparameters

    parser.add_argument('--n_eval_per_task', type=int, default=25,
                        help='Number of support sets to try for a single task.')

    parser.add_argument('--n_eval_per_support', type=int, default=1,
                        help='Number of different batches to evaluate on per support set.')

    parser.add_argument('--eval_every_n', type=int, default=250,
                        help='Number of different batches to evaluate on per support set.')

    ## MISC
    # Versioning, logging
    parser.add_argument('--version', type=str, default='test',
                        help='Construct model save name using versioning.')

    parser.add_argument('--checkpoint_path', type=str, default="./checkpoints/ProtoMAMLwRestarts",
                        help='Directory to save models to.')

    # Debugging
    parser.add_argument('--overfit_on_single_query', default=False, type=lambda x: bool(strtobool(x)),
                        help='As a check for learning, overfits model on a single query set.')
    parser.add_argument('--random_data', default=False, type=lambda x: bool(strtobool(x)),
                        help='Check for memory leak when using random data')
    parser.add_argument('--debug', default=False, type=lambda x: bool(strtobool(x)),
                        help='Whether to run in debug mode.')
    parser.add_argument('--gpu', default=True, type=lambda x: bool(strtobool(x)),
                        help='Whether to train on GPU (if available) or CPU.')
    parser.add_argument('--logging', default=True, type=lambda x: bool(strtobool(x)),
                        help='Whether to log using Tensorboard or not.')

    ## RESTART
    parser.add_argument('--warm_restart', default=True, type=lambda x: bool(strtobool(x)),
                        help='Whether or not to look for pre-trained model to restart.')
    parser.add_argument('--manual_crash', default=250, type=int,
                        help='As a check for learning, overfits model on a single query set.')

    config = vars(parser.parse_args())

    train(config)