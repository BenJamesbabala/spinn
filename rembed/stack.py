"""Theano-based stack implementations."""


import theano
from theano import tensor as T

from rembed import util


def update_hard_stack(stack_t, stack_pushed, stack_merged, push_value,
                      merge_value, mask):
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

    # Build two copies of the stack batch: one where every stack has received
    # a push op, and one where every stack has received a merge op.
    #
    # Copy 1: Push.
    stack_pushed = T.set_subtensor(stack_pushed[:, 0], push_value)
    stack_pushed = T.set_subtensor(stack_pushed[:, 1:], stack_t[:, :-1])

    # Copy 2: Merge.
    stack_merged = T.set_subtensor(stack_merged[:, 0], merge_value)
    stack_merged = T.set_subtensor(stack_merged[:, 1:-1], stack_t[:, 2:])

    # Make sure mask broadcasts over all dimensions after the first.
    mask = mask.dimshuffle(0, "x", "x")
    mask = T.cast(mask, dtype=theano.config.floatX)
    stack_next = mask * stack_merged + (1. - mask) * stack_pushed

    return stack_next


class HardStack(object):

    """
    Model 0/1/2 hard stack implementation.

    This model scans a sequence using a hard stack. It optionally predicts
    stack operations using an MLP, and can receive supervision on these
    predictions from some external parser which acts as the "ground truth"
    parser.

    Model 0: predict_network=None, use_predictions=False
    Model 1: predict_network=something, use_predictions=False
    Model 2: predict_network=something, use_predictions=True
    """

    def __init__(self, model_dim, word_embedding_dim, lstm_hidden_dim, vocab_size, seq_length, 
                 compose_network, embedding_projection_network, apply_dropout, vs, predict_network=None,
                 use_predictions=False, X=None, transitions=None, initial_embeddings=None,
                 make_test_fn=False, embedding_dropout_keep_rate=1.0):
        """
        Construct a HardStack.

        Args:
            model_dim: Dimensionality of token embeddings and stack values
            vocab_size: Number of unique tokens in vocabulary
            seq_length: Maximum sequence length which will be processed by this
              stack
            compose_network: Blocks-like function which accepts arguments
              `inp, inp_dim, outp_dim, vs, name` (see e.g. `util.Linear`).
              Given a Theano batch `inp` of dimension `batch_size * inp_dim`,
              returns a transformed Theano batch of dimension
              `batch_size * outp_dim`.
            embedding_projection_network: Same form as `compose_network`.
            apply_dropout: A Theano scalar indicating whether to apply dropout (1.0)
              or eval-mode rescaling (0.0).
            vs: VariableStore instance for parameter storage
            predict_network: Blocks-like function which maps values
              `3 * model_dim` to `action_dim`
            use_predictions: If `True`, use the predictions from the model
              (rather than the ground-truth `transitions`) to perform stack
              operations
            X: Theano batch describing input matrix, or `None` (in which case
              this instance will make its own batch variable).
            transitions: Theano batch describing transition matrix, or `None`
              (in which case this instance will make its own batch variable).
            make_test_fn: If set, create a function to run a scan for testing.
            embedding_dropout_keep_rate: The keep rate for dropout on projected
              embeddings.
        """

        self.model_dim = model_dim
        self.word_embedding_dim = word_embedding_dim
        self.hidden_dim = lstm_hidden_dim
        self.vocab_size = vocab_size
        self.seq_length = seq_length

        self._compose_network = compose_network
        self._embedding_projection_network = embedding_projection_network
        self._predict_network = predict_network
        self.use_predictions = use_predictions

        self._vs = vs

        self.initial_embeddings = initial_embeddings

        self.apply_dropout = apply_dropout
        self.embedding_dropout_keep_rate = embedding_dropout_keep_rate

        self.X = X
        self.transitions = transitions

        self._make_params()
        self._make_inputs()
        self._make_scan()

        if make_test_fn:
            self.scan_fn = theano.function([self.X, self.transitions, self.apply_dropout],
                                           self.final_stack)

    def _make_params(self):
        # Per-token embeddings.
        if self.initial_embeddings is not None:
            def EmbeddingInitializer(shape):
                return self.initial_embeddings
            self.embeddings = self._vs.add_param(
                "embeddings", (self.vocab_size, self.word_embedding_dim), initializer=EmbeddingInitializer)
        else:
            self.embeddings = self._vs.add_param(
                "embeddings", (self.vocab_size, self.word_embedding_dim))

    def _make_inputs(self):
        self.X = self.X or T.imatrix("X")
        self.transitions = self.transitions or T.imatrix("transitions")

    def _make_scan(self):
        """Build the sequential composition / scan graph."""

        batch_size, max_stack_size = self.X.shape

        # Stack batch is a 3D tensor.
        stack_shape = (batch_size, max_stack_size, self.model_dim)
        stack_init = T.zeros(stack_shape)

        # Allocate two helper stack copies (passed as non_seqs into scan).
        stack_pushed = T.zeros(stack_shape)
        stack_merged = T.zeros(stack_shape)

        # Look up all of the embeddings that will be used.
        raw_embeddings = self.embeddings[self.X]  # batch_size * seq_length * emb_dim

        # Allocate a "buffer" stack initialized with projected embeddings,
        # and maintain a cursor in this buffer.
        buffer_t = self._embedding_projection_network(
            raw_embeddings, self.word_embedding_dim, self.model_dim, self._vs, name="project")
        buffer_t = util.Dropout(buffer_t, self.embedding_dropout_keep_rate, self.apply_dropout)

        # Collapse buffer to (batch_size * buffer_size) * emb_dim for fast indexing.
        buffer_t = buffer_t.reshape((-1, self.model_dim))

        buffer_cur_init = T.zeros((batch_size,), dtype="int")

        # Dimshuffle inputs to seq_len * batch_size for scanning
        transitions = self.transitions.dimshuffle(1, 0)

        self.final_stack, self.transitions_pred = self.get_stack_prediction(transitions,
            stack_pushed, stack_merged, buffer_t, stack_init, buffer_cur_init)

    def get_stack_prediction(self, transitions, stack_pushed, stack_merged, 
            buffer_t, stack_init, buffer_cur_init):
        '''
        Returns (final_stack, predicted_transitions) tuple. predicted_transitions
        is None for Model0 and the output of tracking LSTM for Model1/2.
        '''
        raise NotImplementedError("method not implementated")


class Model0(HardStack):

    def __init__(self, *args, **kwargs):
        kwargs["predict_network"] = None
        kwargs["use_predictions"] = False
        super(Model0, self).__init__(*args, **kwargs)

    def _step(self, transitions_t, stack_t, buffer_cur_t, stack_pushed,
             stack_merged, buffer):
        batch_size, _ = self.X.shape
        # Extract top buffer values.
        idxs = buffer_cur_t + (T.arange(batch_size) * self.seq_length)
        buffer_top_t = buffer[idxs]
        mask = transitions_t

        # Now update the stack: first precompute merge results.
        merge_items = stack_t[:, :2].reshape((-1, self.model_dim * 2))
        merge_value = self._compose_network(
            merge_items, self.model_dim * 2, self.model_dim,
            self._vs, name="compose")

        # Compute new stack value.
        stack_next = update_hard_stack(
            stack_t, stack_pushed, stack_merged, buffer_top_t,
            merge_value, mask)

        # Move buffer cursor as necessary. Since mask == 1 when merge, we
        # should increment each buffer cursor by 1 - mask
        buffer_cur_next = buffer_cur_t + (1 - mask)
        return stack_next, buffer_cur_next

    def get_stack_prediction(self, transitions, stack_pushed, stack_merged,
            buffer_t, stack_init, buffer_cur_init):
        outputs_info = [stack_init, buffer_cur_init]

        scan_ret = theano.scan(
            self._step, transitions,
            non_sequences=[stack_pushed, stack_merged, buffer_t],
            outputs_info=outputs_info)[0]

        return scan_ret[0][-1], None


class Model1(HardStack):

    def __init__(self, *args, **kwargs):
        kwargs["predict_network"] = kwargs.get("predict_network", util.TrackingUnit)
        kwargs["use_predictions"] = False
        super(Model1, self).__init__(*args, **kwargs)

    def _step(self, transitions_t, stack_t, buffer_cur_t, hidden_prev, stack_pushed,
             stack_merged, buffer):
        batch_size, _ = self.X.shape
        # Extract top buffer values.
        idxs = buffer_cur_t + (T.arange(batch_size) * self.seq_length)
        buffer_top_t = buffer[idxs]

        predict_inp = T.concatenate(
                [stack_t[:, 0], stack_t[:, 1], buffer_top_t], axis=1)
        hidden, actions_t = self._predict_network(
                hidden_prev, predict_inp, self.model_dim * 3, self.hidden_dim,
                self._vs, name="predict_actions")

        if self.use_predictions:
            # Use predicted actions to build a mask.
            mask = actions_t.argmax(axis=1)
        else:
            # Use transitions provided from external parser.
            mask = transitions_t

        # Now update the stack: first precompute merge results.
        merge_items = stack_t[:, :2].reshape((-1, self.model_dim * 2))
        merge_value = self._compose_network(
            merge_items, self.model_dim * 2, self.model_dim,
            self._vs, name="compose")

        # Compute new stack value.
        stack_next = update_hard_stack(
            stack_t, stack_pushed, stack_merged, buffer_top_t,
            merge_value, mask)

        # Move buffer cursor as necessary. Since mask == 1 when merge, we
        # should increment each buffer cursor by 1 - mask
        buffer_cur_next = buffer_cur_t + (1 - mask)

        return stack_next, buffer_cur_next, hidden, actions_t
        
    def get_stack_prediction(self, transitions, stack_pushed, stack_merged,
            buffer_t, stack_init, buffer_cur_init):
        batch_size, _ = self.X.shape
        hidden_shape = (batch_size, self.hidden_dim * 2)
        hidden_init = T.zeros(hidden_shape)

        outputs_info = [stack_init, buffer_cur_init, hidden_init, None]
        
        scan_ret = theano.scan(
            self._step, transitions,
            non_sequences=[stack_pushed, stack_merged, buffer_t],
            outputs_info=outputs_info)[0]
        return scan_ret[0][-1], scan_ret[3].dimshuffle(1, 0, 2)

class Model2(Model1):

    def __init__(self, *args, **kwargs):
        kwargs["predict_network"] = kwargs.get("predict_network", util.TrackingUnit)
        kwargs["use_predictions"] = True
        super(Model2, self).__init__(*args, **kwargs)
