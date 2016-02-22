import unittest

import numpy as np
import theano
from theano import tensor as T

from rembed import cuda_util, util
from rembed.stack import ThinStack
from rembed.recurrences import Recurrence, Model0
from rembed.util import VariableStore, CropAndPad, IdentityLayer, batch_subgraph_gradients


class ThinStackTestCase(unittest.TestCase):

    """Basic functional tests for ThinStack with dummy data."""

    def _make_stack(self, seq_length=4):
        self.batch_size = 2
        self.embedding_dim = embedding_dim = 3
        self.vocab_size = vocab_size = 10
        self.seq_length = seq_length

        spec = util.ModelSpec(embedding_dim, embedding_dim, self.batch_size,
                              vocab_size, seq_length)

        def compose_network(inp, inp_dim, outp_dim, vs, name="compose"):
            # Just add the two embeddings!
            W = T.concatenate([T.eye(outp_dim), T.eye(outp_dim)], axis=0)
            return inp.dot(W)

        X = T.imatrix("X")
        transitions = T.imatrix("transitions")
        training_mode = T.scalar("training_mode")
        ground_truth_transitions_visible = T.scalar("ground_truth_transitions_visible", dtype="int32")
        vs = VariableStore()

        # Swap in our own dummy embeddings and weights.
        initial_embeddings = np.arange(vocab_size).reshape(
            (vocab_size, 1)).repeat(embedding_dim, axis=1)

        recurrence = Model0(spec, vs, compose_network)
        self.stack = ThinStack(spec, recurrence, IdentityLayer, training_mode,
                               ground_truth_transitions_visible, vs,
                               X=X,
                               transitions=transitions,
                               make_test_fn=True,
                               initial_embeddings=initial_embeddings,
                               use_input_batch_norm=False,
                               use_input_dropout=False)

    def test_basic_ff(self):
        self._make_stack(4)

        X = np.array([
            [3, 1,  2, 0],
            [3, 2,  4, 5]
        ], dtype=np.int32)

        transitions = np.array([
            # First input: push a bunch onto the stack
            [0, 0, 0, 0],
            # Second input: push, then merge, then push more. (Leaves one item
            # on the buffer.)
            [0, 0, 1, 0]
        ], dtype=np.int32)

        expected = np.array([[ 3.,  3.,  3.],
                             [ 3.,  3.,  3.],
                             [ 1.,  1.,  1.],
                             [ 2.,  2.,  2.],
                             [ 2.,  2.,  2.],
                             [ 5.,  5.,  5.],
                             [ 0.,  0.,  0.],
                             [ 4.,  4.,  4.]])

        self.stack.scan_fn(X, transitions, 1.0, 1)
        ret = self.stack.stack.get_value()
        np.testing.assert_almost_equal(ret, expected)

    def test_with_cropped_data(self):
        dataset = [
            {
                "tokens": [1, 2, 4, 5, 6, 2],
                "transitions": [0, 0, 1, 0, 0, 1, 1, 0, 1, 0, 1]
            },
            {
                "tokens": [6, 1],
                "transitions": [0, 0, 1]
            },
            {
                "tokens": [6, 1, 2, 3, 5, 1],
                "transitions": [0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1]
            }
        ]

        seq_length = 5
        self._make_stack(seq_length)

        dataset = CropAndPad(dataset, seq_length)
        X = np.array([example["tokens"] for example in dataset],
                     dtype=np.int32)
        transitions = np.array([example["transitions"] for example in dataset],
                               dtype=np.int32)
        expected = np.array([[[8, 8, 8],
                              [0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0]],
                             [[7, 7, 7],
                              [0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0]],
                             [[0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0],
                              [0, 0, 0]]])

        ret = self.stack.scan_fn(X, transitions, 1.0, 1)
        np.testing.assert_almost_equal(ret, expected)


class BackpropTestMixin(object):

    def _make_cost(self, stack_top):
        logits = stack_top[:, :self.num_classes]
        costs = T.nnet.categorical_crossentropy(T.nnet.softmax(logits), self.y)
        return costs.mean()


class ThinStackBackpropTestCase(unittest.TestCase, BackpropTestMixin):

    def setUp(self):
        if 'gpu' not in theano.config.device:
            raise RuntimeError("Thin stack only defined for GPU usage")

        self.embedding_dim = self.model_dim = 2
        self.vocab_size = 5
        self.seq_length = 5
        self.batch_size = 2
        self.num_classes = 2

        spec = util.ModelSpec(self.model_dim, self.embedding_dim,
                              self.batch_size, self.vocab_size,
                              self.seq_length)

        self.vs = vs = VariableStore()
        def compose_network(inp, *args, **kwargs):
            W = vs.add_param("W", (self.model_dim * 2, self.model_dim))
            b = vs.add_param("b", (self.model_dim,),
                             initializer=util.ZeroInitializer())
            return T.dot(inp, W) + b

        self.compose_network = compose_network
        recurrence = Model0(spec, vs, compose_network)

        self.X = T.imatrix("X")
        self.transitions = T.imatrix("transitions")
        self.y = T.ivector("y")

        self.stack = ThinStack(spec, recurrence, IdentityLayer, 0.0, 1.0, vs,
                               X=self.X, transitions=self.transitions,
                               use_input_batch_norm=False,
                               use_input_dropout=False)

    def _fake_stack_ff(self):
        """Fake a stack feedforward S S M S M with the given data."""

        # seq_length * batch_size * emb_dim
        X_emb = self.stack.embeddings[self.X].dimshuffle(1, 0, 2)

        z1 = T.concatenate([X_emb[1], X_emb[0]], axis=1)
        c1 = self.compose_network(z1)

        z2 = T.concatenate([X_emb[2], c1], axis=1)
        c2 = self.compose_network(z2)

        return c2

    def test_backprop(self):
        # Simulate a batch of two token sequences, each with the same
        # transition sequence
        X = np.array([[0, 1, 2, 3, 1], [2, 1, 3, 0, 1]], dtype=np.int32)
        y = np.array([1, 0], dtype=np.int32)
        transitions = np.tile([0, 0, 1, 0, 1], (2, 1)).astype(np.int32)

        W, b = self.vs.vars["W"], self.vs.vars["b"]

        simulated_top = self._fake_stack_ff()
        simulated_cost = self._make_cost(simulated_top)
        f_simulated = theano.function(
            [self.X, self.y],
            (simulated_cost, T.grad(simulated_cost, W),
             T.grad(simulated_cost, b),
             T.grad(simulated_cost, self.stack.embeddings)))

        top = self.stack.final_stack[-self.batch_size:]
        cost = self._make_cost(top)
        error_signal = T.grad(cost, top)

        # Build automatic backprop function.
        self.stack.make_backprop_scan(error_signal)
        f = theano.function(
            [self.X, self.transitions, self.y],
            (cost, self.stack.gradients[W], self.stack.gradients[b], self.stack.embedding_gradients),
            updates=self.stack.scan_updates + self.stack.bscan_updates)

        b_cost_sim, b_dW_sim, b_db_sim, b_embedding_gradients_sim = f_simulated(X, y)
        b_cost, b_dW, b_db, b_embedding_gradients = f(X, transitions, y)

        np.testing.assert_almost_equal(b_cost_sim, b_cost)
        np.testing.assert_almost_equal(b_dW_sim, b_dW)
        np.testing.assert_almost_equal(b_db_sim, b_db)
        np.testing.assert_almost_equal(b_embedding_gradients_sim, b_embedding_gradients)


class ThinStackTrackingBackpropTestCase(unittest.TestCase, BackpropTestMixin):

    def setUp(self):
        if 'gpu' not in theano.config.device:
            raise RuntimeError("Thin stack only defined for GPU usage")

        self.embedding_dim = self.model_dim = 2
        self.vocab_size = 5
        self.batch_size = 2
        self.num_classes = 2

        self.vs = VariableStore()

        def compose_network(inp, hidden, *args, **kwargs):
            conc = T.concatenate([hidden, inp], axis=1)

            W = self.vs.add_param("W", (self.model_dim / 2 + self.model_dim * 2, self.model_dim))
            b = self.vs.add_param("b", (self.model_dim,),
                                  initializer=util.ZeroInitializer())
            return T.dot(conc, W) + b

        self.compose_network = compose_network

        self.X = T.imatrix("X")
        self.transitions = T.imatrix("transitions")
        self.y = T.ivector("y")

    def _build(self, length):
        spec = util.ModelSpec(self.model_dim, self.embedding_dim,
                              self.batch_size, self.vocab_size,
                              length)
        recurrence = Model0(spec, self.vs, self.compose_network,
                            use_tracking_lstm=True, tracking_lstm_hidden_dim=1)
        stack = ThinStack(spec, recurrence, IdentityLayer, 0.0, 1.0, self.vs,
                          X=self.X, transitions=self.transitions,
                          use_input_batch_norm=False,
                          use_input_dropout=False)

        return stack

    def _fake_stack_ff(self, stack):
        """Fake a stack feedforward S S M S M S S S M M M with the given data."""

        f_push = lambda *inputs: stack.recurrence(inputs)[0]
        f_merge = lambda *inputs: stack.recurrence(inputs)[1]

        # seq_length * batch_size * emb_dim
        X_emb = stack.embeddings[self.X].dimshuffle(1, 0, 2)
        zero = T.zeros((self.batch_size, self.model_dim))
        t0 = T.zeros((self.batch_size, self.model_dim))

        # Shift.
        t1, = f_push(zero, zero, X_emb[0], t0)

        # Shift.
        t2, = f_push(X_emb[0], zero, X_emb[1], t1)

        # Merge.
        c3, t3 = f_merge(X_emb[1], X_emb[0], X_emb[2], t2)
        self.c3 = c3
        if stack.seq_length <= 3:
            return locals()

        # Shift.
        t4, = f_push(c3, zero, X_emb[2], t3)

        # Merge.
        c5, t5 = f_merge(X_emb[2], c3, X_emb[3], t4)
        if stack.seq_length <= 5:
            return locals()

        t6, = f_push(c5, zero, X_emb[3], t5)

        t7, = f_push(X_emb[3], c5, X_emb[4], t6)

        t8, = f_push(X_emb[4], X_emb[3], X_emb[5], t7)

        c9, t9 = f_merge(X_emb[5], X_emb[4], X_emb[6], t8)

        c10, t10 = f_merge(c9, X_emb[3], X_emb[6], t9)

        c11, t11 = f_merge(c10, c5, X_emb[6], t10)

        return locals()

    def _test_backprop(self, sim_top, stack, X, transitions, y):
        rel_vars = [(name, var) for name, var in self.vs.vars.iteritems()
                    if name != "embeddings"]

        sim_cost = self._make_cost(sim_top)
        all_grads = [T.grad(sim_cost, var) for _, var in rel_vars]
        f_sim = theano.function(
            [self.X, self.y],
            [sim_top, sim_cost, T.grad(sim_cost, stack.embeddings)] + all_grads)

        top = stack.final_stack[-self.batch_size:]
        cost = self._make_cost(top)
        error_signal = T.grad(cost, top)

        stack.make_backprop_scan(error_signal)
        f = theano.function(
            [self.X, self.transitions, self.y],
            [top, cost, stack.embedding_gradients] + [stack.gradients[var] for _, var in rel_vars],
            updates=stack.scan_updates + stack.bscan_updates)

        checks = ["top", "cost", "d/embeddings"] + ["d/%s" % name for name, _ in rel_vars]
        sim = f_sim(X, y)
        real = f(X, transitions, y)

        for check, sim_i, real_i in zip(checks, sim, real):
            np.testing.assert_almost_equal(sim_i, real_i, err_msg=check,
                                           decimal=4, verbose=True)

    def test_backprop_3(self):
        """Check a valid 3-transition S S M sequence."""
        X = np.array([[0, 1, 2], [2, 1, 3]], dtype=np.int32)
        y = np.array([1, 0], dtype=np.int32)
        transitions = np.tile([0, 0, 1], (2, 1)).astype(np.int32)

        stack = self._build(3)
        simulated_top = self._fake_stack_ff(stack)["c3"]

        self._test_backprop(simulated_top, stack, X, transitions, y)

    def test_backprop_5(self):
        # Simulate a batch of two token sequences, each with the same
        # transition sequence
        X = np.array([[0, 1, 2, 3, 1], [2, 1, 3, 0, 1]], dtype=np.int32)
        y = np.array([1, 0], dtype=np.int32)
        transitions = np.tile([0, 0, 1, 0, 1], (2, 1)).astype(np.int32)

        stack = self._build(5)
        simulated_top = self._fake_stack_ff(stack)["c5"]

        self._test_backprop(simulated_top, stack, X, transitions, y)

    def test_backprop_11(self):
        """Check a valid 11-transition S S M S M S S S M M M sequence."""
        X = np.array([[0, 1, 2, 3, 1, 3, 1, 0, 2, 2, 3],
                      [2, 1, 0, 2, 2, 1, 0, 3, 1, 0, 2]], dtype=np.int32)
        y = np.array([1, 0], dtype=np.int32)
        transitions = np.tile([0, 0, 1, 0, 1, 0, 0, 0, 1, 1, 1], (2, 1)).astype(np.int32)

        stack = self._build(11)
        simulated_top = self._fake_stack_ff(stack)["c11"]

        self._test_backprop(simulated_top, stack, X, transitions, y)


class ThinStackTreeLSTMTrackingLSTMBackpropTestCase(ThinStackTrackingBackpropTestCase):

    def setUp(self):
        if 'gpu' not in theano.config.device:
            raise RuntimeError("Thin stack only defined for GPU usage")

        self.embedding_dim = self.model_dim = 2
        self.vocab_size = 5
        self.batch_size = 2
        self.num_classes = 2

        self.vs = VariableStore()
        self.compose_network = util.TreeLSTMLayer

        self.X = T.imatrix("X")
        self.transitions = T.imatrix("transitions")
        self.y = T.ivector("y")

    def _fake_stack_ff(self, stack):
        """Fake a stack feedforward S S M S M S S S M M M with the given data."""

        f_push = lambda *inputs: stack.recurrence(inputs)[0]
        f_merge = lambda *inputs: stack.recurrence(inputs)[1]

        # seq_length * batch_size * emb_dim
        X_emb = stack.embeddings[self.X].dimshuffle(1, 0, 2)
        zero = T.zeros((self.batch_size, self.model_dim))
        t0 = T.zeros((self.batch_size, self.model_dim))

        # Shift.
        t1, = f_push(zero, zero, X_emb[0], t0)

        # Shift.
        t2, = f_push(X_emb[0], zero, X_emb[1], t1)

        # Merge.
        #t2 = theano.gradient.consider_constant(t2)
        c3, t3 = f_merge(X_emb[1], X_emb[0], X_emb[2], t2)
        if stack.seq_length <= 3:
            return locals()

        # Shift.
        t4, = f_push(c3, zero, X_emb[2], t3)

        # Merge.
        c5, t5 = f_merge(X_emb[2], c3, X_emb[3], t4)
        if stack.seq_length <= 5:
            return locals()

        t6, = f_push(c5, zero, X_emb[3], t5)

        t7, = f_push(X_emb[3], c5, X_emb[4], t6)

        t8, = f_push(X_emb[4], X_emb[3], X_emb[5], t7)

        c9, t9 = f_merge(X_emb[5], X_emb[4], X_emb[6], t8)

        c10, t10 = f_merge(c9, X_emb[3], X_emb[6], t9)

        c11, t11 = f_merge(c10, c5, X_emb[6], t10)

        return locals()




if __name__ == '__main__':
    unittest.main()
