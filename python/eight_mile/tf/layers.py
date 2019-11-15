import tensorflow as tf
import numpy as np
from eight_mile.utils import listify, Offsets, wraps, get_version
import math
BASELINE_TF_TRAIN_FLAG = None


def set_tf_log_level(ll):
    #0     | DEBUG            | [Default] Print all messages
    #1     | INFO             | Filter out INFO messages
    #2     | WARNING          | Filter out INFO & WARNING messages
    #3     | ERROR            | Filter out all messages
    import os
    TF_VERSION = get_version(tf)
    if TF_VERSION < 2:
        import tensorflow.logging as tf_logging
    else:
        from absl import logging as tf_logging
    tf_ll = tf_logging.WARN
    tf_cpp_ll = 1
    ll = ll.lower()
    if ll == 'debug':
        tf_ll = tf_logging.DEBUG
        tf_cpp_ll = 0
    if ll == 'info':
        tf_cpp_ll = 0
        tf_ll = tf_logging.INFO
    if ll == 'error':
        tf_ll = tf_logging.ERROR
        tf_cpp_ll = 2
    tf_logging.set_verbosity(tf_ll)
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = f'{tf_cpp_ll}'


def SET_TRAIN_FLAG(X):
    global BASELINE_TF_TRAIN_FLAG
    BASELINE_TF_TRAIN_FLAG = X


def TRAIN_FLAG():
    """Create a global training flag on first use"""
    global BASELINE_TF_TRAIN_FLAG
    if BASELINE_TF_TRAIN_FLAG is not None:
        return BASELINE_TF_TRAIN_FLAG

    BASELINE_TF_TRAIN_FLAG = tf.placeholder_with_default(False, shape=(), name="TRAIN_FLAG")
    return BASELINE_TF_TRAIN_FLAG


# Mapped
def tensor_and_lengths(inputs):
    if isinstance(inputs, (list, tuple)):
        in_tensor, lengths = inputs
    else:
        in_tensor = inputs
        lengths = None  ##tf.reduce_sum(tf.cast(tf.not_equal(inputs, 0), tf.int32), axis=1)

    return in_tensor, lengths

# Get rid of this?
def new_placeholder_dict(train):
    global BASELINE_TF_TRAIN_FLAG

    if train:
        return {BASELINE_TF_TRAIN_FLAG: 1}
    return {}


def gelu(x):
    return 0.5*x*(1+tf.tanh(math.sqrt(2/math.pi)*(x+0.044715*tf.pow(x, 3))))


def swish(x):
    return x*tf.nn.sigmoid(x)


def masked_fill(t, mask, value):
    return t * (1 - tf.cast(mask, t.dtype)) + value * tf.cast(mask, t.dtype)

#https://stackoverflow.com/questions/41897212/how-to-sort-a-multi-dimensional-tensor-using-the-returned-indices-of-tf-nn-top-k
def gather_k(a, b, best_idx, k):
    shape_a = get_shape_as_list(a)
    auxiliary_indices = tf.meshgrid(*[tf.range(d) for d in (tf.unstack(shape_a[:(a.get_shape().ndims - 1)]) + [k])], indexing='ij')

    sorted_b = tf.gather_nd(b, tf.stack(auxiliary_indices[:-1] + [best_idx], axis=-1))
    return sorted_b

def get_shape_as_list(x):
    """
    This function makes sure we get a number whenever possible, and otherwise, gives us back
    a graph operation, but in both cases, presents as a list.  This makes it suitable for a
    bunch of different operations within TF, and hides away some details that we really dont care about, but are
    a PITA to get right...

    Borrowed from Alec Radford:
    https://github.com/openai/finetune-transformer-lm/blob/master/utils.py#L38
    """
    try:
        ps = x.get_shape().as_list()
    except:
        ps = x.shape
    ts = tf.shape(x)
    return [ts[i] if ps[i] is None else ps[i] for i in range(len(ps))]


def bth2bht(t):
    return tf.transpose(t, [0, 2, 1])


def ident(t):
    return t


def tbh2bht(t):
    return tf.tranpose(t, [0, 2, 1])

def tbh2bth(t):
    return tf.transpose(t, [1, 0, 2])


def bth2tbh(t):
    return t.transpose(t, [1, 0, 2])


# Mapped
def get_activation(name='relu'):
    if name is None or name == 'ident':
        return tf.nn.identity
    if name == 'softmax':
        return tf.nn.softmax
    if name == 'tanh':
        return tf.nn.tanh
    if name == 'sigmoid':
        return tf.nn.sigmoid
    if name == 'gelu':
        return gelu
    if name == 'swish':
        return swish
        return tf.identity
    if name == 'leaky_relu':
        return tf.nn.leaky_relu
    return tf.nn.relu


# Mapped
class ConvEncoder(tf.keras.layers.Layer):
    def __init__(self, insz, outsz, filtsz, pdrop, activation='relu'):
        super().__init__()
        self.output_dim = outsz
        self.conv = tf.keras.layers.Conv1D(filters=outsz, kernel_size=filtsz, padding='same')
        self.act = get_activation(activation)
        self.dropout = tf.keras.layers.Dropout(pdrop)

    def call(self, inputs):
        conv_out = self.act(self.conv(inputs))
        return self.dropout(conv_out, TRAIN_FLAG())


# Mapped
class ConvEncoderStack(tf.keras.layers.Layer):

    def __init__(self, insz, outsz, filtsz, pdrop, layers=1, activation='relu'):
        super().__init__()

        first_layer = ConvEncoder(insz, outsz, filtsz, pdrop, activation)
        self.layers.append(first_layer)
        for i in range(layers-1):
            subsequent_layer = ResidualBlock(ConvEncoder(insz, outsz, filtsz, pdrop, activation))
            self.layers.append(subsequent_layer)

    def call(self, inputs):
        for layer in self.layers:
            x = layer(x)
        return x


# Mapped
class ParallelConv(tf.keras.layers.Layer):
    DUMMY_AXIS = 1
    TIME_AXIS = 2
    FEATURE_AXIS = 3

    def __init__(self, insz, outsz, filtsz, activation='relu', name=None, **kwargs):
        """Do parallel convolutions with multiple filter widths and max-over-time pooling.

        :param filtsz: The list of filter widths to use.
        :param dsz: The depths of the input (H).
        :param motsz: The number of conv filters to use (can be an int or a list to allow for various sized filters)
        :param activation: (``str``) The name of the activation function to use (`default='relu`)
        """
        super().__init__(name=name)
        self.Ws = []
        self.bs = []
        self.activation = get_activation(activation)

        motsz = outsz
        if not isinstance(outsz, list):
            motsz = [outsz] * len(filtsz)

        for fsz, cmotsz in zip(filtsz, motsz):
            kernel_shape = [1, int(fsz), int(insz), int(cmotsz)]
            self.Ws.append(self.add_weight('cmot-{}/W'.format(fsz), shape=kernel_shape))
            self.bs.append(self.add_weight('cmot-{}/b'.format(fsz), shape=[cmotsz], initializer=tf.constant_initializer(0.0)))

        self.output_dim = sum(motsz)

    def call(self, inputs):
        """
        :param inputs: The inputs in the shape [B, T, H].
        :return: Combined result
        """
        mots = []
        expanded = tf.expand_dims(inputs, ParallelConv.DUMMY_AXIS)
        for W, b in zip(self.Ws, self.bs):
            conv = tf.nn.conv2d(
                expanded, W,
                strides=[1, 1, 1, 1],
                padding="SAME", name="CONV"
            )
            activation = self.activation(tf.nn.bias_add(conv, b), 'activation')
            mot = tf.reduce_max(activation, [ParallelConv.TIME_AXIS], keepdims=True)
            mots.append(mot)
        combine = tf.reshape(tf.concat(values=mots, axis=ParallelConv.FEATURE_AXIS), [-1, self.output_dim])
        return combine

    def compute_output_shape(self, input_shape):
        return input_shape[0], self.output_dim

    @property
    def requires_length(self):
        return False


def lstm_cell(hsz, forget_bias=1.0, **kwargs):
    """Produce a single cell with no dropout
    :param hsz: (``int``) The number of hidden units per LSTM
    :param forget_bias: (``int``) Defaults to 1
    :return: a cell
    """
    num_proj = kwargs.get('projsz')
    if num_proj and num_proj == hsz:
        num_proj = None
    cell = tf.contrib.rnn.LSTMCell(hsz, forget_bias=forget_bias, state_is_tuple=True, num_proj=num_proj)
    skip_conn = bool(kwargs.get('skip_conn', False))
    return tf.nn.rnn_cell.ResidualWrapper(cell) if skip_conn else cell


def lstm_cell_w_dropout(hsz, pdrop, forget_bias=1.0, variational=False, training=False, **kwargs):
    """Produce a single cell with dropout
    :param hsz: (``int``) The number of hidden units per LSTM
    :param pdrop: (``int``) The probability of keeping a unit value during dropout
    :param forget_bias: (``int``) Defaults to 1
    :param variational (``bool``) variational recurrence is on
    :param training (``bool``) are we training? (defaults to ``False``)
    :return: a cell
    """
    output_keep_prob = tf.contrib.framework.smart_cond(training, lambda: 1.0 - pdrop, lambda: 1.0)
    state_keep_prob = tf.contrib.framework.smart_cond(training, lambda: 1.0 - pdrop if variational else 1.0, lambda: 1.0)
    num_proj = kwargs.get('projsz')
    cell = tf.contrib.rnn.LSTMCell(hsz, forget_bias=forget_bias, state_is_tuple=True, num_proj=num_proj)
    skip_conn = bool(kwargs.get('skip_conn', False))
    cell = tf.nn.rnn_cell.ResidualWrapper(cell) if skip_conn else cell
    output = tf.contrib.rnn.DropoutWrapper(cell,
                                           output_keep_prob=output_keep_prob,
                                           state_keep_prob=state_keep_prob,
                                           variational_recurrent=variational,
                                           dtype=tf.float32)
    return output



class LSTMEncoder2(tf.keras.layers.Layer):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None,
                 dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        """Produce a stack of LSTMs with dropout performed on all but the last layer.

        :param hsz: (``int``) The number of hidden units per LSTM
        :param nlayers: (``int``) The number of layers of LSTMs to stack
        :param pdrop: (``int``) The probability of dropping a unit value during dropout
        :param variational: (``bool``) variational recurrence is on
        :param output_fn: A function that filters output to decide what to return
        :param requires_length: (``bool``) Does the input require an input length (defaults to ``True``)
        :param name: (``str``) Optional, defaults to `None`
        :return: a stacked cell
        """
        super().__init__(name=name)
        self._requires_length = requires_length
        self.rnns = []
        for _ in range(nlayers-1):
            self.rnns.append(tf.keras.layers.LSTM(hsz,
                                                  return_sequences=True,
                                                  recurrent_dropout=pdrop if variational else 0.0,
                                                  dropout=pdrop if not variational else 0.0))
        if nlayers == 1 and not dropout_in_single_layer and not variational:
            pdrop = 0.0
        self.rnns.append(tf.keras.layers.LSTM(hsz,
                                              return_sequences=True,
                                              return_state=True,
                                              recurrent_dropout=pdrop if variational else 0.0,
                                              dropout=pdrop if not variational else 0.0))

    def output_fn(self, output, state):
        """Returns back the output sequence of an RNN and hidden state

        :param output: A temporal vector of output
        :param state: `(output, hidden_last)`, where `hidden_last` = `(h, c)`
        :return:
        """
        return output, state

    def call(self, inputs):
        inputs, lengths = tensor_and_lengths(inputs)
        mask = tf.sequence_mask(lengths)
        for rnn in self.rnns:
            outputs = rnn(inputs, mask=mask)
            inputs = outputs
        rnnout, h, c = outputs
        return self.output_fn(rnnout, (h, c))

    @property
    def requires_length(self):
        return self._requires_length


class LSTMEncoderWithState2(tf.keras.layers.Layer):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, name=None,
                 dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(name=name)
        self._requires_length = False
        self.hsz = hsz
        self.rnns = []
        for _ in range(nlayers-1):
            self.rnns.append(tf.keras.layers.LSTM(hsz,
                                                  return_sequences=True,
                                                  return_state=True,
                                                  recurrent_dropout=pdrop if variational else 0.0,
                                                  dropout=pdrop if not variational else 0.0))
        if nlayers == 1 and not dropout_in_single_layer and not variational:
            pdrop = 0.0
        self.rnns.append(tf.keras.layers.LSTM(hsz,
                                              return_sequences=True,
                                              return_state=True,
                                              recurrent_dropout=pdrop if variational else 0.0,
                                              dropout=pdrop if not variational else 0.0))
        self.requires_state = True

    def call(self, inputs):
        """The format of the output here is

        `output: B, T, H`
        `hidden: List[(h, c), (h, c), ...]`
        :param inputs:
        :return:
        """
        inputs, hidden_state_input = inputs

        hidden_outputs = []
        initial_state = None
        for i, rnn in enumerate(self.rnns):
            if hidden_state_input is not None:
                hidden_state = hidden_state_input[i]
                initial_state = (hidden_state[0], hidden_state[1])
            outputs, h, c = rnn(inputs, initial_state=initial_state)
            hidden_outputs.append((h, c))
            inputs = outputs
        return outputs, hidden_outputs

    def zero_state(self, batchsz):
        num_rnns = len(self.rnns)
        zstate = []
        for i, _ in enumerate(self.rnns):
            zstate.append((np.zeros((batchsz, num_rnns), dtype=np.float32),
                           np.zeros((batchsz, num_rnns), dtype=np.float32)))

        return zstate


class LSTMEncoderSequence2(LSTMEncoder2):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None,
                 dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(insz=insz, hsz=hsz, nlayers=nlayers, pdrop=pdrop, variational=variational,
                         requires_length=requires_length, name=name, dropout_in_single_layer=dropout_in_single_layer,
                         skip_conn=skip_conn, projsz=projsz, **kwargs)

    def output_fn(self, output, state):
        """Return sequence `(BxTxC)`

        :param output: The sequence
        :param state: The hidden state
        :return: The sequence `(BxTxC)`
        """
        return output


class LSTMEncoderHidden2(LSTMEncoder2):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None,
                 dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(insz=insz, hsz=hsz, nlayers=nlayers, pdrop=pdrop, variational=variational,
                         requires_length=requires_length, name=name, dropout_in_single_layer=dropout_in_single_layer,
                         skip_conn=skip_conn, projsz=projsz, **kwargs)

    def output_fn(self, output, state):
        """Return last hidden state `(h, c)`

        :param output: The sequence
        :param state: The hidden state
        :return: The last hidden state `(h, c)`
        """
        return state[0]



class LSTMEncoderHiddenContext2(LSTMEncoder2):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None,
                 dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(insz=insz, hsz=hsz, nlayers=nlayers, pdrop=pdrop, variational=variational,
                         requires_length=requires_length, name=name, dropout_in_single_layer=dropout_in_single_layer,
                         skip_conn=skip_conn, projsz=projsz, **kwargs)

    def output_fn(self, output, state):
        """Return last hidden state `(h, c)`

        :param output: The sequence
        :param state: The hidden state
        :return: The last hidden state `(h, c)`
        """
        return state


class LSTMEncoder1(tf.keras.layers.Layer):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None,
                 dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        """Produce a stack of LSTMs with dropout performed on all but the last layer.

        :param hsz: (``int``) The number of hidden units per LSTM
        :param nlayers: (``int``) The number of layers of LSTMs to stack
        :param pdrop: (``int``) The probability of dropping a unit value during dropout
        :param variational: (``bool``) variational recurrence is on
        :param output_fn: A function that filters output to decide what to return
        :param requires_length: (``bool``) Does the input require an input length (defaults to ``True``)
        :param name: (``str``) Optional, defaults to `None`
        :return: a stacked cell
        """
        super().__init__(name=name)
        self._requires_length = requires_length

        if variational or dropout_in_single_layer:
            self.rnn = tf.contrib.rnn.MultiRNNCell([lstm_cell_w_dropout(hsz, pdrop,
                                                                        variational=variational,
                                                                        training=TRAIN_FLAG(),
                                                                        skip_conn=skip_conn,
                                                                        projsz=projsz) for _ in
                                                    range(nlayers)],
                                                   state_is_tuple=True
                                                   )
        else:
            self.rnn = tf.contrib.rnn.MultiRNNCell(
                [lstm_cell_w_dropout(hsz, pdrop, training=TRAIN_FLAG(),
                                     skip_conn=skip_conn, projsz=projsz) if i < nlayers - 1 else lstm_cell(hsz, skip_conn=skip_conn, projsz=projsz) for i in range(nlayers)],
                state_is_tuple=True
            )

    def call(self, inputs):
        inputs, lengths = tensor_and_lengths(inputs)
        with tf.variable_scope(self._name):
            rnnout, hidden = tf.nn.dynamic_rnn(self.rnn, inputs, sequence_length=lengths, dtype=tf.float32)
        state = (hidden[-1].h, hidden[-1].c)
        return self.output_fn(rnnout, state)

    def output_fn(self, output, state):
        """Returns back the output sequence of an RNN and hidden state

        :param output: A temporal vector of output
        :param hidden: `(output, hidden_last)`, where `hidden_last` = `(h, c)`
        :return:
        """
        return output, state

    @property
    def requires_length(self):
        return self._requires_length


class LSTMEncoderSequence1(LSTMEncoder1):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None,
                 dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(insz=insz, hsz=hsz, nlayers=nlayers, pdrop=pdrop, variational=variational,
                         requires_length=requires_length, name=name, dropout_in_single_layer=dropout_in_single_layer,
                         skip_conn=skip_conn, projsz=projsz, **kwargs)

    def output_fn(self, output, state):
        """Return sequence `(BxTxC)`

        :param output: The sequence
        :param state: The hidden state
        :return: The sequence `(BxTxC)`
        """
        return output


class LSTMEncoderHidden1(LSTMEncoder1):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None,
                 dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(insz=insz, hsz=hsz, nlayers=nlayers, pdrop=pdrop, variational=variational,
                         requires_length=requires_length, name=name, dropout_in_single_layer=dropout_in_single_layer,
                         skip_conn=skip_conn, projsz=projsz, **kwargs)

    def output_fn(self, output, state):
        """Return last hidden state `(h, c)`

        :param output: The sequence
        :param hidden: The hidden state
        :return: The last hidden state `(h, c)`
        """
        return state[0]


class LSTMEncoderHiddenContext1(LSTMEncoder1):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None,
                 dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(insz=insz, hsz=hsz, nlayers=nlayers, pdrop=pdrop, variational=variational,
                         requires_length=requires_length, name=name, dropout_in_single_layer=dropout_in_single_layer,
                         skip_conn=skip_conn, projsz=projsz, **kwargs)

    def output_fn(self, output, state):
        """Return last hidden state `(h, c)`

        :param output: The sequence
        :param hidden: The hidden state
        :return: The last hidden state `(h, c)`
        """
        return state


class LSTMEncoderWithState1(LSTMEncoder1):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, name=None, dropout_in_single_layer=True, **kwargs):
        super().__init__(insz=insz,
                         hsz=hsz,
                         nlayers=nlayers,
                         pdrop=pdrop,
                         variational=variational,
                         requires_length=False,
                         name=name,
                         dropout_in_single_layer=dropout_in_single_layer, **kwargs)
        self.requires_state = True

    def zero_state(self, batchsz):
        return self.rnn.zero_state(batchsz, tf.float32)

    def call(self, inputs):

        inputs, hidden = inputs
        rnnout, hidden = tf.nn.dynamic_rnn(self.rnn, inputs, initial_state=hidden, dtype=tf.float32)
        return rnnout, hidden  # (hidden[-1].h, hidden[-1].c)



class LSTMEncoderAll(tf.keras.layers.Layer):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None, dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(name=name)

        """Produce a stack of LSTMs with dropout performed on all but the last layer.

        :param hsz: (``int``) The number of hidden units per LSTM
        :param nlayers: (``int``) The number of layers of LSTMs to stack
        :param pdrop: (``int``) The probability of dropping a unit value during dropout
        :param variational: (``bool``) variational recurrence is on
        :param requires_length: (``bool``) Does the input require an input length (defaults to ``True``)
        :param name: (``str``) Optional, defaults to `None`
        :return: a stacked cell
        """
        super().__init__(name=name)
        self._requires_length = requires_length
        self.rnns = []
        for _ in range(nlayers-1):
            rnn = tf.keras.layers.LSTM(hsz,
                                       return_sequences=True,
                                       return_state=True,
                                       recurrent_dropout=pdrop if variational else 0.0,
                                       dropout=pdrop if not variational else 0.0)
            self.rnns.append(rnn)
        if nlayers == 1 and not dropout_in_single_layer and not variational:
            pdrop = 0.0
        rnn = tf.keras.layers.LSTM(hsz//2,
                                   return_sequences=True,
                                   return_state=True,
                                   recurrent_dropout=pdrop if variational else 0.0,
                                   dropout=pdrop if not variational else 0.0)

        # This concat mode only works on the sequences, we still are getting 4 objects back for the state
        self.rnns.append(rnn)

    def output_fn(self, rnnout, state):
        return rnnout, state

    def call(self, inputs):
        inputs, lengths = tensor_and_lengths(inputs)
        mask = tf.sequence_mask(lengths)
        # (num_layers * num_directions, batch, hidden_size):
        ## TODO: how to combine this?
        hs = []
        cs = []
        for rnn in self.rnns:
            outputs, h, c = rnn(inputs, mask=mask)
            hs.append(h)
            cs.append(c)
            inputs = outputs

        h = tf.stack(hs)
        c = tf.stack(cs)
        return self.output_fn(outputs, (h, c))

    @property
    def requires_length(self):
        return self._requires_length


class BiLSTMEncoderAll(tf.keras.layers.Layer):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None, dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(name=name)

        """Produce a stack of LSTMs with dropout performed on all but the last layer.

        :param hsz: (``int``) The number of hidden units per LSTM
        :param nlayers: (``int``) The number of layers of LSTMs to stack
        :param pdrop: (``int``) The probability of dropping a unit value during dropout
        :param variational: (``bool``) variational recurrence is on
        :param requires_length: (``bool``) Does the input require an input length (defaults to ``True``)
        :param name: (``str``) Optional, defaults to `None`
        :return: a stacked cell
        """
        super().__init__(name=name)
        self._requires_length = requires_length
        self.rnns = []
        for _ in range(nlayers-1):
            rnn = tf.keras.layers.LSTM(hsz//2,
                                       return_sequences=True,
                                       return_state=True,
                                       recurrent_dropout=pdrop if variational else 0.0,
                                       dropout=pdrop if not variational else 0.0)
            self.rnns.append(tf.keras.layers.Bidirectional(rnn))
        if nlayers == 1 and not dropout_in_single_layer and not variational:
            pdrop = 0.0
        rnn = tf.keras.layers.LSTM(hsz//2,
                                   return_sequences=True,
                                   return_state=True,
                                   recurrent_dropout=pdrop if variational else 0.0,
                                   dropout=pdrop if not variational else 0.0)

        # This concat mode only works on the sequences, we still are getting 4 objects back for the state
        self.rnns.append(tf.keras.layers.Bidirectional(rnn, merge_mode='concat'))

    def output_fn(self, rnnout, state):
        return rnnout, state

    def call(self, inputs):
        inputs, lengths = tensor_and_lengths(inputs)
        mask = tf.sequence_mask(lengths)
        # (num_layers * num_directions, batch, hidden_size):
        hs = []
        cs = []
        for rnn in self.rnns:
            outputs, h1, c1, h2, c2 = rnn(inputs, mask=mask)
            h = tf.stack([h1, h2])
            c = tf.stack([c1, c2])
            hs.append(h)
            cs.append(c)
            inputs = outputs

        _, B, H = get_shape_as_list(h)
        h = tf.reshape(tf.stack(hs), [-1, B, H*2])
        c = tf.reshape(tf.stack(cs), [-1, B, H*2])
        return self.output_fn(outputs, (h, c))

    @property
    def requires_length(self):
        return self._requires_length

# Mapped
class BiLSTMEncoder2(tf.keras.layers.Layer):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None, dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(name=name)

        """Produce a stack of LSTMs with dropout performed on all but the last layer.

        :param hsz: (``int``) The number of hidden units per LSTM
        :param nlayers: (``int``) The number of layers of LSTMs to stack
        :param pdrop: (``int``) The probability of dropping a unit value during dropout
        :param variational: (``bool``) variational recurrence is on
        :param requires_length: (``bool``) Does the input require an input length (defaults to ``True``)
        :param name: (``str``) Optional, defaults to `None`
        :return: a stacked cell
        """
        super().__init__(name=name)
        self._requires_length = requires_length
        self.rnns = []
        for _ in range(nlayers-1):
            rnn = tf.keras.layers.LSTM(hsz//2,
                                       return_sequences=True,
                                       recurrent_dropout=pdrop if variational else 0.0,
                                       dropout=pdrop if not variational else 0.0)
            self.rnns.append(tf.keras.layers.Bidirectional(rnn))
        if nlayers == 1 and not dropout_in_single_layer and not variational:
            pdrop = 0.0
        rnn = tf.keras.layers.LSTM(hsz//2,
                                   return_sequences=True,
                                   return_state=True,
                                   recurrent_dropout=pdrop if variational else 0.0,
                                   dropout=pdrop if not variational else 0.0)

        # This concat mode only works on the sequences, we still are getting 4 objects back for the state
        self.rnns.append(tf.keras.layers.Bidirectional(rnn, merge_mode='concat'))

    def output_fn(self, rnnout, state):
        return rnnout, state

    def call(self, inputs):
        inputs, lengths = tensor_and_lengths(inputs)
        mask = tf.sequence_mask(lengths)
        for rnn in self.rnns:
            outputs = rnn(inputs, mask=mask)
            inputs = outputs

        rnnout, h_fwd, c_fwd, h_bwd, c_bwd = outputs
        return self.output_fn(rnnout, ((h_fwd, c_fwd), (h_bwd, c_bwd)))

    @property
    def requires_length(self):
        return self._requires_length


class BiLSTMEncoderSequence2(BiLSTMEncoder2):
    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None, dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(insz, hsz, nlayers, pdrop, variational, requires_length, name, dropout_in_single_layer, skip_conn, projsz, **kwargs)

    def output_fn(self, rnnout, state):
        return rnnout


class BiLSTMEncoderHidden2(BiLSTMEncoder2):
    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None, dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(insz, hsz, nlayers, pdrop, variational, requires_length, name, dropout_in_single_layer, skip_conn, projsz, **kwargs)

    def output_fn(self, rnnout, state):
        return tf.concat([state[0][0], state[1][0]], axis=-1)


class BiLSTMEncoderHiddenContext2(BiLSTMEncoder2):
    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None, dropout_in_single_layer=False, skip_conn=False, projsz=None, **kwargs):
        super().__init__(insz, hsz, nlayers, pdrop, variational, requires_length, name, dropout_in_single_layer, skip_conn, projsz, **kwargs)

    def output_fn(self, rnnout, state):
        return tuple(tf.concat([state[0][i], state[1][i]], axis=-1) for i in range(2))


# Mapped
class BiLSTMEncoder1(tf.keras.layers.Layer):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None,  skip_conn=False, projsz=None, **kwargs):
        """Produce a stack of LSTMs with dropout performed on all but the last layer.

        :param hsz: (``int``) The number of hidden units per biLSTM (`hsz//2` used for each dir)
        :param nlayers: (``int``) The number of layers of LSTMs to stack
        :param pdrop: (``int``) The probability of dropping a unit value during dropout
        :param variational (``bool``) variational recurrence is on
        :param training (``bool``) Are we training? (defaults to ``False``)
        :return: a stacked cell
        """
        super().__init__(name=name)
        self._requires_length = requires_length
        hsz = hsz // 2
        if variational:
            self.fwd_rnn = tf.contrib.rnn.MultiRNNCell([lstm_cell_w_dropout(hsz, pdrop, variational=variational, training=TRAIN_FLAG(), skip_conn=skip_conn, projsz=projsz) for _ in
                                                        range(nlayers)],
                                                       state_is_tuple=True
                                                       )
            self.bwd_rnn = tf.contrib.rnn.MultiRNNCell(
                [lstm_cell_w_dropout(hsz, pdrop, variational=variational, training=TRAIN_FLAG(), skip_conn=skip_conn, projsz=projsz) for _ in
                 range(nlayers)],
                state_is_tuple=True
            )
        else:
            self.fwd_rnn = tf.contrib.rnn.MultiRNNCell(
                [lstm_cell_w_dropout(hsz, pdrop, training=TRAIN_FLAG(), skip_conn=skip_conn, projsz=projsz) if i < nlayers - 1 else lstm_cell(hsz, skip_conn=skip_conn, projsz=projsz) for i in range(nlayers)],
                state_is_tuple=True
            )
            self.bwd_rnn = tf.contrib.rnn.MultiRNNCell(
                [lstm_cell_w_dropout(hsz, pdrop, training=TRAIN_FLAG(), skip_conn=skip_conn, projsz=projsz) if i < nlayers - 1 else lstm_cell(hsz) for i in
                 range(nlayers)],
                state_is_tuple=True,
            )

    def output_fn(self, rnnout, state):
        return rnnout, state

    def call(self, inputs):
        inputs, lengths = tensor_and_lengths(inputs)
        rnnout, (fwd_state, backward_state) = tf.nn.bidirectional_dynamic_rnn(self.fwd_rnn, self.bwd_rnn, inputs, sequence_length=lengths, dtype=tf.float32)
        rnnout = tf.concat(axis=2, values=rnnout)
        return self.output_fn(rnnout, ((fwd_state[-1].h, fwd_state[-1].c), (backward_state[-1].h, backward_state[-1].c)))

    @property
    def requires_length(self):
        return self._requires_length


class BiLSTMEncoderSequence1(BiLSTMEncoder1):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None,  skip_conn=False, projsz=None, **kwargs):
        """Produce a stack of LSTMs with dropout performed on all but the last layer.

        :param hsz: (``int``) The number of hidden units per LSTM
        :param nlayers: (``int``) The number of layers of LSTMs to stack
        :param pdrop: (``int``) The probability of dropping a unit value during dropout
        :param variational (``bool``) variational recurrence is on
        :param training (``bool``) Are we training? (defaults to ``False``)
        :return: a stacked cell
        """
        super().__init__(insz, hsz, nlayers, pdrop, variational, requires_length, name, skip_conn, projsz, **kwargs)

    def output_fn(self, rnnout, state):
        return rnnout


class BiLSTMEncoderHidden1(BiLSTMEncoder1):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None,  skip_conn=False, projsz=None, **kwargs):
        """Produce a stack of LSTMs with dropout performed on all but the last layer.

        :param hsz: (``int``) The number of hidden units per LSTM
        :param nlayers: (``int``) The number of layers of LSTMs to stack
        :param pdrop: (``int``) The probability of dropping a unit value during dropout
        :param variational (``bool``) variational recurrence is on
        :param training (``bool``) Are we training? (defaults to ``False``)
        :return: a stacked cell
        """
        super().__init__(insz, hsz, nlayers, pdrop, variational, requires_length, name, skip_conn, projsz, **kwargs)

    def output_fn(self, rnnout, state):
        return tf.concat([state[0][0], state[1][0]], axis=-1)


class BiLSTMEncoderHiddenContext1(BiLSTMEncoder1):

    def __init__(self, insz, hsz, nlayers, pdrop=0.0, variational=False, requires_length=True, name=None,  skip_conn=False, projsz=None, **kwargs):
        """Produce a stack of LSTMs with dropout performed on all but the last layer.

        :param hsz: (``int``) The number of hidden units per LSTM
        :param nlayers: (``int``) The number of layers of LSTMs to stack
        :param pdrop: (``int``) The probability of dropping a unit value during dropout
        :param variational (``bool``) variational recurrence is on
        :param training (``bool``) Are we training? (defaults to ``False``)
        :return: a stacked cell
        """
        super().__init__(insz, hsz, nlayers, pdrop, variational, requires_length, name, skip_conn, projsz, **kwargs)

    def output_fn(self, rnnout, state):
        return state

if get_version(tf) < 2:
    LSTMEncoder = LSTMEncoder1
    LSTMEncoderSequence = LSTMEncoderSequence1
    LSTMEncoderWithState = LSTMEncoderWithState1
    LSTMEncoderHidden = LSTMEncoderHidden1
    LSTMEncoderHiddenContext = LSTMEncoderHiddenContext1
    BiLSTMEncoder = BiLSTMEncoder1
    BiLSTMEncoderSequence = BiLSTMEncoderSequence1
    BiLSTMEncoderHidden = BiLSTMEncoderHidden1
    BiLSTMEncoderHiddenContext = BiLSTMEncoderHiddenContext1
    from tensorflow.contrib.crf import crf_decode, crf_log_norm, crf_unary_score, crf_binary_score
    def crf_sequence_score(inputs, tag_indices, sequence_lengths, transition_params):
        """Computes the unnormalized score for a tag sequence.

        This is a patched version of the contrib
        where we dont do any length 1 sequence optimizations.  This was causing a very odd error
        where the true branch of smart_cond was being executed despite the predicate evaluating to 0.
        This probably makes it even slower than usual :(

        Args:
          inputs: A [batch_size, max_seq_len, num_tags] tensor of unary potentials
              to use as input to the CRF layer.
          tag_indices: A [batch_size, max_seq_len] matrix of tag indices for which we
              compute the unnormalized score.
          sequence_lengths: A [batch_size] vector of true sequence lengths.
          transition_params: A [num_tags, num_tags] transition matrix.
        Returns:
          sequence_scores: A [batch_size] vector of unnormalized sequence scores.
        """

        # Compute the scores of the given tag sequence.
        unary_scores = crf_unary_score(tag_indices, sequence_lengths, inputs)
        binary_scores = crf_binary_score(tag_indices, sequence_lengths, transition_params)
        sequence_scores = unary_scores + binary_scores
        return sequence_scores
else:
    LSTMEncoder = LSTMEncoder2
    LSTMEncoderSequence = LSTMEncoderSequence2
    LSTMEncoderWithState = LSTMEncoderWithState2
    LSTMEncoderHidden = LSTMEncoderHidden2
    LSTMEncoderHiddenContext = LSTMEncoderHiddenContext2
    BiLSTMEncoder = BiLSTMEncoder2
    BiLSTMEncoderSequence = BiLSTMEncoderSequence2
    BiLSTMEncoderHidden = BiLSTMEncoderHidden2
    BiLSTMEncoderHiddenContext = BiLSTMEncoderHiddenContext2
    from tensorflow_addons.text.crf import crf_decode, crf_sequence_score, crf_log_norm


class EmbeddingsStack(tf.keras.layers.Layer):

    def __init__(self, embeddings_dict, dropout_rate=0.0, requires_length=False, name=None, **kwargs):
        """Takes in a dictionary where the keys are the input tensor names, and the values are the embeddings

        :param embeddings_dict: (``dict``) dictionary of each feature embedding
        """

        super().__init__(name=name)
        self.embeddings = embeddings_dict
        self.dropout = tf.keras.layers.Dropout(dropout_rate)
        self._requires_length = requires_length

    def items(self):
        return self.embeddings.items()

    def call(self, inputs):
        """This method performs "embedding" of the inputs.  The base method here then concatenates along depth
        dimension to form word embeddings

        :return: A 3-d vector where the last dimension is the concatenated dimensions of all embeddings
        """
        all_embeddings_out = []
        for k, embedding in self.embeddings.items():
            x = inputs[k]
            embeddings_out = embedding(x)
            all_embeddings_out.append(embeddings_out)
        word_embeddings = tf.concat(values=all_embeddings_out, axis=-1)
        return self.dropout(word_embeddings, TRAIN_FLAG())

    @property
    def dsz(self):
        total_dsz = 0
        for embeddings in self.embeddings.values():
            total_dsz += embeddings.get_dsz()
        return total_dsz

    @property
    def requires_length(self):
        return self.requires_length

    @property
    def output_dim(self):
        return self.dsz


class DenseStack(tf.keras.layers.Layer):

    def __init__(self, insz, hsz, activation='relu', pdrop_value=0.5, init=None, name=None, **kwargs):
        """Stack 1 or more hidden layers, optionally (forming an MLP)

        :param hsz: (``int``) The number of hidden units
        :param activation:  (``str``) The name of the activation function to use
        :param pdrop_value: (``float``) The dropout probability
        :param init: The tensorflow initializer

        """
        super().__init__(name=name)
        hszs = listify(hsz)
        self.layer_stack = [tf.keras.layers.Dense(hsz, kernel_initializer=init, activation=activation) for hsz in hszs]
        self.dropout = tf.keras.layers.Dropout(pdrop_value)

    def call(self, inputs):
        """Stack 1 or more hidden layers, optionally (forming an MLP)

        :param inputs: The fixed representation of the model
        :param training: (``bool``) A boolean specifying if we are training or not
        :param init: The tensorflow initializer
        :param kwargs: See below

        :Keyword Arguments:
        * *hsz* -- (``int``) The number of hidden units (defaults to `100`)

        :return: The final layer
        """
        x = inputs
        for layer in self.layer_stack:
            x = layer(x)
            x = self.dropout(x, TRAIN_FLAG())
        return x

    @property
    def requires_length(self):
        return False


class WithDropout(tf.keras.layers.Layer):

    def __init__(self, layer, pdrop=0.5):
        super(WithDropout, self).__init__()
        self.layer = layer
        self.dropout = tf.keras.layers.Dropout(pdrop)

    def call(self, inputs):
        return self.dropout(self.layer(inputs), TRAIN_FLAG())

    @property
    def output_dim(self):
        return self.layer.output_dim


class Highway(tf.keras.layers.Layer):

    def __init__(self, input_size, name=None, **kwargs):
        super(Highway, self).__init__(name=name)
        self.proj = tf.keras.layers.Dense(input_size, activation='relu')
        self.transform = tf.keras.layers.Dense(input_size, bias_initializer=tf.keras.initializers.Constant(value=-2.0), activation='sigmoid')

    def call(self, inputs):
        proj_result = self.proj(inputs)
        proj_gate = self.transform(inputs)
        gated = (proj_gate * proj_result) + ((1 - proj_gate) * inputs)
        return gated

    @property
    def requires_length(self):
        return False


class ResidualBlock(tf.keras.layers.Layer):

    def __init__(self, layer=None, name=None, **kwargs):
        super(ResidualBlock, self).__init__(name=name)
        self.layer = layer

    def call(self, inputs):
        return inputs + self.layer(inputs)

    @property
    def requires_length(self):
        return False


class SkipConnection(ResidualBlock):

    def __init__(self, input_size, activation='relu'):
        super(SkipConnection, self).__init__(tf.keras.layers.Dense(input_size, activation=activation))


class TimeDistributedProjection(tf.keras.layers.Layer):

    def __init__(self, num_outputs, name=None):
        """Set up a low-order projection (embedding) by flattening the batch and time dims and matmul

        :param name: The name for this scope
        :param num_outputs: The number of feature maps out
        """
        super().__init__(True, name)
        self.output_dim = num_outputs
        self.W = None
        self.b = None

    def build(self, input_shape):

        nx = int(input_shape[-1])
        self.W = self.add_weight("W", [nx, self.output_dim])
        self.b = self.add_weight("b", [self.output_dim], initializer=tf.constant_initializer(0.0))
        super().build(input_shape)

    def call(self, inputs):
        """Low-order projection (embedding) by flattening the batch and time dims and matmul

        :param inputs: The input tensor
        :return: An output tensor having the same dims as the input, except the last which is `output_dim`
        """
        input_shape = get_shape_as_list(inputs)
        collapse = tf.reshape(inputs, [-1, input_shape[-1]])
        c = tf.matmul(collapse, self.W) + self.b
        c = tf.reshape(c, input_shape[:-1] + [self.output_dim])
        return c

    def compute_output_shape(self, input_shape):
        return input_shape[0], self.output_dim

    @property
    def requires_length(self):
        return False

def split_heads(x, num_heads):
    shp = get_shape_as_list(x)
    dsz = shp[-1]
    r = tf.reshape(x, shp[:-1] + [num_heads, dsz // num_heads])
    # (B, T, num_heads, d_k) -> (B, num_heads, T, d_k)
    return tf.transpose(r, [0, 2, 1, 3])


def combine_heads(x):
    x = tf.transpose(x, [0, 2, 1, 3])
    shp = get_shape_as_list(x)
    num_heads, head_sz = shp[-2:]
    new_x_shape = shp[:-2]+[num_heads * head_sz]
    new_x = tf.reshape(x, new_x_shape)
    return new_x


class MultiHeadedAttention(tf.keras.layers.Layer):
    """
    Multi-headed attention from https://arxiv.org/abs/1706.03762 via http://nlp.seas.harvard.edu/2018/04/03/attention.html

    Multi-headed attention provides multiple looks of low-order projections K, Q and V using an attention function
    (specifically `scaled_dot_product_attention` in the paper.  This allows multiple relationships to be illuminated
    via attention on different positional and representational information from each head.

    The number of heads `h` times the low-order projection dim `d_k` is equal to `d_model` (which is asserted upfront).
    This means that each weight matrix can be simply represented as a linear transformation from `d_model` to `d_model`,
    and partitioned into heads after the fact.

    Finally, an output projection is applied which brings the output space back to `d_model`, in preparation for the
    sub-sequent `FFN` sub-layer.

    There are 3 uses of multi-head attention in the Transformer.
    For encoder-decoder layers, the queries come from the previous decoder layer, and the memory keys come from
    the encoder.  For encoder layers, the K, Q and V all come from the output of the previous layer of the encoder.
    And for self-attention in the decoder, K, Q and V all come from the decoder, but here it is masked to prevent using
    future values
    """
    def __init__(self, num_heads, d_model, dropout=0.1, scale=False):
        """Constructor for multi-headed attention

        :param h: The number of heads
        :param d_model: The model hidden size
        :param dropout (``float``): The amount of dropout to use
        :param attn_fn: A function to apply attention, defaults to SDP
        """
        super().__init__()
        assert d_model % num_heads == 0
        self.d_k = d_model // num_heads
        self.h = num_heads
        self.w_Q = tf.keras.layers.Dense(units=d_model)
        self.w_K = tf.keras.layers.Dense(units=d_model)
        self.w_V = tf.keras.layers.Dense(units=d_model)
        self.w_O = tf.keras.layers.Dense(units=d_model)
        self.attn_fn = self._scaled_dot_product_attention if scale else self._dot_product_attention
        self.attn = None
        self.dropout = tf.keras.layers.Dropout(dropout)

    def _scaled_dot_product_attention(self, query, key, value, mask=None):
        w = tf.matmul(query, key, transpose_b=True)

        w *= tf.rsqrt(tf.cast(tf.shape(query)[2], tf.float32))

        if mask is not None:
            w = w * mask + -1e9 * (1 - mask)

        weights = tf.nn.softmax(w, name="attention_weights")
        weights = self.dropout(weights, training=TRAIN_FLAG())
        return tf.matmul(weights, value), weights

    def _dot_product_attention(self, query, key, value, mask=None):
        w = tf.matmul(query, key, transpose_b=True)

        if mask is not None:
            w = w * mask + -1e9 * (1 - mask)

        weights = tf.nn.softmax(w, name="attention_weights")
        weights = self.dropout(weights, training=TRAIN_FLAG())
        return tf.matmul(weights, value), weights

    def call(self, qkvm):
        query, key, value, mask = qkvm

        # (B, H, T, D)
        query = split_heads(self.w_Q(query), self.h)
        key = split_heads(self.w_K(key), self.h)
        value = split_heads(self.w_V(value), self.h)
        x, self.attn = self.attn_fn(query, key, value, mask=mask)
        x = combine_heads(x)
        return self.w_O(x)


class TransformerEncoder(tf.keras.layers.Layer):
    def __init__(self, d_model, num_heads, pdrop, scale=True, activation_type='relu', d_ff=None, name=None):
        super().__init__(name=name)
        self.d_model = d_model
        self.d_ff = d_ff if d_ff is not None else 4 * d_model
        self.self_attn = MultiHeadedAttention(num_heads, d_model, pdrop, scale=scale)
        self.ffn = FFN(d_model, pdrop, activation_type, d_ff, name='ffn')
        self.ln1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.ln2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.dropout = tf.keras.layers.Dropout(pdrop)

    def call(self, inputs):
        """
        :param inputs: `(x, mask)`
        :return: The output tensor
        """
        x, mask = inputs

        x = self.ln1(x)
        h = self.self_attn((x, x, x, mask))
        x = x + self.dropout(h, TRAIN_FLAG())

        x = self.ln2(x)
        x = x + self.dropout(self.ffn(x), TRAIN_FLAG())
        return x


class TransformerDecoder(tf.keras.layers.Layer):

    def __init__(self, d_model, num_heads, pdrop, scale=True, activation_type='relu', d_ff=None, name=None):
        super().__init__(name=name)
        self.d_model = d_model
        self.d_ff = d_ff if d_ff is not None else 4 * d_model
        self.self_attn = MultiHeadedAttention(num_heads, self.d_model, pdrop, scale=scale)
        self.src_attn = MultiHeadedAttention(num_heads, self.d_model, pdrop, scale=scale)
        self.ffn = FFN(d_model, pdrop, activation_type, d_ff, name='ffn')
        self.ln1 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.ln2 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.ln3 = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        self.dropout = tf.keras.layers.Dropout(pdrop)

    def call(self, inputs):
        x, memory, src_mask, tgt_mask = inputs
        x = self.ln1(x)
        x = x + self.dropout(self.self_attn((x, x, x, tgt_mask)), TRAIN_FLAG())

        x = self.ln2(x)
        x = x + self.dropout(self.src_attn((x, memory, memory, src_mask)), TRAIN_FLAG())

        x = self.ln3(x)
        x = x + self.dropout(self.ffn(x), TRAIN_FLAG())
        return x


class TransformerEncoderStack(tf.keras.layers.Layer):

    def __init__(self, d_model, num_heads, pdrop, scale=True, layers=1, activation_type='relu', d_ff=None, name=None, **kwargs):
        super().__init__(name=name)
        self.encoders = []
        self.ln = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        for i in range(layers):
            self.encoders.append(TransformerEncoder(d_model, num_heads, pdrop, scale, activation_type, d_ff))

    def call(self, inputs):
        x, mask = inputs
        for layer in self.encoders:
            x = layer((x, mask))
        return self.ln(x)


class TransformerDecoderStack(tf.keras.layers.Layer):
    def __init__(self, d_model, num_heads, pdrop, scale=True, layers=1, activation='relu', d_ff=None, name=None, **kwargs):
        super().__init__(name=name)
        self.decoders = []
        self.ln = tf.keras.layers.LayerNormalization(epsilon=1e-6)
        for i in range(layers):
            self.decoders.append(TransformerDecoder(d_model, num_heads, pdrop, scale, activation, d_ff))

    def call(self, inputs):
        x, memory, src_mask, tgt_mask = inputs
        for layer in self.decoders:
            x = layer((x, memory, src_mask, tgt_mask))
        return self.ln(x)


class FFN(tf.keras.layers.Layer):
    """
    FFN from https://arxiv.org/abs/1706.03762 via http://nlp.seas.harvard.edu/2018/04/03/attention.html

    The `FFN` layer is block in the Transformer that follows multi-headed self-attention.  It consists
    of an expansion from `d_model` to `d_ff` (with sub-sequent relu and dropout), followed by a squeeze
    layer that pushes it back to `d_model`.  In the `tensor2tensor` codebase, this is implemented as convolution of
    size 1 over the temporal sequence, which is equivalent, but in PyTorch, we dont need to do anything explicitly,
    thanks to https://github.com/pytorch/pytorch/pull/1935!

    """
    def __init__(self, d_model, pdrop, activation='relu', d_ff=None, name=None):
        """Constructor, takes in model size (which is the external currency of each block) and the feed-forward size

        :param d_model: The model size.  This is the size passed through each block
        :param d_ff: The feed-forward internal size, which is typical 4x larger, used internally
        :param pdrop: The probability of dropping output
        """
        super().__init__(name=name)
        if d_ff is None:
            d_ff = 4 * d_model
        self.expansion = tf.keras.layers.Dense(d_ff)
        self.squeeze = tf.keras.layers.Dense(d_model)
        self.dropout = tf.keras.layers.Dropout(pdrop)
        self.act = tf.keras.layers.Activation(activation)

    def call(self, inputs):
        return self.squeeze(self.dropout(self.act(self.expansion(inputs)), TRAIN_FLAG()))


class TaggerGreedyDecoder(tf.keras.layers.Layer):

    def __init__(self, num_tags, constraint_mask=None, name=None):
        super().__init__(name=name)
        self.num_tags = num_tags
        self.inv_mask = None
        if constraint_mask is not None:
            _, inv_mask = constraint_mask
            self.inv_mask = inv_mask * tf.constant(-1e4)

        self.A = self.add_weight("transitions_raw", shape=(num_tags, num_tags), dtype=tf.float32, init='zeros', trainable=False)

    @property
    def transitions(self):
        if self.inv_mask is not None:
            return tf.nn.log_softmax(self.A + self.inv_mask)
        return self.A

    def neg_log_loss(self, unary, tags, lengths):
        # Cross entropy loss
        mask = tf.sequence_mask(lengths)
        cross_entropy = tf.one_hot(tags, self.num_tags, axis=-1) * tf.log(tf.nn.softmax(unary))
        cross_entropy = -tf.reduce_sum(cross_entropy, reduction_indices=2)
        cross_entropy *= mask
        cross_entropy = tf.reduce_sum(cross_entropy, axis=1)
        all_loss = tf.reduce_mean(cross_entropy, name="loss")
        return all_loss

    def call(self, inputs, training=False, mask=None):

        unary, lengths = inputs

        if self.inv_mask is not None:
            bsz = tf.shape(unary)[0]
            lsz = self.num_tags
            np_gos = np.full((1, 1, lsz), -1e4, dtype=np.float32)
            np_gos[:, :, Offsets.GO] = 0
            gos = tf.constant(np_gos)
            start = tf.tile(gos, [bsz, 1, 1])
            probv = tf.concat([start, unary], axis=1)
            viterbi, _ = crf_decode(probv, self.transitions, lengths + 1)
            return tf.identity(viterbi[:, 1:], name="best")
        else:
            return tf.argmax(self.probs, 2, name="best")


class CRF(tf.keras.layers.Layer):

    def __init__(self, num_tags, constraint_mask=None, name=None):
        """Initialize the object.
        :param num_tags: int, The number of tags in your output (emission size)
        :param constraint_mask: torch.ByteTensor, Constraints on the transitions [1, N, N]
        :param name: str, Optional name, defaults to `None`
        """
        super().__init__(name=name)

        self.A = self.add_weight("transitions_raw", shape=(num_tags, num_tags), dtype=tf.float32)
        self.num_tags = num_tags
        self.mask = None
        self.inv_mask = None
        if constraint_mask is not None:
            self.mask, inv_mask = constraint_mask
            self.inv_mask = inv_mask * tf.constant(-1e4)

    @property
    def transitions(self):
        if self.inv_mask is not None:
            return (self.A * self.mask) + self.inv_mask
        return self.A

    def score_sentence(self, unary, tags, lengths):
        """Score a batch of sentences.

        :param unary: torch.FloatTensor: [B, T, N]
        :param tags: torch.LongTensor: [B, T]
        :param lengths: torch.LongTensor: [B]

        :return: torch.FloatTensor: [B]
        """
        #unary = tf.Print(unary, [tf.shape(unary)])
        #tags = tf.Print(tags, [tf.shape(tags)])
        return crf_sequence_score(unary, tf.cast(tags, tf.int32), tf.cast(lengths, tf.int32), self.transitions)

    def call(self, inputs, training=False):

        unary, lengths = inputs
        if training:
            return crf_log_norm(unary, lengths, self.transitions)
        else:
            return self.decode(unary, lengths)

    def decode(self, unary, lengths):
        """Do Viterbi decode on a batch.

        :param unary: torch.FloatTensor: [T, B, N] or [B, T, N]
        :param lengths: torch.LongTensor: [B]

        :return: List[torch.LongTensor]: [B] the paths
        :return: torch.FloatTensor: [B] the path score
        """
        bsz = tf.shape(unary)[0]
        lsz = self.num_tags
        np_gos = np.full((1, 1, lsz), -1e4, dtype=np.float32)
        np_gos[:, :, Offsets.GO] = 0
        gos = tf.constant(np_gos)

        start = tf.tile(gos, [bsz, 1, 1])
        start = tf.nn.log_softmax(start, axis=-1)

        probv = tf.concat([start, unary], axis=1)

        viterbi, _ = crf_decode(probv, self.transitions, lengths + 1)
        return tf.identity(viterbi[:, 1:], name="best")

    def neg_log_loss(self, unary, tags, lengths):
        """Neg Log Loss with a Batched CRF.

        :param unary: torch.FloatTensor: [T, B, N] or [B, T, N]
        :param tags: torch.LongTensor: [T, B] or [B, T]
        :param lengths: torch.LongTensor: [B]

        :return: torch.FloatTensor: [B]
        """
        fwd_score = self((unary, tf.cast(lengths, tf.int32)), training=True)
        gold_score = self.score_sentence(unary, tags, lengths)
        log_likelihood = gold_score - fwd_score
        return -tf.reduce_mean(log_likelihood)


class MeanPool1D(tf.keras.layers.Layer):
    def __init__(self, dsz, trainable=False, name=None, dtype=tf.float32, batch_first=True, *args, **kwargs):
        """This is a layers the calculates the mean pooling in a length awareway.

           This was originally a wrapper around tf.keras.layers.GlobalAveragePooling1D()
           but that had problems because the mask didn't work then the dimension we
           are pooling over was variable length.

           looking here https://github.com/tensorflow/tensorflow/blob/1cf0898dd4331baf93fe77205550f2c2e6c90ee5/tensorflow/python/keras/layers/pooling.py#L639

           We can see that the input shape is being gotten as a list where for the
           value of `input_shape[step_axis]` is `None` instead of getting the shape
           via `tf.shape`. This means that when they do the reshape the
           broadcast_shape is `[-1, None, 1]` which causes an error.
        """
        super().__init__(trainable, name, dtype)
        self.output_dim = dsz
        self.reduction_dim = 1 if batch_first else 0

    def call(self, inputs):
        tensor, lengths = tensor_and_lengths(inputs)
        # Regardless of whether the input is batch first or time first the result of the
        # sum is `[B, H]` so the lengths (which is `[B]`) should always be expanded with
        # `-1` to `[B, -1]` so that is broadcasts.
        return tf.reduce_sum(tensor, self.reduction_dim) / tf.cast(tf.expand_dims(lengths, -1), tf.float32)

    @property
    def requires_length(self):
        return True


class TagSequenceModel(tf.keras.Model):

    def __init__(self, nc, embeddings, transducer, decoder=None, name=None):
        super().__init__(name=name)
        if isinstance(embeddings, dict):
            self.embed_model = EmbeddingsStack(embeddings)
        else:
            assert isinstance(embeddings, EmbeddingsStack)
            self.embed_model = embeddings
        self.transducer_model = transducer
        self.proj_layer = TimeDistributedProjection(nc)
        decoder_model = CRF(nc) if decoder is None else decoder
        self.decoder_model = decoder_model

    def transduce(self, inputs):
        lengths = inputs.get('lengths')

        embedded = self.embed_model(inputs)
        embedded = (embedded, lengths)
        transduced = self.proj_layer(self.transducer_model(embedded))
        return transduced

    def decode(self, transduced, lengths):
        return self.decoder_model((transduced, lengths))

    def call(self, inputs, training=None):
        transduced = self.transduce(inputs)
        return self.decode(transduced, inputs.get('lengths'))

    def neg_log_loss(self, unary, tags, lengths):
        return self.decoder_model.neg_log_loss(unary, tags, lengths)


class LangSequenceModel(tf.keras.Model):

    def __init__(self, nc, embeddings, transducer, decoder=None, name=None):
        super().__init__(name=name)
        if isinstance(embeddings, dict):
            self.embed_model = EmbeddingsStack(embeddings)
        else:
            assert isinstance(embeddings, EmbeddingsStack)
            self.embed_model = embeddings
        self.transducer_model = transducer
        if hasattr(transducer, 'requires_state') and transducer.requires_state:
            self._call = self._call_with_state
            self.requires_state = True
        else:
            self._call = self._call_without_state
            self.requires_state = False
        self.output_layer = TimeDistributedProjection(nc)
        self.decoder_model = decoder

    def call(self, inputs):
        return self._call(inputs)

    def _call_with_state(self, inputs):

        h = inputs.get('h')

        embedded = self.embed_model(inputs)
        transduced, hidden = self.transducer_model((embedded, h))
        transduced = self.output_layer(transduced)
        return transduced, hidden

    def _call_without_state(self, inputs):
        embedded = self.embed_model(inputs)
        transduced = self.transducer_model((embedded))
        transduced = self.output_layer(transduced)
        return transduced, None


class EmbedPoolStackModel(tf.keras.Model):

    def __init__(self, nc, embeddings, pool_model, stack_model=None):
        super().__init__()
        if isinstance(embeddings, dict):
            self.embed_model = EmbeddingsStack(embeddings)
        else:
            assert isinstance(embeddings, EmbeddingsStack)
            self.embed_model = embeddings

        self.pool_requires_length = False
        if hasattr(pool_model, 'requires_length'):
            self.pool_requires_length = pool_model.requires_length
        self.output_layer = tf.keras.layers.Dense(nc)
        self.pool_model = pool_model
        self.stack_model = stack_model

    def call(self, inputs):
        lengths = inputs.get('lengths')

        embedded = self.embed_model(inputs)

        if self.pool_requires_length:
            embedded = (embedded, lengths)
        pooled = self.pool_model(embedded)
        stacked = self.stack_model(pooled) if self.stack_model is not None else pooled
        return self.output_layer(stacked)


class FineTuneModel(tf.keras.Model):

    def __init__(self, nc, embeddings, stack_model=None):
        super().__init__()
        if isinstance(embeddings, dict):
            self.finetuned = EmbeddingsStack(embeddings)
        else:
            assert isinstance(embeddings, EmbeddingsStack)
            self.finetuned = embeddings
        self.stack_model = stack_model
        self.output_layer = tf.keras.layers.Dense(nc)

    def call(self, inputs):
        base_layers = self.finetuned(inputs)
        stacked = self.stack_model(base_layers) if self.stack_model is not None else base_layers
        return self.output_layer(stacked)


class CompositeModel(tf.keras.Model):
    def __init__(self, models):
        super().__init__()
        self.models = models
        self._requires_length = any(getattr(m, 'requires_length', False) for m in self.models)
        # self.output_dim = sum(m.output_dim for m in self.models)

    def call(self, inputs, training=None, mask=None):
        inputs, lengths = tensor_and_lengths(inputs)
        pooled = []
        for m in self.models:
            if getattr(m, 'requires_length', False):
                pooled.append(m((inputs, lengths)))
            else:
                pooled.append(m(inputs))
        return tf.concat(pooled, -1)

    @property
    def requires_length(self):
        return self._requires_length


def highway_conns(inputs, wsz_all, n):
    """Produce one or more highway connection layers

    :param inputs: The sub-graph input
    :param wsz_all: The number of units
    :param n: How many layers of gating
    :return: graph output
    """
    x = inputs
    for i in range(n):
        x = Highway(wsz_all)(x)
    return x


def skip_conns(inputs, wsz_all, n, activation_fn='relu'):
    x = inputs
    for i in range(n):
        x = SkipConnection(wsz_all, activation_fn)(x)
    return x


def parallel_conv(input_, filtsz, dsz, motsz, activation_fn='relu'):
    return ParallelConv(dsz, motsz, filtsz, activation_fn)(input_)


def char_word_conv_embeddings(char_vec, filtsz, char_dsz, nfeats, activation_fn=tf.nn.tanh, gating=skip_conns, num_gates=1):
    """This wrapper takes in a character vector as input and performs parallel convolutions on it, followed by a
    pooling operation and optional residual or highway connections

    :param char_vec: The vector input
    :param filtsz: A list or scalar containing filter sizes for each parallel filter
    :param char_dsz: The character dimension size
    :param nfeats: A list or scalar of the number of pooling units for each filter operation
    :param activation_fn: A function for activation (`tf.nn.tanh` etc)
    :param gating: A gating function to apply to the output
    :param num_gates: The number of gates to apply
    :return: The embedding output, the full number of units
    """
    if isinstance(nfeats, (list, tuple)):
        wsz_all = np.sum(nfeats)
    else:
        wsz_all = len(filtsz) * nfeats
    combine = parallel_conv(char_vec, filtsz, char_dsz, nfeats, activation_fn)
    joined = gating(combine, wsz_all, num_gates)
    return joined, wsz_all

def create_session():
    """This function protects against TF allocating all the memory

    Some combination of cuDNN 7.6 with CUDA 10 on TF 1.13 with RTX cards
    allocate additional memory which isnt available since TF by default
    hogs it all.


    This also provides an abstraction that can be extended later to offer
    more config params that raw `tf.Session()` calls dont

    :return: A `tf.Session`
    """
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    return tf.Session(config=config)


def reload_lower_layers(sess, checkpoint):
    """
    Get the intersection of all non-output layers and declared vars in this graph and restore them

    :param sess: (`tf.Session`) A tensorflow session to restore from
    :param checkpoint: (`str`) checkpoint to read from
    :return: None
    """
    latest = tf.train.latest_checkpoint(checkpoint)
    print('Reloading ' + latest)
    model_vars = set([t[0] for t in tf.train.list_variables(latest)])
    g = tf.get_collection_ref(tf.GraphKeys.GLOBAL_VARIABLES)
    g = [v for v in g if not v.op.name.startswith('OptimizeLoss')]
    g = [v for v in g if not v.op.name.startswith('output/')]
    g = [v for v in g if v.op.name in model_vars]
    saver = tf.train.Saver(g)
    saver.restore(sess, latest)


def tf_device_wrapper(func):
    @wraps(func)
    def with_device(*args, **kwargs):
        device = kwargs.get('device', 'default')
        if device == 'cpu' and 'sess' not in kwargs:
            g = tf.Graph()
            sess = tf.Session(graph=g, config=tf.ConfigProto(allow_soft_placement=True, device_count={'CPU': 1, 'GPU': 0}))
            kwargs['sess'] = sess
            return func(*args, **kwargs)
        return func(*args, **kwargs)
    return with_device


class BaseAttention(tf.keras.layers.Layer):

    def __init__(self, hsz):
        super().__init__()
        self.hsz = hsz
        self.W_c = tf.keras.layers.Dense(hsz, use_bias=False)

    def call(self, qkvm):
        query_t, keys_bth, values_bth, keys_mask = qkvm
        # Output(t) = B x H x 1
        # Keys = B x T x H
        # a = B x T x 1
        a = self._attention(query_t, keys_bth, keys_mask)
        attended = self._update(a, query_t, values_bth)

        return attended

    def _attention(self, query_t, keys_bth, keys_mask):
        pass

    def _update(self, a, query_t, values_bth):
        # a = B x T
        # Want to apply over context, scaled by a
        # (B x 1 x T) (B x T x H) = (B x 1 x H)
        B, H = get_shape_as_list(a)
        a = tf.reshape(a, [B, 1, H])
        c_t = tf.squeeze(a @ values_bth, 1)

        attended = tf.concat([c_t, query_t], -1)
        attended = tf.nn.tanh(self.W_c(attended))
        return attended


class LuongDotProductAttention(BaseAttention):

    def __init__(self, hsz):
        super().__init__(hsz)

    def _attention(self, query_t, keys_bth, keys_mask):
        a = keys_bth @ tf.expand_dims(query_t, 2)
        a = tf.squeeze(a, -1)
        if keys_mask is not None:
            masked_fill(a, keys_mask == 0, -1e9)
        a = tf.nn.softmax(a, axis=-1)
        return a


class ScaledDotProductAttention(BaseAttention):

    def __init__(self, hsz):
        super().__init__(hsz)

    def _attention(self, query_t, keys_bth, keys_mask):
        a = keys_bth @ tf.expand_dims(query_t, 2)
        a = a / math.sqrt(self.hsz)
        a = tf.squeeze(a, -1)
        if keys_mask is not None:
            masked_fill(a, keys_mask == 0, -1e9)
        a = tf.nn.softmax(a, axis=-1)
        return a


class LuongGeneralAttention(BaseAttention):

    def __init__(self, hsz):
        super().__init__(hsz)
        self.W_a = tf.keras.layers.Dense(self.hsz, use_bias=False)

    def _attention(self, query_t, keys_bth, keys_mask):
        a = keys_bth @ tf.expand_dims(self.W_a(query_t), 2)
        a = tf.squeeze(a, -1)
        if keys_mask is not None:
            masked_fill(a, keys_mask == 0, -1e9)
        a = tf.nn.softmax(a, axis=-1)
        return a


class BahdanauAttention(BaseAttention):

    def __init__(self, hsz):
        super().__init__(hsz)
        self.hsz = hsz
        self.W_a = tf.keras.layers.Dense(self.hsz, use_bias=False)
        self.E_a = tf.keras.layers.Dense(self.hsz, use_bias=False)
        self.v = tf.keras.layers.Dense(1, use_bias=False)

    def _attention(self, query_t, keys_bth, keys_mask):
        B, T, H = get_shape_as_list(keys_bth)
        q = tf.reshape(self.W_a(query_t), [B, 1, H])
        u = self.E_a(keys_bth)

        z = tf.nn.tanh(q + u)
        a = tf.squeeze(self.v(z), -1)

        if keys_mask is not None:
            masked_fill(a, keys_mask == 0, -1e9)
        a = tf.nn.softmax(a, axis=-1)
        return a

    def _update(self, a, query_t, values_bth):
        # a = B x T
        # Want to apply over context, scaled by a
        # (B x 1 x T) (B x T x H) = (B x 1 x H) -> (B x H)
        # context_vector shape after sum == (batch_size, hidden_size)

        B, T_k = get_shape_as_list(a)
        a = tf.reshape(a, [B, 1, T_k])
        c_t = tf.squeeze(a @ values_bth, 1)
        attended = tf.concat([c_t, query_t], -1)
        attended = self.W_c(attended)
        return attended



def gnmt_length_penalty(lengths, alpha=0.8):
    """Calculate a length penalty from https://arxiv.org/pdf/1609.08144.pdf

    The paper states the penalty as (5 + |Y|)^a / (5 + 1)^a. This is implemented
    as ((5 + |Y|) / 6)^a for a (very) tiny performance boost

    :param lengths: `np.array`: [B, K] The lengths of the beams.
    :param alpha: `float`: A hyperparameter. See Table 2 for a search on this
        parameter.

    :returns:
        `torch.FloatTensor`: [B, K, 1] The penalties.
    """
    penalty = tf.constant(np.power(((5.0 + lengths) / 6.0), alpha))
    return tf.expand_dims(penalty, -1)


def no_length_penalty(lengths):
    """A dummy function that returns a no penalty (1)."""
    return tf.expand_dims(np.ones_like(lengths), -1)


def repeat_batch(t, K, dim=0):
    """Repeat a tensor while keeping the concept of a batch.

    :param t: `torch.Tensor`: The tensor to repeat.
    :param K: `int`: The number of times to repeat the tensor.
    :param dim: `int`: The dimension to repeat in. This should be the
        batch dimension.

    :returns: `torch.Tensor`: The repeated tensor. The new shape will be
        batch size * K at dim, the rest of the shapes will be the same.

    Example::

        >>> a = tf.constant(np.arange(10).view(2, -1))
        >>> a
	tensor([[0, 1, 2, 3, 4],
		[5, 6, 7, 8, 9]])
	>>> repeat_batch(a, 2)
	tensor([[0, 1, 2, 3, 4],
		[0, 1, 2, 3, 4],
		[5, 6, 7, 8, 9],
		[5, 6, 7, 8, 9]])
    """
    shape = get_shape_as_list(t)
    tiling = [1] * (len(shape) + 1)
    tiling[dim + 1] = K
    tiled = tf.tile(tf.expand_dims(t, dim + 1), tiling)
    old_bsz = shape[dim]
    new_bsz = old_bsz * K
    new_shape = list(shape[:dim]) + [new_bsz] + list(shape[dim+1:])
    return tf.reshape(tiled, new_shape)


def update_lengths(lengths, eoses, idx):
    """Update the length of a generated tensor based on the first EOS found.

    This is useful for a decoding situation where tokens after an EOS
    can be something other than EOS. This also makes sure that a second
    generated EOS doesn't affect the lengths.

    :param lengths: `torch.LongTensor`: The lengths where zero means an
        unfinished sequence.
    :param eoses:  `torch.ByteTensor`: A mask that has 1 for sequences that
        generated an EOS.
    :param idx: `int`: What value to fill the finished lengths with (normally
        the current decoding timestep).

    :returns: `torch.Tensor`: The updated lengths tensor (same shape and type).
    """
    # If a length is 0 it has never had a length set so it is eligible to have
    # this EOS be the length.
    updatable_lengths = (lengths == 0)
    # If this length can be updated AND this token is an eos
    lengths_mask = updatable_lengths & eoses
    return masked_fill(lengths, lengths_mask, idx)


class BeamSearchBase:

    def __init__(self, beam=1, length_penalty=None, **kwargs):
        self.length_penalty = length_penalty if length_penalty else no_length_penalty
        self.K = beam

    def init(self, encoder_outputs):
        pass

    def step(self, paths, extra):
        pass

    def update(self, beams, extra):
        pass

    def __call__(self, encoder_outputs, **kwargs):
        """Perform batched Beam Search.

        Note:
            The paths and lengths generated do not include the <GO> token.

        :param encoder_outputs: `namedtuple` The outputs of the encoder class.
        :param init: `Callable(ecnoder_outputs: encoder_outputs, K: int)` -> Any: A
            callable that is called once at the start of the search to initialize
            things. This returns a blob that is passed to other callables.
        :param step: `Callable(paths: torch.LongTensor, extra) -> (probs: torch.FloatTensor, extra):
            A callable that is does a single decoding step. It returns the log
            probabilities over the vocabulary in the last dimension. It also returns
            any state the decoding process needs.
        :param update: `Callable(beams: torch.LongTensor, extra) -> extra:
            A callable that is called to edit the decoding state based on the selected
            best beams.
        :param length_penalty: `Callable(lengths: torch.LongTensor) -> torch.floatTensor
            A callable that generates a penalty based on the lengths. Lengths is
            [B, K] and the returned penalty should be [B, K, 1] (or [B, K, V] to
            have token based penalties?)

        :Keyword Arguments:
        * *beam* -- `int`: The number of beams to use.
        * *mxlen* -- `int`: The max number of steps to run the search for.

        :returns:
            tuple(preds: torch.LongTensor, lengths: torch.LongTensor, scores: torch.FloatTensor)
            preds: The predicted values: [B, K, max(lengths)]
            lengths: The length of each prediction [B, K]
            scores: The score of each path [B, K]
        """
        mxlen = kwargs.get('mxlen', 100)
        bsz = get_shape_as_list(encoder_outputs.output)[0]
        extra = self.init(encoder_outputs)
        paths = np.full((bsz, self.K, 1), Offsets.GO)
        # This tracks the log prob of each beam. This is distinct from score which
        # is based on the log prob and penalties.
        log_probs = np.zeros((bsz, self.K))
        # Tracks the lengths of the beams, unfinished beams have a lengths of zero.
        lengths = np.zeros((bsz, self.K), np.int32)

        for i in range(mxlen - 1):
            probs, extra = self.step(paths, extra)
            V = get_shape_as_list(probs)[-1]
            probs = tf.reshape(probs, (bsz, self.K, V))  # [B, K, V]
            if i > 0:
                # This mask is for all beams that are done.
                done_mask = (lengths != 0)  # [B, K, 1]
                done_mask = tf.expand_dims(done_mask, -1)
                # Can creating this mask be moved out of the loop? It never changes but we don't have V
                # This mask selects the EOS token
                eos_mask = np.zeros((1, 1, V))
                eos_mask[:, :, Offsets.EOS] = 1
                # This mask selects the EOS token of only the beams that are done.
                mask = done_mask & eos_mask
                # Put all probability mass on the EOS token for finished beams.
                # Otherwise as the other beams get longer they will all give
                # up and eventually select this beam and all outputs become
                # the same.
                probs = masked_fill(probs, done_mask, -1e8)
                probs = masked_fill(probs, mask, 0)
                probs = tf.expand_dims(log_probs, -1) + probs  # [B, K, V]
                # Calculate the score of the beam based on the current length.
                valid_lengths = masked_fill(lengths, lengths == 0, i+1)
                path_scores = probs / tf.cast(self.length_penalty(valid_lengths), tf.float32)
            else:
                # On the first step we only look at probabilities for the first beam.
                # If we don't then the probs will be the same for each beam
                # This means the same token will be selected for each beam
                # And we won't get any diversity.
                # Using only the first beam ensures K different starting points.
                path_scores = probs[:, 0, :]

            flat_scores = tf.reshape(path_scores, (bsz, -1))  # [B, K * V]
            best_scores, best_idx = tf.math.top_k(flat_scores, self.K)
            # Get the log_probs of the best scoring beams
            probs = tf.reshape(probs, (bsz, -1))
            log_probs = gather_k(flat_scores, probs, best_idx, self.K)
            log_probs = tf.reshape(log_probs, (bsz, self.K))

            best_beams = best_idx // V  # Get which beam it came from
            best_idx = best_idx % V  # Get the index of the word regardless of which beam it is.

            # Best Beam index is relative within the batch (only [0, K)).
            # This makes the index global (e.g. best beams for the second
            # batch example is in [K, 2*K)).
            offsets = np.arange(bsz) * self.K
            offset_beams = tf.cast(best_beams, tf.int64) + tf.expand_dims(offsets, -1)
            flat_beams = tf.reshape(offset_beams, [bsz * self.K])
            # Select the paths to extend based on the best beams
            flat_paths = tf.reshape(paths, [bsz * self.K, -1])
            new_paths = tf.gather(flat_paths, flat_beams)
            new_paths = tf.reshape(new_paths, [bsz, self.K, -1])
            # Add the selected outputs to the paths
            paths = tf.concat([new_paths, tf.expand_dims(tf.cast(best_idx, tf.int64), -1)], axis=2)

            # Select the lengths to keep tracking based on the valid beams left.
            ##
            flat_lengths = tf.reshape(lengths, [-1])
            lengths = tf.gather(flat_lengths, flat_beams)
            lengths = tf.reshape(lengths, (bsz, self.K))
            extra = self.update(flat_beams, extra)

            # Updated lengths based on if we hit EOS
            last = paths[:, :, -1]
            eoses = (last == Offsets.EOS)
            ##
            lengths = update_lengths(lengths, eoses, i + 1)
            if tf.reduce_sum(tf.cast(lengths != 0, np.int32)) == self.K:
                break
        else:
            # This runs if the loop didn't break meaning one beam hit the max len
            # Add an EOS to anything that hasn't hit the end. This makes the scores real.
            probs, extra = self.step(paths, extra)

            V = get_shape_as_list(probs)[-1]
            probs = tf.reshape(probs, (bsz, self.K, V))
            probs = probs[:, :, Offsets.EOS]  # Select the score of EOS
            # If any of the beams are done mask out the score of this EOS (they already had an EOS)
            probs = masked_fill(probs, (lengths != 0), 0)
            log_probs = log_probs + probs
            end_tokens = np.full((bsz, self.K, 1), Offsets.EOS)
            paths = tf.concat([paths, end_tokens], axis=2)
            lengths = update_lengths(lengths, np.ones_like(lengths) == 1, mxlen)
            best_scores = log_probs / tf.cast(tf.squeeze(self.length_penalty(lengths), -1), tf.float32)

        # Slice off the Offsets.GO token
        paths = paths[:, :, 1:]
        return paths, lengths, best_scores



class StackedLSTMCell(tf.keras.layers.AbstractRNNCell):
    def __init__(self, num_layers, input_size, rnn_size, dropout):
        super().__init__()
        self.rnn_size = rnn_size
        self.dropout = tf.keras.layers.Dropout(dropout)
        self.num_layers = num_layers
        self.layers = []

        for i in range(num_layers):
            self.layers.append(tf.keras.layers.LSTMCell(rnn_size, use_bias=False))

    @property
    def state_size(self):
        """size(s) of state(s) used by this cell.

        It can be represented by an Integer, a TensorShape or a tuple of Integers
        or TensorShapes.
        """
        raise NotImplementedError('Abstract method')

    @property
    def output_size(self):
        """Integer or TensorShape: size of outputs produced by this cell."""
        return self.rnn_size

    def call(self, input, hidden):
        h_0, c_0 = hidden
        hs, cs = [], []
        for i, layer in enumerate(self.layers):
            input, (h_i, c_i) = layer(input, (h_0[i], c_0[i]))
            if i != self.num_layers - 1:
                input = self.dropout(input)
            hs.append(h_i)
            cs.append(c_i)

        hs = tf.stack(hs)
        cs = tf.stack(cs)

        return input, (hs, cs)


class StackedGRUCell(tf.keras.layers.AbstractRNNCell):

    def __init__(self, num_layers, input_size, rnn_size, dropout):
        super().__init__()
        self.dropout = tf.keras.layers.Dropout(dropout)
        self.rnn_size = rnn_size
        self.num_layers = num_layers
        self.layers = []

        for i in range(num_layers):
            self.layers.append(tf.keras.layers.GRUCell(rnn_size))

    def call(self, input, hidden):
        h_0 = hidden
        hs = []
        for i, layer in enumerate(self.layers):
            input, h_i = layer(input, h_0)
            if i != self.num_layers:
                input = self.dropout(input)
            hs.append(h_i)

        hs = tf.stack(hs)

        return input, hs

    @property
    def output_size(self):
        """Integer or TensorShape: size of outputs produced by this cell."""
        return self.rnn_size


if get_version(tf) < 2:
    def rnn_cell(hsz, rnntype, st=None):
        """Produce a single RNN cell

        :param hsz: (``int``) The number of hidden units per LSTM
        :param rnntype: (``str``): `lstm` or `gru`
        :param st: (``bool``) state is tuple? defaults to `None`
        :return: a cell
        """
        if st is not None:
            cell = tf.contrib.rnn.LSTMCell(hsz, state_is_tuple=st) if rnntype.endswith('lstm') else tf.contrib.rnn.GRUCell(hsz)
        else:
            cell = tf.contrib.rnn.LSTMCell(hsz) if rnntype.endswith('lstm') else tf.contrib.rnn.GRUCell(hsz)
        return cell

else:

    def rnn_cell(insz, hsz, rnntype, nlayers=1, dropout=0.5):

        if rnntype == 'gru':
            rnn = StackedGRUCell(nlayers, insz, hsz, dropout)
        else:
            rnn = StackedLSTMCell(nlayers, insz, hsz, dropout)
        return rnn
