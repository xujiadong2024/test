# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
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
"""
Fine-tuning the library models for language modeling on a text file (GPT, GPT-2, BERT, RoBERTa).
GPT and GPT-2 are fine-tuned using a causal language modeling (CLM) loss while BERT and RoBERTa are fine-tuned
using a masked language modeling (MLM) loss.
"""

from __future__ import absolute_import
import os
import sys
import pickle
import torch
import json
import random
import logging
import argparse
import numpy as np
from io import open
from itertools import cycle
import torch.nn as nn
from rouge import Rouge
from gensim.summarization.bm25 import BM25
from nltk.translate.bleu_score import sentence_bleu
from tqdm import tqdm, trange
from torch.utils.data import DataLoader, Dataset, SequentialSampler, RandomSampler, TensorDataset
from torch.utils.data.distributed import DistributedSampler
from transformers import (WEIGHTS_NAME, AdamW, get_linear_schedule_with_warmup,
                          RobertaConfig, RobertaModel, RobertaTokenizer)
from transformers import T5Tokenizer, T5ForConditionalGeneration

MODEL_CLASSES = {'roberta': (RobertaConfig, RobertaModel, RobertaTokenizer)}

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                    datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

os.environ['CUDA_LAUNCH_BLOCKING'] = '1'


class Example(object):
    """A single training/test example."""

    def __init__(self,
                 idx,
                 source_code,
                 review_code,
                 target,
                 ):
        self.idx = idx
        self.source_code = source_code
        self.review_code = review_code
        self.target = target


def read_examples(filename):
    """Read examples from filename."""
    examples = []
    with open(filename, 'rb') as f:
        data = json.load(f)
        data = data['data']
        for idx, js in enumerate(data):
            if 'idx' not in js:
                js['idx'] = idx
            source_code = js['source_code'].replace(" <newline>", "").strip()
            review_code = js['review_code'].strip()
            comments = js['comments'].strip()
            idx = js['idx']
            examples.append(
                Example(
                    idx=idx,
                    source_code=source_code,
                    review_code=review_code,
                    target=comments,
                )
            )
    return examples


class InputFeatures(object):
    """A single training/test features for a example."""

    def __init__(self,
                 example_id,
                 source_ids,
                 target_ids,
                 source_mask,
                 target_mask,

                 ):
        self.example_id = example_id
        self.source_ids = source_ids
        self.target_ids = target_ids
        self.source_mask = source_mask
        self.target_mask = target_mask


def convert_examples_to_features(examples, tokenizer, args, stage=None):
    features = []

    task_prefix = 'Output review comments: '

    for example_index, example in enumerate(examples):

        source_code_tokens = tokenizer.tokenize(task_prefix + example.source_code.strip())[:args.max_source_length - 1]
        source_tokens = source_code_tokens + [tokenizer.eos_token]

        source_ids = tokenizer.convert_tokens_to_ids(source_tokens)
        source_mask = [1] * (len(source_tokens))
        padding_length = args.max_source_length - len(source_ids)
        source_ids = [0] * padding_length + source_ids
        source_mask = [0] * padding_length + source_mask

        if stage == "test":
            target_tokens = tokenizer.tokenize("None")
        else:
            target_tokens = tokenizer.tokenize(example.target)[:args.max_target_length - 1]
        target_tokens = target_tokens + [tokenizer.eos_token]
        target_ids = tokenizer.convert_tokens_to_ids(target_tokens)
        target_mask = [1] * len(target_ids)
        padding_length = args.max_target_length - len(target_ids)
        # target_ids+=[tokenizer.pad_token_id]*padding_length
        target_ids += [-100] * padding_length
        target_mask += [0] * padding_length

        if example_index < 5:
            if stage == 'train':
                logger.info("*** Example ***")
                logger.info("idx: {}".format(example.idx))

                logger.info("source_tokens: {}".format([x.replace('\u0120', '_') for x in source_tokens]))
                logger.info("source_ids: {}".format(' '.join(map(str, source_ids))))
                logger.info("source_mask: {}".format(' '.join(map(str, source_mask))))

                logger.info("target_tokens: {}".format([x.replace('\u0120', '_') for x in target_tokens]))
                logger.info("target_ids: {}".format(' '.join(map(str, target_ids))))
                logger.info("target_mask: {}".format(' '.join(map(str, target_mask))))

        features.append(
            InputFeatures(
                example_index,
                source_ids,
                target_ids,
                source_mask,
                target_mask,
            )
        )
    return features


def set_seed(args):
    """set random seed."""
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if args.n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--model_type", default=None, type=str, required=True,
                        help="Model type: e.g. roberta")
    parser.add_argument("--model_name_or_path", default=None, type=str, required=True,
                        help="Path to pre-trained model: e.g. roberta-base")
    parser.add_argument("--output_dir", default=None, type=str, required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")
    parser.add_argument("--load_model_path", default=None, type=str,
                        help="Path to trained model: Should contain the .bin files")
    ## Other parameters
    parser.add_argument("--train_filename", default=None, type=str,
                        help="The train filename. Should contain the .jsonl files for this task.")
    parser.add_argument("--dev_filename", default=None, type=str,
                        help="The dev filename. Should contain the .jsonl files for this task.")
    parser.add_argument("--test_filename", default=None, type=str,
                        help="The test filename. Should contain the .jsonl files for this task.")

    parser.add_argument("--config_name", default="", type=str,
                        help="Pretrained config name or path if not the same as model_name")
    parser.add_argument("--tokenizer_name", default="", type=str,
                        help="Pretrained tokenizer name or path if not the same as model_name")
    parser.add_argument("--max_source_length", default=64, type=int,
                        help="The maximum total source sequence length after tokenization. Sequences longer "
                             "than this will be truncated, sequences shorter will be padded.")
    parser.add_argument("--max_target_length", default=32, type=int,
                        help="The maximum total target sequence length after tokenization. Sequences longer "
                             "than this will be truncated, sequences shorter will be padded.")
    parser.add_argument("--num_return_sequences", default=1, type=int,
                        help="The sequence num return from each input")
    parser.add_argument("--length_penalty", default=2, type=int,
                        help="The length penalty of model generation")

    parser.add_argument("--do_train", action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval", action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_test", action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_lower_case", action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--no_cuda", action='store_true',
                        help="Avoid using CUDA when available")

    parser.add_argument("--train_batch_size", default=8, type=int,
                        help="Batch size per GPU/CPU for training.")
    parser.add_argument("--eval_batch_size", default=8, type=int,
                        help="Batch size per GPU/CPU for evaluation.")
    parser.add_argument('--gradient_accumulation_steps', type=int, default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument("--learning_rate", default=5e-5, type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--beam_size", default=10, type=int,
                        help="beam size for beam search")
    parser.add_argument("--weight_decay", default=0.0, type=float,
                        help="Weight deay if we apply some.")
    parser.add_argument("--adam_epsilon", default=1e-8, type=float,
                        help="Epsilon for Adam optimizer.")
    parser.add_argument("--max_grad_norm", default=1.0, type=float,
                        help="Max gradient norm.")
    parser.add_argument("--num_train_epochs", default=3.0, type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--max_steps", default=-1, type=int,
                        help="If > 0: set total number of training steps to perform. Override num_train_epochs.")
    parser.add_argument("--eval_steps", default=-1, type=int,
                        help="")
    parser.add_argument("--train_steps", default=-1, type=int,
                        help="")
    parser.add_argument("--warmup_steps", default=0, type=int,
                        help="Linear warmup over warmup_steps.")
    parser.add_argument("--local_rank", type=int, default=-1,
                        help="For distributed training: local_rank")
    parser.add_argument('--seed', type=int, default=42,
                        help="random seed for initialization")
    # print arguments
    args = parser.parse_args()
    logger.info(args)

    # Setup CUDA, GPU & distributed training
    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        args.n_gpu = torch.cuda.device_count()
    else:  # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        torch.distributed.init_process_group(backend='nccl')
        args.n_gpu = 1
    logger.warning("Process rank: %s, device: %s, n_gpu: %s, distributed training: %s",
                   args.local_rank, device, args.n_gpu, bool(args.local_rank != -1))
    args.device = device
    # Set seed
    set_seed(args)
    # make dir if output_dir not exist
    if os.path.exists(args.output_dir) is False:
        os.makedirs(args.output_dir)

    tokenizer = T5Tokenizer.from_pretrained(args.model_name_or_path)
    model = T5ForConditionalGeneration.from_pretrained(args.model_name_or_path)

    special_tokens_dict = {'additional_special_tokens': ['<review_tag>']}
    tokenizer.add_special_tokens(special_tokens_dict)
    model.resize_token_embeddings(len(tokenizer))

    if args.load_model_path is not None:
        logger.info("reload model from {}".format(args.load_model_path))
        model.load_state_dict(torch.load(args.load_model_path), strict=False)  # 加入strict=False，忽略参数不匹配

    model.to(device)
    if args.local_rank != -1:
        # Distributed training
        try:
            from apex.parallel import DistributedDataParallel as DDP
        except ImportError:
            raise ImportError(
                "Please install apex from https://www.github.com/nvidia/apex to use distributed and fp16 training.")

        model = DDP(model)
    elif args.n_gpu > 1:
        # multi-gpu training
        model = torch.nn.DataParallel(model)

    if args.do_train:
        # Prepare training data loader
        train_examples = read_examples(args.train_filename)
        train_features = convert_examples_to_features(train_examples, tokenizer, args, stage='train')
        all_source_ids = torch.tensor([f.source_ids for f in train_features], dtype=torch.long)
        all_source_mask = torch.tensor([f.source_mask for f in train_features], dtype=torch.long)
        all_target_ids = torch.tensor([f.target_ids for f in train_features], dtype=torch.long)
        all_target_mask = torch.tensor([f.target_mask for f in train_features], dtype=torch.long)
        train_data = TensorDataset(all_source_ids, all_source_mask, all_target_ids, all_target_mask)

        if args.local_rank == -1:
            train_sampler = RandomSampler(train_data)
        else:
            train_sampler = DistributedSampler(train_data)
        train_dataloader = DataLoader(train_data, sampler=train_sampler,
                                      batch_size=args.train_batch_size // args.gradient_accumulation_steps)

        num_train_optimization_steps = args.train_steps

        # Prepare optimizer and schedule (linear warmup and decay)
        no_decay = ['bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in model.named_parameters() if not any(nd in n for nd in no_decay)],
             'weight_decay': args.weight_decay},
            {'params': [p for n, p in model.named_parameters() if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]
        optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate, eps=args.adam_epsilon)
        scheduler = get_linear_schedule_with_warmup(optimizer, num_warmup_steps=args.warmup_steps,
                                                    num_training_steps=num_train_optimization_steps)

        # Start training
        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", len(train_examples))
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num epoch = %d", num_train_optimization_steps * args.train_batch_size // len(train_examples))

        model.train()
        dev_dataset = {}
        nb_tr_examples, nb_tr_steps, tr_loss, global_step, best_bleu, best_loss, best_rouge = 0, 0, 0, 0, 0, 1e6, 0
        bar = tqdm(range(num_train_optimization_steps), total=num_train_optimization_steps)
        train_dataloader = cycle(train_dataloader)
        eval_flag = True
        for step in bar:
            batch = next(train_dataloader)
            batch = tuple(t.to(device) for t in batch)
            source_ids, source_mask, target_ids, target_mask = batch
            # loss,_,_,_ = model(source_ids=source_ids,source_mask=source_mask,target_ids=target_ids,target_mask=target_mask)
            loss = model(input_ids=source_ids, attention_mask=source_mask, labels=target_ids).loss

            if args.n_gpu > 1:
                loss = loss.mean()  # mean() to average on multi-gpu.
            if args.gradient_accumulation_steps > 1:
                loss = loss / args.gradient_accumulation_steps
            tr_loss += loss.item()
            train_loss = round(tr_loss * args.gradient_accumulation_steps / (nb_tr_steps + 1), 4)
            bar.set_description("loss {}".format(train_loss))
            nb_tr_examples += source_ids.size(0)
            nb_tr_steps += 1
            loss.backward()

            if (nb_tr_steps + 1) % args.gradient_accumulation_steps == 0:
                # Update parameters
                optimizer.step()
                optimizer.zero_grad()
                scheduler.step()
                global_step += 1
                eval_flag = True

            if args.do_eval and ((global_step + 1) % args.eval_steps == 0) and eval_flag:
                # Eval model with dev dataset
                tr_loss = 0
                nb_tr_examples, nb_tr_steps = 0, 0
                eval_flag = False
                if 'dev_loss' in dev_dataset:
                    eval_examples, eval_data = dev_dataset['dev_loss']
                else:
                    eval_examples = read_examples(args.dev_filename)
                    eval_features = convert_examples_to_features(eval_examples, tokenizer, args, stage='dev')
                    all_source_ids = torch.tensor([f.source_ids for f in eval_features], dtype=torch.long)
                    all_source_mask = torch.tensor([f.source_mask for f in eval_features], dtype=torch.long)
                    all_target_ids = torch.tensor([f.target_ids for f in eval_features], dtype=torch.long)
                    all_target_mask = torch.tensor([f.target_mask for f in eval_features], dtype=torch.long)
                    eval_data = TensorDataset(all_source_ids, all_source_mask, all_target_ids, all_target_mask)
                    dev_dataset['dev_loss'] = eval_examples, eval_data
                eval_sampler = SequentialSampler(eval_data)
                eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

                logger.info("\n***** Running evaluation *****")
                logger.info("  Num examples = %d", len(eval_examples))
                logger.info("  Batch size = %d", args.eval_batch_size)

                # Start Evaling model
                model.eval()
                eval_loss, tokens_num = 0, 0
                for batch in eval_dataloader:
                    batch = tuple(t.to(device) for t in batch)
                    source_ids, source_mask, target_ids, target_mask = batch

                    with torch.no_grad():
                        # _,loss,num = model(source_ids=source_ids,source_mask=source_mask, target_ids=target_ids,target_mask=target_mask)
                        loss = model(input_ids=source_ids, attention_mask=source_mask, labels=target_ids).loss
                    eval_loss += loss.sum().item()
                    tokens_num += target_mask.sum().item()
                # Pring loss of dev dataset
                model.train()
                eval_loss = eval_loss / tokens_num
                result = {'eval_ppl': round(np.exp(eval_loss), 5),
                          'global_step': global_step + 1,
                          'train_loss': round(train_loss, 5)}
                for key in sorted(result.keys()):
                    logger.info("  %s = %s", key, str(result[key]))
                logger.info("  " + "*" * 20)

                # save last checkpoint
                last_output_dir = os.path.join(args.output_dir, 'checkpoint-last')
                if not os.path.exists(last_output_dir):
                    os.makedirs(last_output_dir)
                model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
                output_model_file = os.path.join(last_output_dir, "pytorch_model.bin")
                torch.save(model_to_save.state_dict(), output_model_file)
                tokenizer.save_pretrained(args.output_dir + '/tokenizer_last')

                if eval_loss < best_loss:
                    logger.info("Save best ppl model checkpoint to checkpoint-best-ppl")
                    logger.info("  Best ppl:%s", round(np.exp(eval_loss), 5))
                    logger.info("  " + "*" * 20)
                    best_loss = eval_loss
                    # Save best checkpoint for best ppl
                    output_dir = os.path.join(args.output_dir, 'checkpoint-best-ppl')
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
                    output_model_file = os.path.join(output_dir, "pytorch_model.bin")
                    torch.save(model_to_save.state_dict(), output_model_file)
                    tokenizer.save_pretrained(args.output_dir + '/tokenizer_best_ppl')

                # Calculate bleu
                if 'dev_bleu' in dev_dataset:
                    eval_examples, eval_data = dev_dataset['dev_bleu']
                else:
                    eval_examples = read_examples(args.dev_filename)
                    # eval_examples = random.sample(eval_examples,min(6000,len(eval_examples)))
                    eval_features = convert_examples_to_features(eval_examples, tokenizer, args, stage='test')
                    all_source_ids = torch.tensor([f.source_ids for f in eval_features], dtype=torch.long)
                    all_source_mask = torch.tensor([f.source_mask for f in eval_features], dtype=torch.long)
                    eval_data = TensorDataset(all_source_ids, all_source_mask)
                    dev_dataset['dev_bleu'] = eval_examples, eval_data

                eval_sampler = SequentialSampler(eval_data)
                eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

                model.eval()
                p = []
                for batch in eval_dataloader:
                    batch = tuple(t.to(device) for t in batch)
                    source_ids, source_mask = batch
                    with torch.no_grad():
                        # preds = model(source_ids=source_ids,source_mask=source_mask)
                        preds = model.generate(
                            input_ids=source_ids,
                            attention_mask=source_mask,
                            eos_token_id=tokenizer.eos_token_id,
                            max_length=args.max_target_length,
                            length_penalty=args.length_penalty,
                            num_beams=args.beam_size,
                            num_return_sequences=args.num_return_sequences,
                            do_sample=False,  # disable sampling to test if batching affects output
                        )
                        for pred in preds:
                            t = pred.cpu().numpy()
                            t = list(t)
                            if 1 in t:
                                t = t[:t.index(1)]
                            text = tokenizer.decode(t, skip_special_tokens=True, clean_up_tokenization_spaces=False)
                            p.append(text)
                model.train()
                temp_p = []
                for index, gold in enumerate(eval_examples):
                    best_generation = ""
                    best_score = -1.0
                    for i in range(index * args.num_return_sequences, (index + 1) * args.num_return_sequences):
                        # score = sentence_bleu([gold.target.strip().lower().split()], p[i].strip().lower().split())
                        scores = Rouge().get_scores(hyps=p[i].strip().lower(), refs=gold.target.strip().lower())
                        score = (scores[0]['rouge-1']['f'] + scores[0]['rouge-2']['f'] + scores[0]['rouge-l']['f']) / 3
                        if score > best_score:
                            best_generation = p[i]
                            best_score = score
                    temp_p.append(best_generation)
                p = temp_p
                predictions = []
                rouge_1, rouge_2, rouge_l, perfect_pred, dev_bleu, precision, recall = 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0
                with open(os.path.join(args.output_dir, "dev.output"), 'w') as f, open(
                        os.path.join(args.output_dir, "dev.gold"), 'w') as f1:
                    for inf, gold in zip(p, eval_examples):
                        # calculate perfect prediction
                        if inf.strip().lower() == gold.target.strip().lower():
                            perfect_pred += 1
                        # calculate bleu-4
                        if inf.strip() == "":
                            dev_bleu += 0.0
                        else:
                            dev_bleu += sentence_bleu([gold.target.strip().split()], inf.strip().split())
                        # calculate Rouge
                        if inf.strip() == "":
                            scores = Rouge().get_scores(" ", refs=gold.target.strip())
                        else:
                            scores = Rouge().get_scores(hyps=inf.strip().lower(), refs=gold.target.strip().lower())
                        rouge_1 += scores[0]['rouge-1']['f']
                        rouge_2 += scores[0]['rouge-2']['f']
                        rouge_l += scores[0]['rouge-l']['f']
                        precision += scores[0]['rouge-1']['p']
                        recall += scores[0]['rouge-1']['r']

                        predictions.append(str(gold.idx) + '\t' + inf)
                        f.write(str(gold.idx) + '\t' + inf + '\n')
                        f1.write(str(gold.idx) + '\t' + gold.target + '\n')

                total = len(predictions)

                dev_bleu /= total
                logger.info("  %s = %s " % ("bleu-4", str(dev_bleu)))
                logger.info("  " + "*" * 20)
                if dev_bleu > best_bleu:
                    logger.info("Save best bleu model checkpoint to checkpoint-best-bleu")
                    logger.info("  Best bleu:%s", dev_bleu)
                    logger.info("  " + "*" * 20)
                    best_bleu = dev_bleu
                    # Save best checkpoint for best bleu
                    output_dir = os.path.join(args.output_dir, 'checkpoint-best-bleu')
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
                    output_model_file = os.path.join(output_dir, "pytorch_model.bin")
                    torch.save(model_to_save.state_dict(), output_model_file)
                    tokenizer.save_pretrained(args.output_dir + '/tokenizer_best_bleu')

                total = len(predictions)
                rouge_1 /= total
                rouge_2 /= total
                rouge_l /= total
                precision /= total
                recall /= total
                logger.info("  %s = %s " % ("rouge-1", str(rouge_1)))
                logger.info("  %s = %s " % ("rouge-2", str(rouge_2)))
                logger.info("  %s = %s " % ("rouge-l", str(rouge_l)))
                logger.info("  " + "*" * 20)
                logger.info("  %s = %s " % ("precision", str(precision)))
                logger.info("  %s = %s " % ("recall", str(recall)))
                logger.info("  " + "*" * 20)
                perfect_pred /= total
                logger.info("  %s = %s " % ("perfect-prediction", str(perfect_pred)))
                logger.info("  " + "*" * 20)
                if (rouge_1 + rouge_2 + rouge_l) / 3 > best_rouge:
                    best_rouge = (rouge_1 + rouge_2 + rouge_l) / 3
                    logger.info("Save best average rouge model checkpoint to checkpoint-best-rouge")
                    logger.info("  Best rouge:%s", str(best_rouge))
                    logger.info("  " + "*" * 20)
                    # Save best checkpoint for best rouge
                    output_dir = os.path.join(args.output_dir, 'checkpoint-best-rouge')
                    if not os.path.exists(output_dir):
                        os.makedirs(output_dir)
                    model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
                    output_model_file = os.path.join(output_dir, "pytorch_model.bin")
                    torch.save(model_to_save.state_dict(), output_model_file)
                    tokenizer.save_pretrained(args.output_dir + '/tokenizer_best_rouge')

    if args.do_test:
        files = []
        if args.dev_filename is not None:
            files.append(args.dev_filename)
        if args.test_filename is not None:
            files.append(args.test_filename)
        for idx, file in enumerate(files):
            logger.info("Test file: {}".format(file))
            eval_examples = read_examples(file)
            eval_features = convert_examples_to_features(eval_examples, tokenizer, args, stage='test')
            all_source_ids = torch.tensor([f.source_ids for f in eval_features], dtype=torch.long)
            all_source_mask = torch.tensor([f.source_mask for f in eval_features], dtype=torch.long)
            eval_data = TensorDataset(all_source_ids, all_source_mask)

            # Calculate bleu
            eval_sampler = SequentialSampler(eval_data)
            eval_dataloader = DataLoader(eval_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

            model.eval()
            p = []
            for batch in tqdm(eval_dataloader, total=len(eval_dataloader)):
                batch = tuple(t.to(device) for t in batch)
                source_ids, source_mask = batch
                with torch.no_grad():
                    # preds = model(source_ids=source_ids,source_mask=source_mask)
                    preds = model.generate(
                        input_ids=source_ids,
                        attention_mask=source_mask,
                        eos_token_id=tokenizer.eos_token_id,
                        max_length=args.max_target_length,
                        length_penalty=args.length_penalty,
                        num_beams=args.beam_size,
                        num_return_sequences=args.num_return_sequences,
                        do_sample=False,  # disable sampling to test if batching affects output
                    )
                    for pred in preds:
                        t = pred.cpu().numpy()
                        t = list(t)
                        if 1 in t:
                            t = t[:t.index(1)]
                        text = tokenizer.decode(t, skip_special_tokens=True, clean_up_tokenization_spaces=False)
                        p.append(text)
            # model.train()
            temp_p = []
            for index, gold in enumerate(eval_examples):
                best_generation = ""
                best_score = -1.0
                for i in range(index * args.num_return_sequences, (index + 1) * args.num_return_sequences):
                    if p[i].strip() == "":
                        scores = Rouge().get_scores(hyps=" ", refs=gold.target.strip().lower())
                    else:
                        scores = Rouge().get_scores(hyps=p[i].strip().lower(), refs=gold.target.strip().lower())
                    score = (scores[0]['rouge-1']['f'] + scores[0]['rouge-2']['f'] + scores[0]['rouge-l']['f']) / 3
                    if score > best_score:
                        best_generation = p[i]
                        best_score = score
                temp_p.append(best_generation)
            p = temp_p
            predictions = []
            rouge_1, rouge_2, rouge_l, perfect_pred, dev_bleu, precision, recall = 0.0, 0.0, 0.0, 0, 0.0, 0.0, 0.0
            with open(os.path.join(args.output_dir, "test_{}.output".format(str(args.beam_size))), 'w') as f, open(
                    os.path.join(args.output_dir, "test_{}.gold".format(str(args.beam_size))), 'w') as f1:
                for inf, gold in zip(p, eval_examples):
                    # calculate perfect prediction
                    if inf.strip().lower() == gold.target.strip().lower():
                        perfect_pred += 1
                    # calculate bleu-4
                    if inf.strip() == "":
                        dev_bleu += 0.0
                    else:
                        dev_bleu += sentence_bleu([gold.target.strip().split()], inf.strip().split())
                    # calculate Rouge
                    if inf.strip() == "":
                        scores = Rouge().get_scores(" ", refs=gold.target.strip())
                    else:
                        scores = Rouge().get_scores(hyps=inf.strip().lower(), refs=gold.target.strip().lower())
                    rouge_1 += scores[0]['rouge-1']['f']
                    rouge_2 += scores[0]['rouge-2']['f']
                    rouge_l += scores[0]['rouge-l']['f']
                    precision += scores[0]['rouge-1']['p']
                    recall += scores[0]['rouge-1']['r']

                    predictions.append(str(gold.idx) + '\t' + inf)
                    f.write(str(gold.idx) + '\t' + inf + '\n')
                    f1.write(str(gold.idx) + '\t' + gold.target + '\n')

            total = len(predictions)

            dev_bleu /= total
            logger.info("  %s = %s " % ("bleu-4", str(dev_bleu)))
            logger.info("  " + "*" * 20)

            rouge_1 /= total
            rouge_2 /= total
            rouge_l /= total
            precision /= total
            recall /= total
            logger.info("  %s = %s " % ("rouge-1", str(rouge_1)))
            logger.info("  %s = %s " % ("rouge-2", str(rouge_2)))
            logger.info("  %s = %s " % ("rouge-l", str(rouge_l)))
            logger.info("  " + "*" * 20)
            logger.info("  %s = %s " % ("precision", str(precision)))
            logger.info("  %s = %s " % ("recall", str(recall)))
            logger.info("  " + "*" * 20)
            perfect_pred /= total
            logger.info("  %s = %s " % ("perfect-prediction", str(perfect_pred)))
            logger.info("  " + "*" * 20)


if __name__ == "__main__":
    main()


