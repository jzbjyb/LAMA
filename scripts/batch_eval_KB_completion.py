# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
#
from lama.modules import build_model_by_name
import lama.utils as utils
from lama.utils import print_sentence_predictions, load_vocab
import lama.options as options
from tqdm import tqdm
from random import shuffle
import os
import json
import spacy
import lama.modules.base_connector as base
from pprint import pprint
import logging.config
import logging
import pickle
from multiprocessing.pool import ThreadPool
import multiprocessing
import lama.evaluation_metrics as metrics
import time, sys
import numpy as np
import torch
import torch.nn.functional as F
from copy import deepcopy


def load_file(filename):
    data = []
    with open(filename, "r") as f:
        for line in f.readlines():
            data.append(json.loads(line))
    return data


def create_logdir_with_timestamp(base_logdir, modelname):
    timestr = time.strftime("%Y%m%d_%H%M%S")

    # create new directory
    log_directory = "{}/{}_{}/".format(base_logdir, modelname, timestr)
    os.makedirs(log_directory)

    path = "{}/last".format(base_logdir)
    try:
        os.unlink(path)
    except Exception:
        pass
    os.symlink(log_directory, path)
    return log_directory


def parse_template(template, subject_label, object_label):
    SUBJ_SYMBOL = "[X]"
    OBJ_SYMBOL = "[Y]"
    template = template.replace(SUBJ_SYMBOL, subject_label)
    template = template.replace(OBJ_SYMBOL, object_label)
    return [template]


def parse_template_tokenize(template, subject_label, model, mask_part='relation'):
    assert mask_part in {'relation', 'sub'}
    SUBJ_SYMBOL = "[X]"
    OBJ_SYMBOL = "[Y]"
    template.split()
    x_pos = template.index(SUBJ_SYMBOL)
    y_pos = template.index(OBJ_SYMBOL)
    template = template.replace(SUBJ_SYMBOL, model.mask_token)
    template = template.replace(OBJ_SYMBOL, model.mask_token)
    toks = model.tokenize(template)
    all_mask = []
    mask_pos = []
    for i, tok in enumerate(toks):
        if tok == model.mask_token:
            all_mask.append(0)
            mask_pos.append(i)
        elif mask_part == 'relation':
            all_mask.append(1)
        elif mask_part == 'sub':
            all_mask.append(0)
    assert len(mask_pos) == 2, 'not binary relation'
    ind = mask_pos[0] if x_pos < y_pos else mask_pos[1]
    sub_toks = model.tokenize(subject_label)
    toks = toks[:ind] + sub_toks + toks[ind + 1:]
    if mask_part == 'relation':
        all_mask = all_mask[:ind] + ([0] * len(sub_toks)) + all_mask[ind + 1:]
    elif mask_part == 'sub':
        all_mask = all_mask[:ind] + ([1] * len(sub_toks)) + all_mask[ind + 1:]
    return [toks], [all_mask]


def bracket_relational_phrase(template, subject_label, object_label):
    SUBJ_SYMBOL = "[X]"
    OBJ_SYMBOL = "[Y]"
    sub_ind = template.find(SUBJ_SYMBOL)
    obj_ind = template.find(OBJ_SYMBOL)
    start_ind = min(sub_ind, obj_ind) + len(SUBJ_SYMBOL)
    end_ind = max(sub_ind, obj_ind)
    template = template[:start_ind] + ' [ ' + template[start_ind:end_ind] + ' ] ' + template[end_ind:]
    template = template.replace(SUBJ_SYMBOL, subject_label.replace('[', '(').replace(']', ')'))
    template = template.replace(OBJ_SYMBOL, object_label.replace('[', '(').replace(']', ')'))
    return template


def replace_template(old_template, new_relational_phrase):
    SUBJ_SYMBOL = "[X]"
    OBJ_SYMBOL = "[Y]"
    sub_ind = old_template.find(SUBJ_SYMBOL)
    obj_ind = old_template.find(OBJ_SYMBOL)
    start_ind = min(sub_ind, obj_ind) + len(SUBJ_SYMBOL)
    end_ind = max(sub_ind, obj_ind)
    template = old_template[:start_ind] + ' ' + new_relational_phrase + ' ' + old_template[end_ind:]
    return template


def init_logging(log_directory):
    logger = logging.getLogger("LAMA")
    logger.setLevel(logging.DEBUG)

    os.makedirs(log_directory, exist_ok=True)

    # logging format
    # "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    )

    # file handler
    fh = logging.FileHandler(str(log_directory) + "/info.log")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(formatter)

    # console handler
    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.WARNING)
    ch.setFormatter(formatter)

    logger.addHandler(fh)
    logger.addHandler(ch)

    logger.propagate = False

    return logger


def batchify(data, batch_size, key='masked_sentences'):
    msg = ""
    list_samples_batches = []
    list_sentences_batches = []
    current_samples_batch = []
    current_sentences_batches = []
    c = 0

    # sort to group togheter sentences with similar length
    for sample in sorted(
        data, key=lambda k: len(" ".join(k[key]).split())
    ):
        masked_sentences = sample[key]
        current_samples_batch.append(sample)
        current_sentences_batches.append(masked_sentences)
        c += 1
        if c >= batch_size:
            list_samples_batches.append(current_samples_batch)
            list_sentences_batches.append(current_sentences_batches)
            current_samples_batch = []
            current_sentences_batches = []
            c = 0

    # last batch
    if current_samples_batch and len(current_samples_batch) > 0:
        list_samples_batches.append(current_samples_batch)
        list_sentences_batches.append(current_sentences_batches)

    return list_samples_batches, list_sentences_batches, msg


def run_thread(arguments):

    msg = ""

    # 1. compute the ranking metrics on the filtered log_probs tensor
    sample_MRR, sample_P, experiment_result, return_msg = metrics.get_ranking(
        arguments["filtered_log_probs"],
        arguments["vocab"],
        label_index=arguments["label_index"],
        index_list=arguments["index_list"],
        print_generation=arguments["interactive"],
        topk=1,  # TODO: only use top 1 to increase speed
    )
    msg += "\n" + return_msg

    sample_perplexity = 0.0
    if arguments["interactive"]:
        pprint(arguments["sample"])
        # THIS IS OPTIONAL - mainly used for debuggind reason
        # 2. compute perplexity and print predictions for the complete log_probs tensor
        sample_perplexity, return_msg = print_sentence_predictions(
            arguments["original_log_probs"],
            arguments["token_ids"],
            arguments["vocab"],
            masked_indices=arguments["masked_indices"],
            print_generation=arguments["interactive"],
        )
        input("press enter to continue...")
        msg += "\n" + return_msg

    return experiment_result, sample_MRR, sample_P, sample_perplexity, msg


def lowercase_samples(samples):
    new_samples = []
    for sample in samples:
        sample["obj_label"] = sample["obj_label"].lower()
        sample["sub_label"] = sample["sub_label"].lower()
        lower_masked_sentences = []
        for sentence in sample["masked_sentences"]:
            sentence = sentence.lower()
            sentence = sentence.replace(base.MASK.lower(), base.MASK)
            lower_masked_sentences.append(sentence)
        sample["masked_sentences"] = lower_masked_sentences
        new_samples.append(sample)
    return new_samples


def filter_samples(model, samples, vocab_subset, max_sentence_length, template):
    msg = ""
    new_samples = []
    samples_exluded = 0
    for sample in samples:
        excluded = False
        if "obj_label" in sample and "sub_label" in sample:

            obj_label_ids = model.get_id(sample["obj_label"])

            if obj_label_ids:
                recostructed_word = " ".join(
                    [model.vocab[x] for x in obj_label_ids]
                ).strip()
            else:
                recostructed_word = None

            excluded = False
            if not template or len(template) == 0:
                masked_sentences = sample["masked_sentences"]
                text = " ".join(masked_sentences)
                if len(text.split()) > max_sentence_length:
                    msg += "\tEXCLUDED for exeeding max sentence length: {}\n".format(
                        masked_sentences
                    )
                    samples_exluded += 1
                    excluded = True

            # MAKE SURE THAT obj_label IS IN VOCABULARIES
            if vocab_subset:
                for x in sample["obj_label"].split(" "):
                    if x not in vocab_subset:
                        excluded = True
                        msg += "\tEXCLUDED object label {} not in vocab subset\n".format(
                            sample["obj_label"]
                        )
                        samples_exluded += 1
                        break

            if excluded:
                pass
            elif obj_label_ids is None:
                msg += "\tEXCLUDED object label {} not in model vocabulary\n".format(
                    sample["obj_label"]
                )
                samples_exluded += 1
            elif not recostructed_word or recostructed_word != sample["obj_label"]:
                msg += "\tEXCLUDED object label {} not in model vocabulary\n".format(
                    sample["obj_label"]
                )
                samples_exluded += 1
            # elif vocab_subset is not None and sample['obj_label'] not in vocab_subset:
            #   msg += "\tEXCLUDED object label {} not in vocab subset\n".format(sample['obj_label'])
            #   samples_exluded+=1
            elif "judgments" in sample:
                # only for Google-RE
                num_no = 0
                num_yes = 0
                for x in sample["judgments"]:
                    if x["judgment"] == "yes":
                        num_yes += 1
                    else:
                        num_no += 1
                if num_no > num_yes:
                    # SKIP NEGATIVE EVIDENCE
                    pass
                else:
                    new_samples.append(sample)
            else:
                new_samples.append(sample)
        else:
            msg += "\tEXCLUDED since 'obj_label' not sample or 'sub_label' not in sample: {}\n".format(
                sample
            )
            samples_exluded += 1
    msg += "samples exluded  : {}\n".format(samples_exluded)
    return new_samples, msg


def main(args,
         shuffle_data=True,
         model=None,
         refine_template=False,
         get_objs=False,
         dynamic='none',
         use_prob=False,
         bt_obj=None,
         temp_model=None):

    if len(args.models_names) > 1:
        raise ValueError('Please specify a single language model (e.g., --lm "bert").')

    msg = ""

    [model_type_name] = args.models_names

    #print(model)
    if model is None:
        model = build_model_by_name(model_type_name, args)

    if model_type_name == "fairseq":
        model_name = "fairseq_{}".format(args.fairseq_model_name)
    elif model_type_name == "bert":
        model_name = "BERT_{}".format(args.bert_model_name)
    elif model_type_name == "elmo":
        model_name = "ELMo_{}".format(args.elmo_model_name)
    else:
        model_name = model_type_name.title()

    # initialize logging
    if args.full_logdir:
        log_directory = args.full_logdir
    else:
        log_directory = create_logdir_with_timestamp(args.logdir, model_name)
    logger = init_logging(log_directory)
    msg += "model name: {}\n".format(model_name)

    # deal with vocab subset
    vocab_subset = None
    index_list = None
    msg += "args: {}\n".format(args)
    if args.common_vocab_filename is not None:
        vocab_subset = load_vocab(args.common_vocab_filename)
        msg += "common vocabulary size: {}\n".format(len(vocab_subset))

        # optimization for some LM (such as ELMo)
        model.optimize_top_layer(vocab_subset)

        filter_logprob_indices, index_list = model.init_indices_for_filter_logprobs(
            vocab_subset, logger
        )

    logger.info("\n" + msg + "\n")

    # dump arguments on file for log
    with open("{}/args.json".format(log_directory), "w") as outfile:
        json.dump(vars(args), outfile)

    # stats
    samples_with_negative_judgement = 0
    samples_with_positive_judgement = 0

    # Mean reciprocal rank
    MRR = 0.0
    MRR_negative = 0.0
    MRR_positive = 0.0

    # Precision at (default 10)
    Precision = 0.0
    Precision1 = 0.0
    Precision_negative = 0.0
    Precision_positivie = 0.0

    P1_li = []
    obj_labels = []

    data = load_file(args.dataset_filename)

    if args.lowercase:
        # lowercase all samples
        logger.info("lowercasing all samples...")
        all_samples = lowercase_samples(data)
    else:
        # keep samples as they are
        all_samples = data

    all_samples, ret_msg = filter_samples(
        model, data, vocab_subset, args.max_sentence_length, args.template
    )

    # OUT_FILENAME = "{}.jsonl".format(args.dataset_filename)
    # with open(OUT_FILENAME, 'w') as outfile:
    #     for entry in all_samples:
    #         json.dump(entry, outfile)
    #         outfile.write('\n')

    logger.info("\n" + ret_msg + "\n")

    print('#head-tails {} -> {}'.format(len(data), len(all_samples)))

    samples_batches_li, sentences_batches_li = [], []
    for template in args.template:
        # if template is active (1) use a single example for (sub,obj) and (2) ...
        if template and template != "":
            facts = []
            for sample in all_samples:
                sub = sample["sub_label"]
                obj = sample["obj_label"]
                if (sub, obj) not in facts:
                    facts.append((sub, obj))
            local_msg = "distinct template facts: {}".format(len(facts))
            logger.info("\n" + local_msg + "\n")
            new_all_samples = []
            for fact in facts:
                (sub, obj) = fact
                sample = {}
                sample["sub_label"] = sub
                sample["obj_label"] = obj
                # sobstitute all sentences with a standard template
                sample["masked_sentences"] = parse_template(
                    template.strip(), sample["sub_label"].strip(), model.mask_token
                )
                if dynamic.startswith('bt_topk') or temp_model is not None:
                    sample['sub_masked_sentences'] = parse_template_tokenize(
                        template.strip(), sample["sub_label"].strip(), model, mask_part='sub'
                    )
                if dynamic == 'real_lm' or dynamic.startswith('real_lm_topk'):
                    sample["tokenized_sentences"] = parse_template_tokenize(
                        template.strip(), sample["sub_label"].strip(), model, mask_part='relation'
                    )
                # substitute sub and obj placeholder in template with corresponding str
                # and add bracket to the relational phrase
                sample['bracket_sentences'] = bracket_relational_phrase(
                    template.strip(), sample['sub_label'].strip(), sample['obj_label'].strip()
                )
                new_all_samples.append(sample)

        # create uuid if not present
        i = 0
        for sample in new_all_samples:
            if "uuid" not in sample:
                sample["uuid"] = i
            i += 1

        # shuffle data
        if shuffle_data:
            perm = np.random.permutation(len(new_all_samples))
            new_all_samples = np.array(new_all_samples)[perm]

        samples_batches, sentences_batches, ret_msg = batchify(new_all_samples, args.batch_size)
        logger.info("\n" + ret_msg + "\n")
        samples_batches_li.append(samples_batches)
        sentences_batches_li.append(sentences_batches)

        sub_obj_labels = [(sample['sub_label'], sample['obj_label'])
                          for batch in samples_batches for sample in batch]
        if get_objs:
            print('sub_obj_label {}'.format('\t'.join(map(lambda p: '{}\t{}'.format(*p), sub_obj_labels))))
            return

        if refine_template:
            bracket_sentences = [sample['bracket_sentences'] for sample in new_all_samples]
            new_temp = model.refine_cloze(bracket_sentences, batch_size=32, try_cuda=True)
            new_temp = replace_template(template.strip(), ' '.join(new_temp))
            print('old temp: {}'.format(template.strip()))
            print('new temp: {}'.format(new_temp))
            return new_temp

    # ThreadPool
    num_threads = args.threads
    if num_threads <= 0:
        # use all available threads
        num_threads = multiprocessing.cpu_count()
    pool = ThreadPool(num_threads)
    list_of_results = []

    samples_batches_li = list(zip(*samples_batches_li))
    sentences_batches_li = list(zip(*sentences_batches_li))

    c_inc_meaning = ['top12 prob gap', 'top1 prob']
    c_inc_stat = np.zeros((2, 3))  # [[*, c_num], [*, inc_num]]

    loss_list = []
    features_list = []
    bt_features_list = []

    for i in tqdm(range(len(samples_batches_li))):

        samples_b_all = samples_batches_li[i]
        sentences_b_all = sentences_batches_li[i]

        filter_lp_merge = None
        samples_b = samples_b_all[-1]
        max_score = float('-inf')
        consist_score_li = []

        for sentences_b, samples_b_this in zip(sentences_b_all, samples_b_all):
            # TODO: add tokens_tensor and mask_tensor for more models
            original_log_probs_list, token_ids_list, masked_indices_list, tokens_tensor, mask_tensor = \
                model.get_batch_generation(sentences_b, logger=logger)

            if use_prob:  # use prob instead of log prob
                original_log_probs_list = original_log_probs_list.exp()

            if dynamic == 'real_lm' or dynamic.startswith('real_lm_topk'):
                sentences_b_mask_rel = [s['tokenized_sentences'][0] for s in samples_b_this]
                relation_mask = [s['tokenized_sentences'][1] for s in samples_b_this]
                consist_log_probs_list, _, _, tokens_tensor, mask_tensor = \
                    model.get_batch_generation(sentences_b_mask_rel, logger=logger, relation_mask=relation_mask)
            else:
                consist_log_probs_list = original_log_probs_list

            if dynamic == 'lm' or dynamic == 'real_lm' or dynamic.startswith('real_lm_topk'):
                # use avg prob of the templates as score
                mask_tensor = mask_tensor.float()
                consist_log_probs_list_flat = consist_log_probs_list.view(-1, consist_log_probs_list.size(-1))
                token_logprob = torch.gather(consist_log_probs_list_flat, dim=1, index=tokens_tensor.view(-1, 1)).view(*consist_log_probs_list.size()[:2])
                token_logprob = token_logprob * mask_tensor
                consist_score = token_logprob.sum(-1) / mask_tensor.sum(-1)  # normalized prob

            '''
            if vocab_subset is not None:
                # filter log_probs
                filtered_log_probs_list = model.filter_logprobs(
                    original_log_probs_list, filter_logprob_indices
                )
            else:
                filtered_log_probs_list = original_log_probs_list
            '''

            # get the prediction probability
            if vocab_subset is not None:
                filtered_log_probs_list = [
                    flp[masked_indices_list[ind][0]].index_select(dim=-1, index=filter_logprob_indices)
                    for ind, flp in enumerate(original_log_probs_list)]
            else:
                filtered_log_probs_list = [flp[masked_indices_list[ind][0]] for ind, flp in
                                           enumerate(original_log_probs_list)]

            if dynamic.startswith('bt_topk'):
                obj_topk = int(dynamic.rsplit('-', 1)[1])
                top_obj_pred = [flp.topk(k=obj_topk) for flp in filtered_log_probs_list]
                top_obj_logprob, top_obj_pred = zip(*top_obj_pred)

            if dynamic.startswith('obj_lm_topk'):
                # use highest obj prob as consistency score
                consist_score = torch.tensor([torch.max(flp).item() for flp in filtered_log_probs_list])
            elif dynamic.startswith('obj_lmgap_topk'):
                # the gap between the highest prediction log p1 - log p2
                get_gap = lambda top2: (top2[0] - top2[1]).item()
                consist_score = torch.tensor([get_gap(torch.topk(flp, k=2)[0]) for flp in filtered_log_probs_list])
            elif dynamic.startswith('bt_topk'):
                # use the obj_topk highest obj to "back translate" sub
                consist_score_obj_topk = []
                used_vocab = vocab_subset if vocab_subset is not None else model.vocab
                for obj_i in range(obj_topk):
                    sentences_b_mask_sub = [[replace_list(s['sub_masked_sentences'][0][0], model.mask_token, used_vocab[obj_pred[obj_i].item()])]
                                            for s, obj_pred in zip(samples_b_this, top_obj_pred)]
                    sub_mask = [s['sub_masked_sentences'][1] for s in samples_b_this]
                    # TODO: only masked lm can do this
                    consist_log_probs_list, _, _, tokens_tensor, mask_tensor = \
                        model.get_batch_generation(sentences_b_mask_sub, logger=logger, relation_mask=sub_mask)
                    # use avg prob of the sub as score
                    mask_tensor = mask_tensor.float()
                    consist_log_probs_list_flat = consist_log_probs_list.view(-1, consist_log_probs_list.size(-1))
                    token_logprob = torch.gather(consist_log_probs_list_flat, dim=1, index=tokens_tensor.view(-1, 1)).view(
                        *consist_log_probs_list.size()[:2])
                    token_logprob = token_logprob * mask_tensor
                    consist_score = token_logprob.sum(-1) / mask_tensor.sum(-1)  # normalized prob
                    consist_score_obj_topk.append(consist_score)

                # SHAPE: (batch_size, obj_topk)
                consist_score_obj_topk = torch.stack(consist_score_obj_topk).permute(1, 0)
                consist_score_weight = torch.stack(top_obj_logprob).exp()
                # SHAPE: (batch_size)
                consist_score = (consist_score_obj_topk * consist_score_weight).sum(-1) / (consist_score_weight.sum(-1) + 1e-10)

            # add to overall probability
            if filter_lp_merge is None:
                filter_lp_merge = filtered_log_probs_list
                if dynamic == 'lm' or dynamic == 'real_lm':
                    max_score = consist_score
                elif dynamic.startswith('real_lm_topk') or \
                        dynamic.startswith('obj_lm_topk') or \
                        dynamic.startswith('obj_lmgap_topk') or \
                        dynamic.startswith('bt_topk'):
                    consist_score_li.append(consist_score)
            else:
                if dynamic == 'none' and temp_model is None:
                    filter_lp_merge = [a + b for a, b in zip(filter_lp_merge, filtered_log_probs_list)]
                elif dynamic == 'lm' or dynamic == 'real_lm':
                    filter_lp_merge = \
                        [a if c >= d else b for a, b, c, d in
                         zip(filter_lp_merge, filtered_log_probs_list, max_score, consist_score)]
                    max_score = torch.max(max_score, consist_score)
                elif dynamic.startswith('real_lm_topk') or \
                        dynamic.startswith('obj_lm_topk') or \
                        dynamic.startswith('obj_lmgap_topk') or \
                        dynamic.startswith('bt_topk'):
                    filter_lp_merge.extend(filtered_log_probs_list)
                    consist_score_li.append(consist_score)
                elif temp_model is not None:
                    filter_lp_merge.extend(filtered_log_probs_list)

        label_index_list = []
        for sample in samples_b:
            obj_label_id = model.get_id(sample["obj_label"])

            # MAKE SURE THAT obj_label IS IN VOCABULARIES
            if obj_label_id is None:
                raise ValueError(
                    "object label {} not in model vocabulary".format(
                        sample["obj_label"]
                    )
                )
            elif model.vocab[obj_label_id[0]] != sample["obj_label"]:
                raise ValueError(
                    "object label {} not in model vocabulary".format(
                        sample["obj_label"]
                    )
                )
            elif vocab_subset is not None and sample["obj_label"] not in vocab_subset:
                raise ValueError(
                    "object label {} not in vocab subset".format(sample["obj_label"])
                )

            label_index_list.append(obj_label_id)

        if dynamic.startswith('real_lm_topk') or \
                dynamic.startswith('obj_lm_topk') or \
                dynamic.startswith('obj_lmgap_topk') or \
                dynamic.startswith('bt_topk') or \
                temp_model is not None:  # analyze prob
            # SHAPE: (batch_size, num_temp, filter_vocab_size)
            filter_lp_merge = torch.stack(filter_lp_merge, 0).view(
                len(sentences_b_all), len(filter_lp_merge) // len(sentences_b_all), -1).permute(1, 0, 2)
            # SHAPE: (batch_size)
            label_index_tensor = torch.tensor([index_list.index(li[0]) for li in label_index_list])
            c_inc = np.array(metrics.analyze_prob(
                filter_lp_merge, label_index_tensor, output=False, method='sample'))
            c_inc_stat += c_inc

        # SHAPE: (batch_size, num_temp, filter_vocab_size)
        filter_lp_unmerge = filter_lp_merge

        if temp_model is not None:  # optimize template weights
            temp_model_, optimizer = temp_model
            if optimizer is None:  # predict
                filter_lp_merge = temp_model_(args.relation, filter_lp_merge.detach(), target=None)
            elif optimizer == 'precompute':  # pre-compute and save featuers
                lp = filter_lp_merge
                # SHAPE: (batch_size * num_temp)
                features = torch.gather(lp.contiguous().view(-1, lp.size(-1)), dim=1,
                                        index=label_index_tensor.repeat(lp.size(1)).view(-1, 1))
                features = features.view(-1, lp.size(1))
                features_list.append(features)
                if bt_obj is None:
                    continue
            elif optimizer is not None:  # train on the fly
                optimizer.zero_grad()
                loss = temp_model_(args.relation, filter_lp_merge.detach(), target=label_index_tensor.detach())
                loss.backward()
                optimizer.step()
                loss_list.append(loss.item())
                continue

        if dynamic.startswith('real_lm_topk') or \
                dynamic.startswith('obj_lm_topk') or \
                dynamic.startswith('obj_lmgap_topk') or \
                dynamic.startswith('bt_topk'):  # dynamic ensemble
            real_lm_topk = min(int(dynamic[dynamic.find('topk') + 4:].split('-')[0]), len(consist_score_li))
            # SHAPE: (batch_size, num_temp)
            consist_score_li = torch.stack(consist_score_li, -1)
            # SHAPE: (batch_size, topk)
            consist_score, consist_ind = consist_score_li.topk(real_lm_topk, dim=-1)
            # SHAPE: (batch_size, 1)
            consist_score = consist_score.min(-1, keepdim=True)[0]
            # SHAPE: (batch_size, num_temp, 1)
            consist_mask = (consist_score_li >= consist_score).float().unsqueeze(-1)
            # avg over top k
            filter_lp_merge = filter_lp_merge * consist_mask
            filter_lp_merge = filter_lp_merge.sum(1) / consist_mask.sum(1)

        if bt_obj:  # choose top bt_obj objects and bach-translate subject
            # get the top bt_obj objects with highest probability
            used_vocab = vocab_subset if vocab_subset is not None else model.vocab
            temp_model_, optimizer = temp_model
            if optimizer is None:  # use beam search
                # SHAPE: (batch_size, bt_obj)
                objs_score, objs_ind = filter_lp_merge.topk(bt_obj, dim=-1)
                objs_ind = torch.sort(objs_ind, dim=-1)[0]  # the index must be ascending
            elif optimizer == 'precompute':  # use ground truth
                objs_ind = torch.tensor([index_list.index(li[0]) for li in label_index_list]).view(-1, 1)
                bt_obj = 1

            # bach translation
            sub_lp_list = []
            for sentences_b, samples_b_this in zip(sentences_b_all, samples_b_all):  # iter over templates
                for obj_i in range(bt_obj):  # iter over objs
                    sentences_b_mask_sub = [[replace_list(s['sub_masked_sentences'][0][0], model.mask_token, used_vocab[obj_pred[obj_i].item()])]
                                            for s, obj_pred in zip(samples_b_this, objs_ind)]
                    sub_mask = [s['sub_masked_sentences'][1] for s in samples_b_this]
                    # TODO: only masked lm can do this
                    lp, _, _, tokens_tensor, mask_tensor = \
                        model.get_batch_generation(sentences_b_mask_sub, logger=logger, relation_mask=sub_mask)
                    # use avg prob of the sub as score
                    mask_tensor = mask_tensor.float()
                    lp_flat = lp.view(-1, lp.size(-1))
                    sub_lp = torch.gather(lp_flat, dim=1, index=tokens_tensor.view(-1, 1)).view(*lp.size()[:2])
                    sub_lp = sub_lp * mask_tensor
                    sub_lp_avg = sub_lp.sum(-1) / mask_tensor.sum(-1)  # normalized prob
                    sub_lp_list.append(sub_lp_avg)

            # SHAPE: (batch_size, num_temp, top_obj_num)
            num_temp = len(sentences_b_all)
            sub_lp_list = torch.cat(sub_lp_list, 0).view(num_temp, bt_obj, -1).permute(2, 0, 1)

            if optimizer == 'precompute':
                bt_features_list.append(sub_lp_list.squeeze(-1))
                continue

            # select obj prob
            expand_mask = torch.zeros_like(filter_lp_unmerge)
            expand_mask.scatter_(-1, objs_ind.unsqueeze(1).repeat(1, num_temp, 1), 1)
            # SHAPE: (batch_size, num_temp, top_obj_num)
            obj_lp_list = torch.masked_select(filter_lp_unmerge, expand_mask.eq(1)).view(-1, num_temp, bt_obj)

            # run temp model
            # SHAPE: (batch_size, vocab_size)
            filter_lp_merge_expand = torch.zeros_like(filter_lp_merge)
            # SHAPE: (batch_size, top_obj_num)
            filter_lp_merge = temp_model_(args.relation, torch.cat([obj_lp_list, sub_lp_list], 1), target=None)

            # expand results to vocab_size
            filter_lp_merge_expand.scatter_(-1, objs_ind, filter_lp_merge)
            filter_lp_merge = filter_lp_merge_expand + expand_mask[:, 0, :].log()  # mask out other objs

        arguments = [
            {
                "original_log_probs": original_log_probs,
                "filtered_log_probs": filtered_log_probs,
                "token_ids": token_ids,
                "vocab": model.vocab,
                "label_index": label_index[0],
                "masked_indices": masked_indices,
                "interactive": args.interactive,
                "index_list": index_list,
                "sample": sample,
            }
            for original_log_probs, filtered_log_probs, token_ids, masked_indices, label_index, sample in zip(
                original_log_probs_list,
                filter_lp_merge,
                token_ids_list,
                masked_indices_list,
                label_index_list,
                samples_b,
            )
        ]

        # single thread for debug
        # for isx,a in enumerate(arguments):
        #     print(samples_b[isx])
        #     run_thread(a)

        # multithread
        res = pool.map(run_thread, arguments)

        for idx, result in enumerate(res):

            result_masked_topk, sample_MRR, sample_P, sample_perplexity, msg = result

            logger.info("\n" + msg + "\n")

            sample = samples_b[idx]

            element = {}
            element["sample"] = sample
            element["uuid"] = sample["uuid"]
            element["token_ids"] = token_ids_list[idx]
            element["masked_indices"] = masked_indices_list[idx]
            element["label_index"] = label_index_list[idx]
            element["masked_topk"] = result_masked_topk
            element["sample_MRR"] = sample_MRR
            element["sample_Precision"] = sample_P
            element["sample_perplexity"] = sample_perplexity
            element["sample_Precision1"] = result_masked_topk["P_AT_1"]

            # print()
            # print("idx: {}".format(idx))
            # print("masked_entity: {}".format(result_masked_topk['masked_entity']))
            # for yi in range(10):
            #     print("\t{} {}".format(yi,result_masked_topk['topk'][yi]))
            # print("masked_indices_list: {}".format(masked_indices_list[idx]))
            # print("sample_MRR: {}".format(sample_MRR))
            # print("sample_P: {}".format(sample_P))
            # print("sample: {}".format(sample))
            # print()

            MRR += sample_MRR
            Precision += sample_P
            Precision1 += element["sample_Precision1"]
            P1_li.append(element["sample_Precision1"])

            '''
            if element["sample_Precision1"] == 1:
                print(element["sample"])
                input(1)
            else:
                print(element["sample"])
                input(0)
            '''

            # the judgment of the annotators recording whether they are
            # evidence in the sentence that indicates a relation between two entities.
            num_yes = 0
            num_no = 0

            if "judgments" in sample:
                # only for Google-RE
                for x in sample["judgments"]:
                    if x["judgment"] == "yes":
                        num_yes += 1
                    else:
                        num_no += 1
                if num_no >= num_yes:
                    samples_with_negative_judgement += 1
                    element["judgement"] = "negative"
                    MRR_negative += sample_MRR
                    Precision_negative += sample_P
                else:
                    samples_with_positive_judgement += 1
                    element["judgement"] = "positive"
                    MRR_positive += sample_MRR
                    Precision_positivie += sample_P

            list_of_results.append(element)

    if temp_model is not None:
        if temp_model[1] == 'precompute':
            features = torch.cat(features_list, 0)
            if bt_obj:
                bt_features = torch.cat(bt_features_list, 0)
                features = torch.cat([features, bt_features], 1)
            return features
        if temp_model[1] is not None:
            return np.mean(loss_list)

    pool.close()
    pool.join()

    # stats
    # Mean reciprocal rank
    MRR /= len(list_of_results)

    # Precision
    Precision /= len(list_of_results)
    Precision1 /= len(list_of_results)

    msg = "all_samples: {}\n".format(len(all_samples))
    msg += "list_of_results: {}\n".format(len(list_of_results))
    msg += "global MRR: {}\n".format(MRR)
    msg += "global Precision at 10: {}\n".format(Precision)
    msg += "global Precision at 1: {}\n".format(Precision1)

    if samples_with_negative_judgement > 0 and samples_with_positive_judgement > 0:
        # Google-RE specific
        MRR_negative /= samples_with_negative_judgement
        MRR_positive /= samples_with_positive_judgement
        Precision_negative /= samples_with_negative_judgement
        Precision_positivie /= samples_with_positive_judgement
        msg += "samples_with_negative_judgement: {}\n".format(
            samples_with_negative_judgement
        )
        msg += "samples_with_positive_judgement: {}\n".format(
            samples_with_positive_judgement
        )
        msg += "MRR_negative: {}\n".format(MRR_negative)
        msg += "MRR_positive: {}\n".format(MRR_positive)
        msg += "Precision_negative: {}\n".format(Precision_negative)
        msg += "Precision_positivie: {}\n".format(Precision_positivie)

    logger.info("\n" + msg + "\n")
    print("\n" + msg + "\n")

    # dump pickle with the result of the experiment
    all_results = dict(
        list_of_results=list_of_results, global_MRR=MRR, global_P_at_10=Precision
    )
    with open("{}/result.pkl".format(log_directory), "wb") as f:
        pickle.dump(all_results, f)

    print('P1all {}'.format('\t'.join(map(str, P1_li))))
    print('meaning: {}'.format(c_inc_meaning))
    print('correct-incorrect {}'.format(
        '\t'.join(map(str, (c_inc_stat[:, :-1] / (c_inc_stat[:, -1:] + 1e-5)).reshape(-1)))))

    return Precision1


def replace_list(li, from_ele, to_ele):
    li = deepcopy(li)
    li[li.index(from_ele)] = to_ele
    return li


if __name__ == "__main__":
    parser = options.get_eval_KB_completion_parser()
    args = options.parse_args(parser)
    main(args)
