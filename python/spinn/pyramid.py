
import numpy as np

# PyTorch
import torch
import torch.nn as nn
from torch.autograd import Variable
import torch.nn.functional as F

from spinn.util.blocks import Embed, to_gpu, MLP, Linear, HeKaimingInitializer, gumbel_sample
from spinn.util.misc import Args, Vocab
from spinn.util.blocks import SimpleTreeLSTM
from spinn.util.sparks import sparks


def build_model(data_manager, initial_embeddings, vocab_size,
                num_classes, FLAGS, context_args, composition_args, **kwargs):
    use_sentence_pair = data_manager.SENTENCE_PAIR_DATA
    model_cls = Pyramid

    return model_cls(model_dim=FLAGS.model_dim,
                     word_embedding_dim=FLAGS.word_embedding_dim,
                     vocab_size=vocab_size,
                     initial_embeddings=initial_embeddings,
                     num_classes=num_classes,
                     embedding_keep_rate=FLAGS.embedding_keep_rate,
                     use_sentence_pair=use_sentence_pair,
                     use_difference_feature=FLAGS.use_difference_feature,
                     use_product_feature=FLAGS.use_product_feature,
                     classifier_keep_rate=FLAGS.semantic_classifier_keep_rate,
                     mlp_dim=FLAGS.mlp_dim,
                     num_mlp_layers=FLAGS.num_mlp_layers,
                     mlp_ln=FLAGS.mlp_ln,
                     composition_ln=FLAGS.composition_ln,
                     context_args=context_args,
                     trainable_temperature=FLAGS.pyramid_trainable_temperature,
                     test_temperature_multiplier=FLAGS.pyramid_test_time_temperature_multiplier,
                     selection_dim=FLAGS.pyramid_selection_dim,
                     gumbel=FLAGS.pyramid_gumbel,
                     )


class Pyramid(nn.Module):

    def __init__(self, model_dim=None,
                 word_embedding_dim=None,
                 vocab_size=None,
                 use_product_feature=None,
                 use_difference_feature=None,
                 initial_embeddings=None,
                 num_classes=None,
                 embedding_keep_rate=None,
                 use_sentence_pair=False,
                 classifier_keep_rate=None,
                 mlp_dim=None,
                 num_mlp_layers=None,
                 mlp_ln=None,
                 composition_ln=None,
                 context_args=None,
                 trainable_temperature=None,
                 test_temperature_multiplier=None,
                 selection_dim=None,
                 gumbel=None,
                 **kwargs
                 ):
        super(Pyramid, self).__init__()

        self.use_sentence_pair = use_sentence_pair
        self.use_difference_feature = use_difference_feature
        self.use_product_feature = use_product_feature
        self.model_dim = model_dim
        self.test_temperature_multiplier = test_temperature_multiplier
        self.trainable_temperature = trainable_temperature
        self.gumbel = gumbel
        self.selection_dim = selection_dim

        self.classifier_dropout_rate = 1. - classifier_keep_rate
        self.embedding_dropout_rate = 1. - embedding_keep_rate

        vocab = Vocab()
        vocab.size = initial_embeddings.shape[0] if initial_embeddings is not None else vocab_size
        vocab.vectors = initial_embeddings

        self.embed = Embed(word_embedding_dim, vocab.size, vectors=vocab.vectors)

        self.composition_fn = SimpleTreeLSTM(model_dim / 2,
                                             composition_ln=composition_ln)
        self.selection_fn_1 = Linear(initializer=HeKaimingInitializer)(model_dim, selection_dim)
        self.selection_fn_2 = Linear(initializer=HeKaimingInitializer)(selection_dim, 1)

        def selection_fn(selection_input):
            selection_hidden = F.tanh(self.selection_fn_1(selection_input))
            return self.selection_fn_2(selection_hidden)
        self.selection_fn = selection_fn

        mlp_input_dim = self.get_features_dim()

        self.mlp = MLP(mlp_input_dim, mlp_dim, num_classes,
                       num_mlp_layers, mlp_ln, self.classifier_dropout_rate)

        if self.trainable_temperature:
            self.temperature = nn.Parameter(torch.ones(1, 1), requires_grad=True)

        self.encode = context_args.encoder
        self.reshape_input = context_args.reshape_input
        self.reshape_context = context_args.reshape_context

        # For sample printing and logging
        self.merge_sequence_memory = None
        self.inverted_vocabulary = None
        self.temperature_to_display = 0.0

    def run_hard_pyramid(self, x, show_sample=False):
        batch_size, seq_len, model_dim = x.data.size()

        state_pairs = torch.chunk(x, seq_len, 1)
        unbatched_state_pairs = [[] for _ in range(batch_size)]
        for i in range(seq_len):
            unbatched_step = torch.chunk(state_pairs[i], batch_size, 0)
            for b in range(batch_size):
                unbatched_state_pairs[b].append(unbatched_step[b])

        if show_sample:
            self.merge_sequence_memory = []
        else:
            self.merge_sequence_memory = None

        def recompute_selection_logits(to_recompute):
            "Recompute logits at certain locations in the (batch size) x (sequences length) matrix.
            The input is a list of pairs, each pair being:
                0: the batch position (which sentence in the batch)
                1: the position in the sentence (which word in the sentence). The smaller index is used.
                e.g. 'rescore words 3 and 4 in sentence 6' -> (6, 3)"
            left = torch.squeeze(
                torch.cat([unbatched_state_pairs[batch_pos][merge_pos][:, :, self.model_dim / 2:] for batch_pos, merge_pos in to_recompute], 0))
            right = torch.squeeze(
                torch.cat([unbatched_state_pairs[batch_pos][merge_pos + 1][:, :, self.model_dim / 2:] for batch_pos, merge_pos in to_recompute], 0))
            selection_input = torch.cat([left, right], 1)
            selection_logit = self.selection_fn(selection_input)
            split_selection_logit = torch.chunk(selection_logit, len(to_recompute), 0)
            assert len(to_recompute) == len(split_selection_logit), 'not all to_recompute inputs get a result'
            return split_selection_logit

        # Most activations won't change between steps, so this can be preserved
        # and updated only when needed.
        unbatched_selection_logits_list = [[] for _ in range(batch_size)]
        for position in range(seq_len - 1):
            to_recompute = [(b, position) for b in range(batch_size)]
            split_selection_logit = recompute_selection_logits(to_recompute)
            for b in range(batch_size):
                # Keep as Variables so they can update parameters via RL later.
                unbatched_selection_logits_list[b].append(
                    split_selection_logit[b])
        assert len(unbatched_selection_logits_list) == batch_size
        for sublist in unbatched_selection_logits_list:
            assert len(sublist) == seq_len - 1

        # unbatched_selection_logits_list[batch_position][sentence_position]
        # Each entry is a 1x1 Variable. Variables contain raw output of
        # selection network, pre-softmax.

        # Combine this with advantage to do RL.
        # Each sublist should be the same size (B)
        selected_logits_per_layer = [] # [layer][batch_position]

        for layer in range(seq_len - 1, 0, -1):
            # SLL: B arrays of size S
            # Doing softmax prevents logits from exploding
            selection_logits = F.log_softmax(torch.cat([
                torch.cat([unbatched_selection_logits_list[b][i]
                                for i in range(layer)], 1)
                for b in range(batch_size)], 0))
            merge_indices = torch.max(selection_logits, 1)[1].data.cpu()

            # Remember chosen logits so they can be reinforced later on.
            selected_logits_per_layer.append([
                unbatched_selection_logits_list[b][merge_indices[b]]
                    for b in range(batch_size)])

            if show_sample:
                self.merge_sequence_memory.append(merge_indices[8])

            # Collect inputs to merge
            lefts = [unbatched_state_pairs[b][merge_indices[b]] for b in range(batch_size)]
            rights = [unbatched_state_pairs[b][merge_indices[b] + 1] for b in range(batch_size)]

            # Run the merge
            left = torch.squeeze(torch.cat(lefts, 0))
            right = torch.squeeze(torch.cat(rights, 0))

            composition_result = torch.unsqueeze(self.composition_fn(left, right), 1)

            # Unpack and apply
            composition_result_list = torch.chunk(composition_result, batch_size, 0)
            for b in range(batch_size):
                unbatched_state_pairs[b][merge_indices[b]] = composition_result_list[b]
                del unbatched_state_pairs[b][merge_indices[b] + 1]

            # Recompute invalidated selection logits in one big batch:
            # This is organized this way as the amount that needs to recompute depends
            # on the number of merges that were at the edge of the pyramid structure.
            if layer > 1:
                # Each item is a pair (batch_position, merge_position)
                to_recompute = []
                for b in range(batch_size):
                    del unbatched_selection_logits_list[b][merge_indices[b]]
                    if merge_indices[b] > 0:
                        to_recompute.append((b, merge_indices[b] - 1))
                    if merge_indices[b] < len(unbatched_selection_logits_list[b]):
                        to_recompute.append((b, merge_indices[b]))
                split_selection_logit = recompute_selection_logits(to_recompute)
                for i in range(len(to_recompute)):
                    batch_pos, merge_pos = to_recompute[i]
                    unbatched_selection_logits_list[index_pair[0]][index_pair[1]] = \
                        split_selection_logit[i]

        for sublist in unbatched_selection_logits_list:
            assert len(sublist) == 1
        return torch.squeeze(
            torch.cat([unbatched_state_pairs[b][0][:, :, self.model_dim / 2:] for b in range(batch_size)], 0))

    def run_embed(self, x):
        batch_size, seq_length = x.size()

        embeds = self.embed(x)
        embeds = self.reshape_input(embeds, batch_size, seq_length)
        embeds = self.encode(embeds)
        embeds = self.reshape_context(embeds, batch_size, seq_length)
        embeds = torch.cat([b.unsqueeze(0) for b in torch.chunk(embeds, batch_size, 0)], 0)
        embeds = F.dropout(embeds, self.embedding_dropout_rate, training=self.training)

        return embeds

    def forward(self, sentences, transitions, y_batch=None, show_sample=False,
                pyramid_temperature_multiplier=1.0, **kwargs):
        # Useful when investigating dynamic batching:
        # self.seq_lengths = sentences.shape[1] - (sentences == 0).sum(1)

        x = self.unwrap(sentences, transitions)
        emb = self.run_embed(x)

        hh = self.run_hard_pyramid(emb, show_sample)

        h = self.wrap(hh)
        output = self.mlp(self.build_features(h))

        # From output_hook of rl_spinn
        if not self.training:
            return {
                'output': output
            }

        probs = F.softmax(output).data.cpu()
        target = torch.from_numpy(y_batch).long()

        # Get Reward.
        # TODO: reimplement transition_acc_as_reward
        rewards = self.build_reward(probs, target, rl_reward=self.rl_reward)

        # Get Baseline.
        baseline = self.build_baseline(
            rewards, sentences, transitions, y_batch)

        # Calculate advantage.
        advantage = rewards - baseline

        # Whiten advantage. This is also called Variance Normalization.
        if self.rl_whiten:
            advantage = (advantage - advantage.mean()) / \
                (advantage.std() + 1e-8)

        # Assign REINFORCE output.
        self.policy_loss = self.reinforce(advantage)

        return {
            'output': output,
            'policy_loss': self.reinforce(advantage)
        }

    def get_features_dim(self):
        features_dim = self.model_dim if self.use_sentence_pair else self.model_dim / 2
        if self.use_sentence_pair:
            if self.use_difference_feature:
                features_dim += self.model_dim / 2
            if self.use_product_feature:
                features_dim += self.model_dim / 2
        return features_dim

    def build_features(self, h):
        if self.use_sentence_pair:
            h_prem, h_hyp = h
            features = [h_prem, h_hyp]
            if self.use_difference_feature:
                features.append(h_prem - h_hyp)
            if self.use_product_feature:
                features.append(h_prem * h_hyp)
            features = torch.cat(features, 1)
        else:
            features = h
        return features

    # --- Sample printing ---

    def prettyprint_sample(self, tree):
        if isinstance(tree, tuple):
            return '( ' + self.prettyprint_sample(tree[0]) + \
                ' ' + self.prettyprint_sample(tree[1]) + ' )'
        else:
            return tree

    def get_sample(self, x, vocabulary):
        if not self.inverted_vocabulary:
            self.inverted_vocabulary = dict([(vocabulary[key], key) for key in vocabulary])
        token_sequence = [self.inverted_vocabulary[token] for token in x[8, :]]
        for merge in self.get_sample_merge_sequence():
            token_sequence[merge] = (token_sequence[merge], token_sequence[merge + 1])
            del token_sequence[merge + 1]
        return token_sequence[0]

    def get_sample_merge_sequence(self):
        return self.merge_sequence_memory

    # --- Sentence Style Switches ---

    def unwrap(self, sentences, transitions):
        if self.use_sentence_pair:
            return self.unwrap_sentence_pair(sentences, transitions)
        return self.unwrap_sentence(sentences, transitions)

    def wrap(self, hh):
        if self.use_sentence_pair:
            return self.wrap_sentence_pair(hh)
        return self.wrap_sentence(hh)

    # --- Sentence Specific ---

    def unwrap_sentence_pair(self, sentences, transitions):
        x_prem = sentences[:, :, 0]
        x_hyp = sentences[:, :, 1]
        x = np.concatenate([x_prem, x_hyp], axis=0)

        return to_gpu(Variable(torch.from_numpy(x), volatile=not self.training))

    def wrap_sentence_pair(self, hh):
        batch_size = hh.size(0) / 2
        h = ([hh[:batch_size], hh[batch_size:]])
        return h

    # --- Sentence Pair Specific ---

    def unwrap_sentence(self, sentences, transitions):
        return to_gpu(Variable(torch.from_numpy(sentences), volatile=not self.training))

    def wrap_sentence(self, hh):
        return hh
