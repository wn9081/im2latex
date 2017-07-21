#!/usr/bin/env python2
# -*- coding: utf-8 -*-
"""
    Copyright 2017 Sumeet S Singh

    This file is part of im2latex solution by Sumeet S Singh.

    This program is free software: you can redistribute it and/or modify
    it under the terms of the Affero GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    This program is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    Affero GNU General Public License for more details.

    You should have received a copy of the Affero GNU General Public License
    along with this program.  If not, see <http://www.gnu.org/licenses/>.

Created on Sat Jul  8 19:33:38 2017
Tested on python 2.7

@author: Sumeet S Singh
"""

import collections
import itertools
import dl_commons as dlc
import tf_commons as tfc
import tensorflow as tf
from keras.applications.vgg16 import VGG16
from keras import backend as K
from CALSTM import CALSTM, CALSTMState
from hyper_params import Im2LatexModelParams, HYPER

def build_image_context(params, image_batch):
    ## Conv-net
    assert K.int_shape(image_batch) == (params.B,) + params.image_shape
    ################ Build VGG Net ################
    with tf.variable_scope('VGGNet'):
        # K.set_image_data_format('channels_last')
        convnet = VGG16(include_top=False, weights='imagenet', pooling=None, input_shape=params.image_shape)
        convnet.trainable = False
        print 'convnet output_shape = ', convnet.output_shape
        a = convnet(image_batch)
        assert K.int_shape(a) == (params.B, params.H, params.W, params.D)

        ## Combine HxW into a single dimension L
        a = tf.reshape(a, shape=(params.B or -1, params.L, params.D))
        assert K.int_shape(a) == (params.B, params.L, params.D)
    
    return a
        
Im2LatexState = collections.namedtuple('Im2LatexState', ('calstm_state', 'yProbs'))
class Im2LatexModel(tf.nn.rnn_cell.RNNCell):
    """
    One timestep of the decoder model. The entire function can be seen as a complex RNN-cell
    that includes a LSTM stack and an attention model.
    """
    def __init__(self, params, beamsearch_width=1, reuse=None):
        """
        Args:
            params (Im2LatexModelParams)
            beamsearch_width (integer): Only used when inferencing with beamsearch. Otherwise set it to 1.
                Will cause the batch_size in internal assert statements to get multiplied by beamwidth.
            reuse: Passed into the _reuse Tells the underlying RNNs whether or not to reuse the scope.
        """
        self._params = self.C = Im2LatexModelParams(params)
        self.outer_scope = tf.get_variable_scope()
        with tf.variable_scope('Im2LatexRNN') as scope:
            super(Im2LatexModel, self).__init__(_reuse=reuse, _scope=scope, name=scope.name)
            self.rnn_scope = tf.get_variable_scope()

            ## Beam Width to be supplied to BeamsearchDecoder. It essentially broadcasts/tiles a
            ## batch of input from size B to B * BeamWidth. Set this value to 1 in the training
            ## phase.
            self._beamsearch_width = beamsearch_width

            ## Image features from the Conv-Net
            self._im = tf.placeholder(dtype=self.C.dtype, shape=((self.C.B,)+self.C.image_shape), name='image')
            self._a = build_image_context(params, self._im)
            ## self._a = tf.placeholder(dtype=self.C.dtype, shape=(self.C.B, self.C.L, self.C.D), name='a')

            ## First step of x_s is 1 - the begin-sequence token. Shape = (T, B); T==1
            self._x_0 = tf.ones(shape=(1, self.C.B*beamsearch_width), dtype=tf.int32, name='go')

#            if len(self.C.D_RNN) == 1:
#                self._CALSTM_stack = CALSTM(self.C.D_RNN[0], self._a, beamsearch_width, reuse)
#                self._num_calstm_layers = 1
#            else:
            cells = []
            for i, rnn_params in enumerate(self.C.D_RNN, start=1):
                with tf.variable_scope('%d'%i):
                    cells.append(CALSTM(rnn_params, self._a, beamsearch_width, reuse))
            self._CALSTM_stack = tf.nn.rnn_cell.MultiRNNCell(cells)
            self._num_calstm_layers = len(self.C.D_RNN)

            with tf.variable_scope('Ey'):
                self._embedding_matrix = tf.get_variable('Embedding_Matrix', (self.C.K, self.C.m))


    @property
    def BeamWidth(self):
        return self._beamsearch_width

    @property 
    def RuntimeBatchSize(self):
        return self.C.B * self.BeamWidth
    
    def _output_layer(self, Ex_t, h_t, z_t):
        
        ## Renaming HyperParams for convenience
        CONF = self.C
        B = self.C.B*self.BeamWidth
        D = self.C.D
        m = self.C.m
        Kv =self.C.K
        n = self._CALSTM_stack.output_size
        
        assert K.int_shape(Ex_t) == (B, m)
        assert K.int_shape(h_t) == (B, self._CALSTM_stack.output_size)
        assert K.int_shape(z_t) == (B, D)
        
        ## First layer of output MLP
        if CONF.output_follow_paper: ## Follow the paper.
            ## Affine transformation of h_t and z_t from size n/D to bring it down to m
            o_t = tfc.FCLayer({'num_units':m, 'activation_fn':None, 'tb':CONF.tb}, 
                              batch_input_shape=(B,n+D))(tf.concat([h_t, z_t], -1)) # o_t: (B, m)
            ## h_t and z_t are both dimension m now. So they can now be added to Ex_t.
            o_t = o_t + Ex_t # Paper does not multiply this with weights - weird.
            ## non-linearity for the first layer
            o_t = tfc.Activation(CONF, batch_input_shape=(B,m))(o_t)
            dim = m
        else: ## Use a straight MLP Stack
            o_t = K.concatenate((Ex_t, h_t, z_t)) # (B, m+n+D)
            dim = m+n+D

        ## Regular MLP layers
        assert CONF.output_layers.layers_units[-1] == Kv
        logits_t = tfc.MLPStack(CONF.output_layers, batch_input_shape=(B,dim))(o_t)
            
        assert K.int_shape(logits_t) == (B, Kv)
        
        return tf.nn.softmax(logits_t), logits_t

    def zero_state(self, batch_size, dtype):
        return Im2LatexState(self._CALSTM_stack.zero_state(batch_size, dtype),
                             tf.zeros((batch_size, self.C.K), dtype=dtype, name='yProbs'))
    
    def init_state(self):
        
        ################ Initializer MLP ################
        with tf.variable_scope(self.outer_scope):
            with tf.variable_scope('Initializer_MLP'):
    
                ## As per the paper, this is a multi-headed MLP. It has a base stack of common layers, plus
                ## one additional output layer for each of the h and c LSTM states. So if you had
                ## configured - say 3 CALSTM-stacks with 2 LSTM cells per CALSTM-stack you would end-up with
                ## 6 top-layers on top of the base MLP stack. Base MLP stack is specified in param 'init_model'
                a = K.mean(self._a, axis=1) # final shape = (B, D)
                a = tfc.MLPStack(self.C.init_model)(a)

                counter = itertools.count(1)
                def zero_to_init_state(zs, counter):
                    assert isinstance(zs, Im2LatexState)
                    cs = zs.calstm_state
#                    if self._num_calstm_layers == 1:
#                        assert isinstance(cs, CALSTMState)
#                        cs = CALSTM.zero_to_init_state(cs, counter, self.C.init_model, a)
#                    else:
                    ## tuple(CALSTMState1, ...)
                    assert isinstance(cs, tuple) and not isinstance(cs, CALSTMState)
                    lst = []
                    for i in xrange(len(cs)):
                        assert isinstance(cs[i], CALSTMState)
                        lst.append(CALSTM.zero_to_init_state(cs[i], counter, 
                                                             self.C.init_model_final_layers, a))
                    
                    cs = tuple(lst)
                        
                    return zs._replace(calstm_state=cs)
                            
                with tf.variable_scope('Output_Layers'):
                    init_state = self.zero_state(self.C.B*self.BeamWidth, dtype=self.C.dtype)
                    init_state = zero_to_init_state(init_state, counter)

        return init_state
            
    def _embedding_lookup(self, ids):
        m = self.C.m
        assert self._embedding_matrix is not None
        #assert K.int_shape(ids) == (B,)
        shape = list(K.int_shape(ids))
        embedded = tf.nn.embedding_lookup(self._embedding_matrix, ids)
        shape.append(m)
        ## Embedding lookup forgets the leading dimensions (e.g. (B,))
        ## Fix that here.
        embedded.set_shape(shape) # (...,m)
        return embedded
                    
    def call(self, Ex_t, state):
        """ 
        One step of the RNN API of this class.
        Layers a deep-output layer on top of CALSTM
        """
        ## State
        calstm_state_t_1 = state.calstm_state
        ## CALSTM stack
        htop_t, calstm_state_t = self._CALSTM_stack(Ex_t, calstm_state_t_1)
        ## Output layer
        yProbs_t, yLogits_t = self._output_layer(Ex_t, htop_t, calstm_state_t[-1].ztop)
        
        return yLogits_t, Im2LatexState(calstm_state_t, yProbs_t)

    ScanOut = collections.namedtuple('ScanOut', ('yLogits', 'state'))
    def _scan_step_training(self, out_t_1, x_t):
        with tf.variable_scope('Ey'):
            Ex_t = self._embedding_lookup(x_t)

        yLogits_t, state_t = self(Ex_t, out_t_1[1])
        
        return self.ScanOut(yLogits_t, state_t)
    
    def build_train_graph(self):
        ## y_s is the batch of target word sequences
        y_s_p = tf.placeholder(dtype=tf.int32, shape=(self.C.B, None), name='Y_s') # (B, T)
        seq_lens = tf.placeholder(dtype=tf.int32, shape=(self.C.B,), name='seq_lens')
        
        with tf.variable_scope(self.rnn_scope):
            ## tf.scan requires time-dimension to be the first dimension
            y_s = K.permute_dimensions(y_s_p, (1, 0)) # (T, B)
            
            ################ Build x_s ################
            ## x_s is y_s time-delayed by 1 timestep. First token is 1 - the begin-sequence token.
            ## last token of y_s which is <eos> token (zero) will not appear in x_s
            x_s = K.concatenate((self._x_0, y_s[0:-1]), axis=0)
            
            """ Build the training graph of the model """
            accum = self.ScanOut(tf.zeros(shape=(self.RuntimeBatchSize, self.C.K), dtype=self.C.dtype),
                                 self.init_state())
            out_s = tf.scan(self._scan_step_training, x_s, 
                            initializer=accum, swap_memory=True)
            ## yLogits_s, yProbs_s, alpha_s = out_s.yLogits, out_s.state.yProbs, out_s.state.calstm_state.alpha
            ## WARNING: THIS IS ONLY ACCURATE FOR 1 CALSTM LAYER. GATHER ALPHAS OF LOWER CALSTM LAYERS.
            yLogits_s = out_s.yLogits
            alpha_s_n = tf.stack([cs.alpha for cs in out_s.state.calstm_state], axis=0) # (N, T, B, L)
            ## Switch the batch dimension back to first position - (B, T, ...)
            ## yProbs = K.permute_dimensions(yProbs_s, [1,0,2])
            yLogits = K.permute_dimensions(yLogits_s, [1,0,2])
            alpha = K.permute_dimensions(alpha_s_n, [0,2,1,3]) # (N, B, T, L)
            
            return self._optimizer(yLogits, y_s_p, alpha, seq_lens).updated({'y_s': y_s_p, 
                                                                           'seq_lens': seq_lens,
                                                                           'im': self._im})

    def _optimizer(self, yLogits, y_s, alpha, sequence_lengths):
        B = self.C.B
        Kv =self.C.K
        L = self.C.L
        N = self._num_calstm_layers
        
        assert K.int_shape(yLogits) == (B, None, Kv) # (B, T, K)
        assert K.int_shape(alpha) == (N, B, None, L) # (N, B, T, L)
        assert K.int_shape(y_s) == (B, None) # (B, T)
        assert K.int_shape(sequence_lengths) == (B,)
        
        ################ Build Cost Function ################
        with tf.variable_scope('Cost'):
            sequence_mask = tf.sequence_mask(sequence_lengths, maxlen=tf.shape(y_s)[1],
                                             dtype=self.C.dtype) # (B, T)
            assert K.int_shape(sequence_mask) == (B,None) # (B,T)

            ## Masked negative log-likelihood of the sequence.
            ## Note that log(product(p_t)) = sum(log(p_t)) therefore taking log of
            ## joint-sequence-probability is same as taking sum of log of probability at each time-step

            ## Compute Sequence Log-Loss / Log-Likelihood = -Log( product(p_t) ) = -sum(Log(p_t))
            if self.C.sum_logloss:
                ## Here we do not normalize the log-loss across time-steps because the
                ## paper as well as it's source-code do not do that.
                loss_vector = tf.contrib.seq2seq.sequence_loss(logits=yLogits, 
                                                               targets=y_s, 
                                                               weights=sequence_mask, 
                                                               average_across_timesteps=False,
                                                               average_across_batch=True)
                log_likelihood = tf.reduce_sum(loss_vector) # scalar
            else: ## Standard log perplexity (average per-word)
                log_likelihood = tf.contrib.seq2seq.sequence_loss(logits=yLogits, 
                                                               targets=y_s, 
                                                               weights=sequence_mask, 
                                                               average_across_timesteps=True,
                                                               average_across_batch=True)

            alpha_mask =  tf.expand_dims(sequence_mask, axis=2) # (B, T, 1)
            ## Calculate the alpha penalty: lambda * sum_over_i&b(square(C/L - sum_over_t(alpha_i)))
            ## 
            if self.C.MeanSumAlphaEquals1:
                mean_sum_alpha_i = 1.0
            else:
                mean_sum_alpha_i = tf.cast(sequence_lengths, dtype=tf.float32) / self.C.L # (B,)
                mean_sum_alpha_i = tf.expand_dims(mean_sum_alpha_i, axis=1) # (B, 1)

#                sum_over_t = tf.reduce_sum(tf.multiply(alpha,sequence_mask), axis=1, keep_dims=False)# (B, L)
#                squared_diff = tf.squared_difference(sum_over_t, mean_sum_alpha_i) # (B, L)
#                alpha_penalty = self.C.pLambda * tf.reduce_sum(squared_diff, keep_dims=False) # scalar
            sum_over_t = tf.reduce_sum(tf.multiply(alpha, alpha_mask), axis=2, keep_dims=False)# (N, B, L)
            squared_diff = tf.squared_difference(sum_over_t, mean_sum_alpha_i) # (N, B, L)
            alpha_penalty = self.C.pLambda * tf.reduce_sum(squared_diff, keep_dims=False) # scalar
            
            tf.summary.scalar("Loss/log_likelihood", log_likelihood)
            tf.summary.scalar("Loss/alpha-penalty", alpha_penalty)

        ################ Build CTC Cost Function ################
        ## Compute CTC loss/score with intermediate blanks removed. (We've collapsed all blanks in our
        ## train/test sequences to a single space, so our training samples are already as compact as
        ## possible). This will have the following side-effect:
        ##  1) The network will be told that it is okay to omit blanks (spaces) or emit multiple blanks
        ##     since CTC will ignore those. This makes the learning easier, but we'll need to insert blanks
        ##     between tokens at inferencing step.
        with tf.variable_scope('CTC_Cost'):
            ## sparse tensor
            y_idx =    tf.where(tf.not_equal(y_s, 0))
            y_vals =   tf.gather_nd(y_s, y_idx)
            y_sparse = tf.SparseTensor(y_idx, y_vals, tf.shape(y_s, out_type=tf.int64))
            ctc_loss = tf.nn.ctc_loss(y_sparse, yLogits, sequence_lengths,
                           ctc_merge_repeated=False, time_major=False)           
            tf.summary.scalar("Loss/CTC", ctc_loss)

        if self.C.use_ctc_loss:
            cost = ctc_loss + alpha_penalty
        else:
            cost = log_likelihood + alpha_penalty
            
        # Optimizer
        with tf.variable_scope('Optimizer'):
            global_step = tf.get_variable('global_step', dtype=tf.int32, trainable=False, initializer=0)
            optimizer = tf.train.AdamOptimizer()
            train = optimizer.minimize(cost, global_step=global_step)
            ##tf_optimizer = tf.train.GradientDescentOptimizer(tf_rate).minimize(tf_loss, global_step=tf_step, 
            ##                                                               name="optimizer")
        
        return dlc.Properties({
                'train': train,
                'log_likelihood': log_likelihood,
                'ctc_loss': ctc_loss,
                'alpha_penalty': alpha_penalty,
                'cost': cost,
                'global_step':global_step
                })

    def beamsearch(self, x_0):
        """ Build the prediction graph of the model using beamsearch """
        pass
        
def train(batch_iterator):
    graph = tf.Graph()
    with graph.as_default():
        model = Im2LatexModel(HYPER)
        train_ops = model.build_train_graph()
        
        config=tf.ConfigProto(log_device_placement=True)
        config.gpu_options.allow_growth = True

        with tf.Session(config=config) as session:
            print 'Flushing graph to disk'
            tf_sw = tf.summary.FileWriter(tfc.makeTBDir(HYPER.tb), graph=graph)
            tf_sw.flush()
#            tf.initialize_all_variables().run()
            tf.global_variables_initializer().run()
        
            if batch_iterator is None:
                return
            
            for b in batch_iterator:
                if b.step >=2:
                    break
                feed = {train_ops.y_s: b.y_s, train_ops.seq_lens: b.seq_len, train_ops.im: b.im}
                session.run(train_ops.train, feed_dict=feed)
            
