#!/usr/bin/env python

# Loads a file where each line contains a label, followed by a tab, followed
# by a sequence of words with a binary parse indicated by space-separated parentheses.
#
# Example:
# sentence_label	( ( word word ) ( ( word word ) word ) )
import itertools
import numpy as np

from rembed import util


REDUCE_OP = '*MERGE*'
PADDING_TOKEN = "*PADDING*"
UNK_TOKEN = "*UNK*"

NUM_CLASSES = 2


def convert_binary_bracketed_data(filename):
    examples = []
    with open(filename, 'r') as f:
        for line in f:
            example = {}
            line = line.strip()
            tab_split = line.split('\t')
            example["label"] = tab_split[0]
            example["sentence"] = tab_split[1]
            example["tokens"] = []
            example["transitions"] = []

            for word in example["sentence"].split(' '):
                if word != "(":
                    if word != ")":
                        example["tokens"].append(word)
                    example["transitions"].append(1 if word == ")" else 0)

            examples.append(example)
    return examples


def load_data(path, vocabulary=None, seq_length=None, batch_size=32,
              eval_mode=False, logger=None):
    dataset = convert_binary_bracketed_data(path)

    if not vocabulary:
        # Build vocabulary from data
        # TODO(SB): Use a fixed vocab file in case this takes especially long, or we want
        # to include vocab items that don't appear in the training data.
        vocabulary = {PADDING_TOKEN: 0,
                      UNK_TOKEN: 1}
        types = set(itertools.chain.from_iterable([example["tokens"]
                                                   for example in dataset]))
        vocabulary.update({type: i + 2 for i, type in enumerate(types)})

    # Convert token sequences to integer sequences
    dataset = util.tokens_to_ids(vocabulary, dataset)
    dataset = util.crop_and_pad(dataset, seq_length, logger=logger)

    X = np.array([example["tokens"] for example in dataset],
                 dtype=np.int32)
    transitions = np.array([example["transitions"] for example in dataset],
                           dtype=np.int32)

    y = np.array([0 if example["label"] == "F" else 1 for example in dataset],
                 dtype=np.int32)

    if logger:
        logger.Log("Loaded %i examples to sequences of length %i" %
                   (len(dataset), seq_length))

    # Build batched data iterator.
    all_data = (X, transitions, y)
    if eval_mode:
        data_iter = util.MakeEvalIterator(all_data, batch_size)
    else:
        data_iter = util.MakeTrainingIterator(all_data, batch_size)

    return data_iter, vocabulary


if __name__ == "__main__":
    # Demo:
    examples = import_binary_bracketed_data('bl-data/bl_dev.tsv')
    print examples[0]
