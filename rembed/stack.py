"""Theano-based stack implementations."""

from functools import partial

import numpy as np
import theano
from theano.ifelse import ifelse

from theano import tensor as T
from rembed import cuda_util, util


def update_hard_stack(t, t_f, stack_t, push_value, merge_value, merge_queue_t,
                      merge_cursors_t, mask, batch_size, stack_shift, cursors_shift):
    """Compute the new value of the given hard stack.

    This performs stack pushes and pops in parallel, and somewhat wastefully.
    It accepts a precomputed merge result (in `merge_value`) and a precomputed
    push value `push_value` for all examples, and switches between the two
    outcomes based on the per-example value of `mask`.

    Args:
        stack_t: Current stack value
        stack_pushed: Helper stack structure, of same size as `stack_t`
        stack_merged: Helper stack structure, of same size as `stack_t`
        push_value: Batch of values to be pushed
        merge_value: Batch of merge results
        mask: Batch of booleans: 1 if merge, 0 if push
    """

    mask2 = mask.dimshuffle(0, "x")
    top_next = mask2 * merge_value + (1 - mask2) * push_value
    stack_next = cuda_util.AdvancedIncSubtensor1Floats(set_instead_of_inc=True, inplace=True)(
            stack_t, top_next, t_f * batch_size + stack_shift)

    cursors_next = merge_cursors_t + (mask * -1 + (1 - mask) * 1)
    queue_next = cuda_util.AdvancedIncSubtensor1Floats(set_instead_of_inc=True, inplace=True)(
            merge_queue_t, t_f, cursors_shift + cursors_next)
    # DEV super hacky: don't update queue unless we have valid cursors
    # TODO necessary?
    queue_next = ifelse(cursors_next.min() < 0, merge_queue_t + 0.0, queue_next)

    return stack_next, queue_next, cursors_next


class HardStack(object):

    """
    Model 0/1/2 hard stack implementation.

    This model scans a sequence using a hard stack. It optionally predicts
    stack operations using an MLP, and can receive supervision on these
    predictions from some external parser which acts as the "ground truth"
    parser.

    Model 0: prediction_and_tracking_network=None, train_with_predicted_transitions=False
    Model 1: prediction_and_tracking_network=something, train_with_predicted_transitions=False
    Model 2: prediction_and_tracking_network=something, train_with_predicted_transitions=True
    """

    def __init__(self, model_dim, word_embedding_dim, batch_size, vocab_size, seq_length, compose_network,
                 embedding_projection_network, training_mode, ground_truth_transitions_visible, vs,
                 prediction_and_tracking_network=None,
                 predict_transitions=False,
                 train_with_predicted_transitions=False,
                 interpolate=False,
                 X=None,
                 transitions=None,
                 initial_embeddings=None,
                 make_test_fn=False,
                 use_input_batch_norm=True,
                 use_input_dropout=True,
                 embedding_dropout_keep_rate=1.0,
                 ss_mask_gen=None,
                 ss_prob=0.0,
                 use_tracking_lstm=False,
                 tracking_lstm_hidden_dim=8,
                 connect_tracking_comp=False,
                 context_sensitive_shift=False,
                 context_sensitive_use_relu=False):
        """
        Construct a HardStack.

        Args:
            model_dim: Dimensionality of token embeddings and stack values
            word_embedding_dim: dimension of the word embedding
            vocab_size: Number of unique tokens in vocabulary
            seq_length: Maximum sequence length which will be processed by this
              stack
            compose_network: Blocks-like function which accepts arguments
              `inp, inp_dim, outp_dim, vs, name` (see e.g. `util.Linear`).
              Given a Theano batch `inp` of dimension `batch_size * inp_dim`,
              returns a transformed Theano batch of dimension
              `batch_size * outp_dim`.
            embedding_projection_network: Same form as `compose_network`.
            training_mode: A Theano scalar indicating whether to act as a training model
              with dropout (1.0) or to act as an eval model with rescaling (0.0).
            ground_truth_transitions_visible: A Theano scalar. If set (1.0), allow the model access
              to ground truth transitions. This can be disabled at evaluation time to force Model 1
              (or 12SS) to evaluate in the Model 2 style with predicted transitions. Has no effect
              on Model 0.
            vs: VariableStore instance for parameter storage
            prediction_and_tracking_network: Blocks-like function which either maps values
              `3 * model_dim` to `action_dim` or uses the more complex TrackingUnit template.
            predict_transitions: If set, predict transitions. If not, the tracking LSTM may still
              be used for other purposes.
            train_with_predicted_transitions: If `True`, use the predictions from the model
              (rather than the ground-truth `transitions`) to perform stack
              operations
            interpolate: If True, use scheduled sampling while training
            X: Theano batch describing input matrix, or `None` (in which case
              this instance will make its own batch variable).
            transitions: Theano batch describing transition matrix, or `None`
              (in which case this instance will make its own batch variable).
            initial_embeddings: pretrained embeddings or None
            make_test_fn: If set, create a function to run a scan for testing.
            use_input_batch_norm: If True, use batch normalization
            use_input_dropout: If True, use dropout
            embedding_dropout_keep_rate: The keep rate for dropout on projected embeddings.
            ss_mask_gen: A theano random stream
            ss_prob: Scheduled sampling probability
            use_tracking_lstm: If True, LSTM will be used in the tracking unit
            tracking_lstm_hidden_dim: hidden state dimension of the tracking LSTM
            connect_tracking_comp: If True, the hidden state of tracking LSTM will be
                fed to the TreeLSTM in the composition unit
            context_sensitive_shift: If True, the hidden state of tracking LSTM and the embedding
                vector will be used to calculate the vector that will be pushed onto the stack
            context_sensitive_use_relu: If True, a ReLU layer will be used while doing context
                sensitive shift, otherwise a Linear layer will be used
        """

        self.model_dim = model_dim
        self.word_embedding_dim = word_embedding_dim
        self.use_tracking_lstm = use_tracking_lstm
        self.tracking_lstm_hidden_dim = tracking_lstm_hidden_dim

        self.batch_size = batch_size
        self.vocab_size = vocab_size
        self.seq_length = seq_length
        self.stack_size = seq_length

        self._compose_network = compose_network
        self._embedding_projection_network = embedding_projection_network
        self._prediction_and_tracking_network = prediction_and_tracking_network
        self._predict_transitions = predict_transitions
        self.train_with_predicted_transitions = train_with_predicted_transitions

        self._vs = vs

        self.initial_embeddings = initial_embeddings

        self.training_mode = training_mode
        self.ground_truth_transitions_visible = ground_truth_transitions_visible
        self.embedding_dropout_keep_rate = embedding_dropout_keep_rate

        self.X = X
        self.transitions = transitions

        self.use_input_batch_norm = use_input_batch_norm
        self.use_input_dropout = use_input_dropout

        # Mask for scheduled sampling.
        self.ss_mask_gen = ss_mask_gen
        # Flag for scheduled sampling.
        self.interpolate = interpolate
        # Training step number.
        self.ss_prob = ss_prob
        # Connect tracking unit and composition unit.
        self.connect_tracking_comp = connect_tracking_comp
        assert (use_tracking_lstm or not connect_tracking_comp), \
            "Must use tracking LSTM if connecting tracking and composition units"
        self.context_sensitive_shift = context_sensitive_shift
        assert (use_tracking_lstm or not context_sensitive_shift), \
            "Must use tracking LSTM while doing context sensitive shift"
        self.context_sensitive_use_relu = context_sensitive_use_relu

        self._make_params()
        self._make_shared()
        self._make_inputs()
        self._make_scan()

        if make_test_fn:
            self.scan_fn = theano.function([self.X, self.transitions, self.training_mode,
                                            self.ground_truth_transitions_visible],
                                           self.stack, updates=self.scan_updates,
                                           accept_inplace=True,
                                           on_unused_input="warn")

    def _make_params(self):
        # Per-token embeddings.
        if self.initial_embeddings is not None:
            def EmbeddingInitializer(shape):
                return self.initial_embeddings
            self.embeddings = self._vs.add_param(
                    "embeddings", (self.vocab_size, self.word_embedding_dim),
                    initializer=EmbeddingInitializer,
                    trainable=False)
        else:
            self.embeddings = self._vs.add_param(
                "embeddings", (self.vocab_size, self.word_embedding_dim))

    def _make_shared(self):
        stack_init = np.zeros((self.stack_size * self.batch_size, self.model_dim), dtype=np.float32)
        self._stack_orig = theano.shared(stack_init, borrow=False, name="stack_orig")
        self.stack = theano.shared(stack_init, borrow=False, name="stack")

        stack_bwd_init = np.zeros((self.stack_size * self.batch_size, self.model_dim), dtype=np.float32)
        self._stack_bwd_orig = theano.shared(stack_bwd_init, borrow=False, name="stack_bwd_orig")
        self.stack_bwd = theano.shared(stack_bwd_init, borrow=False, name="stack_bwd")

        # TODO: don't fix model_dim
        aux_stack_init = np.zeros((self.stack_size * self.batch_size, self.tracking_lstm_hidden_dim * 2), dtype=np.float32)
        self._aux_stack_orig = theano.shared(aux_stack_init, borrow=False, name="aux_stack_orig")
        self.aux_stack = theano.shared(aux_stack_init, borrow=False, name="aux_stack")

        cursors_init = np.zeros((self.batch_size,)).astype(np.float32) - 1.0
        self._cursors_orig = theano.shared(cursors_init, borrow=False, name="cursors_orig")
        self.cursors = theano.shared(cursors_init, borrow=False, name="cursors")

        queue_init = np.zeros((self.batch_size * self.stack_size,)).astype(np.float32)
        self._queue_orig = theano.shared(queue_init, borrow=False, name="queue_orig")
        self.queue = theano.shared(queue_init, borrow=False, name="queue")

        zero_updates = {
                self.stack: self._stack_orig,
                self.aux_stack: self._aux_stack_orig,
                self.cursors: self._cursors_orig,
                self.queue: self._queue_orig
        }
        self.zero = theano.function([], (), updates=zero_updates)

    def _make_inputs(self):
        self.X = self.X or T.imatrix("X")
        self.transitions = self.transitions or T.imatrix("transitions")

    def _step(self, t, t_f, transitions_t, transitions_t_f, ss_mask_gen_matrix_t,
              buffer_cur_t, tracking_hidden, buffer,
              ground_truth_transitions_visible):
        batch_size, _ = self.X.shape

        # Extract top buffer values.
        idxs = buffer_cur_t + self._buffer_shift
        buffer_top_t = cuda_util.AdvancedSubtensor1Floats("F_buffer_top")(buffer, idxs)

        if self.context_sensitive_shift:
            # Combine with the hidden state from previous unit.
            tracking_h_t = tracking_hidden[:, :self.tracking_lstm_hidden_dim]
            context_comb_input_t = T.concatenate([tracking_h_t, buffer_top_t], axis=1)
            context_comb_input_dim = self.word_embedding_dim + self.tracking_lstm_hidden_dim
            comb_layer = util.ReLULayer if self.context_sensitive_use_relu else util.Linear
            buffer_top_t = comb_layer(context_comb_input_t, context_comb_input_dim, self.model_dim,
                                self._vs, name="context_comb_unit", use_bias=True,
                                initializer=util.HeKaimingInitializer())

        # Fetch top two stack elements.
        stack_1 = cuda_util.AdvancedSubtensor1Floats("F_stack1")(self.stack, (t - 1) * self.batch_size + self._stack_shift)
        # Get pointers into stack for second-to-top element.
        cursors = self.cursors - 1.0
        stack_2_ptrs = cuda_util.AdvancedSubtensor1Floats("F_stack2_ptrs")(self.queue, cursors + self._queue_shift)
        stack_2_ptrs = stack_2_ptrs * batch_size + self._stack_shift
        # Retrieve second-to-top element.
        stack_2 = cuda_util.AdvancedSubtensor1Floats("F_stack2")(self.stack, stack_2_ptrs)

        # Zero out stack_2 elements which are invalid (i.e., were drawn with
        # negative cursor values)
        #
        # TODO: Probably incurs H<->D because of bool mask. Do on the GPU.
        #
        # TODO: Factor out this zeros constant and the one in the next ifelse
        # op
        stack_2_mask = cursors < 0
        stack_2_mask2 = stack_2_mask.dimshuffle(0, "x")
        stack_2 = stack_2_mask2 * T.zeros((self.batch_size, self.model_dim)) + (1. - stack_2_mask2) * stack_2
        # Also update stack_2_ptrs for backprop pass. Set -1 sentinel, which
        # indicates that stack_2 is empty.
        stack_2_ptrs = stack_2_mask * (-1. * T.ones((self.batch_size,))) + (1. - stack_2_mask) * stack_2_ptrs

        # stack_2 values are not valid unless we are on t >= 1 (TODO?)
        stack_2 = ifelse(t <= 1, T.zeros((self.batch_size, self.model_dim)), stack_2)

        if self._prediction_and_tracking_network is not None:
            # We are predicting our own stack operations.
            predict_inp = T.concatenate(
                [stack_1, stack_2, buffer_top_t], axis=1)

            if self.use_tracking_lstm:
                # Update the hidden state and obtain predicted actions.
                tracking_hidden, actions_t = self._prediction_and_tracking_network(
                    tracking_hidden, predict_inp, self.model_dim * 3,
                    self.tracking_lstm_hidden_dim, self._vs,
                    name="prediction_and_tracking")
            else:
                # Obtain predicted actions directly.
                actions_t = self._prediction_and_tracking_network(
                    predict_inp, self.model_dim * 3, util.NUM_TRANSITION_TYPES, self._vs,
                    name="prediction_and_tracking")

        if self.train_with_predicted_transitions:
            # Model 2 case.
            if self.interpolate:
                # Only use ground truth transitions if they are marked as visible to the model.
                effective_ss_mask_gen_matrix_t = ss_mask_gen_matrix_t * ground_truth_transitions_visible
                # Interpolate between truth and prediction using bernoulli RVs
                # generated prior to the step.
                mask = (transitions_t * effective_ss_mask_gen_matrix_t
                        + actions_t.argmax(axis=1) * (1 - effective_ss_mask_gen_matrix_t))
            else:
                # Use predicted actions to build a mask.
                mask = actions_t.argmax(axis=1)
        elif self._predict_transitions:
            # Use transitions provided from external parser when not masked out
            mask = (transitions_t * ground_truth_transitions_visible
                        + actions_t.argmax(axis=1) * (1 - ground_truth_transitions_visible))
        else:
            # Model 0 case
            mask = transitions_t_f

        # Now update the stack: first precompute merge results.
        merge_items = T.concatenate([stack_1, stack_2], axis=1)
        if self.connect_tracking_comp:
            tracking_h_t = tracking_hidden[:, :self.tracking_lstm_hidden_dim]
            merge_value = self._compose_network(merge_items, tracking_h_t, self.model_dim,
                self._vs, name="compose", external_state_dim=self.tracking_lstm_hidden_dim)
        else:
            merge_value = self._compose_network(merge_items, self.model_dim * 2, self.model_dim,
                self._vs, name="compose")

        # Compute new stack value.
        stack_next, merge_queue_next, merge_cursors_next = update_hard_stack(
            t, t_f, self.stack, buffer_top_t, merge_value, self.queue, self.cursors,
            mask, self.batch_size, self._stack_shift, self._cursors_shift)

        # Move buffer cursor as necessary. Since mask == 1 when merge, we
        # should increment each buffer cursor by 1 - mask.
        buffer_cur_next = buffer_cur_t + (1 - transitions_t_f)

        # Update auxiliary stacks. (DEV)
        aux_stack_next = cuda_util.AdvancedIncSubtensor1Floats(set_instead_of_inc=True)(
                self.aux_stack, tracking_hidden, t_f * self.batch_size + self._stack_shift)

        if self._predict_transitions:
            ret_val = buffer_cur_next, tracking_hidden, actions_t
        else:
            ret_val = buffer_cur_next, tracking_hidden, stack_2_ptrs

        if not self.interpolate:
            # Use ss_mask as a redundant return value.
            ret_val = (ss_mask_gen_matrix_t,) + ret_val

        updates = {
            self.stack: stack_next,
            self.aux_stack: aux_stack_next,
            self.queue: merge_queue_next,
            self.cursors: merge_cursors_next
        }

        return ret_val, updates

    def _make_scan(self):
        """Build the sequential composition / scan graph."""

        batch_size = self.batch_size
        max_stack_size = stack_size = self.stack_size
        self.batch_range = batch_range = T.arange(batch_size, dtype="int32")

        self._queue_shift = T.cast(batch_range * self.seq_length,
                                   theano.config.floatX)
        self._buffer_shift = self._queue_shift
        self._cursors_shift = T.cast(batch_range * self.stack_size,
                                     theano.config.floatX)
        self._stack_shift = T.cast(batch_range, theano.config.floatX)

        # Look up all of the embeddings that will be used.
        raw_embeddings = self.embeddings[self.X]  # batch_size * seq_length * emb_dim

        if self.context_sensitive_shift:
            # Use the raw embedding vectors, they will be combined with the hidden state of
            # the tracking unit later
            buffer_t = raw_embeddings
            buffer_emb_dim = self.word_embedding_dim
        else:
            # Allocate a "buffer" stack initialized with projected embeddings,
            # and maintain a cursor in this buffer.
            buffer_t = self._embedding_projection_network(
                raw_embeddings, self.word_embedding_dim, self.model_dim, self._vs, name="project")
            if self.use_input_batch_norm:
                buffer_t = util.BatchNorm(buffer_t, self.model_dim, self._vs, "buffer",
                    self.training_mode, axes=[0, 1])
            if self.use_input_dropout:
                buffer_t = util.Dropout(buffer_t, self.embedding_dropout_keep_rate, self.training_mode)
            buffer_emb_dim = self.model_dim

        # Collapse buffer to (batch_size * buffer_size) * emb_dim for fast indexing.
        self.buffer_t = buffer_t = buffer_t.reshape((-1, buffer_emb_dim))

        buffer_cur_init = T.zeros((batch_size,), theano.config.floatX)

        DUMMY = T.zeros((2, 2)) # a dummy tensor used as a place-holder

        # Dimshuffle inputs to seq_len * batch_size for scanning
        transitions = self.transitions.dimshuffle(1, 0)
        transitions_f = T.cast(transitions, dtype=theano.config.floatX)

        # Initialize the hidden state for the tracking LSTM, if needed.
        if self.use_tracking_lstm:
            # TODO: Unify what 'dim' means with LSTM. Here, it's the dim of
            # each of h and c. For 'model_dim', it's the combined dimension
            # of the full hidden state (so h and c are each model_dim/2).
            hidden_init = T.zeros((batch_size, self.tracking_lstm_hidden_dim * 2))
        else:
            hidden_init = DUMMY

        # Set up the output list for scanning over _step().
        if self._predict_transitions:
            outputs_info = [stack_init, buffer_cur_init, hidden_init, None]
        else:
            outputs_info = [buffer_cur_init, hidden_init, None]

        # Prepare data to scan over.
        sequences = [T.arange(transitions.shape[0]),
                     T.arange(transitions.shape[0], dtype="float32"),
                     transitions, transitions_f]
        if self.interpolate:
            # Generate Bernoulli RVs to simulate scheduled sampling
            # if the interpolate flag is on.
            ss_mask_gen_matrix = self.ss_mask_gen.binomial(
                                transitions.shape, p=self.ss_prob)
            # Take in the RV sequence as input.
            sequences.append(ss_mask_gen_matrix)
        else:
            # Take in the RV sequqnce as a dummy output. This is
            # done to avaid defining another step function.
            outputs_info = [DUMMY] + outputs_info

        scan_ret, self.scan_updates = theano.scan(
                self._step,
                sequences=sequences,
                non_sequences=[buffer_t, self.ground_truth_transitions_visible],
                outputs_info=outputs_info,
                name="fwd")

        ret_shift = 0 if self.interpolate else 1
        self.final_buf = scan_ret[ret_shift + 0][-1]
        self.stack_2_ptrs = scan_ret[ret_shift + 2]
        self.buf_ptrs = scan_ret[ret_shift + 0]

        self.final_stack = self.scan_updates[self.stack]
        self.final_aux_stack = self.scan_updates[self.aux_stack]

        self.transitions_pred = None
        if self._predict_transitions:
            self.transitions_pred = scan_ret[-1].dimshuffle(1, 0, 2)


    def make_backprop_scan(self, extra_inputs, extra_outputs, error_signal,
                           f_push_delta, f_merge_delta, wrt_shapes):
        """
        Args:
            extra_inputs: List of auxiliary stack representations, matching
                the shape of the main stack on the leading axis (i.e.,
                `(seq_length * batch_size) * input_dim`). Each stack
                stores some value which acted as an extra input at each
                timestep. e.g. a tracking-RNN stack will have as extra_input
                a single stack containing the tracking RNN states.
            extra_outputs: A list signaling any extra outputs the stack step
                function may have had which were involved in the main
                computation. Each list element is an integer specifying the
                dimensionality of the corresponding output. For each output
                we have to store a gradient d(cost)/d(output) in an auxiliary
                stack during backpropagation.
            error_signal: The external gradient d(cost)/d(stack top). A Theano
                batch of size `batch_size * model_dim`.
            f_push_delta: A function generated by
                `util.batch_subgraph_gradients` which represents the gradient
                subgraph for a single push operation. A vanilla push operation
                accepts three inputs (stack top, stack second-from-top,
                buffer top) and generates 0 outputs (an embedding is simply
                moved to the stack). In this case, `f_push_delta` will be
                called as follows:

                    f_push_delta((stack_1, stack_2, buf_top), (,))

                In the general case, `f_push_delta` is called with the
                following form:

                    f_push_delta(inputs, grads_above)

                where `inputs` is `(stack_1, stack_2, buf_top)` plus any extra
                inputs (specified in `extra_inputs`) and `grads_above` is a
                list of gradients d(cost)/d(output_i). `grads_above` has the
                same length as `extra_outputs`.

                `f_push_delta` should return a tuple `(d_inp, d_wrt)`, where
                `d_inp` is a list of gradients of cost with respect to each
                provided input (e.g. `stack_1`, `stack_2`, `buf_top` for the
                vanilla case). These gradients become the `grads_above` for
                some previous timestep in a vanilla stack implementation.
                `d_wrt` is a list of cost gradients with respect to some
                external parameter set. You must specify the expected shapes
                of the `d_wrt` outputs in the `wrt_shapes` parameter to this
                backprop generator.

                For more information on `f_push_delta` (e.g. on its expected
                return value, see `util.batch_subgraph_gradients`). This
                backprop generator is heavily coupled with this `util`
                function, and we expect you to use it. Otherwise success is not
                guaranteed. :)
            f_merge_delta: A function like `f_push_delta` but which specifies
                the gradient subgraph for a single merge operation. Because a
                vanilla merge operation has 1 output (not zero, like pushing),
                you can always at least one `grad_above` to be provided to this
                function. Of course, the basic form is still the same:

                    f_merge_delta((stack_1, stack_2, buf_top), (err_signal))

                where `err_signal` here represents
                d(cost)/d(stack element at this timestep).

                `f_merge_delta` should otherwise behave exactly the same
                externally as `f_push_delta`. Its `d_wrt` outputs must match
                those of `f_push_delta`. Again, see
                `util.batch_subgraph_gradients` for a full and general spec for
                this function.
            wrt_shapes: A list of tuple shape specifications for the `d_wrt`
                outputs of the subgradient graphs. e.g. if we are accumulating
                gradients w.r.t. a 50x20-dim matrix `W` and a 20-dim vector
                `b`, `wrt_shapes` would be `[(50, 20), (20,)]`.
        """

        if not hasattr(self, "stack_2_ptrs"):
            raise RuntimeError("self._make_scan (forward pass) must be defined "
                               "before self.make_backprop_scan is called")

        extra_bwd_init = [theano.shared(np.zeros((self.stack_size * self.batch_size, dim), dtype=np.float32),
                                        borrow=False, name="bprop/bwd/%i" % i)
                          for i, dim in enumerate(extra_outputs)]
        # TODO add to zero fn

        # Useful batch zero-constants.
        zero_stack = T.zeros((self.batch_size, self.model_dim))
        zero_extra_inps = [T.zeros((self.batch_size, inp.shape[1])) for inp in extra_inputs]

        batch_size = self.batch_size
        batch_range = T.arange(batch_size)
        stack_shift = T.cast(batch_range, theano.config.floatX)
        buffer_shift = T.cast(batch_range * self.seq_length, theano.config.floatX)

        def lookup(t_f, stack_fwd, stack_2_ptrs_t, buffer_cur_t,
                  stack_bwd_t, extra_bwd):
            """Retrieve all relevant bwd inputs/outputs at time `t`."""

            grad_cursor = t_f * batch_size + stack_shift
            main_grad = cuda_util.AdvancedSubtensor1Floats("B_maingrad")(
                stack_bwd_t, grad_cursor)
            extra_grads = tuple([
                cuda_util.AdvancedSubtensor1Floats("B_extragrad_%i" % i)(
                    extra_bwd_i, grad_cursor)
                for i, extra_bwd_i in enumerate(extra_bwd)])

            # Find the timesteps of the two elements involved in the potential
            # merge at this timestep.
            t_c1 = (t_f - 1.0) * batch_size + stack_shift
            t_c2 = stack_2_ptrs_t

            # Find the two elements involved in the potential merge.
            c1 = cuda_util.AdvancedSubtensor1Floats("B_stack1")(stack_fwd, t_c1)
            c2 = cuda_util.AdvancedSubtensor1Floats("B_stack2")(stack_fwd, t_c2)

            # Mask over examples which have invalid c2 cursors.
            c2_mask = (t_c2 < 0).dimshuffle(0, "x")
            c2 = c2_mask * zero_stack + (1. - c2_mask) * c2

            # Guard against indexing edge cases.
            c1 = ifelse(T.eq(t_f, 0.0), zero_stack, c1)
            # TODO is this one covered by c2_mask above? I think so.
            c2 = ifelse(t_f <= 1.0, zero_stack, c2)

            buffer_top_t = cuda_util.AdvancedSubtensor1Floats("B_buffer_top")(
                self.buffer_t, buffer_cur_t + buffer_shift)

            # Retrieve extra inputs from auxiliary stack(s).
            extra_inps_t = [cuda_util.AdvancedSubtensor1Floats("B_extra_inp_%i" % i)(
                extra_inp_i, t_c1)
                for extra_inp_i in extra_inputs]
            # TODO could avoid the branching by just pegging on an extra zero
            # row as precomputation
            extra_inps_t = tuple([
                ifelse(T.eq(t_f, 0.0), zero_extra, extra_inp_i)
                for extra_inp_i, zero_extra
                in zip(extra_inps_t, zero_extra_inps)])

            inputs = (c1, c2, buffer_top_t) + extra_inps_t
            grads = (main_grad,) + extra_grads
            return t_c1, t_c2, inputs, grads

        def step_b(# sequences
                   t_f, transitions_t_f, stack_2_ptrs_t, buffer_cur_t,
                   dE,
                   # rest (incl. outputs_info, non_sequences)
                   *rest):
            # Separate the accum arguments from the non-sequence arguments.
            n_wrt, n_extra_bwd = len(wrt_shapes), len(extra_outputs)
            wrt_deltas = rest[:n_wrt]
            stack_bwd_t = rest[n_wrt]
            extra_bwd = rest[n_wrt + 1:n_wrt + 1 + n_extra_bwd]
            id_buffer, stack_final = \
                rest[n_wrt + 1 + n_extra_bwd:n_wrt + 1 + n_extra_bwd + 2]

            # At first iteration, drop the external error signal into the main
            # backward stack.
            stack_bwd_next = ifelse(T.eq(t_f, self.seq_length - 1),
                                    T.set_subtensor(stack_bwd_t[-self.batch_size:], error_signal),
                                    stack_bwd_t)

            # Retrieve all relevant inputs/outputs at this timestep.
            t_c1, t_c2, inputs, grads = \
                lookup(t_f, stack_final, stack_2_ptrs_t, buffer_cur_t,
                       stack_bwd_next, extra_bwd)
            main_grad = grads[0]

            # Calculate deltas for this timestep.
            m_delta_inp, m_delta_wrt = f_merge_delta(inputs, grads)
            # NB: main_grad is not passed to push function.
            p_delta_inp, p_delta_wrt = f_push_delta(inputs, grads[1:])

            # Check that delta function outputs match (at least in number).
            assert len(m_delta_inp) == len(p_delta_inp), \
                "%i %i" % (len(m_delta_inp), len(p_delta_inp))
            assert len(m_delta_wrt) == len(p_delta_wrt), \
                "%i %i" % (len(m_delta_wrt), len(p_delta_wrt))

            # Retrieve embedding indices on buffer at this timestep.
            # (Necessary for sending embedding gradients.)
            buffer_ids_t = cuda_util.AdvancedSubtensor1Floats("B_buffer_ids")(
                    id_buffer, buffer_cur_t + buffer_shift)

            # Prepare masks for op-wise gradient accumulation.
            # TODO: Record actual transitions (e.g. for model 1S and higher)
            # and repeat those here
            mask = transitions_t_f
            masks = [mask, mask.dimshuffle(0, "x"),
                     mask.dimshuffle(0, "x", "x")]

            # Accumulate inp deltas, switching over push/merge decision.
            stacks = (stack_bwd_next, stack_bwd_next, dE) + extra_bwd
            new_stacks = {}
            cursors = (t_c1, t_c2, buffer_ids_t) + ((t_c1,) * len(extra_bwd))
            for stack, cursor, m_delta, p_delta in zip(stacks, cursors, m_delta_inp, p_delta_inp):
                base = new_stacks.get(stack, stack)

                mask_i = masks[m_delta.ndim - 1]
                delta = mask * m_delta + (1. - mask) * p_delta

                # Run subtensor update on associated structure using the
                # current cursor.
                new_stack = cuda_util.AdvancedIncSubtensor1Floats()(
                    base, delta, cursor)
                new_stacks[stack] = new_stack

            # Accumulate wrt deltas, switching over push/merge decision.
            new_wrt_deltas = []
            for i, (accum_delta, m_delta, p_delta) in enumerate(zip(wrt_deltas, m_delta_wrt, p_delta_wrt)):
                # Check that tensors returned by delta functions match shape
                # expectations.
                assert accum_delta.ndim == m_delta.ndim - 1, \
                    "%i %i" % (accum_delta.ndim, m_delta.ndim)
                assert accum_delta.ndim == p_delta.ndim - 1, \
                    "%i %i" % (accum_delta.ndim, p_delta.ndim)

                mask_i = masks[m_delta.ndim - 1]
                # TODO: Is this at all efficient? (Bring back GPURowSwitch?)
                delta = (mask_i * m_delta + (1. - mask_i) * p_delta).sum(axis=0)
                new_wrt_deltas.append(accum_delta + delta)

            # On push ops, backprop the stack_bwd error onto the embedding
            # parameters.
            # TODO make sparse?
            dE_next = new_stacks.pop(dE)
            dE_next = cuda_util.AdvancedIncSubtensor1Floats()(
                dE_next, (1. - masks[1]) * main_grad, buffer_ids_t)

            updates = util.prepare_updates_dict(new_stacks)
            return [dE_next] + new_wrt_deltas, updates

        # TODO: These should come from forward pass -- not fixed -- in model
        # 1S, etc.
        transitions_f = T.cast(self.transitions.dimshuffle(1, 0),
                               dtype=theano.config.floatX)

        ts_f = T.cast(T.arange(transitions_f.shape[0]), dtype=theano.config.floatX)

        # Representation of buffer using embedding indices rather than values
        id_buffer = T.cast(self.X.flatten(), theano.config.floatX)
        # Build sequence of buffer pointers, where buf_ptrs[i] indicates the
        # buffer pointer values *before* computation at timestep *i* proceeds.
        # (This means we need to slice off the last actual buf_ptr output and
        # prepend a dummy.)
        buf_ptrs = T.concatenate([T.zeros((1, batch_size,)),
                                  self.buf_ptrs[:-1]], axis=0)

        sequences = [ts_f, transitions_f, self.stack_2_ptrs, buf_ptrs]
        outputs_info = [T.zeros_like(self.embeddings)]

        # The `patternbroadcast` call below prevents any of the alloc axes from
        # being broadcastable. (Trust the client -- they should give us the
        # parameter shapes exactly, of course!)
        outputs_info += [T.patternbroadcast(T.zeros(shape), (False,) * len(shape))
                         for shape in wrt_shapes]

        # bwd stacks
        non_sequences = [self.stack_bwd] + extra_bwd_init
        # auxiliary data
        non_sequences += [id_buffer, self.final_stack]
        # more helpers (not referenced directly in code, but we need to include
        # them as non-sequences to satisfy scan strict mode)
        non_sequences += [self.buffer_t] + extra_inputs
        non_sequences += self._vs.vars.values()

        bscan_ret, self.bscan_updates = theano.scan(
                step_b, sequences, outputs_info, non_sequences,
                go_backwards=True,
                strict=True,
                name="stack_bwd")

        self.deltas = [deltas[-1] for deltas in bscan_ret[1:]]
        self.dE = bscan_ret[0][-1]


class Model0(HardStack):

    def __init__(self, *args, **kwargs):
        use_tracking_lstm = kwargs.get("use_tracking_lstm", False)
        if use_tracking_lstm:
            kwargs["prediction_and_tracking_network"] = partial(util.TrackingUnit, make_logits=False)
        else:
            kwargs["prediction_and_tracking_network"] = None

        kwargs["predict_transitions"] = False
        kwargs["train_with_predicted_transitions"] = False
        kwargs["interpolate"] = False
        super(Model0, self).__init__(*args, **kwargs)


class Model1(HardStack):

    def __init__(self, *args, **kwargs):
        # Set the tracking unit based on supplied tracking_lstm_hidden_dim.
        use_tracking_lstm = kwargs.get("use_tracking_lstm", False)
        if use_tracking_lstm:
            kwargs["prediction_and_tracking_network"] = util.TrackingUnit
        else:
            kwargs["prediction_and_tracking_network"] = util.Linear
        # Defaults to not using predictions while training and not using scheduled sampling.
        kwargs["predict_transitions"] = True
        kwargs["train_with_predicted_transitions"] = False
        kwargs["interpolate"] = False
        super(Model1, self).__init__(*args, **kwargs)


class Model2(HardStack):

    def __init__(self, *args, **kwargs):
        # Set the tracking unit based on supplied tracking_lstm_hidden_dim.
        use_tracking_lstm = kwargs.get("use_tracking_lstm", False)
        if use_tracking_lstm:
            kwargs["prediction_and_tracking_network"] = util.TrackingUnit
        else:
            kwargs["prediction_and_tracking_network"] = util.Linear
        # Defaults to using predictions while training and not using scheduled sampling.
        kwargs["predict_transitions"] = True
        kwargs["train_with_predicted_transitions"] = True
        kwargs["interpolate"] = False
        super(Model2, self).__init__(*args, **kwargs)


class Model2S(HardStack):

    def __init__(self, *args, **kwargs):
        use_tracking_lstm = kwargs.get("use_tracking_lstm", False)
        if use_tracking_lstm:
            kwargs["prediction_and_tracking_network"] = util.TrackingUnit
        else:
            kwargs["prediction_and_tracking_network"] = util.Linear
        # Use supplied settings and use scheduled sampling.
        kwargs["predict_transitions"] = True
        kwargs["train_with_predicted_transitions"] = True
        kwargs["interpolate"] = True
        super(Model2S, self).__init__(*args, **kwargs)
